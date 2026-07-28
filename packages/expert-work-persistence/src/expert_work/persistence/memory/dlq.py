"""Stream K.K7 — dead-letter queue for memory writebacks.

The orchestrator's ``memory_writeback`` node sometimes can't land the
extracted memories: the LLM extraction raises, the embedder is slow,
or the DB write hits a transient error. Without a queue those memories
were ``logger.warning`` + lost (audit gap G2b — best-effort = weak
version). This module adds the minimum viable retry path:

* ``MemoryWritebackDLQ`` — the abstract repository the writeback node
  pushes to on failure, and the retention-cleanup-job pulls from on
  schedule.
* ``DLQRow`` — wire shape carrying enough context (tenant, user,
  thread, extracted ``[(kind, content), ...]``, attempts, last_error)
  for the retry worker to redo the embed + DB write.
* ``InMemoryMemoryWritebackDLQ`` for unit tests.
* ``SqlMemoryWritebackDLQ`` for prod (Postgres / asyncpg).

The queue is deliberately thin — no priority, no exponential-backoff
algebra, no per-key fairness. Stream K.K7's job is to not lose
memories; sophistication can come later if the failure rate calls for
it.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import MemoryWritebackDLQRow

#: How long a claimed row stays invisible to other ``take_ready`` callers
#: before the next sweep treats it as an orphaned claim (crashed worker)
#: and reclaims it. W1-PR1 Task 2 — the DLQ has no separate "in progress"
#: status column, so the claim lease is expressed entirely through
#: ``next_retry_at``: claiming pushes it to ``now + _CLAIM_LEASE_S``, and
#: whichever of ``mark_done`` / ``record_failure`` eventually settles the
#: row overwrites it with something sooner (or deletes the row outright).
_CLAIM_LEASE_S: int = 600


@dataclass(frozen=True)
class DLQRow:
    """One pending memory writeback in the dead-letter queue.

    ``extracted`` is the list of ``(kind, content)`` pairs the
    orchestrator originally extracted from the trajectory; the retry
    worker re-embeds and re-writes them.
    """

    id: UUID
    tenant_id: UUID
    user_id: UUID
    source_thread_id: str | None
    source_run_id: str | None
    extracted: tuple[tuple[str, str], ...]
    attempts: int
    next_retry_at: datetime
    last_error: str | None
    created_at: datetime


def _row_to_dlq(row: MemoryWritebackDLQRow) -> DLQRow:
    pairs: list[tuple[str, str]] = []
    for item in row.extracted or ():
        if isinstance(item, list | tuple) and len(item) == 2:
            kind, content = item
            pairs.append((str(kind), str(content)))
    return DLQRow(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        source_thread_id=row.source_thread_id,
        source_run_id=row.source_run_id,
        extracted=tuple(pairs),
        attempts=int(row.attempts),
        next_retry_at=row.next_retry_at,
        last_error=row.last_error,
        created_at=row.created_at,
    )


class MemoryWritebackDLQ(abc.ABC):
    """Repository for failed memory writebacks."""

    @abc.abstractmethod
    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        source_thread_id: str | None,
        source_run_id: str | None = None,
        extracted: Sequence[tuple[str, str]],
        error: str,
    ) -> DLQRow:
        """Add one pending writeback. ``next_retry_at`` is ``now()`` so
        the next sweep picks it up immediately on the first failure;
        ``record_failure`` schedules subsequent retries further out."""

    @abc.abstractmethod
    async def take_ready(self, *, limit: int, now: datetime) -> list[DLQRow]:
        """Atomically claim up to ``limit`` rows whose ``next_retry_at <=
        now`` — a fleet of DLQ worker replicas shares this queue without
        double-embedding/double-writing the same row (W1-PR1). Each
        returned row's ``attempts`` already reflects this claim (bumped by
        one) and its ``next_retry_at`` has been pushed to ``now +
        _CLAIM_LEASE_S``; a replica that crashes mid-retry leaves the claim
        behind for a later sweep to reclaim once the lease elapses. The
        worker is responsible for either calling :meth:`mark_done` on
        success or :meth:`record_failure` on a new error — both settle
        ``next_retry_at`` to something sooner than the lease (or delete the
        row outright)."""

    @abc.abstractmethod
    async def mark_done(self, *, row_id: UUID) -> None:
        """Delete the row — the retry succeeded."""

    @abc.abstractmethod
    async def record_failure(
        self,
        *,
        row_id: UUID,
        error: str,
        when: datetime,
        next_retry_at: datetime,
    ) -> None:
        """Store ``error`` and schedule ``next_retry_at``. Does **not**
        bump ``attempts`` — :meth:`take_ready` already counted this
        attempt at claim time (W1-PR1)."""

    @abc.abstractmethod
    async def count(self) -> int:
        """Total queue depth — used by the worker for a metric."""

    @abc.abstractmethod
    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Phase 3a (purge_user) — drop every pending writeback for a user.

        Removes all of the user's queued rows and returns the count deleted
        (0 when none / re-purge). Tenant- AND user-scoped — the ``(tenant_id,
        user_id)`` predicate never touches another tenant's or user's rows.
        The queued payload holds extracted memory content (PII), so it is
        hard-deleted with the rest of the user's memory."""


