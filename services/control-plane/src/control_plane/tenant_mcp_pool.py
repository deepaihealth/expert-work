"""Per-tenant remote MCP server pool — Stream V-D (Mini-ADR V-4).

A tenant's registered remote MCP servers (``tenant_mcp_server``) are built
into a per-tenant :class:`MCPServerPool` on first use and reused across agent
builds. A registry change reaches only the server it concerns (B-259:
:meth:`server_changed` retires it, :meth:`server_removed` stops it, the
tenant's other servers' clients are reused by the next build);
:meth:`invalidate` (legacy whole-pool event) and :meth:`close_all` (shutdown)
close everything.

Decoupling: the orchestrator never imports this — the agent builder receives a
``Callable`` provider bound to this service (mirrors ``mcp_allowlist_provider``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Literal
from uuid import UUID

from control_plane.mcp_kept_clients import KeptClient, KeptMcpClients
from control_plane.mcp_retired_clients import (
    RetiredMcpClients,
    close_retired,
    release_clients,
    retire_in_place,
)
from expert_work.persistence import TenantMcpServerStore
from expert_work.protocol import TenantMcpServerRecord
from expert_work.runtime.secret_store import SecretStore
from orchestrator.tools.mcp import (
    MCPClient,
    MCPServerConfig,
    MCPServerPool,
    MCPServerPoolLimitError,
)

logger = logging.getLogger("expert_work.control_plane.tenant_mcp_pool")

# Bounded rebuild attempts when invalidation keeps landing mid-build (audit #2).
_MAX_BUILD_ATTEMPTS = 5

# TTL backstop for missed invalidation-bus events (PR-E3a) — matches the
# built-agent cache TTL (AgentRuntime.cache_ttl_s = 1800s). Without it the
# pool lives forever, so one lost bus event would be permanent staleness.
_POOL_TTL_S = 1800.0

# Provider handed to the agent builder: tenant_id -> that tenant's remote pool.
TenantMcpPoolProvider = Callable[[UUID], Awaitable[MCPServerPool]]

# Factory so tests can inject a RecordingMCPClient instead of real transports.
McpClientFactory = Callable[[MCPServerConfig], Awaitable[MCPClient]]


def _record_to_config(record: TenantMcpServerRecord) -> MCPServerConfig:
    """Map a registry record to an orchestrator :class:`MCPServerConfig`.

    Bearer auth carries the ``token_ref`` so the client builder resolves it
    via the SecretStore (the value never lives on the config — Mini-ADR U-11).
    """
    auth_config: dict[str, str] = {}
    if record.auth_type == "bearer" and record.token_secret_ref is not None:
        auth_config["token_ref"] = record.token_secret_ref
    # Custom headers ride alongside auth (Mini-ADR U-11 pattern): the secret ref
    # is carried on the config and resolved at connect-out, never the values.
    if record.custom_headers_ref is not None:
        auth_config["headers_ref"] = record.custom_headers_ref
    return MCPServerConfig(
        name=record.name,
        transport=record.transport,
        url=record.url,
        auth_type=record.auth_type,
        auth_config=auth_config,
        timeout_s=record.timeout_s,
        sse_read_timeout_s=record.sse_read_timeout_s,
    )


def connection_fingerprint(record: TenantMcpServerRecord) -> object:
    """B-259 —— 池里这一项的客户端由什么决定;与 ``_record_to_config`` 同源。
    注意:令牌 / 自定义头原地重贴时引用不变、指纹也不变,调用方要另行按「变了」处理。"""
    return _record_to_config(record)


#: 一次租户服务修改对池的影响(B-259)。
ServerChange = Literal["none", "changed", "removed"]

#: B-259 —— 运行期不读的字段(行元数据 + 探活结果);其余字段一律当作会影响运行,
#: 新加的字段默认也算(与平台目录同一原则)。
_NO_RUNTIME_EFFECT = frozenset(
    {
        "id",
        "tenant_id",
        "created_at",
        "updated_at",
        "created_by",
        "last_probe_at",
        "last_probe_status",
        "last_probe_error",
    }
)


def classify_server_patch(
    before: TenantMcpServerRecord, after: TenantMcpServerRecord, *, secret_rewritten: bool
) -> ServerChange:
    """B-259 —— 改完是停用的一律按 ``removed``(幂等):``before`` 不是行锁下读的,
    并发的启用 / 停用可能让它过期,宁可多硬停一次空的,也不漏停一个。
    否则重新启用、任何运行期字段变了、或令牌 / 头原地重贴 = ``changed``;都没有 = ``none``。"""
    if not after.enabled:
        return "removed"
    if not before.enabled or secret_rewritten:
        return "changed"
    ignored = set(_NO_RUNTIME_EFFECT)
    if before.model_dump(exclude=ignored) != after.model_dump(exclude=ignored):
        return "changed"
    return "none"


class TenantMcpPoolService:
    """Caches one :class:`MCPServerPool` per tenant, built from the registry."""

    def __init__(
        self,
        *,
        store: TenantMcpServerStore,
        secret_store: SecretStore | None,
        client_factory: McpClientFactory,
        clock: Callable[[], float] = time.monotonic,
        on_expire: Callable[[UUID], None] | None = None,
    ) -> None:
        self._store = store
        self._secret_store = secret_store
        self._client_factory = client_factory
        self._clock = clock  # injectable monotonic time (tests)
        # B-248 — called once the TTL retired a tenant's pool: the app evicts
        # that tenant's cached agent builds (they hold its clients), so only
        # runs already in flight are left on the retired clients.
        self._on_expire = on_expire
        # tenant_id -> (pool, built_at monotonic stamp for the TTL backstop)
        self._pools: dict[UUID, tuple[MCPServerPool, float]] = {}
        self._locks_guard = asyncio.Lock()  # guards _pools + _tenant_locks + _generation
        self._tenant_locks: dict[UUID, asyncio.Lock] = {}
        self._generation: dict[UUID, int] = {}
        # Clients retired on TTL expiry that runs may still hold (weak): a
        # later ``invalidate`` / ``close_all`` must hard-stop them too.
        # Guarded by ``_locks_guard`` like ``_pools``.
        self._retired: RetiredMcpClients[UUID] = RetiredMcpClients()
        # B-259 —— 缓存池成员的连接指纹;改一项时其它项暂存在这里给下次构建复用。
        # Guarded by ``_locks_guard``.
        self._kept: KeptMcpClients[UUID] = KeptMcpClients()

    async def _tenant_lock(self, tenant_id: UUID) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._tenant_locks.get(tenant_id)
            if lock is None:
                lock = asyncio.Lock()
                self._tenant_locks[tenant_id] = lock
            return lock

    async def get_or_build(self, tenant_id: UUID) -> MCPServerPool:
        """Return the tenant's remote pool, building (and caching) on miss.

        Uses a per-tenant lock so different tenants build in parallel while
        still deduplicating concurrent builds for the same tenant.  A server
        that fails to connect is skipped (logged, no tenant-derived values) so
        one bad server cannot break the whole agent build.  When the server cap
        is hit the just-opened client is closed before breaking — it was never
        added to the pool so ``pool.close_all`` cannot reach it.

        Generation counter guards the lost-invalidation race: ``invalidate``
        bumps the generation under ``_locks_guard`` while a build may be
        in-flight.  The build captures the generation before building and
        re-checks it before caching.  If the generation changed the build was
        invalidated mid-flight — the just-built pool is closed and the build is
        **retried** (audit #2). Serving the invalidated pool would hand the
        caller a closed, empty pool (``close_all`` clears its clients), so a
        rebuild is the only way to return fresh, usable data. Retries are
        bounded; under a sustained invalidation storm the last attempt serves a
        fresh-but-uncached pool rather than spinning forever.
        """
        for attempt in range(_MAX_BUILD_ATTEMPTS):
            last_attempt = attempt == _MAX_BUILD_ATTEMPTS - 1
            lock = await self._tenant_lock(tenant_id)
            async with lock:
                cached = self._pools.get(tenant_id)
                if cached is not None:
                    cached_pool, built_at = cached
                    if self._clock() - built_at < _POOL_TTL_S:
                        return cached_pool
                    # TTL backstop (PR-E3a): expired — drop like ``invalidate``
                    # (the generation bump keeps any concurrent build from
                    # caching stale data), then rebuild. Retire, not close:
                    # cached agents and in-flight runs still hold these
                    # clients and keep calling them on one-shot sessions.
                    async with self._locks_guard:
                        self._generation[tenant_id] = self._generation.get(tenant_id, 0) + 1
                        self._pools.pop(tenant_id, None)
                        self._retired.add_pool(tenant_id, cached_pool)
                        self._kept.dropped(tenant_id)
                    try:
                        await cached_pool.retire_all()
                    except Exception:
                        logger.warning("tenant_mcp_pool.expired_close_failed")
                    self._notify_expired(tenant_id)
                async with self._locks_guard:
                    gen = self._generation.get(tenant_id, 0)
                    carried = self._kept.peek(tenant_id)
                pool, members, fresh = await self._build(tenant_id, carried)
                unused: list[MCPClient] = []
                async with self._locks_guard:
                    cached_now = self._generation.get(tenant_id, 0) == gen
                    if cached_now:
                        for server, client in self._kept.cached(tenant_id, members):
                            self._retired.add(tenant_id, server, client)
                            unused.append(client)
                        # 复用的长连接按它最早的出生时间算 TTL,兜底时限不被拉长。
                        born = [m.born_at for m in members.values()]
                        self._pools[tenant_id] = (pool, min([*born, self._clock()]))
                    elif last_attempt:
                        # B-248 — no cache entry will ever close it: track it
                        # for invalidate / close_all, retire it below.
                        self._retired.add_pool(tenant_id, pool)
                        self._kept.adopted(tenant_id, members)
            if cached_now:
                # B-259 —— 暂存里没被复用的(指纹变了 / 这一项没了):退役。
                await release_clients(retire=unused)
                return pool
            if last_attempt:
                # Give up retrying — serve this fresh pool uncached (still
                # usable; the next caller rebuilds). Outside the tenant lock.
                await retire_in_place(pool)
                return pool
            # Invalidation landed mid-build: close what this attempt opened and
            # retry so the caller gets a usable pool instead of a closed, empty
            # one. B-259 —— 复用来的暂存客户端不关:别的构建 / run 还拿着它们。
            await release_clients(close=fresh)
        raise RuntimeError("get_or_build: retry loop exited without returning")  # pragma: no cover

    async def _build(
        self, tenant_id: UUID, carried: dict[str, KeptClient]
    ) -> tuple[MCPServerPool, dict[str, KeptClient], list[MCPClient]]:
        """Build the tenant's pool from the registry; returns it, its members
        and the clients this call opened (as opposed to reused from ``carried``)."""
        pool = MCPServerPool()
        members: dict[str, KeptClient] = {}
        fresh: list[MCPClient] = []
        records = await self._store.list_for_tenant(tenant_id=tenant_id)
        for record in records:
            if not record.enabled:
                continue
            fingerprint = connection_fingerprint(record)
            kept = carried.get(record.name)
            if kept is not None and kept.fingerprint != fingerprint:
                kept = None
            try:
                if kept is not None:
                    # B-259 —— 连接配置没变:复用暂存的长连接,不重连。
                    client, born_at = kept.client, kept.born_at
                else:
                    client = await self._client_factory(_record_to_config(record))
                    born_at = self._clock()
                await pool.add(record.name, client)
                members[record.name] = KeptClient(client, fingerprint, born_at)
                if kept is None:
                    fresh.append(client)
            except MCPServerPoolLimitError:
                # Cap reached: close the just-opened client (it was never
                # added, so pool.close_all can't reach it) and stop — further
                # adds would also be rejected. A reused one stays carried.
                if kept is None:
                    try:
                        await client.close()
                    except Exception:
                        logger.warning("tenant_mcp_pool.cap_orphan_close_failed")
                logger.warning("tenant_mcp_pool.server_cap_reached")
                break
            except Exception:
                logger.warning("tenant_mcp_pool.server_build_failed")
        return pool, members, fresh

    def _notify_expired(self, tenant_id: UUID) -> None:
        """Run ``on_expire`` (build eviction); a failure never fails the caller."""
        if self._on_expire is None:
            return
        try:
            self._on_expire(tenant_id)
        except Exception as exc:
            logger.warning("tenant_mcp_pool.on_expire_failed err=%s", type(exc).__name__)

    async def server_changed(self, tenant_id: UUID, name: str) -> None:
        """B-259 —— 租户的服务 ``name`` 新加 / 改了连接配置 / 重新启用:只退役它自己的
        客户端(正在跑的调用跑完;之后走一次性会话、仍是旧配置,直到持有它的构建被清掉),
        该租户其它服务的长连接留给下一次构建复用。"""
        await self._switch(tenant_id, name, stop=False)

    async def server_removed(self, tenant_id: UUID, name: str) -> None:
        """B-259 —— 租户的服务 ``name`` 被停用 / 删除:只硬停它自己(连同 TTL 先前
        退役的副本),该租户其它服务照常。"""
        await self._switch(tenant_id, name, stop=True)

    async def _switch(self, tenant_id: UUID, name: str, *, stop: bool) -> None:
        async with self._locks_guard:
            # 正在进行的构建可能是按旧登记读的:让它重来。
            self._generation[tenant_id] = self._generation.get(tenant_id, 0) + 1
            self._pools.pop(tenant_id, None)
            hit = self._kept.switch(tenant_id, name)
            target = [hit.client] if hit is not None else []
            if stop:
                target += self._retired.pop_one(tenant_id, name)
            elif hit is not None:
                self._retired.add(tenant_id, name, hit.client)
        # 先清构建,再拆连接:新 run 不会在拆的过程中拿到旧构建(B-248 同序)。
        self._notify_expired(tenant_id)
        if stop:
            await release_clients(close=target)
        else:
            await release_clients(retire=target)

    async def invalidate(self, tenant_id: UUID) -> None:
        """Close + drop the tenant's cached pool (next build rebuilds it), and
        hard-stop the tenant's clients an earlier TTL expiry retired."""
        async with self._locks_guard:
            self._generation[tenant_id] = self._generation.get(tenant_id, 0) + 1
            entry = self._pools.pop(tenant_id, None)
            retired = self._retired.pop(tenant_id) + self._kept.pop_key(tenant_id)
        if entry is not None:
            try:
                await entry[0].close_all()
            except Exception:
                logger.warning("tenant_mcp_pool.invalidate_close_failed")
        await close_retired(retired)

    async def close_all(self) -> None:
        """Close every cached pool and every retired client (app shutdown)."""
        async with self._locks_guard:
            pools = [entry[0] for entry in self._pools.values()]
            self._pools.clear()
            retired = self._retired.pop_all() + self._kept.pop_all()
            # Bump every known tenant's generation so any in-flight build won't
            # cache a stale pool after shutdown clears the store.
            for tid in list(self._tenant_locks):
                self._generation[tid] = self._generation.get(tid, 0) + 1
            self._tenant_locks.clear()
        for pool in pools:
            try:
                await pool.close_all()
            except Exception as exc:
                logger.warning(
                    "tenant_mcp_pool.close_all_failed pool_count=%d",
                    len(pools),
                    exc_info=exc,
                )
        await close_retired(retired)
