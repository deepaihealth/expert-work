"""Platform MCP connector catalog CRUD API — Stream W (Mini-ADR W-6).

system_admin-only CRUD over the platform-curated MCP connector catalog. Every
handler:

* first gates on the RBAC matrix via ``require("mcp_catalog", <action>)``
  (system_admin auto-gets tenant-ADMIN authority there), then re-checks
  ``principal.is_system_admin`` inline — defense in depth: this is a *platform*
  surface (NULL-tenant rows), same precedent as ``platform_config.py``;
* drives every store call inside ``bypass_rls_session()`` because the catalog
  rows are tenant-less and the RLS policy would otherwise hide them from a
  normally-scoped session (the W-8 trap);
* audits ``name``/``category``/``required_tier``/``transport`` only — never any
  secret value (the catalog declares auth-field *names*, not tenant secrets).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from control_plane.api._authz import platform_only, require
from control_plane.audit import emit
from control_plane.mcp_probe import McpProbeError, probe_remote_mcp
from control_plane.tenant_scope import bypass_rls_session
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import (
    McpConnectorCatalogAlreadyExistsError,
    McpConnectorCatalogInUseError,
    McpConnectorCatalogNotFoundError,
    McpConnectorCatalogStore,
    McpOAuthConnectionStore,
    TenantMcpServerStore,
)
from expert_work.protocol import (
    AuditAction,
    McpConnectorCatalogPatch,
    McpConnectorCatalogRecord,
    McpConnectorCatalogUpsert,
    Principal,
)
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.secret_store import SecretStore, parse_secret_ref
from orchestrator.tools.mcp import MCPToolDef

logger = logging.getLogger("expert_work.control_plane.mcp_catalog")

_DEFAULT_PROBE_TIMEOUT_S = 30.0


#: 403 body for a non-system-admin caller. Per-router on purpose: the message
#: names the resource being protected, which is what makes the refusal actionable.
_PLATFORM_SCOPE_MESSAGE = "only a system admin may manage the MCP connector catalog"


def _get_catalog_store(request: Request) -> McpConnectorCatalogStore:
    return request.app.state.mcp_connector_catalog_store  # type: ignore[no-any-return]


def _get_oauth_store(request: Request) -> McpOAuthConnectionStore:
    return request.app.state.mcp_oauth_connection_store  # type: ignore[no-any-return]


def _get_tenant_mcp_server_store(request: Request) -> TenantMcpServerStore:
    return request.app.state.tenant_mcp_server_store  # type: ignore[no-any-return]


def _get_secret_store(request: Request) -> SecretStore:
    return request.app.state.secret_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_platform_mcp_pool_service(request: Request) -> object:  # type: ignore[no-untyped-def]
    return getattr(request.app.state, "platform_mcp_pool_service", None)


def _get_agent_runtime(request: Request) -> object:  # type: ignore[no-untyped-def]
    return getattr(request.app.state, "agent_runtime", None)


async def _invalidate_platform_mcp(pool_service: object, agent_runtime: object) -> None:
    """Rebuild the platform shared pool + evict every cached agent (P1b).

    A catalog create / update / delete changes the process-global platform
    shared MCP pool, which feeds every tenant's build — so the pool is dropped
    (next build rebuilds from the catalog) and every cached built-agent is
    invalidated across all tenants.
    """
    if pool_service is not None:
        await pool_service.invalidate()  # type: ignore[attr-defined]
    if agent_runtime is not None:
        agent_runtime.invalidate_all()  # type: ignore[attr-defined]


def _bearer_secret_name(name: str) -> str:
    """SecretStore key for a platform connector's shared bearer token (slug=name)."""
    return f"expert-work/platform/mcp/{name}/token"


async def _probe_catalog_server(
    *,
    name: str,
    transport: str,
    url: str,
    bearer_token: str | None,
    timeout_s: float | None,
    sse_read_timeout_s: float | None,
) -> Sequence[MCPToolDef]:
    """Connect-probe a platform server (connect + list_tools). Raises McpProbeError.

    Only ``none``/``bearer`` servers are probeable platform-side; ``oauth2`` uses
    per-user tokens the platform never holds, so callers skip it.
    """
    return await probe_remote_mcp(
        name=name,
        transport=transport,
        url=url,
        bearer_token=bearer_token,
        timeout_s=timeout_s if timeout_s is not None else _DEFAULT_PROBE_TIMEOUT_S,
        sse_read_timeout_s=sse_read_timeout_s,
    )


