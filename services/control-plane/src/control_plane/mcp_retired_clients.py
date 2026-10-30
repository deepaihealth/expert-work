"""MCP clients a pool retired on TTL expiry, still reachable for a hard stop.

On TTL expiry the platform / tenant / user-OAuth pools *retire* their clients
and drop the pool: cached agents and in-flight runs keep calling a retired
client on one-shot sessions. A later ``invalidate`` (server deleted / disabled,
platform ``disabled_tools`` change, OAuth disconnect) or ``close_all``
(shutdown) only sees the *current* pool, so without this registry those runs
would keep calling a revoked server with old credentials.

Entries are held weakly: a retired client disappears from here as soon as no
agent or run references it, so the registry never keeps a client alive.
(Clients are dataclasses with ``eq=True`` and hence unhashable, so they are
keyed by ``id`` — an entry is removed when its client is collected, before the
id can be reused.)
"""

from __future__ import annotations

import logging
import weakref
from collections.abc import Callable, Hashable, Sequence

from orchestrator.tools.mcp import MCPClient, MCPServerPool

logger = logging.getLogger("expert_work.control_plane.mcp_retired_clients")


class RetiredMcpClients[K: Hashable]:
    """Weak, per-pool-key record of clients retired on TTL expiry."""

    def __init__(self) -> None:
        self._clients: weakref.WeakValueDictionary[tuple[K, int], MCPClient] = (
            weakref.WeakValueDictionary()
        )

    def __len__(self) -> int:
        return len(self._clients)

    def add_pool(self, key: K, pool: MCPServerPool) -> None:
        """Record every client of ``pool`` (call before the pool is retired)."""
        for name in pool.names():
            client = pool.get(name)
            if client is not None:
                self._clients[(key, id(client))] = client

    def pop(self, key: K) -> list[MCPClient]:
        """Remove and return the live retired clients recorded under ``key``."""
        return self._pop(lambda entry_key: entry_key == key)

    def pop_all(self) -> list[MCPClient]:
        """Remove and return every live retired client."""
        return self._pop(lambda _entry_key: True)

    def _pop(self, match: Callable[[K], bool]) -> list[MCPClient]:
        taken = [(ref, client) for ref, client in list(self._clients.items()) if match(ref[0])]
        for ref, _client in taken:
            self._clients.pop(ref, None)
        return [client for _ref, client in taken]


async def close_retired(clients: Sequence[MCPClient]) -> None:
    """Hard-stop retired clients; a failure is logged by type and never raised."""
    for client in clients:
        try:
            await client.close()
        except Exception as exc:
            logger.warning("mcp_pool.retired_close_failed err=%s", type(exc).__name__)
