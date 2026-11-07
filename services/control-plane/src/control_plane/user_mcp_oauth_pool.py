"""Per-(tenant, user) MCP OAuth pool — Stream MCP-OAUTH (OA-3b).

A user's ``connected`` :class:`mcp_oauth_connection` rows are built into a
per-user :class:`MCPServerPool` and reused across that user's agent builds.

Each OAuth connection becomes a **bearer**-style ``MCPServerConfig`` whose
``token_ref`` is the connection's stored ``access_token_ref`` — so the existing
``_build_mcp_client`` bearer path resolves the (already-refreshed-at-write-time)
access token from the secret store and injects ``Authorization: Bearer``. No
oauth2 client branch is needed.

A :class:`McpOAuthRefresher` (OA-6) is consulted per connection: it refreshes a
near-expiry access token in place, or reports the connection unusable (revoked /
expired), in which case it simply isn't attached and the rest of the agent still
builds. When no refresher is wired (some tests), an expired token is skipped.

Concurrency mirrors :class:`TenantMcpPoolService` (Stream V-D): a per-key lock
deduplicates builds, and a generation counter + rebuild-on-conflict closes the
lost-invalidation race without ever serving a closed pool.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from control_plane.mcp_oauth_refresh import McpOAuthRefresher
from control_plane.mcp_retired_clients import (
    RetiredMcpClients,
    close_retired,
    release_clients,
    retire_in_place,
)
from control_plane.tenant_scope import bypass_rls_session
from expert_work.persistence import McpConnectorCatalogStore, McpOAuthConnectionStore
from expert_work.protocol import McpConnectorCatalogRecord, McpOAuthConnectionRecord
from orchestrator.tools.mcp import (
    MCPClient,
    MCPServerConfig,
    MCPServerPool,
    MCPServerPoolLimitError,
)

logger = logging.getLogger("expert_work.control_plane.user_mcp_oauth_pool")

# (tenant_id, user_id) -> that user's connected-OAuth pool.
UserMcpOAuthPoolProvider = Callable[[UUID, str], Awaitable[MCPServerPool]]
McpClientFactory = Callable[[MCPServerConfig], Awaitable[MCPClient]]
Clock = Callable[[], datetime]

_MAX_BUILD_ATTEMPTS = 5

# TTL backstop for missed invalidation-bus events (PR-E3a) — matches the
# built-agent cache TTL (AgentRuntime.cache_ttl_s = 1800s). Without it the
# pool lives forever, so one lost bus event would be permanent staleness.
_POOL_TTL_S = 1800.0

# B-292 —— 构建时有连接没连上:等够这么久后的下一次取池重试一次(取值理由同租户池)。
_FAILED_RETRY_S = 60.0


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class UserMcpOAuthPoolService:
    """Caches one :class:`MCPServerPool` per (tenant, user), from OAuth connections."""

    def __init__(
        self,
        *,
        oauth_store: McpOAuthConnectionStore,
        catalog_store: McpConnectorCatalogStore,
        client_factory: McpClientFactory,
        refresher: McpOAuthRefresher | None = None,
        clock: Clock = _utc_now,
        monotonic_clock: Callable[[], float] = time.monotonic,
        on_expire: Callable[[UUID, str], None] | None = None,
    ) -> None:
        self._oauth_store = oauth_store
        self._catalog_store = catalog_store
        self._client_factory = client_factory
        self._refresher = refresher
        self._clock = clock
        # ``clock`` is wall-time for token expiry; the TTL backstop needs a
        # monotonic source (injectable in tests), hence the separate knob.
        self._monotonic_clock = monotonic_clock
        # B-248 — called once the TTL retired a user's pool: the app evicts
        # that user's cached agent builds (they hold its clients), so only
        # runs already in flight are left on the retired clients.
        self._on_expire = on_expire
        # key -> (pool, built_at monotonic stamp for the TTL backstop)
        self._pools: dict[tuple[UUID, str], tuple[MCPServerPool, float]] = {}
        self._locks_guard = asyncio.Lock()
        self._key_locks: dict[tuple[UUID, str], asyncio.Lock] = {}
        self._generation: dict[tuple[UUID, str], int] = {}
        # Clients retired on TTL expiry that runs may still hold (weak): a
        # later ``invalidate`` (disconnect) / ``close_all`` must hard-stop them.
        # Guarded by ``_locks_guard`` like ``_pools``.
        self._retired: RetiredMcpClients[tuple[UUID, str]] = RetiredMcpClients()
        # B-248 — per cached key, the connectors left out because the platform
        # had disabled them, so re-enabling one expires only those pools.
        self._skipped_off: dict[tuple[UUID, str], frozenset[str]] = {}
        # Connectors switched off on this replica (``connector_off`` until a
        # ``connector_on``): a last-attempt build that read the catalog before
        # the switch must not serve them. Guarded by ``_locks_guard``.
        self._switched_off: set[str] = set()
        # B-292 —— 缓存池缺了构建时没连上的连接:key -> 何时(单调时钟)可以重试。
        # 用户池每次 run 定构建缓存键时都会取,所以到点后的那次取池就地重试,不用后台任务。
        self._retry_at: dict[tuple[UUID, str], float] = {}

    def _retry_due(self, key: tuple[UUID, str]) -> bool:
        """B-292 —— 缓存池缺了没连上的连接,且已等够重试间隔。"""
        retry_at = self._retry_at.get(key)
        return retry_at is not None and self._monotonic_clock() >= retry_at

    async def _key_lock(self, key: tuple[UUID, str]) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._key_locks[key] = lock
            return lock

    async def _resolve_usable(
        self, record: McpOAuthConnectionRecord
    ) -> McpOAuthConnectionRecord | None:
        """Return a usable record (refreshing via OA-6 when wired), else ``None``."""
        if self._refresher is not None:
            return await self._refresher.ensure_fresh(record)
        return record if self._usable(record) else None

    def _usable(self, record: McpOAuthConnectionRecord) -> bool:
        if record.status != "connected" or not record.access_token_ref:
            return False
        # No refresher wired (some tests): skip an expired token — attaching it
        # would just fail at call time.
        return not (
            record.token_expires_at is not None and record.token_expires_at <= self._clock()
        )

    def _record_to_config(
        self, record: McpOAuthConnectionRecord, entry: McpConnectorCatalogRecord | None
    ) -> MCPServerConfig | None:
        # ``entry`` is the record's catalog row, prefetched by the caller in one
        # batch (was a per-record get_by_id here — the N+1). The FK is ON DELETE
        # CASCADE, so a live connection always has its entry.
        if entry is None:
            return None
        return MCPServerConfig(
            name=record.name,
            transport=entry.transport,
            url=record.resolved_url,
            auth_type="bearer",
            auth_config={"token_ref": record.access_token_ref or ""},
            timeout_s=30.0,
        )

    async def get_or_build(self, tenant_id: UUID, user_id: str) -> MCPServerPool:
        """Return the user's OAuth pool, building (and caching) on miss."""
        key = (tenant_id, user_id)
        for attempt in range(_MAX_BUILD_ATTEMPTS):
            last_attempt = attempt == _MAX_BUILD_ATTEMPTS - 1
            lock = await self._key_lock(key)
            async with lock:
                cached = self._pools.get(key)
                # B-292 —— 重试缺连接的缓存池时:它还留在缓存里,连着的客户端从它原样
                # 复用(不重连),只重拨缺的;新池进缓存时才替换它。
                reuse: MCPServerPool | None = None
                reuse_built_at = 0.0
                if cached is not None:
                    cached_pool, built_at = cached
                    if self._monotonic_clock() - built_at < _POOL_TTL_S:
                        if not self._retry_due(key):
                            return cached_pool
                        reuse, reuse_built_at = cached_pool, built_at
                    else:
                        # TTL backstop (PR-E3a): expired — drop like ``invalidate``
                        # (the generation bump keeps any concurrent build from
                        # caching stale data), then rebuild. Retire, not close:
                        # cached agents and in-flight runs still hold these
                        # clients and keep calling them on one-shot sessions.
                        async with self._locks_guard:
                            self._generation[key] = self._generation.get(key, 0) + 1
                            self._pools.pop(key, None)
                            self._retired.add_pool(key, cached_pool)
                        try:
                            await cached_pool.retire_all()
                        except Exception:
                            logger.warning("user_mcp_oauth_pool.expired_close_failed")
                        self._notify_expired(key)
                async with self._locks_guard:
                    gen = self._generation.get(key, 0)
                pool = MCPServerPool()
                fresh: list[MCPClient] = []
                failed = False
                skipped_off: set[str] = set()
                records = await self._oauth_store.list_for_user(
                    tenant_id=tenant_id, user_id=user_id
                )
                # One batched catalog lookup (platform NULL-tenant rows →
                # bypass RLS) instead of a get_by_id per record inside the
                # loop — that per-record read was the N+1 on this build path.
                async with bypass_rls_session():
                    entries = await self._catalog_store.get_by_ids([r.catalog_id for r in records])
                for record in records:
                    entry = entries.get(record.catalog_id)
                    if entry is not None and not entry.enabled:
                        # B-248 — a connector the platform disabled is off
                        # for every user (as for new connects), no refresh.
                        skipped_off.add(record.name)
                        continue
                    reused = reuse.get(record.name) if reuse is not None else None
                    if reused is not None:
                        # B-292 —— 连着的原样复用:不重连,也不再走刷新(同继续用旧池)。
                        try:
                            await pool.add(record.name, reused)
                        except MCPServerPoolLimitError:
                            logger.warning("user_mcp_oauth_pool.server_cap_reached")
                            break
                        continue
                    usable = await self._resolve_usable(record)
                    if usable is None:
                        continue
                    config = self._record_to_config(usable, entries.get(usable.catalog_id))
                    if config is None:
                        continue
                    try:
                        client = await self._client_factory(config)
                        await pool.add(record.name, client)
                        fresh.append(client)
                    except MCPServerPoolLimitError:
                        try:
                            await client.close()
                        except Exception:
                            logger.warning("user_mcp_oauth_pool.cap_orphan_close_failed")
                        logger.warning("user_mcp_oauth_pool.server_cap_reached")
                        break
                    except Exception:
                        logger.warning("user_mcp_oauth_pool.server_build_failed")
                        failed = True
                switched_off: list[MCPClient] = []
                dropped: list[MCPClient] | None = None
                async with self._locks_guard:
                    if self._generation.get(key, 0) == gen:
                        # B-292 —— 重试出的池沿用旧池的出生时间:TTL 兜底时限不被拉长。
                        born = self._monotonic_clock() if reuse is None else reuse_built_at
                        self._pools[key] = (pool, born)
                        self._skipped_off[key] = frozenset(skipped_off)
                        if failed:
                            self._retry_at[key] = self._monotonic_clock() + _FAILED_RETRY_S
                        else:
                            self._retry_at.pop(key, None)
                        if reuse is None:
                            return pool
                        # 旧池里没被新池复用的(被上限挤掉):随旧池一起退役,别没人管。
                        dropped = []
                        for server in reuse.names():
                            old = reuse.get(server)
                            if old is not None and pool.get(server) is not old:
                                self._retired.add(key, server, old)
                                dropped.append(old)
                    elif last_attempt:
                        # B-248 — no cache entry will ever close it: track it
                        # for invalidate / close_all, retire it below.
                        self._retired.add_pool(key, pool)
                        for server in pool.names():
                            found = pool.get(server)
                            if server in self._switched_off and found is not None:
                                switched_off.append(found)
            if dropped is not None and reuse is not None:
                # B-292 —— 重试出的池进了缓存。新连上了连接才清这个用户的构建,
                # 让它们挂上新工具;还是连不上就不清(工具没变)。
                await release_clients(retire=dropped)
                if set(pool.names()) - set(reuse.names()):
                    logger.info("user_mcp_oauth_pool.failed_server_recovered")
                    self._notify_expired(key)
                return pool
            if last_attempt:
                # Outside the key lock (B-248). A connector switched off
                # mid-build is stopped, the rest retired.
                await release_clients(close=switched_off)
                if reuse is None:
                    await retire_in_place(pool)
                else:
                    # B-292 —— 复用来的客户端仍归旧池,只退役本次新拨的。
                    await release_clients(retire=fresh)
                return pool
            if reuse is not None:
                # B-292 —— 作废的重试只关本次新拨的:复用来的还在旧池里服务。
                await release_clients(close=fresh)
                continue
            try:
                await pool.close_all()
            except Exception:
                logger.warning("user_mcp_oauth_pool.stale_close_failed")
        raise RuntimeError("get_or_build: retry loop exited")  # pragma: no cover

    def _notify_expired(self, key: tuple[UUID, str]) -> None:
        """Run ``on_expire`` (build eviction); a failure never fails the caller."""
        if self._on_expire is None:
            return
        try:
            self._on_expire(*key)
        except Exception as exc:
            logger.warning("user_mcp_oauth_pool.on_expire_failed err=%s", type(exc).__name__)

    def _bump_all_generations(self) -> None:
        """Make every in-flight build retry (it may have read the catalog
        before the change). Caller holds ``_locks_guard``."""
        for key in self._key_locks:
            self._generation[key] = self._generation.get(key, 0) + 1

    async def connector_off(self, name: str) -> None:
        """B-248 — the platform disabled / force-deleted oauth2 connector
        ``name``: hard-stop its clients (cached and retired copies) for every
        user. The rest of each affected pool is retired, not closed, so the
        user's other connectors keep serving runs already in flight."""
        async with self._locks_guard:
            self._switched_off.add(name)
            hit = [key for key, (pool, _) in self._pools.items() if name in pool.names()]
            stop = self._retired.pop_named(name)
            rest: list[MCPClient] = []
            for key in hit:
                pool, _ = self._pools.pop(key)
                self._retired.add_pool(key, pool)
                for server in pool.names():
                    client = pool.get(server)
                    if client is not None:
                        (stop if server == name else rest).append(client)
            self._bump_all_generations()
        # Builds first, so no new run picks a cached agent mid-teardown.
        for key in hit:
            self._notify_expired(key)
        await release_clients(close=stop, retire=rest)

    async def connector_on(self, name: str) -> None:
        """B-248 — oauth2 connector ``name`` was re-enabled: expire (retire,
        never stop) the pools built while it was off, so their next build
        picks it up."""
        async with self._locks_guard:
            self._switched_off.discard(name)
            hit = [key for key in self._pools if name in self._skipped_off.get(key, ())]
            rest: list[MCPClient] = []
            for key in hit:
                pool, _ = self._pools.pop(key)
                self._retired.add_pool(key, pool)
                rest.extend(c for c in map(pool.get, pool.names()) if c is not None)
            self._bump_all_generations()
        for key in hit:
            self._notify_expired(key)
        await release_clients(retire=rest)

    async def invalidate(self, tenant_id: UUID, user_id: str) -> None:
        """Close + drop the (tenant, user) cached pool (next build rebuilds),
        and hard-stop that user's clients an earlier TTL expiry retired."""
        key = (tenant_id, user_id)
        async with self._locks_guard:
            self._generation[key] = self._generation.get(key, 0) + 1
            entry = self._pools.pop(key, None)
            retired = self._retired.pop(key)
        if entry is not None:
            try:
                await entry[0].close_all()
            except Exception:
                logger.warning("user_mcp_oauth_pool.invalidate_close_failed")
        await close_retired(retired)

    async def close_all(self) -> None:
        """Close every cached pool and every retired client (app shutdown)."""
        async with self._locks_guard:
            pools = [entry[0] for entry in self._pools.values()]
            self._pools.clear()
            retired = self._retired.pop_all()
            for key in list(self._key_locks):
                self._generation[key] = self._generation.get(key, 0) + 1
            self._key_locks.clear()
        for pool in pools:
            try:
                await pool.close_all()
            except Exception as exc:
                logger.warning("user_mcp_oauth_pool.close_all_failed", exc_info=exc)
        await close_retired(retired)
