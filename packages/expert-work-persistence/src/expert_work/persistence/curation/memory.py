"""In-memory curation stores for unit tests — Stream J.12."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from expert_work.persistence.curation.base import CurationCandidateStore, EvalDatasetStore
from expert_work.protocol import (
    CandidateStatus,
    CurationCandidateRecord,
    CurationSignal,
    EvalDatasetRecord,
)


class InMemoryEvalDatasetStore(EvalDatasetStore):
    """In-memory ``EvalDatasetStore`` — keyed by case id."""

    def __init__(self) -> None:
        self._rows: dict[UUID, EvalDatasetRecord] = {}

    async def create(self, record: EvalDatasetRecord) -> EvalDatasetRecord:
        if record.id in self._rows:
            msg = f"eval_dataset row already exists for id {record.id}"
            raise ValueError(msg)
        self._rows[record.id] = record
        return record

    async def get(self, *, dataset_id: UUID, tenant_id: UUID) -> EvalDatasetRecord | None:
        row = self._rows.get(dataset_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def list_by_agent(self, *, tenant_id: UUID, agent_name: str) -> list[EvalDatasetRecord]:
        rows = [
            r
            for r in self._rows.values()
            if r.tenant_id == tenant_id and r.agent_name == agent_name
        ]
        rows.sort(key=lambda r: r.created_at)
        return rows

    async def list_by_tenant(self, *, tenant_id: UUID) -> list[EvalDatasetRecord]:
        rows = [r for r in self._rows.values() if r.tenant_id == tenant_id]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows

    async def list_all_tenants(self) -> list[EvalDatasetRecord]:
        # Stream N — no tenant filter.
        rows = list(self._rows.values())
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows

    async def update(self, record: EvalDatasetRecord) -> bool:
        existing = self._rows.get(record.id)
        if existing is None or existing.tenant_id != record.tenant_id:
            return False
        self._rows[record.id] = record
        return True

    async def delete(self, *, dataset_id: UUID, tenant_id: UUID) -> bool:
        row = self._rows.get(dataset_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        del self._rows[dataset_id]
        return True

    async def count_by_tenant(self, *, tenant_id: UUID) -> int:
        return sum(1 for r in self._rows.values() if r.tenant_id == tenant_id)

    async def anonymize_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        updated = 0
        for did, r in list(self._rows.items()):
            if r.tenant_id == tenant_id and r.source_user_id == user_id:
                self._rows[did] = r.model_copy(update={"source_user_id": None})
                updated += 1
        return updated


class InMemoryCurationCandidateStore(CurationCandidateStore):
    """In-memory ``CurationCandidateStore`` — keyed by candidate id."""

    def __init__(self) -> None:
        self._rows: dict[UUID, CurationCandidateRecord] = {}

    async def upsert(self, record: CurationCandidateRecord) -> bool:
        for existing in self._rows.values():
            if (
                existing.tenant_id == record.tenant_id
                and existing.trajectory_key == record.trajectory_key
            ):
                return False
        self._rows[record.id] = record
        return True

    async def get(self, *, candidate_id: UUID, tenant_id: UUID) -> CurationCandidateRecord | None:
        row = self._rows.get(candidate_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def get_by_trajectory_key(
        self, *, tenant_id: UUID, trajectory_key: str
    ) -> CurationCandidateRecord | None:
        for row in self._rows.values():
            if row.tenant_id == tenant_id and row.trajectory_key == trajectory_key:
                return row
        return None

    async def list_for_review(
        self,
        *,
        tenant_id: UUID,
        agent_name: str | None = None,
        status: CandidateStatus | None = None,
        signal: CurationSignal | None = None,
    ) -> list[CurationCandidateRecord]:
        rows = [
            r
            for r in self._rows.values()
            if r.tenant_id == tenant_id
            and (agent_name is None or r.agent_name == agent_name)
            and (status is None or r.status is status)
            and (signal is None or r.signal == signal)
        ]
        rows.sort(key=lambda r: r.detected_at, reverse=True)
        return rows

    async def list_for_review_all_tenants(
        self,
        *,
        agent_name: str | None = None,
        status: CandidateStatus | None = None,
        signal: CurationSignal | None = None,
        unevolved_only: bool = False,
    ) -> list[CurationCandidateRecord]:
        # Stream N — no tenant filter.
        rows = [
            r
            for r in self._rows.values()
            if (agent_name is None or r.agent_name == agent_name)
            and (status is None or r.status is status)
            and (signal is None or r.signal == signal)
            and (not unevolved_only or r.evolved_at is None)
        ]
        rows.sort(key=lambda r: r.detected_at, reverse=True)
        return rows

    async def mark_evolved(self, *, candidate_id: UUID, tenant_id: UUID, at: datetime) -> bool:
        existing = self._rows.get(candidate_id)
        if existing is None or existing.tenant_id != tenant_id:
            return False
        self._rows[candidate_id] = existing.model_copy(update={"evolved_at": at})
        return True

    async def record_retry(self, *, candidate_id: UUID, tenant_id: UUID) -> int:
        existing = self._rows.get(candidate_id)
        if existing is None or existing.tenant_id != tenant_id:
            return 0
        bumped = existing.model_copy(update={"retry_count": existing.retry_count + 1})
        self._rows[candidate_id] = bumped
        return bumped.retry_count

    async def update(self, record: CurationCandidateRecord) -> bool:
        existing = self._rows.get(record.id)
        if existing is None or existing.tenant_id != record.tenant_id:
            return False
        self._rows[record.id] = record
        return True

    async def revert_promoted_for_dataset(self, *, dataset_id: UUID, tenant_id: UUID) -> int:
        reverted = 0
        for cid, r in list(self._rows.items()):
            if (
                r.tenant_id == tenant_id
                and r.eval_dataset_id == dataset_id
                and r.status is CandidateStatus.PROMOTED
            ):
                # One-shot copy — a PROMOTED record without a dataset id is an
                # illegal intermediate state (frozen model validator).
                self._rows[cid] = r.model_copy(
                    update={"status": CandidateStatus.PENDING, "eval_dataset_id": None}
                )
                reverted += 1
        return reverted

    async def anonymize_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        updated = 0
        for cid, r in list(self._rows.items()):
            if r.tenant_id == tenant_id and r.user_id == user_id:
                self._rows[cid] = r.model_copy(update={"user_id": None})
                updated += 1
        return updated
