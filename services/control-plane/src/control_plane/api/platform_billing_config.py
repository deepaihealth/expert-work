"""``/v1/platform/billing-config`` — platform billing-rollup toggle (Stream 12.4).

system_admin-only view + write of the platform billing config. For now one
flag: ``rollup_enabled`` — the offline ``BillingRollupJob`` reads it before each
run and skips when ``false``, so a platform operator can pause cost rollup from
the admin UI without touching the k8s CronJob (the cron schedule itself stays in
k8s). An absent row means "default" → rollup enabled.

Gating mirrors :mod:`control_plane.api.platform_judge_config`: ``principal``
arrives via the shared ``_principal`` dependency, handlers gate on
``principal.is_system_admin``, responses use the ``{success,data,error}`` envelope.
The table is tenant-less with no RLS policy (migration 0083), so store calls need
no ``bypass_rls_session()``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from control_plane.api._authz import _principal, platform_only
from control_plane.audit import emit
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.platform_billing_config import PlatformBillingConfigStore
from expert_work.protocol import AuditAction, Principal
from expert_work.runtime.audit.logger import AuditLogger

#: 403 body for a non-system-admin caller. Per-router on purpose: the message
#: names the resource being protected, which is what makes the refusal actionable.
_PLATFORM_SCOPE_MESSAGE = "only a system admin may manage the platform billing config"


class PlatformBillingConfigWrite(BaseModel):
    """Write payload — the platform billing-rollup enable flag."""

    model_config = ConfigDict(extra="forbid")
    rollup_enabled: bool


def _get_store(request: Request) -> PlatformBillingConfigStore:
    return request.app.state.platform_billing_config_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def build_platform_billing_config_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/platform/billing-config",
        tags=["platform_config"],
        dependencies=[Depends(platform_only(_PLATFORM_SCOPE_MESSAGE))],
    )

    @router.get("")
    async def get_platform_billing_config(
        principal: Annotated[Principal, Depends(_principal)],
        store: Annotated[PlatformBillingConfigStore, Depends(_get_store)],
    ) -> dict[str, object]:
        """The platform billing-rollup toggle. Absent row → default enabled."""
        row = await store.get()
        return {
            "success": True,
            "data": {"rollup_enabled": row.rollup_enabled if row is not None else True},
            "error": None,
        }

    @router.put("")
    async def put_platform_billing_config(
        payload: PlatformBillingConfigWrite,
        principal: Annotated[Principal, Depends(_principal)],
        store: Annotated[PlatformBillingConfigStore, Depends(_get_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        """Set the platform billing-rollup enable flag. system_admin-only."""
        await store.put(rollup_enabled=payload.rollup_enabled, updated_by=principal.subject_id)
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.PLATFORM_BILLING_CONFIG_UPDATED,
            resource_type="platform_credential",
            resource_id="billing-config",
            trace_id=current_trace_id_hex(),
            details={"rollup_enabled": payload.rollup_enabled},
        )
        return {
            "success": True,
            "data": {"rollup_enabled": payload.rollup_enabled},
            "error": None,
        }

    return router
