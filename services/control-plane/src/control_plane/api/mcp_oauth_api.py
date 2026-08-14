"""``/v1/mcp-oauth`` + catalog OAuth initiate — Stream MCP-OAUTH (OA-3a).

Wires the OAuth 2.1 engine (``control_plane.mcp_oauth``) to HTTP so a **user**
can connect an ``oauth2`` catalog connector:

* ``POST /v1/mcp-servers/catalog/{id}/oauth/initiate`` — discover the AS, mint
  PKCE + state, create a ``pending`` ``mcp_oauth_connection``, return the browser
  authorize URL.
* ``GET /v1/mcp-oauth/callback`` — validate ``state``, exchange the code, store
  the tokens in the encrypted secret store (only ``secret://`` refs persist),
  mark the connection ``connected``.

Per-user pool resolution + runtime token injection land in OA-3b — connecting
establishes the token; the agent consumes it next.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from control_plane.api._authz import console_only, require
from control_plane.audit import emit as audit_emit
from control_plane.mcp_oauth import (
    McpOAuthError,
    build_authorize_url,
    default_http_client,
    discover_oauth_metadata,
    exchange_code,
    generate_pkce,
    generate_state,
    validate_oauth_redirect,
)
from control_plane.tenancy import TenantConfigNotConfiguredError
from control_plane.tenant_scope import bypass_rls_session
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import (
    McpConnectorCatalogStore,
    McpOAuthConnectionAlreadyExistsError,
    McpOAuthConnectionStore,
)
from expert_work.protocol import (
    AuditAction,
    AuditResult,
    McpOAuthConnectionPatch,
    Principal,
    TenantPlan,
    tier_satisfies,
)
from expert_work.protocol.mcp_oauth_connection import McpOAuthConnectionRecord
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.secret_store import SecretStore, parse_secret_ref

logger = logging.getLogger("expert_work.control_plane.mcp_oauth_api")


def _catalog_store(request: Request) -> McpConnectorCatalogStore:
    return request.app.state.mcp_connector_catalog_store  # type: ignore[no-any-return]


def _conn_store(request: Request) -> McpOAuthConnectionStore:
    return request.app.state.mcp_oauth_connection_store  # type: ignore[no-any-return]


def _secret_store(request: Request) -> SecretStore:
    return request.app.state.secret_store  # type: ignore[no-any-return]


def _audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


class InitiateOAuthRequest(BaseModel):
    """Optional initiate body (multi-client OAuth). A client (web / native /
    embedded) supplies its OWN ``redirect_uri`` so the provider returns the
    browser to that client; omitted → the global default."""

    model_config = ConfigDict(extra="forbid")
    redirect_uri: str | None = None


def _global_redirect(request: Request) -> str | None:
    return getattr(request.app.state.settings, "mcp_oauth_redirect_uri", None)  # type: ignore[no-any-return]


def _resolve_redirect_uri(request: Request, client_redirect: str | None) -> str:
    """Resolve + validate the effective redirect URI for an initiate.

    Prefers the client-supplied ``redirect_uri`` (validated against the
    allowlist), else the global default. Raises 503 when neither is available,
    422 when the client value is not allowlisted (open-redirect guard)."""
    settings = request.app.state.settings
    global_uri = _global_redirect(request)
    effective = client_redirect or global_uri
    if not effective:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "MCP_OAUTH_NOT_CONFIGURED",
                "message": "no redirect_uri supplied and mcp_oauth_redirect_uri is not configured",
            },
        )
    try:
        return validate_oauth_redirect(
            effective,
            allowlist=getattr(settings, "mcp_oauth_redirect_allowlist", []),
            allow_loopback=getattr(settings, "mcp_oauth_allow_loopback_redirect", True),
            default=global_uri,
        )
    except McpOAuthError as exc:
        raise HTTPException(
            status_code=422, detail={"code": exc.code, "message": exc.message}
        ) from exc


async def _resolve_plan(request: Request, tenant_id: UUID) -> TenantPlan:
    svc = getattr(request.app.state, "tenant_config_service", None)
    if svc is None:
        return TenantPlan.FREE
    try:
        cfg = await svc.get(tenant_id=tenant_id)
    except TenantConfigNotConfiguredError:
        return TenantPlan.FREE
    return cfg.plan  # type: ignore[no-any-return]


async def _tenant_enabled(request: Request, tenant_id: UUID, name: str) -> bool:
    """Whether the tenant has opted into this platform connector (mcp_allowlist).

    Opt-in gate (P2): a tenant admin must enable a platform oauth server before
    its users may authorize. Empty / unconfigured allowlist = nothing enabled.
    """
    svc = getattr(request.app.state, "tenant_config_service", None)
    if svc is None:
        return False
    try:
        cfg = await svc.get(tenant_id=tenant_id)
    except TenantConfigNotConfiguredError:
        return False
    return name in cfg.mcp_allowlist


def _secret_name(tenant_id: UUID, connection_id: UUID, kind: str) -> str:
    # connection_id (a UUID) keeps the path injection-safe — user_id is opaque.
    return f"expert-work/tenant/{tenant_id}/mcp-oauth/{connection_id}/{kind}"


# Fields safe to expose: never the token refs or the short-lived flow secrets.
_PUBLIC_DROP = frozenset({"access_token_ref", "refresh_token_ref", "oauth_state", "pkce_verifier"})


def _public(record: McpOAuthConnectionRecord) -> dict[str, object]:
    data: dict[str, object] = record.model_dump(mode="json")
    for key in _PUBLIC_DROP:
        data.pop(key, None)
    return data


async def _invalidate_user_caches(request: Request, tenant_id: UUID, user_id: str) -> None:
    """Drop the user's OAuth pool + per-user agents so the next run rebuilds.

    Both services are optional (tests may not wire them)."""
    pool_svc = getattr(request.app.state, "user_mcp_oauth_pool_service", None)
    if pool_svc is not None:
        await pool_svc.invalidate(tenant_id, user_id)
    agent_runtime = getattr(request.app.state, "agent_runtime", None)
    if agent_runtime is not None:
        agent_runtime.invalidate_user(tenant_id, user_id)


def build_mcp_oauth_router() -> APIRouter:
    """OA-3a per-user OAuth initiate + callback."""
    router = APIRouter(tags=["mcp_oauth"], dependencies=[Depends(console_only())])

    @router.post("/v1/mcp-servers/catalog/{catalog_id}/oauth/initiate", response_model=None)
    async def initiate(
        catalog_id: Annotated[UUID, Path()],
        principal: Annotated[Principal, Depends(require("mcp_oauth", "write"))],
        request: Request,
        payload: Annotated[InitiateOAuthRequest | None, Body()] = None,
    ) -> JSONResponse:
        tenant_id = principal.tenant_id
        user_id = principal.subject_id
        catalog_store = _catalog_store(request)
        conn_store = _conn_store(request)
        # Multi-client OAuth: the client may supply its own redirect_uri.
        redirect_uri = _resolve_redirect_uri(
            request, payload.redirect_uri if payload is not None else None
        )

        async with bypass_rls_session():
            entry = await catalog_store.get_by_id(catalog_id)
        if entry is None or not entry.enabled:
            raise HTTPException(
                status_code=404,
                detail={"code": "MCP_CATALOG_NOT_FOUND", "message": "catalog entry not found"},
            )
        if entry.auth_type != "oauth2" or not entry.oauth_client_id:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "MCP_CATALOG_NOT_OAUTH",
                    "message": "catalog entry is not an oauth2 connector",
                },
            )
        plan = await _resolve_plan(request, tenant_id)
        if not tier_satisfies(plan, entry.required_tier):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "MCP_CATALOG_TIER_REQUIRED",
                    "message": f"connector requires the {entry.required_tier.value} tier",
                },
            )
        # Tenant-level opt-in gate (P2): a tenant admin must enable the connector
        # for the tenant before its users may authorize ("租户选择使用").
        if not await _tenant_enabled(request, tenant_id, entry.name):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "MCP_CATALOG_NOT_ENABLED",
                    "message": "connector not enabled for this tenant",
                },
            )

        # oauth2 entries carry the MCP server URL directly in url_template.
        mcp_url = entry.url_template
        async with default_http_client() as http:
            try:
                metadata = await discover_oauth_metadata(mcp_url=mcp_url, http=http)
            except McpOAuthError as exc:
                raise HTTPException(
                    status_code=502, detail={"code": exc.code, "message": exc.message}
                ) from exc

        pkce = generate_pkce()
        state = generate_state()

        # Re-initiating resets any prior connection for this connector.
        existing = await conn_store.get_for_connector(
            tenant_id=tenant_id, user_id=user_id, catalog_id=catalog_id
        )
        if existing is not None:
            await conn_store.delete(connection_id=existing.id, tenant_id=tenant_id, user_id=user_id)
        try:
            connection = await conn_store.create(
                tenant_id=tenant_id,
                user_id=user_id,
                catalog_id=catalog_id,
                name=entry.name,
                resolved_url=mcp_url,
                scopes=entry.oauth_scopes or "",
                redirect_uri=redirect_uri,
                oauth_state=state,
                pkce_verifier=pkce.verifier,
            )
        except McpOAuthConnectionAlreadyExistsError as exc:  # pragma: no cover — deleted above
            raise HTTPException(status_code=409, detail="connection already exists") from exc

        authorize_url = build_authorize_url(
            metadata=metadata,
            client_id=entry.oauth_client_id,
            redirect_uri=redirect_uri,
            scopes=entry.oauth_scopes or "",
            state=state,
            pkce_challenge=pkce.challenge,
        )
        return JSONResponse(
            status_code=201,
            content={
                "connection_id": str(connection.id),
                "authorize_url": authorize_url,
                "status": "pending",
            },
        )

    @router.get("/v1/mcp-oauth/callback", response_model=None)
    async def callback(
        principal: Annotated[Principal, Depends(require("mcp_oauth", "write"))],
        request: Request,
        state: Annotated[str, Query()],
        code: Annotated[str, Query()],
    ) -> JSONResponse:
        tenant_id = principal.tenant_id
        user_id = principal.subject_id
        catalog_store = _catalog_store(request)
        conn_store = _conn_store(request)
        secret_store = _secret_store(request)
        audit = _audit(request)

        connection = await conn_store.get_by_state(
            tenant_id=tenant_id, user_id=user_id, oauth_state=state
        )
        if connection is None or connection.status != "pending":
            raise HTTPException(
                status_code=400,
                detail={"code": "MCP_OAUTH_STATE_INVALID", "message": "unknown or stale state"},
            )
        async with bypass_rls_session():
            entry = await catalog_store.get_by_id(connection.catalog_id)
        if entry is None or not entry.oauth_client_id:
            raise HTTPException(
                status_code=404,
                detail={"code": "MCP_CATALOG_NOT_FOUND", "message": "catalog entry not found"},
            )

        # OAuth requires the token-exchange redirect_uri to match the one used at
        # authorize — reuse the value stored on the connection at initiate (falls
        # back to the global default for pre-multi-client rows).
        redirect_uri = connection.redirect_uri or _global_redirect(request)
        if not redirect_uri:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "MCP_OAUTH_NOT_CONFIGURED",
                    "message": "connection has no redirect_uri and no global default is configured",
                },
            )
        async with default_http_client() as http:
            try:
                metadata = await discover_oauth_metadata(mcp_url=connection.resolved_url, http=http)
                tokens = await exchange_code(
                    metadata=metadata,
                    client_id=entry.oauth_client_id,
                    code=code,
                    code_verifier=connection.pkce_verifier or "",
                    redirect_uri=redirect_uri,
                    http=http,
                )
            except McpOAuthError as exc:
                await conn_store.update(
                    connection_id=connection.id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    patch=McpOAuthConnectionPatch(status="error", last_error=exc.code),
                )
                raise HTTPException(
                    status_code=502, detail={"code": exc.code, "message": exc.message}
                ) from exc

        access_name = _secret_name(tenant_id, connection.id, "access")
        await secret_store.put(access_name, tokens.access_token)
        access_ref = f"secret://{access_name}"
        refresh_ref: str | None = None
        if tokens.refresh_token:
            refresh_name = _secret_name(tenant_id, connection.id, "refresh")
            await secret_store.put(refresh_name, tokens.refresh_token)
            refresh_ref = f"secret://{refresh_name}"

        expires_at = (
            datetime.now(tz=UTC) + timedelta(seconds=tokens.expires_in)
            if tokens.expires_in
            else None
        )
        updated = await conn_store.update(
            connection_id=connection.id,
            tenant_id=tenant_id,
            user_id=user_id,
            patch=McpOAuthConnectionPatch(
                status="connected",
                access_token_ref=access_ref,
                refresh_token_ref=refresh_ref,
                token_expires_at=expires_at,
                scopes=tokens.scope if tokens.scope is not None else None,
                last_refresh_at=datetime.now(tz=UTC),
                clear_flow_state=True,
            ),
        )
        await audit_emit(
            audit,
            tenant_id=tenant_id,
            actor_id=user_id,
            action=AuditAction.MCP_SERVER_CREATE,
            resource_type="mcp_oauth_connection",
            resource_id=str(updated.id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={"scope": "oauth", "name": updated.name, "source": "oauth_callback"},
        )
        # OA-3b — drop the user's cached OAuth pool + per-user agents so the next
        # run rebuilds with the new connection.
        await _invalidate_user_caches(request, tenant_id, user_id)
        return JSONResponse(
            status_code=200,
            content={"connection_id": str(updated.id), "name": updated.name, "status": "connected"},
        )

    @router.get("/v1/mcp-oauth/connections", response_model=None)
    async def list_connections(
        principal: Annotated[Principal, Depends(require("mcp_oauth", "read"))],
        request: Request,
    ) -> JSONResponse:
        """OA-4 — the caller's own OAuth connections (status / scopes / expiry /
        last_error). Token refs + flow secrets are never exposed."""
        conns = await _conn_store(request).list_for_user(
            tenant_id=principal.tenant_id, user_id=principal.subject_id
        )
        return JSONResponse(status_code=200, content={"items": [_public(c) for c in conns]})

    @router.delete("/v1/mcp-oauth/connections/{connection_id}", response_model=None)
    async def disconnect(
        connection_id: Annotated[UUID, Path()],
        principal: Annotated[Principal, Depends(require("mcp_oauth", "delete"))],
        request: Request,
    ) -> JSONResponse:
        """OA-4 — disconnect: delete the stored tokens, drop the row, invalidate
        the caches."""
        tenant_id = principal.tenant_id
        user_id = principal.subject_id
        conn_store = _conn_store(request)
        secret_store = _secret_store(request)
        audit = _audit(request)

        existing = await conn_store.get(
            connection_id=connection_id, tenant_id=tenant_id, user_id=user_id
        )
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "MCP_OAUTH_CONNECTION_NOT_FOUND", "message": "not found"},
            )
        # Best-effort token deletion: failures don't block the disconnect.
        for ref in (existing.access_token_ref, existing.refresh_token_ref):
            if ref:
                try:
                    await secret_store.delete(parse_secret_ref(ref))
                except Exception:
                    logger.warning("mcp_oauth.disconnect_secret_delete_failed")
        await conn_store.delete(connection_id=connection_id, tenant_id=tenant_id, user_id=user_id)
        await _invalidate_user_caches(request, tenant_id, user_id)
        await audit_emit(
            audit,
            tenant_id=tenant_id,
            actor_id=user_id,
            action=AuditAction.MCP_SERVER_DELETE,
            resource_type="mcp_oauth_connection",
            resource_id=str(connection_id),
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={"scope": "oauth", "name": existing.name, "source": "oauth_disconnect"},
        )
        return JSONResponse(status_code=204, content=None)

    return router
