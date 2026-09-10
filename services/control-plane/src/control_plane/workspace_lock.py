"""Postgres advisory-lock :class:`WorkspaceLock` — Stream TE-8.

The orchestrator tool layer declares the :class:`WorkspaceLock` Protocol and
defaults to a no-op; the control plane owns the database engine, so the real
cross-replica implementation lives here.

:class:`PgWorkspaceLock` holds ``pg_advisory_xact_lock`` keyed on the
workspace identity (``{tenant}:{user}``) for the duration of a write, so
``write_file`` / ``bash`` writes to one user's workspace serialise across
orchestrator replicas (TE-ADR-3). It uses the *transaction*-scoped advisory
lock (auto-released at commit/rollback) — never the session-scoped variant,
which leaks under PgBouncer transaction mode (``infra/README.md`` § Postgres).

It borrows the xact-lock + ``hashtextextended`` technique from
:func:`expert_work.runtime.event_log.db._acquire_thread_lock`, but with two
deliberate differences the technique alone doesn't cover:

- **The lock is held across a long external exec** (the sandbox write, up to
  bash's 300 s). The transaction therefore sits *idle-in-transaction* for
  that whole window, so the per-database short-DML timeout defaults
  (``idle_in_transaction_session_timeout`` / ``statement_timeout``) would
  otherwise terminate the txn mid-write — releasing the advisory lock early
  and breaking cross-replica exclusion. We raise both via ``SET LOCAL``
  (txn-scoped, auto-reverted, safe under PgBouncer transaction mode) above
  the max write duration. The event-log precedent doesn't need this because
  its txn is millisecond-short.
- **Its own advisory key space.** event_log uses the single-arg 64-bit
  ``pg_advisory_xact_lock(bigint)`` space; this uses the *two-arg*
  ``pg_advisory_xact_lock(int4, int4)`` space — a structurally separate key
  space in Postgres — so a workspace key can never collide with a thread key.

The lock is taken on a *raw* (non-RLS) session: it never reads a
tenant-scoped table, so it needs no tenant role / RLS context. Tenant
isolation comes from the ``{tenant}:{user}`` key itself.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_plane.advisory_locks import WORKSPACE_LOCK_CLASSID

logger = logging.getLogger(__name__)

#: Cap the lock transaction's idle / statement time above the max write exec
#: (bash 300 s) so the per-DB short-DML defaults can't terminate the txn
#: mid-write (which would release the advisory lock early). ``SET LOCAL`` only.
_LOCK_TXN_TIMEOUT_MS = 360_000


@dataclass
class PgWorkspaceLock:
    """Cross-replica per-workspace write lock backed by a PG advisory lock.

    Structurally satisfies ``orchestrator.tools.WorkspaceLock``."""

    #: A *raw* (non-RLS) sessionmaker — the lock touches no tenant tables.
    session_factory: async_sessionmaker[AsyncSession]

    @asynccontextmanager
    async def acquire(self, *, tenant_id: UUID | None, user_id: UUID | None) -> AsyncIterator[None]:
        # An ephemeral workspace (no user binding) is a per-sandbox tmpfs, not
        # a shared volume, so it needs no cross-replica lock. A call with no
        # tenant binding is rejected downstream before any write; skip+log so a
        # mis-wired "should-lock but tenant-less" path is observable.
        if tenant_id is None or user_id is None:
            logger.debug("workspace lock skipped (tenant_id=%s user_id=%s)", tenant_id, user_id)
            yield
            return
        key = f"{tenant_id}:{user_id}"
        async with self.session_factory() as session, session.begin():
            # SET LOCAL is txn-scoped and auto-reverted — safe under PgBouncer
            # transaction mode (a SET SESSION would leak to the next pooled txn).
            await session.execute(
                text(f"SET LOCAL idle_in_transaction_session_timeout = {_LOCK_TXN_TIMEOUT_MS}")
            )
            await session.execute(text(f"SET LOCAL statement_timeout = {_LOCK_TXN_TIMEOUT_MS}"))
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:classid, hashtext(:k))"),
                {"classid": WORKSPACE_LOCK_CLASSID, "k": key},
            )
            yield
        # RT-6 Tier B (RT-ADR-20) — a write-capable tool completed under the lock
        # (a raise would have skipped this and rolled the lock txn back). Record
        # it so a paused approval whose ``requested_at`` predates it can detect a
        # possible approve-then-swap-script. This bumps on *any* non-raising
        # holder — including a read-only bash (ls / cat) — so it over-approximates
        # a mutation; that's the safe direction for a forensic signal. Done in a
        # SEPARATE best-effort txn *after* the advisory lock releases, never
        # inside it: a bump failure (a schema-less deployment, a transient error)
        # must never poison the lock txn and break the exclusion contract. It is
        # audit-only — a missed bump is acceptable.
        await self._record_write(tenant_id, user_id)

    async def _record_write(self, tenant_id: UUID, user_id: UUID) -> None:
        """Best-effort ``last_write_at = now()`` bump (RT-6 Tier B, RT-ADR-20).

        ``user_workspace`` carries no RLS (migration 0018), so this raw session
        updates it without a tenant role. A workspace never ``resolve``-d yet
        (no row) simply matches nothing. Any failure is swallowed — the drift
        signal is a forensic aid, not a correctness requirement.
        """
        try:
            async with self.session_factory() as session, session.begin():
                await session.execute(
                    text(
                        "UPDATE user_workspace SET last_write_at = now() "
                        "WHERE tenant_id = :t AND user_id = :u"
                    ),
                    {"t": str(tenant_id), "u": str(user_id)},
                )
        except Exception:
            logger.debug(
                "workspace last_write_at bump skipped (tenant_id=%s user_id=%s)",
                tenant_id,
                user_id,
                exc_info=True,
            )
