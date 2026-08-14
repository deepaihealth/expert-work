"""``/v1/service_accounts`` admin endpoints — Stream C.3."""

from __future__ import annotations

import logging
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api._authz import console_only, require
from control_plane.api.api_keys import _vault_name
from control_plane.audit import emit
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.auth import (
    ApiKeyStore,
    DuplicateServiceAccountError,
    ServiceAccountStore,
)
from expert_work.protocol import AuditAction, Principal, ServiceAccount
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.secret_store import SecretStore

logger = logging.getLogger("expert_work.control_plane.api.service_accounts")


class CreateServiceAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)


class ServiceAccountListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ServiceAccount]
    total: int
    cross_tenant: bool = False  # Stream N — true ⇔ ?tenant_id=* response


def _get_repo(request: Request) -> ServiceAccountStore:
    return request.app.state.service_account_repo  # type: ignore[no-any-return]


def _get_keys(request: Request) -> ApiKeyStore:
    return request.app.state.api_key_repo  # type: ignore[no-any-return]


def _get_secret_store(request: Request) -> SecretStore:
    return request.app.state.secret_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def build_service_accounts_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/service_accounts",
        tags=["service_accounts"],
        dependencies=[Depends(console_only())],
    )

    @router.post("", status_code=201)
    async def create_service_account(
        payload: CreateServiceAccountRequest,
        principal: Annotated[Principal, Depends(require("service_account", "write"))],
        repo: Annotated[ServiceAccountStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        try:
            sa = await repo.create(
                tenant_id=principal.tenant_id,
                name=payload.name,
                description=payload.description,
                created_by=principal.subject_id,
            )
        except DuplicateServiceAccountError as exc:
            # Do not echo the user-supplied name into the log — CodeQL
            # ``py/log-injection``. The tenant_id (from JWT) is logged by
            # the ObservabilityMiddleware already; that's enough context.
            logger.info("service_account.create.duplicate")
            raise HTTPException(
                status_code=409,
                detail={"code": "SERVICE_ACCOUNT_DUPLICATE", "message": "name already taken"},
            ) from exc
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.SERVICE_ACCOUNT_CREATE,
            resource_type="service_account",
            resource_id=str(sa.id),
            trace_id=current_trace_id_hex(),
            details={"name": sa.name},
        )
        return {"success": True, "data": sa.model_dump(mode="json"), "error": None}

    @router.get("")
    async def list_service_accounts(
        request: Request,
        principal: Annotated[Principal, Depends(require("service_account", "read"))],
        repo: Annotated[ServiceAccountStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,  # Stream N
    ) -> dict[str, object]:
        scope = await ensure_tenant_scope(
            principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/service_accounts",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                items = await repo.list_all_tenants(limit=limit, offset=offset)
            else:
                items = await repo.list_by_tenant(
                    tenant_id=scope.tenant_id, limit=limit, offset=offset
                )
        body = ServiceAccountListResponse(
            items=items, total=len(items), cross_tenant=isinstance(scope, CrossTenant)
        )
        return {"success": True, "data": body.model_dump(mode="json"), "error": None}

    @router.delete("/{service_account_id}", status_code=204)
    async def delete_service_account(
        service_account_id: UUID,
        principal: Annotated[Principal, Depends(require("service_account", "delete"))],
        repo: Annotated[ServiceAccountStore, Depends(_get_repo)],
        keys: Annotated[ApiKeyStore, Depends(_get_keys)],
        secrets: Annotated[SecretStore, Depends(_get_secret_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> None:
        # ``api_key`` rows FK-cascade on SA delete (ondelete="CASCADE"), but
        # the vault has no such cascade — purge each key's vault entry
        # first (best-effort) or the plaintext orphans forever.
        for key in await keys.list_by_service_account(
            tenant_id=principal.tenant_id, service_account_id=service_account_id
        ):
            try:
                await secrets.delete(_vault_name(principal.tenant_id, key.id))
            except Exception:
                logger.warning("service_account.delete.vault_delete_failed api_key_id=%s", key.id)

        removed = await repo.delete(
            tenant_id=principal.tenant_id, service_account_id=service_account_id
        )
        if not removed:
            raise HTTPException(status_code=404, detail="service_account not found")
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.SERVICE_ACCOUNT_DELETE,
            resource_type="service_account",
            resource_id=str(service_account_id),
            trace_id=current_trace_id_hex(),
        )

    return router
