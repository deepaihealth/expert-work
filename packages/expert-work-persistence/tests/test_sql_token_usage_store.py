"""Integration tests for DbTokenUsageStore against a real Postgres.

Pins per-tenant isolation for the read-by-tenant methods. This harness connects
as a superuser (RLS bypassed) — mirroring the app's runtime DB posture — so the
explicit ``tenant_id`` SQL predicate is the actual cross-tenant guard, not RLS.
Without the predicate these reads return every tenant's usage.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
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
from expert_work.persistence.token_usage_store import DbTokenUsageStore, TokenUsageRecord

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

SqlStoreFixture = tuple[DbTokenUsageStore, AsyncEngine]


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def usage_store(postgres_container: PostgresContainer) -> Iterator[SqlStoreFixture]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    session_factory = create_async_session_factory(engine)
    yield DbTokenUsageStore(session_factory), engine


def _rec(tenant: UUID) -> TokenUsageRecord:
    return TokenUsageRecord(
        tenant_id=tenant,
        agent_name="agent",
        agent_version="v1",
        model="claude",
        input_tokens=10,
        output_tokens=5,
    )


@pytest.mark.asyncio
async def test_list_for_tenant_excludes_other_tenants(usage_store: SqlStoreFixture) -> None:
    store, engine = usage_store
    try:
        t1, t2 = uuid4(), uuid4()
        await store.insert(_rec(t1))
        await store.insert(_rec(t1))
        await store.insert(_rec(t2))
        rows = await store.list_for_tenant(tenant_id=t1)
        assert len(rows) == 2
        assert all(r.tenant_id == t1 for r in rows)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_for_tenant_window_excludes_other_tenants(usage_store: SqlStoreFixture) -> None:
    store, engine = usage_store
    try:
        t1, t2 = uuid4(), uuid4()
        await store.insert(_rec(t1))
        await store.insert(_rec(t2))
        await store.insert(_rec(t2))
        # Wide window covers every inserted row (observed_at defaults to now()).
        wide_start = datetime(2000, 1, 1, tzinfo=UTC)
        wide_end = datetime(2100, 1, 1, tzinfo=UTC)
        rows = await store.list_for_tenant_window(tenant_id=t1, start=wide_start, end=wide_end)
        assert len(rows) == 1
        assert all(r.tenant_id == t1 for r in rows)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_window_all_tenants_crosses_tenants(usage_store: SqlStoreFixture) -> None:
    """W4 — the all-tenants read returns every tenant's rows.

    Also exercises the ``SET LOCAL ROLE audit_reader`` path end-to-end:
    assuming the role DROPS privileges to ``audit_reader`` even under this
    superuser harness, so the read only works if migration 0140's
    ``GRANT SELECT ON token_usage TO audit_reader`` actually landed.
    """
    store, engine = usage_store
    try:
        t1, t2 = uuid4(), uuid4()
        await store.insert(_rec(t1))
        await store.insert(_rec(t2))
        await store.insert(_rec(t2))
        wide_start = datetime(2000, 1, 1, tzinfo=UTC)
        wide_end = datetime(2100, 1, 1, tzinfo=UTC)
        rows = await store.list_window_all_tenants(start=wide_start, end=wide_end)
        # The shared container keeps other tests' rows — filter to ours.
        mine = [r for r in rows if r.tenant_id in {t1, t2}]
        assert len(mine) == 3
        assert {r.tenant_id for r in mine} == {t1, t2}
        # Empty window → nothing (same half-open semantics as per-tenant).
        assert await store.list_window_all_tenants(start=wide_end, end=wide_end) == []
    finally:
        await engine.dispose()
