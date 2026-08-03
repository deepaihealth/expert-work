"""SQLAlchemy-backed :class:`QualityScoreStore` — Stream RT-5 (RT-ADR-24)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import QualityScoreRow
from expert_work.persistence.quality_score.base import QualityScoreStore
from expert_work.protocol import QualityScoreRecord


def _row_to_record(row: QualityScoreRow) -> QualityScoreRecord:
    return QualityScoreRecord(
        tenant_id=row.tenant_id,
        agent_name=row.agent_name,
        agent_version=row.agent_version,
        run_id=row.run_id,
        thread_id=row.thread_id,
        overall=row.overall,
        dimensions=dict(row.dimensions),
        rationale=row.rationale,
        judge_model=row.judge_model,
        observed_at=row.observed_at,
        id=row.id,
    )


class SqlQualityScoreStore(QualityScoreStore):
    """Postgres-backed ``quality_score`` repository (per-tenant RLS)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def insert(self, record: QualityScoreRecord) -> QualityScoreRecord:
        stmt = (
            pg_insert(QualityScoreRow)
            .values(
                tenant_id=record.tenant_id,
                agent_name=record.agent_name,
                agent_version=record.agent_version,
                run_id=record.run_id,
                thread_id=record.thread_id,
                overall=record.overall,
                dimensions=record.dimensions,
                rationale=record.rationale,
                judge_model=record.judge_model,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "run_id"])
            .returning(QualityScoreRow)
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                # Conflict — the run was already judged. Return the stored row.
                existing = (
                    await session.execute(
                        select(QualityScoreRow).where(
                            QualityScoreRow.tenant_id == record.tenant_id,
                            QualityScoreRow.run_id == record.run_id,
                        )
                    )
                ).scalar_one()
                await session.commit()
                return _row_to_record(existing)
            await session.commit()
            await session.refresh(row)
            return _row_to_record(row)

    async def exists(self, *, tenant_id: UUID, run_id: UUID) -> bool:
        stmt = select(func.count()).where(
            QualityScoreRow.tenant_id == tenant_id,
            QualityScoreRow.run_id == run_id,
        )
        async with self._sf() as session:
            return int((await session.execute(stmt)).scalar_one()) > 0

    async def count_since(self, *, tenant_id: UUID, since: datetime) -> int:
        stmt = select(func.count()).where(
            QualityScoreRow.tenant_id == tenant_id,
            QualityScoreRow.observed_at >= since,
        )
        async with self._sf() as session:
            return int((await session.execute(stmt)).scalar_one())

    async def list_scores(
        self,
        *,
        tenant_id: UUID,
        agent_name: str | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[QualityScoreRecord]:
        stmt = select(QualityScoreRow).where(QualityScoreRow.tenant_id == tenant_id)
        if agent_name is not None:
            stmt = stmt.where(QualityScoreRow.agent_name == agent_name)
        if since is not None:
            stmt = stmt.where(QualityScoreRow.observed_at >= since)
        # id tiebreak — same ordering contract as the all-tenants sibling.
        stmt = stmt.order_by(QualityScoreRow.observed_at.desc(), QualityScoreRow.id).limit(limit)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(row) for row in rows]

    async def list_scores_all_tenants(
        self,
        *,
        agent_name: str | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[QualityScoreRecord]:
        # W4 — no tenant filter; caller must wrap in bypass_rls_session().
        stmt = select(QualityScoreRow)
        if agent_name is not None:
            stmt = stmt.where(QualityScoreRow.agent_name == agent_name)
        if since is not None:
            stmt = stmt.where(QualityScoreRow.observed_at >= since)
        stmt = stmt.order_by(QualityScoreRow.observed_at.desc(), QualityScoreRow.id).limit(limit)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(row) for row in rows]

    async def list_agents_with_scores_since(self, *, since: datetime) -> list[tuple[UUID, str]]:
        stmt = (
            select(QualityScoreRow.tenant_id, QualityScoreRow.agent_name)
            .where(QualityScoreRow.observed_at >= since)
            .distinct()
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        return [(row.tenant_id, row.agent_name) for row in rows]

    async def window_stats(
        self, *, tenant_id: UUID, agent_name: str, since: datetime, until: datetime
    ) -> tuple[int, float | None]:
        stmt = select(func.count(), func.avg(QualityScoreRow.overall)).where(
            QualityScoreRow.tenant_id == tenant_id,
            QualityScoreRow.agent_name == agent_name,
            QualityScoreRow.observed_at >= since,
            QualityScoreRow.observed_at < until,
        )
        async with self._sf() as session:
            count, mean = (await session.execute(stmt)).one()
        return int(count), (float(mean) if mean is not None else None)
