"""SQLAlchemy-backed ``TriggerStore`` — Stream J.10 (Mini-ADR J-26 / J-42)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import AgentTriggerRow, TriggerRunRow
from expert_work.persistence.trigger.base import TriggerRunStore, TriggerStore
from expert_work.protocol import (
    ContextMode,
    TriggerKind,
    TriggerRecord,
    TriggerRunRecord,
    TriggerRunStatus,
    TriggerSource,
)


def _row_to_dto(row: AgentTriggerRow) -> TriggerRecord:
    return TriggerRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        agent_name=row.agent_name,
        agent_version=row.agent_version,
        name=row.name,
        kind=cast(TriggerKind, row.kind),
        config=dict(row.config or {}),
        enabled=row.enabled,
        source=cast(TriggerSource, row.source),
        webhook_secret_hash=row.webhook_secret_hash,
        originating_thread_id=row.originating_thread_id,
        context_mode=cast(ContextMode, row.context_mode),
        last_fired_at=row.last_fired_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlTriggerStore(TriggerStore):
    """Postgres-backed trigger registry — the ``agent_trigger`` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(self, record: TriggerRecord) -> TriggerRecord:
        async with self._sf() as session:
            session.add(
                AgentTriggerRow(
                    id=record.id,
                    tenant_id=record.tenant_id,
                    user_id=record.user_id,
                    agent_name=record.agent_name,
                    agent_version=record.agent_version,
                    name=record.name,
                    kind=record.kind,
                    config=dict(record.config),
                    enabled=record.enabled,
                    source=record.source,
                    webhook_secret_hash=record.webhook_secret_hash,
                    originating_thread_id=record.originating_thread_id,
                    context_mode=record.context_mode,
                    last_fired_at=record.last_fired_at,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
            )
            await session.commit()
        return record

    async def get(self, *, trigger_id: UUID, tenant_id: UUID) -> TriggerRecord | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(AgentTriggerRow).where(
                        AgentTriggerRow.id == trigger_id,
                        AgentTriggerRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        return _row_to_dto(row) if row is not None else None

    async def list_by_agent(self, *, tenant_id: UUID, agent_name: str) -> list[TriggerRecord]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(AgentTriggerRow)
                        .where(
                            AgentTriggerRow.tenant_id == tenant_id,
                            AgentTriggerRow.agent_name == agent_name,
                        )
                        .order_by(AgentTriggerRow.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_dto(r) for r in rows]

    async def list_by_tenant(
        self,
        *,
        tenant_id: UUID,
        agent_name: str | None = None,
        agent_version: str | None = None,
    ) -> list[TriggerRecord]:
        stmt = (
            select(AgentTriggerRow)
            .where(AgentTriggerRow.tenant_id == tenant_id)
            .order_by(AgentTriggerRow.created_at.asc())
        )
        if agent_name is not None:
            stmt = stmt.where(AgentTriggerRow.agent_name == agent_name)
        if agent_version is not None:
            stmt = stmt.where(AgentTriggerRow.agent_version == agent_version)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_dto(r) for r in rows]

    async def list_by_user(
        self, *, tenant_id: UUID, user_id: UUID, agent_name: str | None = None
    ) -> list[TriggerRecord]:
        async with self._sf() as session:
            stmt = select(AgentTriggerRow).where(
                AgentTriggerRow.tenant_id == tenant_id,
                AgentTriggerRow.user_id == user_id,
            )
            if agent_name is not None:
                stmt = stmt.where(AgentTriggerRow.agent_name == agent_name)
            stmt = stmt.order_by(AgentTriggerRow.created_at.asc())
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_dto(r) for r in rows]

    async def list_all_tenants(
        self,
        *,
        agent_name: str | None = None,
        agent_version: str | None = None,
    ) -> list[TriggerRecord]:
        # Stream N — no tenant filter; caller must wrap in bypass_rls_session().
        stmt = select(AgentTriggerRow).order_by(AgentTriggerRow.created_at.asc())
        if agent_name is not None:
            stmt = stmt.where(AgentTriggerRow.agent_name == agent_name)
        if agent_version is not None:
            stmt = stmt.where(AgentTriggerRow.agent_version == agent_version)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_dto(r) for r in rows]

    async def list_enabled_cron(self) -> list[TriggerRecord]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(AgentTriggerRow)
                        .where(
                            AgentTriggerRow.kind == "cron",
                            AgentTriggerRow.enabled.is_(True),
                        )
                        .order_by(AgentTriggerRow.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_dto(r) for r in rows]

    async def update(self, record: TriggerRecord) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                sa_update(AgentTriggerRow)
                .where(
                    AgentTriggerRow.id == record.id,
                    AgentTriggerRow.tenant_id == record.tenant_id,
                )
                .values(
                    user_id=record.user_id,
                    agent_version=record.agent_version,
                    name=record.name,
                    kind=record.kind,
                    config=dict(record.config),
                    enabled=record.enabled,
                    source=record.source,
                    webhook_secret_hash=record.webhook_secret_hash,
                    originating_thread_id=record.originating_thread_id,
                    context_mode=record.context_mode,
                    last_fired_at=record.last_fired_at,
                    updated_at=record.updated_at,
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def disable_for_agent(
        self, *, agent_name: str, agent_version: str, tenant_id: UUID
    ) -> int:
        """Deletion hygiene PR4 §B — agent soft-delete cascade.

        Predicates: ``enabled == true AND agent_name == AND agent_version ==
        AND tenant_id ==``; sets ``enabled=false``; returns rows changed.
        """
        stmt = (
            sa_update(AgentTriggerRow)
            .where(
                AgentTriggerRow.tenant_id == tenant_id,
                AgentTriggerRow.agent_name == agent_name,
                AgentTriggerRow.agent_version == agent_version,
                AgentTriggerRow.enabled.is_(True),
            )
            .values(enabled=False, updated_at=func.now())
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def claim_cron_fire(
        self,
        *,
        trigger_id: UUID,
        tenant_id: UUID,
        expected_last_fired_at: datetime | None,
        new_last_fired_at: datetime,
    ) -> bool:
        # CAS on the cron slot — stamp ``last_fired_at`` only while it still
        # equals what the caller read. ``IS NOT DISTINCT FROM`` makes a
        # never-fired trigger (NULL) match ``expected=None``. Exactly one
        # instance's UPDATE matches a row; the loser gets rowcount 0.
        async with self._sf() as session:
            result = await session.execute(
                sa_update(AgentTriggerRow)
                .where(
                    AgentTriggerRow.id == trigger_id,
                    AgentTriggerRow.tenant_id == tenant_id,
                    AgentTriggerRow.last_fired_at.is_not_distinct_from(expected_last_fired_at),
                )
                .values(last_fired_at=new_last_fired_at, updated_at=new_last_fired_at)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def delete(self, *, trigger_id: UUID, tenant_id: UUID) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                sa_delete(AgentTriggerRow).where(
                    AgentTriggerRow.id == trigger_id,
                    AgentTriggerRow.tenant_id == tenant_id,
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> list[UUID]:
        stmt = (
            sa_delete(AgentTriggerRow)
            .where(
                AgentTriggerRow.tenant_id == tenant_id,
                AgentTriggerRow.user_id == user_id,
            )
            .returning(AgentTriggerRow.id)
        )
        async with self._sf() as session:
            deleted = (await session.execute(stmt)).scalars().all()
            await session.commit()
        return list(deleted)

    async def get_for_webhook(self, *, trigger_id: UUID) -> TriggerRecord | None:
        # Tenant-unscoped — RLS is bypassed by the caller's contextvar.
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(AgentTriggerRow).where(AgentTriggerRow.id == trigger_id)
                )
            ).scalar_one_or_none()
        return _row_to_dto(row) if row is not None else None

    async def count_cron_by_tenant(self, *, tenant_id: UUID) -> int:
        async with self._sf() as session:
            result = await session.execute(
                select(func.count())
                .select_from(AgentTriggerRow)
                .where(
                    AgentTriggerRow.tenant_id == tenant_id,
                    AgentTriggerRow.kind == "cron",
                )
            )
        return int(result.scalar_one())


def _run_row_to_dto(row: TriggerRunRow) -> TriggerRunRecord:
    return TriggerRunRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        trigger_id=row.trigger_id,
        run_id=row.run_id,
        status=TriggerRunStatus(row.status),
        attempt=row.attempt,
        next_retry_at=row.next_retry_at,
        error=row.error,
        triggered_at=row.triggered_at,
    )


class SqlTriggerRunStore(TriggerRunStore):
    """Postgres-backed trigger-firing registry — the ``trigger_run`` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(self, record: TriggerRunRecord) -> TriggerRunRecord:
        async with self._sf() as session:
            session.add(
                TriggerRunRow(
                    id=record.id,
                    tenant_id=record.tenant_id,
                    trigger_id=record.trigger_id,
                    run_id=record.run_id,
                    status=record.status.value,
                    attempt=record.attempt,
                    next_retry_at=record.next_retry_at,
                    error=record.error,
                    triggered_at=record.triggered_at,
                )
            )
            await session.commit()
        return record

    async def get(self, *, trigger_run_id: UUID, tenant_id: UUID) -> TriggerRunRecord | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(TriggerRunRow).where(
                        TriggerRunRow.id == trigger_run_id,
                        TriggerRunRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        return _run_row_to_dto(row) if row is not None else None

    async def update(self, record: TriggerRunRecord) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                sa_update(TriggerRunRow)
                .where(
                    TriggerRunRow.id == record.id,
                    TriggerRunRow.tenant_id == record.tenant_id,
                )
                .values(
                    run_id=record.run_id,
                    status=record.status.value,
                    attempt=record.attempt,
                    next_retry_at=record.next_retry_at,
                    error=record.error,
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def claim_retry(self, *, trigger_run_id: UUID, tenant_id: UUID) -> bool:
        # CAS ``retrying`` → ``fired`` (clear the backoff) so exactly one
        # instance re-fires this firing; the loser's UPDATE matches no row.
        async with self._sf() as session:
            result = await session.execute(
                sa_update(TriggerRunRow)
                .where(
                    TriggerRunRow.id == trigger_run_id,
                    TriggerRunRow.tenant_id == tenant_id,
                    TriggerRunRow.status == TriggerRunStatus.RETRYING.value,
                )
                .values(status=TriggerRunStatus.FIRED.value, next_retry_at=None)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def claim_reconcile(self, record: TriggerRunRecord) -> bool:
        # CAS ``fired`` → ``record.status`` so exactly one of {scheduler
        # reconcile, fire-now endpoint} finalizes this firing; the loser's
        # UPDATE matches no row (status already moved off ``fired``).
        async with self._sf() as session:
            result = await session.execute(
                sa_update(TriggerRunRow)
                .where(
                    TriggerRunRow.id == record.id,
                    TriggerRunRow.tenant_id == record.tenant_id,
                    TriggerRunRow.status == TriggerRunStatus.FIRED.value,
                )
                .values(
                    run_id=record.run_id,
                    status=record.status.value,
                    attempt=record.attempt,
                    next_retry_at=record.next_retry_at,
                    error=record.error,
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def list_by_trigger(self, *, trigger_id: UUID, tenant_id: UUID) -> list[TriggerRunRecord]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(TriggerRunRow)
                        .where(
                            TriggerRunRow.trigger_id == trigger_id,
                            TriggerRunRow.tenant_id == tenant_id,
                        )
                        .order_by(TriggerRunRow.triggered_at.desc())
                    )
                )
                .scalars()
                .all()
            )
        return [_run_row_to_dto(r) for r in rows]

    async def delete_for_triggers(self, *, trigger_ids: Sequence[UUID], tenant_id: UUID) -> int:
        ids = list(trigger_ids)
        if not ids:
            return 0
        stmt = sa_delete(TriggerRunRow).where(
            TriggerRunRow.tenant_id == tenant_id,
            TriggerRunRow.trigger_id.in_(ids),
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def list_fired(self, *, limit: int = 1000) -> list[TriggerRunRecord]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(TriggerRunRow)
                        .where(TriggerRunRow.status == TriggerRunStatus.FIRED.value)
                        .order_by(TriggerRunRow.triggered_at.asc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [_run_row_to_dto(r) for r in rows]

    async def list_due_retries(
        self, *, before: datetime, limit: int = 1000
    ) -> list[TriggerRunRecord]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(TriggerRunRow)
                        .where(
                            TriggerRunRow.status == TriggerRunStatus.RETRYING.value,
                            TriggerRunRow.next_retry_at <= before,
                        )
                        .order_by(TriggerRunRow.next_retry_at.asc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [_run_row_to_dto(r) for r in rows]
