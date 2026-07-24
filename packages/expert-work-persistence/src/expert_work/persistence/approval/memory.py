"""In-memory ``ApprovalStore`` for unit tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from expert_work.persistence.approval.base import ApprovalStore
from expert_work.protocol import ApprovalRecord, ApprovalStatus


class InMemoryApprovalStore(ApprovalStore):
    def __init__(self) -> None:
        # Keyed by run_id — a run pauses at most once at a time.
        self._rows: dict[UUID, ApprovalRecord] = {}

    async def create(self, record: ApprovalRecord) -> ApprovalRecord:
        if record.run_id in self._rows:
            msg = f"approval row already exists for run {record.run_id}"
            raise ValueError(msg)
        self._rows[record.run_id] = record
        return record

    async def get_by_run(self, *, run_id: UUID, tenant_id: UUID) -> ApprovalRecord | None:
        row = self._rows.get(run_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def list_expired(
        self,
        *,
        before: datetime,
        limit: int = 1000,
    ) -> list[ApprovalRecord]:
        rows = [
            r
            for r in self._rows.values()
            if r.status == ApprovalStatus.PENDING and r.timeout_at < before
        ]
        rows.sort(key=lambda r: r.timeout_at)
        return rows[:limit]

    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        status: ApprovalStatus,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ApprovalRecord], int]:
        rows = [r for r in self._rows.values() if r.tenant_id == tenant_id and r.status == status]
        rows.sort(key=lambda r: r.requested_at)
        return rows[offset : offset + limit], len(rows)

    async def list_all_tenants(
        self,
        *,
        status: ApprovalStatus,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ApprovalRecord], int]:
        rows = [r for r in self._rows.values() if r.status == status]
        rows.sort(key=lambda r: r.requested_at)
        return rows[offset : offset + limit], len(rows)

    async def count_pending(self) -> int:
        return sum(1 for r in self._rows.values() if r.status == ApprovalStatus.PENDING)

    async def mark_decided(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        status: ApprovalStatus,
        decided_by: str,
        decided_at: datetime,
        modified_args: dict[str, object] | None = None,
        idempotency_key: str | None = None,
        continuation_run_id: UUID | None = None,
        binding_digest: str | None = None,
    ) -> bool:
        # check-then-set with no ``await`` between → atomic under asyncio's
        # cooperative scheduling (mirrors the SQL conditional-UPDATE CAS).
        row = self._rows.get(run_id)
        if row is None or row.tenant_id != tenant_id or row.status != ApprovalStatus.PENDING:
            return False
        update: dict[str, object] = {
            "status": status,
            "decided_by": decided_by,
            "decided_at": decided_at,
            "modified_args": modified_args,
            "idempotency_key": idempotency_key,
            "continuation_run_id": continuation_run_id,
        }
        # RT-6 Tier A (RT-ADR-19) — modify re-binds; approve/reject keep the mint digest.
        if binding_digest is not None:
            update["binding_digest"] = binding_digest
        self._rows[run_id] = row.model_copy(update=update)
        return True

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        victims = [
            run_id
            for run_id, r in self._rows.items()
            if r.tenant_id == tenant_id and r.user_id == user_id
        ]
        for run_id in victims:
            del self._rows[run_id]
        return len(victims)

    async def delete_for_threads(self, *, thread_ids: Sequence[UUID], tenant_id: UUID) -> int:
        if not thread_ids:
            return 0
        wanted = set(thread_ids)
        victims = [
            run_id
            for run_id, r in self._rows.items()
            if r.tenant_id == tenant_id and r.thread_id in wanted
        ]
        for run_id in victims:
            del self._rows[run_id]
        return len(victims)
