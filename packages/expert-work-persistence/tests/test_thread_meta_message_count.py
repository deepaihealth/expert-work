"""thread_meta.message_count —— 新建写 0、存量留 None、更新可回读。

Parametrized across both ``ThreadMetaStore`` implementations — SQL and
in-memory predicates must stay byte-identical (see memory:sandbox-migration
store-predicate-drift lesson).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    InMemoryThreadMetaStore,
    SqlThreadMetaStore,
    ThreadMetaStore,
    create_async_engine_from_config,
    create_async_session_factory,
)

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(params=["memory", "sql"])
async def thread_meta_store(request: pytest.FixtureRequest) -> AsyncIterator[ThreadMetaStore]:
    """Yields both the in-memory and the Postgres-backed store — every test
    in this module runs once per backend so the two predicates can't drift
    apart unnoticed."""
    if request.param == "memory":
        yield InMemoryThreadMetaStore()
        return

    request.node.add_marker(pytest.mark.integration)
    postgres_container: PostgresContainer = request.getfixturevalue("postgres_container")
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    session_factory = create_async_session_factory(engine)
    try:
        yield SqlThreadMetaStore(session_factory)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_defaults_to_zero(thread_meta_store: ThreadMetaStore) -> None:
    tid, tenant = uuid4(), uuid4()
    meta = await thread_meta_store.create(thread_id=tid, tenant_id=tenant, created_by="u")
    assert meta.message_count == 0


@pytest.mark.asyncio
async def test_update_message_count_round_trip(thread_meta_store: ThreadMetaStore) -> None:
    tid, tenant = uuid4(), uuid4()
    await thread_meta_store.create(thread_id=tid, tenant_id=tenant, created_by="u")
    assert await thread_meta_store.update_message_count(tid, 7, tenant_id=tenant) is True
    got = await thread_meta_store.get(tid, tenant_id=tenant)
    assert got is not None and got.message_count == 7


@pytest.mark.asyncio
async def test_update_message_count_cross_tenant_is_noop(
    thread_meta_store: ThreadMetaStore,
) -> None:
    tid, tenant = uuid4(), uuid4()
    await thread_meta_store.create(thread_id=tid, tenant_id=tenant, created_by="u")
    assert await thread_meta_store.update_message_count(tid, 7, tenant_id=uuid4()) is False
    got = await thread_meta_store.get(tid, tenant_id=tenant)
    assert got is not None and got.message_count == 0
