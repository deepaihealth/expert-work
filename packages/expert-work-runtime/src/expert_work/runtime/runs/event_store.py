"""Durable SSE event store — Stream H.3 PR 3 (Mini-ADR H-7).

Persists every frame emitted to :class:`StreamBridge` so RunDetail's
Event stream panel (Stream H.3 PR 4) can replay terminal runs past the
bridge's 60-second cleanup window. Two implementations behind one ABC:

* :class:`InMemoryRunEventStore` — unit tests + the default app before
  the SQL backend is wired.
* :class:`SqlRunEventStore` — Postgres-backed, the ``run_event`` table
  (migration 0038).

Producer side: ``run_agent`` calls :meth:`RunEventStore.append` after
every ``bridge.publish`` (Stream H.3 PR 3). Failure → log + counter +
swallow; the SSE stream is never blocked by a store hiccup.

Consumer side: ``GET /v1/sessions/{thread}/runs/{run}/events`` (Stream
H.3 PR 4) chooses :meth:`bridge.subscribe` for live runs and
:meth:`RunEventStore.list` for terminal runs; the SSE wire format is
identical (decision A: SSE id ``"{created_at_ms}-{seq}"``).

Tenant scoping rides on the RLS policy walking ``run_event → agent_run
→ tenant_id``; the API never passes ``tenant_id`` here.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models.run_event import RunEventRow

#: Stream H.3 PR 3 (decision D) — same hard cap as RunStore.list_*.
MAX_LIST_LIMIT = 500


def _clamp_limit(limit: int) -> int:
    if limit < 1:
        return 1
    return min(limit, MAX_LIST_LIMIT)


@dataclass(frozen=True, slots=True)
class RunEventRecord:
    """One persisted SSE frame."""

    run_id: UUID
    seq: int
    event_name: str
    data: Any
    #: Millisecond epoch — replay endpoint re-emits SSE id as
    #: ``f"{created_at_ms}-{seq}"`` (matches ``StreamBridge`` live wire
    #: format so the client parser doesn't distinguish live vs replay).
    created_at_ms: int
    created_at: datetime


class RunEventStore(abc.ABC):
    """Append + read-by-run for persisted SSE frames."""

    @abc.abstractmethod
    async def append(self, record: RunEventRecord) -> None:
        """Persist one event frame for ``record.run_id``.

        Append-only — the ``(run_id, seq)`` primary key catches duplicate
        sequence numbers. Producers (``run_agent``) MUST supply
        monotonic ``seq`` per run.
        """

    @abc.abstractmethod
    async def list(
        self,
        *,
        run_id: UUID,
        since_seq: int | None = None,
        limit: int = 100,
    ) -> Sequence[RunEventRecord]:
        """Return frames for ``run_id``, oldest first; ``limit`` clamped
        to :data:`MAX_LIST_LIMIT`.

        Semantics (matches SSE ``Last-Event-ID``):

        * ``since_seq is None`` → from the beginning of the stream.
        * ``since_seq=N`` → events with ``seq > N`` (exclusive — the
          caller has already processed up to seq N).

        Tenant scoping is enforced by RLS on the underlying table (the
        policy joins ``agent_run.tenant_id = current_setting('app.tenant_id')``),
        so a cross-tenant probe returns an empty list rather than raising.
        """

    @abc.abstractmethod
    async def delete_for_runs(self, *, run_ids: Sequence[UUID]) -> int:
        """Remove ALL events for the given runs. Empty input removes nothing.

        Returns rows removed. Deletion-hygiene PR3 §A — called by the
        in-memory RunStore mirror; the SQL RunStore empties run_event
        inside its own delete transaction instead (atomicity).
        """

    async def next_seq(self, *, run_id: UUID) -> int:
        """The next free seq for ``run_id`` — ``max(seq) + 1``, or 0 if none.

        Stream 9.4 (HA failover) — when a peer instance resumes a reclaimed run
        it re-enters ``run_agent`` with a fresh seq counter. Restarting at 0
        would collide with the original owner's already-persisted frames on the
        ``(run_id, seq)`` primary key. Seeding the counter past the durable tail
        keeps the resumed run's events append-only and gap-free. Default pages
        through :meth:`list`; SQL overrides with a single ``MAX`` query.
        """
        last = -1
        since: int | None = None
        while True:
            batch = await self.list(run_id=run_id, since_seq=since, limit=MAX_LIST_LIMIT)
            if not batch:
                break
            last = batch[-1].seq
            if len(batch) < MAX_LIST_LIMIT:
                break
            since = last
        return last + 1


class InMemoryRunEventStore(RunEventStore):
    """In-memory :class:`RunEventStore` — unit tests."""

    def __init__(self) -> None:
        # Keyed by run_id for fast list; ordered insertion preserves seq.
        self._events: dict[UUID, list[RunEventRecord]] = {}

    async def append(self, record: RunEventRecord) -> None:
        bucket = self._events.setdefault(record.run_id, [])
        # Append-only invariant — duplicate ``(run_id, seq)`` is a producer
        # bug; surface it the same way the SQL primary key would.
        for existing in bucket:
            if existing.seq == record.seq:
                msg = f"duplicate seq={record.seq} for run_id={record.run_id}"
                raise ValueError(msg)
        bucket.append(record)

    async def list(
        self,
        *,
        run_id: UUID,
        since_seq: int | None = None,
        limit: int = 100,
    ) -> Sequence[RunEventRecord]:
        clamped = _clamp_limit(limit)
        rows = self._events.get(run_id, [])
        if since_seq is None:
            filtered = list(rows)
        else:
            filtered = [r for r in rows if r.seq > since_seq]
        filtered.sort(key=lambda r: r.seq)
        return filtered[:clamped]

    async def delete_for_runs(self, *, run_ids: Sequence[UUID]) -> int:
        removed = 0
        for rid in run_ids:
            removed += len(self._events.pop(rid, []))
        return removed


class SqlRunEventStore(RunEventStore):
    """Postgres-backed :class:`RunEventStore` — the ``run_event`` table.

    ``session_factory`` must be the RLS-wrapped sessionmaker — the
    ``app.tenant_id`` GUC scopes both ``append`` (the policy's
    ``WITH CHECK`` walks via the FK) and ``list``.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def append(self, record: RunEventRecord) -> None:
        async with self._sf() as session:
            session.add(
                RunEventRow(
                    run_id=record.run_id,
                    seq=record.seq,
                    event_name=record.event_name,
                    data=record.data,
                    created_at_ms=record.created_at_ms,
                    created_at=record.created_at,
                )
            )
            await session.commit()

    async def list(
        self,
        *,
        run_id: UUID,
        since_seq: int | None = None,
        limit: int = 100,
    ) -> Sequence[RunEventRecord]:
        clamped = _clamp_limit(limit)
        stmt = select(RunEventRow).where(RunEventRow.run_id == run_id)
        if since_seq is not None:
            stmt = stmt.where(RunEventRow.seq > since_seq)
        stmt = stmt.order_by(RunEventRow.seq.asc()).limit(clamped)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(r) for r in rows]

    async def delete_for_runs(self, *, run_ids: Sequence[UUID]) -> int:
        if not run_ids:
            return 0
        async with self._sf() as session:
            result = await session.execute(
                delete(RunEventRow).where(RunEventRow.run_id.in_(list(run_ids)))
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def next_seq(self, *, run_id: UUID) -> int:
        """``max(seq) + 1`` for ``run_id`` in one query (0 if none)."""
        stmt = select(func.coalesce(func.max(RunEventRow.seq) + 1, 0)).where(
            RunEventRow.run_id == run_id
        )
        async with self._sf() as session:
            return int((await session.execute(stmt)).scalar_one())


def _row_to_record(row: RunEventRow) -> RunEventRecord:
    return RunEventRecord(
        run_id=row.run_id,
        seq=row.seq,
        event_name=row.event_name,
        data=row.data,
        created_at_ms=row.created_at_ms,
        created_at=row.created_at,
    )


def make_event_record(
    *,
    run_id: UUID,
    seq: int,
    event_name: str,
    data: Any,
    created_at_ms: int | None = None,
) -> RunEventRecord:
    """Convenience builder — derives ``created_at`` from ``created_at_ms``.

    Producer-side helper so ``run_agent`` can drop one-liners next to
    its existing ``bridge.publish`` calls; ``created_at_ms`` defaults to
    ``time.time() * 1000`` matching ``StreamBridge._next_id``.
    """
    if created_at_ms is None:
        import time

        created_at_ms = int(time.time() * 1000)
    return RunEventRecord(
        run_id=run_id,
        seq=seq,
        event_name=event_name,
        data=data,
        created_at_ms=created_at_ms,
        created_at=datetime.fromtimestamp(created_at_ms / 1000.0, tz=UTC),
    )
