"""知识库关键词检索的执行计划守卫(B-175 波 2 PR1)。

28d7dd90 的关键词语句在评测库上每题 10~44 秒:没有统计信息(从未 ANALYZE、关了
autovacuum)时,规划器把「范围内分段数 / 各词命中数」两个单行聚合放进嵌套循环的内侧,
每个命中行重算一遍(4,931 行命中 → 两个聚合各跑 4,931 次)。生产表统计信息过期时同样
可能发生,所以这里在同样的条件下跑真实语句,对计划做结构断言,不依赖耗时。

另一个同源问题:idf 权重若被提升进逐行表达式,每个命中行都会重算全部 ``ln(...)``。

本地跑前: export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.protocol import KnowledgeChunk

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "knowledge_keyword_plan_guard"

#: The evaluation database's planner settings (and a common production shape).
_EVAL_LIKE = (
    "SET LOCAL enable_indexscan = off",
    "SET LOCAL max_parallel_workers_per_gather = 0",
    "SET LOCAL work_mem = '4MB'",
)

PlanFixture = tuple[SqlKnowledgeStore, AsyncEngine]


@pytest.fixture
async def plan_db(postgres_container: PostgresContainer) -> AsyncIterator[PlanFixture]:
    """A fresh database whose ``knowledge_chunk`` is never analysed."""
    admin = str(postgres_container.get_connection_url()).replace("+psycopg2", "")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_DB}"')
    dsn = f"{admin.rpartition('/')[0]}/{_DB}"
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("ALTER TABLE knowledge_chunk SET (autovacuum_enabled = false)")
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    )
    try:
        yield SqlKnowledgeStore(create_async_session_factory(engine)), engine
    finally:
        await engine.dispose()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


async def _seed(store: SqlKnowledgeStore, chunks: int) -> tuple[UUID, UUID]:
    tenant = uuid4()
    kb_id = (await store.create_base(tenant_id=tenant, name="kb")).id
    document = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="a.md")
    words = ("苹果 沙拉", "香蕉 甜点", "苹果 香蕉 果汁", "面包 牛奶")
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=document.id,
        chunks=[
            KnowledgeChunk(
                id=uuid4(),
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=document.id,
                chunk_index=index,
                content=f"{words[index % len(words)]} 第{index}条",
                embedding=(1.0,) + (0.0,) * (EMBEDDING_DIM - 1),
            )
            for index in range(chunks)
        ],
    )
    return tenant, kb_id


def _nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [node, *(n for child in node.get("Plans", []) for n in _nodes(child))]


async def test_per_query_parts_run_once_without_statistics(plan_db: PlanFixture) -> None:
    """With no statistics and the evaluation database's planner settings, the
    chunks-in-scope count, the per-unit hit sums and the idf weights each run
    once per query, and the per-row score only multiplies precomputed weights
    (no ``ln`` and no numeric round trip per matching row)."""
    store, engine = plan_db
    tenant, kb_id = await _seed(store, 600)
    async with engine.connect() as conn:
        analysed = (
            await conn.execute(
                text("SELECT count(*) FROM pg_stats WHERE tablename = 'knowledge_chunk'")
            )
        ).scalar_one()
    assert analysed == 0  # the condition this guard is about

    captured: list[tuple[str, Any]] = []

    def _capture(conn: Any, cursor: Any, statement: str, params: Any, *_: Any) -> None:
        captured.append((statement, params))

    event.listen(engine.sync_engine, "before_cursor_execute", _capture)
    try:
        hits = await store.keyword_search_scored(
            tenant_id=tenant, kb_ids=[kb_id], query="苹果和香蕉可以一起吃吗?", limit=10
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _capture)
    assert len(hits) == 10
    statement, params = captured[-1]
    async with engine.begin() as conn:
        for setting in _EVAL_LIKE:
            await conn.execute(text(setting))
        plan = (
            await conn.exec_driver_sql(
                "EXPLAIN (ANALYZE, VERBOSE, FORMAT JSON) " + statement, params
            )
        ).scalar_one()
    nodes = _nodes(plan[0]["Plan"])
    aggregates = [node["Actual Loops"] for node in nodes if node["Node Type"] == "Aggregate"]
    assert aggregates
    assert aggregates == [1] * len(aggregates)
    per_row = [
        " ".join(node.get("Output", []))
        for node in nodes
        if node["Actual Rows"] * node["Actual Loops"] > 1
    ]
    assert not [output for output in per_row if "ln(" in output or "numeric" in output]
