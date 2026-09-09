"""``FeedbackStore.upsert`` / ``list_for_thread_scoped`` / ``down_rated_thread_ids`` — P-2 PR1.

迁移 0152 的形状先在这里钉住(部分唯一索引真的拒绝重复),再对两套实现跑同一场景。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import DatabaseConfig, create_async_engine_from_config

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
async def migrated_engine(postgres_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        yield engine
    finally:
        await engine.dispose()


_INSERT = text(
    "INSERT INTO feedback (tenant_id, thread_id, run_id, rating, actor_id, source) "
    "VALUES (:t, :th, :r, 'down', :a, :s)"
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_0152_partial_unique_index_rejects_duplicate_run_actor(
    migrated_engine: AsyncEngine,
) -> None:
    """同 (tenant, run, actor) 第二行被 ``feedback_run_actor_uniq`` 拒绝。

    run_id 为 NULL 的老式行不受约束。
    """
    tenant, thread, run = uuid4(), uuid4(), uuid4()
    async with migrated_engine.begin() as conn:
        await conn.execute(
            _INSERT, {"t": tenant, "th": thread, "r": run, "a": "u-1", "s": "external"}
        )
    with pytest.raises(IntegrityError) as exc_info:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                _INSERT, {"t": tenant, "th": thread, "r": run, "a": "u-1", "s": "external"}
            )
    assert "feedback_run_actor_uniq" in str(exc_info.value)
    # 老式行(run_id NULL)想插几条插几条。
    async with migrated_engine.begin() as conn:
        for _ in range(2):
            await conn.execute(
                _INSERT, {"t": tenant, "th": thread, "r": None, "a": "u-1", "s": "console"}
            )
    # 上面那两条 NULL 行**证不出**部分谓词在不在:Postgres 唯一索引默认
    # NULLS DISTINCT,键里带 NULL 的行本来就互不相等,去掉 WHERE 一样能插。
    # 谓词本身只能直接读 indexdef 钉住(去掉 postgresql_where 这条就红)。
    async with migrated_engine.connect() as conn:
        indexdef = (
            await conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'feedback_run_actor_uniq'")
            )
        ).scalar_one()
    assert "WHERE (run_id IS NOT NULL)" in indexdef


@pytest.mark.integration
@pytest.mark.asyncio
async def test_0152_source_check_and_candidate_columns(migrated_engine: AsyncEngine) -> None:
    with pytest.raises(IntegrityError) as exc_info:
        async with migrated_engine.begin() as conn:
            await conn.execute(
                _INSERT, {"t": uuid4(), "th": uuid4(), "r": uuid4(), "a": "u", "s": "widget"}
            )
    assert "feedback_source_valid" in str(exc_info.value)
    async with migrated_engine.connect() as conn:
        cols = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'curation_candidate'"
                )
            )
        }
    assert {
        "feedback_run_id",
        "feedback_comment",
        "feedback_changed_at",
        "feedback_source",
    } <= cols
