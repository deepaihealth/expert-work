"""SQLAlchemy-backed :class:`PlatformRecognitionConfigStore` — B-209.

Single-row singleton (``id == "singleton"``), tenant-less, no RLS policy
(migration ``0169``). Mirrors :class:`SqlPlatformJudgeConfigStore`.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import PlatformRecognitionConfigRow as _Model
from expert_work.persistence.platform_recognition_config.base import (
    PlatformRecognitionConfigRecord,
    PlatformRecognitionConfigStore,
)

_SINGLETON_ID = "singleton"


def _record(row: _Model) -> PlatformRecognitionConfigRecord:
    return PlatformRecognitionConfigRecord(
        ocr_provider=row.ocr_provider,
        ocr_model=row.ocr_model,
        fallback_provider=row.fallback_provider,
        fallback_model=row.fallback_model,
        figure_provider=row.figure_provider,
        figure_model=row.figure_model,
        max_pages=row.max_pages,
        updated_by=row.updated_by,
    )


class SqlPlatformRecognitionConfigStore(PlatformRecognitionConfigStore):
    """Postgres-backed single-row platform recognition config repository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def get(self) -> PlatformRecognitionConfigRecord | None:
        async with self._sf() as session:
            row = (
                await session.execute(select(_Model).where(_Model.id == _SINGLETON_ID))
            ).scalar_one_or_none()
        return _record(row) if row is not None else None

    async def put(self, record: PlatformRecognitionConfigRecord) -> None:
        values = {**asdict(record), "updated_at": datetime.now(tz=UTC)}
        async with self._sf() as session:
            await session.execute(
                pg_insert(_Model)
                .values(id=_SINGLETON_ID, **values)
                .on_conflict_do_update(index_elements=["id"], set_=values)
            )
            await session.commit()
