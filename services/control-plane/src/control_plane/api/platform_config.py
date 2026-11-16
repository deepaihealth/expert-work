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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
from control_plane.credential_base_url import (
    BaseUrlRejectedError,
    audit_fields,
    ensure_public_address,
    normalize_base_url,
    official_base_url,
    requires_new_key,
)
from control_plane.credential_check import (
    CheckPlan,
    NoCheckModelError,
    plan_check,
    run_check,
)
from control_plane.invalidation_bus import InvalidationEvent
from control_plane.platform_provider_uses import (
    PlatformProviderUse,
    PlatformProviderUseOverride,
    resolve_platform_provider_uses,
)
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
    TenantProviderSecretRecord,
    Tool,
    validate_secret_ref,
)
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.secret_store import (
    SecretNotFoundError,
    SecretStore,
    parse_secret_ref,
)

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

    B-308 — provider writes may also carry the credential's **request address**:

    * ``base_url`` tells "not sent" from "sent empty" (``model_fields_set``):
      not sent keeps the row's address (so a plain enable / disable toggle never
      clears it); ``null`` or an empty string clears it back to the official
      address.
    * ``force_save`` is "save anyway": store the row although the connectivity
      check failed. It never overrides a malformed or a not-allowed address.

    Sending a non-empty ``base_url`` runs the connectivity check **every time**,
    also when it is the address the row already has — that is how an admin
    re-tests an address and clears its "unverified" mark. A client that only
    changes ``enabled`` / ``priority`` leaves the field out.

    A pasted ``value`` overwrites the row's fixed slot in the SecretStore, as
    before B-308. Changing the address needs a new key in the same save: a
    pasted ``value`` or a ``secret_ref`` other than the row's current one.

    Tool writes share this payload and refuse ``base_url``.
    """

    model_config = ConfigDict(extra="forbid")
    secret_ref: str | None = None
    value: SecretStr | None = None
    enabled: bool = True
    #: Stream Y-MK — failover order within a provider (lower tried first).
    #: Ignored for tool writes.
    priority: int = 100
    #: B-308 — request address (API root up to the version segment). See class doc.
    base_url: str | None = None
    #: B-308 — save although the connectivity check failed.
    force_save: bool = False

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


def _write_ref(payload: PlatformSecretWrite, *, name: str) -> str:
    """The ref the catalog row will hold after this write — computed without
    touching the SecretStore.

    A pasted raw ``value`` is stored under the slot ``name`` and surfaced as
    ``secret://<name>``; an operator-supplied ``secret_ref`` is kept as-is.
    """
    if payload.value is not None:
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


async def _store_pasted_value(
    payload: PlatformSecretWrite, secret_store: SecretStore, *, name: str
) -> None:
    """Encrypt a pasted raw ``value`` into the SecretStore under ``name``; the
    value never reaches the catalog or the audit row (Mini-ADR Q-4/Q-7)."""
    if payload.value is not None:
        await secret_store.put(name, payload.value.get_secret_value())


def _reject(error: BaseUrlRejectedError) -> HTTPException:
    return HTTPException(status_code=422, detail=error.detail())


def _reject_tool_base_url(payload: PlatformSecretWrite) -> None:
    """B-308 — a tool credential has no request address; the shared payload's
    field is refused when sent at all."""
    if "base_url" in payload.model_fields_set:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "BASE_URL_NOT_SUPPORTED",
                "message": "a request address cannot be set for a tool credential",
            },
        )


@dataclass(frozen=True)
class _ProviderWrite:
    """What a provider write will store, decided before anything is written."""

    secret_ref: str
    base_url: str | None
    base_url_unverified: bool
    #: Audit label: ``passed`` / ``forced:<reason>`` / ``not_run``.
    connectivity_check: str
    #: The vendor's HTTP status when a forced save followed a vendor error.
    connectivity_status: int | None = None
    #: The interface whose check failed before a forced save.
    connectivity_interface: str | None = None

    def audit(self) -> dict[str, object]:
        """B-308 — what the audit row says about the address: host + path
        fingerprint (never the address itself) and how the check went."""
        details: dict[str, object] = {
            **audit_fields(self.base_url),
            "connectivity_check": self.connectivity_check,
        }
        if self.connectivity_status is not None:
            details["connectivity_status"] = self.connectivity_status
        if self.connectivity_interface is not None:
            details["connectivity_interface"] = self.connectivity_interface
        return details


async def _plan_provider_write(
    request: Request,
    *,
    provider: str,
    payload: PlatformSecretWrite,
    secret_name: str,
    existing: PlatformProviderSecretRecord | TenantProviderSecretRecord | None,
    secret_store: SecretStore,
    usage_tenant_id: UUID,
    audit_refusal: Callable[[dict[str, object]], Awaitable[None]],
) -> _ProviderWrite:
    """B-308 §4.2 — run every check of a provider write, in order, **before**
    anything is stored:

    1. the address's format, that its host is public, and "a new address
       needs a new key";
    2. the connectivity check, with the plaintext of this request (when none
       was pasted, the key behind the request's ``secret_ref``).

    A refusal at either step leaves the SecretStore and the catalog row exactly
    as they were, and a refusal at step 1 sends no request anywhere. A failed
    connectivity check did call out with the key, so it leaves an audit row of
    its own (``audit_refusal``) even though nothing was saved.
    """
    address_sent = "base_url" in payload.model_fields_set

    async def refused(code: str, address: str | None) -> None:
        # A refusal that is about where a key would go is on record even though
        # nothing was saved: host and code, never the address.
        await audit_refusal({**audit_fields(address), "refused": code, "saved": False})

    base_url: str | None
    if not address_sent:
        base_url = existing.base_url if existing is not None else None
    elif payload.base_url is None or not payload.base_url.strip():
        base_url = None
    else:
        try:
            # ``None`` when the address is the provider's official root.
            base_url = normalize_base_url(provider, payload.base_url)
        except BaseUrlRejectedError as exc:
            raise _reject(exc) from exc
        if base_url is not None:
            try:
                await ensure_public_address(base_url)
            except BaseUrlRejectedError as exc:
                await refused(exc.code, base_url)
                raise _reject(exc) from exc
    secret_ref = _write_ref(payload, name=secret_name)
    before = (existing.secret_ref, existing.base_url) if existing is not None else None
    if requires_new_key(
        before=before, after=(secret_ref, base_url), pasted=payload.value is not None
    ):
        await refused("BASE_URL_NEEDS_NEW_KEY", base_url)
        raise HTTPException(
            status_code=422,
            detail={
                "code": "BASE_URL_NEEDS_NEW_KEY",
                "message": (
                    "the request address changed: give this service's key in the same "
                    "save (paste it, or give another reference)"
                ),
            },
        )
    if base_url is None:
        # Official address: nothing to check, and no "unverified" address left.
        return _ProviderWrite(secret_ref, None, False, "not_run")
    key_changed = payload.value is not None or (
        existing is not None and secret_ref != existing.secret_ref
    )
    if not (address_sent or key_changed):
        # Only enabled / priority changed: keep the address and its flag as they are.
        unverified = existing.base_url_unverified if existing is not None else False
        return _ProviderWrite(secret_ref, base_url, unverified, "not_run")
    if payload.value is not None:
        api_key = payload.value.get_secret_value()
    else:
        try:
            api_key = await secret_store.get(parse_secret_ref(secret_ref))
        except SecretNotFoundError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "SECRET_NOT_FOUND",
                    "message": (
                        "the referenced key does not exist in the secret store; "
                        "check the reference or paste the key"
                    ),
                },
            ) from exc
    try:
        plan = await _check_plan(request, provider)
    except NoCheckModelError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "BASE_URL_CHECK_UNAVAILABLE",
                "message": (
                    "no chat model of this provider is available to test the request "
                    "address; enable one in the model catalog, then save again"
                ),
            },
        ) from exc
    result = await run_check(
        plan,
        provider=cast(Provider, provider),
        base_url=base_url,
        api_key=api_key,
        usage_store=getattr(request.app.state, "token_usage_store", None),
        usage_tenant_id=usage_tenant_id,
    )
    if result.blocked:
        await refused("BASE_URL_BLOCKED_HOST", base_url)
        raise _reject(
            BaseUrlRejectedError(
                "BASE_URL_BLOCKED_HOST",
                "the request address is not allowed: only public https addresses are supported",
            )
        )
    if result.invalid:
        raise _reject(
            BaseUrlRejectedError(
                "BASE_URL_INVALID", "the request address is not valid: it cannot be requested"
            )
        )
    if result.passed:
        return _ProviderWrite(secret_ref, base_url, False, "passed")
    if not payload.force_save:
        await audit_refusal(
            {
                **audit_fields(base_url),
                "connectivity_check": f"failed:{result.reason}",
                "connectivity_status": result.http_status,
                "connectivity_interface": result.interface,
                "saved": False,
            }
        )
        raise HTTPException(
            status_code=422,
            detail={
                "code": "BASE_URL_CHECK_FAILED",
                "message": "the connectivity check of the request address failed",
                "reason": result.reason,
                "status": result.http_status,
                # Which of the provider's interfaces did not answer.
                "interface": result.interface,
            },
        )
    return _ProviderWrite(
        secret_ref,
        base_url,
        True,
        f"forced:{result.reason}",
        result.http_status,
        result.interface,
    )


async def _check_plan(request: Request, provider: str) -> list[CheckPlan]:
    """What the connectivity check calls for ``provider`` (see ``plan_check``)."""
    agent_store = _get_agent_spec_store(request)
    rate_cards = getattr(request.app.state, "model_rate_card_store", None)
    async with bypass_rls_session():
        specs = (
            await agent_store.list_all_tenants(status=None, limit=1000)
            if agent_store is not None
            else []
        )
        prices = await rate_cards.list(provider=provider) if rate_cards is not None else []
    used_by_agents = _provider_usage_counts(
        specs, embedding_provider=_embedding_provider(request)
    ).get(cast(Provider, provider), 0)
    platform_uses = (await _platform_provider_uses(request)).get(provider, [])
    return plan_check(
        provider,
        specs=specs,
        used_by_agents=used_by_agents,
        platform_uses=platform_uses,
        prices=prices,
    )


async def _save_provider_key[RowT: (PlatformProviderSecretRecord, TenantProviderSecretRecord)](
    request: Request,
    *,
    provider: str,
    payload: PlatformSecretWrite,
    canonical: str,
    current: RowT | None,
    secret_store: SecretStore,
    usage_tenant_id: UUID,
    audit_refusal: Callable[[dict[str, object]], Awaitable[None]],
    write_row: Callable[[_ProviderWrite], Awaitable[RowT]],
) -> tuple[RowT, _ProviderWrite]:
    """One provider-key write, for a platform key and a tenant override alike
    (B-308 §4.2). ``current`` is the row as read at the start.

    1. every check (:func:`_plan_provider_write`) — nothing stored yet;
    2. a pasted key overwrites the row's fixed slot ``canonical``;
    3. then the row is written.

    The key goes in before the row on purpose: a new address becomes visible
    to other replicas only once the key that goes with it is in place, so no
    reader pairs the new address with the old key. A failure between 2 and 3
    leaves the old address with the new key (the save reports the error).
    """
    write = await _plan_provider_write(
        request,
        provider=provider,
        payload=payload,
        secret_name=canonical,
        existing=current,
        secret_store=secret_store,
        usage_tenant_id=usage_tenant_id,
        audit_refusal=audit_refusal,
    )
    await _store_pasted_value(payload, secret_store, name=canonical)
    async with bypass_rls_session():
        row = await write_row(write)
    return row, write


def _platform_tenant_id(request: Request, fallback: UUID) -> UUID:
    """The platform's reserved tenant — a platform key's check is billed to it."""
    settings = getattr(request.app.state, "settings", None)
    return settings.platform_tenant_id if settings is not None else fallback


def _invalidate_built_agents(request: Request, *, tenant_id: UUID | None = None) -> None:
    """凭据轮换后清 built-agent 缓存 —— 轮换是同 ref 原地覆写值
    (``_canonical_secret_name`` 对同 (provider,key_id) 恒同 slot),ref 比对
    发现不了,已构建 agent 里烤住的旧明文 key 只能靠显式失效清掉。
    同时清 secret 值缓存(T3 落地后 ``app.state`` 上才有,getattr 兜底)。

    PR-E3b — 本地失效之外再广播一条 ``platform_secrets`` 事件,让对端副本
    做同样的清理(总线 handler 内联同样的三层;自投递双打无害)。"""
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
    bus = getattr(request.app.state, "invalidation_bus", None)
    if bus is not None:
        bus.publish_soon(
            InvalidationEvent(
                kind="platform_secrets",
                tenant_id=str(tenant_id) if tenant_id is not None else None,
            )
        )


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


async def _platform_provider_uses(request: Request) -> dict[str, list[PlatformProviderUse]]:
    """B-51 —— provider → 平台自身依赖它的功能列表。

    登记表(``platform_provider_uses.PLATFORM_PROVIDER_USES``)是 env 层的
    声明;两个功能的实况还压着 DB 覆盖层,在这里修正:

    * 向量化 / 重排 —— ``PlatformEmbeddingConfigService``(DB 行赢,行在
      就不回落 env;重排整条 ``None`` 就是关着的);
    * 质量裁判 —— ``PlatformQualityConfigService``,``enabled`` 是部署级
      硬闸 **AND** UI 开关(后者默认关),只看 settings 会把一个没开的功能
      报成开着,正是 banner 最该避免的噪音。

    轻量测试 app 不挂 settings → 返回空表(与 ``_env_provider_refs`` 同款
    降级),凭据页照旧只少这一列。
    """
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        return {}
    overrides: dict[str, PlatformProviderUseOverride] = {}
    embedding_service = getattr(request.app.state, "platform_embedding_config_service", None)
    if embedding_service is not None:
        embedding = await embedding_service.effective_embedding_config()
        if embedding is not None:
            overrides["embedding"] = PlatformProviderUseOverride(
                provider=embedding[0], model=embedding[1]
            )
        rerank = await embedding_service.effective_rerank_config()
        overrides["rerank"] = (
            PlatformProviderUseOverride(provider=rerank[0], model=rerank[1], enabled=True)
            if rerank is not None
            else PlatformProviderUseOverride(enabled=False)
        )
    quality_service = getattr(request.app.state, "quality_config_service", None)
    if quality_service is not None:
        quality = await quality_service.effective()
        overrides["quality_judge"] = PlatformProviderUseOverride(
            provider=quality.judge_provider,
            model=quality.judge_model,
            enabled=quality.enabled,
        )
    return resolve_platform_provider_uses(settings, overrides=overrides)


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
        platform_uses = await _platform_provider_uses(request)
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
                        # B-308 — the key's request address (``None`` =
                        # official) and whether it was saved without a passing
                        # connectivity check.
                        "base_url": k.base_url,
                        "base_url_unverified": k.base_url_unverified,
                    }
                    for k in keys
                ],
                # B-308 — the official root address, the form a request address
                # takes (the edit form's placeholder); ``None`` when this
                # provider takes no request address.
                "official_base_url": official_base_url(provider),
                "used_by_agents": prov_counts.get(provider, 0),
                # B-51 —— 平台自身的依赖。``used_by_agents`` 只数 agent,
                # 一行「未设置 + 被引用 0」曾经把「平台在用」说成「没人用」。
                "platform_uses": [use.as_dict() for use in platform_uses.get(provider, [])],
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
        canonical = _canonical_secret_name(provider=provider, key_id=key_id)
        async with bypass_rls_session():
            current = await store.get_provider(provider, key_id)

        async def write_row(write: _ProviderWrite) -> PlatformProviderSecretRecord:
            return await store.upsert_provider(
                provider=cast(Provider, provider),
                key_id=key_id,
                secret_ref=write.secret_ref,
                enabled=payload.enabled,
                priority=payload.priority,
                actor_id=principal.subject_id,
                base_url=write.base_url,
                base_url_unverified=write.base_url_unverified,
            )

        row, write = await _save_provider_key(
            request,
            provider=provider,
            payload=payload,
            canonical=canonical,
            current=current,
            secret_store=secret_store,
            usage_tenant_id=_platform_tenant_id(request, principal.tenant_id),
            audit_refusal=lambda details: _emit_platform_audit(
                audit,
                principal=principal,
                action=AuditAction.PLATFORM_PROVIDER_CREDENTIAL_UPSERT,
                key=f"{provider}#{key_id}",
                details={"key_id": key_id, **details},
            ),
            write_row=write_row,
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
                "secret_ref": write.secret_ref,
                "key_id": key_id,
                "priority": payload.priority,
                **write.audit(),
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
        _reject_tool_base_url(payload)
        tool_secret_name = _canonical_secret_name(tool=tool)
        secret_ref = _write_ref(payload, name=tool_secret_name)
        await _store_pasted_value(payload, secret_store, name=tool_secret_name)
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
                # B-308 — the override row carries its own ``base_url`` /
                # ``base_url_unverified``; no address = the official one.
                "override": override.model_dump(mode="json") if override is not None else None,
                "effective_source": source,
                "effective_ref": effective,
                "official_base_url": official_base_url(provider),
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
        canonical = _canonical_secret_name(tenant_id=tenant_id, provider=provider)
        async with bypass_rls_session():
            current = next(
                (r for r in await store.list_tenant_providers(tenant_id) if r.provider == provider),
                None,
            )

        async def write_row(write: _ProviderWrite) -> TenantProviderSecretRecord:
            return await store.upsert_tenant_provider(
                tenant_id=tenant_id,
                provider=cast(Provider, provider),
                secret_ref=write.secret_ref,
                enabled=payload.enabled,
                actor_id=principal.subject_id,
                base_url=write.base_url,
                base_url_unverified=write.base_url_unverified,
            )

        # B-308 — same path as a platform key; the check's usage is recorded
        # under this tenant.
        row, write = await _save_provider_key(
            request,
            provider=provider,
            payload=payload,
            canonical=canonical,
            current=current,
            secret_store=secret_store,
            usage_tenant_id=tenant_id,
            audit_refusal=lambda details: _emit_platform_audit(
                audit,
                principal=principal,
                action=AuditAction.PLATFORM_PROVIDER_CREDENTIAL_TENANT_UPSERT,
                key=provider,
                details={"tenant_id": str(tenant_id), **details},
            ),
            write_row=write_row,
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
                "secret_ref": write.secret_ref,
                **write.audit(),
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
        _reject_tool_base_url(payload)
        tool_secret_name = _canonical_secret_name(tenant_id=tenant_id, tool=tool)
        secret_ref = _write_ref(payload, name=tool_secret_name)
        await _store_pasted_value(payload, secret_store, name=tool_secret_name)
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