# ---------------------------------------------------------------------------
# In-memory implementation — unit tests
# ---------------------------------------------------------------------------


@dataclass
class InMemoryMemoryWritebackDLQ(MemoryWritebackDLQ):
    """Process-local DLQ for tests. Not safe across processes."""

    _rows: dict[UUID, DLQRow] = field(default_factory=dict)

    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        source_thread_id: str | None,
        source_run_id: str | None = None,
        extracted: Sequence[tuple[str, str]],
        error: str,
    ) -> DLQRow:
        now = datetime.now(UTC)
        row = DLQRow(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            source_thread_id=source_thread_id,
            source_run_id=source_run_id,
            extracted=tuple((str(k), str(c)) for k, c in extracted),
            attempts=0,
            next_retry_at=now,
            last_error=error,
            created_at=now,
        )
        self._rows[row.id] = row
        return row

    async def take_ready(self, *, limit: int, now: datetime) -> list[DLQRow]:
        ready = [r for r in self._rows.values() if r.next_retry_at <= now]
        ready.sort(key=lambda r: r.next_retry_at)
        lease_until = now + timedelta(seconds=_CLAIM_LEASE_S)
        claimed: list[DLQRow] = []
        for row in ready[:limit]:
            updated = DLQRow(
                id=row.id,
                tenant_id=row.tenant_id,
                user_id=row.user_id,
                source_thread_id=row.source_thread_id,
                source_run_id=row.source_run_id,
                extracted=row.extracted,
                attempts=row.attempts + 1,
                next_retry_at=lease_until,
                last_error=row.last_error,
                created_at=row.created_at,
            )
            self._rows[row.id] = updated
            claimed.append(updated)
        return claimed

    async def mark_done(self, *, row_id: UUID) -> None:
        self._rows.pop(row_id, None)

    async def record_failure(
        self,
        *,
        row_id: UUID,
        error: str,
        when: datetime,
        next_retry_at: datetime,
    ) -> None:
        del when  # parameter retained for API symmetry with the SQL path
        existing = self._rows.get(row_id)
        if existing is None:
            return
        updated = DLQRow(
            id=existing.id,
            tenant_id=existing.tenant_id,
            user_id=existing.user_id,
            source_thread_id=existing.source_thread_id,
            source_run_id=existing.source_run_id,
            extracted=existing.extracted,
            attempts=existing.attempts,
            next_retry_at=next_retry_at,
            last_error=error,
            created_at=existing.created_at,
        )
        self._rows[row_id] = updated

    async def count(self) -> int:
        return len(self._rows)

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        victims = [
            rid
            for rid, r in self._rows.items()
            if r.tenant_id == tenant_id and r.user_id == user_id
        ]
        for rid in victims:
            del self._rows[rid]
        return len(victims)


