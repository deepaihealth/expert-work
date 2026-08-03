"""``/v1/tenants/{tenant_id}/quotas`` admin endpoints — Stream C.5.

CRUD on ``tenant_quota`` rows. All write paths require the admin
role; read returns the per-tenant config including currently active
limit values.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from control_plane.api._authz import require
from control_plane.audit import emit
from control_plane.tenant_scope import (
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.quota import TenantQuotaStore
from expert_work.protocol import AuditAction, Principal, TenantQuotaPatch
from expert_work.runtime.audit.logger import AuditLogger

logger = logging.getLogger("expert_work.control_plane.api.tenant_quotas")


def _get_repo(request: Request) -> TenantQuotaStore:
    return request.app.state.tenant_quota_repo  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def build_tenant_quotas_router() -> APIRouter:
    router = APIRouter(prefix="/v1/tenants", tags=["tenant_quotas"])

    @router.get("/{tenant_id}/quotas")
    async def list_tenant_quotas(
        tenant_id: UUID,
        request: Request,
        principal: Annotated[Principal, Depends(require("quota", "read"))],
        repo: Annotated[TenantQuotaStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        # W4 (PR-2) — path-param target through the central resolver: plain
        # tenant admins keep their 403 on foreign tenants (TENANT_NOT_ALLOWED),
        # system_admin cross-tenant hits emit SYSTEM_TENANT_SWITCH.
        scope = await ensure_single_tenant_scope(
            principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/tenants/{tenant_id}/quotas",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            rows = await repo.list_by_tenant(tenant_id=scope.tenant_id)
        await emit(
            audit,
            tenant_id=scope.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.QUOTA_CONFIG_READ,
            resource_type="quota",
            resource_id=None,
            trace_id=current_trace_id_hex(),
            details={"count": len(rows)},
        )
        return {
            "success": True,
            "data": [r.model_dump(mode="json") for r in rows],
            "error": None,
        }

    @router.post("/{tenant_id}/quotas", status_code=201)
    async def upsert_tenant_quota(
        tenant_id: UUID,
        payload: TenantQuotaPatch,
        request: Request,
        principal: Annotated[Principal, Depends(require("quota", "write"))],
        repo: Annotated[TenantQuotaStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        scope = await ensure_single_tenant_scope(
            principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="POST /v1/tenants/{tenant_id}/quotas",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            row = await repo.upsert(
                tenant_id=scope.tenant_id,
                patch=payload,
                updated_by=principal.subject_id,
            )
        await emit(
            audit,
            tenant_id=scope.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.QUOTA_CONFIG_WRITE,
            resource_type="quota",
            resource_id=str(row.id),
            trace_id=current_trace_id_hex(),
            details={
                "dimension": payload.dimension.value,
                "scope": dict(payload.scope),
                "limit_value": payload.limit_value,
                "burst": payload.burst,
            },
        )
        return {"success": True, "data": row.model_dump(mode="json"), "error": None}

    @router.delete("/{tenant_id}/quotas/{quota_id}", status_code=204)
    async def delete_tenant_quota(
        tenant_id: UUID,
        quota_id: UUID,
        request: Request,
        principal: Annotated[Principal, Depends(require("quota", "delete"))],
        repo: Annotated[TenantQuotaStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> None:
        scope = await ensure_single_tenant_scope(
            principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="DELETE /v1/tenants/{tenant_id}/quotas/{quota_id}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            deleted = await repo.delete(quota_id=quota_id, tenant_id=scope.tenant_id)
        if not deleted:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "QUOTA_NOT_FOUND",
                    "message": "tenant_quota row not found for this tenant",
                },
            )
        await emit(
            audit,
            tenant_id=scope.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.QUOTA_CONFIG_DELETE,
            resource_type="quota",
            resource_id=str(quota_id),
            trace_id=current_trace_id_hex(),
        )

    return router
