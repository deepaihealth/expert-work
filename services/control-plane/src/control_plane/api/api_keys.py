"""``/v1/service_accounts/{id}/api_keys`` + ``/v1/api_keys`` endpoints — Stream C.3.

Stream K.K1 added ``POST /v1/api_keys/{api_key_id}/rotate`` for
double-active rotation (Mini-ADR K-1).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api._authz import require
from control_plane.audit import emit
from control_plane.auth.api_key_verifier import mint_api_key
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.auth import (
    ApiKeyStore,
    DuplicateApiKeyPrefixError,
    ServiceAccountStore,
)
from expert_work.protocol import ApiKey, ApiKeyCreated, ApiKeyScope, AuditAction, Principal
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.secret_store import SecretNotFoundError, SecretStore

logger = logging.getLogger("expert_work.control_plane.api.api_keys")

# Stream K.K1 (Mini-ADR K-1) — double-active rotation grace bounds.
_DEFAULT_GRACE_PERIOD_S: int = 300
_MAX_GRACE_PERIOD_S: int = 3600
_MIN_GRACE_PERIOD_S: int = 0


class CreateApiKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scopes: list[ApiKeyScope] = Field(default_factory=list)
    expires_at: datetime | None = None


class RotateApiKeyRequest(BaseModel):
    """Stream K.K1 — body for ``POST /v1/api_keys/{api_key_id}/rotate``.

    ``grace_period_s`` controls how long the old bearer keeps verifying
    after rotation. Default 300 (5 min) — long enough for clients to
    swap, short enough that a compromised key is not left valid
    indefinitely. For emergency revocation pass 0 here (or use
    ``DELETE /v1/api_keys/{id}`` which short-circuits the grace path).
    """

    model_config = ConfigDict(extra="forbid")
    grace_period_s: int = Field(
        default=_DEFAULT_GRACE_PERIOD_S,
        ge=_MIN_GRACE_PERIOD_S,
        le=_MAX_GRACE_PERIOD_S,
    )


def _get_keys(request: Request) -> ApiKeyStore:
    return request.app.state.api_key_repo  # type: ignore[no-any-return]


def _get_accounts(request: Request) -> ServiceAccountStore:
    return request.app.state.service_account_repo  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _vault_name(tenant_id: UUID, api_key_id: UUID) -> str:
    return f"expert-work/tenant/{tenant_id}/api-key/{api_key_id}"


def _get_secret_store(request: Request) -> SecretStore:
    return request.app.state.secret_store  # type: ignore[no-any-return]


async def _find_api_key(keys: ApiKeyStore, *, tenant_id: UUID, api_key_id: UUID) -> ApiKey | None:
    """Fetch one row by id — ``ApiKeyStore`` has no single-id ``get``
    (only list methods), so this scans the tenant's key list."""
    for row in await keys.list_by_tenant(tenant_id=tenant_id):
        if row.id == api_key_id:
            return row
    return None


