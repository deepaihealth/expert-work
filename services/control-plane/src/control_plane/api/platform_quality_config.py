"""``/v1/platform/quality-config`` — platform quality-monitor config (RT-5 PR-3b).

system_admin-only view + write of the production quality-monitoring knobs the
resident sampler + drift worker read each cycle (sampling rate / judge model /
drift thresholds / the ``enabled`` toggle). Non-secret config.

``enabled`` is the operational on/off (ANDed with the ``enable_quality_monitor``
deploy gate). Judge tokens cost money, so it defaults off — a fresh platform
turns monitoring on here.

Gating mirrors :mod:`control_plane.api.platform_judge_config`: ``principal``
via ``_principal``, handlers gate on ``is_system_admin``, ``{success,data,error}``
envelope.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api._authz import _principal, platform_only
from control_plane.audit import emit
from control_plane.platform_quality_config import (
    EffectiveQualityConfig,
    PlatformQualityConfigService,
)
from control_plane.platform_secrets import PlatformSecretsService
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.platform_quality_config import PlatformQualityConfigRow
from expert_work.protocol import AuditAction, Principal, models_for_provider
from expert_work.runtime.audit.logger import AuditLogger

#: 403 body for a non-system-admin caller. Per-router on purpose: the message
#: names the resource being protected, which is what makes the refusal actionable.
_PLATFORM_SCOPE_MESSAGE = "only a system admin may manage the platform quality config"


class PlatformQualityConfigWrite(BaseModel):
    """Full write payload for the platform quality config (all fields required)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    sampling_rate_pct: int = Field(ge=0, le=100)
    daily_cap: int = Field(gt=0)
    monitor_interval_s: int = Field(gt=0)
    monitor_batch_size: int = Field(gt=0)
    judge_provider: str = Field(min_length=1)
    judge_model: str = Field(min_length=1)
    drift_interval_s: int = Field(gt=0)
    drift_recent_window_h: int = Field(gt=0)
    drift_baseline_window_h: int = Field(gt=0)
    drift_min_samples: int = Field(gt=0)
    drift_threshold: float = Field(gt=0, le=1)
    drift_cooldown_h: int = Field(gt=0)


def _get_service(request: Request) -> PlatformQualityConfigService:
    return request.app.state.quality_config_service  # type: ignore[no-any-return]


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


def _effective_dict(cfg: EffectiveQualityConfig) -> dict[str, object]:
    return {
        "enabled": cfg.enabled,
        "sampling_rate_pct": cfg.sampling_rate_pct,
        "daily_cap": cfg.daily_cap,
        "monitor_interval_s": cfg.monitor_interval_s,
        "monitor_batch_size": cfg.monitor_batch_size,
        "judge_provider": cfg.judge_provider,
        "judge_model": cfg.judge_model,
        "drift_interval_s": cfg.drift_interval_s,
        "drift_recent_window_h": cfg.drift_recent_window_h,
        "drift_baseline_window_h": cfg.drift_baseline_window_h,
        "drift_min_samples": cfg.drift_min_samples,
        "drift_threshold": cfg.drift_threshold,
        "drift_cooldown_h": cfg.drift_cooldown_h,
    }


def build_platform_quality_config_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/platform/quality-config",
        tags=["platform_config"],
        dependencies=[Depends(platform_only(_PLATFORM_SCOPE_MESSAGE))],
    )

    @router.get("")
    async def get_platform_quality_config(
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[PlatformQualityConfigService, Depends(_get_service)],
    ) -> dict[str, object]:
        """Effective quality config + whether it is still the (unsaved) default."""
        cfg = await service.effective()
        is_default = (await service.get_row()) is None
        return {
            "success": True,
            "data": {"config": _effective_dict(cfg), "is_default": is_default},
            "error": None,
        }

    @router.put("")
    async def put_platform_quality_config(
        payload: PlatformQualityConfigWrite,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[PlatformQualityConfigService, Depends(_get_service)],
        secrets_service: Annotated[PlatformSecretsService, Depends(_get_secrets_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        """Write the full quality config. system_admin-only.

        The judge provider must have a configured platform credential (else the
        judge would silently fail on every sample, as the RT-5 live run showed).
        """

        configured = set(await secrets_service.effective_provider_credentials())
        if payload.judge_provider not in configured:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "JUDGE_PROVIDER_KEY_MISSING",
                    "message": (
                        f"configure the {payload.judge_provider!r} provider key in "
                        "platform credentials before selecting it as the quality judge"
                    ),
                },
            )
        # A mistyped / non-chat judge model would pass the provider check but drop
        # every sample silently (score() → None) — the exact failure this guards.
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

        await service.put(
            PlatformQualityConfigRow(
                enabled=payload.enabled,
                sampling_rate_pct=payload.sampling_rate_pct,
                daily_cap=payload.daily_cap,
                monitor_interval_s=payload.monitor_interval_s,
                monitor_batch_size=payload.monitor_batch_size,
                judge_provider=payload.judge_provider,
                judge_model=payload.judge_model,
                drift_interval_s=payload.drift_interval_s,
                drift_recent_window_h=payload.drift_recent_window_h,
                drift_baseline_window_h=payload.drift_baseline_window_h,
                drift_min_samples=payload.drift_min_samples,
                drift_threshold=payload.drift_threshold,
                drift_cooldown_h=payload.drift_cooldown_h,
                updated_by=principal.subject_id,
            )
        )

        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.PLATFORM_QUALITY_CONFIG_UPDATED,
            resource_type="platform_credential",
            resource_id="quality-config",
            trace_id=current_trace_id_hex(),
            details={"enabled": payload.enabled, "judge_provider": payload.judge_provider},
        )

        cfg = await service.effective()
        return {"success": True, "data": {"config": _effective_dict(cfg)}, "error": None}

    return router


__all__ = ["build_platform_quality_config_router"]
