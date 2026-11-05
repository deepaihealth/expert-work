"""SQLAlchemy-backed :class:`AgentInstanceStore` (Postgres / asyncpg)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.agent_instance.base import AgentInstanceStore
from expert_work.persistence.bulk_delete import count_where, delete_where
from expert_work.persistence.models import AgentInstanceRow
from expert_work.protocol import AgentInstanceRecord


def _row_to_record(row: AgentInstanceRow) -> AgentInstanceRecord:
    return AgentInstanceRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_code=row.agent_code,
        user_id=row.user_id,
        created_at=row.created_at,
        last_active_at=row.last_active_at,
    )


class SqlAgentInstanceStore(AgentInstanceStore):
    """Postgres-backed per-user agent-instance bindings (tenant-scoped RLS)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def touch(
        self, *, tenant_id: UUID, agent_code: str, user_id: UUID
    ) -> AgentInstanceRecord:
        now = datetime.now(UTC)
        stmt = (
            pg_insert(AgentInstanceRow)
            .values(
                tenant_id=tenant_id,
                agent_code=agent_code,
                user_id=user_id,
                created_at=now,
                last_active_at=now,
            )
            .on_conflict_do_update(
                constraint="agent_instance_identity_uniq",
                set_={"last_active_at": now},
            )
            .returning(AgentInstanceRow)
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
            return _row_to_record(row)

    async def get(
        self, *, tenant_id: UUID, agent_code: str, user_id: UUID
    ) -> AgentInstanceRecord | None:
        stmt = select(AgentInstanceRow).where(
            AgentInstanceRow.tenant_id == tenant_id,
            AgentInstanceRow.agent_code == agent_code,
            AgentInstanceRow.user_id == user_id,
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _row_to_record(row) if row is not None else None

    async def list_by_agent(
        self, *, tenant_id: UUID, agent_code: str, limit: int = 100, offset: int = 0
    ) -> list[AgentInstanceRecord]:
        stmt = (
            select(AgentInstanceRow)
            .where(
                AgentInstanceRow.tenant_id == tenant_id,
                AgentInstanceRow.agent_code == agent_code,
            )
            .order_by(AgentInstanceRow.last_active_at.desc())
            .limit(limit)
            .offset(offset)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(r) for r in rows]

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        stmt = delete(AgentInstanceRow).where(
            AgentInstanceRow.tenant_id == tenant_id,
            AgentInstanceRow.user_id == user_id,
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def list_by_user(
        self, *, tenant_id: UUID, user_id: UUID, limit: int = 100, offset: int = 0
    ) -> list[AgentInstanceRecord]:
        stmt = (
            select(AgentInstanceRow)
            .where(
                AgentInstanceRow.tenant_id == tenant_id,
                AgentInstanceRow.user_id == user_id,
            )
            .order_by(AgentInstanceRow.last_active_at.desc())
            .limit(limit)
            .offset(offset)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(r) for r in rows]

    async def count_for_agent(self, *, tenant_id: UUID, agent_code: str) -> int:
        return await count_where(
            self._sf,
            AgentInstanceRow,
            AgentInstanceRow.tenant_id == tenant_id,
            AgentInstanceRow.agent_code == agent_code,
        )

    async def delete_for_agent(self, *, tenant_id: UUID, agent_code: str) -> int:
        return await delete_where(
            self._sf,
            AgentInstanceRow,
            AgentInstanceRow.tenant_id == tenant_id,
            AgentInstanceRow.agent_code == agent_code,
        )
