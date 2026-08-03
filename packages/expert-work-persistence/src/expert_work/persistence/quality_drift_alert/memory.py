"""In-memory :class:`QualityDriftAlertStore` — Stream RT-5 (RT-ADR-24)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from expert_work.persistence.quality_drift_alert.base import QualityDriftAlertStore
from expert_work.protocol import QualityDriftAlertRecord


class InMemoryQualityDriftAlertStore(QualityDriftAlertStore):
    """List-backed append-only alert history; lock-guarded."""

    def __init__(self) -> None:
        self._alerts: list[QualityDriftAlertRecord] = []
        self._seq = 0
        self._lock = asyncio.Lock()

    async def insert(self, record: QualityDriftAlertRecord) -> QualityDriftAlertRecord:
        async with self._lock:
            self._seq += 1
            stored = record.model_copy(
                update={
                    "id": self._seq,
                    "detected_at": record.detected_at or datetime.now(tz=UTC),
                }
            )
            self._alerts.append(stored)
            return stored

    async def latest_alert_at(self, *, tenant_id: UUID, agent_name: str) -> datetime | None:
        async with self._lock:
            times = [
                rec.detected_at
                for rec in self._alerts
                if rec.tenant_id == tenant_id
                and rec.agent_name == agent_name
                and rec.detected_at is not None
            ]
        return max(times) if times else None

    async def list_alerts(
        self,
        *,
        tenant_id: UUID,
        agent_name: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[QualityDriftAlertRecord]:
        async with self._lock:
            rows = [
                rec
                for rec in self._alerts
                if rec.tenant_id == tenant_id
                and (agent_name is None or rec.agent_name == agent_name)
                and (since is None or (rec.detected_at is not None and rec.detected_at >= since))
            ]
        rows.sort(key=lambda r: r.detected_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return rows[:limit]

    async def list_alerts_all_tenants(
        self,
        *,
        agent_name: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[QualityDriftAlertRecord]:
        # W4 — no tenant filter; detected_at DESC with id tiebreak (two stable
        # sorts) matching the SQL implementation byte-for-byte.
        async with self._lock:
            rows = [
                rec
                for rec in self._alerts
                if (agent_name is None or rec.agent_name == agent_name)
                and (since is None or (rec.detected_at is not None and rec.detected_at >= since))
            ]
        rows.sort(key=lambda r: r.id or 0)
        rows.sort(key=lambda r: r.detected_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return rows[:limit]
