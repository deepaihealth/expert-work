"""SQLAlchemy-backed :class:`PlatformDynamicWorkerConfigStore` — B3 PR2.

Single-row singleton (``id == "singleton"``), tenant-less. Callers MUST wrap
calls in ``bypass_rls_session()`` (no RLS policy on the table). Mirrors
:class:`SqlPlatformToolBudgetConfigStore`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import PlatformDynamicWorkerConfigRow as _Model
from expert_work.persistence.platform_dynamic_worker_config.base import (
    PlatformDynamicWorkerConfigRow,
    PlatformDynamicWorkerConfigStore,
)

_SINGLETON_ID = "singleton"


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _record(row: _Model) -> PlatformDynamicWorkerConfigRow:
    return PlatformDynamicWorkerConfigRow(
        max_concurrent=row.max_concurrent,
        max_per_run=row.max_per_run,
        max_iterations=row.max_iterations,
        cap_max_concurrent=row.cap_max_concurrent,
        cap_max_per_run=row.cap_max_per_run,
        cap_max_iterations=row.cap_max_iterations,
        updated_by=row.updated_by,
    )


class SqlPlatformDynamicWorkerConfigStore(PlatformDynamicWorkerConfigStore):
    """Postgres-backed single-row platform dynamic-worker config repository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def get(self) -> PlatformDynamicWorkerConfigRow | None:
        async with self._sf() as session:
            row = (
                await session.execute(select(_Model).where(_Model.id == _SINGLETON_ID))
            ).scalar_one_or_none()
        return _record(row) if row is not None else None

    async def put(
        self,
        *,
        max_concurrent: int,
        max_per_run: int,
        max_iterations: int,
        cap_max_concurrent: int,
        cap_max_per_run: int,
        cap_max_iterations: int,
        updated_by: str | None,
    ) -> None:
        now = _utc_now()
        values = {
            "max_concurrent": max_concurrent,
            "max_per_run": max_per_run,
            "max_iterations": max_iterations,
            "cap_max_concurrent": cap_max_concurrent,
            "cap_max_per_run": cap_max_per_run,
            "cap_max_iterations": cap_max_iterations,
            "updated_at": now,
            "updated_by": updated_by,
        }
        async with self._sf() as session:
            stmt = (
                pg_insert(_Model)
                .values(id=_SINGLETON_ID, **values)
                .on_conflict_do_update(index_elements=["id"], set_=values)
            )
            await session.execute(stmt)
            await session.commit()
