"""``/v1/platform/tool-budget-config`` — platform tool-output-budget on/off (Phase 3).

system_admin-only view + write of the EFFECTIVE platform on/off for the
tool-output-budget feature (generalized externalization + persist floor + CM-12
prune). Unset is a valid state — the service then falls back to the
``EXPERT_WORK_TOOL_OUTPUT_BUDGET`` env default.

Gating mirrors :mod:`control_plane.api.platform_judge_config`: ``principal``
arrives via the shared ``_principal`` dependency, handlers gate on
``principal.is_system_admin``, responses use the ``{success,data,error}`` envelope.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from control_plane.api._authz import _principal, platform_only
from control_plane.audit import emit
from control_plane.invalidation_bus import InvalidationEvent
from control_plane.platform_tool_budget_config import PlatformToolBudgetConfigService
from expert_work.common.observability import current_trace_id_hex
from expert_work.protocol import AuditAction, Principal
from expert_work.runtime.audit.logger import AuditLogger

#: 403 body for a non-system-admin caller. Per-router on purpose: the message
#: names the resource being protected, which is what makes the refusal actionable.
_PLATFORM_SCOPE_MESSAGE = "only a system admin may manage the platform tool-budget config"


class PlatformToolBudgetConfigWrite(BaseModel):
    """Write payload — the platform tool-output-budget on/off flag."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool


def _get_service(request: Request) -> PlatformToolBudgetConfigService:
    return request.app.state.platform_tool_budget_config_service  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


async def _view(service: PlatformToolBudgetConfigService) -> dict[str, object]:
    """``{enabled, effective}``: the resolved on/off + whether it is an explicit
    platform override (``enabled`` is null ⇒ using the env default)."""
    return {
        "enabled": await service.configured_enabled(),
        "effective": await service.effective_enabled(),
    }


def build_platform_tool_budget_config_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/platform/tool-budget-config",
        tags=["platform_config"],
        dependencies=[Depends(platform_only(_PLATFORM_SCOPE_MESSAGE))],
    )

    @router.get("")
    async def get_platform_tool_budget_config(
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[PlatformToolBudgetConfigService, Depends(_get_service)],
    ) -> dict[str, object]:
        """The platform tool-budget on/off (effective + whether overridden)."""
        return {"success": True, "data": await _view(service), "error": None}

    @router.put("")
    async def put_platform_tool_budget_config(
        payload: PlatformToolBudgetConfigWrite,
        request: Request,
        principal: Annotated[Principal, Depends(_principal)],
        service: Annotated[PlatformToolBudgetConfigService, Depends(_get_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        """Set the platform tool-budget on/off. system_admin-only."""
        await service.put(enabled=payload.enabled, updated_by=principal.subject_id)
        # PR-E3b bug fix — the switch is resolved at BUILD time and baked into
        # ``BuiltAgent`` (``platform_tool_budget_enabled``): without dropping
        # the build layer the flip only reaches new builds, so existing agents
        # keep the old value until the 1800s build TTL, even on a single pod.
        # Local evict + broadcast (the ``platform_tool_budget`` handler
        # encodes the same two layers for peer replicas).
        runtime = getattr(request.app.state, "agent_runtime", None)
        if runtime is not None:
            runtime.invalidate_all()
        bus = getattr(request.app.state, "invalidation_bus", None)
        if bus is not None:
            await bus.publish(InvalidationEvent(kind="platform_tool_budget"))
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.PLATFORM_TOOL_BUDGET_UPDATED,
            resource_type="platform_credential",
            resource_id="tool-budget-config",
            trace_id=current_trace_id_hex(),
            details={"enabled": payload.enabled},
        )
        return {"success": True, "data": await _view(service), "error": None}

    return router
