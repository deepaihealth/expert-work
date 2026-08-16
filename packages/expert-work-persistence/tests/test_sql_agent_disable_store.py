"""Integration test for :class:`SqlAgentDisableStore` — Stream RT-4 (RT-ADR-16).

Exercises the 0114_agent_disable migration + the store (upsert / get / RLS
isolation) against a real Postgres, mirroring test_sql_tenant_config_store.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlAgentDisableStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.rls import build_rls_sessionmaker, current_tenant_id_var

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

APP_ROLE = "expert_work_app"
APP_PASSWORD = "expert_work_app_test_pw"  # test-only fixture password


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


def _rewrite_credentials(dsn: str, user: str, password: str) -> str:
    parsed = urlparse(dsn)
    new_netloc = f"{user}:{password}@{parsed.hostname}"
    if parsed.port is not None:
        new_netloc = f"{new_netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=new_netloc))


def _provision_app_role(sync_dsn: str) -> None:
    admin_engine = create_engine(sync_dsn, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
                {"role": APP_ROLE},
            ).first()
            if exists is None:
                conn.execute(text(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PASSWORD}'"))
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
            conn.execute(
                text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE "
                    f"ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
                )
            )
            conn.execute(
                text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
            )
    finally:
        admin_engine.dispose()


@pytest.fixture
def agent_disable_store(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlAgentDisableStore, AsyncEngine]]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    _provision_app_role(_sync_dsn(postgres_container))

    app_dsn = _rewrite_credentials(_async_dsn(postgres_container), APP_ROLE, APP_PASSWORD)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=app_dsn))
    sf = build_rls_sessionmaker(create_async_session_factory(engine))
    yield SqlAgentDisableStore(sf), engine


@pytest.fixture(autouse=True)
def reset_rls() -> Iterator[None]:
    tok = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tok)


@pytest.mark.asyncio
async def test_set_disabled_then_get_round_trip(
    agent_disable_store: tuple[SqlAgentDisableStore, AsyncEngine],
) -> None:
    store, engine = agent_disable_store
    try:
        tenant = uuid4()
        current_tenant_id_var.set(tenant)
        # Absent before any write.
        assert await store.get(tenant_id=tenant, agent_name="support-bot") is None

        rec = await store.set_disabled(
            tenant_id=tenant,
            agent_name="support-bot",
            disabled=True,
            reason="incident-42",
            disabled_by="admin@acme",
        )
        assert rec.disabled is True
        assert rec.reason == "incident-42"
        assert rec.disabled_at is not None

        fetched = await store.get(tenant_id=tenant, agent_name="support-bot")
        assert fetched is not None
        assert fetched.disabled is True
        assert fetched.disabled_by == "admin@acme"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upsert_and_enable_clears_metadata(
    agent_disable_store: tuple[SqlAgentDisableStore, AsyncEngine],
) -> None:
    store, engine = agent_disable_store
    try:
        tenant = uuid4()
        current_tenant_id_var.set(tenant)
        await store.set_disabled(
            tenant_id=tenant, agent_name="a", disabled=True, reason="first", disabled_by="u1"
        )
        # Second write to the same (tenant, name) upserts, not duplicates.
        await store.set_disabled(
            tenant_id=tenant, agent_name="a", disabled=True, reason="second", disabled_by="u2"
        )
        mid = await store.get(tenant_id=tenant, agent_name="a")
        assert mid is not None
        assert mid.reason == "second"

        enabled = await store.set_disabled(
            tenant_id=tenant, agent_name="a", disabled=False, reason=None, disabled_by="u2"
        )
        assert enabled.disabled is False
        assert enabled.reason is None
        assert enabled.disabled_at is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rls_blocks_cross_tenant_read(
    agent_disable_store: tuple[SqlAgentDisableStore, AsyncEngine],
) -> None:
    store, engine = agent_disable_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        current_tenant_id_var.set(tenant_a)
        await store.set_disabled(
            tenant_id=tenant_a, agent_name="a", disabled=True, reason=None, disabled_by="x"
        )
        current_tenant_id_var.set(tenant_a)
        assert await store.get(tenant_id=tenant_a, agent_name="a") is not None
        # Scope to B: A's row is invisible under RLS.
        current_tenant_id_var.set(tenant_b)
        assert await store.get(tenant_id=tenant_a, agent_name="a") is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_disabled_names_matches_the_in_memory_store(
    agent_disable_store: tuple[SqlAgentDisableStore, AsyncEngine],
) -> None:
    """SQL 与 in-memory 的谓词必须同义 —— 两个后端各写一遍过滤条件,
    是本仓反复出问题的地方(SQL 用 ``== True`` / 内存用 ``is True``
    这类差异不会被单后端测试发现)。同一组输入喂两个 store,断言输出相等。
    """
    from expert_work.persistence.agent_disable.memory import InMemoryAgentDisableStore

    sql_store, engine = agent_disable_store
    try:
        mem_store = InMemoryAgentDisableStore()
        tenant_id = uuid4()
        current_tenant_id_var.set(tenant_id)
        fixtures = [("off-1", True), ("off-2", True), ("back-on", False)]
        for name, disabled in fixtures:
            for store in (sql_store, mem_store):
                await store.set_disabled(
                    tenant_id=tenant_id,
                    agent_name=name,
                    disabled=disabled,
                    reason=None,
                    disabled_by=None,
                )

        sql_result = await sql_store.list_disabled_names(tenant_id=tenant_id)
        mem_result = await mem_store.list_disabled_names(tenant_id=tenant_id)
        assert sql_result == mem_result == {"off-1", "off-2"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_disabled_names_does_not_leak_across_tenants(
    agent_disable_store: tuple[SqlAgentDisableStore, AsyncEngine],
) -> None:
    store, engine = agent_disable_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        current_tenant_id_var.set(tenant_a)
        await store.set_disabled(
            tenant_id=tenant_a, agent_name="mine", disabled=True, reason=None, disabled_by=None
        )
        current_tenant_id_var.set(tenant_b)
        await store.set_disabled(
            tenant_id=tenant_b, agent_name="theirs", disabled=True, reason=None, disabled_by=None
        )
        current_tenant_id_var.set(tenant_a)
        assert await store.list_disabled_names(tenant_id=tenant_a) == {"mine"}
    finally:
        await engine.dispose()
