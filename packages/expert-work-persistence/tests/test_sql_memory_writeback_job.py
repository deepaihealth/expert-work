"""B-168 —— ``SqlMemoryWritebackJobStore`` against Postgres.

The shared contract (``conftest.py``) pins SQL ≡ in-memory predicate
semantics; the concurrency test pins what an in-memory store cannot prove:
``FOR UPDATE SKIP LOCKED`` + the CAS update hand each job to exactly one
claimer, and a user's jobs never run side by side even when claimers race.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
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
from expert_work.persistence.memory.writeback_job import (
    MemoryWritebackJobStore,
    SqlMemoryWritebackJobStore,
)

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

Scenario = Callable[[MemoryWritebackJobStore], Awaitable[None]]


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def engine(postgres_container: PostgresContainer) -> Iterator[AsyncEngine]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")
    yield create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))


async def _fresh_store(engine: AsyncEngine) -> SqlMemoryWritebackJobStore:
    # ``claim_next`` is a cross-tenant scan: every scenario assumes the table
    # holds only its own rows.
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE memory_writeback_job"))
    return SqlMemoryWritebackJobStore(create_async_session_factory(engine))


@pytest.mark.asyncio
async def test_writeback_job_contract(
    engine: AsyncEngine, writeback_job_scenarios: tuple[Scenario, ...]
) -> None:
    try:
        for scenario in writeback_job_scenarios:
            await scenario(await _fresh_store(engine))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_claimers_never_share_a_job(engine: AsyncEngine) -> None:
    """(c) —— 并发领取:每条任务只给一个领取者;同一用户的任务不会同时在跑。"""
    try:
        store = await _fresh_store(engine)
        tenant = uuid4()
        now = datetime.now(UTC)
        users = [uuid4() for _ in range(6)]
        for user in users:
            for _ in range(3):  # three jobs per user
                await store.enqueue(
                    tenant_id=tenant,
                    user_id=user,
                    agent_name="a",
                    agent_version="1",
                    thread_id=uuid4(),
                    run_id=uuid4(),
                    trace_id=None,
                    now=now,
                )

        claimed = await asyncio.gather(
            *[store.claim_next(now=now, lease_s=300, max_attempts=3) for _ in range(12)]
        )
        jobs = [j for j in claimed if j is not None]
        ids = [j.id for j in jobs]
        assert len(ids) == len(set(ids))  # never the same job twice
        per_user = [j.user_id for j in jobs]
        assert len(per_user) == len(set(per_user))  # at most one running job per user
        assert set(per_user) == set(users)  # and every user got one
        assert all(j.attempts == 1 for j in jobs)
    finally:
        await engine.dispose()
