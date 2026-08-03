"""SQLAlchemy-backed eval-run store — P1-S2.1."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.eval.base import EvalRunStore
from expert_work.persistence.models import EvalCaseResultRow, EvalRunRow
from expert_work.protocol import (
    EvalCaseResultRecord,
    EvalRunRecord,
    EvalRunStatus,
    EvalTriggeredBy,
)

_TERMINAL = {EvalRunStatus.PASSED, EvalRunStatus.FAILED, EvalRunStatus.ERROR}


def _run_to_dto(row: EvalRunRow) -> EvalRunRecord:
    return EvalRunRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        suite=row.suite,
        status=EvalRunStatus(row.status),
        triggered_by=EvalTriggeredBy(row.triggered_by),
        summary=dict(row.summary) if row.summary is not None else None,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _case_to_dto(row: EvalCaseResultRow) -> EvalCaseResultRecord:
    return EvalCaseResultRecord(
        id=row.id,
        run_id=row.run_id,
        tenant_id=row.tenant_id,
        capability=row.capability,
        case_id=row.case_id,
        passed=row.passed,
        session_id=row.session_id,
        scores=dict(row.scores or {}),
        session_metrics=dict(row.session_metrics) if row.session_metrics is not None else None,
    )


class SqlEvalRunStore(EvalRunStore):
    """Postgres-backed eval-run registry — ``eval_run`` + ``eval_case_result``."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create_run(self, record: EvalRunRecord) -> EvalRunRecord:
        async with self._sf() as session:
            session.add(
                EvalRunRow(
                    id=record.id,
                    tenant_id=record.tenant_id,
                    suite=record.suite,
                    status=record.status.value,
                    triggered_by=record.triggered_by.value,
                    summary=dict(record.summary) if record.summary is not None else None,
                    created_at=record.created_at,
                    started_at=record.started_at,
                    finished_at=record.finished_at,
                )
            )
            await session.commit()
        return record

    async def get_run(self, *, run_id: UUID, tenant_id: UUID) -> EvalRunRecord | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(EvalRunRow).where(
                        EvalRunRow.id == run_id,
                        EvalRunRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            return _run_to_dto(row) if row is not None else None

    async def set_status(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        status: EvalRunStatus,
        summary: dict[str, object] | None = None,
    ) -> bool:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(EvalRunRow).where(
                        EvalRunRow.id == run_id,
                        EvalRunRow.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            now = datetime.now(UTC)
            row.status = status.value
            if status is EvalRunStatus.RUNNING and row.started_at is None:
                row.started_at = now
            if status in _TERMINAL:
                row.finished_at = now
            if summary is not None:
                row.summary = summary
            await session.commit()
            return True

    async def claim(self, *, run_id: UUID, tenant_id: UUID) -> bool:
        # Atomic CAS — exactly one worker wins. ``status='queued'`` is the
        # guard; the loser's UPDATE matches zero rows (already running).
        async with self._sf() as session:
            result = await session.execute(
                update(EvalRunRow)
                .where(
                    EvalRunRow.id == run_id,
                    EvalRunRow.tenant_id == tenant_id,
                    EvalRunRow.status == EvalRunStatus.QUEUED.value,
                )
                .values(status=EvalRunStatus.RUNNING.value, started_at=datetime.now(UTC))
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def list_by_status_all_tenants(self, status: EvalRunStatus) -> list[EvalRunRecord]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(EvalRunRow)
                        .where(EvalRunRow.status == status.value)
                        .order_by(EvalRunRow.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return [_run_to_dto(r) for r in rows]

    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        status: EvalRunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[EvalRunRecord], int]:
        async with self._sf() as session:
            where = [EvalRunRow.tenant_id == tenant_id]
            if status is not None:
                where.append(EvalRunRow.status == status.value)
            total = (
                await session.execute(select(func.count()).select_from(EvalRunRow).where(*where))
            ).scalar_one()
            rows = (
                (
                    await session.execute(
                        select(EvalRunRow)
                        .where(*where)
                        .order_by(EvalRunRow.created_at.desc())
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .scalars()
                .all()
            )
            return [_run_to_dto(r) for r in rows], int(total)

    async def list_all_tenants(
        self,
        *,
        status: EvalRunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[EvalRunRecord], int]:
        # W4 — no tenant filter; caller must wrap in bypass_rls_session().
        stmt = select(EvalRunRow)
        count_stmt = select(func.count()).select_from(EvalRunRow)
        if status is not None:
            stmt = stmt.where(EvalRunRow.status == status.value)
            count_stmt = count_stmt.where(EvalRunRow.status == status.value)
        async with self._sf() as session:
            total = (await session.execute(count_stmt)).scalar_one()
            rows = (
                (
                    await session.execute(
                        stmt.order_by(EvalRunRow.created_at.desc(), EvalRunRow.id)
                        .limit(limit)
                        .offset(offset)
                    )
                )
                .scalars()
                .all()
            )
            return [_run_to_dto(r) for r in rows], int(total)

    async def append_case_result(self, record: EvalCaseResultRecord) -> EvalCaseResultRecord:
        async with self._sf() as session:
            row = EvalCaseResultRow(
                run_id=record.run_id,
                tenant_id=record.tenant_id,
                capability=record.capability,
                case_id=record.case_id,
                passed=record.passed,
                session_id=record.session_id,
                scores=dict(record.scores),
                session_metrics=(
                    dict(record.session_metrics) if record.session_metrics is not None else None
                ),
                created_at=datetime.now(UTC),
            )
            session.add(row)
            await session.commit()
            return record.model_copy(update={"id": row.id})

    async def list_case_results(
        self, *, run_id: UUID, tenant_id: UUID
    ) -> list[EvalCaseResultRecord]:
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(EvalCaseResultRow)
                        .where(
                            EvalCaseResultRow.run_id == run_id,
                            EvalCaseResultRow.tenant_id == tenant_id,
                        )
                        .order_by(EvalCaseResultRow.id)
                    )
                )
                .scalars()
                .all()
            )
            return [_case_to_dto(r) for r in rows]
