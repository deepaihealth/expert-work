"""Feedback store — Stream G.6.

Persists user 👍/👎 feedback on agent sessions. Two implementations
behind one ABC, matching the audit-log store pattern:

* :class:`InMemoryFeedbackStore` — dev / unit tests (no durability).
* :class:`DbFeedbackStore` — Postgres-backed; RLS-scoped via the
  sessionmaker wrapper (the ``app.tenant_id`` GUC is set after BEGIN,
  so the ``feedback`` table's tenant-isolation policy applies).
"""

from __future__ import annotations

import abc
import itertools
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models.feedback import FeedbackRow

#: Stream HX-2 — cross-tenant scan role (ledger/audit precedent).
#: ``SET LOCAL`` lifts on commit/rollback; the role is SELECT-only.
_SET_AUDIT_READER_ROLE = text("SET LOCAL ROLE audit_reader")


@dataclass(frozen=True)
class FeedbackRecord:
    """One feedback row. ``id`` / ``created_at`` are ``None`` pre-insert."""

    tenant_id: UUID
    thread_id: UUID
    rating: str
    actor_id: str
    turn_seq: int | None = None
    trace_id: str | None = None
    comment: str | None = None
    id: int | None = None
    created_at: datetime | None = None
    #: Stream HX-2 (Mini-ADR HX-B1) -- FeedbackConsumerWorker stamp.
    processed_at: datetime | None = None
    #: P-2 — 打分对象(run);NULL = 0152 之前的历史行。
    run_id: UUID | None = None
    #: P-2 — 'console' | 'external'。
    source: str = "console"
    #: P-2 — 对接方附带的段落标签,只存不 join。
    item_id: str | None = None
    #: P-2 — 改票时间;None = 从没改过。
    updated_at: datetime | None = None


def _record_id(record: FeedbackRecord) -> int:
    return record.id or 0


class FeedbackStore(abc.ABC):
    """Append + read-by-thread for user feedback."""

    @abc.abstractmethod
    async def insert(self, record: FeedbackRecord) -> FeedbackRecord:
        """Persist one feedback row; return it with ``id`` + ``created_at`` filled."""

    @abc.abstractmethod
    async def list_for_thread(self, *, thread_id: UUID) -> list[FeedbackRecord]:
        """Return feedback for one thread, newest first.

        Tenant scoping is the caller's RLS context (the GUC / contextvar),
        not a SQL filter — so this doubles as the cross-tenant isolation
        check (test #64).
        """

    @abc.abstractmethod
    async def down_rated_threads(self, *, thread_ids: Sequence[UUID]) -> set[UUID]:
        """The subset of ``thread_ids`` that carry at least one 👎 row.

        Stream HX-2 (Mini-ADR HX-B2) — the rollback gate joins user
        disapproval into the per-version outcome window by thread.
        Tenant scoping is the caller's RLS context, same as
        :meth:`list_for_thread` (the ``feedback`` table is FORCE-RLS, so
        the caller must hold a tenant scope — an owner bypass reads zero
        rows silently).
        """

    @abc.abstractmethod
    async def list_unprocessed_down_all_tenants(self, *, limit: int) -> list[FeedbackRecord]:
        """Cross-tenant scan: 👎 rows not yet consumed, oldest first.

        Stream HX-2 (Mini-ADR HX-B1) — the FeedbackConsumerWorker's
        enumeration. ``feedback`` is FORCE-RLS, so the SQL implementation
        assumes the ``audit_reader`` BYPASSRLS role for this read (the
        ledger / audit cross-tenant precedent); the caller must be inside
        a ``bypass`` scope so no tenant GUC is emitted.
        """

    @abc.abstractmethod
    async def mark_processed(self, *, feedback_id: int, processed_at: datetime) -> bool:
        """Stamp ``processed_at`` on one row; ``False`` if missing.

        A *write* — must run under the row's own tenant RLS scope (the
        BYPASSRLS role is read-only by grant). Idempotent: re-stamping a
        processed row just overwrites the timestamp.
        """

    @abc.abstractmethod
    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        """Hard-delete every feedback row for ``tenant_id`` on any of ``thread_ids``.

        Deletion-hygiene purge (Task 8) — cascades feedback removal when
        a user's threads are purged. Scoped by both tenant and thread id
        so another tenant's row sharing the same thread id is untouched.
        An empty ``thread_ids`` deletes nothing and returns ``0``.

        A *write* — expected to run inside a tenant-scoped RLS session;
        the ``tenant_id`` argument must agree with the session's GUC
        (mismatched values delete zero rows rather than another
        tenant's data, since RLS clamps the row set before the
        explicit ``WHERE`` even applies).
        """

    @abc.abstractmethod
    async def upsert(self, record: FeedbackRecord) -> tuple[FeedbackRecord, bool]:
        """Insert-or-overwrite keyed by ``(tenant_id, run_id, actor_id)`` — P-2「可改票」.

        Returns ``(stored, updated)``: ``updated`` is ``True`` when an existing
        row was overwritten. Overwrite replaces ``rating / comment / item_id /
        trace_id / turn_seq / source`` and stamps ``updated_at``; ``id``,
        ``created_at`` and ``processed_at`` are left alone. ``record.run_id``
        must be set — raises ``ValueError`` otherwise (thread-only rows are
        append-only history, never upserted).
        """

    @abc.abstractmethod
    async def list_for_thread_scoped(
        self, *, tenant_id: UUID, thread_id: UUID
    ) -> list[FeedbackRecord]:
        """Like :meth:`list_for_thread` but with an explicit tenant predicate,
        newest (``id``) first. New read paths use this — the runtime connects
        with a BYPASSRLS role, so :meth:`list_for_thread`'s "RLS is the tenant
        filter" contract is not something P-2 read paths can lean on.
        """

    @abc.abstractmethod
    async def down_rated_thread_ids(self, *, tenant_id: UUID | None, limit: int = 500) -> set[UUID]:
        """Thread ids carrying ≥1 👎, oldest-👎-first, capped at ``limit``.

        Feeds ``GET /v1/conversations?has_down_rated`` (P-2 §6), mirroring
        ``RunStore.thread_ids_with_runs``. ``tenant_id=None`` is the
        cross-tenant aggregate: the SQL implementation assumes the
        ``audit_reader`` BYPASSRLS role for that read (same precedent as
        :meth:`list_unprocessed_down_all_tenants`); the caller must be in a
        bypass scope so no tenant GUC is emitted.
        """