def build_api_keys_router() -> APIRouter:
    router = APIRouter(tags=["api_keys"])

    @router.post(
        "/v1/service_accounts/{service_account_id}/api_keys",
        status_code=201,
    )
    async def create_api_key(
        service_account_id: UUID,
        payload: CreateApiKeyRequest,
        principal: Annotated[Principal, Depends(require("api_key", "write"))],
        keys: Annotated[ApiKeyStore, Depends(_get_keys)],
        accounts: Annotated[ServiceAccountStore, Depends(_get_accounts)],
        secrets: Annotated[SecretStore, Depends(_get_secret_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        sa = await accounts.get(
            tenant_id=principal.tenant_id, service_account_id=service_account_id
        )
        if sa is None:
            raise HTTPException(status_code=404, detail="service_account not found")
        if not sa.is_active:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "SERVICE_ACCOUNT_INACTIVE",
                    "message": "cannot mint API keys for an inactive service account",
                },
            )

        generated = mint_api_key(tenant_id=principal.tenant_id)
        # Retry once on prefix collision (probability ≈ 1/2^28; cheap belt).
        for attempt in range(2):
            try:
                key = await keys.create(
                    tenant_id=principal.tenant_id,
                    service_account_id=sa.id,
                    prefix=generated.prefix,
                    secret_hash=generated.secret_hash,
                    scopes=payload.scopes,
                    expires_at=payload.expires_at,
                    created_by=principal.subject_id,
                )
                break
            except DuplicateApiKeyPrefixError:
                if attempt == 1:
                    logger.warning("api_key.prefix_collision_retry_failed")
                    raise HTTPException(
                        status_code=500,
                        detail={"code": "INTERNAL_ERROR", "message": "regenerate API key"},
                    ) from None
                generated = mint_api_key(tenant_id=principal.tenant_id)
        try:
            await secrets.put(_vault_name(principal.tenant_id, key.id), generated.plaintext)
        except Exception:
            logger.warning("api_key.vault_put_failed api_key_id=%s", key.id)
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.API_KEY_CREATE,
            resource_type="api_key",
            resource_id=str(key.id),
            trace_id=current_trace_id_hex(),
            details={
                "service_account_id": str(sa.id),
                "scopes": [s.value for s in payload.scopes],
            },
        )
        created = ApiKeyCreated.from_key(api_key=key, plaintext=generated.plaintext)
        return {"success": True, "data": created.model_dump(mode="json"), "error": None}

    @router.get("/v1/service_accounts/{service_account_id}/api_keys")
    async def list_api_keys(
        service_account_id: UUID,
        principal: Annotated[Principal, Depends(require("api_key", "read"))],
        keys: Annotated[ApiKeyStore, Depends(_get_keys)],
    ) -> dict[str, object]:
        items = await keys.list_by_service_account(
            tenant_id=principal.tenant_id, service_account_id=service_account_id
        )
        return {
            "success": True,
            "data": {"items": [k.model_dump(mode="json") for k in items], "total": len(items)},
            "error": None,
        }

    @router.get("/v1/api_keys")
    async def list_api_keys_admin(
        request: Request,
        principal: Annotated[Principal, Depends(require("api_key", "read"))],
        keys: Annotated[ApiKeyStore, Depends(_get_keys)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        service_account_id: Annotated[UUID | None, Query()] = None,
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,  # Stream N
    ) -> dict[str, object]:
        """Stream N — top-level admin list across SAs / tenants.

        - Tenant admin: ``tenant_id`` omitted / equals home → list all
          keys in their tenant, optional ``service_account_id`` filter.
        - System admin: ``tenant_id=*`` → list every tenant's keys.
        """
        scope = await ensure_tenant_scope(
            principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/api_keys",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                items = await keys.list_all_tenants(service_account_id=service_account_id)
            else:
                items = await keys.list_by_tenant(
                    tenant_id=scope.tenant_id, service_account_id=service_account_id
                )
        return {
            "success": True,
            "data": {
                "items": [k.model_dump(mode="json") for k in items],
                "total": len(items),
                "cross_tenant": isinstance(scope, CrossTenant),
            },
            "error": None,
        }

    @router.delete("/v1/api_keys/{api_key_id}", status_code=204)
    async def revoke_api_key(
        api_key_id: UUID,
        principal: Annotated[Principal, Depends(require("api_key", "delete"))],
        keys: Annotated[ApiKeyStore, Depends(_get_keys)],
        secrets: Annotated[SecretStore, Depends(_get_secret_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> None:
        ok = await keys.revoke(tenant_id=principal.tenant_id, api_key_id=api_key_id)
        if not ok:
            raise HTTPException(status_code=404, detail="api_key not found")
        try:
            await secrets.delete(_vault_name(principal.tenant_id, api_key_id))
        except Exception:
            # 不带 api_key_id:它是用户可控的路径参数(CodeQL log-injection 面),
            # 且紧随其后的 API_KEY_REVOKE 审计行已带 resource_id,同请求可关联。
            logger.warning("api_key.vault_delete_failed tenant_id=%s", principal.tenant_id)
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.API_KEY_REVOKE,
            resource_type="api_key",
            resource_id=str(api_key_id),
            trace_id=current_trace_id_hex(),
        )

    @router.post("/v1/api_keys/{api_key_id}/rotate", status_code=201)
    async def rotate_api_key(
        api_key_id: UUID,
        payload: RotateApiKeyRequest,
        principal: Annotated[Principal, Depends(require("api_key", "write"))],
        keys: Annotated[ApiKeyStore, Depends(_get_keys)],
        secrets: Annotated[SecretStore, Depends(_get_secret_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        """Stream K.K1 — double-active key rotation.

        Issues a replacement bearer for ``api_key_id`` while keeping the
        old bearer alive for ``grace_period_s`` so live clients can swap
        without an outage. Mini-ADR K-1 (STREAM-K-DESIGN § 4) commits to
        the double-active model; immediate revocation stays available via
        ``DELETE /v1/api_keys/{id}``.

        Returns ``201`` with both the rotated old row (including
        ``rotated_at`` + ``grace_period_s``) and the freshly minted
        ``ApiKeyCreated`` carrying the new plaintext.

        ``404`` for unknown id / wrong tenant / already revoked / already
        rotated — operators must pick one explicit action at a time so
        the audit trail stays unambiguous.
        """
        generated = mint_api_key(tenant_id=principal.tenant_id)
        rotated_at = datetime.now(UTC)
        # CodeQL py/uninitialized-local — initialise so static analysis
        # sees ``result`` defined on every branch (the only way the loop
        # exits without a ``break`` is the inner ``raise``, which we keep
        # paranoia-fallback safe with this default).
        result: tuple[ApiKey, ApiKey] | None = None
        # One retry on prefix collision matches ``create_api_key`` above.
        for attempt in range(2):
            try:
                result = await keys.rotate(
                    tenant_id=principal.tenant_id,
                    api_key_id=api_key_id,
                    new_prefix=generated.prefix,
                    new_secret_hash=generated.secret_hash,
                    grace_period_s=payload.grace_period_s,
                    rotated_at=rotated_at,
                    actor_id=principal.subject_id,
                )
                break
            except DuplicateApiKeyPrefixError:
                if attempt == 1:
                    logger.warning("api_key.rotate_prefix_collision_retry_failed")
                    raise HTTPException(
                        status_code=500,
                        detail={"code": "INTERNAL_ERROR", "message": "regenerate API key"},
                    ) from None
                generated = mint_api_key(tenant_id=principal.tenant_id)

        if result is None:
            # Unknown id, wrong tenant, already revoked, or already rotated.
            raise HTTPException(status_code=404, detail="api_key not found")

        old_key, new_key = result
        # rotate() always mints a fresh id for the new row (the old row is
        # kept in place, stamped with rotated_at, for the grace window) —
        # so the vault write follows the id, and the old id's plaintext is
        # no longer viewable once rotated.
        try:
            await secrets.put(_vault_name(principal.tenant_id, new_key.id), generated.plaintext)
        except Exception:
            logger.warning("api_key.vault_put_failed api_key_id=%s", new_key.id)
        try:
            await secrets.delete(_vault_name(principal.tenant_id, old_key.id))
        except Exception:
            logger.warning("api_key.vault_delete_failed api_key_id=%s", old_key.id)
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.API_KEY_ROTATE,
            resource_type="api_key",
            resource_id=str(old_key.id),
            trace_id=current_trace_id_hex(),
            details={
                "new_api_key_id": str(new_key.id),
                "grace_period_s": payload.grace_period_s,
            },
        )
        created = ApiKeyCreated.from_key(api_key=new_key, plaintext=generated.plaintext)
        return {
            "success": True,
            "data": {
                "old_api_key": old_key.model_dump(mode="json"),
                "new_api_key": created.model_dump(mode="json"),
            },
            "error": None,
        }

    @router.post("/v1/api_keys/{api_key_id}/reveal")
    async def reveal_api_key(
        api_key_id: UUID,
        principal: Annotated[Principal, Depends(require("api_key", "write"))],
        keys: Annotated[ApiKeyStore, Depends(_get_keys)],
        secrets: Annotated[SecretStore, Depends(_get_secret_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        """Re-display the plaintext bearer for a previously created key.

        Only keys minted (or rotated) after vault storage landed have a
        plaintext to show — older rows 404 with
        ``API_KEY_PLAINTEXT_UNAVAILABLE`` (rotate to get a viewable one).
        """
        record = await _find_api_key(keys, tenant_id=principal.tenant_id, api_key_id=api_key_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"code": "API_KEY_NOT_FOUND"})
        try:
            plaintext = await secrets.get(_vault_name(principal.tenant_id, api_key_id))
        except SecretNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "API_KEY_PLAINTEXT_UNAVAILABLE",
                    "message": "key predates reveal support; rotate to get a viewable one",
                },
            ) from exc
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.API_KEY_REVEAL,
            resource_type="api_key",
            resource_id=str(api_key_id),
            trace_id=current_trace_id_hex(),
        )
        return {"success": True, "data": {"plaintext": plaintext}, "error": None}

    return router
