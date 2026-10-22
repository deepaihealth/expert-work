"""B-209 PR-2 (re-review round 4) —— the figure reconciler's pass is
single-flight across replicas: two reconcilers on independent connections to
one real Postgres race a pass; exactly one runs it, the other skips at once.
(The document lookup itself is an in-memory stand-in: only the lock is under
test here.)"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from control_plane.knowledge.recognition.figure_reconcile import (
    FigureReconciler,
    ReconcileOutcome,
)
from control_plane.knowledge.recognition.figure_storage import FigureStorage
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.database import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.runtime.storage import InMemoryObjectStore

pytestmark = pytest.mark.integration

#: Long enough that the loser's lock attempt lands while the winner holds it.
_HOLD_S = 0.3


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
async def engine(postgres_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine_from_config(
        DatabaseConfig(dsn=_async_dsn(postgres_container), pgbouncer_mode=False)
    )
    try:
        yield eng
    finally:
        await eng.dispose()


class _Slow(FigureReconciler):
    async def _pass(self) -> ReconcileOutcome:
        await asyncio.sleep(_HOLD_S)
        return ReconcileOutcome()


async def test_one_of_two_replicas_runs_the_pass(engine: AsyncEngine) -> None:
    session_factory = create_async_session_factory(engine)

    def replica() -> FigureReconciler:
        return _Slow(
            store=InMemoryKnowledgeStore(),
            storage=FigureStorage(InMemoryObjectStore()),
            interval_s=60,
            session_factory=session_factory,
        )

    results = await asyncio.gather(replica().run_once(), replica().run_once())
    assert sorted(result.skipped for result in results) == [False, True]
    # The lock went with the winner's transaction: the next pass runs.
    assert (await replica().run_once()).skipped is False
