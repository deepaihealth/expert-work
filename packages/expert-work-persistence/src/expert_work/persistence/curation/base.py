"""Abstract curation stores — Stream J.12 (Mini-ADR J-43).

Two repositories backing the learning / feedback loop
(STREAM-J-DESIGN § 17):

* :class:`EvalDatasetStore` — the curated eval cases (the
  ``eval_dataset`` table), shared with J.13.
* :class:`CurationCandidateStore` — trajectories the curation worker
  flagged for human review (the ``curation_candidate`` table).

Both are tenant-scoped. The curation worker upserts candidates while
iterating trajectories cross-tenant; it re-scopes to each trajectory's
own tenant before the upsert, so these stores need no cross-tenant
method.

Implementations:
- :mod:`expert_work.persistence.curation.memory`
- :mod:`expert_work.persistence.curation.sql`
"""

from __future__ import annotations

import abc
from datetime import datetime
from uuid import UUID

from expert_work.protocol import (
    CandidateStatus,
    CurationCandidateRecord,
    CurationSignal,
    EvalDatasetRecord,
)


class EvalDatasetStore(abc.ABC):
    """Registry of curated eval cases — the ``eval_dataset`` table."""

    @abc.abstractmethod
    async def create(self, record: EvalDatasetRecord) -> EvalDatasetRecord:
        """Persist a new curated eval case."""

    @abc.abstractmethod
    async def get(self, *, dataset_id: UUID, tenant_id: UUID) -> EvalDatasetRecord | None:
        """Return the case row, or ``None`` when unknown / cross-tenant."""

    @abc.abstractmethod
    async def list_by_agent(self, *, tenant_id: UUID, agent_name: str) -> list[EvalDatasetRecord]:
        """Return every curated case for ``agent_name`` — backs the export CLI."""

    @abc.abstractmethod
    async def list_by_tenant(self, *, tenant_id: UUID) -> list[EvalDatasetRecord]:
        """Return every curated case under the tenant, newest first."""

    @abc.abstractmethod
    async def list_all_tenants(self) -> list[EvalDatasetRecord]:
        """Cross-tenant list — Stream N (Mini-ADR N-4).

        Caller MUST be inside ``bypass_rls_session()`` or RLS denies all rows.
        """

    @abc.abstractmethod
    async def update(self, record: EvalDatasetRecord) -> bool:
        """Replace a case row (matched by ``id`` + ``tenant_id``); return hit."""

    @abc.abstractmethod
    async def delete(self, *, dataset_id: UUID, tenant_id: UUID) -> bool:
        """Delete a case row; return ``True`` iff it existed."""

    @abc.abstractmethod
    async def count_by_tenant(self, *, tenant_id: UUID) -> int:
        """Count a tenant's curated cases — backs the create-time quota."""

    @abc.abstractmethod
    async def anonymize_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Phase 3a (purge_user) — null ``source_user_id``, KEEP the eval cases.

        An eval case is a tenant asset (curated test data), not per-user PII —
        the row is KEPT and only ``source_user_id`` (the provenance link to the
        purged user) is nulled. Returns the count updated. Tenant- AND
        user-scoped; ``tenant_id`` is NOT NULL on this table so the predicate is
        unambiguous."""


class CurationCandidateStore(abc.ABC):
    """Registry of curation candidates — the ``curation_candidate`` table."""

    @abc.abstractmethod
    async def upsert(self, record: CurationCandidateRecord) -> bool:
        """Insert ``record`` only if no candidate exists for its trajectory.

        Keyed by ``(tenant_id, trajectory_key)``. Returns ``True`` when a
        row was inserted, ``False`` when a candidate already existed —
        the worker re-scans trajectories every cycle and relies on this
        to flag each at most once (Mini-ADR J-43).
        """

    @abc.abstractmethod
    async def get(self, *, candidate_id: UUID, tenant_id: UUID) -> CurationCandidateRecord | None:
        """Return the candidate row, or ``None`` when unknown / cross-tenant."""

    @abc.abstractmethod
    async def get_by_trajectory_key(
        self, *, tenant_id: UUID, trajectory_key: str
    ) -> CurationCandidateRecord | None:
        """Return the candidate for a trajectory, or ``None`` when none exists."""

    @abc.abstractmethod
    async def list_for_review(
        self,
        *,
        tenant_id: UUID,
        agent_name: str | None = None,
        status: CandidateStatus | None = None,
        signal: CurationSignal | None = None,
    ) -> list[CurationCandidateRecord]:
        """Return candidates for the review UI, newest first.

        ``agent_name`` / ``status`` / ``signal`` are optional filters.
        """

    @abc.abstractmethod
    async def list_for_review_all_tenants(
        self,
        *,
        agent_name: str | None = None,
        status: CandidateStatus | None = None,
        signal: CurationSignal | None = None,
        unevolved_only: bool = False,
    ) -> list[CurationCandidateRecord]:
        """Cross-tenant review list — Stream N (Mini-ADR N-4).

        Caller MUST be inside ``bypass_rls_session()``. ``unevolved_only`` (4.4
        #5) filters to candidates the SE-6 evolution worker hasn't yet processed
        (``evolved_at IS NULL``); the worker passes ``True`` so it doesn't
        re-distil the same trajectory every interval, while the human-review
        surface keeps the default (sees all candidates).
        """

    @abc.abstractmethod
    async def mark_evolved(self, *, candidate_id: UUID, tenant_id: UUID, at: datetime) -> bool:
        """Stamp ``evolved_at`` so the SE-6 worker won't re-process this
        candidate (4.4 #5). Idempotent; returns whether a row was matched.
        Orthogonal to ``update`` (which carries the J.12 review verdict)."""

    @abc.abstractmethod
    async def record_retry(self, *, candidate_id: UUID, tenant_id: UUID) -> int:
        """SE-16 (SE-A40) — atomically bump ``retry_count`` after a transient
        distillation failure (aux LLM timeout / rate limit / connection) and
        return the new value. The candidate stays unevolved so the next sweep
        re-picks it; the worker marks evolved (gives up) once the returned
        count reaches its budget. Returns 0 when no row matched."""

    @abc.abstractmethod
    async def update(self, record: CurationCandidateRecord) -> bool:
        """Replace a candidate row (matched by ``id`` + ``tenant_id``); return hit.

        Used by the curation API to record promote / dismiss verdicts.
        """

    @abc.abstractmethod
    async def revert_promoted_for_dataset(self, *, dataset_id: UUID, tenant_id: UUID) -> int:
        """Deletion hygiene PR3 — revert PROMOTED candidates pointing at a
        deleted ``eval_dataset`` row back to PENDING.

        Predicate: ``status == PROMOTED AND eval_dataset_id == dataset_id``
        (tenant-scoped). Writes ``status=PENDING, eval_dataset_id=None`` in
        one shot (a PROMOTED candidate without a dataset id is an illegal
        state); every other column (``reviewed_at`` / ``evolved_at`` …) is
        left untouched. The candidate becomes re-promotable — promoting
        again mints a fresh ``eval_dataset`` row. Returns the count
        reverted."""

    @abc.abstractmethod
    async def anonymize_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Phase 3a (purge_user) — null ``user_id``, KEEP the candidate rows.

        A curation candidate is tenant learning-loop data, not per-user PII —
        the row is KEPT and only ``user_id`` (the provenance link to the purged
        user) is nulled. Returns the count updated. Tenant- AND user-scoped;
        ``tenant_id`` is NOT NULL on this table so the predicate is
        unambiguous."""
