"""Knowledge retrieval log — B-175 P2-PR4.

One row per retrieval: an agent's ``knowledge_search``, a console retrieval
test, or (later) an open-API search. It records which bases were searched, the
query (cut to :data:`RETRIEVAL_LOG_QUERY_MAX_CHARS`), the parameters, how the
search ended, each result's ids and scores, the degradation flags, the latency,
and the run / conversation (thread) / actor behind it. **Never document text**:
a result is ids and scores, resolved to documents when read.

A row that searched several bases is listed under each of them, and its
``result_count`` / results span all of them.

Rows are read per base by employees (the console's ``session:read`` gate, like
conversation records) and go away with the base, with a purged conversation
(its thread id) or user (their runs, threads and console tests), and past the
retention window (the retention-cleanup job,
:meth:`KnowledgeRetrievalLogStore.delete_older_than`).
"""

from __future__ import annotations

import abc
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, get_args
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from expert_work.persistence.models import KnowledgeRetrievalLogRow

#: Who searched — the table's CHECK constraint lists the same three.
RetrievalLogSource = Literal["agent", "console_test", "open_api"]
RETRIEVAL_LOG_SOURCES: tuple[RetrievalLogSource, ...] = get_args(RetrievalLogSource)
#: Longest query kept (the column's CHECK); a longer one is stored cut.
RETRIEVAL_LOG_QUERY_MAX_CHARS = 2000
#: ``status`` of a search that raised (no results, not "found nothing").
RETRIEVAL_LOG_ERROR = "error"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RetrievalLogEntry:
    """One logged retrieval. ``results`` are JSON objects of ids and scores
    (``chunk_id``, ``document_id``, the four scores …), best first.
    ``run_id`` / ``thread_id`` are the agent run and its conversation;
    ``actor_id`` the employee behind a console test."""

    tenant_id: UUID
    kb_ids: tuple[UUID, ...]
    source: RetrievalLogSource
    query: str
    status: str
    latency_ms: int
    params: Mapping[str, Any] = field(default_factory=dict)
    results: tuple[Mapping[str, Any], ...] = ()
    flags: Mapping[str, Any] = field(default_factory=dict)
    run_id: UUID | None = None
    thread_id: UUID | None = None
    actor_id: str | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if self.source not in RETRIEVAL_LOG_SOURCES:
            msg = f"unknown retrieval log source: {self.source!r}"
            raise ValueError(msg)

    @property
    def result_count(self) -> int:
        return len(self.results)


def _stored(entry: RetrievalLogEntry) -> RetrievalLogEntry:
    """``entry`` as both backends store it: the query cut to the cap."""
    if len(entry.query) <= RETRIEVAL_LOG_QUERY_MAX_CHARS:
        return entry
    return replace(entry, query=entry.query[:RETRIEVAL_LOG_QUERY_MAX_CHARS])


class KnowledgeRetrievalLogStore(abc.ABC):
    """Write and read retrieval log rows. Every read and per-tenant delete is
    scoped to ``tenant_id``; :meth:`delete_older_than` is the one cross-tenant
    call (the retention job, under an RLS bypass)."""

    @abc.abstractmethod
    async def add(self, entry: RetrievalLogEntry) -> None:
        """Write one row (the query cut to :data:`RETRIEVAL_LOG_QUERY_MAX_CHARS`)."""

    @abc.abstractmethod
    async def get(self, *, tenant_id: UUID, log_id: UUID) -> RetrievalLogEntry | None:
        """The tenant's row, or ``None``."""

    @abc.abstractmethod
    async def list_for_base(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        source: RetrievalLogSource | None = None,
        status: str | None = None,
        zero_hit: bool = False,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[RetrievalLogEntry], int]:
        """One page of the rows that searched ``kb_id``, newest first (``id``
        tiebreak), plus how many match. A row that also searched other bases
        is listed here too, with its counts over all of them. ``zero_hit``
        keeps searches that came back with no result (not ones that raised,
        status :data:`RETRIEVAL_LOG_ERROR`); ``since`` is inclusive, ``until``
        exclusive."""

    @abc.abstractmethod
    async def delete_for_base(self, *, tenant_id: UUID, kb_id: UUID) -> int:
        """Delete every row that searched ``kb_id`` (also those that spanned
        other bases too); returns how many."""

    @abc.abstractmethod
    async def delete_for_runs(self, *, tenant_id: UUID, run_ids: Collection[UUID]) -> int:
        """Delete the rows written by these runs (user purge); returns how many."""

    @abc.abstractmethod
    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Collection[UUID]) -> int:
        """Delete the rows of these conversations, whoever's run wrote them
        (conversation and user purge); returns how many."""

    @abc.abstractmethod
    async def delete_for_actor(self, *, tenant_id: UUID, actor_id: str) -> int:
        """Delete the console tests this employee ran (user purge); returns how many."""

    @abc.abstractmethod
    async def delete_older_than(self, *, before: datetime, limit: int) -> int:
        """Delete at most ``limit`` rows of any tenant created before ``before``;
        returns how many — the caller repeats until fewer than ``limit``."""


