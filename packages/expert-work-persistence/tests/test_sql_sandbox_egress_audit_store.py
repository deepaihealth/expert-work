"""Integration tests for :class:`SqlSandboxEgressAuditStore` against real Postgres."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
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
        until = now + timedelta(minutes=1)
        # Two blocked_auth + one blocked_ssrf inside the window, one allowed
        # inside the window (excluded), one blocked_ssrf before the window
        # (excluded by the cursor).
        await _insert(engine, verdict="blocked_auth", occurred_at=now)
        await _insert(engine, verdict="blocked_auth", occurred_at=now)
        await _insert(engine, verdict="blocked_ssrf", occurred_at=now)
        await _insert(engine, verdict="allowed", occurred_at=now)
        await _insert(engine, verdict="blocked_ssrf", occurred_at=since - timedelta(minutes=1))

        counts = await store.count_by_verdict_since(since=since, until=until)
        assert counts == {"blocked_auth": 2, "blocked_ssrf": 1}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_count_by_verdict_since_lower_bound_is_inclusive(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        since = datetime.now(UTC)
        until = since + timedelta(minutes=5)
        await _insert(engine, verdict="blocked_auth", occurred_at=since)  # exactly at cursor

        counts = await store.count_by_verdict_since(since=since, until=until)
        assert counts == {"blocked_auth": 1}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_count_by_verdict_since_upper_bound_is_exclusive(
    sql_store: SqlStoreFixture,
) -> None:
    """Fix round 1 — double-count guard: a row landing exactly on ``until``
    (the next cycle's ``since``) must be counted by neither cycle twice, so
    this cycle must NOT count it."""
    store, engine = sql_store
    try:
        since = datetime.now(UTC)
        until = since + timedelta(minutes=5)
        await _insert(engine, verdict="blocked_auth", occurred_at=until)  # exactly at scan-start

        counts = await store.count_by_verdict_since(since=since, until=until)
        assert counts == {}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_count_by_verdict_since_empty_window_returns_empty_dict(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        now = datetime.now(UTC)
        counts = await store.count_by_verdict_since(since=now, until=now + timedelta(minutes=1))
        assert counts == {}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_metrics_scan_uses_the_partial_index(sql_store: SqlStoreFixture) -> None:
    """0143 的 partial index 对 metrics 那条窗口聚合可用 —— 光建索引不算数。

    索引谓词跟查询谓词只要差一个字,Postgres 就当它不适用、默默回退顺序扫描:
    建了索引、查询照旧慢,而且没有任何报错。所以断言看的是执行计划,不是
    ``pg_indexes`` 里有没有这一行。

    ``enable_seqscan = off``:测试表只有几行,优化器此刻当然更愿意顺序扫。
    这里要证的是「这个索引对这条查询可用」,不是「优化器此刻会选它」——
    关掉顺序扫描,可用则走索引,不可用则仍然是 Seq Scan(那就是红)。
    """
    _store, engine = sql_store
    try:
        now = datetime.now(UTC)
        await _insert(engine, verdict="blocked_auth", occurred_at=now)
        await _insert(engine, verdict="allowed", occurred_at=now)

        async with engine.begin() as conn:
            await conn.execute(text("SET LOCAL enable_seqscan = off"))
            rows = await conn.execute(
                text(
                    "EXPLAIN SELECT verdict, count(*) FROM sandbox_egress_audit "
                    "WHERE occurred_at >= :since AND occurred_at < :until "
                    "AND verdict <> 'allowed' GROUP BY verdict"
                ),
                {"since": now - timedelta(minutes=5), "until": now + timedelta(minutes=5)},
            )
            plan = "\n".join(str(r[0]) for r in rows)

        assert "sandbox_egress_audit_scan_idx" in plan, plan
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_retention_role_can_delete_from_the_audit_table(sql_store: SqlStoreFixture) -> None:
    """``retention_cleanup_worker`` 拿得到 SELECT/DELETE。

    这张表建表至今没有任何表级 GRANT,而 PR-E 给它加了清扫 pass —— 少了这条
    授权,清扫作业每次跑都是 permission denied(0131 记过同样的欠账)。

    DELETE 带 ``WHERE occurred_at <= :cutoff``(照 Task 5 清扫作业的真实查询形状),
    不是无条件 ``DELETE FROM``:Postgres 对无 WHERE 的全表删除不检查 SELECT 权限,
    只测无条件删除的话,GRANT 掉了 SELECT 这一半这条用例也看不出来。

    ``postgres_container`` 是 session-scoped(见 conftest.py),同一进程内跑的
    其它用例不会清空这张表,所以这里不能断言删了恰好 1 行——用同一个 cutoff
    先数一遍再跟删除结果比对,避免跟同文件里其它用例的插入行数耦合。
    """
    _store, engine = sql_store
    try:
        occurred_at = datetime.now(UTC)
        await _insert(engine, verdict="allowed", occurred_at=occurred_at)
        cutoff = occurred_at + timedelta(minutes=1)
        async with engine.begin() as conn:
            before = (
                await conn.execute(
                    text("SELECT count(*) FROM sandbox_egress_audit WHERE occurred_at <= :cutoff"),
                    {"cutoff": cutoff},
                )
            ).scalar_one()
            await conn.execute(text("SET LOCAL ROLE retention_cleanup_worker"))
            deleted = await conn.execute(
                text("DELETE FROM sandbox_egress_audit WHERE occurred_at <= :cutoff"),
                {"cutoff": cutoff},
            )
        assert deleted.rowcount == before
    finally:
        await engine.dispose()
