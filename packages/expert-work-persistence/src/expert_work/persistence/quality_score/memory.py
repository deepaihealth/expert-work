"""In-memory :class:`QualityScoreStore` — Stream RT-5 (RT-ADR-24)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from expert_work.persistence.quality_score.base import QualityScoreStore
from expert_work.protocol import QualityScoreRecord


class InMemoryQualityScoreStore(QualityScoreStore):
    """Dict-backed store; ``(tenant_id, run_id)`` dedup, lock-guarded."""

    def __init__(self) -> None:
        self._by_run: dict[tuple[UUID, UUID], QualityScoreRecord] = {}
        self._seq = 0
        self._lock = asyncio.Lock()

    async def insert(self, record: QualityScoreRecord) -> QualityScoreRecord:
        key = (record.tenant_id, record.run_id)
        async with self._lock:
            existing = self._by_run.get(key)
            if existing is not None:
                return existing
            self._seq += 1
            stored = record.model_copy(
                update={"id": self._seq, "observed_at": record.observed_at or datetime.now(tz=UTC)}
            )
            self._by_run[key] = stored
            return stored

    async def exists(self, *, tenant_id: UUID, run_id: UUID) -> bool:
        async with self._lock:
            return (tenant_id, run_id) in self._by_run

    async def count_since(self, *, tenant_id: UUID, since: datetime) -> int:
        async with self._lock:
            return sum(
                1
                for rec in self._by_run.values()
                if rec.tenant_id == tenant_id
                and rec.observed_at is not None
                and rec.observed_at >= since
            )

    async def list_scores(
        self,
        *,
        tenant_id: UUID,
        agent_name: str | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[QualityScoreRecord]:
        async with self._lock:
            rows = [
                rec
                for rec in self._by_run.values()
                if rec.tenant_id == tenant_id
                and (agent_name is None or rec.agent_name == agent_name)
                and (since is None or (rec.observed_at is not None and rec.observed_at >= since))
            ]
        rows.sort(key=lambda r: r.observed_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return rows[:limit]

    async def list_scores_all_tenants(
        self,
        *,
        agent_name: str | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[QualityScoreRecord]:
        # W4 — no tenant filter; observed_at DESC with id tiebreak (two stable
        # sorts) matching the SQL implementation byte-for-byte.
        async with self._lock:
            rows = [
                rec
                for rec in self._by_run.values()
                if (agent_name is None or rec.agent_name == agent_name)
                and (since is None or (rec.observed_at is not None and rec.observed_at >= since))
            ]
        rows.sort(key=lambda r: r.id or 0)
        rows.sort(key=lambda r: r.observed_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return rows[:limit]

    async def list_agents_with_scores_since(self, *, since: datetime) -> list[tuple[UUID, str]]:
        async with self._lock:
            pairs = {
                (rec.tenant_id, rec.agent_name)
                for rec in self._by_run.values()
                if rec.observed_at is not None and rec.observed_at >= since
            }
        return sorted(pairs)

    async def window_stats(
        self, *, tenant_id: UUID, agent_name: str, since: datetime, until: datetime
    ) -> tuple[int, float | None]:
        async with self._lock:
            overalls = [
                rec.overall
                for rec in self._by_run.values()
                if rec.tenant_id == tenant_id
                and rec.agent_name == agent_name
                and rec.observed_at is not None
                and since <= rec.observed_at < until
            ]
        if not overalls:
            return 0, None
        return len(overalls), sum(overalls) / len(overalls)
