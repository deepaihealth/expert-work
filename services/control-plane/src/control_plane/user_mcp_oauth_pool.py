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
    ) -> None:
        self._oauth_store = oauth_store
        self._catalog_store = catalog_store
        self._client_factory = client_factory
        self._refresher = refresher
        self._clock = clock
        # ``clock`` is wall-time for token expiry; the TTL backstop needs a
        # monotonic source (injectable in tests), hence the separate knob.
        self._monotonic_clock = monotonic_clock
        # key -> (pool, built_at monotonic stamp for the TTL backstop)
        self._pools: dict[tuple[UUID, str], tuple[MCPServerPool, float]] = {}
        self._locks_guard = asyncio.Lock()
        self._key_locks: dict[tuple[UUID, str], asyncio.Lock] = {}
        self._generation: dict[tuple[UUID, str], int] = {}

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
                if cached is not None:
                    cached_pool, built_at = cached
                    if self._monotonic_clock() - built_at < _POOL_TTL_S:
                        return cached_pool
                    # TTL backstop (PR-E3a): expired — close + drop exactly
                    # like ``invalidate`` (the generation bump keeps any
                    # concurrent build from caching stale data), then rebuild.
                    async with self._locks_guard:
                        self._generation[key] = self._generation.get(key, 0) + 1
                        self._pools.pop(key, None)
                    try:
                        await cached_pool.close_all()
                    except Exception:
                        logger.warning("user_mcp_oauth_pool.expired_close_failed")
                async with self._locks_guard:
                    gen = self._generation.get(key, 0)
                pool = MCPServerPool()
                records = await self._oauth_store.list_for_user(
                    tenant_id=tenant_id, user_id=user_id
                )
                # One batched catalog lookup (platform NULL-tenant rows →
                # bypass RLS) instead of a get_by_id per record inside the
                # loop — that per-record read was the N+1 on this build path.
                async with bypass_rls_session():
                    entries = await self._catalog_store.get_by_ids([r.catalog_id for r in records])
                for record in records:
                    usable = await self._resolve_usable(record)
                    if usable is None:
                        continue
                    config = self._record_to_config(usable, entries.get(usable.catalog_id))
                    if config is None:
                        continue
                    try:
                        client = await self._client_factory(config)
                        await pool.add(record.name, client)
                    except MCPServerPoolLimitError:
                        try:
                            await client.close()
                        except Exception:
                            logger.warning("user_mcp_oauth_pool.cap_orphan_close_failed")
                        logger.warning("user_mcp_oauth_pool.server_cap_reached")
                        break
                    except Exception:
                        logger.warning("user_mcp_oauth_pool.server_build_failed")
                async with self._locks_guard:
                    if self._generation.get(key, 0) == gen:
                        self._pools[key] = (pool, self._monotonic_clock())
                        return pool
                    if last_attempt:
                        return pool
            try:
                await pool.close_all()
            except Exception:
                logger.warning("user_mcp_oauth_pool.stale_close_failed")
        raise RuntimeError("get_or_build: retry loop exited")  # pragma: no cover

    async def invalidate(self, tenant_id: UUID, user_id: str) -> None:
        """Close + drop the (tenant, user) cached pool (next build rebuilds)."""
        key = (tenant_id, user_id)
        async with self._locks_guard:
            self._generation[key] = self._generation.get(key, 0) + 1
            entry = self._pools.pop(key, None)
        if entry is not None:
            try:
                await entry[0].close_all()
            except Exception:
                logger.warning("user_mcp_oauth_pool.invalidate_close_failed")

    async def close_all(self) -> None:
        """Close every cached pool (app shutdown)."""
        async with self._locks_guard:
            pools = [entry[0] for entry in self._pools.values()]
            self._pools.clear()
            for key in list(self._key_locks):
                self._generation[key] = self._generation.get(key, 0) + 1
            self._key_locks.clear()
        for pool in pools:
            try:
                await pool.close_all()
            except Exception as exc:
                logger.warning("user_mcp_oauth_pool.close_all_failed", exc_info=exc)
