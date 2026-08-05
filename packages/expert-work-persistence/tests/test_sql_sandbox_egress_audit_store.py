"""Integration tests for :class:`SqlSandboxEgressAuditStore` against real Postgres."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

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
from expert_work.persistence.models import SandboxEgressAuditRow
from expert_work.persistence.sandbox_egress_audit import SqlSandboxEgressAuditStore

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

SqlStoreFixture = tuple[SqlSandboxEgressAuditStore, AsyncEngine]


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
    yield SqlSandboxEgressAuditStore(session_factory), engine


async def _insert(engine: AsyncEngine, *, verdict: str, occurred_at: datetime) -> None:
    session_factory = create_async_session_factory(engine)
    async with session_factory() as session:
        session.add(
            SandboxEgressAuditRow(
                tenant_id=uuid4(),
                agent_name="alpha",
                agent_version="1.0.0",
                sandbox_id="sbx-1",
                target_host="api.openai.com",
                target_port=443,
                verdict=verdict,
                bytes_up=10,
                bytes_down=20,
                duration_ms=5,
                occurred_at=occurred_at,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_count_by_verdict_since_aggregates_and_excludes_allowed(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        now = datetime.now(UTC)
        since = now - timedelta(minutes=5)
        # Two blocked_auth + one blocked_ssrf inside the window, one allowed
        # inside the window (excluded), one blocked_ssrf before the window
        # (excluded by the cursor).
        await _insert(engine, verdict="blocked_auth", occurred_at=now)
        await _insert(engine, verdict="blocked_auth", occurred_at=now)
        await _insert(engine, verdict="blocked_ssrf", occurred_at=now)
        await _insert(engine, verdict="allowed", occurred_at=now)
        await _insert(engine, verdict="blocked_ssrf", occurred_at=since - timedelta(minutes=1))

        counts = await store.count_by_verdict_since(since=since)
        assert counts == {"blocked_auth": 2, "blocked_ssrf": 1}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_count_by_verdict_since_window_boundary_is_inclusive(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        since = datetime.now(UTC)
        await _insert(engine, verdict="blocked_auth", occurred_at=since)  # exactly at cursor

        counts = await store.count_by_verdict_since(since=since)
        assert counts == {"blocked_auth": 1}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_count_by_verdict_since_empty_window_returns_empty_dict(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        counts = await store.count_by_verdict_since(since=datetime.now(UTC))
        assert counts == {}
    finally:
        await engine.dispose()