class InMemoryFeedbackStore(FeedbackStore):
    """In-memory :class:`FeedbackStore` — dev / unit tests."""

    def __init__(self) -> None:
        self._rows: list[FeedbackRecord] = []
        self._ids = itertools.count(1)

    async def insert(self, record: FeedbackRecord) -> FeedbackRecord:
        stored = replace(record, id=next(self._ids), created_at=datetime.now(UTC))
        self._rows.append(stored)
        return stored

    async def list_for_thread(self, *, thread_id: UUID) -> list[FeedbackRecord]:
        rows = [r for r in self._rows if r.thread_id == thread_id]
        return sorted(rows, key=_record_id, reverse=True)

    async def down_rated_threads(self, *, thread_ids: Sequence[UUID]) -> set[UUID]:
        wanted = set(thread_ids)
        return {r.thread_id for r in self._rows if r.thread_id in wanted and r.rating == "down"}

    async def list_unprocessed_down_all_tenants(self, *, limit: int) -> list[FeedbackRecord]:
        rows = [r for r in self._rows if r.rating == "down" and r.processed_at is None]
        return sorted(rows, key=_record_id)[:limit]

    async def mark_processed(self, *, feedback_id: int, processed_at: datetime) -> bool:
        for i, r in enumerate(self._rows):
            if r.id == feedback_id:
                self._rows[i] = replace(r, processed_at=processed_at)
                return True
        return False

    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        if not thread_ids:
            return 0
        wanted = set(thread_ids)
        before = len(self._rows)
        self._rows = [
            r for r in self._rows if not (r.tenant_id == tenant_id and r.thread_id in wanted)
        ]
        return before - len(self._rows)

    async def upsert(self, record: FeedbackRecord) -> tuple[FeedbackRecord, bool]:
        if record.run_id is None:
            raise ValueError("upsert requires run_id")
        for i, r in enumerate(self._rows):
            if (
                r.tenant_id == record.tenant_id
                and r.run_id == record.run_id
                and r.actor_id == record.actor_id
            ):
                stored = replace(
                    r,
                    rating=record.rating,
                    comment=record.comment,
                    item_id=record.item_id,
                    trace_id=record.trace_id,
                    turn_seq=record.turn_seq,
                    source=record.source,
                    updated_at=datetime.now(UTC),
                )
                self._rows[i] = stored
                return stored, True
        stored = replace(record, id=next(self._ids), created_at=datetime.now(UTC), updated_at=None)
        self._rows.append(stored)
        return stored, False

    async def list_for_thread_scoped(
        self, *, tenant_id: UUID, thread_id: UUID
    ) -> list[FeedbackRecord]:
        rows = [r for r in self._rows if r.tenant_id == tenant_id and r.thread_id == thread_id]
        return sorted(rows, key=_record_id, reverse=True)

    async def down_rated_thread_ids(self, *, tenant_id: UUID | None, limit: int = 500) -> set[UUID]:
        out: list[UUID] = []
        for r in sorted(self._rows, key=_record_id):
            if r.rating != "down":
                continue
            if tenant_id is not None and r.tenant_id != tenant_id:
                continue
            if r.thread_id in out:
                continue
            out.append(r.thread_id)
            if len(out) >= limit:
                break
        return set(out)


