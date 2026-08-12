# ============================================================
# Adapted from bytedance/deer-flow @ 813d3c94efa7fdea6aafcb4f459304db91fcaed0
# Source: backend/packages/harness/deerflow/persistence/thread_meta/sql.py
# License: MIT (see vendor LICENSE)
# Modifications:
#   - Aligned to ThreadMetaStore (expert_work.persistence.thread_meta.base)
#   - Backed by expert_work.persistence.models.ThreadMetaRow (ADR-0002 schema)
#   - tenant_id (UUID) is a required arg, no AUTO sentinel / contextvar
#   - Returns Pydantic ThreadMeta (expert-work-protocol) instead of dict
# Last sync: 2026-05-11
# ============================================================

"""SQLAlchemy-backed ``ThreadMetaStore`` (Postgres / asyncpg)."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.like_escape import like_contains
from expert_work.persistence.models import ThreadMetaRow
from expert_work.persistence.models.agent_run import AgentRunRow
from expert_work.persistence.thread_meta.base import ThreadMetaStore, ThreadOrder
from expert_work.protocol import ThreadMeta, ThreadStatus


def _row_to_meta(row: ThreadMetaRow) -> ThreadMeta:
    return ThreadMeta(
        thread_id=row.thread_id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        created_by=row.created_by,
        status=ThreadStatus(row.status),
        title=row.title,
        agent_name=row.agent_name,
        agent_version=row.agent_version,
        message_count=row.message_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _filter_clauses(
    *,
    tenant_id: UUID | None,
    status: ThreadStatus | None,
    user_id: UUID | None,
    agent_name: str | None,
    agent_version: str | None,
    nonempty: bool,
    q: str | None,
    q_thread_ids: Collection[UUID] | None,
    include_archived: bool,
    thread_ids: Collection[UUID] | None,
) -> list[ColumnElement[bool]]:
    """The shared WHERE set for list/count — one source so the pager's
    ``total`` can never drift from the page query. Callers short-circuit
    an empty ``thread_ids`` before building clauses."""
    clauses: list[ColumnElement[bool]] = []
    if tenant_id is not None:
        clauses.append(ThreadMetaRow.tenant_id == tenant_id)
    if thread_ids is not None:
        clauses.append(ThreadMetaRow.thread_id.in_(list(thread_ids)))
    if status is not None:
        clauses.append(ThreadMetaRow.status == status.value)
    elif not include_archived:
        clauses.append(ThreadMetaRow.status != ThreadStatus.ARCHIVED.value)
    if user_id is not None:
        clauses.append(ThreadMetaRow.user_id == user_id)
    if agent_name is not None:
        clauses.append(ThreadMetaRow.agent_name == agent_name)
    if agent_version is not None:
        clauses.append(ThreadMetaRow.agent_version == agent_version)
    if nonempty:
        clauses.append(exists().where(AgentRunRow.thread_id == ThreadMetaRow.thread_id))
    if q:
        title_hit = ThreadMetaRow.title.ilike(like_contains(q), escape="\\")
        # Content search (IA M4): the API resolves message-content matches
        # to a thread-id set first; a q hit is "title matches OR content
        # matched" so one search box spans both.
        if q_thread_ids:
            clauses.append(or_(title_hit, ThreadMetaRow.thread_id.in_(list(q_thread_ids))))
        else:
            clauses.append(title_hit)
    return clauses


class SqlThreadMetaStore(ThreadMetaStore):
    """Postgres-backed thread metadata repository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(
        self,
        *,
        thread_id: UUID,
        tenant_id: UUID,
        created_by: str,
        user_id: UUID | None = None,
        agent_name: str | None = None,
        agent_version: str | None = None,
    ) -> ThreadMeta:
        now = datetime.now(UTC)
        row = ThreadMetaRow(
            thread_id=thread_id,
            tenant_id=tenant_id,
            user_id=user_id,
            created_by=created_by,
            status=ThreadStatus.ACTIVE.value,
            agent_name=agent_name,
            agent_version=agent_version,
            message_count=0,
            created_at=now,
            updated_at=now,
        )
        async with self._sf() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                msg = f"thread_meta already exists for thread_id={thread_id}"
                raise ValueError(msg) from exc
            await session.refresh(row)
            return _row_to_meta(row)

    async def get(self, thread_id: UUID, *, tenant_id: UUID) -> ThreadMeta | None:
        async with self._sf() as session:
            row = await session.get(ThreadMetaRow, thread_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return _row_to_meta(row)

    async def get_many(
        self, thread_ids: Collection[UUID], *, tenant_id: UUID
    ) -> dict[UUID, ThreadMeta]:
        ids = list(dict.fromkeys(thread_ids))
        if not ids:
            return {}
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(ThreadMetaRow).where(
                            ThreadMetaRow.tenant_id == tenant_id,
                            ThreadMetaRow.thread_id.in_(ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return {row.thread_id: _row_to_meta(row) for row in rows}

    async def list_by_tenant(
        self,
        tenant_id: UUID,
        *,
        status: ThreadStatus | None = None,
        user_id: UUID | None = None,
        agent_name: str | None = None,
        agent_version: str | None = None,
        nonempty: bool = False,
        q: str | None = None,
        q_thread_ids: Collection[UUID] | None = None,
        include_archived: bool = False,
        thread_ids: Collection[UUID] | None = None,
        order_by: ThreadOrder = "created_at",
        limit: int = 100,
        offset: int = 0,
    ) -> list[ThreadMeta]:
        if thread_ids is not None and not thread_ids:
            return []
        return await self._list(
            tenant_id=tenant_id,
            status=status,
            user_id=user_id,
            agent_name=agent_name,
            agent_version=agent_version,
            nonempty=nonempty,
            q=q,
            q_thread_ids=q_thread_ids,
            include_archived=include_archived,
            thread_ids=thread_ids,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )

    async def list_all_tenants(
        self,
        *,
        status: ThreadStatus | None = None,
        user_id: UUID | None = None,
        agent_name: str | None = None,
        agent_version: str | None = None,
        nonempty: bool = False,
        q: str | None = None,
        q_thread_ids: Collection[UUID] | None = None,
        include_archived: bool = False,
        thread_ids: Collection[UUID] | None = None,
        order_by: ThreadOrder = "created_at",
        limit: int = 100,
        offset: int = 0,
    ) -> list[ThreadMeta]:
        # Stream N — no tenant filter; caller must wrap in bypass_rls_session().
        if thread_ids is not None and not thread_ids:
            return []
        return await self._list(
            tenant_id=None,
            status=status,
            user_id=user_id,
            agent_name=agent_name,
            agent_version=agent_version,
            nonempty=nonempty,
            q=q,
            q_thread_ids=q_thread_ids,
            include_archived=include_archived,
            thread_ids=thread_ids,
            order_by=order_by,
            limit=limit,
            offset=offset,
        )

    async def _list(
        self,
        *,
        tenant_id: UUID | None,
        status: ThreadStatus | None,
        user_id: UUID | None,
        agent_name: str | None,
        agent_version: str | None,
        nonempty: bool,
        q: str | None,
        q_thread_ids: Collection[UUID] | None,
        include_archived: bool,
        thread_ids: Collection[UUID] | None,
        order_by: ThreadOrder,
        limit: int,
        offset: int,
    ) -> list[ThreadMeta]:
        stmt = select(ThreadMetaRow).where(
            *_filter_clauses(
                tenant_id=tenant_id,
                status=status,
                user_id=user_id,
                agent_name=agent_name,
                agent_version=agent_version,
                nonempty=nonempty,
                q=q,
                q_thread_ids=q_thread_ids,
                include_archived=include_archived,
                thread_ids=thread_ids,
            )
        )
        if order_by == "last_activity":
            # Newest-run-first: join each thread's max(run.created_at);
            # run-less threads fall back to their creation time.
            last_run = (
                select(
                    AgentRunRow.thread_id.label("thread_id"),
                    func.max(AgentRunRow.created_at).label("last_run_at"),
                )
                .group_by(AgentRunRow.thread_id)
                .subquery()
            )
            stmt = stmt.outerjoin(
                last_run, last_run.c.thread_id == ThreadMetaRow.thread_id
            ).order_by(func.coalesce(last_run.c.last_run_at, ThreadMetaRow.created_at).desc())
        else:
            stmt = stmt.order_by(ThreadMetaRow.created_at.desc())
        stmt = stmt.limit(limit).offset(offset)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_meta(r) for r in rows]

    async def count_by_tenant(
        self,
        tenant_id: UUID,
        *,
        status: ThreadStatus | None = None,
        user_id: UUID | None = None,
        agent_name: str | None = None,
        agent_version: str | None = None,
        nonempty: bool = False,
        q: str | None = None,
        q_thread_ids: Collection[UUID] | None = None,
        include_archived: bool = False,
        thread_ids: Collection[UUID] | None = None,
    ) -> int:
        if thread_ids is not None and not thread_ids:
            return 0
        return await self._count(
            tenant_id=tenant_id,
            status=status,
            user_id=user_id,
            agent_name=agent_name,
            agent_version=agent_version,
            nonempty=nonempty,
            q=q,
            q_thread_ids=q_thread_ids,
            include_archived=include_archived,
            thread_ids=thread_ids,
        )

    async def count_all_tenants(
        self,
        *,
        status: ThreadStatus | None = None,
        user_id: UUID | None = None,
        agent_name: str | None = None,
        agent_version: str | None = None,
        nonempty: bool = False,
        q: str | None = None,
        q_thread_ids: Collection[UUID] | None = None,
        include_archived: bool = False,
        thread_ids: Collection[UUID] | None = None,
    ) -> int:
        # Stream N — caller must wrap in bypass_rls_session().
        if thread_ids is not None and not thread_ids:
            return 0
        return await self._count(
            tenant_id=None,
            status=status,
            user_id=user_id,
            agent_name=agent_name,
            agent_version=agent_version,
            nonempty=nonempty,
            q=q,
            q_thread_ids=q_thread_ids,
            include_archived=include_archived,
            thread_ids=thread_ids,
        )

    async def _count(
        self,
        *,
        tenant_id: UUID | None,
        status: ThreadStatus | None,
        user_id: UUID | None,
        agent_name: str | None,
        agent_version: str | None,
        nonempty: bool,
        q: str | None,
        q_thread_ids: Collection[UUID] | None,
        include_archived: bool,
        thread_ids: Collection[UUID] | None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(ThreadMetaRow)
            .where(
                *_filter_clauses(
                    tenant_id=tenant_id,
                    status=status,
                    user_id=user_id,
                    agent_name=agent_name,
                    agent_version=agent_version,
                    nonempty=nonempty,
                    q=q,
                    q_thread_ids=q_thread_ids,
                    include_archived=include_archived,
                    thread_ids=thread_ids,
                )
            )
        )
        async with self._sf() as session:
            return int((await session.execute(stmt)).scalar_one())

    async def update_status(
        self,
        thread_id: UUID,
        status: ThreadStatus,
        *,
        tenant_id: UUID,
    ) -> bool:
        stmt = (
            update(ThreadMetaRow)
            .where(
                ThreadMetaRow.thread_id == thread_id,
                ThreadMetaRow.tenant_id == tenant_id,
            )
            .values(status=status.value, updated_at=datetime.now(UTC))
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) > 0

    async def update_title(
        self,
        thread_id: UUID,
        title: str,
        *,
        tenant_id: UUID,
    ) -> bool:
        stmt = (
            update(ThreadMetaRow)
            .where(
                ThreadMetaRow.thread_id == thread_id,
                ThreadMetaRow.tenant_id == tenant_id,
            )
            .values(title=title, updated_at=datetime.now(UTC))
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) > 0

    async def update_message_count(
        self,
        thread_id: UUID,
        count: int,
        *,
        tenant_id: UUID,
    ) -> bool:
        stmt = (
            update(ThreadMetaRow)
            .where(
                ThreadMetaRow.thread_id == thread_id,
                ThreadMetaRow.tenant_id == tenant_id,
            )
            .values(message_count=count, updated_at=datetime.now(UTC))
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) > 0

    async def check_access(self, thread_id: UUID, tenant_id: UUID) -> bool:
        async with self._sf() as session:
            row = await session.get(ThreadMetaRow, thread_id)
            return row is not None and row.tenant_id == tenant_id

    async def delete(self, thread_id: UUID, *, tenant_id: UUID) -> bool:
        stmt = delete(ThreadMetaRow).where(
            ThreadMetaRow.thread_id == thread_id,
            ThreadMetaRow.tenant_id == tenant_id,
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0) > 0
