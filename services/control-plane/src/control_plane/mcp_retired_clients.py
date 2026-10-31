"""MCP clients a pool retired on TTL expiry, still reachable for a hard stop.

On TTL expiry the platform / tenant / user-OAuth pools *retire* their clients
and drop the pool: cached agents and in-flight runs keep calling a retired
client on one-shot sessions. A later ``invalidate`` (server deleted / disabled,
platform ``disabled_tools`` change, OAuth disconnect) or ``close_all``
(shutdown) only sees the *current* pool, so without this registry those runs
would keep calling a revoked server with old credentials.

A pool ``get_or_build`` serves uncached on its last attempt (invalidations kept
landing mid-build) is recorded and retired the same way (B-248): no cache entry
owns it, so nothing ever closed its sessions and no invalidation reached them.

Entries are held weakly: a retired client disappears from here as soon as no
agent or run references it, so the registry never keeps a client alive.
(Clients are dataclasses with ``eq=True`` and hence unhashable, so they are
keyed by ``id`` — an entry is removed when its client is collected, before the
id can be reused. The server name rides in the key so one connector's copies
can be stopped alone, B-248.)
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable, Hashable, Sequence

from orchestrator.tools.mcp import MCPClient, MCPServerPool

logger = logging.getLogger("expert_work.control_plane.mcp_retired_clients")

#: B-248 — cap on one bulk teardown (catalog-change handlers run in the request
#: and in the bus subscriber loop, which must not stall on slow servers). Past
#: it the remaining teardowns are cancelled; ``close()`` marks its client
#: stopped before it waits for anything, so a cancelled close is still a stop.
TEARDOWN_BUDGET_S = 10.0


class RetiredMcpClients[K: Hashable]:
    """Weak, per-pool-key record of clients retired on TTL expiry."""

    def __init__(self) -> None:
        self._clients: weakref.WeakValueDictionary[tuple[K, str, int], MCPClient] = (
            weakref.WeakValueDictionary()
        )

    def __len__(self) -> int:
        return len(self._clients)

    def add_pool(self, key: K, pool: MCPServerPool) -> None:
        """Record every client of ``pool`` (call before the pool is retired)."""
        for name in pool.names():
            client = pool.get(name)
            if client is not None:
                self._clients[(key, name, id(client))] = client

    def pop(self, key: K) -> list[MCPClient]:
        """Remove and return the live retired clients recorded under ``key``."""
        return self._pop(lambda entry: entry[0] == key)

    def pop_named(self, name: str) -> list[MCPClient]:
        """Remove and return every live retired client of server ``name``."""
        return self._pop(lambda entry: entry[1] == name)

    def pop_all(self) -> list[MCPClient]:
        """Remove and return every live retired client."""
        return self._pop(lambda _entry: True)

    def _pop(self, match: Callable[[tuple[K, str, int]], bool]) -> list[MCPClient]:
        taken = [(ref, client) for ref, client in list(self._clients.items()) if match(ref)]
        for ref, _client in taken:
            self._clients.pop(ref, None)
        return [client for _ref, client in taken]


async def retire_in_place(pool: MCPServerPool) -> None:
    """Retire ``pool``'s clients but keep them in it, so the pool still serves.

    For the uncached last-attempt pool (B-248): retired, a client keeps no
    long-lived session for anyone to leak and serves each call on a one-shot
    one. A failure is logged by type and never raised.
    """
    for name in pool.names():
        client = pool.get(name)
        retire = getattr(client, "retire", None)
        if retire is None:
            continue
        try:
            await retire()
        except Exception as exc:
            logger.warning("mcp_pool.uncached_retire_failed err=%s", type(exc).__name__)


async def release_clients(
    *, close: Sequence[MCPClient] = (), retire: Sequence[MCPClient] = ()
) -> None:
    """Hard-stop ``close`` and retire ``retire`` (closing those without
    ``retire``), concurrently and within :data:`TEARDOWN_BUDGET_S` (B-248).
    Failures are logged by type and never raised."""

    async def _one(release: Callable[[], Awaitable[None]]) -> None:
        try:
            await release()
        except Exception as exc:
            logger.warning("mcp_pool.release_failed err=%s", type(exc).__name__)

    releases = [c.close for c in close] + [getattr(c, "retire", c.close) for c in retire]
    if not releases:
        return
    try:
        async with asyncio.timeout(TEARDOWN_BUDGET_S):
            await asyncio.gather(*(_one(r) for r in releases))
    except TimeoutError:
        logger.warning("mcp_pool.release_over_budget clients=%d", len(releases))


async def close_retired(clients: Sequence[MCPClient]) -> None:
    """Hard-stop retired clients; a failure is logged by type and never raised."""
    for client in clients:
        try:
            await client.close()
        except Exception as exc:
            logger.warning("mcp_pool.retired_close_failed err=%s", type(exc).__name__)
