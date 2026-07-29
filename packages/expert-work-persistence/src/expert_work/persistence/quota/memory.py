"""In-memory implementations of the Stream C.5 quota stores."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from expert_work.persistence.quota.base import (
    BudgetExceededError,
    DuplicateQuotaError,
    ReservationNotFoundError,
    TenantQuotaStore,
    TokenReservationStore,
)
from expert_work.protocol import (
    ReservationState,
    TenantBudgetRecord,
    TenantQuotaPatch,
    TenantQuotaRecord,
    TokenReservationRecord,
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _scope_key(scope: dict[str, str]) -> str:
    """Stable canonicalisation of ``scope`` so equality matches DB UNIQUE."""
    return json.dumps(scope, sort_keys=True, separators=(",", ":"))


class InMemoryTenantQuotaStore(TenantQuotaStore):
    """Backed by a single dict; lock-guarded for asyncio safety."""

    def __init__(self) -> None:
        self._rows: dict[UUID, TenantQuotaRecord] = {}
        self._lock = asyncio.Lock()

    async def list_by_tenant(self, *, tenant_id: UUID) -> list[TenantQuotaRecord]:
        async with self._lock:
            rows = [r for r in self._rows.values() if r.tenant_id == tenant_id]
            return sorted(rows, key=lambda r: (r.dimension.value, _scope_key(r.scope)))

    async def upsert(
        self,
        *,
        tenant_id: UUID,
        patch: TenantQuotaPatch,
        updated_by: str,
    ) -> TenantQuotaRecord:
        scope_key = _scope_key(patch.scope)
        async with self._lock:
            for existing_id, row in self._rows.items():
                if (
                    row.tenant_id == tenant_id
                    and row.dimension == patch.dimension
                    and _scope_key(row.scope) == scope_key
                ):
                    updated = row.model_copy(
                        update={
                            "limit_value": patch.limit_value,
                            "burst": patch.burst,
                            "effective_until": patch.effective_until,
                            "updated_by": updated_by,
                            "updated_at": _now(),
                        }
                    )
                    self._rows[existing_id] = updated
                    return updated
            row = TenantQuotaRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                dimension=patch.dimension,
                scope=patch.scope,
                limit_value=patch.limit_value,
                burst=patch.burst,
                effective_from=_now(),
                effective_until=patch.effective_until,
                updated_by=updated_by,
                updated_at=_now(),
            )
            self._rows[row.id] = row
            return row

    async def delete(self, *, quota_id: UUID, tenant_id: UUID) -> bool:
        async with self._lock:
            row = self._rows.get(quota_id)
            if row is None or row.tenant_id != tenant_id:
                return False
            del self._rows[quota_id]
            return True

    # Test-helper (not on the Protocol): seed a row directly.
    async def insert_for_test(self, record: TenantQuotaRecord) -> None:
        async with self._lock:
            for r in self._rows.values():
                if (
                    r.tenant_id == record.tenant_id
                    and r.dimension == record.dimension
                    and _scope_key(r.scope) == _scope_key(record.scope)
                ):
                    raise DuplicateQuotaError(
                        tenant_id=record.tenant_id,
                        dimension=record.dimension,
                        scope=record.scope,
                    )
            self._rows[record.id] = record


class InMemoryTokenReservationStore(TokenReservationStore):
    """In-memory reservation ledger keyed by reservation_id + (tenant, month)."""

    def __init__(self) -> None:
        self._reservations: dict[UUID, TokenReservationRecord] = {}
        self._ledger: dict[tuple[UUID, date], TenantBudgetRecord] = {}
        self._lock = asyncio.Lock()

    async def _ensure_budget_locked(self, tenant_id: UUID, month: date) -> TenantBudgetRecord:
        # Caller holds ``self._lock``.
        key = (tenant_id, month)
        row = self._ledger.get(key)
        if row is None:
            row = TenantBudgetRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                month=month,
                budget_total=0,
                used_total=0,
                reserved_total=0,
                updated_at=_now(),
            )
            self._ledger[key] = row
        return row

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
        async with self._lock:
            now = _now()
            month = now.date().replace(day=1)
            budget = await self._ensure_budget_locked(tenant_id, month)
            if budget.budget_total > 0:
                projected = budget.used_total + budget.reserved_total + estimated
                if projected > budget.budget_total:
                    raise BudgetExceededError(tenant_id=tenant_id, month=month)
            row = TokenReservationRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                agent_name=agent_name,
                thread_id=thread_id,
                parent_thread_id=parent_thread_id,
                model=model,
                estimated=estimated,
                actual=None,
                state=ReservationState.RESERVED,
                reserved_at=now,
                closed_at=None,
            )
            self._reservations[row.id] = row
            self._ledger[(tenant_id, month)] = budget.model_copy(
                update={
                    "reserved_total": budget.reserved_total + estimated,
                    "updated_at": now,
                }
            )
            return row

    async def commit(
        self,
        *,
        reservation_id: UUID,
        tenant_id: UUID,
        actual_tokens: int,
    ) -> TokenReservationRecord:
        async with self._lock:
            row = self._reservations.get(reservation_id)
            if row is None or row.tenant_id != tenant_id:
                raise ReservationNotFoundError(reservation_id=reservation_id)
            if row.state is not ReservationState.RESERVED:
                # Idempotent re-commit: just return the existing row.
                return row
            now = _now()
            updated = row.model_copy(
                update={
                    "actual": actual_tokens,
                    "state": ReservationState.COMMITTED,
                    "closed_at": now,
                }
            )
            self._reservations[reservation_id] = updated
            budget = await self._ensure_budget_locked(
                tenant_id, row.reserved_at.date().replace(day=1)
            )
            self._ledger[(tenant_id, budget.month)] = budget.model_copy(
                update={
                    "used_total": budget.used_total + actual_tokens,
                    "reserved_total": max(0, budget.reserved_total - row.estimated),
                    "updated_at": now,
                }
            )
            return updated

    async def release(
        self,
        *,
        reservation_id: UUID,
        tenant_id: UUID,
        new_state: ReservationState = ReservationState.RELEASED,
    ) -> TokenReservationRecord:
        if new_state not in (ReservationState.RELEASED, ReservationState.EXPIRED):
            msg = f"release new_state must be RELEASED or EXPIRED, got {new_state.value}"
            raise ValueError(msg)
        async with self._lock:
            row = self._reservations.get(reservation_id)
            if row is None or row.tenant_id != tenant_id:
                raise ReservationNotFoundError(reservation_id=reservation_id)
            if row.state is not ReservationState.RESERVED:
                return row
            now = _now()
            updated = row.model_copy(
                update={"state": new_state, "closed_at": now},
            )
            self._reservations[reservation_id] = updated
            budget = await self._ensure_budget_locked(
                tenant_id, row.reserved_at.date().replace(day=1)
            )
            self._ledger[(tenant_id, budget.month)] = budget.model_copy(
                update={
                    "reserved_total": max(0, budget.reserved_total - row.estimated),
                    "updated_at": now,
                }
            )
            return updated

    async def expire_reserved(self, *, reservation_id: UUID, tenant_id: UUID) -> bool:
        async with self._lock:
            row = self._reservations.get(reservation_id)
            if row is None or row.tenant_id != tenant_id:
                return False
            if row.state is not ReservationState.RESERVED:
                # Already closed by a peer — loser, no refund, no hook.
                return False
            now = _now()
            self._reservations[reservation_id] = row.model_copy(
                update={"state": ReservationState.EXPIRED, "closed_at": now}
            )
            budget = await self._ensure_budget_locked(
                tenant_id, row.reserved_at.date().replace(day=1)
            )
            self._ledger[(tenant_id, budget.month)] = budget.model_copy(
                update={
                    "reserved_total": max(0, budget.reserved_total - row.estimated),
                    "updated_at": now,
                }
            )
            return True

    async def list_expired(
        self,
        *,
        max_age_seconds: int,
        limit: int = 100,
    ) -> Sequence[TokenReservationRecord]:
        cutoff = _now() - timedelta(seconds=max_age_seconds)
        async with self._lock:
            expired = [
                r
                for r in self._reservations.values()
                if r.state is ReservationState.RESERVED and r.reserved_at < cutoff
            ]
            return sorted(expired, key=lambda r: r.reserved_at)[:limit]

    async def get(self, *, reservation_id: UUID, tenant_id: UUID) -> TokenReservationRecord | None:
        async with self._lock:
            row = self._reservations.get(reservation_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return row

    async def get_budget(self, *, tenant_id: UUID, month: date) -> TenantBudgetRecord | None:
        async with self._lock:
            return self._ledger.get((tenant_id, month))

    # Test-helper: seed the ledger total for "tenant has X tokens for this month".
    async def set_budget_total_for_test(
        self, *, tenant_id: UUID, month: date, budget_total: int
    ) -> None:
        async with self._lock:
            row = await self._ensure_budget_locked(tenant_id, month)
            self._ledger[(tenant_id, month)] = row.model_copy(
                update={"budget_total": budget_total, "updated_at": _now()}
            )
