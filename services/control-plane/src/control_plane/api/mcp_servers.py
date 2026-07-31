"""Tenant MCP server registration API — Stream V-C."""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from control_plane.api._authz import require
from control_plane.audit import emit
from control_plane.mcp_probe import McpProbeError, probe_remote_mcp
from control_plane.tenancy.tenant_config import TenantConfigNotConfiguredError
from control_plane.tenant_scope import (
    CrossTenant,
    SingleTenant,
    applied_scope,
    bypass_rls_session,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.common.url_validation import RemoteURLError, validate_remote_url
from expert_work.persistence import (
    McpConnectorCatalogStore,
    TenantMcpServerAlreadyExistsError,
    TenantMcpServerNotFoundError,
    TenantMcpServerStore,
)
from expert_work.protocol import (
    AgentSpecRecord,
    AgentSpecStatus,
    AuditAction,
    McpServerAuthType,
    McpServerProbeStatus,
    McpServerTransport,
    Principal,
    TenantConfigPatch,
    TenantMcpServerPatch,
    TenantMcpServerRecord,
    TenantPlan,
    tier_satisfies,
)
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.secret_store import SecretStore, parse_secret_ref

logger = logging.getLogger("expert_work.control_plane.api.mcp_servers")

_DEFAULT_TIMEOUT_S = 30.0

# Page size for the delete-time agent-spec reference sweep. The sweep MUST see
# every spec of the tenant — a single fixed page silently hid references past
# the boundary and deleted in-use servers.
_SPEC_PAGE_SIZE = 200


def manifest_references_server(spec_json: Mapping[str, Any], server_name: str) -> bool:
    """Return whether an agent manifest references the named MCP server.

    Reads ``spec.tools[].servers`` from the raw manifest dict (the
    ``MCPToolSpec.servers`` field is added in V-E; pre-V-E manifests have no
    ``servers`` key, so this is dormant — and forward-compatible — until then).
    """
    spec = spec_json.get("spec")
    if not isinstance(spec, Mapping):
        return False
    tools = spec.get("tools")
    if not isinstance(tools, list):
        return False
    for tool in tools:
        if not isinstance(tool, Mapping) or tool.get("type") != "mcp":
            continue
        servers = tool.get("servers")
        if isinstance(servers, list) and server_name in servers:
            return True
    return False


def manifest_uses_implicit_all(manifest: dict[str, object], /) -> bool:
    """True when the manifest has an ``mcp`` tool whose ``servers`` list is
    empty/absent — the documented "every available server" wildcard. Such an
    agent follows the live server set dynamically, so it is NOT a hard
    reference (delete proceeds), but the impact count is surfaced."""
    spec = manifest.get("spec")
    if not isinstance(spec, Mapping):
        return False
    tools = spec.get("tools")
    if not isinstance(tools, list):
        return False
    for tool in tools:
        if not isinstance(tool, Mapping) or tool.get("type") != "mcp":
            continue
        servers = tool.get("servers")
        # Absent (pre-V-E manifests) and explicit [] both mean the wildcard.
        if not isinstance(servers, list) or not servers:
            return True
    return False


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


# Custom HTTP headers (M1): name → value, values may be secrets (SecretStr keeps
# them out of logs/repr). Bounded to keep the encrypted blob small.
_MAX_CUSTOM_HEADERS = 32
CustomHeaders = dict[str, SecretStr]


class CreateMcpServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    transport: McpServerTransport
    url: str = Field(min_length=1)
    auth_type: McpServerAuthType = "none"
    token: SecretStr | None = None
    custom_headers: CustomHeaders | None = None
    sse_read_timeout_s: float | None = Field(default=None, gt=0, le=3600)
    timeout_s: float = Field(default=_DEFAULT_TIMEOUT_S, gt=0, le=300)


class UpdateMcpServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str | None = Field(default=None, min_length=1)
    token: SecretStr | None = None
    custom_headers: CustomHeaders | None = None
    sse_read_timeout_s: float | None = Field(default=None, gt=0, le=3600)
    timeout_s: float | None = Field(default=None, gt=0, le=300)
    enabled: bool | None = None


class TestConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transport: McpServerTransport
    url: str = Field(min_length=1)
    auth_type: McpServerAuthType = "none"
    token: SecretStr | None = None
    custom_headers: CustomHeaders | None = None
    sse_read_timeout_s: float | None = Field(default=None, gt=0, le=3600)
    timeout_s: float = Field(default=_DEFAULT_TIMEOUT_S, gt=0, le=300)


# ---------------------------------------------------------------------------
# DI accessors
# ---------------------------------------------------------------------------


def _get_store(request: Request) -> TenantMcpServerStore:
    return request.app.state.tenant_mcp_server_store  # type: ignore[no-any-return]


def _get_catalog_store(request: Request) -> McpConnectorCatalogStore:
    return request.app.state.mcp_connector_catalog_store  # type: ignore[no-any-return]


def _get_secret_store(request: Request) -> SecretStore:
    return request.app.state.secret_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_agent_spec_store(request: Request) -> object:
    # Wired as ``app.state.agent_spec_repo`` in create_app (Stream B.5).
    return getattr(request.app.state, "agent_spec_repo", None)


def _get_tenant_mcp_pool_service(request: Request) -> object:  # type: ignore[no-untyped-def]
    return getattr(request.app.state, "tenant_mcp_pool_service", None)


def _get_agent_runtime(request: Request) -> object:  # type: ignore[no-untyped-def]
    return getattr(request.app.state, "agent_runtime", None)


def _get_tenant_config_service(request: Request) -> object:  # type: ignore[no-untyped-def]
    return getattr(request.app.state, "tenant_config_service", None)


def _get_mcp_probe_limiter(request: Request) -> object:  # type: ignore[no-untyped-def]
    return getattr(request.app.state, "mcp_probe_limiter", None)


async def _enforce_probe_rate_limit(limiter: object, tenant_id: UUID) -> None:
    """Charge the dedicated MCP-probe bucket (audit #6); 429 on exhaustion.

    Every probe-bearing endpoint opens a server-side outbound connection to a
    tenant-chosen URL, so it gets a tighter bucket than the global tenant-tier
    limiter. ``None`` (limiter disabled / unwired) is a no-op so tests and dev
    that don't build it keep working.
    """
    if limiter is None:
        return
    decision = await limiter.acquire(dimension="mcp_probe", key=str(tenant_id))  # type: ignore[attr-defined]
    if not decision.allowed:
        retry_after = max(1, math.ceil(decision.retry_after_s))
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            detail={
                "code": "MCP_PROBE_RATE_LIMITED",
                "message": "too many MCP connection probes; slow down",
                "retry_after_s": retry_after,
            },
        )


