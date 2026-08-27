"""``/v1/tenants/{tenant_id}/config`` admin endpoints — Stream C.7.

GET (operator + admin) returns the full :class:`TenantConfigRecord`;
PUT (admin only) accepts a :class:`TenantConfigPatch` (partial
update). Both go through :class:`TenantConfigService` so the 60s LRU
cache stays warm and the audit log captures the access.

Stream Y-1 — LLM credentials are platform-exclusive. ``credentials_mode``
can only be ``platform``, so the former all-or-nothing tenant-mode switch
gate (Stream O Mini-ADR O-4) and its dry-run preview endpoint were removed;
Pydantic now rejects any non-``platform`` value in the request body with 422.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request

from control_plane.api._authz import console_only, require
from control_plane.invalidation_bus import InvalidationEvent
from control_plane.ratelimit import parse_rate_limit_override
from control_plane.tenancy import TenantConfigNotConfiguredError, TenantConfigService
from control_plane.tenant_scope import (
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.protocol import (
    AgentSpecRecord,
    Principal,
    Provider,
    TenantConfigPatch,
    Tool,
)
from expert_work.runtime.audit.logger import AuditLogger

logger = logging.getLogger("expert_work.control_plane.api.tenant_config")


def _get_service(request: Request) -> TenantConfigService:
    return request.app.state.tenant_config_service  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_agent_spec_store(request: Request) -> AgentSpecStore | None:
    """Stream O — AgentSpecStore is optional (some test apps don't wire it).
    When absent, the credentials view simply omits the used-by-agent counts
    (there are no specs to walk); resolution itself is platform-only (Y-1).

    Wired as ``app.state.agent_spec_repo`` in create_app (Stream B.5).
    """
    return getattr(request.app.state, "agent_spec_repo", None)


def _embedding_provider(request: Request) -> Provider:
    """Stream O Mini-ADR O-12 — the platform embedding provider whose
    credential long-term memory needs. Read from settings on app.state;
    test apps that don't wire settings fall back to the ``qwen`` default
    (matching :class:`Settings.embedding_provider`)."""
    settings = getattr(request.app.state, "settings", None)
    provider: Provider = getattr(settings, "embedding_provider", "qwen")
    return provider


async def _credentials_catalog(
    request: Request,
) -> tuple[list[Provider], dict[Provider, str], list[Tool], dict[Tool, str]]:
    """Stream O Mini-ADR O-13 — the effective platform catalog the Credentials
    panel renders rows from.

    Stream P (Mini-ADR P-9): read the **merged** view (env seed + runtime DB
    overlay) from :class:`PlatformSecretsService` so the panel reflects
    providers a platform admin configured at runtime, not just env. Falls back
    to env-only settings when the service isn't wired (lightweight test apps)."""
    service = getattr(request.app.state, "platform_secrets_service", None)
    if service is not None:
        provs = await service.effective_provider_credentials()
        tools = await service.effective_tool_credentials()
        return list(provs.keys()), provs, list(tools.keys()), tools
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        return [], {}, [], {}
    return (
        settings.effective_supported_providers,
        settings.effective_platform_provider_credentials,
        settings.effective_supported_tools,
        settings.effective_platform_tool_credentials,
    )


def _providers_referenced_by(
    record: AgentSpecRecord, *, embedding_provider: Provider
) -> set[Provider]:
    """Providers a single agent transitively references — primary model +
    its fallback chain, vision model + fallbacks, and
    ``memory_consolidation.aux_model``.

    Mini-ADR O-12 — long-term memory uses the platform ``embedding_provider``
    (a platform-infra provider, not declared in any manifest model field), so
    an agent declaring ``memory.long_term`` references it too. Rerank is NOT
    included — a missing rerank credential degrades gracefully (Mini-ADR O-9)."""
    agent_spec = record.spec
    used: set[Provider] = set()
    stack = [agent_spec.spec.model]
    if agent_spec.spec.vision is not None:
        stack.append(agent_spec.spec.vision.model)
        stack.extend(agent_spec.spec.vision.fallbacks)
    consolidation = agent_spec.spec.policies.memory_consolidation
    if consolidation.aux_model is not None:
        stack.append(consolidation.aux_model)
    while stack:
        current = stack.pop()
        used.add(current.provider)
        stack.extend(current.fallback)
    memory = agent_spec.spec.memory
    if memory is not None and memory.long_term is not None:
        used.add(embedding_provider)
    return used


def _tools_referenced_by(record: AgentSpecRecord) -> set[Tool]:
    """External SaaS tools a single agent references. ``web_search`` is the
    only :data:`TOOL_CATALOG` entry today; other tool names (filesystem /
    exec_python / MCP) don't consume Stream O credentials."""
    used: set[Tool] = set()
    for entry in record.spec.spec.tools:
        tool_name = getattr(entry, "name", None) or getattr(entry, "tool", None)
        if tool_name == "web_search":
            used.add("web_search")
    return used


def _provider_usage_counts(
    specs: Iterable[AgentSpecRecord], *, embedding_provider: Provider
) -> dict[Provider, int]:
    """Stream O Mini-ADR O-13 — per-provider count of agents referencing it
    (drives the Credentials panel's ``used_by_agents`` column)."""
    counts: dict[Provider, int] = {}
    for record in specs:
        for provider in _providers_referenced_by(record, embedding_provider=embedding_provider):
            counts[provider] = counts.get(provider, 0) + 1
    return counts


def _tool_usage_counts(specs: Iterable[AgentSpecRecord]) -> dict[Tool, int]:
    """Stream O Mini-ADR O-13 — per-tool count of agents referencing it."""
    counts: dict[Tool, int] = {}
    for record in specs:
        for tool in _tools_referenced_by(record):
            counts[tool] = counts.get(tool, 0) + 1
    return counts


def build_tenant_config_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/tenants", tags=["tenant_config"], dependencies=[Depends(console_only())]
    )

    @router.get("/{tenant_id}/config")
    async def get_tenant_config(
        tenant_id: UUID,
        request: Request,
        principal: Annotated[Principal, Depends(require("tenant_config", "read"))],
        svc: Annotated[TenantConfigService, Depends(_get_service)],
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
            endpoint="GET /v1/tenants/{tenant_id}/config",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        try:
            async with applied_scope(scope):
                record = await svc.get(tenant_id=scope.tenant_id, actor_id=principal.subject_id)
        except TenantConfigNotConfiguredError as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "TENANT_CONFIG_NOT_FOUND",
                    "message": "no tenant_config row exists for this tenant",
                },
            ) from exc
        return {"success": True, "data": record.model_dump(mode="json"), "error": None}

    @router.put("/{tenant_id}/config")
    async def upsert_tenant_config(
        tenant_id: UUID,
        payload: TenantConfigPatch,
        principal: Annotated[Principal, Depends(require("tenant_config", "write"))],
        svc: Annotated[TenantConfigService, Depends(_get_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        request: Request,
    ) -> dict[str, object]:
        scope = await ensure_single_tenant_scope(
            principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="PUT /v1/tenants/{tenant_id}/config",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        # Stream C.6 — validate the rate_limit_override shape at write time so a
        # bad config (typo / wrong unit) is rejected here, never silently
        # ignored by the limiter at runtime.
        if payload.rate_limit_override is not None:
            try:
                parse_rate_limit_override(payload.rate_limit_override)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "TENANT_CONFIG_INVALID_RATE_LIMIT_OVERRIDE",
                        "message": str(exc),
                    },
                ) from exc
        # Stream Y-1 — LLM credentials are platform-exclusive. ``credentials_mode``
        # is a ``Literal["platform"]``, so Pydantic rejects any other value in the
        # request body with 422 before reaching this handler; no switch gate needed.
        try:
            async with applied_scope(scope):
                record = await svc.upsert(
                    tenant_id=scope.tenant_id, patch=payload, actor_id=principal.subject_id
                )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "TENANT_CONFIG_FIRST_UPSERT_REQUIRES_DISPLAY_NAME",
                    "message": str(exc),
                },
            ) from exc
        # PR-E3a — tenant_config fields (mcp_allowlist above all) are
        # BUILD-TIME inputs baked into BuiltAgent: this write must drop the
        # tenant's built agents locally (the pre-existing gap) AND broadcast
        # a tenant_config event so peer replicas drop their 60s config cache
        # + builds together (two-layer invariant, encoded in the bus handler).
        runtime = getattr(request.app.state, "agent_runtime", None)
        if runtime is not None:
            runtime.invalidate_tenant(scope.tenant_id)
        # PR-E3b — a ``model_credentials_ref`` swap must also drop the tenant's
        # cached plaintext secret values (300s CredentialValueCache), or embed/
        # rerank/judge keep using the OLD key on this pod until the TTL.
        cache = getattr(request.app.state, "credential_value_cache", None)
        if cache is not None:
            cache.invalidate_tenant(scope.tenant_id)
        bus = getattr(request.app.state, "invalidation_bus", None)
        if bus is not None:
            await bus.publish(
                InvalidationEvent(kind="tenant_config", tenant_id=str(scope.tenant_id))
            )
        # PR-D (E3b) — rate_limit_override is read through the middleware's own
        # 30s TTL cache (NOT the tenant-config cache the event above drops), so
        # it needs its own eviction: local + broadcast, one more event on top of
        # ``tenant_config`` (the established "one write touches several inner
        # layers → several events" posture, cf. mcp_servers enable/disable).
        # Trigger = the field APPEARS in this request's body, read off
        # ``model_fields_set``. That covers ``{}`` (the actual clear-override
        # value) and explicit null (which the store ignores — evicting anyway
        # is a harmless DB re-read); deliberately NOT an old-vs-new diff: the
        # write is authoritative either way and a spurious evict is cheap.
        if "rate_limit_override" in payload.model_fields_set:
            overrides = getattr(request.app.state, "tenant_rate_limit_overrides", None)
            if overrides is not None:
                overrides.invalidate(scope.tenant_id)
            if bus is not None:
                await bus.publish(
                    InvalidationEvent(kind="rate_limit_override", tenant_id=str(scope.tenant_id))
                )
        return {"success": True, "data": record.model_dump(mode="json"), "error": None}

    @router.get("/{tenant_id}/config/credentials")
    async def get_credentials_view(
        tenant_id: UUID,
        principal: Annotated[Principal, Depends(require("tenant_config", "read"))],
        svc: Annotated[TenantConfigService, Depends(_get_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        request: Request,
    ) -> dict[str, object]:
        """Stream O Mini-ADR O-13 — composite view that drives the Admin UI
        Credentials panel: per catalog provider/tool, the platform-configured
        flag, the tenant secret_ref, the used-by-agents count, plus the current
        mode. No secret VALUES are returned — only refs (kms:// URIs) and
        booleans."""
        scope = await ensure_single_tenant_scope(
            principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/tenants/{tenant_id}/config/credentials",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        supported_provs, plat_provs, supported_tools, plat_tools = await _credentials_catalog(
            request
        )
        async with applied_scope(scope):
            try:
                record = await svc.get(tenant_id=scope.tenant_id, actor_id=principal.subject_id)
                mode: str = record.credentials_mode
                tenant_provs = record.model_credentials_ref
                tenant_tools = record.tool_credentials
            except TenantConfigNotConfiguredError:
                mode, tenant_provs, tenant_tools = "platform", {}, {}
            embedding_provider = _embedding_provider(request)
            prov_counts: dict[Provider, int] = {}
            tool_counts: dict[Tool, int] = {}
            agent_store = _get_agent_spec_store(request)
            if agent_store is not None:
                specs = await agent_store.list_by_tenant(
                    tenant_id=scope.tenant_id, status=None, limit=1000
                )
                prov_counts = _provider_usage_counts(specs, embedding_provider=embedding_provider)
                tool_counts = _tool_usage_counts(specs)
        providers = [
            {
                "provider": provider,
                "platform_configured": provider in plat_provs,
                "tenant_secret_ref": tenant_provs.get(provider),
                "used_by_agents": prov_counts.get(provider, 0),
            }
            for provider in supported_provs
        ]
        tools = [
            {
                "tool": tool,
                "platform_configured": tool in plat_tools,
                "tenant_secret_ref": tenant_tools.get(tool),
                "used_by_agents": tool_counts.get(tool, 0),
            }
            for tool in supported_tools
        ]
        data = {"mode": mode, "providers": providers, "tools": tools}
        return {"success": True, "data": data, "error": None}

    return router
