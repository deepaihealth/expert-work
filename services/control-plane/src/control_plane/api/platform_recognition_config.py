"""``/v1/platform/recognition-config`` —— 平台「文档识别」设置(B-209 §5.7)。

system_admin 才能看和改。三个模型 + 一个上限:

* 扫描页识别模型 / 兜底识别模型:只能选模型目录里声明了识别能力(``ocr_caps``)且平台已配
  该厂商密钥的模型;
* 图片描述模型(PR-2 用):必须是能收提示词的看图模型(``vision=True``、不是只做版面解析的
  模型 —— glm-ocr 不收提示词,不能用);
* 单份文档识别页数上限:1~10,000。

某一对两项都空 = 关掉它(扫描页记「待识别」);写法同
:mod:`control_plane.api.platform_judge_config`(``{success,data,error}`` 信封、审计、失效总线)。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api._authz import _principal, platform_only
from control_plane.audit import emit
from control_plane.invalidation_bus import InvalidationEvent
from control_plane.platform_recognition_config import PlatformRecognitionConfigService
from control_plane.platform_secrets import PlatformSecretsService
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.platform_recognition_config import PlatformRecognitionConfigRecord
from expert_work.protocol import (
    PROVIDER_CATALOG,
    AuditAction,
    ModelEntry,
    Principal,
    models_for_provider,
)
from expert_work.runtime.audit.logger import AuditLogger

_PLATFORM_SCOPE_MESSAGE = "only a system admin may manage the platform document-recognition config"

#: Upper bound for the per-document page cap (the largest corpus document was
#: 930 pages; the default is 1,000).
MAX_PAGES_LIMIT = 10_000


class PlatformRecognitionConfigWrite(BaseModel):
    """Write payload. Provider / model **names** only — never secrets. Each
    pair is both-or-neither; neither switches that model off."""

    model_config = ConfigDict(extra="forbid")
    ocr_provider: str | None = None
    ocr_model: str | None = None
    fallback_provider: str | None = None
    fallback_model: str | None = None
    figure_provider: str | None = None
    figure_model: str | None = None
    max_pages: int = Field(ge=1, le=MAX_PAGES_LIMIT)


def _get_service(request: Request) -> PlatformRecognitionConfigService:
    return request.app.state.platform_recognition_config_service  # type: ignore[no-any-return]


def _get_secrets_service(request: Request) -> PlatformSecretsService:
    return request.app.state.platform_secrets_service  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _reads_pages(entry: ModelEntry) -> bool:
    return entry.ocr_caps is not None


def _describes_figures(entry: ModelEntry) -> bool:
    return entry.vision and entry.chat_model


def _options(configured: set[str], fits: Callable[[ModelEntry], bool]) -> list[dict[str, str]]:
    return [
        {"provider": provider, "model": entry.name}
        for provider in PROVIDER_CATALOG
        if provider in configured
        for entry in models_for_provider(provider)
        if fits(entry)
    ]


def _pair(provider: str | None, model: str | None) -> dict[str, str] | None:
    return {"provider": provider, "model": model} if provider and model else None


def _view(record: PlatformRecognitionConfigRecord, *, is_default: bool) -> dict[str, object]:
    return {
        "ocr": _pair(record.ocr_provider, record.ocr_model),
        "fallback": _pair(record.fallback_provider, record.fallback_model),
        "figure": _pair(record.figure_provider, record.figure_model),
        "max_pages": record.max_pages,
        "is_default": is_default,
    }


def _check_pair(
    *,
    role: str,
    provider: str | None,
    model: str | None,
    configured: set[str],
    fits: Callable[[ModelEntry], bool],
    requirement: str,
) -> None:
    if (provider is None) != (model is None):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_RECOGNITION_PAIR",
                "message": f"provide both {role}_provider and {role}_model, or neither",
            },
        )
    if provider is None or model is None:
        return
    if provider not in configured:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "RECOGNITION_PROVIDER_KEY_MISSING",
                "message": f"configure the {provider!r} provider key in platform credentials first",
            },
        )
    entry = next((e for e in models_for_provider(provider) if e.name == model), None)
    if entry is None or not fits(entry):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_RECOGNITION_MODEL",
                "message": f"{provider}/{model} is not {requirement}",
            },
        )


def build_platform_recognition_config_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/platform/recognition-config",
        tags=["platform_config"],
        dependencies=[Depends(platform_only(_PLATFORM_SCOPE_MESSAGE))],
    )

    @router.get("")
    async def get_platform_recognition_config(
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[PlatformRecognitionConfigService, Depends(_get_service)],
        secrets_service: Annotated[PlatformSecretsService, Depends(_get_secrets_service)],
    ) -> dict[str, object]:
        """The effective selection plus the models each slot may use."""
        del principal
        record, is_default = await service.effective()
        configured: set[str] = set(await secrets_service.effective_provider_credentials())
        return {
            "success": True,
            "data": {
                **_view(record, is_default=is_default),
                "available_ocr": _options(configured, _reads_pages),
                "available_figure": _options(configured, _describes_figures),
            },
            "error": None,
        }

    @router.put("")
    async def put_platform_recognition_config(
        payload: PlatformRecognitionConfigWrite,
        request: Request,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[PlatformRecognitionConfigService, Depends(_get_service)],
        secrets_service: Annotated[PlatformSecretsService, Depends(_get_secrets_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        configured: set[str] = set(await secrets_service.effective_provider_credentials())
        for role, provider, model, fits, requirement in (
            (
                "ocr",
                payload.ocr_provider,
                payload.ocr_model,
                _reads_pages,
                "a recognition model",
            ),
            (
                "fallback",
                payload.fallback_provider,
                payload.fallback_model,
                _reads_pages,
                "a recognition model",
            ),
            (
                "figure",
                payload.figure_provider,
                payload.figure_model,
                _describes_figures,
                "a vision model that takes a prompt",
            ),
        ):
            _check_pair(
                role=role,
                provider=provider,
                model=model,
                configured=configured,
                fits=fits,
                requirement=requirement,
            )
        record = PlatformRecognitionConfigRecord(
            ocr_provider=payload.ocr_provider,
            ocr_model=payload.ocr_model,
            fallback_provider=payload.fallback_provider,
            fallback_model=payload.fallback_model,
            figure_provider=payload.figure_provider,
            figure_model=payload.figure_model,
            max_pages=payload.max_pages,
            updated_by=principal.subject_id,
        )
        await service.put(record)
        # Read per document at ingest time — peers only drop their 30 s cache.
        bus = getattr(request.app.state, "invalidation_bus", None)
        if bus is not None:
            await bus.publish(InvalidationEvent(kind="platform_recognition"))
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.PLATFORM_RECOGNITION_CONFIG_UPDATED,
            resource_type="platform_credential",
            resource_id="recognition-config",
            trace_id=current_trace_id_hex(),
            details=payload.model_dump(),
        )
        saved, is_default = await service.effective()
        return {"success": True, "data": _view(saved, is_default=is_default), "error": None}

    return router
