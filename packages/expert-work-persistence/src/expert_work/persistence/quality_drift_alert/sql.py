"""SQLAlchemy-backed :class:`QualityDriftAlertStore` — Stream RT-5 (RT-ADR-24)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import QualityDriftAlertRow
from expert_work.persistence.quality_drift_alert.base import QualityDriftAlertStore
from expert_work.protocol import QualityDriftAlertRecord


def _row_to_record(row: QualityDriftAlertRow) -> QualityDriftAlertRecord:
    return QualityDriftAlertRecord(
        tenant_id=row.tenant_id,
        agent_name=row.agent_name,
        recent_mean=row.recent_mean,
        baseline_mean=row.baseline_mean,
        drift_pct=row.drift_pct,
        recent_count=row.recent_count,
        baseline_count=row.baseline_count,
        detected_at=row.detected_at,
        id=row.id,
    )


class SqlQualityDriftAlertStore(QualityDriftAlertStore):
    """Postgres-backed ``quality_drift_alert`` repository (per-tenant RLS)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def insert(self, record: QualityDriftAlertRecord) -> QualityDriftAlertRecord:
        row = QualityDriftAlertRow(
            tenant_id=record.tenant_id,
            agent_name=record.agent_name,
            recent_mean=record.recent_mean,
            baseline_mean=record.baseline_mean,
            drift_pct=record.drift_pct,
            recent_count=record.recent_count,
            baseline_count=record.baseline_count,
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _row_to_record(row)

    async def latest_alert_at(self, *, tenant_id: UUID, agent_name: str) -> datetime | None:
        stmt = select(func.max(QualityDriftAlertRow.detected_at)).where(
            QualityDriftAlertRow.tenant_id == tenant_id,
            QualityDriftAlertRow.agent_name == agent_name,
        )
        async with self._sf() as session:
            return (await session.execute(stmt)).scalar_one_or_none()

    async def list_alerts(
        self,
        *,
        tenant_id: UUID,
        agent_name: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[QualityDriftAlertRecord]:
        stmt = select(QualityDriftAlertRow).where(QualityDriftAlertRow.tenant_id == tenant_id)
        if agent_name is not None:
            stmt = stmt.where(QualityDriftAlertRow.agent_name == agent_name)
        if since is not None:
            stmt = stmt.where(QualityDriftAlertRow.detected_at >= since)
        stmt = stmt.order_by(QualityDriftAlertRow.detected_at.desc()).limit(limit)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(row) for row in rows]

    async def list_alerts_all_tenants(
        self,
        *,
        agent_name: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[QualityDriftAlertRecord]:
        # W4 — no tenant filter; caller must wrap in bypass_rls_session().
        stmt = select(QualityDriftAlertRow)
        if agent_name is not None:
            stmt = stmt.where(QualityDriftAlertRow.agent_name == agent_name)
        if since is not None:
            stmt = stmt.where(QualityDriftAlertRow.detected_at >= since)
        stmt = stmt.order_by(
            QualityDriftAlertRow.detected_at.desc(), QualityDriftAlertRow.id
        ).limit(limit)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(row) for row in rows]