async def _invalidate_tenant_mcp(
    pool_service: object, agent_runtime: object, tenant_id: UUID
) -> None:
    """Invalidate the tenant's MCP pool cache + any cached built-agents (Stream V-D).

    Called after each successful registry mutation (POST/PATCH/DELETE) so the
    next agent build picks up the changed server list. Both services are optional
    so existing tests that don't wire them continue to pass.
    """
    if pool_service is not None:
        await pool_service.invalidate(tenant_id)  # type: ignore[attr-defined]
    if agent_runtime is not None:
        agent_runtime.invalidate_tenant(tenant_id)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _token_secret_name(tenant_id: UUID, name: str) -> str:
    return f"expert-work/tenant/{tenant_id}/mcp/{name}/token"


def _headers_secret_name(tenant_id: UUID, name: str) -> str:
    return f"expert-work/tenant/{tenant_id}/mcp/{name}/headers"


# HTTP header field-name: a conservative token (letters/digits/hyphen). Blocks
# CR/LF/colon/whitespace so a tenant value cannot inject a second header or
# split the request line.
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")


def _resolve_custom_headers(
    custom_headers: CustomHeaders | None, auth_type: McpServerAuthType
) -> dict[str, str] | None:
    """Validate + unwrap tenant custom headers to a plain ``{name: value}`` map.

    Rejects (422): too many headers, malformed names, empty values, and — when
    ``auth_type='bearer'`` — an ``Authorization`` header (it would be silently
    overridden by the bearer token, so make the conflict explicit). Returns
    ``None`` when no headers were supplied.
    """
    if not custom_headers:
        return None
    if len(custom_headers) > _MAX_CUSTOM_HEADERS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MCP_SERVER_HEADERS_INVALID",
                "message": f"at most {_MAX_CUSTOM_HEADERS} custom headers allowed",
            },
        )
    resolved: dict[str, str] = {}
    for raw_name, secret in custom_headers.items():
        nm = raw_name.strip()
        if not _HEADER_NAME_RE.match(nm):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "MCP_SERVER_HEADERS_INVALID",
                    "message": "header name must match ^[A-Za-z0-9][A-Za-z0-9-]{0,127}$",
                },
            )
        if auth_type == "bearer" and nm.lower() == "authorization":
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "MCP_SERVER_HEADER_CONFLICT",
                    "message": (
                        "a custom Authorization header conflicts with bearer auth; "
                        "remove it or use auth_type='none'"
                    ),
                },
            )
        value = secret.get_secret_value()
        if not value.strip():
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "MCP_SERVER_HEADERS_INVALID",
                    "message": f"header {nm!r} has an empty value",
                },
            )
        resolved[nm] = value
    return resolved


async def _store_custom_headers(
    secret_store: SecretStore,
    *,
    tenant_id: UUID,
    name: str,
    headers: dict[str, str] | None,
) -> tuple[str | None, list[str] | None]:
    """Persist the ``{name: value}`` header map as one encrypted SecretStore blob.

    Returns ``(secret_ref, header_names)`` — both ``None`` when no headers. Only
    the ref + (non-secret) names are stored on the row; the values stay encrypted.
    """
    if not headers:
        return None, None
    sname = _headers_secret_name(tenant_id, name)
    await secret_store.put(sname, json.dumps(headers))
    return f"secret://{sname}", sorted(headers)


async def _resolve_plan(tenant_config_service: object, tenant_id: UUID) -> TenantPlan:
    """Tenant plan tier for entitlement.

    FREE (the default tier) when the config service is unwired or the tenant has
    no config row yet — mirroring the ``allow_custom_mcp_servers`` default.
    """
    if tenant_config_service is None:
        return TenantPlan.FREE
    try:
        cfg = await tenant_config_service.get(tenant_id=tenant_id)  # type: ignore[attr-defined]
    except TenantConfigNotConfiguredError:
        return TenantPlan.FREE
    return cfg.plan  # type: ignore[no-any-return]


