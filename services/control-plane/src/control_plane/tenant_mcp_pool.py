"""Per-tenant remote MCP server pool — Stream V-D (Mini-ADR V-4).

A tenant's registered remote MCP servers (``tenant_mcp_server``) are built
into a per-tenant :class:`MCPServerPool` on first use and reused across agent
builds. The pool is invalidated (closed + dropped) when the tenant's registry
changes (the registration API calls :meth:`invalidate`) and all pools are
closed at app shutdown (:meth:`close_all`).

Decoupling: the orchestrator never imports this — the agent builder receives a
``Callable`` provider bound to this service (mirrors ``mcp_allowlist_provider``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from uuid import UUID

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


class TenantMcpPoolService:
    """Caches one :class:`MCPServerPool` per tenant, built from the registry."""

    def __init__(
        self,
        *,
        store: TenantMcpServerStore,
        secret_store: SecretStore | None,
        client_factory: McpClientFactory,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._secret_store = secret_store
        self._client_factory = client_factory
        self._clock = clock  # injectable monotonic time (tests)
        # tenant_id -> (pool, built_at monotonic stamp for the TTL backstop)
        self._pools: dict[UUID, tuple[MCPServerPool, float]] = {}
        self._locks_guard = asyncio.Lock()  # guards _pools + _tenant_locks + _generation
        self._tenant_locks: dict[UUID, asyncio.Lock] = {}
        self._generation: dict[UUID, int] = {}

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
                    # TTL backstop (PR-E3a): expired — close + drop exactly
                    # like ``invalidate`` (the generation bump keeps any
                    # concurrent build from caching stale data), then rebuild.
                    async with self._locks_guard:
                        self._generation[tenant_id] = self._generation.get(tenant_id, 0) + 1
                        self._pools.pop(tenant_id, None)
                    try:
                        await cached_pool.close_all()
                    except Exception:
                        logger.warning("tenant_mcp_pool.expired_close_failed")
                async with self._locks_guard:
                    gen = self._generation.get(tenant_id, 0)
                pool = MCPServerPool()
                records = await self._store.list_for_tenant(tenant_id=tenant_id)
                for record in records:
                    if not record.enabled:
                        continue
                    try:
                        client = await self._client_factory(_record_to_config(record))
                        await pool.add(record.name, client)
                    except MCPServerPoolLimitError:
                        # Cap reached: close the just-opened client (it was never
                        # added, so pool.close_all can't reach it) and stop —
                        # further adds would also be rejected.
                        try:
                            await client.close()
                        except Exception:
                            logger.warning("tenant_mcp_pool.cap_orphan_close_failed")
                        logger.warning("tenant_mcp_pool.server_cap_reached")
                        break
                    except Exception:
                        logger.warning("tenant_mcp_pool.server_build_failed")
                async with self._locks_guard:
                    if self._generation.get(tenant_id, 0) == gen:
                        self._pools[tenant_id] = (pool, self._clock())
                        return pool
                    if last_attempt:
                        # Give up retrying — serve this fresh pool uncached
                        # (still usable; the next caller rebuilds).
                        return pool
            # Invalidation landed mid-build: close the orphan and retry so the
            # caller gets a usable pool instead of a closed, empty one.
            try:
                await pool.close_all()
            except Exception:
                logger.warning("tenant_mcp_pool.stale_close_failed")
        raise RuntimeError("get_or_build: retry loop exited without returning")  # pragma: no cover

    async def invalidate(self, tenant_id: UUID) -> None:
        """Close + drop the tenant's cached pool (next build rebuilds it)."""
        async with self._locks_guard:
            self._generation[tenant_id] = self._generation.get(tenant_id, 0) + 1
            entry = self._pools.pop(tenant_id, None)
        if entry is not None:
            try:
                await entry[0].close_all()
            except Exception:
                logger.warning("tenant_mcp_pool.invalidate_close_failed")

    async def close_all(self) -> None:
        """Close every cached pool (app shutdown)."""
        async with self._locks_guard:
            pools = [entry[0] for entry in self._pools.values()]
            self._pools.clear()
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
