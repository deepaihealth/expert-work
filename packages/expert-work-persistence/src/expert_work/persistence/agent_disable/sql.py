"""SQLAlchemy-backed :class:`AgentDisableStore` — Stream RT-4 (RT-ADR-16)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.agent_disable.base import AgentDisableStore
from expert_work.persistence.models import AgentDisableRow
from expert_work.protocol import AgentDisableRecord


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _row_to_record(row: AgentDisableRow) -> AgentDisableRecord:
    return AgentDisableRecord(
        tenant_id=row.tenant_id,
        agent_name=row.agent_name,
        disabled=row.disabled,
        reason=row.reason,
        disabled_by=row.disabled_by,
        disabled_at=row.disabled_at,
        updated_at=row.updated_at,
    )


class SqlAgentDisableStore(AgentDisableStore):
    """Postgres-backed ``agent_disable`` repository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def get(self, *, tenant_id: UUID, agent_name: str) -> AgentDisableRecord | None:
        async with self._sf() as session:
            row = await session.get(AgentDisableRow, (tenant_id, agent_name))
        return _row_to_record(row) if row is not None else None

    async def set_disabled(
        self,
        *,
        tenant_id: UUID,
        agent_name: str,
        disabled: bool,
        reason: str | None,
        disabled_by: str | None,
    ) -> AgentDisableRecord:
        now = _utc_now()
        # Enable clears the disable metadata; disable stamps it.
        disabled_at = now if disabled else None
        eff_reason = reason if disabled else None
        eff_by = disabled_by if disabled else None
        stmt = (
            pg_insert(AgentDisableRow)
            .values(
                tenant_id=tenant_id,
                agent_name=agent_name,
                disabled=disabled,
                reason=eff_reason,
                disabled_by=eff_by,
                disabled_at=disabled_at,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "agent_name"],
                set_={
                    "disabled": disabled,
                    "reason": eff_reason,
                    "disabled_by": eff_by,
                    "disabled_at": disabled_at,
                    "updated_at": now,
                },
            )
            .returning(AgentDisableRow)
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
            await session.refresh(row)
            return _row_to_record(row)

    async def list_disabled_names(self, *, tenant_id: UUID) -> set[str]:
        # 只 SELECT 名字这一列 —— 调用方只要名字集合,不需要整行。
        # ``disabled.is_(True)`` 而不是 ``== True``:enable 过的行仍在表里
        # (disabled=False),必须被过滤掉。
        stmt = select(AgentDisableRow.agent_name).where(
            AgentDisableRow.tenant_id == tenant_id,
            AgentDisableRow.disabled.is_(True),
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return set(rows)