def _public(record: McpConnectorCatalogRecord) -> dict[str, object]:
    """Response projection — never leak the ``secret://`` ref; expose only a flag."""
    data = record.model_dump(mode="json")
    data.pop("bearer_token_ref", None)
    data["has_bearer_token"] = record.bearer_token_ref is not None
    return data


def build_mcp_catalog_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/platform/mcp-catalog",
        tags=["mcp_catalog"],
        dependencies=[Depends(platform_only(_PLATFORM_SCOPE_MESSAGE))],
    )

    @router.post("", status_code=201)
    async def create_catalog_entry(
        payload: McpConnectorCatalogUpsert,
        principal: Annotated[Principal, Depends(require("mcp_catalog", "write"))],
        store: Annotated[McpConnectorCatalogStore, Depends(_get_catalog_store)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        pool_service: Annotated[object, Depends(_get_platform_mcp_pool_service)],
        agent_runtime: Annotated[object, Depends(_get_agent_runtime)],
    ) -> dict[str, object]:
        upsert = payload
        # Validate connectivity BEFORE persisting (parity with custom-server
        # registration): a none/bearer server must connect + list tools. oauth2
        # is per-user (no platform token) → not probeable platform-side, skipped.
        if payload.auth_type in ("none", "bearer"):
            raw_token = (
                payload.bearer_token.get_secret_value()
                if payload.bearer_token is not None
                else None
            )
            try:
                await _probe_catalog_server(
                    name=payload.name,
                    transport=payload.transport,
                    url=payload.url_template,
                    bearer_token=raw_token,
                    timeout_s=payload.timeout_s,
                    sse_read_timeout_s=payload.sse_read_timeout_s,
                )
            except McpProbeError as exc:
                raise HTTPException(
                    status_code=422, detail={"code": exc.code, "message": exc.message}
                ) from exc
        if payload.bearer_token is not None:
            # Platform shared bearer (A): write the token to the SecretStore and
            # persist only the ref; the plaintext never reaches the DB / logs.
            sname = _bearer_secret_name(payload.name)
            await secret_store.put(sname, payload.bearer_token.get_secret_value())
            upsert = payload.model_copy(
                update={"bearer_token_ref": f"secret://{sname}", "bearer_token": None}
            )
        try:
            async with bypass_rls_session():
                record = await store.create(upsert=upsert, actor_id=principal.subject_id)
        except McpConnectorCatalogAlreadyExistsError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "CATALOG_DUPLICATE", "message": "name already registered"},
            ) from exc
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.MCP_CATALOG_CREATE,
            resource_type="mcp_connector_catalog",
            resource_id=record.name,
            trace_id=current_trace_id_hex(),
            details={
                "name": record.name,
                "category": record.category,
                "required_tier": record.required_tier.value,
                "transport": record.transport,
            },  # NEVER include any secret value
        )
        await _invalidate_platform_mcp(pool_service, agent_runtime)
        return {"success": True, "data": _public(record), "error": None}

    @router.get("")
    async def list_catalog_entries(
        principal: Annotated[Principal, Depends(require("mcp_catalog", "read"))],
        store: Annotated[McpConnectorCatalogStore, Depends(_get_catalog_store)],
        category: Annotated[str | None, Query()] = None,
    ) -> dict[str, object]:
        async with bypass_rls_session():
            rows = await store.list(category=category)
        return {
            "success": True,
            "data": [_public(r) for r in rows],
            "error": None,
        }

    @router.get("/{catalog_id}")
    async def get_catalog_entry(
        catalog_id: Annotated[UUID, Path()],
        principal: Annotated[Principal, Depends(require("mcp_catalog", "read"))],
        store: Annotated[McpConnectorCatalogStore, Depends(_get_catalog_store)],
    ) -> dict[str, object]:
        async with bypass_rls_session():
            record = await store.get_by_id(catalog_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "CATALOG_NOT_FOUND", "message": "not found"},
            )
        return {"success": True, "data": _public(record), "error": None}

    @router.post("/{catalog_id}/tools")
    async def list_catalog_tools(
        catalog_id: Annotated[UUID, Path()],
        principal: Annotated[Principal, Depends(require("mcp_catalog", "read"))],
        store: Annotated[McpConnectorCatalogStore, Depends(_get_catalog_store)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
    ) -> dict[str, object]:
        """Live-probe a configured platform server and list its tools.

        ``status`` is ``ok`` (tools listed) / ``unreachable`` (probe failed, with
        an ``error`` code) / ``not_probeable`` (oauth2 — per-user token the
        platform never holds). Always 200 (except 404/403) so the edit drawer can
        render the outcome inline rather than treating it as a request failure.
        """
        async with bypass_rls_session():
            record = await store.get_by_id(catalog_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "CATALOG_NOT_FOUND", "message": "not found"},
            )
        if record.auth_type == "oauth2":
            return {
                "success": True,
                "data": {"status": "not_probeable", "tool_count": 0, "tools": [], "error": None},
                "error": None,
            }
        token = (
            await secret_store.get(parse_secret_ref(record.bearer_token_ref))
            if record.bearer_token_ref is not None
            else None
        )
        try:
            tools = await _probe_catalog_server(
                name=record.name,
                transport=record.transport,
                url=record.url_template,
                bearer_token=token,
                timeout_s=record.timeout_s,
                sse_read_timeout_s=record.sse_read_timeout_s,
            )
        except McpProbeError as exc:
            return {
                "success": True,
                "data": {
                    "status": "unreachable",
                    "tool_count": 0,
                    "tools": [],
                    "error": exc.code,
                },
                "error": None,
            }
        return {
            "success": True,
            "data": {
                "status": "ok",
                "tool_count": len(tools),
                "tools": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "input_schema": t.input_schema,
                        # The platform-curated denylist state, so the edit page can
                        # render each tool's enable toggle without a second call.
                        "disabled": t.name in record.disabled_tools,
                    }
                    for t in tools
                ],
                "error": None,
            },
            "error": None,
        }

    @router.patch("/{catalog_id}")
    async def update_catalog_entry(
        catalog_id: Annotated[UUID, Path()],
        payload: McpConnectorCatalogPatch,
        principal: Annotated[Principal, Depends(require("mcp_catalog", "write"))],
        store: Annotated[McpConnectorCatalogStore, Depends(_get_catalog_store)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        pool_service: Annotated[object, Depends(_get_platform_mcp_pool_service)],
        agent_runtime: Annotated[object, Depends(_get_agent_runtime)],
    ) -> dict[str, object]:
        patch = payload
        # Load the existing row when the URL or token changes — needed to re-probe
        # and (for a re-pasted token) to resolve the secret path.
        existing = None
        if payload.bearer_token is not None or payload.url_template is not None:
            async with bypass_rls_session():
                existing = await store.get_by_id(catalog_id)
            if existing is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "CATALOG_NOT_FOUND", "message": "not found"},
                )
        # Re-validate connectivity when the URL or token changes (none/bearer).
        if (
            existing is not None
            and existing.auth_type in ("none", "bearer")
            and (payload.url_template is not None or payload.bearer_token is not None)
        ):
            probe_url = payload.url_template or existing.url_template
            if payload.bearer_token is not None:
                probe_token: str | None = payload.bearer_token.get_secret_value()
            elif existing.bearer_token_ref is not None:
                probe_token = await secret_store.get(parse_secret_ref(existing.bearer_token_ref))
            else:
                probe_token = None
            try:
                await _probe_catalog_server(
                    name=existing.name,
                    transport=existing.transport,
                    url=probe_url,
                    bearer_token=probe_token,
                    timeout_s=payload.timeout_s
                    if payload.timeout_s is not None
                    else existing.timeout_s,
                    sse_read_timeout_s=payload.sse_read_timeout_s
                    if payload.sse_read_timeout_s is not None
                    else existing.sse_read_timeout_s,
                )
            except McpProbeError as exc:
                raise HTTPException(
                    status_code=422, detail={"code": exc.code, "message": exc.message}
                ) from exc
        if payload.bearer_token is not None and existing is not None:
            # Re-paste the platform shared token: write the new value, persist ref.
            sname = _bearer_secret_name(existing.name)
            await secret_store.put(sname, payload.bearer_token.get_secret_value())
            patch = payload.model_copy(
                update={"bearer_token_ref": f"secret://{sname}", "bearer_token": None}
            )
        try:
            async with bypass_rls_session():
                record = await store.update(catalog_id=catalog_id, patch=patch)
        except McpConnectorCatalogNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "CATALOG_NOT_FOUND", "message": "not found"},
            ) from exc
        except ValueError as exc:
            # The merged record violated a cross-field invariant (e.g. the
            # bearer/secret rule) — the record validator raised on re-validation.
            raise HTTPException(
                status_code=422,
                detail={"code": "CATALOG_INVALID", "message": str(exc)},
            ) from exc
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.MCP_CATALOG_UPDATE,
            resource_type="mcp_connector_catalog",
            resource_id=record.name,
            trace_id=current_trace_id_hex(),
            details={
                "name": record.name,
                "category": record.category,
                "required_tier": record.required_tier.value,
                "enabled": record.enabled,
            },
        )
        await _invalidate_platform_mcp(pool_service, agent_runtime)
        return {"success": True, "data": _public(record), "error": None}

    @router.delete("/{catalog_id}", status_code=204)
    async def delete_catalog_entry(
        catalog_id: Annotated[UUID, Path()],
        principal: Annotated[Principal, Depends(require("mcp_catalog", "delete"))],
        store: Annotated[McpConnectorCatalogStore, Depends(_get_catalog_store)],
        oauth_store: Annotated[McpOAuthConnectionStore, Depends(_get_oauth_store)],
        tenant_mcp_server_store: Annotated[
            TenantMcpServerStore, Depends(_get_tenant_mcp_server_store)
        ],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        pool_service: Annotated[object, Depends(_get_platform_mcp_pool_service)],
        agent_runtime: Annotated[object, Depends(_get_agent_runtime)],
        force: Annotated[bool, Query()] = False,
    ) -> None:
        # Resolve the row first so the audit record carries the stable name.
        async with bypass_rls_session():
            existing = await store.get_by_id(catalog_id)
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "CATALOG_NOT_FOUND", "message": "not found"},
            )

        # Instance check FIRST — before any destructive action, including the
        # force OAuth-connection cascade below (Task 8 review I-1). A tenant
        # that has instantiated this catalog entry into tenant_mcp_server blocks
        # deletion unconditionally (``force`` never bypasses this — only the
        # OAuth-connection cascade is force-able). Without this order-first
        # check, an overlapping state (instantiated AND holding OAuth
        # connections) would let ``force=true`` destroy real user connections
        # and secrets before ``store.delete``'s RESTRICT-triggered
        # IntegrityError mapping ever got a chance to say no — the IntegrityError
        # mapping below stays in place as a defense-in-depth fallback (e.g. a
        # race between this check and the delete).
        async with bypass_rls_session():
            instance_count = await tenant_mcp_server_store.count_for_catalog(catalog_id=catalog_id)
        if instance_count > 0:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CATALOG_IN_USE",
                    "message": "catalog entry is instantiated by one or more tenants",
                },
            )

        # Users may still hold live OAuth connections to this connector — the
        # tenant_mcp_server RESTRICT guard above never fires for oauth2
        # connectors (no tenant_mcp_server row is ever created for them), so
        # this is the only app-level protection. Default: 409 with the count
        # so the admin sees the blast radius; ``?force=true`` opts into the
        # cascade (delete every connection + its token secrets).
        async with bypass_rls_session():
            connection_count = await oauth_store.count_for_catalog(catalog_id=catalog_id)
        if connection_count > 0 and not force:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CATALOG_HAS_OAUTH_CONNECTIONS",
                    "message": (
                        f"{connection_count} user OAuth connection(s) still reference this "
                        "connector — pass ?force=true to delete them along with the catalog "
                        "entry"
                    ),
                    "count": connection_count,
                },
            )

        connections_removed = 0
        secrets_removed = 0
        secrets_failed = 0
        if force and connection_count > 0:
            # ``list_for_catalog`` defaults to limit=1000 — pass the count we
            # already have (M-1) so a connector with >1000 connections doesn't
            # silently orphan secrets past the default cap and undercount the
            # audit totals below.
            async with bypass_rls_session():
                connections = await oauth_store.list_for_catalog(
                    catalog_id=catalog_id, limit=max(connection_count, 1)
                )
            for connection in connections:
                for ref in (connection.access_token_ref, connection.refresh_token_ref):
                    if not ref:
                        continue
                    try:
                        await secret_store.delete(parse_secret_ref(ref))
                        secrets_removed += 1
                    except Exception:
                        secrets_failed += 1
                        logger.warning(
                            "mcp_catalog.delete_force_secret_cleanup_failed", exc_info=True
                        )
            async with bypass_rls_session():
                connections_removed = await oauth_store.delete_for_catalog(catalog_id=catalog_id)

        try:
            async with bypass_rls_session():
                await store.delete(catalog_id)
        except McpConnectorCatalogNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "CATALOG_NOT_FOUND", "message": "not found"},
            ) from exc
        except McpConnectorCatalogInUseError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CATALOG_IN_USE",
                    "message": "catalog entry is instantiated by one or more tenants",
                },
            ) from exc
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.MCP_CATALOG_DELETE,
            resource_type="mcp_connector_catalog",
            resource_id=existing.name,
            trace_id=current_trace_id_hex(),
            details={
                "name": existing.name,
                "connections_removed": connections_removed,
                "secrets_removed": secrets_removed,
                "secrets_failed": secrets_failed,
            },
        )
        await _invalidate_platform_mcp(pool_service, agent_runtime)

    return router
