"""``/v1/platform/dynamic-worker-config`` — platform dynamic-worker limits (B3 PR2).

system_admin-only view + write of the EFFECTIVE platform ``dynamic_worker``
limits (``max_concurrent``, ``max_per_run``, ``max_iterations``). Unset is a
valid state — the service then falls back to the process's env-default
settings snapshot.

Gating mirrors :mod:`control_plane.api.platform_tool_budget_config`:
``principal`` arrives via the shared ``_principal`` dependency, handlers gate
on ``principal.is_system_admin``, responses use the ``{success,data,error}``
envelope.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api._authz import _principal, platform_only
from control_plane.audit import emit
from control_plane.platform_dynamic_worker_config import (
    DynamicWorkerConfig,
    PlatformDynamicWorkerConfigService,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.protocol import AuditAction, Principal
from expert_work.runtime.audit.logger import AuditLogger

#: 403 body for a non-system-admin caller. Per-router on purpose: the message
#: names the resource being protected, which is what makes the refusal actionable.
_PLATFORM_SCOPE_MESSAGE = "only a system admin may manage the platform dynamic-worker config"


class PlatformDynamicWorkerConfigWrite(BaseModel):
    """Write payload — the platform ``dynamic_worker`` limits."""

    model_config = ConfigDict(extra="forbid")
    max_concurrent: int = Field(ge=1, le=16)
    max_per_run: int = Field(ge=1, le=256)
    max_iterations: int = Field(ge=1, le=64)


def _get_service(request: Request) -> PlatformDynamicWorkerConfigService:
    return request.app.state.platform_dynamic_worker_config_service  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _config_dict(config: DynamicWorkerConfig) -> dict[str, int]:
    return {
        "max_concurrent": config.max_concurrent,
        "max_per_run": config.max_per_run,
        "max_iterations": config.max_iterations,
    }


async def _view(service: PlatformDynamicWorkerConfigService) -> dict[str, object]:
    """``{configured, effective}``: the resolved limits + whether they are an
    explicit platform override (``configured`` is null ⇒ using the env
    default)."""
    configured = await service.configured()
    return {
        "configured": _config_dict(configured) if configured is not None else None,
        "effective": _config_dict(await service.effective()),
    }


def build_platform_dynamic_worker_config_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/platform/dynamic-worker-config",
        tags=["platform_config"],
        dependencies=[Depends(platform_only(_PLATFORM_SCOPE_MESSAGE))],
    )

    @router.get("")
    async def get_platform_dynamic_worker_config(
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[PlatformDynamicWorkerConfigService, Depends(_get_service)],
    ) -> dict[str, object]:
        """The platform dynamic-worker limits (effective + whether overridden)."""
        return {"success": True, "data": await _view(service), "error": None}

    @router.put("")
    async def put_platform_dynamic_worker_config(
        payload: PlatformDynamicWorkerConfigWrite,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[PlatformDynamicWorkerConfigService, Depends(_get_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        """Set the platform dynamic-worker limits. system_admin-only."""
        await service.put(
            max_concurrent=payload.max_concurrent,
            max_per_run=payload.max_per_run,
            max_iterations=payload.max_iterations,
            updated_by=principal.subject_id,
        )
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.PLATFORM_DYNAMIC_WORKER_UPDATED,
            resource_type="platform_credential",
            resource_id="dynamic-worker-config",
            trace_id=current_trace_id_hex(),
            details={
                "max_concurrent": payload.max_concurrent,
                "max_per_run": payload.max_per_run,
                "max_iterations": payload.max_iterations,
            },
        )
        return {"success": True, "data": await _view(service), "error": None}

    return router
