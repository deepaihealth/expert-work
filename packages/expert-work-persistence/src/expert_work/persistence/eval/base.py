"""Abstract eval-run store — P1-S2.1 (eval platform ops layer).

Backs the durable record of eval runs + per-case results that the
resident ``EvalWorker`` (S2.1b) populates. Tenant-scoped: every method
takes ``tenant_id`` so an in-memory backend matches the Postgres FORCE-RLS
semantics. The worker's queued-run scan is cross-tenant
(:meth:`EvalRunStore.list_by_status_all_tenants`) and MUST run inside
``bypass_rls_session()`` — same posture as the memory consolidator.

Implementations:
- :mod:`expert_work.persistence.eval.memory`
- :mod:`expert_work.persistence.eval.sql`
"""

from __future__ import annotations

import abc
from uuid import UUID

from expert_work.protocol import (
    EvalCaseResultRecord,
    EvalRunRecord,
    EvalRunStatus,
)


class EvalRunStore(abc.ABC):
    """Registry of eval runs + their case results."""

    @abc.abstractmethod
    async def create_run(self, record: EvalRunRecord) -> EvalRunRecord:
        """Persist a new run (typically ``status=queued``)."""

    @abc.abstractmethod
    async def get_run(self, *, run_id: UUID, tenant_id: UUID) -> EvalRunRecord | None:
        """Return the run, or ``None`` when unknown / cross-tenant."""

    @abc.abstractmethod
    async def set_status(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        status: EvalRunStatus,
        summary: dict[str, object] | None = None,
    ) -> bool:
        """Advance the status machine; stamp ``started_at`` on first RUNNING and
        ``finished_at`` on a terminal status. Return ``True`` iff the run existed."""

    @abc.abstractmethod
    async def claim(self, *, run_id: UUID, tenant_id: UUID) -> bool:
        """Atomically claim a queued run for execution; ``True`` iff won.

        Stream 9.5 — CAS on ``status='queued'`` (→ ``running`` + stamp
        ``started_at``) so exactly one eval worker executes a run when several
        instances (blue/green) scan the queue concurrently. The loser sees
        ``False`` (rowcount 0) and skips. Without this two workers both
        ``list``-then-run the same queued run → duplicate execution.
        """

    @abc.abstractmethod
    async def list_by_status_all_tenants(self, status: EvalRunStatus) -> list[EvalRunRecord]:
        """Cross-tenant list by status — backs the worker claim scan.

        Caller MUST be inside ``bypass_rls_session()`` or RLS denies all rows.
        """

    @abc.abstractmethod
    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        status: EvalRunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[EvalRunRecord], int]:
        """Per-tenant page of runs (``created_at`` DESC), plus the total count
        *before* pagination. Backs the operator list page (S2.5). The W4
        cross-tenant aggregate is :meth:`list_all_tenants`.
        """

    @abc.abstractmethod
    async def list_all_tenants(
        self,
        *,
        status: EvalRunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[EvalRunRecord], int]:
        """Cross-tenant page of runs (``created_at`` DESC, ``id`` tiebreak),
        plus the pre-pagination total — the W4 ``tenant_id=*`` aggregate.
        Caller MUST wrap the call in ``bypass_rls_session()`` /
        ``applied_scope(CrossTenant)`` (same posture as
        :meth:`list_by_status_all_tenants`)."""

    @abc.abstractmethod
    async def append_case_result(self, record: EvalCaseResultRecord) -> EvalCaseResultRecord:
        """Persist one case result; return it with the assigned ``id``."""

    @abc.abstractmethod
    async def list_case_results(
        self, *, run_id: UUID, tenant_id: UUID
    ) -> list[EvalCaseResultRecord]:
        """Return every case result under ``run_id``, insertion order."""
