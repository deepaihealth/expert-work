"""SQLAlchemy-backed ``ThreadMessageStore`` (Postgres / asyncpg).

Content search rides the ``gin_trgm_ops`` index from migration 0106; the
sweep's work-queue selection joins ``thread_meta`` / ``agent_run`` (same
cross-model precedent as ``ThreadMetaStore``'s ``nonempty``).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import exists, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.like_escape import like_contains
from expert_work.persistence.models.agent_run import AgentRunRow
from expert_work.persistence.models.thread_message import ThreadMessageRow, ThreadMessageSyncRow
from expert_work.persistence.models.thread_meta import ThreadMetaRow
from expert_work.persistence.thread_message.base import MessageTurn, ThreadMessageStore


class SqlThreadMessageStore(ThreadMessageStore):
    """Postgres-backed transcript mirror."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def sync_thread(
        self,
        *,
        thread_id: UUID,
        tenant_id: UUID,
        turns: Sequence[MessageTurn],
        synced_at: datetime,
    ) -> None:
        async with self._sf() as session:
            if turns:
                stmt = pg_insert(ThreadMessageRow).values(
                    [
                        {
                            "thread_id": thread_id,
                            "seq": t.seq,
                            "tenant_id": tenant_id,
                            "role": t.role,
                            "content": t.content,
                        }
                        for t in turns
                    ]
                )
                await session.execute(
                    stmt.on_conflict_do_nothing(index_elements=["thread_id", "seq"])
                )
            mark = pg_insert(ThreadMessageSyncRow).values(
                thread_id=thread_id,
                tenant_id=tenant_id,
                synced_at=synced_at,
                message_count=len(turns),
            )
            await session.execute(
                mark.on_conflict_do_update(
                    index_elements=["thread_id"],
                    set_={"synced_at": synced_at, "message_count": len(turns)},
                )
            )
            await session.commit()

    async def mark_superseded(
        self,
        *,
        thread_id: UUID,
        tenant_id: UUID,
        seq_from: int,
        seq_to: int,
        superseded_by: UUID,
    ) -> int:
        async with self._sf() as session:
            result = await session.execute(
                update(ThreadMessageRow)
                .where(
                    ThreadMessageRow.thread_id == thread_id,
                    ThreadMessageRow.tenant_id == tenant_id,
                    ThreadMessageRow.seq >= seq_from,
                    ThreadMessageRow.seq < seq_to,
                )
                .values({"superseded_by": superseded_by})
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def search_thread_ids(
        self,
        *,
        tenant_id: UUID | None,
        q: str,
        limit: int = 500,
    ) -> set[UUID]:
        stmt = (
            select(ThreadMessageRow.thread_id)
            .where(ThreadMessageRow.content.ilike(like_contains(q), escape="\\"))
            .distinct()
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(ThreadMessageRow.tenant_id == tenant_id)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return set(rows)

    async def pending_thread_ids(self, *, limit: int) -> list[tuple[UUID, UUID]]:
        sync = ThreadMessageSyncRow
        never_synced = sync.thread_id.is_(None) & exists().where(
            AgentRunRow.thread_id == ThreadMetaRow.thread_id
        )
        new_activity = exists().where(
            AgentRunRow.thread_id == ThreadMetaRow.thread_id,
            AgentRunRow.updated_at > sync.synced_at,
        )
        stmt = (
            select(ThreadMetaRow.thread_id, ThreadMetaRow.tenant_id)
            .outerjoin(sync, sync.thread_id == ThreadMetaRow.thread_id)
            .where(never_synced | new_activity)
            # Fresh activity first so a large backfill can't starve the
            # "search a conversation from a minute ago" path.
            .order_by(sync.thread_id.is_(None).asc())
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        return [(r[0], r[1]) for r in rows]
