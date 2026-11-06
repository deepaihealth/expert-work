"""Abstract :class:`QualityScoreStore` — Stream RT-5 (RT-ADR-24)."""

from __future__ import annotations

import abc
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from expert_work.protocol import QualityScoreRecord


class QualityScoreStore(abc.ABC):
    """Persistence for per-run production quality verdicts (time-series)."""

    @abc.abstractmethod
    async def insert(self, record: QualityScoreRecord) -> QualityScoreRecord:
        """Idempotent insert; a re-scan of the same run is a no-op.

        Dedup is ``(tenant_id, run_id)`` (``ON CONFLICT DO NOTHING``): the
        sampling watermark can overlap harmlessly. Returns the stored record
        (the pre-existing one on conflict) with ``id`` / ``observed_at`` set.
        """

    @abc.abstractmethod
    async def exists(self, *, tenant_id: UUID, run_id: UUID) -> bool:
        """Whether a verdict for ``(tenant_id, run_id)`` already exists.

        Lets the monitor skip a run it already judged *before* spending judge
        tokens — a run re-entering the candidate feed (its
        ``agent_run.updated_at`` bumped after it was scored) must not be
        re-judged or re-counted (RT-ADR-22).
        """

    @abc.abstractmethod
    async def count_since(self, *, tenant_id: UUID, since: datetime) -> int:
        """Count a tenant's verdicts with ``observed_at >= since``.

        Backs the per-tenant daily sampling cap (RT-ADR-22 cost guardrail).
        """

    @abc.abstractmethod
    async def list_scores(
        self,
        *,
        tenant_id: UUID,
        agent_name: str | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[QualityScoreRecord]:
        """Recent verdicts, newest ``observed_at`` first (``id`` tiebreak).

        Serves the drift window (RT-ADR-24) and the dashboard trend
        (RT-ADR-26). ``agent_name`` / ``since`` narrow the series.
        """

    @abc.abstractmethod
    async def list_scores_all_tenants(
        self,
        *,
        agent_name: str | None = None,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[QualityScoreRecord]:
        """:meth:`list_scores` across every tenant (``observed_at`` DESC,
        ``id`` tiebreak) — the W4 ``tenant_id=*`` aggregate. Caller MUST wrap
        the call in ``bypass_rls_session()`` / ``applied_scope(CrossTenant)``.
        """

    @abc.abstractmethod
    async def list_agents_with_scores_since(self, *, since: datetime) -> list[tuple[UUID, str]]:
        """Distinct ``(tenant_id, agent_name)`` with a verdict at/after ``since``.

        Cross-tenant — the drift worker (RT-ADR-24) runs it under the RLS-bypass
        scope to enumerate the agents worth a drift check this cycle.
        """

    @abc.abstractmethod
    async def window_stats(
        self, *, tenant_id: UUID, agent_name: str, since: datetime, until: datetime
    ) -> tuple[int, float | None]:
        """``(count, mean overall)`` for ``observed_at`` in ``[since, until)``.

        SQL aggregate (not a bounded ``list_scores``) so a wide baseline window
        is never silently truncated. ``mean`` is ``None`` when ``count`` is 0.
        """

    # --- B-278 彻底删除智能体 ---------------------------------------------

    @abc.abstractmethod
    async def count_for_agent(self, *, tenant_id: UUID, agent_name: str) -> int:
        """B-278 —— rows ``delete_for_agent`` would remove (purge preview)."""

    @abc.abstractmethod
    async def delete_for_agent(self, *, tenant_id: UUID, agent_name: str) -> int:
        """B-278 —— hard-delete every row whose ``agent_name`` is exactly this
        name (agent purge). Tenant-scoped; returns the rows removed."""

    # --- B-280 删除单个对话 -------------------------------------------------

    @abc.abstractmethod
    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        """B-280 —— hard-delete the verdicts of these conversations (conversation
        purge). Tenant-scoped; returns the rows removed."""
