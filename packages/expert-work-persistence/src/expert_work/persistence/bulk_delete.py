"""Count-then-delete helpers for the agent purge (B-278).

The purge preview must report exactly what the purge would delete, so every
store's ``count_*`` and ``delete_*`` pair share one ``WHERE`` here instead of
spelling the predicate twice. ``*_in_chunks`` variants split a large id list
(a purged agent can own thousands of conversations) into bounded ``IN``
lists, the same 500-wide chunks ``FeedbackStore.delete_for_threads`` uses.

Every caller passes a ``tenant_id`` predicate: the runtime connects as a
role that bypasses RLS, so the ``WHERE tenant_id = ...`` is the isolation.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute

#: ``IN`` list width — well under asyncpg's 32767 bind-parameter cap.
CHUNK = 500


async def count_where(
    sf: async_sessionmaker[AsyncSession], model: type[Any], *clauses: ColumnElement[bool]
) -> int:
    """``SELECT count(*) FROM model WHERE clauses``."""
    async with sf() as session:
        value = await session.scalar(select(func.count()).select_from(model).where(*clauses))
    return int(value or 0)


async def delete_where(
    sf: async_sessionmaker[AsyncSession], model: type[Any], *clauses: ColumnElement[bool]
) -> int:
    """``DELETE FROM model WHERE clauses``; returns the rows removed."""
    async with sf() as session:
        result = await session.execute(
            delete(model).where(*clauses).execution_options(synchronize_session=False)
        )
        await session.commit()
    return int(getattr(result, "rowcount", 0) or 0)


async def count_in_chunks(
    sf: async_sessionmaker[AsyncSession],
    model: type[Any],
    column: InstrumentedAttribute[Any],
    values: Sequence[object],
    *clauses: ColumnElement[bool],
) -> int:
    """:func:`count_where` with ``column IN values``, ``values`` split in chunks."""
    total = 0
    for i in range(0, len(values), CHUNK):
        total += await count_where(sf, model, column.in_(list(values[i : i + CHUNK])), *clauses)
    return total


async def delete_in_chunks(
    sf: async_sessionmaker[AsyncSession],
    model: type[Any],
    column: InstrumentedAttribute[Any],
    values: Sequence[object],
    *clauses: ColumnElement[bool],
) -> int:
    """:func:`delete_where` with ``column IN values``, one transaction per chunk."""
    total = 0
    for i in range(0, len(values), CHUNK):
        total += await delete_where(sf, model, column.in_(list(values[i : i + CHUNK])), *clauses)
    return total
