"""Integration tests for :class:`SqlEvalRunStore` against a real Postgres — W4.

First SQL coverage for this store, added alongside the ``list_all_tenants``
aggregate (W4 cross-tenant read scope): exercises the cross-tenant page +
the per-tenant sibling against the real ``eval_run`` schema. Fixture style
mirrors ``test_sql_thread_meta_store.py`` (container-superuser session — the
production posture for the aggregate is the app's superuser connection with
the RLS GUC skipped via ``bypass_rls_session``).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.eval import SqlEvalRunStore
from expert_work.protocol import EvalRunRecord, EvalRunStatus, EvalTriggeredBy

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

SqlStoreFixture = tuple[SqlEvalRunStore, AsyncEngine]


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def sql_store(postgres_container: PostgresContainer) -> Iterator[SqlStoreFixture]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    session_factory = create_async_session_factory(engine)
    yield SqlEvalRunStore(session_factory), engine


def _run(
    *,
    tenant: UUID,
    created_at: datetime,
    status: EvalRunStatus = EvalRunStatus.QUEUED,
    run_id: UUID | None = None,
) -> EvalRunRecord:
    return EvalRunRecord(
        id=run_id if run_id is not None else uuid4(),
        tenant_id=tenant,
        suite="m0_baseline",
        status=status,
        triggered_by=EvalTriggeredBy.MANUAL,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_list_all_tenants_spans_tenants_newest_first(sql_store: SqlStoreFixture) -> None:
    """W4 — the aggregate pages across tenants (created_at DESC); the
    per-tenant sibling stays scoped. Session container is shared, so
    assertions filter to this test's tenants."""
    store, engine = sql_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        now = datetime.now(UTC)
        older = await store.create_run(_run(tenant=tenant_a, created_at=now - timedelta(minutes=2)))
        newer = await store.create_run(_run(tenant=tenant_b, created_at=now - timedelta(minutes=1)))
        passed = await store.create_run(
            _run(tenant=tenant_b, created_at=now, status=EvalRunStatus.PASSED)
        )

        items, total = await store.list_all_tenants(limit=500)
        mine = [r for r in items if r.tenant_id in {tenant_a, tenant_b}]
        assert [r.id for r in mine] == [passed.id, newer.id, older.id]
        assert total >= 3

        # status narrows the aggregate the same way as the sibling.
        queued, _ = await store.list_all_tenants(status=EvalRunStatus.QUEUED, limit=500)
        assert [r.id for r in queued if r.tenant_id in {tenant_a, tenant_b}] == [newer.id, older.id]

        # Per-tenant sibling stays scoped.
        a_items, a_total = await store.list_for_tenant(tenant_id=tenant_a)
        assert (a_total, [r.id for r in a_items]) == (1, [older.id])
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_all_tenants_id_tiebreak_on_equal_created_at(
    sql_store: SqlStoreFixture,
) -> None:
    """W4 review C-3 — ``created_at`` ties order by ``id`` ASC, in both the
    aggregate and the per-tenant sibling (C-7). Rows are inserted in
    *descending* ``id.int`` order so a dropped tiebreak degrades to insertion
    (heap-scan) order and the exact-order assertion goes red."""
    store, engine = sql_store
    try:
        tenant = uuid4()
        ts = datetime(2035, 1, 1, 12, 0, tzinfo=UTC)
        id_lo, id_hi = sorted((uuid4(), uuid4()), key=lambda u: u.int)
        for run_id in (id_hi, id_lo):  # descending insertion
            await store.create_run(_run(tenant=tenant, created_at=ts, run_id=run_id))

        items, _ = await store.list_all_tenants(limit=500)
        assert [r.id for r in items if r.tenant_id == tenant] == [id_lo, id_hi]

        # C-7 — the per-tenant sibling applies the same tiebreak.
        sibling, total = await store.list_for_tenant(tenant_id=tenant)
        assert (total, [r.id for r in sibling]) == (2, [id_lo, id_hi])
    finally:
        await engine.dispose()
