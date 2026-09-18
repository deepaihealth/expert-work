# ruff: noqa: RUF001 —— 断言里是平台贴给模型的中文全角标点
"""Integration tests for SqlThreadMessageStore against a real Postgres.

Covers what the in-memory double can't: the pg_trgm ILIKE search with
wildcard escaping, ON CONFLICT idempotency, and the sweep's work-queue
selection (backfill via missing watermark / re-select on new run activity /
fresh-activity-first ordering) across thread_meta + agent_run.
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
    MessageTurn,
    SqlThreadMessageStore,
    SqlThreadMetaStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.models.agent_run import AgentRunRow

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

Fixture = tuple[SqlThreadMessageStore, SqlThreadMetaStore, AsyncEngine]


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def sql_stores(postgres_container: PostgresContainer) -> Iterator[Fixture]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    session_factory = create_async_session_factory(engine)
    yield SqlThreadMessageStore(session_factory), SqlThreadMetaStore(session_factory), engine


async def _seed_run(engine: AsyncEngine, *, tenant_id: UUID, thread_id: UUID, at: datetime) -> None:
    session_factory = create_async_session_factory(engine)
    async with session_factory() as session:
        session.add(
            AgentRunRow(
                id=uuid4(),
                tenant_id=tenant_id,
                thread_id=thread_id,
                status="success",
                on_disconnect="cancel",
                created_at=at,
                updated_at=at,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_sync_search_and_escape_sql(sql_stores: Fixture) -> None:
    messages, threads, engine = sql_stores
    try:
        tenant, other_tenant = uuid4(), uuid4()
        thread = (
            await threads.create(thread_id=uuid4(), tenant_id=tenant, created_by="u")
        ).thread_id
        foreign = (
            await threads.create(thread_id=uuid4(), tenant_id=other_tenant, created_by="u")
        ).thread_id
        now = datetime.now(UTC)

        await messages.sync_thread(
            thread_id=thread,
            tenant_id=tenant,
            turns=[
                MessageTurn(seq=0, role="user", content="I was charged 100% twice, 请退款"),
                MessageTurn(seq=1, role="assistant", content="Refund case opened"),
            ],
            synced_at=now,
        )
        await messages.sync_thread(
            thread_id=foreign,
            tenant_id=other_tenant,
            turns=[MessageTurn(seq=0, role="user", content="charged here too")],
            synced_at=now,
        )

        # Substring, case-insensitive, CJK, and tenant-scoped.
        assert await messages.search_thread_ids(tenant_id=tenant, q="CHARGED") == {thread}
        assert await messages.search_thread_ids(tenant_id=tenant, q="退款") == {thread}
        # LIKE wildcards match literally (shared like_contains escaping).
        assert await messages.search_thread_ids(tenant_id=tenant, q="100%") == {thread}
        assert await messages.search_thread_ids(tenant_id=tenant, q="100_") == set()
        # Cross-tenant aggregate spans both tenants.
        both = await messages.search_thread_ids(tenant_id=None, q="charged")
        assert both == {thread, foreign}

        # Idempotent re-sync: existing seq rows never mutate.
        await messages.sync_thread(
            thread_id=thread,
            tenant_id=tenant,
            turns=[MessageTurn(seq=0, role="user", content="MUTATED")],
            synced_at=now,
        )
        assert await messages.search_thread_ids(tenant_id=tenant, q="MUTATED") == set()
        assert await messages.search_thread_ids(tenant_id=tenant, q="退款") == {thread}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_hidden_rows_are_mirrored_but_not_searchable_sql(sql_stores: Fixture) -> None:
    """B-73 ① —— 与内存后端**同一道谓词**(``test_platform_scaffolding_is_...``)。

    两套实现的谓词漂了,就是「本地全绿、生产另一个答案」。这里还多验一件内存后端
    验不了的事:``hidden`` 是唯一在 ON CONFLICT 时也写的列 —— 0156 之前的存量行上
    它是默认的 false 而不是真值,只有重扫覆盖才收敛得回来,而其余列照旧 DO NOTHING。
    """
    messages, threads, engine = sql_stores
    try:
        tenant = uuid4()
        thread = (
            await threads.create(thread_id=uuid4(), tenant_id=tenant, created_by="u")
        ).thread_id
        now = datetime.now(UTC)
        block = "[本轮输入]（平台自动生成）输入文件目录 $EXPERT_WORK_INPUTS_DIR"

        # 存量形态:0156 之前写下的行,没有 hidden 的信息。
        await messages.sync_thread(
            thread_id=thread,
            tenant_id=tenant,
            turns=[
                MessageTurn(seq=0, role="user", content="做一版随访方案"),
                MessageTurn(seq=1, role="user", content=block),
            ],
            synced_at=now,
        )
        assert await messages.search_thread_ids(tenant_id=tenant, q="本轮输入") == {thread}

        # sweep 重扫同一线程:hidden 被覆盖回真值,content 照旧不动。
        await messages.sync_thread(
            thread_id=thread,
            tenant_id=tenant,
            turns=[
                MessageTurn(seq=0, role="user", content="做一版随访方案"),
                MessageTurn(seq=1, role="user", content="MUTATED", hidden=True),
            ],
            synced_at=now,
        )
        assert await messages.search_thread_ids(tenant_id=tenant, q="本轮输入") == set()
        assert (
            await messages.search_thread_ids(tenant_id=tenant, q="EXPERT_WORK_INPUTS_DIR") == set()
        )
        # 正文没被改写(DO NOTHING 仍然管着其余列)。
        assert await messages.search_thread_ids(tenant_id=tenant, q="MUTATED") == set()
        # 真正的用户话照常搜得到 —— 这道谓词不是把整个线程从搜索里摘出去。
        assert await messages.search_thread_ids(tenant_id=tenant, q="随访") == {thread}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pending_selection_backfill_and_activity_sql(sql_stores: Fixture) -> None:
    messages, threads, engine = sql_stores
    try:
        tenant = uuid4()
        now = datetime.now(UTC)
        # backfill: has a run, no watermark yet.
        backfill = (
            await threads.create(thread_id=uuid4(), tenant_id=tenant, created_by="u")
        ).thread_id
        await _seed_run(engine, tenant_id=tenant, thread_id=backfill, at=now - timedelta(hours=2))
        # fresh: watermark exists but a newer run landed after it.
        fresh = (
            await threads.create(thread_id=uuid4(), tenant_id=tenant, created_by="u")
        ).thread_id
        await _seed_run(engine, tenant_id=tenant, thread_id=fresh, at=now)
        await messages.sync_thread(
            thread_id=fresh, tenant_id=tenant, turns=[], synced_at=now - timedelta(hours=1)
        )
        # settled: watermark newer than its last run — not selected.
        settled = (
            await threads.create(thread_id=uuid4(), tenant_id=tenant, created_by="u")
        ).thread_id
        await _seed_run(engine, tenant_id=tenant, thread_id=settled, at=now - timedelta(hours=2))
        await messages.sync_thread(thread_id=settled, tenant_id=tenant, turns=[], synced_at=now)
        # empty thread: no run at all — never enters the queue.
        await threads.create(thread_id=uuid4(), tenant_id=tenant, created_by="u")

        pending = await messages.pending_thread_ids(limit=10)
        ids = [t for t, _ in pending]
        assert set(ids) == {backfill, fresh}
        # Fresh activity is ordered before backfill so it can't be starved.
        assert ids[0] == fresh
        assert dict(pending)[backfill] == tenant

        # Mirroring the backfill thread removes it from the queue.
        await messages.sync_thread(thread_id=backfill, tenant_id=tenant, turns=[], synced_at=now)
        remaining = [t for t, _ in await messages.pending_thread_ids(limit=10)]
        assert backfill not in remaining
    finally:
        await engine.dispose()