async def _resolve_list_scope(
    request: Request,
    principal: Principal,
    tenant_id: UUID | Literal["*"] | None,
    audit: AuditLogger,
    *,
    endpoint: str,
) -> SingleTenant:
    """W3 read scope for the two mcp-server lists.

    ``TenantMcpServerStore`` has no cross-tenant aggregate reader and the "*"
    aggregate is a spec non-goal here — a resolved :class:`CrossTenant` falls
    back to the caller's home tenant (the pre-scope-threading behavior,
    mirroring the front-end ``concreteTenantScope`` collapse).
    """
    scope = await ensure_tenant_scope(
        principal,
        tenant_id,
        audit,
        trace_id=current_trace_id_hex(),
        endpoint=endpoint,
        cross_tenant_enabled=cross_tenant_query_enabled(request),
    )
    if isinstance(scope, CrossTenant):
        return SingleTenant(tenant_id=principal.tenant_id)
    return scope


async def _tenant_allowlist(tenant_config_service: object, tenant_id: UUID) -> list[str]:
    """The tenant's enabled platform-server names (``mcp_allowlist``).

    Empty when the config service is unwired or the tenant has no config row —
    the opt-in default ("租户选择使用": nothing enabled until selected, P2).
    """
    if tenant_config_service is None:
        return []
    try:
        cfg = await tenant_config_service.get(tenant_id=tenant_id)  # type: ignore[attr-defined]
    except TenantConfigNotConfiguredError:
        return []
    return list(cfg.mcp_allowlist)  # type: ignore[attr-defined]


def _public(record: object) -> dict[str, object]:
    # Serialize the record WITHOUT exposing the token_secret_ref — a ref
    # (not a secret value) but dropped from the public payload to keep the
    # API surface minimal. Health fields (last_probe_*) flow through.
    data: dict[str, object] = record.model_dump(mode="json")  # type: ignore[attr-defined]
    data.pop("token_secret_ref", None)
    # custom_headers_ref points at the encrypted blob — drop it; the (non-secret)
    # custom_header_names stay so the UI can list configured headers.
    data.pop("custom_headers_ref", None)
    return data


