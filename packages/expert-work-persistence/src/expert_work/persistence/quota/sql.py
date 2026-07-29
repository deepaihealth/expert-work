"""SQLAlchemy-backed Stream C.5 quota stores."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import (
    TenantQuotaRow,
    TokenBudgetLedgerRow,
    TokenReservationRow,
)
from expert_work.persistence.quota.base import (
    BudgetExceededError,
    ReservationNotFoundError,
    TenantQuotaStore,
    TokenReservationStore,
)
from expert_work.protocol import (
    QuotaDimension,
    ReservationState,
    TenantBudgetRecord,
    TenantQuotaPatch,
    TenantQuotaRecord,
    TokenReservationRecord,
)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _month_of(when: datetime) -> date:
    return when.date().replace(day=1)


def _row_to_quota(row: TenantQuotaRow) -> TenantQuotaRecord:
    return TenantQuotaRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        dimension=QuotaDimension(row.dimension),
        scope={str(k): str(v) for k, v in row.scope.items()},
        limit_value=row.limit_value,
        burst=row.burst,
        effective_from=row.effective_from,
        effective_until=row.effective_until,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
    )


def _row_to_reservation(row: TokenReservationRow) -> TokenReservationRecord:
    return TokenReservationRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_name=row.agent_name,
        thread_id=row.thread_id,
        parent_thread_id=row.parent_thread_id,
        model=row.model,
        estimated=row.estimated,
        actual=row.actual,
        state=ReservationState(row.state),
        reserved_at=row.reserved_at,
        closed_at=row.closed_at,
    )


def _row_to_budget(row: TokenBudgetLedgerRow) -> TenantBudgetRecord:
    return TenantBudgetRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        month=row.month,
        budget_total=row.budget_total,
        used_total=row.used_total,
        reserved_total=row.reserved_total,
        updated_at=row.updated_at,
    )


# ---------------------------------------------------------------------------
# TenantQuota
# ---------------------------------------------------------------------------


class SqlTenantQuotaStore(TenantQuotaStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def list_by_tenant(self, *, tenant_id: UUID) -> list[TenantQuotaRecord]:
        stmt = (
            select(TenantQuotaRow)
            .where(TenantQuotaRow.tenant_id == tenant_id)
            .order_by(TenantQuotaRow.dimension)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_quota(r) for r in rows]

    async def upsert(
        self,
        *,
        tenant_id: UUID,
        patch: TenantQuotaPatch,
        updated_by: str,
    ) -> TenantQuotaRecord:
        now = _utc_now()
        stmt = (
            pg_insert(TenantQuotaRow)
            .values(
                tenant_id=tenant_id,
                dimension=patch.dimension.value,
                scope=dict(patch.scope),
                limit_value=patch.limit_value,
                burst=patch.burst,
                effective_until=patch.effective_until,
                updated_by=updated_by,
                updated_at=now,
            )
            .on_conflict_do_update(
                constraint="tenant_quota_tenant_dimension_scope_uniq",
                set_={
                    "limit_value": patch.limit_value,
                    "burst": patch.burst,
                    "effective_until": patch.effective_until,
                    "updated_by": updated_by,
                    "updated_at": now,
                },
            )
            .returning(TenantQuotaRow)
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
        return _row_to_quota(row)

    async def delete(self, *, quota_id: UUID, tenant_id: UUID) -> bool:
        stmt = delete(TenantQuotaRow).where(
            and_(TenantQuotaRow.id == quota_id, TenantQuotaRow.tenant_id == tenant_id)
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0


# ---------------------------------------------------------------------------
# Reservation + monthly ledger
# ---------------------------------------------------------------------------


class SqlTokenReservationStore(TokenReservationStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def _bump_reserved(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        month: date,
        delta: int,
        now: datetime,
    ) -> None:
        """Atomically add ``delta`` to ``reserved_total`` (upsert-then-increment).

        The arithmetic runs in SQL (``reserved_total = reserved_total + :delta``)
        so concurrent transactions on the same ``(tenant_id, month)`` ledger row
        serialise at the row lock instead of losing each other's read-modify-write.
        """
        await session.execute(
            pg_insert(TokenBudgetLedgerRow)
            .values(
                tenant_id=tenant_id,
                month=month,
                budget_total=0,
                used_total=0,
                reserved_total=max(0, delta),
                updated_at=now,
            )
            .on_conflict_do_update(
                constraint="token_budget_ledger_tenant_month_uniq",
                set_={
                    "reserved_total": func.greatest(0, TokenBudgetLedgerRow.reserved_total + delta),
                    "updated_at": now,
                },
            )
        )

    async def _apply_commit_ledger(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        month: date,
        used_delta: int,
        reserved_delta: int,
        now: datetime,
    ) -> None:
        """Atomically apply a commit's ``used += / reserved -=`` to the ledger.

        The ledger row always exists by commit time (``reserve`` created it), so
        a plain row-locked UPDATE suffices — no upsert.
        """
        await session.execute(
            update(TokenBudgetLedgerRow)
            .where(
                and_(
                    TokenBudgetLedgerRow.tenant_id == tenant_id,
                    TokenBudgetLedgerRow.month == month,
                )
            )
            .values(
                used_total=TokenBudgetLedgerRow.used_total + used_delta,
                reserved_total=func.greatest(
                    0, TokenBudgetLedgerRow.reserved_total + reserved_delta
                ),
                updated_at=now,
            )
        )

    async def _lock_reservation(
        self, session: AsyncSession, *, reservation_id: UUID, tenant_id: UUID
    ) -> TokenReservationRow | None:
        """``SELECT … FOR UPDATE`` the reservation so its state transition is
        serialised — a concurrent commit/release/expire blocks until we commit,
        then re-reads the already-closed state and no-ops (exactly-once refund)."""
        return (
            await session.execute(
                select(TokenReservationRow)
                .where(
                    and_(
                        TokenReservationRow.id == reservation_id,
                        TokenReservationRow.tenant_id == tenant_id,
                    )
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

    async def _lock_budget_row(
        self, session: AsyncSession, *, tenant_id: UUID, month: date, now: datetime
    ) -> TokenBudgetLedgerRow:
        """Ensure the ``(tenant_id, month)`` ledger row exists, then
        ``SELECT … FOR UPDATE`` it — same lock-then-branch style as
        ``_lock_reservation``. The row lock serialises concurrent
        ``reserve()`` calls on this ledger row, so the budget check +
        ``reserved_total`` bump in ``reserve()`` below happen as one
        atomic step instead of racing across a separate check-then-bump
        round trip (the TOCTOU this method fixes).

        ``ON CONFLICT DO NOTHING`` seeds a fresh unlimited-budget row
        without itself racing the lock: concurrent seeders each no-op
        past one another, then every caller locks the same settled row.
        """
        await session.execute(
            pg_insert(TokenBudgetLedgerRow)
            .values(
                tenant_id=tenant_id,
                month=month,
                budget_total=0,
                used_total=0,
                reserved_total=0,
                updated_at=now,
            )
            .on_conflict_do_nothing(constraint="token_budget_ledger_tenant_month_uniq")
        )
        return (
            await session.execute(
                select(TokenBudgetLedgerRow)
                .where(
                    and_(
                        TokenBudgetLedgerRow.tenant_id == tenant_id,
                        TokenBudgetLedgerRow.month == month,
                    )
                )
                .with_for_update()
            )
        ).scalar_one()

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
        async with self._sf() as session:
            now = _utc_now()
            month = _month_of(now)
            ledger = await self._lock_budget_row(session, tenant_id=tenant_id, month=month, now=now)
            if ledger.budget_total > 0:
                projected = ledger.used_total + ledger.reserved_total + estimated
                if projected > ledger.budget_total:
                    raise BudgetExceededError(tenant_id=tenant_id, month=month)
            row = TokenReservationRow(
                tenant_id=tenant_id,
                agent_name=agent_name,
                thread_id=thread_id,
                parent_thread_id=parent_thread_id,
                model=model,
                estimated=estimated,
                actual=None,
                state=ReservationState.RESERVED.value,
                reserved_at=now,
                closed_at=None,
            )
            session.add(row)
            ledger.reserved_total = ledger.reserved_total + estimated
            ledger.updated_at = now
            await session.commit()
            await session.refresh(row)
            return _row_to_reservation(row)

    async def commit(
        self,
        *,
        reservation_id: UUID,
        tenant_id: UUID,
        actual_tokens: int,
    ) -> TokenReservationRecord:
        async with self._sf() as session:
            row = await self._lock_reservation(
                session, reservation_id=reservation_id, tenant_id=tenant_id
            )
            if row is None:
                raise ReservationNotFoundError(reservation_id=reservation_id)
            if row.state != ReservationState.RESERVED.value:
                return _row_to_reservation(row)
            now = _utc_now()
            row.actual = actual_tokens
            row.state = ReservationState.COMMITTED.value
            row.closed_at = now
            await self._apply_commit_ledger(
                session,
                tenant_id=tenant_id,
                month=_month_of(row.reserved_at),
                used_delta=actual_tokens,
                reserved_delta=-row.estimated,
                now=now,
            )
            await session.commit()
            await session.refresh(row)
            return _row_to_reservation(row)

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
        async with self._sf() as session:
            row = await self._lock_reservation(
                session, reservation_id=reservation_id, tenant_id=tenant_id
            )
            if row is None:
                raise ReservationNotFoundError(reservation_id=reservation_id)
            if row.state != ReservationState.RESERVED.value:
                return _row_to_reservation(row)
            now = _utc_now()
            row.state = new_state.value
            row.closed_at = now
            await self._bump_reserved(
                session,
                tenant_id=tenant_id,
                month=_month_of(row.reserved_at),
                delta=-row.estimated,
                now=now,
            )
            await session.commit()
            await session.refresh(row)
            return _row_to_reservation(row)

    async def expire_reserved(self, *, reservation_id: UUID, tenant_id: UUID) -> bool:
        """Atomically transition ``RESERVED → EXPIRED`` + refund; ``True`` iff won.

        Stream 9.5 — the reaper runs on every instance. ``SELECT … FOR UPDATE``
        serialises competing reapers (and a racing client commit/release) so
        exactly one performs the EXPIRED transition + ledger refund. The loser
        sees the already-closed state and returns ``False`` → its caller skips
        the ``on_expire`` side effect and the expiry count. A missing/cross-tenant
        row also returns ``False`` (already gone — nothing to expire)."""
        async with self._sf() as session:
            row = await self._lock_reservation(
                session, reservation_id=reservation_id, tenant_id=tenant_id
            )
            if row is None or row.state != ReservationState.RESERVED.value:
                return False
            now = _utc_now()
            row.state = ReservationState.EXPIRED.value
            row.closed_at = now
            await self._bump_reserved(
                session,
                tenant_id=tenant_id,
                month=_month_of(row.reserved_at),
                delta=-row.estimated,
                now=now,
            )
            await session.commit()
            return True

    async def list_expired(
        self,
        *,
        max_age_seconds: int,
        limit: int = 100,
    ) -> Sequence[TokenReservationRecord]:
        cutoff = _utc_now() - timedelta(seconds=max_age_seconds)
        stmt = (
            select(TokenReservationRow)
            .where(
                and_(
                    TokenReservationRow.state == ReservationState.RESERVED.value,
                    TokenReservationRow.reserved_at < cutoff,
                )
            )
            .order_by(TokenReservationRow.reserved_at)
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_reservation(r) for r in rows]

    async def get(self, *, reservation_id: UUID, tenant_id: UUID) -> TokenReservationRecord | None:
        async with self._sf() as session:
            row = await session.get(TokenReservationRow, reservation_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return _row_to_reservation(row)

    async def get_budget(self, *, tenant_id: UUID, month: date) -> TenantBudgetRecord | None:
        stmt = select(TokenBudgetLedgerRow).where(
            and_(
                TokenBudgetLedgerRow.tenant_id == tenant_id,
                TokenBudgetLedgerRow.month == month,
            )
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _row_to_budget(row) if row is not None else None

    async def set_budget_total(
        self,
        *,
        tenant_id: UUID,
        month: date,
        budget_total: int,
    ) -> None:
        """Admin helper — set the month's budget cap. Not on the abstract Protocol."""
        now = _utc_now()
        async with self._sf() as session:
            await session.execute(
                pg_insert(TokenBudgetLedgerRow)
                .values(
                    tenant_id=tenant_id,
                    month=month,
                    budget_total=budget_total,
                    used_total=0,
                    reserved_total=0,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    constraint="token_budget_ledger_tenant_month_uniq",
                    set_={"budget_total": budget_total, "updated_at": now},
                )
            )
            await session.commit()
