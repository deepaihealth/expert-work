"""SQLAlchemy-backed curation stores — Stream J.12 (Mini-ADR J-43)."""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.curation.base import CurationCandidateStore, EvalDatasetStore
from expert_work.persistence.models import CurationCandidateRow, EvalDatasetRow
from expert_work.protocol import (
    CandidateStatus,
    CurationCandidateRecord,
    CurationSignal,
    EvalDatasetRecord,
    EvalDatasetSource,
    FeedbackRating,
    TrajectoryOutcome,
)


def _row_to_dto(row: EvalDatasetRow) -> EvalDatasetRecord:
    return EvalDatasetRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_name=row.agent_name,
        name=row.name,
        input=dict(row.input or {}),
        expected=dict(row.expected) if row.expected is not None else None,
        source=cast(EvalDatasetSource, row.source),
        source_trajectory_key=row.source_trajectory_key,
        source_user_id=row.source_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlEvalDatasetStore(EvalDatasetStore):
    """Postgres-backed curated-case registry — the ``eval_dataset`` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(self, record: EvalDatasetRecord) -> EvalDatasetRecord:
        async with self._sf() as session:
            session.add(
                EvalDatasetRow(
                    id=record.id,
                    tenant_id=record.tenant_id,
                    agent_name=record.agent_name,
                    name=record.name,
                    input=dict(record.input),
                    expected=dict(record.expected) if record.expected is not None else None,
                    source=record.source,
                    source_trajectory_key=record.source_trajectory_key,
                    source_user_id=record.source_user_id,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
            )
            await session.commit()
        return record

    async def get(self, *, dataset_id: UUID, tenant_id: UUID) -> EvalDatasetRecord | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(EvalDatasetRow).where(
                        EvalDatasetRow.id == dataset_id,
                        EvalDatasetRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        return _row_to_dto(row) if row is not None else None

    async def list_by_agent(self, *, tenant_id: UUID, agent_name: str) -> list[EvalDatasetRecord]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(EvalDatasetRow)
                        .where(
                            EvalDatasetRow.tenant_id == tenant_id,
                            EvalDatasetRow.agent_name == agent_name,
                        )
                        .order_by(EvalDatasetRow.created_at.asc())
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_dto(r) for r in rows]

    async def list_by_tenant(self, *, tenant_id: UUID) -> list[EvalDatasetRecord]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(EvalDatasetRow)
                        .where(EvalDatasetRow.tenant_id == tenant_id)
                        .order_by(EvalDatasetRow.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_dto(r) for r in rows]

    async def list_all_tenants(self) -> list[EvalDatasetRecord]:
        # Stream N — no tenant filter; caller must wrap in bypass_rls_session().
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(EvalDatasetRow).order_by(EvalDatasetRow.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_dto(r) for r in rows]

    async def update(self, record: EvalDatasetRecord) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                sa_update(EvalDatasetRow)
                .where(
                    EvalDatasetRow.id == record.id,
                    EvalDatasetRow.tenant_id == record.tenant_id,
                )
                .values(
                    agent_name=record.agent_name,
                    name=record.name,
                    input=dict(record.input),
                    expected=dict(record.expected) if record.expected is not None else None,
                    source=record.source,
                    source_trajectory_key=record.source_trajectory_key,
                    source_user_id=record.source_user_id,
                    updated_at=record.updated_at,
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def delete(self, *, dataset_id: UUID, tenant_id: UUID) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                sa_delete(EvalDatasetRow).where(
                    EvalDatasetRow.id == dataset_id,
                    EvalDatasetRow.tenant_id == tenant_id,
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def count_by_tenant(self, *, tenant_id: UUID) -> int:
        async with self._sf() as session:
            result = await session.execute(
                select(func.count())
                .select_from(EvalDatasetRow)
                .where(EvalDatasetRow.tenant_id == tenant_id)
            )
        return int(result.scalar_one())

    async def anonymize_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        stmt = (
            sa_update(EvalDatasetRow)
            .where(
                EvalDatasetRow.tenant_id == tenant_id,
                EvalDatasetRow.source_user_id == user_id,
            )
            .values(source_user_id=None)
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)


def _candidate_row_to_dto(row: CurationCandidateRow) -> CurationCandidateRecord:
    return CurationCandidateRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_name=row.agent_name,
        agent_version=row.agent_version,
        thread_id=row.thread_id,
        user_id=row.user_id,
        trajectory_key=row.trajectory_key,
        outcome=cast(TrajectoryOutcome, row.outcome),
        signal=cast(CurationSignal, row.signal),
        feedback_rating=cast(FeedbackRating | None, row.feedback_rating),
        status=CandidateStatus(row.status),
        eval_dataset_id=row.eval_dataset_id,
        detected_at=row.detected_at,
        reviewed_at=row.reviewed_at,
        evolved_at=row.evolved_at,
        retry_count=row.retry_count,
    )


class SqlCurationCandidateStore(CurationCandidateStore):
    """Postgres-backed curation-candidate registry — the ``curation_candidate`` table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def upsert(self, record: CurationCandidateRecord) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                pg_insert(CurationCandidateRow)
                .values(
                    id=record.id,
                    tenant_id=record.tenant_id,
                    agent_name=record.agent_name,
                    agent_version=record.agent_version,
                    thread_id=record.thread_id,
                    user_id=record.user_id,
                    trajectory_key=record.trajectory_key,
                    outcome=record.outcome,
                    signal=record.signal,
                    feedback_rating=record.feedback_rating,
                    status=record.status.value,
                    eval_dataset_id=record.eval_dataset_id,
                    detected_at=record.detected_at,
                    reviewed_at=record.reviewed_at,
                )
                .on_conflict_do_nothing(constraint="curation_candidate_trajectory_uniq")
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def get(self, *, candidate_id: UUID, tenant_id: UUID) -> CurationCandidateRecord | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(CurationCandidateRow).where(
                        CurationCandidateRow.id == candidate_id,
                        CurationCandidateRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
        return _candidate_row_to_dto(row) if row is not None else None

    async def get_by_trajectory_key(
        self, *, tenant_id: UUID, trajectory_key: str
    ) -> CurationCandidateRecord | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(CurationCandidateRow).where(
                        CurationCandidateRow.tenant_id == tenant_id,
                        CurationCandidateRow.trajectory_key == trajectory_key,
                    )
                )
            ).scalar_one_or_none()
        return _candidate_row_to_dto(row) if row is not None else None

    async def list_for_review(
        self,
        *,
        tenant_id: UUID,
        agent_name: str | None = None,
        status: CandidateStatus | None = None,
        signal: CurationSignal | None = None,
    ) -> list[CurationCandidateRecord]:
        return await self._list_for_review(
            tenant_id=tenant_id, agent_name=agent_name, status=status, signal=signal
        )

    async def list_for_review_all_tenants(
        self,
        *,
        agent_name: str | None = None,
        status: CandidateStatus | None = None,
        signal: CurationSignal | None = None,
        unevolved_only: bool = False,
    ) -> list[CurationCandidateRecord]:
        # Stream N — no tenant filter; caller must wrap in bypass_rls_session().
        return await self._list_for_review(
            tenant_id=None,
            agent_name=agent_name,
            status=status,
            signal=signal,
            unevolved_only=unevolved_only,
        )

    async def mark_evolved(self, *, candidate_id: UUID, tenant_id: UUID, at: datetime) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                sa_update(CurationCandidateRow)
                .where(
                    CurationCandidateRow.id == candidate_id,
                    CurationCandidateRow.tenant_id == tenant_id,
                )
                .values(evolved_at=at)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def record_retry(self, *, candidate_id: UUID, tenant_id: UUID) -> int:
        # SE-16 (SE-A40) — atomic increment; RETURNING avoids a read race
        # between concurrent worker instances.
        async with self._sf() as session:
            result = await session.execute(
                sa_update(CurationCandidateRow)
                .where(
                    CurationCandidateRow.id == candidate_id,
                    CurationCandidateRow.tenant_id == tenant_id,
                )
                .values(retry_count=CurationCandidateRow.retry_count + 1)
                .returning(CurationCandidateRow.retry_count)
            )
            await session.commit()
            value = result.scalar_one_or_none()
        return int(value) if value is not None else 0

    async def _list_for_review(
        self,
        *,
        tenant_id: UUID | None,
        agent_name: str | None,
        status: CandidateStatus | None,
        signal: CurationSignal | None,
        unevolved_only: bool = False,
    ) -> list[CurationCandidateRecord]:
        stmt = select(CurationCandidateRow)
        if tenant_id is not None:
            stmt = stmt.where(CurationCandidateRow.tenant_id == tenant_id)
        if agent_name is not None:
            stmt = stmt.where(CurationCandidateRow.agent_name == agent_name)
        if status is not None:
            stmt = stmt.where(CurationCandidateRow.status == status.value)
        if signal is not None:
            stmt = stmt.where(CurationCandidateRow.signal == signal)
        if unevolved_only:
            stmt = stmt.where(CurationCandidateRow.evolved_at.is_(None))
        stmt = stmt.order_by(CurationCandidateRow.detected_at.desc())
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_candidate_row_to_dto(r) for r in rows]

    async def update(self, record: CurationCandidateRecord) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                sa_update(CurationCandidateRow)
                .where(
                    CurationCandidateRow.id == record.id,
                    CurationCandidateRow.tenant_id == record.tenant_id,
                )
                .values(
                    agent_name=record.agent_name,
                    agent_version=record.agent_version,
                    user_id=record.user_id,
                    outcome=record.outcome,
                    signal=record.signal,
                    feedback_rating=record.feedback_rating,
                    status=record.status.value,
                    eval_dataset_id=record.eval_dataset_id,
                    reviewed_at=record.reviewed_at,
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def revert_promoted_for_dataset(self, *, dataset_id: UUID, tenant_id: UUID) -> int:
        stmt = (
            sa_update(CurationCandidateRow)
            .where(
                CurationCandidateRow.tenant_id == tenant_id,
                CurationCandidateRow.eval_dataset_id == dataset_id,
                CurationCandidateRow.status == CandidateStatus.PROMOTED.value,
            )
            .values(status=CandidateStatus.PENDING.value, eval_dataset_id=None)
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def anonymize_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        stmt = (
            sa_update(CurationCandidateRow)
            .where(
                CurationCandidateRow.tenant_id == tenant_id,
                CurationCandidateRow.user_id == user_id,
            )
            .values(user_id=None)
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)
