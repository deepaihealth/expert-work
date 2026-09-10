"""Cross-replica MCP OAuth refresh lock — Stream MCP-OAUTH (OA-6 hardening).

OAuth access tokens are refreshed lazily at per-user pool build. The pool's
in-process ``asyncio.Lock`` serialises refreshes *within one replica* only —
across **multiple control-plane replicas** (the production HA topology) two
replicas can refresh the same connection concurrently. When the authorization
server rotates the refresh token (RFC 6749 §6 / OAuth 2.1), the second refresh
presents an already-rotated refresh token, the AS replies ``invalid_grant``, and
the connection is wrongly marked ``revoked`` — an intermittent production failure
that scales with the number of OAuth users.

:class:`PgMcpOAuthRefreshLock` closes that gap with a Postgres **transaction**
advisory lock keyed on ``{tenant}:{user}`` — only one replica refreshes a given
user's connections at a time; the others block, then reload and find the token
already fresh. It mirrors :class:`PgWorkspaceLock` (``pg_advisory_xact_lock``,
auto-released at commit, safe under PgBouncer transaction mode) with two
differences:

- **Its own ``classid``** in the two-arg advisory key space, namespaced away
  from every other lock so the key spaces never collide (the value and the
  no-duplicates rule live in :mod:`control_plane.advisory_locks`).
- A **modest idle/statement timeout** (covers one discover+refresh HTTP round
  trip held inside the lock) rather than the workspace lock's 360 s — refresh is
  seconds, not a 300 s sandbox exec.

Taken on a *raw* (non-RLS) session: it touches no tenant table; isolation comes
from the ``{tenant}:{user}`` key.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.advisory_locks import MCP_OAUTH_REFRESH_LOCK_CLASSID

#: Cap the lock txn's idle / statement time above one discover+refresh HTTP round
#: trip (held idle-in-transaction inside the lock). ``SET LOCAL`` only (txn-scoped,
#: PgBouncer-safe). Generous over the ~15 s per-call HTTP timeout.
_LOCK_TXN_TIMEOUT_MS = 120_000


@dataclass
class PgMcpOAuthRefreshLock:
    """Cross-replica per-(tenant, user) OAuth-refresh lock (PG advisory lock)."""

    #: A *raw* (non-RLS) sessionmaker — the lock touches no tenant tables.
    session_factory: async_sessionmaker[AsyncSession]

    @asynccontextmanager
    async def acquire(self, *, tenant_id: UUID, user_id: str) -> AsyncIterator[None]:
        # user_id is the OAuth subject (a string), so the key is built from the
        # text form — matching mcp_oauth_connection.user_id.
        key = f"{tenant_id}:{user_id}"
        async with self.session_factory() as session, session.begin():
            await session.execute(
                text(f"SET LOCAL idle_in_transaction_session_timeout = {_LOCK_TXN_TIMEOUT_MS}")
            )
            await session.execute(text(f"SET LOCAL statement_timeout = {_LOCK_TXN_TIMEOUT_MS}"))
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:classid, hashtext(:k))"),
                {"classid": MCP_OAUTH_REFRESH_LOCK_CLASSID, "k": key},
            )
            yield
