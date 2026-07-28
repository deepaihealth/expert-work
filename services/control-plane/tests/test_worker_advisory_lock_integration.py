"""Integration test — Task 3 (multi-replica readiness, W1 PR1 follow-up).

Boots a real Postgres (testcontainers) and drives two ``run_once()`` calls
concurrently, each on its own :class:`MemoryConsolidator` / :class:`SkillCurator`
instance wired to an independent session/connection against the SAME
database, to prove the ``pg_try_advisory_xact_lock`` single-flight contract
(mirrors :class:`~control_plane.quality_drift_worker.QualityDriftWorker`,
verified end-to-end in ``test_workspace_lock_integration.py`` for
``PgWorkspaceLock``):

- of two replicas racing the same sweep, exactly one really executes it
  (``finished_at`` set — the sweep-body sentinel both workers set
  unconditionally near the end of a real run);
- the other misses the lock and returns an empty summary immediately
  (``finished_at is None`` — the lock-miss short-circuit never reaches the
  sweep body at all, so it never audits or double-counts).

No schema is needed — ``pg_advisory_xact_lock`` is a built-in; the workers'
own business state (memory store / skill store / tenant config / audit) stays
in-memory and is shared across the two instances under test, exactly like two
replicas of the same deployment would share one Postgres-backed store.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from control_plane.memory_consolidator import (
    ConsolidatorRunSummary,
    MemoryConsolidator,
    make_null_consolidator_aux_model,
)
from control_plane.skill_curator import CuratorRunSummary, SkillCurator
from control_plane.tenancy import TenantConfigService
from expert_work.persistence import InMemoryMemoryStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.database import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.skill import InMemorySkillStore
from expert_work.persistence.tenant_config import InMemoryTenantConfigStore
from expert_work.protocol import AuditAction, AuditQuery
from expert_work.runtime.audit.fallback import InMemoryAuditFallbackQueue
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.audit.redactor import DefaultSecretRedactor

pytestmark = pytest.mark.integration

# Hold the lock open long enough that the losing side's own
# ``pg_try_advisory_xact_lock`` attempt is guaranteed to land while the
# winner still has the txn open — otherwise, on an empty store, a real sweep
# finishes in well under a millisecond and the "loser" could legitimately
# acquire the now-free lock afterwards and also run (which would be a
# correct *sequential* single-flight, but would falsely look like "both
# raced and both ran" to this test's concurrency assertion).
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


def _build_logger() -> tuple[AuditLogger, InMemoryAuditLogStore]:
    store = InMemoryAuditLogStore()
    return (
        AuditLogger(
            store=store,
            redactor=DefaultSecretRedactor(),
            fallback=InMemoryAuditFallbackQueue(),
        ),
        store,
    )


def _slow_down_sweep(worker: object, hold_s: float = _HOLD_S) -> None:
    """Patch ``worker._run_sweep`` to sleep before delegating to the real
    body, so the lock stays held long enough to force genuine contention
    (see ``_HOLD_S``). Instance-level attribute shadows the class method —
    the closure keeps calling the original bound method underneath."""
    original = worker._run_sweep  # type: ignore[attr-defined]

    async def _slow() -> object:
        await asyncio.sleep(hold_s)
        return await original()

    worker._run_sweep = _slow  # type: ignore[attr-defined]


class _NullEmbedder:
    async def embed_one(self, text: str, *, tenant_id: UUID) -> tuple[float, ...]:
        del text, tenant_id
        return (0.0,)


async def test_memory_consolidator_run_once_single_flights_across_sessions(
    engine: AsyncEngine,
) -> None:
    session_factory = create_async_session_factory(engine)
    store = InMemoryMemoryStore()  # empty — no tenants, so the sweep body is a fast no-op
    audit_logger, audit_store = _build_logger()
    config_service = TenantConfigService(
        store=InMemoryTenantConfigStore(),
        audit_logger=audit_logger,
    )

    def _make() -> MemoryConsolidator:
        worker = MemoryConsolidator(
            memory_store=store,
            tenant_config_service=config_service,
            audit_logger=audit_logger,
            aux_model=make_null_consolidator_aux_model(),
            embedder=_NullEmbedder(),
            interval_s=60.0,
            session_factory=session_factory,
        )
        _slow_down_sweep(worker)
        return worker

    worker_a, worker_b = _make(), _make()
    results: list[ConsolidatorRunSummary] = await asyncio.gather(
        worker_a.run_once(), worker_b.run_once()
    )

    ran = [r for r in results if r.finished_at is not None]
    skipped = [r for r in results if r.finished_at is None]
    assert len(ran) == 1, "exactly one replica should win the advisory lock and run the sweep"
    assert len(skipped) == 1, "the other replica should miss the lock and no-op immediately"

    page = await audit_store.query(AuditQuery(tenant_id="*", limit=100))
    run_audits = [e for e in page.entries if e.action == AuditAction.MEMORY_CONSOLIDATOR_RUN]
    assert len(run_audits) == 1, "only the winning replica's sweep should emit the run audit row"


async def test_skill_curator_run_once_single_flights_across_sessions(
    engine: AsyncEngine,
) -> None:
    session_factory = create_async_session_factory(engine)
    store = InMemorySkillStore()  # empty — no tenants, so the sweep body is a fast no-op
    audit_logger, audit_store = _build_logger()
    config_service = TenantConfigService(
        store=InMemoryTenantConfigStore(),
        audit_logger=audit_logger,
    )

    def _make() -> SkillCurator:
        worker = SkillCurator(
            skill_store=store,
            tenant_config_service=config_service,
            audit_logger=audit_logger,
            interval_s=60.0,
            session_factory=session_factory,
        )
        _slow_down_sweep(worker)
        return worker

    curator_a, curator_b = _make(), _make()
    results: list[CuratorRunSummary] = await asyncio.gather(
        curator_a.run_once(), curator_b.run_once()
    )

    ran = [r for r in results if r.finished_at is not None]
    skipped = [r for r in results if r.finished_at is None]
    assert len(ran) == 1, "exactly one replica should win the advisory lock and run the sweep"
    assert len(skipped) == 1, "the other replica should miss the lock and no-op immediately"

    page = await audit_store.query(AuditQuery(tenant_id="*", limit=100))
    run_audits = [e for e in page.entries if e.action == AuditAction.SKILL_CURATOR_RUN]
    assert len(run_audits) == 1, "only the winning replica's sweep should emit the run audit row"
