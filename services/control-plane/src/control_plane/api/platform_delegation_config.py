"""``/v1/platform/delegation-config`` — platform delegation-gate capacity
(perf phase2 PR3).

system_admin-only view + write of the EFFECTIVE platform delegation-gate
capacity (``max_concurrent_delegations``). Unset is a valid state — the
service then falls back to the process's built-in default.

Gating mirrors :mod:`control_plane.api.platform_dynamic_worker_config`:
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
from control_plane.platform_delegation_config import (
    DelegationConfig,
    PlatformDelegationConfigService,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.protocol import AuditAction, Principal
from expert_work.runtime.audit.logger import AuditLogger

#: 403 body for a non-system-admin caller. Per-router on purpose: the message
#: names the resource being protected, which is what makes the refusal actionable.
_PLATFORM_SCOPE_MESSAGE = "only a system admin may manage the platform delegation config"


class PlatformDelegationConfigWrite(BaseModel):
    """Write payload — the platform delegation-gate capacity."""

    model_config = ConfigDict(extra="forbid")
    max_concurrent_delegations: int = Field(ge=1, le=64)


def _get_service(request: Request) -> PlatformDelegationConfigService:
    return request.app.state.platform_delegation_config_service  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _config_dict(config: DelegationConfig) -> dict[str, int]:
    return {"max_concurrent_delegations": config.max_concurrent_delegations}


async def _view(service: PlatformDelegationConfigService) -> dict[str, object]:
    """``{configured, effective}``: the resolved capacity + whether it is an
    explicit platform override (``configured`` is null ⇒ using the built-in
    default)."""
    configured = await service.configured()
    return {
        "configured": _config_dict(configured) if configured is not None else None,
        "effective": _config_dict(await service.effective()),
    }


def build_platform_delegation_config_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/platform/delegation-config",
        tags=["platform_config"],
        dependencies=[Depends(platform_only(_PLATFORM_SCOPE_MESSAGE))],
    )

    @router.get("")
    async def get_platform_delegation_config(
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[PlatformDelegationConfigService, Depends(_get_service)],
    ) -> dict[str, object]:
        """The platform delegation-gate capacity (effective + whether overridden)."""
        return {"success": True, "data": await _view(service), "error": None}

    @router.put("")
    async def put_platform_delegation_config(
        payload: PlatformDelegationConfigWrite,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[PlatformDelegationConfigService, Depends(_get_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        """Set the platform delegation-gate capacity. system_admin-only."""
        await service.put(
            max_concurrent_delegations=payload.max_concurrent_delegations,
            updated_by=principal.subject_id,
        )
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.PLATFORM_DELEGATION_UPDATED,
            resource_type="platform_credential",
            resource_id="delegation-config",
            trace_id=current_trace_id_hex(),
            details={"max_concurrent_delegations": payload.max_concurrent_delegations},
        )
        return {"success": True, "data": await _view(service), "error": None}

    return router
