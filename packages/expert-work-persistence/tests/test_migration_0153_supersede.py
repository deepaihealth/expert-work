"""迁移 0153 三列 + SqlRunStore / SqlThreadMessageStore 的 P-1 写读。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect
from testcontainers.postgres import PostgresContainer

from expert_work.persistence.database import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.thread_message import MessageTurn, SqlThreadMessageStore
from expert_work.persistence.thread_meta import SqlThreadMetaStore
from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus
from expert_work.runtime.runs.store import SqlRunStore

pytestmark = pytest.mark.integration
ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


def _column_names(sync_conn: Connection, table: str) -> set[str]:
    """具名而非 lambda —— CI 的 mypy 也扫 tests,lambda 会挂 ``no-untyped-call``。"""
    return {c["name"] for c in inspect(sync_conn).get_columns(table)}


def _agent_run_columns(sync_conn: Connection) -> set[str]:
    return _column_names(sync_conn, "agent_run")


def _thread_message_columns(sync_conn: Connection) -> set[str]:
    return _column_names(sync_conn, "thread_message")


@pytest.mark.asyncio
async def test_0153_columns_and_sql_stores_round_trip(
    postgres_container: PostgresContainer,
) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        async with engine.connect() as conn:
            cols = await conn.run_sync(_agent_run_columns)
            tm_cols = await conn.run_sync(_thread_message_columns)
        assert {"superseded_by_run_id", "regenerated_from_run_id"} <= cols
        assert "superseded_by" in tm_cols

        sf = create_async_session_factory(engine)
        runs, msgs, threads = SqlRunStore(sf), SqlThreadMessageStore(sf), SqlThreadMetaStore(sf)
        tenant, thread, old, new = uuid4(), uuid4(), uuid4(), uuid4()
        now = datetime.now(UTC)
        await threads.create(thread_id=thread, tenant_id=tenant, created_by="p1-test")
        await runs.create(
            RunInfo(
                run_id=old,
                tenant_id=tenant,
                thread_id=thread,
                user_id=None,
                status=RunStatus.SUCCESS,
                on_disconnect=DisconnectMode.CONTINUE,
                is_resume=False,
                error=None,
                created_at=now,
                updated_at=now,
                finished_at=now,
            )
        )
        await runs.create(
            RunInfo(
                run_id=new,
                tenant_id=tenant,
                thread_id=thread,
                user_id=None,
                status=RunStatus.PENDING,
                on_disconnect=DisconnectMode.CONTINUE,
                is_resume=True,
                error=None,
                created_at=now,
                updated_at=now,
                finished_at=None,
                regenerated_from_run_id=old,
            )
        )
        assert (
            await runs.mark_superseded(run_id=old, tenant_id=tenant, superseded_by_run_id=new)
            is True
        )
        old_row = await runs.get(run_id=old, tenant_id=tenant)
        new_row = await runs.get(run_id=new, tenant_id=tenant)
        assert old_row is not None and old_row.superseded_by_run_id == new
        assert new_row is not None and new_row.regenerated_from_run_id == old

        await msgs.sync_thread(
            thread_id=thread,
            tenant_id=tenant,
            synced_at=now,
            turns=[MessageTurn(seq=s, role="user", content=f"m{s}") for s in (1, 6, 8)],
        )
        assert (
            await msgs.mark_superseded(
                thread_id=thread, tenant_id=tenant, seq_from=5, seq_to=10, superseded_by=new
            )
            == 2
        )
    finally:
        await engine.dispose()