# ---------------------------------------------------------------------------
# SQLAlchemy implementation — prod
# ---------------------------------------------------------------------------


class SqlMemoryWritebackDLQ(MemoryWritebackDLQ):
    """Postgres-backed DLQ. Uses one short transaction per call."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def enqueue(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        source_thread_id: str | None,
        source_run_id: str | None = None,
        extracted: Sequence[tuple[str, str]],
        error: str,
    ) -> DLQRow:
        row = MemoryWritebackDLQRow(
            tenant_id=tenant_id,
            user_id=user_id,
            source_thread_id=source_thread_id,
            source_run_id=source_run_id,
            extracted=[[str(k), str(c)] for k, c in extracted],
            last_error=error,
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return _row_to_dlq(row)

    async def take_ready(self, *, limit: int, now: datetime) -> list[DLQRow]:
        # Atomic CAS — a fleet of worker replicas shares this queue without
        # double-embedding/double-writing the same row: ``FOR UPDATE SKIP
        # LOCKED`` lets each replica's transaction skip rows another
        # replica already has locked, then the UPDATE flips exactly the
        # rows this replica won (bumping ``attempts`` and pushing the claim
        # lease) and hands them back via ``RETURNING`` in one round trip.
        # Mirrors ``knowledge/sql.py::claim_documents_for_ingest``.
        lease_until = now + timedelta(seconds=_CLAIM_LEASE_S)
        async with self._sf() as session:
            candidate_ids = (
                (
                    await session.execute(
                        select(MemoryWritebackDLQRow.id)
                        .where(MemoryWritebackDLQRow.next_retry_at <= now)
                        .order_by(MemoryWritebackDLQRow.next_retry_at.asc())
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            if not candidate_ids:
                await session.commit()
                return []
            rows = (
                (
                    await session.execute(
                        update(MemoryWritebackDLQRow)
                        .where(MemoryWritebackDLQRow.id.in_(candidate_ids))
                        .values(
                            attempts=MemoryWritebackDLQRow.attempts + 1,
                            next_retry_at=lease_until,
                        )
                        .returning(MemoryWritebackDLQRow)
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()
        by_id = {r.id: r for r in rows}
        # ``RETURNING`` doesn't preserve the candidate SELECT's ordering —
        # reapply it so callers still see oldest-claimed-first.
        return [_row_to_dlq(by_id[rid]) for rid in candidate_ids]

    async def mark_done(self, *, row_id: UUID) -> None:
        stmt = delete(MemoryWritebackDLQRow).where(MemoryWritebackDLQRow.id == row_id)
        async with self._sf() as session:
            await session.execute(stmt)
            await session.commit()

    async def record_failure(
        self,
        *,
        row_id: UUID,
        error: str,
        when: datetime,
        next_retry_at: datetime,
    ) -> None:
        del when
        stmt = (
            update(MemoryWritebackDLQRow)
            .where(MemoryWritebackDLQRow.id == row_id)
            .values(
                last_error=error,
                next_retry_at=next_retry_at,
            )
        )
        async with self._sf() as session:
            await session.execute(stmt)
            await session.commit()

    async def count(self) -> int:
        async with self._sf() as session:
            result = await session.execute(select(MemoryWritebackDLQRow.id).limit(10_000))
            return len(result.all())

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        stmt = delete(MemoryWritebackDLQRow).where(
            MemoryWritebackDLQRow.tenant_id == tenant_id,
            MemoryWritebackDLQRow.user_id == user_id,
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)


__all__ = [
    "DLQRow",
    "InMemoryMemoryWritebackDLQ",
    "MemoryWritebackDLQ",
    "SqlMemoryWritebackDLQ",
]
