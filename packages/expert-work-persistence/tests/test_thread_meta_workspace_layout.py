"""B-262 P3 —— 老对话迁移时把 ``workspace_layout`` 从 ``agent_root`` 翻成 ``conversation``。

只许这一个方向、按比较再交换(``WHERE workspace_layout = 'agent_root'``):两个副本同时迁
同一个对话,只有一个翻成功(它给模型发一次性说明),另一个看到 ``False``。内存与 SQL 两个
实现同一谓词。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
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


def _dsn(container: PostgresContainer, driver: str) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", f"+{driver}").replace(
        "postgresql://", f"postgresql+{driver}://", 1
    )


@pytest.fixture(params=["memory", pytest.param("sql", marks=pytest.mark.integration)])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[ThreadMetaStore]:
    if request.param == "memory":
        yield InMemoryThreadMetaStore()
        return
    container: PostgresContainer = request.getfixturevalue("postgres_container")
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _dsn(container, "psycopg"))
    command.upgrade(cfg, "head")
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_dsn(container, "asyncpg")))
    try:
        yield SqlThreadMetaStore(create_async_session_factory(engine))
    finally:
        await engine.dispose()


async def _old_thread(store: ThreadMetaStore, tenant: UUID) -> UUID:
    """一条 B-236 之前开的对话(新建的一律是对话视图,这里改回老视图)。"""
    tid = uuid4()
    await store.create(thread_id=tid, tenant_id=tenant, created_by="u")
    if isinstance(store, InMemoryThreadMetaStore):
        meta = store._rows[tid]
        store._rows[tid] = meta.model_copy(update={"workspace_layout": "agent_root"})
    else:
        assert isinstance(store, SqlThreadMetaStore)
        async with store._sf() as session:
            await session.execute(
                text("UPDATE thread_meta SET workspace_layout = 'agent_root' WHERE thread_id = :t"),
                {"t": tid},
            )
            await session.commit()
    return tid


@pytest.mark.asyncio
async def test_promote_flips_an_old_thread_exactly_once(store: ThreadMetaStore) -> None:
    tenant = uuid4()
    tid = await _old_thread(store, tenant)

    assert await store.promote_workspace_layout(tid, tenant_id=tenant) is True
    assert await store.promote_workspace_layout(tid, tenant_id=tenant) is False
    meta = await store.get(tid, tenant_id=tenant)
    assert meta is not None and meta.workspace_layout == "conversation"


@pytest.mark.asyncio
async def test_promote_never_touches_another_tenant_or_a_new_thread(store: ThreadMetaStore) -> None:
    tenant = uuid4()
    tid = await _old_thread(store, tenant)
    fresh = uuid4()
    await store.create(thread_id=fresh, tenant_id=tenant, created_by="u")

    assert await store.promote_workspace_layout(tid, tenant_id=uuid4()) is False
    assert await store.promote_workspace_layout(fresh, tenant_id=tenant) is False
    meta = await store.get(tid, tenant_id=tenant)
    assert meta is not None and meta.workspace_layout == "agent_root"
