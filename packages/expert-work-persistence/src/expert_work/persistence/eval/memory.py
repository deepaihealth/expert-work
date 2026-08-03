"""In-memory eval-run store for unit tests — P1-S2.1."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import count
from uuid import UUID

from expert_work.persistence.eval.base import EvalRunStore
from expert_work.protocol import (
    EvalCaseResultRecord,
    EvalRunRecord,
    EvalRunStatus,
)

_TERMINAL = {EvalRunStatus.PASSED, EvalRunStatus.FAILED, EvalRunStatus.ERROR}


class InMemoryEvalRunStore(EvalRunStore):
    """In-memory ``EvalRunStore`` — runs keyed by id, results in a flat list."""

    def __init__(self) -> None:
        self._runs: dict[UUID, EvalRunRecord] = {}
        self._results: list[EvalCaseResultRecord] = []
        self._ids = count(1)

    async def create_run(self, record: EvalRunRecord) -> EvalRunRecord:
        if record.id in self._runs:
            msg = f"eval_run already exists for id {record.id}"
            raise ValueError(msg)
        self._runs[record.id] = record
        return record

    async def get_run(self, *, run_id: UUID, tenant_id: UUID) -> EvalRunRecord | None:
        row = self._runs.get(run_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def set_status(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        status: EvalRunStatus,
        summary: dict[str, object] | None = None,
    ) -> bool:
        row = self._runs.get(run_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        now = datetime.now(UTC)
        updates: dict[str, object] = {"status": status}
        if status is EvalRunStatus.RUNNING and row.started_at is None:
            updates["started_at"] = now
        if status in _TERMINAL:
            updates["finished_at"] = now
        if summary is not None:
            updates["summary"] = summary
        self._runs[run_id] = row.model_copy(update=updates)
        return True

    async def claim(self, *, run_id: UUID, tenant_id: UUID) -> bool:
        row = self._runs.get(run_id)
        if row is None or row.tenant_id != tenant_id or row.status is not EvalRunStatus.QUEUED:
            return False
        self._runs[run_id] = row.model_copy(
            update={"status": EvalRunStatus.RUNNING, "started_at": datetime.now(UTC)}
        )
        return True

    async def list_by_status_all_tenants(self, status: EvalRunStatus) -> list[EvalRunRecord]:
        rows = [r for r in self._runs.values() if r.status is status]
        rows.sort(key=lambda r: r.created_at)
        return rows

    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        status: EvalRunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[EvalRunRecord], int]:
        rows = [
            r
            for r in self._runs.values()
            if r.tenant_id == tenant_id and (status is None or r.status is status)
        ]
        # created_at DESC with id tiebreak (two stable sorts) — same ordering
        # contract as list_all_tenants, keeps pagination stable on ties.
        rows.sort(key=lambda r: r.id.int)
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[offset : offset + limit], len(rows)

    async def list_all_tenants(
        self,
        *,
        status: EvalRunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[EvalRunRecord], int]:
        # W4 — no tenant filter; created_at DESC with id tiebreak (two stable
        # sorts) matching the SQL implementation byte-for-byte.
        rows = [r for r in self._runs.values() if status is None or r.status is status]
        rows.sort(key=lambda r: r.id.int)
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[offset : offset + limit], len(rows)

    async def append_case_result(self, record: EvalCaseResultRecord) -> EvalCaseResultRecord:
        stored = record.model_copy(update={"id": next(self._ids)})
        self._results.append(stored)
        return stored

    async def list_case_results(
        self, *, run_id: UUID, tenant_id: UUID
    ) -> list[EvalCaseResultRecord]:
        return [r for r in self._results if r.run_id == run_id and r.tenant_id == tenant_id]
