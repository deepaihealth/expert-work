"""``/v1/platform/judge-config`` — platform output/action judge model (Stream PI-3-A1).

system_admin-only view + write of the EFFECTIVE platform **judge** model: the
model used by the PI-2b output judge and the PI-3b action judge. Unset is a
valid state — the judge then falls back to each agent's own primary model.

Gating mirrors :mod:`control_plane.api.platform_embedding_config`: ``principal``
arrives via the shared ``_principal`` dependency, handlers gate on
``principal.is_system_admin``, responses use the ``{success,data,error}`` envelope.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from control_plane.api._authz import _principal, platform_only
from control_plane.audit import emit
from control_plane.platform_judge_config import PlatformJudgeConfigService
from control_plane.platform_secrets import PlatformSecretsService
from expert_work.common.observability import current_trace_id_hex
from expert_work.protocol import (
    PROVIDER_CATALOG,
    AuditAction,
    Principal,
    models_for_provider,
)
from expert_work.runtime.audit.logger import AuditLogger

#: 403 body for a non-system-admin caller. Per-router on purpose: the message
#: names the resource being protected, which is what makes the refusal actionable.
_PLATFORM_SCOPE_MESSAGE = "only a system admin may manage the platform judge config"


class PlatformJudgeConfigWrite(BaseModel):
    """Write payload for the platform judge selection (Stream PI-3-A1).

    Provider/model **names** only — never secrets. Both ``None`` clears the
    config (→ judge falls back to each agent's own model); otherwise both must
    be supplied (validated in the handler)."""

    model_config = ConfigDict(extra="forbid")
    judge_provider: str | None = None
    judge_model: str | None = None


def _get_judge_config_service(request: Request) -> PlatformJudgeConfigService:
    return request.app.state.platform_judge_config_service  # type: ignore[no-any-return]


def _get_secrets_service(request: Request) -> PlatformSecretsService:
    return request.app.state.platform_secrets_service  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _is_chat_model(provider: str, model: str) -> bool:
    """True iff ``model`` is a chat-capable (non-embedding, non-rerank) model."""
    for entry in models_for_provider(provider):
        if entry.name == model:
            return not entry.embeddings and not entry.rerank
    return False


def _pair_to_dict(pair: tuple[str, str] | None) -> dict[str, str] | None:
    if pair is None:
        return None
    provider, model = pair
    return {"provider": provider, "model": model}


def _available(configured: set[str]) -> list[dict[str, str]]:
    """Chat-capable catalog models for every configured platform provider."""
    options: list[dict[str, str]] = []
    for provider in PROVIDER_CATALOG:
        if provider not in configured:
            continue
        for entry in models_for_provider(provider):
            if not entry.embeddings and not entry.rerank and not entry.deprecated:
                options.append({"provider": provider, "model": entry.name})
    return options


def build_platform_judge_config_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/platform/judge-config",
        tags=["platform_config"],
        dependencies=[Depends(platform_only(_PLATFORM_SCOPE_MESSAGE))],
    )

    @router.get("")
    async def get_platform_judge_config(
        principal: Annotated[Principal, Depends(_principal)],
        judge_config_service: Annotated[
            PlatformJudgeConfigService, Depends(_get_judge_config_service)
        ],
        secrets_service: Annotated[PlatformSecretsService, Depends(_get_secrets_service)],
    ) -> dict[str, object]:
        """Effective judge selection + selectable chat models.

        ``judge`` is ``{"provider","model"}`` or ``null`` (→ agent's own model);
        ``available`` lists chat-capable catalog models for configured providers."""
        judge = await judge_config_service.effective_judge_config()
        configured = set(await secrets_service.effective_provider_credentials())
        return {
            "success": True,
            "data": {
                "judge": _pair_to_dict(judge),
                "available": _available(configured),
            },
            "error": None,
        }

    @router.put("")
    async def put_platform_judge_config(
        payload: PlatformJudgeConfigWrite,
        principal: Annotated[Principal, Depends(_principal)],
        judge_config_service: Annotated[
            PlatformJudgeConfigService, Depends(_get_judge_config_service)
        ],
        secrets_service: Annotated[PlatformSecretsService, Depends(_get_secrets_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        """Set (or clear) the platform judge model. system_admin-only.

        Both fields ``None`` clears the config; otherwise both required + the
        provider key must be configured + the model must be a chat model."""

        # Both-or-neither.
        if (payload.judge_provider is None) != (payload.judge_model is None):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_JUDGE_PAIR",
                    "message": (
                        "provide both 'judge_provider' and 'judge_model', or neither to clear"
                    ),
                },
            )

        if payload.judge_provider is not None and payload.judge_model is not None:
            configured = set(await secrets_service.effective_provider_credentials())
            if payload.judge_provider not in configured:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "JUDGE_PROVIDER_KEY_MISSING",
                        "message": (
                            f"configure the {payload.judge_provider!r} provider key in "
                            "platform credentials first"
                        ),
                    },
                )
            if not _is_chat_model(payload.judge_provider, payload.judge_model):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "INVALID_JUDGE_MODEL",
                        "message": (
                            f"{payload.judge_model!r} is not a chat model for "
                            f"provider {payload.judge_provider!r}"
                        ),
                    },
                )

        await judge_config_service.put(
            judge_provider=payload.judge_provider,
            judge_model=payload.judge_model,
            updated_by=principal.subject_id,
        )

        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.PLATFORM_JUDGE_CONFIG_UPDATED,
            resource_type="platform_credential",
            resource_id="judge-config",
            trace_id=current_trace_id_hex(),
            details={
                "judge_provider": payload.judge_provider,
                "judge_model": payload.judge_model,
            },
        )

        judge = await judge_config_service.effective_judge_config()
        return {"success": True, "data": {"judge": _pair_to_dict(judge)}, "error": None}

    return router