class InMemoryKnowledgeRetrievalLogStore(KnowledgeRetrievalLogStore):
    """Dict-backed store for unit tests and the in-memory app."""

    def __init__(self) -> None:
        self._rows: dict[UUID, RetrievalLogEntry] = {}

    async def add(self, entry: RetrievalLogEntry) -> None:
        self._rows[entry.id] = _stored(entry)

    async def get(self, *, tenant_id: UUID, log_id: UUID) -> RetrievalLogEntry | None:
        row = self._rows.get(log_id)
        return row if row is not None and row.tenant_id == tenant_id else None

    async def list_for_base(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        source: RetrievalLogSource | None = None,
        status: str | None = None,
        zero_hit: bool = False,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[RetrievalLogEntry], int]:
        matched = [
            row
            for row in self._rows.values()
            if row.tenant_id == tenant_id
            and kb_id in row.kb_ids
            and (source is None or row.source == source)
            and (status is None or row.status == status)
            and (not zero_hit or (row.result_count == 0 and row.status != RETRIEVAL_LOG_ERROR))
            and (since is None or row.created_at >= since)
            and (until is None or row.created_at < until)
        ]
        matched.sort(key=lambda row: (row.created_at, row.id), reverse=True)
        return matched[offset : offset + limit], len(matched)

    async def delete_for_base(self, *, tenant_id: UUID, kb_id: UUID) -> int:
        return self._delete(lambda row: row.tenant_id == tenant_id and kb_id in row.kb_ids)

    async def delete_for_runs(self, *, tenant_id: UUID, run_ids: Collection[UUID]) -> int:
        wanted = set(run_ids)
        return self._delete(lambda row: row.tenant_id == tenant_id and row.run_id in wanted)

    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Collection[UUID]) -> int:
        wanted = set(thread_ids)
        return self._delete(lambda row: row.tenant_id == tenant_id and row.thread_id in wanted)

    async def delete_for_actor(self, *, tenant_id: UUID, actor_id: str) -> int:
        return self._delete(lambda row: row.tenant_id == tenant_id and row.actor_id == actor_id)

    async def delete_older_than(self, *, before: datetime, limit: int) -> int:
        victims = [log_id for log_id, row in self._rows.items() if row.created_at < before]
        for log_id in victims[:limit]:
            del self._rows[log_id]
        return len(victims[:limit])

    def _delete(self, doomed: Callable[[RetrievalLogEntry], bool]) -> int:
        victims = [log_id for log_id, row in self._rows.items() if doomed(row)]
        for log_id in victims:
            del self._rows[log_id]
        return len(victims)


def _to_entry(row: KnowledgeRetrievalLogRow) -> RetrievalLogEntry:
    return RetrievalLogEntry(
        id=row.id,
        tenant_id=row.tenant_id,
        kb_ids=tuple(row.kb_ids),
        source=row.source,  # type: ignore[arg-type]  # the CHECK constraint's values
        query=row.query,
        status=row.status,
        latency_ms=row.latency_ms,
        params=row.params,
        results=tuple(row.results),
        flags=row.flags,
        run_id=row.run_id,
        thread_id=row.thread_id,
        actor_id=row.actor_id,
        created_at=row.created_at,
    )


def _base_filters(
    tenant_id: UUID,
    kb_id: UUID,
    *,
    source: str | None,
    status: str | None,
    zero_hit: bool,
    since: datetime | None,
    until: datetime | None,
) -> Sequence[ColumnElement[bool]]:
    row = KnowledgeRetrievalLogRow
    clauses: list[ColumnElement[bool]] = [row.tenant_id == tenant_id, row.kb_ids.contains([kb_id])]
    if source is not None:
        clauses.append(row.source == source)
    if status is not None:
        clauses.append(row.status == status)
    if zero_hit:
        clauses.append(row.result_count == 0)
        clauses.append(row.status != RETRIEVAL_LOG_ERROR)
    if since is not None:
        clauses.append(row.created_at >= since)
    if until is not None:
        clauses.append(row.created_at < until)
    return clauses


