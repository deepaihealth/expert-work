"""Abstract quota-store interfaces — Stream C.5.

Two stores power the quota engine's persistent state (Redis carries
the hot-path bucket counters; Postgres carries the durable
admin-curated config + reservations / ledger):

* :class:`TenantQuotaStore` — CRUD on ``tenant_quota`` rows
* :class:`TokenReservationStore` — append / state-machine the
  ``token_reservation`` rows + month-bucketed
  ``token_budget_ledger`` updates

Each method takes ``tenant_id`` explicitly even though RLS would
also enforce it: the application code stays readable, mypy strict
catches the misuse before the database does, and unit tests with the
in-memory impls don't need RLS at all.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from datetime import date
from uuid import UUID

from expert_work.protocol import (
    QuotaDimension,
    ReservationState,
    TenantBudgetRecord,
    TenantQuotaPatch,
    TenantQuotaRecord,
    TokenReservationRecord,
)


class DuplicateQuotaError(Exception):
    """``(tenant_id, dimension, scope)`` collision on insert."""

    def __init__(
        self, *, tenant_id: UUID, dimension: QuotaDimension, scope: dict[str, str]
    ) -> None:
        super().__init__(
            f"tenant_quota already exists: tenant={tenant_id} "
            f"dimension={dimension.value} scope={scope}"
        )
        self.tenant_id = tenant_id
        self.dimension = dimension
        self.scope = scope


class ReservationNotFoundError(Exception):
    """``reservation_id`` does not exist (or is not visible to the tenant)."""

    def __init__(self, *, reservation_id: UUID) -> None:
        super().__init__(f"token_reservation not found: {reservation_id}")
        self.reservation_id = reservation_id


class BudgetExceededError(Exception):
    """Admitting this reservation would exceed the tenant's monthly budget.

    Raised by :meth:`TokenReservationStore.reserve` — the budget check and
    the ``reserved_total`` bump happen atomically (same lock / same
    conditional statement), so this is TOCTOU-safe under concurrent
    ``reserve`` calls on the same ``(tenant_id, month)`` ledger row.
    """

    def __init__(self, *, tenant_id: UUID, month: date) -> None:
        super().__init__(f"token_budget exceeded: tenant={tenant_id} month={month}")
        self.tenant_id = tenant_id
        self.month = month


class TenantQuotaStore(abc.ABC):
    """Persistence Protocol for ``tenant_quota`` rows."""

    @abc.abstractmethod
    async def list_by_tenant(self, *, tenant_id: UUID) -> list[TenantQuotaRecord]:
        """All quota rows for ``tenant_id`` (ordered by dimension)."""

    @abc.abstractmethod
    async def upsert(
        self,
        *,
        tenant_id: UUID,
        patch: TenantQuotaPatch,
        updated_by: str,
    ) -> TenantQuotaRecord:
        """Insert or update a row by ``(tenant_id, dimension, scope)``."""

    @abc.abstractmethod
    async def delete(self, *, quota_id: UUID, tenant_id: UUID) -> bool:
        """Return True if a row was deleted."""


class TokenReservationStore(abc.ABC):
    """Persistence Protocol for reservations + monthly ledger."""

    @abc.abstractmethod
    async def reserve(
        self,
        *,
        tenant_id: UUID,
        agent_name: str,
        thread_id: UUID,
        estimated: int,
        parent_thread_id: UUID | None = None,
        model: str | None = None,
    ) -> TokenReservationRecord:
        """Insert a row in ``RESERVED`` state and bump the ledger reserved_total.

        Raises :class:`BudgetExceededError` if ``budget_total`` is set and
        admitting this reservation would push ``used_total + reserved_total``
        past it — the check is atomic with the bump (no reservation row is
        created on rejection).
        """

    @abc.abstractmethod
    async def commit(
        self,
        *,
        reservation_id: UUID,
        tenant_id: UUID,
        actual_tokens: int,
    ) -> TokenReservationRecord:
        """Transition ``RESERVED → COMMITTED`` and update ledger used/reserved totals."""

    @abc.abstractmethod
    async def release(
        self,
        *,
        reservation_id: UUID,
        tenant_id: UUID,
        new_state: ReservationState = ReservationState.RELEASED,
    ) -> TokenReservationRecord:
        """Transition ``RESERVED → RELEASED`` (or ``EXPIRED``); refund reserved_total."""

    @abc.abstractmethod
    async def expire_reserved(self, *, reservation_id: UUID, tenant_id: UUID) -> bool:
        """Atomically expire a stale ``RESERVED`` row; ``True`` iff this call won.

        Stream 9.5 — the every-instance reaper uses this instead of ``release``
        so the EXPIRED transition + ledger refund happen exactly once when
        several reapers (or a racing client commit/release) hit the same row.
        The loser (already-closed / missing row) gets ``False`` and skips its
        ``on_expire`` side effect + expiry count.
        """

    @abc.abstractmethod
    async def list_expired(
        self,
        *,
        max_age_seconds: int,
        limit: int = 100,
    ) -> Sequence[TokenReservationRecord]:
        """Return ``RESERVED`` rows older than ``max_age_seconds`` (reaper input)."""

    @abc.abstractmethod
    async def get(self, *, reservation_id: UUID, tenant_id: UUID) -> TokenReservationRecord | None:
        """Look up a row by id within the tenant."""

    @abc.abstractmethod
    async def get_budget(self, *, tenant_id: UUID, month: date) -> TenantBudgetRecord | None:
        """Return the current month's ledger row, or None if not seeded."""
