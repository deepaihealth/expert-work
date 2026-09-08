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


@pytest.mark.asyncio
async def test_totals_by_trace_ids_buckets_by_provider_and_model(
    usage_store: SqlStoreFixture,
) -> None:
    """B-42 —— SQL 版与内存版同义:同一 trace 下主 Agent 模型 A + worker 模型 B
    分成两桶,桶的和 == 总量;总量字段与 ``models`` 照旧。"""
    store, engine = usage_store
    try:
        tenant = uuid4()
        trace = f"trace-{uuid4().hex}"

        def _row(
            provider: str | None, model: str, inp: int, out: int, cache_read: int = 0
        ) -> TokenUsageRecord:
            return TokenUsageRecord(
                tenant_id=tenant,
                agent_name="agent",
                agent_version="v1",
                model=model,
                provider=provider,
                trace_id=trace,
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=cache_read,
            )

        await store.insert(_row("anthropic", "claude-sonnet-4-6", 100, 10, cache_read=40))
        await store.insert(_row("zhipu", "glm-5.3", 3_000, 300, cache_read=900))
        await store.insert(_row("anthropic", "claude-sonnet-4-6", 50, 5))
        await store.insert(_row(None, "glm-5.3", 7, 7))  # legacy NULL provider → own bucket

        totals = (await store.totals_by_trace_ids([trace]))[trace]
        assert totals.input_tokens == 3_157
        assert totals.output_tokens == 322
        assert totals.cache_read_tokens == 940
        assert totals.llm_calls == 4
        assert totals.models == ("claude-sonnet-4-6", "glm-5.3")

        assert [(b.provider, b.model, b.llm_calls) for b in totals.by_model] == [
            (None, "glm-5.3", 1),
            ("anthropic", "claude-sonnet-4-6", 2),
            ("zhipu", "glm-5.3", 1),
        ]
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
        ):
            assert sum(getattr(b, field) for b in totals.by_model) == getattr(totals, field), field
    finally:
        await engine.dispose()
