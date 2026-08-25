"""Durable SSE event store — Stream H.3 PR 3 (Mini-ADR H-7).

Persists every frame emitted to :class:`StreamBridge` so RunDetail's
Event stream panel (Stream H.3 PR 4) can replay terminal runs past the
bridge's 60-second cleanup window. Two implementations behind one ABC:

* :class:`InMemoryRunEventStore` — unit tests + the default app before
  the SQL backend is wired.
* :class:`SqlRunEventStore` — Postgres-backed, the ``run_event`` table
  (migration 0038).

Producer side: ``run_agent`` enqueues a record per ``bridge.publish``
into a bounded in-process queue; a background writer drains it in
batches through :meth:`RunEventStore.append_batch` (二期 PR3 — moved
off the SSE stream's hot path). Failure → log + counter + swallow; the
SSE stream is never blocked by a store hiccup.

Consumer side: ``GET /v1/sessions/{thread}/runs/{run}/events`` (Stream
H.3 PR 4) chooses :meth:`bridge.subscribe` for live runs and
:meth:`RunEventStore.list` for terminal runs; the SSE wire format is
identical (decision A: SSE id ``"{created_at_ms}-{seq}"``).

Tenant scoping rides on the RLS policy walking ``run_event → agent_run
→ tenant_id``; the API never passes ``tenant_id`` here.
"""

from __future__ import annotations

import abc
from collections.abc import Collection, Sequence
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

    async def append_batch(self, records: Sequence[RunEventRecord]) -> None:
        """Persist ``records`` (possibly spanning several runs) in one call.

        Default implementation loops :meth:`append` — correct but not
        atomic. Backends that can commit multiple rows in a single
        transaction (see :class:`SqlRunEventStore`) SHOULD override for a
        true single round-trip. ``seq`` is allocated by the producer
        (``run_agent``'s in-process counter), so unlike ``event_log``'s
        ``put_batch`` this needs no advisory lock or in-batch seq
        allocation.

        Any record colliding on ``(run_id, seq)`` raises — the durable
        primary key (SQL) or the in-memory dedup check in :meth:`append`.
        On :class:`SqlRunEventStore` the failure rolls back the WHOLE
        batch (single transaction). The caller (二期 PR3's background
        persist writer, Mini-ADR H-7) swallows the exception, logs, and
        bumps ``expert_work_run_event_persist_errors_total`` for every
        record in the batch — it never blocks the live SSE stream.
        """
        for record in records:
            await self.append(record)

    @abc.abstractmethod
    async def list(
        self,
        *,
        run_id: UUID,
        since_seq: int | None = None,
        limit: int = 100,
        event_names: Collection[str] | None = None,
    ) -> Sequence[RunEventRecord]:
        """Return frames for ``run_id``, oldest first; ``limit`` clamped
        to :data:`MAX_LIST_LIMIT`.

        Semantics (matches SSE ``Last-Event-ID``):

        * ``since_seq is None`` → from the beginning of the stream.
        * ``since_seq=N`` → events with ``seq > N`` (exclusive — the
          caller has already processed up to seq N).

        ``event_names`` narrows to those ``event_name`` values:

        * ``None`` (default) → every frame, i.e. replay's behaviour.
        * a non-empty collection → only frames whose name is in it.
        * an **empty** collection → no rows, never "all rows" — same rule
          as ``RunStore.list_for_tenant``'s ``thread_ids``. Silently
          dropping an empty predicate would widen a caller's query to the
          whole stream.

        The filter is applied BEFORE ``limit``, so a caller asking for a
        few frame kinds gets that many of *those* frames, not whatever
        survives a page of everything. The conversation-history endpoint
        (``external_session_items.py``) needs only ``plan`` / ``approval``
        / ``error`` per run; without this it would read a whole run's
        stream (up to :data:`MAX_LIST_LIMIT` rows) per turn just to keep
        a handful.

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

    async def append_batch(self, records: Sequence[RunEventRecord]) -> None:
        """Append every record in ``records``, one at a time.

        Reuses :meth:`append`'s per-record ``(run_id, seq)`` dedup check —
        a colliding record raises ``ValueError`` and leaves already-appended
        records from earlier in the batch in place (no rollback; only the
        SQL backend's single transaction is truly atomic).
        """
        for record in records:
            await self.append(record)

    async def list(
        self,
        *,
        run_id: UUID,
        since_seq: int | None = None,
        limit: int = 100,
        event_names: Collection[str] | None = None,
    ) -> Sequence[RunEventRecord]:
        # 谓词必须与 :class:`SqlRunEventStore` 逐条同义 —— 本仓库有过
        # 「SQL 与内存 store 谓词分歧」的教训,两边的顺序也一并对齐:
        # 空集合短路 → seq 过滤 → 名字过滤 → 按 seq 升序 → 截断。
        if event_names is not None and not event_names:
            return []
        clamped = _clamp_limit(limit)
        rows = self._events.get(run_id, [])
        if since_seq is None:
            filtered = list(rows)
        else:
            filtered = [r for r in rows if r.seq > since_seq]
        if event_names is not None:
            wanted = set(event_names)
            filtered = [r for r in filtered if r.event_name in wanted]
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

    async def append_batch(self, records: Sequence[RunEventRecord]) -> None:
        """One session + ``add_all`` + single commit — see ``event_log/db.py``'s
        ``put_batch`` for the same pattern. No advisory lock / in-batch seq
        allocation needed here (unlike ``put_batch``): ``seq`` already comes
        pre-assigned from the producer. A duplicate ``(run_id, seq)`` raises
        ``IntegrityError`` on commit and rolls back the whole batch.
        """
        if not records:
            return
        async with self._sf() as session:
            async with session.begin():
                session.add_all(
                    [
                        RunEventRow(
                            run_id=r.run_id,
                            seq=r.seq,
                            event_name=r.event_name,
                            data=r.data,
                            created_at_ms=r.created_at_ms,
                            created_at=r.created_at,
                        )
                        for r in records
                    ]
                )

    async def list(
        self,
        *,
        run_id: UUID,
        since_seq: int | None = None,
        limit: int = 100,
        event_names: Collection[str] | None = None,
    ) -> Sequence[RunEventRecord]:
        # 谓词与 :class:`InMemoryRunEventStore.list` 逐条同义 —— 见那边的注释。
        if event_names is not None and not event_names:
            return []
        clamped = _clamp_limit(limit)
        stmt = select(RunEventRow).where(RunEventRow.run_id == run_id)
        if since_seq is not None:
            stmt = stmt.where(RunEventRow.seq > since_seq)
        if event_names is not None:
            stmt = stmt.where(RunEventRow.event_name.in_(list(event_names)))
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
    ``time.time() * 1000``, the same clock ``InMemoryStreamBridge.publish``
    stamps the live frame id with.
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
