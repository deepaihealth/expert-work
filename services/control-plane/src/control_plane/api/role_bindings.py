"""``/v1/role_bindings`` admin endpoints — Stream C.3."""

from __future__ import annotations

import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.api._authz import console_only, require
from control_plane.audit import emit
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.auth import (
    DuplicateRoleBindingError,
    RoleBindingStore,
)
from expert_work.protocol import AuditAction, BindingConditions, Principal, Role
from expert_work.runtime.audit.logger import AuditLogger

logger = logging.getLogger("expert_work.control_plane.api.role_bindings")


class CreateRoleBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_type: Literal["user", "service_account"]
    subject_id: UUID
    role: Role
    # Stream N — when ``True``, mint a platform-scope binding (role
    # must be ``SYSTEM_ADMIN``). Only ``is_system_admin`` callers may
    # set this; the endpoint enforces that check before persisting.
    platform_scope: bool = False
    # Stream 8.5 — optional ABAC conditions narrowing the grant to matching
    # resource instances. Tenant-scope only (platform-scope must be None).
    conditions: BindingConditions | None = None

    @model_validator(mode="after")
    def _check_scope_role_consistency(self) -> CreateRoleBindingRequest:
        if self.platform_scope and self.role is not Role.SYSTEM_ADMIN:
            raise ValueError("platform_scope=true requires role=system_admin")
        if not self.platform_scope and self.role is Role.SYSTEM_ADMIN:
            raise ValueError("role=system_admin requires platform_scope=true")
        if self.platform_scope and self.conditions is not None:
            raise ValueError("platform_scope bindings must not carry conditions")
        return self


def _get_repo(request: Request) -> RoleBindingStore:
    return request.app.state.role_binding_repo  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def build_role_bindings_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/role_bindings",
        tags=["role_bindings"],
        # 阶段 1.2 —— tenant management surface, not a third-party one. The
        # DELETE carried only RBAC; its two siblings gate on system_admin
        # inline. Router level so the whole surface answers one way.
        dependencies=[Depends(console_only())],
    )

    @router.post("", status_code=201)
    async def create_role_binding(
        payload: CreateRoleBindingRequest,
        principal: Annotated[Principal, Depends(require("role_binding", "write"))],
        repo: Annotated[RoleBindingStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        # Stream N — platform-scope bindings (system admins) can only be
        # created by another system admin. The DTO validator already
        # guarantees role=SYSTEM_ADMIN when platform_scope=true.
        if payload.platform_scope and not principal.is_system_admin:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "PLATFORM_SCOPE_FORBIDDEN",
                    "message": "only a system admin may grant platform-scope bindings",
                },
            )
        try:
            binding = await repo.create(
                subject_type=payload.subject_type,
                subject_id=payload.subject_id,
                tenant_id=None if payload.platform_scope else principal.tenant_id,
                role=payload.role,
                granted_by=principal.subject_id,
                platform_scope=payload.platform_scope,
                conditions=payload.conditions,
            )
        except DuplicateRoleBindingError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ROLE_BINDING_DUPLICATE",
                    "message": "the subject already has this role for this tenant",
                },
            ) from exc
        # Audit is recorded under the principal's home tenant when the
        # binding is platform-scope (the binding itself has no tenant);
        # the cross-tenant audit emitted by ensure_tenant_scope is the
        # other half of the trail.
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.ROLE_BINDING_CREATE,
            resource_type="role_binding",
            resource_id=str(binding.id),
            trace_id=current_trace_id_hex(),
            details={
                "subject_type": payload.subject_type,
                "subject_id": str(payload.subject_id),
                "role": payload.role.value,
                "platform_scope": payload.platform_scope,
            },
        )
        return {"success": True, "data": binding.model_dump(mode="json"), "error": None}

    @router.get("")
    async def list_role_bindings(
        request: Request,
        principal: Annotated[Principal, Depends(require("role_binding", "read"))],
        repo: Annotated[RoleBindingStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,  # Stream N
        platform_scope: Annotated[bool | None, Query()] = None,  # Stream N
    ) -> dict[str, object]:
        # Stream N — listing platform-scope bindings is itself a
        # platform-admin view; non-system-admins cannot read who else
        # holds platform-scope. Forbid before paying the scope-resolve
        # cost so we don't emit a misleading cross-tenant audit row.
        if platform_scope and not principal.is_system_admin:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "PLATFORM_SCOPE_FORBIDDEN",
                    "message": "only a system admin may list platform-scope bindings",
                },
            )
        scope = await ensure_tenant_scope(
            principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/role_bindings",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            if platform_scope:
                # ``platform_scope=true`` is the dedicated lookup —
                # cross-tenant filter is implicit so ignore ``tenant_id``
                # other than the scope-resolution audit (already emitted).
                items = await repo.list_platform_scope()
            elif isinstance(scope, CrossTenant):
                items = await repo.list_all_tenants()
            else:
                items = await repo.list_for_tenant(tenant_id=scope.tenant_id)
        return {
            "success": True,
            "data": {
                "items": [b.model_dump(mode="json") for b in items],
                "total": len(items),
                "cross_tenant": isinstance(scope, CrossTenant) or bool(platform_scope),
            },
            "error": None,
        }

    # 阶段 1.2 —— the sibling routes on this router are system_admin-gated
    # inline; this one was reachable by any API key whose scopes cover it.
    @router.delete("/{binding_id}", status_code=204, dependencies=[Depends(console_only())])
    async def delete_role_binding(
        binding_id: UUID,
        principal: Annotated[Principal, Depends(require("role_binding", "delete"))],
        repo: Annotated[RoleBindingStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> None:
        ok = await repo.delete(tenant_id=principal.tenant_id, role_binding_id=binding_id)
        if not ok:
            raise HTTPException(status_code=404, detail="role_binding not found")
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.ROLE_BINDING_DELETE,
            resource_type="role_binding",
            resource_id=str(binding_id),
            trace_id=current_trace_id_hex(),
        )

    return router
