"""``/v1/platform/credentials`` — platform credential runtime management (Stream P).

system_admin-only CRUD over the platform provider/tool secret-ref overlay
(Mini-ADR P-11). Every handler:

* gates on ``principal.is_system_admin`` inline (platform-level; no RBAC
  ``tenant`` resource — same precedent as ``role_bindings.py`` / ``tenants.py``);
* runs the store reads/writes inside ``bypass_rls_session()`` because the rows
  are tenant-less;
* stores **refs only** in the catalog — never plaintext. A pasted raw key
  (``PlatformSecretWrite.value``, Stream Q) is encrypted into the SecretStore
  and only its generated ``secret://`` ref reaches the catalog (Mini-ADR
  Q-4/Q-6); an operator-supplied ``secret_ref`` is ref-validated (Mini-ADR P-8).

Naming: the storage layer is ``platform_secrets`` (harness blocks ``credentials``
paths); the HTTP surface keeps the design's ``/v1/platform/credentials`` path.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator

from control_plane.api._authz import _principal, platform_only
from control_plane.api.tenant_config import (
    _embedding_provider,
    _get_agent_spec_store,
    _provider_usage_counts,
    _tool_usage_counts,
)
from control_plane.audit import emit
from control_plane.platform_secrets import PlatformSecretsService
from control_plane.tenancy import TenantConfigNotConfiguredError
from control_plane.tenant_scope import bypass_rls_session
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence import PlatformSecretStore
from expert_work.protocol import (
    PROVIDER_CATALOG,
    TOOL_CATALOG,
    AuditAction,
    PlatformProviderSecretRecord,
    Principal,
    Provider,
    Tool,
    validate_secret_ref,
)
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.secret_store import SecretStore

logger = logging.getLogger("expert_work.control_plane.api.platform_config")


#: 403 body for a non-system-admin caller. Per-router on purpose: the message
#: names the resource being protected, which is what makes the refusal actionable.
_PLATFORM_SCOPE_MESSAGE = "only a system admin may manage platform credentials"


class PlatformSecretWrite(BaseModel):
    """Write payload for a platform provider/tool credential — Stream Q (Q-4).

    Accepts **exactly one** of:

    * ``secret_ref`` — a ``secret://`` / ``kms://`` reference the operator
      manages out of band (the original Stream P flow); or
    * ``value`` — a raw key pasted in the web UI. The write path encrypts it
      via the SecretStore and stores only the generated ref in the catalog, so
      the catalog never holds a value (Mini-ADR Q-4/Q-6).

    ``value`` is a :class:`~pydantic.SecretStr` so it never renders in logs.
    """

    model_config = ConfigDict(extra="forbid")
    secret_ref: str | None = None
    value: SecretStr | None = None
    enabled: bool = True
    #: Stream Y-MK — failover order within a provider (lower tried first).
    #: Ignored for tool writes.
    priority: int = 100

    @field_validator("secret_ref")
    @classmethod
    def _check_ref(cls, value: str | None) -> str | None:
        return validate_secret_ref(value) if value is not None else None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> PlatformSecretWrite:
        if (self.secret_ref is None) == (self.value is None):
            msg = "provide exactly one of 'secret_ref' or 'value'"
            raise ValueError(msg)
        return self


def _get_store(request: Request) -> PlatformSecretStore:
    return request.app.state.platform_secret_store  # type: ignore[no-any-return]


def _get_secret_store(request: Request) -> SecretStore:
    return request.app.state.secret_store  # type: ignore[no-any-return]


def _get_service(request: Request) -> PlatformSecretsService:
    return request.app.state.platform_secrets_service  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


async def _resolve_write_ref(
    payload: PlatformSecretWrite, secret_store: SecretStore, *, name: str
) -> str:
    """Return the ref to store in the catalog.

    A pasted raw ``value`` is encrypted into the SecretStore under ``name`` and
    surfaced as ``secret://<name>``; the value never reaches the catalog or the
    audit row (Mini-ADR Q-4/Q-7). An operator-supplied ``secret_ref`` is
    returned as-is.
    """
    if payload.value is not None:
        await secret_store.put(name, payload.value.get_secret_value())
        return f"secret://{name}"
    if payload.secret_ref is None:
        # Unreachable: the model validator enforces exactly one of value /
        # secret_ref. Guard explicitly (not ``assert`` — stripped under -O).
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_SECRET_WRITE",
                "message": "provide exactly one of 'secret_ref' or 'value'",
            },
        )
    return payload.secret_ref


def _invalidate_built_agents(request: Request, *, tenant_id: UUID | None = None) -> None:
    """凭据轮换后清 built-agent 缓存 —— 轮换是同 ref 原地覆写值
    (``_canonical_secret_name`` 对同 (provider,key_id) 恒同 slot),ref 比对
    发现不了,已构建 agent 里烤住的旧明文 key 只能靠显式失效清掉。
    同时清 secret 值缓存(T3 落地后 ``app.state`` 上才有,getattr 兜底)。"""
    runtime = getattr(request.app.state, "agent_runtime", None)
    cache = getattr(request.app.state, "credential_value_cache", None)
    if tenant_id is None:
        if runtime is not None:
            runtime.invalidate_all()
        if cache is not None:
            cache.invalidate_all()
    else:
        if runtime is not None:
            runtime.invalidate_tenant(tenant_id)
        if cache is not None:
            cache.invalidate_tenant(tenant_id)


async def _require_tenant(request: Request, tenant_id: UUID) -> None:
    """404 unless the tenant exists (Stream HX-8 tenant override endpoints)."""
    service = getattr(request.app.state, "tenant_config_service", None)
    if service is None:
        return
    try:
        await service.get(tenant_id=tenant_id)
    except TenantConfigNotConfiguredError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "TENANT_NOT_FOUND", "message": f"tenant {tenant_id} not found"},
        ) from exc


def _env_provider_refs(request: Request) -> dict[Provider, str]:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        return {}
    return dict(settings.effective_platform_provider_credentials)


def _env_tool_refs(request: Request) -> dict[Tool, str]:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        return {}
    return dict(settings.effective_platform_tool_credentials)


def _canonical_secret_name(
    *,
    provider: str | None = None,
    key_id: str | None = None,
    tool: str | None = None,
    tenant_id: UUID | None = None,
) -> str:
    """Rebuild the canonical platform-managed secret slot name.

    Shared by the write path (:func:`_do_upsert_provider` / ``upsert_tool`` /
    ``upsert_tenant_provider`` / ``upsert_tenant_tool``) and the delete
    endpoints below so the two can never drift (spec §B2). Four forms,
    selected by which arguments are given:

    * ``expert-work/platform/llm/{provider}`` (+ ``/{key_id}`` when ``key_id``
      is neither ``None`` nor ``"default"``)
    * ``expert-work/platform/tool/{tool}``
    * ``expert-work/platform/tenant/{tenant_id}/llm/{provider}``
    * ``expert-work/platform/tenant/{tenant_id}/tool/{tool}``
    """
    if tenant_id is not None:
        if tool is not None:
            return f"expert-work/platform/tenant/{tenant_id}/tool/{tool}"
        return f"expert-work/platform/tenant/{tenant_id}/llm/{provider}"
    if tool is not None:
        return f"expert-work/platform/tool/{tool}"
    name = f"expert-work/platform/llm/{provider}"
    if key_id not in (None, "default"):
        name = f"{name}/{key_id}"
    return name


async def _delete_managed_secret(
    secret_store: SecretStore, *, secret_ref: str | None, canonical: str
) -> bool:
    """Best-effort delete of the platform-managed secret backing a catalog row.

    Only deletes when ``secret_ref`` is exactly the platform-managed
    ``secret://{canonical}`` ref — an operator-supplied external
    ``secret://`` / ``kms://`` reference is never touched. Returns whether
    the managed secret was deleted (for the audit ``secret_deleted`` flag).
    """
    if secret_ref != f"secret://{canonical}":
        return False
    try:
        await secret_store.delete(canonical)
    except Exception:
        # The slot name is request-derived (CodeQL py/log-injection) — the
        # audit trail's ``secret_deleted: false`` carries the exact resource.
        logger.warning("platform_config: failed to delete managed secret", exc_info=True)
        return False
    return True


def build_platform_config_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/platform/credentials",
        tags=["platform_config"],
        dependencies=[Depends(platform_only(_PLATFORM_SCOPE_MESSAGE))],
    )

    @router.get("")
    async def get_platform_credentials(
        principal: Annotated[Principal, Depends(_principal)],
        store: Annotated[PlatformSecretStore, Depends(_get_store)],
        request: Request,
    ) -> dict[str, object]:
        """Full catalog view: per provider/tool → source (env/db/unset),
        secret_ref (effective; DB wins), enabled, used_by_agents (cross-tenant).
        Refs + flags only — no secret values."""
        env_provs = _env_provider_refs(request)
        env_tools = _env_tool_refs(request)
        agent_store = _get_agent_spec_store(request)
        embedding_provider = _embedding_provider(request)
        async with bypass_rls_session():
            # Y-MK — group provider rows by provider (one per key_id).
            db_prov_keys: dict[str, list[PlatformProviderSecretRecord]] = {}
            for row in await store.list_providers():
                db_prov_keys.setdefault(row.provider, []).append(row)
            db_tools = {row.tool: row for row in await store.list_tools()}
            tenant_prov_counts = Counter(
                row.provider for row in await store.list_tenant_providers()
            )
            tenant_tool_counts = Counter(row.tool for row in await store.list_tenant_tools())
            specs = (
                await agent_store.list_all_tenants(status=None, limit=1000)
                if agent_store is not None
                else []
            )
        prov_counts = _provider_usage_counts(specs, embedding_provider=embedding_provider)
        tool_counts = _tool_usage_counts(specs)

        def _provider_entry(provider: str) -> dict[str, object]:
            keys = sorted(db_prov_keys.get(provider, []), key=lambda r: (r.priority, r.key_id))
            # Backward-compat scalar fields reflect the primary key: prefer
            # 'default', else the best (lowest-priority) row.
            primary = next((k for k in keys if k.key_id == "default"), keys[0] if keys else None)
            return {
                "provider": provider,
                "source": "db" if keys else ("env" if provider in env_provs else "unset"),
                "secret_ref": (
                    primary.secret_ref if primary is not None else env_provs.get(provider)
                ),
                "enabled": (primary.enabled if primary is not None else provider in env_provs),
                "keys": [
                    {
                        "key_id": k.key_id,
                        "secret_ref": k.secret_ref,
                        "enabled": k.enabled,
                        "priority": k.priority,
                    }
                    for k in keys
                ],
                "used_by_agents": prov_counts.get(provider, 0),
                "tenant_override_count": tenant_prov_counts.get(provider, 0),
            }

        providers = [_provider_entry(provider) for provider in PROVIDER_CATALOG]
        tools = [
            {
                "tool": tool,
                "source": "db" if tool in db_tools else ("env" if tool in env_tools else "unset"),
                "secret_ref": (
                    db_tools[tool].secret_ref if tool in db_tools else env_tools.get(tool)
                ),
                "enabled": db_tools[tool].enabled if tool in db_tools else tool in env_tools,
                "used_by_agents": tool_counts.get(tool, 0),
                "tenant_override_count": tenant_tool_counts.get(tool, 0),
            }
            for tool in TOOL_CATALOG
        ]
        return {
            "success": True,
            "data": {"providers": providers, "tools": tools},
            "error": None,
        }

    async def _do_upsert_provider(
        *,
        provider: str,
        key_id: str,
        payload: PlatformSecretWrite,
        principal: Principal,
        store: PlatformSecretStore,
        service: PlatformSecretsService,
        audit: AuditLogger,
        secret_store: SecretStore,
        request: Request,
    ) -> dict[str, object]:
        if provider not in PROVIDER_CATALOG:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UNKNOWN_PROVIDER",
                    "message": f"provider {provider!r} not in catalog",
                },
            )
        # Stream Y-MK — multiple keys share one secret name slot per key_id so
        # rotated/extra keys never collide with the default one.
        name = _canonical_secret_name(provider=provider, key_id=key_id)
        secret_ref = await _resolve_write_ref(payload, secret_store, name=name)
        async with bypass_rls_session():
            row = await store.upsert_provider(
                provider=cast(Provider, provider),
                key_id=key_id,
                secret_ref=secret_ref,
                enabled=payload.enabled,
                priority=payload.priority,
                actor_id=principal.subject_id,
            )
        service.invalidate()
        _invalidate_built_agents(request)
        await _emit_platform_audit(
            audit,
            principal=principal,
            action=AuditAction.PLATFORM_PROVIDER_CREDENTIAL_UPSERT,
            key=f"{provider}#{key_id}",
            details={
                "enabled": payload.enabled,
                "secret_ref": secret_ref,
                "key_id": key_id,
                "priority": payload.priority,
            },
        )
        return {"success": True, "data": row.model_dump(mode="json"), "error": None}

    @router.put("/providers/{provider}")
    async def upsert_provider(
        provider: str,
        payload: PlatformSecretWrite,
        principal: Annotated[Principal, Depends(_principal)],
        store: Annotated[PlatformSecretStore, Depends(_get_store)],
        service: Annotated[PlatformSecretsService, Depends(_get_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        request: Request,
    ) -> dict[str, object]:
        """Upsert the provider's ``default`` key (Stream P; Y-MK key_id='default')."""
        return await _do_upsert_provider(
            provider=provider,
            key_id="default",
            payload=payload,
            principal=principal,
            store=store,
            service=service,
            audit=audit,
            secret_store=secret_store,
            request=request,
        )

    @router.put("/providers/{provider}/keys/{key_id}")
    async def upsert_provider_key(
        provider: str,
        key_id: str,
        payload: PlatformSecretWrite,
        principal: Annotated[Principal, Depends(_principal)],
        store: Annotated[PlatformSecretStore, Depends(_get_store)],
        service: Annotated[PlatformSecretsService, Depends(_get_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        request: Request,
    ) -> dict[str, object]:
        """Stream Y-MK — upsert one named key of a provider for multi-key failover."""
        return await _do_upsert_provider(
            provider=provider,
            key_id=key_id,
            payload=payload,
            principal=principal,
            store=store,
            service=service,
            audit=audit,
            secret_store=secret_store,
            request=request,
        )

    @router.put("/tools/{tool}")
    async def upsert_tool(
        tool: str,
        payload: PlatformSecretWrite,
        principal: Annotated[Principal, Depends(_principal)],
        store: Annotated[PlatformSecretStore, Depends(_get_store)],
        service: Annotated[PlatformSecretsService, Depends(_get_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        request: Request,
    ) -> dict[str, object]:
        if tool not in TOOL_CATALOG:
            raise HTTPException(
                status_code=422,
                detail={"code": "UNKNOWN_TOOL", "message": f"tool {tool!r} not in catalog"},
            )
        secret_ref = await _resolve_write_ref(
            payload, secret_store, name=_canonical_secret_name(tool=tool)
        )
        async with bypass_rls_session():
            row = await store.upsert_tool(
                tool=cast(Tool, tool),
                secret_ref=secret_ref,
                enabled=payload.enabled,
                actor_id=principal.subject_id,
            )
        service.invalidate()
        _invalidate_built_agents(request)
        await _emit_platform_audit(
            audit,
            principal=principal,
            action=AuditAction.PLATFORM_TOOL_CREDENTIAL_UPSERT,
            key=tool,
            details={"enabled": payload.enabled, "secret_ref": secret_ref},
        )
        return {"success": True, "data": row.model_dump(mode="json"), "error": None}

    @router.delete("/providers/{provider}", status_code=204)
    async def delete_provider(
        provider: str,
        principal: Annotated[Principal, Depends(_principal)],
        store: Annotated[PlatformSecretStore, Depends(_get_store)],
        service: Annotated[PlatformSecretsService, Depends(_get_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        request: Request,
    ) -> None:
        if provider in _env_provider_refs(request):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "PLATFORM_CREDENTIAL_IN_USE",
                    "message": "provider is defined in env config; remove it from settings instead",
                },
            )
        agent_store = _get_agent_spec_store(request)
        embedding_provider = _embedding_provider(request)
        async with bypass_rls_session():
            specs = (
                await agent_store.list_all_tenants(status=None, limit=1000)
                if agent_store is not None
                else []
            )
            in_use = _provider_usage_counts(specs, embedding_provider=embedding_provider).get(
                cast(Provider, provider), 0
            )
            if in_use > 0:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "PLATFORM_CREDENTIAL_IN_USE",
                        "message": f"{in_use} agent(s) reference this provider; disable instead",
                    },
                )
            row = await store.get_provider(cast(Provider, provider))
            secret_deleted = await _delete_managed_secret(
                secret_store,
                secret_ref=row.secret_ref if row is not None else None,
                canonical=_canonical_secret_name(provider=provider, key_id="default"),
            )
            deleted = await store.delete_provider(cast(Provider, provider))
        if not deleted:
            raise HTTPException(status_code=404, detail="platform provider credential not found")
        service.invalidate()
        _invalidate_built_agents(request)
        await _emit_platform_audit(
            audit,
            principal=principal,
            action=AuditAction.PLATFORM_PROVIDER_CREDENTIAL_DELETE,
            key=provider,
            details={"secret_deleted": secret_deleted},
        )

    @router.delete("/providers/{provider}/keys/{key_id}", status_code=204)
    async def delete_provider_key(
        provider: str,
        key_id: str,
        principal: Annotated[Principal, Depends(_principal)],
        store: Annotated[PlatformSecretStore, Depends(_get_store)],
        service: Annotated[PlatformSecretsService, Depends(_get_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        request: Request,
    ) -> None:
        """Stream Y-MK — delete one key. Blocked only when it is the *last*
        remaining key of an in-use, non-env provider (would orphan agents);
        deleting a sibling while others remain is always allowed."""
        agent_store = _get_agent_spec_store(request)
        embedding_provider = _embedding_provider(request)
        async with bypass_rls_session():
            rows = [r for r in await store.list_providers() if r.provider == provider]
            remaining = [r for r in rows if r.key_id != key_id]
            if not remaining and provider not in _env_provider_refs(request):
                specs = (
                    await agent_store.list_all_tenants(status=None, limit=1000)
                    if agent_store is not None
                    else []
                )
                in_use = _provider_usage_counts(specs, embedding_provider=embedding_provider).get(
                    cast(Provider, provider), 0
                )
                if in_use > 0:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "PLATFORM_CREDENTIAL_IN_USE",
                            "message": (
                                f"{in_use} agent(s) reference this provider and this is the "
                                "last key; disable instead or add another key first"
                            ),
                        },
                    )
            target = next((r for r in rows if r.key_id == key_id), None)
            secret_deleted = await _delete_managed_secret(
                secret_store,
                secret_ref=target.secret_ref if target is not None else None,
                canonical=_canonical_secret_name(provider=provider, key_id=key_id),
            )
            deleted = await store.delete_provider(cast(Provider, provider), key_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="platform provider key not found")
        service.invalidate()
        _invalidate_built_agents(request)
        await _emit_platform_audit(
            audit,
            principal=principal,
            action=AuditAction.PLATFORM_PROVIDER_CREDENTIAL_DELETE,
            key=f"{provider}#{key_id}",
            details={"key_id": key_id, "secret_deleted": secret_deleted},
        )

    @router.delete("/tools/{tool}", status_code=204)
    async def delete_tool(
        tool: str,
        principal: Annotated[Principal, Depends(_principal)],
        store: Annotated[PlatformSecretStore, Depends(_get_store)],
        service: Annotated[PlatformSecretsService, Depends(_get_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        request: Request,
    ) -> None:
        if tool in _env_tool_refs(request):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "PLATFORM_CREDENTIAL_IN_USE",
                    "message": "tool is defined in env config; remove it from settings instead",
                },
            )
        agent_store = _get_agent_spec_store(request)
        async with bypass_rls_session():
            specs = (
                await agent_store.list_all_tenants(status=None, limit=1000)
                if agent_store is not None
                else []
            )
            in_use = _tool_usage_counts(specs).get(cast(Tool, tool), 0)
            if in_use > 0:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "PLATFORM_CREDENTIAL_IN_USE",
                        "message": f"{in_use} agent(s) reference this tool; disable instead",
                    },
                )
            row = await store.get_tool(cast(Tool, tool))
            secret_deleted = await _delete_managed_secret(
                secret_store,
                secret_ref=row.secret_ref if row is not None else None,
                canonical=_canonical_secret_name(tool=tool),
            )
            deleted = await store.delete_tool(cast(Tool, tool))
        if not deleted:
            raise HTTPException(status_code=404, detail="platform tool credential not found")
        service.invalidate()
        _invalidate_built_agents(request)
        await _emit_platform_audit(
            audit,
            principal=principal,
            action=AuditAction.PLATFORM_TOOL_CREDENTIAL_DELETE,
            key=tool,
            details={"secret_deleted": secret_deleted},
        )

    # --- per-tenant overrides (Stream HX-8) ---------------------------

    @router.get("/tenants/{tenant_id}")
    async def get_tenant_overrides(
        tenant_id: UUID,
        principal: Annotated[Principal, Depends(_principal)],
        store: Annotated[PlatformSecretStore, Depends(_get_store)],
        request: Request,
    ) -> dict[str, object]:
        """Per-tenant override view: every catalog key with its override row
        (if any) and the tenant-effective source (tenant / suppressed /
        db / env / unset). Refs + flags only — no secret values."""
        await _require_tenant(request, tenant_id)
        env_provs = _env_provider_refs(request)
        env_tools = _env_tool_refs(request)
        async with bypass_rls_session():
            db_provs = {row.provider: row for row in await store.list_providers()}
            db_tools = {row.tool: row for row in await store.list_tools()}
            t_provs = {row.provider: row for row in await store.list_tenant_providers(tenant_id)}
            t_tools = {row.tool: row for row in await store.list_tenant_tools(tenant_id)}

        def _provider_entry(provider: Provider) -> dict[str, object]:
            override = t_provs.get(provider)
            if override is not None:
                source = "tenant" if override.enabled else "suppressed"
                effective = override.secret_ref if override.enabled else None
            elif provider in db_provs and db_provs[provider].enabled:
                source, effective = "db", db_provs[provider].secret_ref
            elif provider in db_provs:
                source, effective = "suppressed", None
            elif provider in env_provs:
                source, effective = "env", env_provs[provider]
            else:
                source, effective = "unset", None
            return {
                "provider": provider,
                "override": override.model_dump(mode="json") if override is not None else None,
                "effective_source": source,
                "effective_ref": effective,
            }

        def _tool_entry(tool: Tool) -> dict[str, object]:
            override = t_tools.get(tool)
            if override is not None:
                source = "tenant" if override.enabled else "suppressed"
                effective = override.secret_ref if override.enabled else None
            elif tool in db_tools and db_tools[tool].enabled:
                source, effective = "db", db_tools[tool].secret_ref
            elif tool in db_tools:
                source, effective = "suppressed", None
            elif tool in env_tools:
                source, effective = "env", env_tools[tool]
            else:
                source, effective = "unset", None
            return {
                "tool": tool,
                "override": override.model_dump(mode="json") if override is not None else None,
                "effective_source": source,
                "effective_ref": effective,
            }

        return {
            "success": True,
            "data": {
                "tenant_id": str(tenant_id),
                "providers": [_provider_entry(p) for p in PROVIDER_CATALOG],
                "tools": [_tool_entry(t) for t in TOOL_CATALOG],
            },
            "error": None,
        }

    @router.put("/tenants/{tenant_id}/providers/{provider}")
    async def upsert_tenant_provider(
        tenant_id: UUID,
        provider: str,
        payload: PlatformSecretWrite,
        principal: Annotated[Principal, Depends(_principal)],
        store: Annotated[PlatformSecretStore, Depends(_get_store)],
        service: Annotated[PlatformSecretsService, Depends(_get_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        request: Request,
    ) -> dict[str, object]:
        if provider not in PROVIDER_CATALOG:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "UNKNOWN_PROVIDER",
                    "message": f"provider {provider!r} not in catalog",
                },
            )
        await _require_tenant(request, tenant_id)
        secret_ref = await _resolve_write_ref(
            payload,
            secret_store,
            name=_canonical_secret_name(tenant_id=tenant_id, provider=provider),
        )
        async with bypass_rls_session():
            row = await store.upsert_tenant_provider(
                tenant_id=tenant_id,
                provider=cast(Provider, provider),
                secret_ref=secret_ref,
                enabled=payload.enabled,
                actor_id=principal.subject_id,
            )
        service.invalidate()
        _invalidate_built_agents(request, tenant_id=tenant_id)
        await _emit_platform_audit(
            audit,
            principal=principal,
            action=AuditAction.PLATFORM_PROVIDER_CREDENTIAL_TENANT_UPSERT,
            key=provider,
            details={
                "tenant_id": str(tenant_id),
                "enabled": payload.enabled,
                "secret_ref": secret_ref,
            },
        )
        return {"success": True, "data": row.model_dump(mode="json"), "error": None}

    @router.put("/tenants/{tenant_id}/tools/{tool}")
    async def upsert_tenant_tool(
        tenant_id: UUID,
        tool: str,
        payload: PlatformSecretWrite,
        principal: Annotated[Principal, Depends(_principal)],
        store: Annotated[PlatformSecretStore, Depends(_get_store)],
        service: Annotated[PlatformSecretsService, Depends(_get_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        request: Request,
    ) -> dict[str, object]:
        if tool not in TOOL_CATALOG:
            raise HTTPException(
                status_code=422,
                detail={"code": "UNKNOWN_TOOL", "message": f"tool {tool!r} not in catalog"},
            )
        await _require_tenant(request, tenant_id)
        secret_ref = await _resolve_write_ref(
            payload,
            secret_store,
            name=_canonical_secret_name(tenant_id=tenant_id, tool=tool),
        )
        async with bypass_rls_session():
            row = await store.upsert_tenant_tool(
                tenant_id=tenant_id,
                tool=cast(Tool, tool),
                secret_ref=secret_ref,
                enabled=payload.enabled,
                actor_id=principal.subject_id,
            )
        service.invalidate()
        _invalidate_built_agents(request, tenant_id=tenant_id)
        await _emit_platform_audit(
            audit,
            principal=principal,
            action=AuditAction.PLATFORM_TOOL_CREDENTIAL_TENANT_UPSERT,
            key=tool,
            details={
                "tenant_id": str(tenant_id),
                "enabled": payload.enabled,
                "secret_ref": secret_ref,
            },
        )
        return {"success": True, "data": row.model_dump(mode="json"), "error": None}

    @router.delete("/tenants/{tenant_id}/providers/{provider}", status_code=204)
    async def delete_tenant_provider(
        tenant_id: UUID,
        provider: str,
        principal: Annotated[Principal, Depends(_principal)],
        store: Annotated[PlatformSecretStore, Depends(_get_store)],
        service: Annotated[PlatformSecretsService, Depends(_get_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        request: Request,
    ) -> None:
        # Deleting an override just falls the tenant back to the platform
        # view — never an outage by itself, so no in-use guard (unlike the
        # platform-row delete above).
        async with bypass_rls_session():
            target = next(
                (r for r in await store.list_tenant_providers(tenant_id) if r.provider == provider),
                None,
            )
            secret_deleted = await _delete_managed_secret(
                secret_store,
                secret_ref=target.secret_ref if target is not None else None,
                canonical=_canonical_secret_name(tenant_id=tenant_id, provider=provider),
            )
            deleted = await store.delete_tenant_provider(
                tenant_id=tenant_id, provider=cast(Provider, provider)
            )
        if not deleted:
            raise HTTPException(status_code=404, detail="tenant provider override not found")
        service.invalidate()
        _invalidate_built_agents(request, tenant_id=tenant_id)
        await _emit_platform_audit(
            audit,
            principal=principal,
            action=AuditAction.PLATFORM_PROVIDER_CREDENTIAL_TENANT_DELETE,
            key=provider,
            details={"tenant_id": str(tenant_id), "secret_deleted": secret_deleted},
        )

    @router.delete("/tenants/{tenant_id}/tools/{tool}", status_code=204)
    async def delete_tenant_tool(
        tenant_id: UUID,
        tool: str,
        principal: Annotated[Principal, Depends(_principal)],
        store: Annotated[PlatformSecretStore, Depends(_get_store)],
        service: Annotated[PlatformSecretsService, Depends(_get_service)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        secret_store: Annotated[SecretStore, Depends(_get_secret_store)],
        request: Request,
    ) -> None:
        async with bypass_rls_session():
            target = next(
                (r for r in await store.list_tenant_tools(tenant_id) if r.tool == tool),
                None,
            )
            secret_deleted = await _delete_managed_secret(
                secret_store,
                secret_ref=target.secret_ref if target is not None else None,
                canonical=_canonical_secret_name(tenant_id=tenant_id, tool=tool),
            )
            deleted = await store.delete_tenant_tool(tenant_id=tenant_id, tool=cast(Tool, tool))
        if not deleted:
            raise HTTPException(status_code=404, detail="tenant tool override not found")
        service.invalidate()
        _invalidate_built_agents(request, tenant_id=tenant_id)
        await _emit_platform_audit(
            audit,
            principal=principal,
            action=AuditAction.PLATFORM_TOOL_CREDENTIAL_TENANT_DELETE,
            key=tool,
            details={"tenant_id": str(tenant_id), "secret_deleted": secret_deleted},
        )

    return router


async def _emit_platform_audit(
    audit: AuditLogger,
    *,
    principal: Principal,
    action: AuditAction,
    key: str,
    details: dict[str, object],
) -> None:
    # Platform credentials are tenant-less; the audit row is filed under the
    # acting admin's home tenant (same convention as platform-scope role
    # bindings). Emitted outside the bypass block so it lands under the
    # request's normal RLS context.
    await emit(
        audit,
        tenant_id=principal.tenant_id,
        actor_id=principal.subject_id,
        action=action,
        resource_type="platform_credential",
        resource_id=key,
        trace_id=current_trace_id_hex(),
        details=details,
    )