async def _record_health(
    store: TenantMcpServerStore,
    *,
    tenant_id: UUID,
    name: str,
    status: McpServerProbeStatus,
    error: str | None = None,
) -> TenantMcpServerRecord | None:
    """Best-effort persist of a probe result (#2). A health-write failure must
    never fail the caller's main operation, so it's swallowed (logged)."""
    try:
        return await store.record_probe_result(
            tenant_id=tenant_id,
            name=name,
            status=status,
            probed_at=datetime.now(tz=UTC),
            error=error,
        )
    except Exception:
        # Don't log the request-derived server name (CodeQL py/log-injection);
        # status is a fixed enum and the trace context carries the rest.
        logger.warning("mcp_server.health_record_failed status=%s", status)
        return None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_mcp_servers_router() -> APIRouter:
    router = APIRouter(prefix="/v1/mcp-servers", tags=["mcp-servers"])

    @router.post("", status_code=201)
    async def create_mcp_server(
        payload: CreateMcpServerRequest,
        principal: Annotated[Principal, Depends(require("mcp_server", "write"))],
        store: Annotated[TenantMcpServerStore, Depends(_get_store)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        pool_service: Annotated[object, Depends(_get_tenant_mcp_pool_service)],
        agent_runtime: Annotated[object, Depends(_get_agent_runtime)],
        tenant_config_service: Annotated[object, Depends(_get_tenant_config_service)],
        probe_limiter: Annotated[object, Depends(_get_mcp_probe_limiter)],
    ) -> dict[str, object]:
        tenant_id = principal.tenant_id
        await _enforce_probe_rate_limit(probe_limiter, tenant_id)
        # 0) Custom kill-switch (Stream W-4): a tenant in catalog-only mode may
        # not register off-catalog custom servers. Skipped when the config
        # service is unwired (preserves Stream V self-service behavior).
        if tenant_config_service is not None:
            try:
                cfg = await tenant_config_service.get(tenant_id=tenant_id)  # type: ignore[attr-defined]
                allow_custom = cfg.allow_custom_mcp_servers
            except TenantConfigNotConfiguredError:
                # No config row yet → the default (allow_custom_mcp_servers=True).
                allow_custom = True
            if not allow_custom:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "MCP_CUSTOM_DISABLED",
                        "message": (
                            "custom MCP server registration is disabled for this "
                            "tenant; use the connector catalog"
                        ),
                    },
                )
        # 1) SSRF check — fail fast with a clear error code before any I/O.
        try:
            validate_remote_url(payload.url)
        except RemoteURLError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "MCP_SERVER_INVALID_URL", "message": str(exc)},
            ) from exc
        # 2) auth_type / token consistency — reject BEFORE any I/O.
        raw_token = payload.token.get_secret_value() if payload.token is not None else None
        if payload.auth_type == "bearer" and not (raw_token and raw_token.strip()):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "MCP_SERVER_TOKEN_REQUIRED",
                    "message": "bearer auth requires a non-empty token",
                },
            )
        if payload.auth_type == "none" and payload.token is not None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "MCP_SERVER_TOKEN_NOT_ALLOWED",
                    "message": "token must not be set when auth_type='none'",
                },
            )
        # 2b) custom headers: validate + unwrap (rejects Authorization vs bearer).
        custom_headers = _resolve_custom_headers(payload.custom_headers, payload.auth_type)
        # 3) reject duplicate BEFORE probe / secret write (avoid orphan secret version).
        if await store.get(tenant_id=tenant_id, name=payload.name) is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "MCP_SERVER_DUPLICATE",
                    "message": "name already registered",
                },
            )
        # 4) connect-probe (connect + list_tools) with the raw token in memory.
        try:
            tools = await probe_remote_mcp(
                name=payload.name,
                transport=payload.transport,
                url=payload.url,
                bearer_token=raw_token,
                timeout_s=payload.timeout_s,
                custom_headers=custom_headers,
                sse_read_timeout_s=payload.sse_read_timeout_s,
            )
        except McpProbeError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
        # 5) persist token + custom headers as secret refs — only after probe success.
        # Orphan-secret note: if store.create fails after this put, the secret version is
        # orphaned — acceptable (no plaintext leak; same pattern as platform_config).
        token_secret_ref: str | None = None
        if raw_token is not None:
            sname = _token_secret_name(tenant_id, payload.name)
            await secret_store.put(sname, raw_token)
            token_secret_ref = f"secret://{sname}"
        headers_ref, header_names = await _store_custom_headers(
            secret_store, tenant_id=tenant_id, name=payload.name, headers=custom_headers
        )
        # 6) create the DB row.
        try:
            record = await store.create(
                tenant_id=tenant_id,
                name=payload.name,
                transport=payload.transport,
                url=payload.url,
                auth_type=payload.auth_type,
                token_secret_ref=token_secret_ref,
                custom_headers_ref=headers_ref,
                custom_header_names=header_names,
                sse_read_timeout_s=payload.sse_read_timeout_s,
                timeout_s=payload.timeout_s,
                created_by=principal.subject_id,
            )
        except TenantMcpServerAlreadyExistsError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "MCP_SERVER_DUPLICATE",
                    "message": "name already registered",
                },
            ) from exc
        # Probe succeeded above (step 4) → seed health as ok (#2).
        record = (
            await _record_health(store, tenant_id=tenant_id, name=record.name, status="ok")
            or record
        )
        logger.info("mcp_server.registered server=%s transport=%s", record.name, record.transport)
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.MCP_SERVER_CREATE,
            resource_type="tenant_mcp_server",
            resource_id=str(record.id),
            trace_id=current_trace_id_hex(),
            details={
                "name": record.name,
                "transport": record.transport,
                "url": record.url,
                "tool_count": len(tools),
            },  # NEVER include the token
        )
        await _invalidate_tenant_mcp(pool_service, agent_runtime, tenant_id)
        return {
            "success": True,
            "data": {**_public(record), "tool_count": len(tools)},
            "error": None,
        }

    @router.get("/catalog")
    async def list_catalog(
        principal: Annotated[Principal, Depends(require("mcp_server", "read"))],
        catalog_store: Annotated[McpConnectorCatalogStore, Depends(_get_catalog_store)],
        tenant_config_service: Annotated[object, Depends(_get_tenant_config_service)],
    ) -> dict[str, object]:
        tenant_id = principal.tenant_id
        plan = await _resolve_plan(tenant_config_service, tenant_id)
        allowlist = set(await _tenant_allowlist(tenant_config_service, tenant_id))
        # The catalog is NULL-tenant — a tenant-scoped session sees zero rows, so
        # every catalog read MUST run inside bypass_rls_session (W-8).
        async with bypass_rls_session():
            entries = await catalog_store.list()
        data = [
            {
                "id": str(entry.id),
                "name": entry.name,
                "display_name": entry.display_name,
                "description": entry.description,
                "category": entry.category,
                "icon": entry.icon,
                "transport": entry.transport,
                "auth_type": entry.auth_type,
                "auth_schema": entry.auth_schema.model_dump(mode="json"),
                "required_tier": entry.required_tier.value,
                "enabled": entry.enabled,
                "entitled": tier_satisfies(plan, entry.required_tier),
                # Opt-in tenant selection state (P2) — name in mcp_allowlist.
                "tenant_enabled": entry.name in allowlist,
            }
            for entry in entries
        ]
        return {"success": True, "data": data, "error": None}

    @router.post("/catalog/{catalog_id}/enable")
    async def enable_platform_server(
        catalog_id: UUID,
        principal: Annotated[Principal, Depends(require("mcp_server", "write"))],
        catalog_store: Annotated[McpConnectorCatalogStore, Depends(_get_catalog_store)],
        tenant_config_service: Annotated[object, Depends(_get_tenant_config_service)],
        pool_service: Annotated[object, Depends(_get_tenant_mcp_pool_service)],
        agent_runtime: Annotated[object, Depends(_get_agent_runtime)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        """Tenant opts into a platform shared server (adds it to mcp_allowlist).

        Opt-in selection (P2 "租户选择使用"): the server becomes usable to this
        tenant's agents only after this call. Tier-gated; only enabled catalog
        entries can be selected. Idempotent.
        """
        tenant_id = principal.tenant_id
        if tenant_config_service is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "TENANT_CONFIG_UNAVAILABLE", "message": "config service unwired"},
            )
        async with bypass_rls_session():
            entry = await catalog_store.get_by_id(catalog_id)
        if entry is None or not entry.enabled:
            raise HTTPException(
                status_code=404,
                detail={"code": "MCP_CATALOG_NOT_FOUND", "message": "not found"},
            )
        plan = await _resolve_plan(tenant_config_service, tenant_id)
        if not tier_satisfies(plan, entry.required_tier):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "MCP_CATALOG_TIER_REQUIRED",
                    "message": f"requires the {entry.required_tier.value} plan",
                },
            )
        try:
            cfg = await tenant_config_service.get(  # type: ignore[attr-defined]
                tenant_id=tenant_id, actor_id=principal.subject_id
            )
        except TenantConfigNotConfiguredError as exc:
            # The allowlist lives on tenant_config, whose first write needs a
            # display_name — onboard the tenant before enabling shared servers.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "TENANT_NOT_CONFIGURED",
                    "message": "configure the tenant before enabling MCP servers",
                },
            ) from exc
        allowlist = list(cfg.mcp_allowlist)
        if entry.name not in allowlist:
            await tenant_config_service.upsert(  # type: ignore[attr-defined]
                tenant_id=tenant_id,
                patch=TenantConfigPatch(mcp_allowlist=[*allowlist, entry.name]),
                actor_id=principal.subject_id,
            )
            await _invalidate_tenant_mcp(pool_service, agent_runtime, tenant_id)
            await emit(
                audit,
                tenant_id=tenant_id,
                actor_id=principal.subject_id,
                action=AuditAction.MCP_CATALOG_ENABLE,
                resource_type="mcp_connector_catalog",
                resource_id=entry.name,
                trace_id=current_trace_id_hex(),
                details={"name": entry.name},
            )
        return {
            "success": True,
            "data": {"name": entry.name, "tenant_enabled": True},
            "error": None,
        }

    @router.delete("/catalog/{catalog_id}/enable", status_code=200)
    async def disable_platform_server(
        catalog_id: UUID,
        principal: Annotated[Principal, Depends(require("mcp_server", "write"))],
        catalog_store: Annotated[McpConnectorCatalogStore, Depends(_get_catalog_store)],
        tenant_config_service: Annotated[object, Depends(_get_tenant_config_service)],
        pool_service: Annotated[object, Depends(_get_tenant_mcp_pool_service)],
        agent_runtime: Annotated[object, Depends(_get_agent_runtime)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> dict[str, object]:
        """Tenant opts out of a platform shared server (removes it from
        mcp_allowlist). Idempotent — a name already absent is a no-op."""
        tenant_id = principal.tenant_id
        if tenant_config_service is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "TENANT_CONFIG_UNAVAILABLE", "message": "config service unwired"},
            )
        async with bypass_rls_session():
            entry = await catalog_store.get_by_id(catalog_id)
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "MCP_CATALOG_NOT_FOUND", "message": "not found"},
            )
        allowlist = await _tenant_allowlist(tenant_config_service, tenant_id)
        if entry.name in allowlist:
            await tenant_config_service.upsert(  # type: ignore[attr-defined]
                tenant_id=tenant_id,
                patch=TenantConfigPatch(mcp_allowlist=[n for n in allowlist if n != entry.name]),
                actor_id=principal.subject_id,
            )
            await _invalidate_tenant_mcp(pool_service, agent_runtime, tenant_id)
            await emit(
                audit,
                tenant_id=tenant_id,
                actor_id=principal.subject_id,
                action=AuditAction.MCP_CATALOG_DISABLE,
                resource_type="mcp_connector_catalog",
                resource_id=entry.name,
                trace_id=current_trace_id_hex(),
                details={"name": entry.name},
            )
        return {
            "success": True,
            "data": {"name": entry.name, "tenant_enabled": False},
            "error": None,
        }

    @router.get("")
    async def list_mcp_servers(
        request: Request,
        principal: Annotated[Principal, Depends(require("mcp_server", "read"))],
        store: Annotated[TenantMcpServerStore, Depends(_get_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — a concrete id lets a system_admin read a foreign
        # tenant's MCP servers from the tenant switcher.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> dict[str, object]:
        scope = await _resolve_list_scope(
            request, principal, tenant_id, audit, endpoint="GET /v1/mcp-servers"
        )
        async with applied_scope(scope):
            rows = await store.list_for_tenant(tenant_id=scope.tenant_id)
        return {"success": True, "data": [_public(r) for r in rows], "error": None}

    @router.post("/test")
    async def test_mcp_connection(
        payload: TestConnectionRequest,
        principal: Annotated[Principal, Depends(require("mcp_server", "write"))],
        probe_limiter: Annotated[object, Depends(_get_mcp_probe_limiter)],
    ) -> dict[str, object]:
        await _enforce_probe_rate_limit(probe_limiter, principal.tenant_id)
        if payload.auth_type == "bearer" and (
            payload.token is None or not payload.token.get_secret_value().strip()
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "MCP_SERVER_TOKEN_REQUIRED",
                    "message": "bearer auth requires a non-empty token",
                },
            )
        raw = payload.token.get_secret_value() if payload.token is not None else None
        custom_headers = _resolve_custom_headers(payload.custom_headers, payload.auth_type)
        try:
            tools = await probe_remote_mcp(
                name="test",
                transport=payload.transport,
                url=payload.url,
                bearer_token=raw,
                timeout_s=payload.timeout_s,
                custom_headers=custom_headers,
                sse_read_timeout_s=payload.sse_read_timeout_s,
            )
        except McpProbeError as exc:
            raise HTTPException(
                status_code=422, detail={"code": exc.code, "message": exc.message}
            ) from exc
        return {"success": True, "data": {"tool_count": len(tools)}, "error": None}

    @router.get("/available")
    async def list_available_mcp_servers(
        request: Request,
        principal: Annotated[Principal, Depends(require("mcp_server", "read"))],
        store: Annotated[TenantMcpServerStore, Depends(_get_store)],
        catalog_store: Annotated[McpConnectorCatalogStore, Depends(_get_catalog_store)],
        tenant_config_service: Annotated[object, Depends(_get_tenant_config_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — same treatment as the base list.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> dict[str, object]:
        scope = await _resolve_list_scope(
            request, principal, tenant_id, audit, endpoint="GET /v1/mcp-servers/available"
        )
        target_tenant = scope.tenant_id
        available: list[dict[str, object]] = []
        allowlist: list[str] = []
        async with applied_scope(scope):
            if tenant_config_service is not None:
                try:
                    cfg = await tenant_config_service.get(tenant_id=target_tenant)  # type: ignore[attr-defined]
                    allowlist = list(cfg.mcp_allowlist)
                except Exception:
                    logger.info("mcp_servers.available.no_tenant_config")
            tenant_rows = await store.list_for_tenant(tenant_id=target_tenant)

        # Fetch the platform catalog once if either the allowlist (platform rows,
        # enriched below) or any catalog-bound tenant row needs it. Catalog is
        # NULL-tenant → bypass RLS (W-8).
        need_catalog = bool(allowlist) or any(
            getattr(r, "catalog_id", None) is not None for r in tenant_rows
        )
        by_name: dict[str, object] = {}
        catalog_names: dict[UUID, str] = {}
        if need_catalog:
            async with bypass_rls_session():
                entries = await catalog_store.list()
            by_name = {e.name: e for e in entries}
            catalog_names = {e.id: e.name for e in entries}

        for name in allowlist:
            entry = by_name.get(name)
            if entry is None:  # stale allowlist → catalog entry deleted; degrade
                available.append({"name": name, "source": "platform"})
                continue
            available.append(
                {
                    "name": name,
                    "source": "platform",
                    "display_name": entry.display_name,  # type: ignore[attr-defined]
                    "auth_type": entry.auth_type,  # type: ignore[attr-defined]
                    "catalog_id": str(entry.id),  # type: ignore[attr-defined]
                }
            )

        for rec in tenant_rows:
            row: dict[str, object] = {
                "name": rec.name,
                "source": "tenant",
                "enabled": rec.enabled,
            }
            catalog_id = getattr(rec, "catalog_id", None)
            if catalog_id is not None:
                row["catalog_id"] = str(catalog_id)
                row["catalog_name"] = catalog_names.get(catalog_id)
            available.append(row)
        return {"success": True, "data": available, "error": None}

    @router.get("/{name}/tools")
    async def list_mcp_server_tools(
        name: Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")],
        request: Request,
        principal: Annotated[Principal, Depends(require("mcp_server", "read"))],
        store: Annotated[TenantMcpServerStore, Depends(_get_store)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        probe_limiter: Annotated[object, Depends(_get_mcp_probe_limiter)],
        catalog_store: Annotated[McpConnectorCatalogStore, Depends(_get_catalog_store)],
        tenant_config_service: Annotated[object, Depends(_get_tenant_config_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W3 read scope — a concrete id lets a system_admin probe a foreign
        # tenant's server tools; "*" is meaningless (one owning tenant).
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> dict[str, object]:
        scope = await ensure_single_tenant_scope(
            principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/mcp-servers/{name}/tools",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        # Rate limit stays keyed on the CALLER's tenant (caller-identity).
        await _enforce_probe_rate_limit(probe_limiter, principal.tenant_id)
        target_tenant = scope.tenant_id

        # The picker lists BOTH tenant-private servers and platform servers the
        # tenant enabled (``/available`` → ``mcp_allowlist``), so tool-probe must
        # resolve both. Tenant rows first; otherwise fall back to a platform
        # catalog server the tenant has enabled.
        async with applied_scope(scope):
            record = await store.get(tenant_id=target_tenant, name=name)
        if record is not None:
            transport, url, timeout_s = record.transport, record.url, record.timeout_s
            raw: str | None = None
            if record.auth_type == "bearer" and record.token_secret_ref is not None:
                raw = await secret_store.get(parse_secret_ref(record.token_secret_ref))
            is_tenant = True
        else:
            async with applied_scope(scope):
                allowlist = await _tenant_allowlist(tenant_config_service, target_tenant)
            entry = None
            if name in allowlist:
                # Catalog rows are platform-global (NULL tenant) → bypass RLS (W-8).
                async with bypass_rls_session():
                    entry = await catalog_store.get_by_name(name)
            if entry is None or not entry.enabled:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "MCP_SERVER_NOT_FOUND", "message": "not found"},
                )
            if entry.auth_type == "oauth2":
                # Per-user OAuth servers expose tools only over a user's own
                # connection — a shared platform probe cannot enumerate them.
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "MCP_SERVER_OAUTH_PROBE_UNSUPPORTED",
                        "message": "connect this OAuth server to list its tools",
                    },
                )
            transport = entry.transport
            url = entry.url_template
            timeout_s = entry.timeout_s if entry.timeout_s is not None else _DEFAULT_TIMEOUT_S
            raw = None
            if entry.auth_type == "bearer" and entry.bearer_token_ref is not None:
                raw = await secret_store.get(parse_secret_ref(entry.bearer_token_ref))
            is_tenant = False

        try:
            tools = await probe_remote_mcp(
                name=name,
                transport=transport,
                url=url,
                bearer_token=raw,
                timeout_s=timeout_s,
            )
        except McpProbeError as exc:
            # On-demand probe doubles as the live-health signal — persist the
            # failure (#2). Only tenant rows carry per-server health. The
            # health stamp belongs to the PROBED row's tenant (scope target).
            if is_tenant:
                async with applied_scope(scope):
                    await _record_health(
                        store, tenant_id=target_tenant, name=name, status="error", error=exc.code
                    )
            raise HTTPException(
                status_code=502, detail={"code": exc.code, "message": exc.message}
            ) from exc
        if is_tenant:
            async with applied_scope(scope):
                await _record_health(store, tenant_id=target_tenant, name=name, status="ok")
        return {
            "success": True,
            "data": [{"name": t.name, "description": t.description or ""} for t in tools],
            "error": None,
        }

    @router.patch("/{name}")
    async def update_mcp_server(
        name: Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")],
        payload: UpdateMcpServerRequest,
        principal: Annotated[Principal, Depends(require("mcp_server", "write"))],
        store: Annotated[TenantMcpServerStore, Depends(_get_store)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        pool_service: Annotated[object, Depends(_get_tenant_mcp_pool_service)],
        agent_runtime: Annotated[object, Depends(_get_agent_runtime)],
    ) -> dict[str, object]:
        tenant_id = principal.tenant_id
        existing = await store.get(tenant_id=tenant_id, name=name)
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "MCP_SERVER_NOT_FOUND", "message": "not found"},
            )
        new_url = payload.url if payload.url is not None else existing.url
        if payload.url is not None:
            try:
                validate_remote_url(new_url)
            except RemoteURLError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "MCP_SERVER_INVALID_URL", "message": str(exc)},
                ) from exc
        # Reject an explicitly-empty token rotation before any I/O.
        if payload.token is not None and not payload.token.get_secret_value().strip():
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "MCP_SERVER_TOKEN_REQUIRED",
                    "message": "token must be non-empty",
                },
            )
        # Custom headers (M1): a non-empty map replaces the header set. Clearing
        # is via delete+recreate (parity with auth-type) — an empty/absent map is
        # left unchanged. Validation rejects Authorization vs the existing auth.
        new_custom_headers = _resolve_custom_headers(payload.custom_headers, existing.auth_type)
        headers_changed = new_custom_headers is not None
        # Re-probe when connectivity-affecting fields change (url, token, headers).
        # next_token_secret_ref: will be set to the new ref only when rotating; else None
        # (TenantMcpServerPatch treats None as "leave unchanged").
        next_token_secret_ref: str | None = None
        next_headers_ref: str | None = None
        next_header_names: list[str] | None = None
        reprobed = payload.url is not None or payload.token is not None or headers_changed
        if reprobed:
            raw_token: str | None
            if payload.token is not None:
                raw_token = payload.token.get_secret_value()
            elif existing.token_secret_ref is not None:
                raw_token = await secret_store.get(parse_secret_ref(existing.token_secret_ref))
            else:
                raw_token = None
            # Effective custom headers for the probe: the new set if replacing,
            # else the existing encrypted blob (so a url/token re-probe still
            # carries the configured headers).
            probe_headers: dict[str, str] | None
            if headers_changed:
                probe_headers = new_custom_headers
            elif existing.custom_headers_ref is not None:
                blob = await secret_store.get(parse_secret_ref(existing.custom_headers_ref))
                loaded = json.loads(blob)
                probe_headers = loaded if isinstance(loaded, dict) else None
            else:
                probe_headers = None
            try:
                await probe_remote_mcp(
                    name=name,
                    transport=existing.transport,
                    url=new_url,
                    bearer_token=raw_token,
                    timeout_s=(
                        payload.timeout_s if payload.timeout_s is not None else existing.timeout_s
                    ),
                    custom_headers=probe_headers,
                    sse_read_timeout_s=(
                        payload.sse_read_timeout_s
                        if payload.sse_read_timeout_s is not None
                        else existing.sse_read_timeout_s
                    ),
                )
            except McpProbeError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"code": exc.code, "message": exc.message},
                ) from exc
            if payload.token is not None and raw_token is not None:
                sname = _token_secret_name(tenant_id, name)
                await secret_store.put(sname, raw_token)  # already resolved above
                next_token_secret_ref = f"secret://{sname}"
            if headers_changed:
                next_headers_ref, next_header_names = await _store_custom_headers(
                    secret_store, tenant_id=tenant_id, name=name, headers=new_custom_headers
                )
        patch = TenantMcpServerPatch(
            url=payload.url,
            token_secret_ref=(next_token_secret_ref if payload.token is not None else None),
            custom_headers_ref=next_headers_ref,
            custom_header_names=next_header_names,
            sse_read_timeout_s=payload.sse_read_timeout_s,
            timeout_s=payload.timeout_s,
            enabled=payload.enabled,
        )
        try:
            record = await store.update(tenant_id=tenant_id, name=name, patch=patch)
        except TenantMcpServerNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "MCP_SERVER_NOT_FOUND", "message": "not found"},
            ) from exc
        if reprobed:  # re-probe above succeeded → refresh health to ok (#2)
            record = (
                await _record_health(store, tenant_id=tenant_id, name=name, status="ok") or record
            )
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.MCP_SERVER_UPDATE,
            resource_type="tenant_mcp_server",
            resource_id=str(record.id),
            trace_id=current_trace_id_hex(),
            details={"name": record.name, "url": record.url, "enabled": record.enabled},
        )
        await _invalidate_tenant_mcp(pool_service, agent_runtime, tenant_id)
        return {"success": True, "data": _public(record), "error": None}

    @router.delete("/{name}", status_code=200)
    async def delete_mcp_server(
        name: Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")],
        principal: Annotated[Principal, Depends(require("mcp_server", "delete"))],
        store: Annotated[TenantMcpServerStore, Depends(_get_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        agent_spec_store: Annotated[object, Depends(_get_agent_spec_store)],
        pool_service: Annotated[object, Depends(_get_tenant_mcp_pool_service)],
        agent_runtime: Annotated[object, Depends(_get_agent_runtime)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
    ) -> dict[str, object]:
        tenant_id = principal.tenant_id
        # (a) Resolve row first so we have the UUID for the audit record and a clean 404.
        record = await store.get(tenant_id=tenant_id, name=name)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "MCP_SERVER_NOT_FOUND", "message": "not found"},
            )
        # (b) Reference check: refuse if any live (non-soft-deleted) agent manifest
        # explicitly references this server. DELETED specs are tombstones —
        # counting them produced a false 409 that locked deletion forever;
        # DEPRECATED still counts as active reference surface.
        # agent_spec_repo may be None in minimal deployments (no spec store wired).
        implicit_all = 0
        if agent_spec_store is not None:
            # Sweep every page: one fixed page hid references past its boundary
            # and let the delete through on tenants with more specs than that.
            specs: list[AgentSpecRecord] = []
            offset = 0
            while True:
                page = await agent_spec_store.list_by_tenant(  # type: ignore[attr-defined]
                    tenant_id=tenant_id, limit=_SPEC_PAGE_SIZE, offset=offset
                )
                # Terminate on an empty page, not on a short one, and advance
                # by what the store actually returned. A server-side limit
                # clamp (RunStore already has one) would make every page
                # "short" — a short-page break would then stop after one page
                # and silently reinstate the very gap this sweep closes. Costs
                # one extra empty query per sweep; deletes are rare.
                if not page:
                    break
                specs.extend(page)
                offset += len(page)
            active_specs = [s for s in specs if s.status is not AgentSpecStatus.DELETED]
            # AgentSpecRecord.spec is an AgentSpec object — convert to dict (once
            # per spec) for the helpers below, which read raw manifest dicts.
            dumped = [(s.name, s.spec.model_dump(mode="json")) for s in active_specs]
            referencing = [
                spec_name
                for spec_name, manifest in dumped
                if manifest_references_server(manifest, name)
            ]
            if referencing:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "MCP_SERVER_IN_USE",
                        "message": (
                            f"referenced by agent(s): {', '.join(sorted(set(referencing)))}"
                        ),
                    },
                )
            # D3: ``servers`` empty/absent = "every available server" wildcard.
            # Those agents follow the live server set dynamically, so they are
            # not a hard reference — the delete proceeds, but the impact count
            # is surfaced in the response and the audit record.
            implicit_all = sum(1 for _, manifest in dumped if manifest_uses_implicit_all(manifest))
        # (c) Delete the row.
        await store.delete(tenant_id=tenant_id, name=name)
        # (c2) Best-effort secret cleanup. This domain is paste-only (no ref-mode
        # secrets — CreateMcpServerRequest.token is a SecretStr, never a caller-
        # supplied secret:// ref), so any ref present is fully platform-managed
        # and safe to delete outright.
        for ref in (record.token_secret_ref, record.custom_headers_ref):
            if ref:
                try:
                    await secret_store.delete(parse_secret_ref(ref))
                except Exception:
                    # ``name`` is request-derived (CodeQL py/log-injection);
                    # the row UUID identifies the server just as well.
                    logger.warning(
                        "mcp_servers: failed to delete secret for server %s",
                        record.id,
                        exc_info=True,
                    )
        # (d) Audit with the row UUID (consistent with POST/PATCH), name in details.
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.MCP_SERVER_DELETE,
            resource_type="tenant_mcp_server",
            resource_id=str(record.id),
            trace_id=current_trace_id_hex(),
            details={"name": name, "implicit_all_agents": implicit_all},
        )
        await _invalidate_tenant_mcp(pool_service, agent_runtime, tenant_id)
        return {
            "success": True,
            "data": {"implicit_all_agents": implicit_all},
            "error": None,
        }

    return router