class DbFeedbackStore(FeedbackStore):
    """Postgres-backed :class:`FeedbackStore`.

    ``session_factory`` must be the RLS-wrapped sessionmaker
    (:func:`expert_work.persistence.rls.build_rls_sessionmaker`): the
    tenant GUC is set after BEGIN, so the ``feedback`` RLS policy scopes
    every row to the calling tenant. An ``insert`` whose tenant context
    is unset fails the policy ``WITH CHECK`` — RLS, not trust.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def insert(self, record: FeedbackRecord) -> FeedbackRecord:
        async with self._sf() as session:
            row = FeedbackRow(
                tenant_id=record.tenant_id,
                thread_id=record.thread_id,
                run_id=record.run_id,
                source=record.source,
                item_id=record.item_id,
                turn_seq=record.turn_seq,
                trace_id=record.trace_id,
                rating=record.rating,
                comment=record.comment,
                actor_id=record.actor_id,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            stored = _row_to_record(row)
            await session.commit()
            return stored

    async def list_for_thread(self, *, thread_id: UUID) -> list[FeedbackRecord]:
        async with self._sf() as session:
            result = await session.execute(
                select(FeedbackRow)
                .where(FeedbackRow.thread_id == thread_id)
                .order_by(FeedbackRow.id.desc())
            )
            return [_row_to_record(row) for row in result.scalars().all()]

    async def down_rated_threads(self, *, thread_ids: Sequence[UUID]) -> set[UUID]:
        if not thread_ids:
            return set()
        async with self._sf() as session:
            result = await session.execute(
                select(FeedbackRow.thread_id)
                .where(FeedbackRow.thread_id.in_(list(thread_ids)))
                .where(FeedbackRow.rating == "down")
                .distinct()
            )
            return set(result.scalars().all())

    async def list_unprocessed_down_all_tenants(self, *, limit: int) -> list[FeedbackRecord]:
        stmt = (
            select(FeedbackRow)
            .where(FeedbackRow.rating == "down", FeedbackRow.processed_at.is_(None))
            .order_by(FeedbackRow.id.asc())
            .limit(limit)
        )
        async with self._sf() as session:
            # First statement: opens the txn AND assumes the BYPASSRLS role
            # (``SET LOCAL`` lifts on commit/rollback). Without it the
            # FORCE-RLS policy collapses to ``tenant_id = NULL`` → zero rows.
            await session.execute(_SET_AUDIT_READER_ROLE)
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(row) for row in rows]

    async def mark_processed(self, *, feedback_id: int, processed_at: datetime) -> bool:
        stmt = (
            update(FeedbackRow)
            .where(FeedbackRow.id == feedback_id)
            .values(processed_at=processed_at)
            .returning(FeedbackRow.id)
        )
        async with self._sf() as session:
            updated = (await session.execute(stmt)).scalars().all()
            await session.commit()
        return bool(updated)

    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        if not thread_ids:
            return 0
        ids = list(thread_ids)
        total = 0
        async with self._sf() as session:
            for i in range(0, len(ids), 500):
                result = await session.execute(
                    delete(FeedbackRow).where(
                        FeedbackRow.tenant_id == tenant_id,
                        FeedbackRow.thread_id.in_(ids[i : i + 500]),
                    )
                )
                total += int(getattr(result, "rowcount", 0) or 0)
            await session.commit()
        return total

    async def upsert(self, record: FeedbackRecord) -> tuple[FeedbackRecord, bool]:
        if record.run_id is None:
            raise ValueError("upsert requires run_id")
        return await self._upsert_once(record, retry=True)

    async def _upsert_once(
        self, record: FeedbackRecord, *, retry: bool
    ) -> tuple[FeedbackRecord, bool]:
        """One SELECT-then-INSERT/UPDATE attempt; ``retry`` allows exactly one more.

        Bounded on purpose. The INSERT branch loses a race only when a
        concurrent writer inserted the same ``(tenant, run, actor)`` first, and
        the retry then finds that row and takes the UPDATE branch — so one
        extra attempt is all the race needs. Retrying without a bound would
        turn an interleaved purge (DELETE between our SELECT and our INSERT)
        into unbounded recursion: each attempt would keep finding no row,
        keep inserting, and keep losing to the next writer. The second
        ``IntegrityError`` propagates instead.
        """
        async with self._sf() as session:
            existing_id = (
                await session.execute(
                    select(FeedbackRow.id).where(
                        FeedbackRow.tenant_id == record.tenant_id,
                        FeedbackRow.run_id == record.run_id,
                        FeedbackRow.actor_id == record.actor_id,
                    )
                )
            ).scalar_one_or_none()
            if existing_id is not None:
                row = (
                    await session.execute(
                        update(FeedbackRow)
                        .where(FeedbackRow.id == existing_id)
                        .values(
                            rating=record.rating,
                            comment=record.comment,
                            item_id=record.item_id,
                            trace_id=record.trace_id,
                            turn_seq=record.turn_seq,
                            source=record.source,
                            updated_at=datetime.now(UTC),
                        )
                        .returning(FeedbackRow)
                    )
                ).scalar_one()
                stored = _row_to_record(row)
                await session.commit()
                return stored, True
            row = FeedbackRow(
                tenant_id=record.tenant_id,
                thread_id=record.thread_id,
                run_id=record.run_id,
                source=record.source,
                item_id=record.item_id,
                turn_seq=record.turn_seq,
                trace_id=record.trace_id,
                rating=record.rating,
                comment=record.comment,
                actor_id=record.actor_id,
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                # Lost a concurrent-insert race on ``feedback_run_actor_uniq`` —
                # the row exists now, so the retry takes the UPDATE branch.
                await session.rollback()
                if not retry:
                    raise
                return await self._upsert_once(record, retry=False)
            await session.refresh(row)
            stored = _row_to_record(row)
            await session.commit()
            return stored, False

    async def list_for_thread_scoped(
        self, *, tenant_id: UUID, thread_id: UUID
    ) -> list[FeedbackRecord]:
        async with self._sf() as session:
            result = await session.execute(
                select(FeedbackRow)
                .where(FeedbackRow.tenant_id == tenant_id, FeedbackRow.thread_id == thread_id)
                .order_by(FeedbackRow.id.desc())
            )
            return [_row_to_record(row) for row in result.scalars().all()]

    async def down_rated_thread_ids(self, *, tenant_id: UUID | None, limit: int = 500) -> set[UUID]:
        stmt = select(FeedbackRow.thread_id).where(FeedbackRow.rating == "down")
        if tenant_id is not None:
            stmt = stmt.where(FeedbackRow.tenant_id == tenant_id)
        stmt = (
            stmt.group_by(FeedbackRow.thread_id)
            .order_by(func.min(FeedbackRow.id).asc())
            .limit(limit)
        )
        async with self._sf() as session:
            if tenant_id is None:
                await session.execute(_SET_AUDIT_READER_ROLE)
            return set((await session.execute(stmt)).scalars().all())


def _row_to_record(row: FeedbackRow) -> FeedbackRecord:
    return FeedbackRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        thread_id=row.thread_id,
        turn_seq=row.turn_seq,
        trace_id=row.trace_id,
        rating=row.rating,
        comment=row.comment,
        actor_id=row.actor_id,
        created_at=row.created_at,
        processed_at=row.processed_at,
        run_id=row.run_id,
        source=row.source,
        item_id=row.item_id,
        updated_at=row.updated_at,
    )
