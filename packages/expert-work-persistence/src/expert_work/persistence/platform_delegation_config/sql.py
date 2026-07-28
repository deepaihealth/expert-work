"""SQLAlchemy-backed :class:`PlatformDelegationConfigStore` — perf phase2 PR3.

Single-row singleton (``id == "singleton"``), tenant-less. Callers MUST wrap
calls in ``bypass_rls_session()`` (no RLS policy on the table). Mirrors
:class:`SqlPlatformDynamicWorkerConfigStore`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import PlatformDelegationConfigRow as _Model
from expert_work.persistence.platform_delegation_config.base import (
    PlatformDelegationConfigRow,
    PlatformDelegationConfigStore,
)

_SINGLETON_ID = "singleton"


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _record(row: _Model) -> PlatformDelegationConfigRow:
    return PlatformDelegationConfigRow(
        max_concurrent_delegations=row.max_concurrent_delegations,
        updated_by=row.updated_by,
    )


class SqlPlatformDelegationConfigStore(PlatformDelegationConfigStore):
    """Postgres-backed single-row platform delegation config repository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def get(self) -> PlatformDelegationConfigRow | None:
        async with self._sf() as session:
            row = (
                await session.execute(select(_Model).where(_Model.id == _SINGLETON_ID))
            ).scalar_one_or_none()
        return _record(row) if row is not None else None

    async def put(self, *, max_concurrent_delegations: int, updated_by: str | None) -> None:
        now = _utc_now()
        async with self._sf() as session:
            stmt = (
                pg_insert(_Model)
                .values(
                    id=_SINGLETON_ID,
                    max_concurrent_delegations=max_concurrent_delegations,
                    updated_at=now,
                    updated_by=updated_by,
                )
                .on_conflict_do_update(
                    index_elements=["id"],
                    set_={
                        "max_concurrent_delegations": max_concurrent_delegations,
                        "updated_at": now,
                        "updated_by": updated_by,
                    },
                )
            )
            await session.execute(stmt)
            await session.commit()
