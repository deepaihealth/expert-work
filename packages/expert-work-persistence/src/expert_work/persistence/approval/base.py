"""Abstract ``ApprovalStore`` repository — Stream J.8 (Mini-ADR J-24).

The durable registry of runs paused for human approval. expert_work's
``RunManager`` is in-memory only, so this store is the only place a
paused run survives a control-plane restart — what ``GET`` reads to
surface ``pending_approval``, what ``POST .../resume`` looks up, and
what the 24h timeout job scans.

Implementations:
- :class:`expert_work.persistence.approval.memory.InMemoryApprovalStore`
- :class:`expert_work.persistence.approval.sql.SqlApprovalStore`
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from expert_work.protocol import ApprovalRecord, ApprovalStatus


class ApprovalStore(abc.ABC):
    """Registry of paused-run approval records, tenant-scoped."""

    @abc.abstractmethod
    async def create(self, record: ApprovalRecord) -> ApprovalRecord:
        """Persist a new ``pending`` approval row when a run pauses.

        ``run_id`` is unique — a run pauses at most once at a time. A
        second ``create`` for the same run is a programming error and
        the SQL backend's unique constraint surfaces it.
        """

    @abc.abstractmethod
    async def get_by_run(self, *, run_id: UUID, tenant_id: UUID) -> ApprovalRecord | None:
        """Return the approval row for ``run_id``, or ``None``.

        ``None`` when the run id is unknown or belongs to another
        tenant — callers turn that into 404, never raising on a
        cross-tenant probe.
        """

    @abc.abstractmethod
    async def list_expired(
        self,
        *,
        before: datetime,
        limit: int = 1000,
    ) -> list[ApprovalRecord]:
        """Pending rows whose ``timeout_at < before`` — the timeout sweep.

        The 24h job calls this with ``before = now``; every returned
        row is past its deadline and eligible for auto-reject.
        """

    @abc.abstractmethod
    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        status: ApprovalStatus,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ApprovalRecord], int]:
        """One tenant's approval rows in ``status`` + the total count.

        Stream HX-7 — backs ``GET /v1/approvals`` (the admin queue
        page). Ordered ``requested_at ASC`` — queue semantics, oldest
        waiting first.
        """

    @abc.abstractmethod
    async def list_all_tenants(
        self,
        *,
        status: ApprovalStatus,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ApprovalRecord], int]:
        """Cross-tenant rows in ``status`` + total (Stream HX-7).

        The system_admin ``tenant_id=*`` scope (Stream N). Like
        :meth:`count_pending`, the SQL read relies on the owner's
        exemption under ENABLE-only RLS — callers run it under a
        bypass scope. Ordered ``requested_at ASC``.
        """

    @abc.abstractmethod
    async def count_pending(self) -> int:
        """Number of ``pending`` rows across all tenants — Stream HX-4.

        Feeds the ``expert_work_control_plane_approvals_pending`` gauge (one
        platform-wide number; per-tenant counts come from the API, not
        a metric label). Like :meth:`list_expired`, the SQL read relies
        on the owner's exemption under ENABLE-only RLS — callers run it
        under a bypass scope (no tenant GUC).
        """

    @abc.abstractmethod
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
        """Flip a ``pending`` row to a terminal verdict; return ``True`` on hit.

        Returns ``False`` when the run id is unknown, in another
        tenant, or already decided (callers treat that as a 404 /
        409 — the verdict is idempotent-once). ``status`` must be one
        of the terminal :class:`ApprovalStatus` values.

        Stream 13.2 — ``idempotency_key`` + ``continuation_run_id`` are written
        atomically with the same conditional UPDATE, so the CAS winner records
        the continuation it spawned. A later retry / lost-race caller re-reads
        them to replay the same continuation instead of conflicting.

        RT-6 Tier A (RT-ADR-19) — ``binding_digest``, when given, overwrites the
        mint-time digest atomically with the CAS (a ``modify`` re-binds to the
        digest of ``modified_args``); ``None`` leaves the mint digest intact.
        """

    @abc.abstractmethod
    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Phase 3a (purge_user) — hard-delete EVERY approval row for a user.

        Removes all of the user's ``agent_approval`` rows (pending or decided)
        and returns the count deleted (0 when none / re-purge). Tenant- AND
        user-scoped — the ``(tenant_id, user_id)`` predicate never touches
        another tenant's or user's rows; rows with a NULL ``user_id`` (legacy /
        machine-triggered) are not this user's and are left untouched."""

    @abc.abstractmethod
    async def delete_for_threads(self, *, thread_ids: Sequence[UUID], tenant_id: UUID) -> int:
        """Hard-delete every approval row for ``tenant_id`` on any of ``thread_ids``.

        Deletion-hygiene purge (PR3) — cascades approval removal when a
        session's threads are purged, so ``purge_session`` leaves no
        orphan ``agent_approval`` rows. Scoped by both tenant and thread
        id so another tenant's row sharing the same thread id is
        untouched. An empty ``thread_ids`` deletes nothing and returns
        ``0``.

        A *write* — ``agent_approval`` is ENABLE-only RLS and the app's
        owner connection is exempt, so the explicit ``(tenant_id,
        thread_id)`` predicate is the tenant guard here, not RLS.
        """