class SqlKnowledgeRetrievalLogStore(KnowledgeRetrievalLogStore):
    """Postgres store over ``knowledge_retrieval_log`` (migration ``0177``)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def add(self, entry: RetrievalLogEntry) -> None:
        stored = _stored(entry)
        async with self._sf() as session:
            session.add(
                KnowledgeRetrievalLogRow(
                    id=stored.id,
                    tenant_id=stored.tenant_id,
                    kb_ids=list(stored.kb_ids),
                    source=stored.source,
                    query=stored.query,
                    params=dict(stored.params),
                    status=stored.status,
                    result_count=stored.result_count,
                    results=[dict(result) for result in stored.results],
                    flags=dict(stored.flags),
                    latency_ms=stored.latency_ms,
                    run_id=stored.run_id,
                    thread_id=stored.thread_id,
                    actor_id=stored.actor_id,
                    created_at=stored.created_at,
                )
            )
            await session.commit()

    async def get(self, *, tenant_id: UUID, log_id: UUID) -> RetrievalLogEntry | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(KnowledgeRetrievalLogRow).where(
                        KnowledgeRetrievalLogRow.tenant_id == tenant_id,
                        KnowledgeRetrievalLogRow.id == log_id,
                    )
                )
            ).scalar_one_or_none()
        return _to_entry(row) if row is not None else None

    async def list_for_base(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        source: RetrievalLogSource | None = None,
        status: str | None = None,
        zero_hit: bool = False,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[RetrievalLogEntry], int]:
        clauses = _base_filters(
            tenant_id,
            kb_id,
            source=source,
            status=status,
            zero_hit=zero_hit,
            since=since,
            until=until,
        )
        row = KnowledgeRetrievalLogRow
        async with self._sf() as session:
            total = (
                await session.execute(select(func.count()).select_from(row).where(*clauses))
            ).scalar_one()
            rows = (
                (
                    await session.execute(
                        select(row)
                        .where(*clauses)
                        .order_by(row.created_at.desc(), row.id.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .scalars()
                .all()
            )
        return [_to_entry(r) for r in rows], int(total)

    async def delete_for_base(self, *, tenant_id: UUID, kb_id: UUID) -> int:
        return await self._delete(
            KnowledgeRetrievalLogRow.tenant_id == tenant_id,
            KnowledgeRetrievalLogRow.kb_ids.contains([kb_id]),
        )

    async def delete_for_runs(self, *, tenant_id: UUID, run_ids: Collection[UUID]) -> int:
        if not run_ids:
            return 0
        return await self._delete(
            KnowledgeRetrievalLogRow.tenant_id == tenant_id,
            KnowledgeRetrievalLogRow.run_id.in_(list(run_ids)),
        )

    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Collection[UUID]) -> int:
        if not thread_ids:
            return 0
        return await self._delete(
            KnowledgeRetrievalLogRow.tenant_id == tenant_id,
            KnowledgeRetrievalLogRow.thread_id.in_(list(thread_ids)),
        )

    async def delete_for_actor(self, *, tenant_id: UUID, actor_id: str) -> int:
        return await self._delete(
            KnowledgeRetrievalLogRow.tenant_id == tenant_id,
            KnowledgeRetrievalLogRow.actor_id == actor_id,
        )

    async def delete_older_than(self, *, before: datetime, limit: int) -> int:
        # No ORDER BY: any ``limit`` expired rows will do, and the scan can stop
        # at the first ``limit`` it finds.
        batch = (
            select(KnowledgeRetrievalLogRow.id)
            .where(KnowledgeRetrievalLogRow.created_at < before)
            .limit(limit)
        )
        return await self._delete(KnowledgeRetrievalLogRow.id.in_(batch))

    async def _delete(self, *clauses: ColumnElement[bool]) -> int:
        # Bulk deletes: no ORM objects to keep in sync in this short session.
        statement = (
            delete(KnowledgeRetrievalLogRow)
            .where(*clauses)
            .execution_options(synchronize_session=False)
        )
        async with self._sf() as session:
            result = await session.execute(statement)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)


__all__ = [
    "RETRIEVAL_LOG_ERROR",
    "RETRIEVAL_LOG_QUERY_MAX_CHARS",
    "RETRIEVAL_LOG_SOURCES",
    "InMemoryKnowledgeRetrievalLogStore",
    "KnowledgeRetrievalLogStore",
    "RetrievalLogEntry",
    "RetrievalLogSource",
    "SqlKnowledgeRetrievalLogStore",
]
