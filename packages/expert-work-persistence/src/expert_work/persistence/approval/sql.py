"""SQLAlchemy-backed ``ApprovalStore`` — Stream J.8 (Mini-ADR J-24)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.approval.base import ApprovalStore
from expert_work.persistence.models import AgentApprovalRow
from expert_work.protocol import ApprovalReasonKind, ApprovalRecord, ApprovalStatus


def _row_to_dto(row: AgentApprovalRow) -> ApprovalRecord:
    return ApprovalRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        run_id=row.run_id,
        thread_id=row.thread_id,
        request_id=row.request_id,
        node=row.node,
        reason_kind=cast(ApprovalReasonKind, row.reason_kind),
        action_summary=row.action_summary,
        proposed_args=dict(row.proposed_args or {}),
        requested_at=row.requested_at,
        timeout_at=row.timeout_at,
        binding_digest=row.binding_digest or "",
        status=ApprovalStatus(row.status),
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        modified_args=dict(row.modified_args) if row.modified_args is not None else None,
        idempotency_key=row.idempotency_key,
        continuation_run_id=row.continuation_run_id,
    )


class SqlApprovalStore(ApprovalStore):
    """Postgres-backed paused-run approval registry."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(self, record: ApprovalRecord) -> ApprovalRecord:
        async with self._sf() as session:
            session.add(
                AgentApprovalRow(
                    id=record.id,
                    tenant_id=record.tenant_id,
                    user_id=record.user_id,
                    run_id=record.run_id,
                    thread_id=record.thread_id,
                    request_id=record.request_id,
                    node=record.node,
                    reason_kind=record.reason_kind,
                    action_summary=record.action_summary,
                    proposed_args=dict(record.proposed_args),
                    requested_at=record.requested_at,
                    timeout_at=record.timeout_at,
                    binding_digest=record.binding_digest or None,
                    status=record.status.value,
                    decided_by=record.decided_by,
                    decided_at=record.decided_at,
                    modified_args=record.modified_args,
                )
            )
            await session.commit()
        return record

    async def get_by_run(self, *, run_id: UUID, tenant_id: UUID) -> ApprovalRecord | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(AgentApprovalRow).where(
                        AgentApprovalRow.run_id == run_id,
                        AgentApprovalRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        return _row_to_dto(row) if row is not None else None

    async def list_expired(
        self,
        *,
        before: datetime,
        limit: int = 1000,
    ) -> list[ApprovalRecord]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(AgentApprovalRow)
                        .where(
                            AgentApprovalRow.status == ApprovalStatus.PENDING.value,
                            AgentApprovalRow.timeout_at < before,
                        )
                        .order_by(AgentApprovalRow.timeout_at.asc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_dto(r) for r in rows]

    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        status: ApprovalStatus,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ApprovalRecord], int]:
        async with self._sf() as session:
            base = select(AgentApprovalRow).where(
                AgentApprovalRow.tenant_id == tenant_id,
                AgentApprovalRow.status == status.value,
            )
            total = int(
                (
                    await session.execute(select(func.count()).select_from(base.subquery()))
                ).scalar_one()
            )
            rows = (
                (
                    await session.execute(
                        base.order_by(AgentApprovalRow.requested_at.asc())
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_dto(r) for r in rows], total

    async def list_all_tenants(
        self,
        *,
        status: ApprovalStatus,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ApprovalRecord], int]:
        async with self._sf() as session:
            base = select(AgentApprovalRow).where(AgentApprovalRow.status == status.value)
            total = int(
                (
                    await session.execute(select(func.count()).select_from(base.subquery()))
                ).scalar_one()
            )
            rows = (
                (
                    await session.execute(
                        base.order_by(AgentApprovalRow.requested_at.asc())
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_dto(r) for r in rows], total

    async def count_pending(self) -> int:
        async with self._sf() as session:
            result = await session.execute(
                select(func.count())
                .select_from(AgentApprovalRow)
                .where(AgentApprovalRow.status == ApprovalStatus.PENDING.value)
            )
            return int(result.scalar_one())

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
        values: dict[str, object | None] = {
            "status": status.value,
            "decided_by": decided_by,
            "decided_at": decided_at,
            "modified_args": modified_args,
            # Stream 13.2 — bound atomically to the winning CAS.
            "idempotency_key": idempotency_key,
            "continuation_run_id": continuation_run_id,
        }
        # RT-6 Tier A (RT-ADR-19) — a modify re-binds to the digest of
        # modified_args, written atomically with the CAS; approve / reject pass
        # None and keep the mint-time digest.
        if binding_digest is not None:
            values["binding_digest"] = binding_digest
        async with self._sf() as session:
            result = await session.execute(
                update(AgentApprovalRow)
                .where(
                    AgentApprovalRow.run_id == run_id,
                    AgentApprovalRow.tenant_id == tenant_id,
                    AgentApprovalRow.status == ApprovalStatus.PENDING.value,
                )
                .values(**values)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        stmt = delete(AgentApprovalRow).where(
            AgentApprovalRow.tenant_id == tenant_id,
            AgentApprovalRow.user_id == user_id,
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def delete_for_threads(self, *, thread_ids: Sequence[UUID], tenant_id: UUID) -> int:
        if not thread_ids:
            return 0
        stmt = delete(AgentApprovalRow).where(
            AgentApprovalRow.tenant_id == tenant_id,
            AgentApprovalRow.thread_id.in_(list(thread_ids)),
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)
