"""Integration test — Task 3 (multi-replica readiness).

Boots a real Postgres (testcontainers) and drives two ``run_once()`` calls
concurrently, each on its own :class:`WorkspaceJanitorWorker` instance wired
to an independent session/connection against the SAME database, to prove
the ``pg_try_advisory_xact_lock`` single-flight contract — logically
identical to ``tests/test_worker_advisory_lock_integration.py`` for
``MemoryConsolidator``/``SkillCurator``, adapted to this worker's
``JanitorRunStats`` shape: of two replicas racing the same cycle, exactly
one wins the lock and sweeps (``skipped=False``, and the one stale
``_scratch`` dir seeded below is the sentinel that a real cycle ran); the
other misses the lock and returns ``skipped=True`` immediately, without
touching the filesystem at all.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from control_plane.workspace_janitor import (
    _SCRATCH_MAX_AGE_S,
    JanitorRunStats,
    WorkspaceJanitorWorker,
)
from control_plane.workspace_quota import WorkspaceQuotaService
from expert_work.persistence import InMemoryTenantQuotaStore
from expert_work.persistence.database import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.workspace.memory import InMemoryUserWorkspaceStore
from expert_work.runtime.storage import InMemoryObjectStore

pytestmark = pytest.mark.integration

# Hold the lock open long enough that the losing side's own
# ``pg_try_advisory_xact_lock`` attempt is guaranteed to land while the
# winner still has the txn open — otherwise, with a single stale dir, a real
# cycle finishes in well under a millisecond and the "loser" could
# legitimately acquire the now-free lock afterwards and also run (which
# would be a correct *sequential* single-flight, but would falsely look like
# "both raced and both ran" to this test's concurrency assertion).
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


def _slow_down_cycle(worker: WorkspaceJanitorWorker, hold_s: float = _HOLD_S) -> None:
    """Patch ``worker._run_cycle`` to sleep before delegating to the real
    body, so the lock stays held long enough to force genuine contention
    (see ``_HOLD_S``). Instance-level attribute shadows the class method —
    the closure keeps calling the original bound method underneath."""
    original = worker._run_cycle  # type: ignore[attr-defined]

    async def _slow(stats: JanitorRunStats) -> None:
        await asyncio.sleep(hold_s)
        await original(stats)

    worker._run_cycle = _slow  # type: ignore[attr-defined]


def _age(path: Path, *, seconds: float) -> None:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


async def test_workspace_janitor_run_once_single_flights_across_sessions(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    session_factory = create_async_session_factory(engine)

    stale = tmp_path / "_scratch" / str(uuid4())
    stale.mkdir(parents=True)
    _age(stale, seconds=_SCRATCH_MAX_AGE_S + 60)

    workspaces = InMemoryUserWorkspaceStore()
    quota_service = WorkspaceQuotaService(
        user_workspaces=workspaces,
        tenant_quotas=InMemoryTenantQuotaStore(),
        workspace_root=str(tmp_path),
    )

    def _make() -> WorkspaceJanitorWorker:
        worker = WorkspaceJanitorWorker(
            user_workspaces=workspaces,
            quota_service=quota_service,
            object_store=InMemoryObjectStore(),
            workspace_root=str(tmp_path),
            interval_s=60.0,
            session_factory=session_factory,
        )
        _slow_down_cycle(worker)
        return worker

    worker_a, worker_b = _make(), _make()
    results: list[JanitorRunStats] = await asyncio.gather(worker_a.run_once(), worker_b.run_once())

    ran = [r for r in results if not r.skipped]
    skipped = [r for r in results if r.skipped]
    assert len(ran) == 1, "exactly one replica should win the advisory lock and run the cycle"
    assert len(skipped) == 1, "the other replica should miss the lock and no-op immediately"
    assert sum(r.scratch_removed for r in results) == 1, (
        "the one stale _scratch dir must be removed exactly once, by the winner only"
    )
    assert not stale.exists()
