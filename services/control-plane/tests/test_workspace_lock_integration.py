"""Integration test for the Stream TE-8 PG advisory workspace lock.

Boots a real Postgres (testcontainers) and drives :class:`PgWorkspaceLock`
from two concurrent coroutines on independent sessions/connections to prove
the cross-replica contract:

- two writers to the *same* workspace serialise (advisory lock is exclusive);
- writers to *different* workspaces run concurrently (no false contention);
- an ephemeral workspace (``user_id=None``) takes no lock.

No schema is needed — ``pg_advisory_xact_lock`` is a built-in.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from control_plane.workspace_lock import PgConversationSeedLock, PgWorkspaceLock
from expert_work.persistence import SqlUserWorkspaceStore
from expert_work.persistence.database import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)

pytestmark = pytest.mark.integration

_HOLD_S = 0.3

_ALEMBIC_INI = (
    Path(__file__).resolve().parents[3] / "packages" / "expert-work-persistence" / "alembic.ini"
)


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
async def engine(postgres_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine_from_config(
        DatabaseConfig(dsn=_async_dsn(postgres_container), pgbouncer_mode=False)
    )
    try:
        yield eng
    finally:
        await eng.dispose()


async def test_same_workspace_writes_serialise(engine: AsyncEngine) -> None:
    lock = PgWorkspaceLock(create_async_session_factory(engine))
    tenant, user = uuid4(), uuid4()
    order: list[str] = []

    async def worker(name: str) -> None:
        async with lock.acquire(tenant_id=tenant, user_id=user):
            order.append(f"{name}-enter")
            await asyncio.sleep(_HOLD_S)
            order.append(f"{name}-exit")

    await asyncio.gather(worker("A"), worker("B"))

    # Exclusive: one holder fully completes before the other enters.
    assert order in (
        ["A-enter", "A-exit", "B-enter", "B-exit"],
        ["B-enter", "B-exit", "A-enter", "A-exit"],
    )


async def test_different_workspaces_run_concurrently(engine: AsyncEngine) -> None:
    lock = PgWorkspaceLock(create_async_session_factory(engine))
    tenant = uuid4()
    user_a, user_b = uuid4(), uuid4()
    order: list[str] = []

    async def worker(name: str, user: object) -> None:
        async with lock.acquire(tenant_id=tenant, user_id=user):  # type: ignore[arg-type]
            order.append(f"{name}-enter")
            await asyncio.sleep(_HOLD_S)
            order.append(f"{name}-exit")

    await asyncio.gather(worker("A", user_a), worker("B", user_b))

    # Different keys don't contend: both enter before either exits.
    assert order[0].endswith("-enter")
    assert order[1].endswith("-enter")


async def test_ephemeral_workspace_takes_no_lock(engine: AsyncEngine) -> None:
    lock = PgWorkspaceLock(create_async_session_factory(engine))
    tenant = uuid4()
    order: list[str] = []

    async def worker(name: str) -> None:
        async with lock.acquire(tenant_id=tenant, user_id=None):
            order.append(f"{name}-enter")
            await asyncio.sleep(_HOLD_S)
            order.append(f"{name}-exit")

    await asyncio.gather(worker("A"), worker("B"))

    # user_id=None → no lock → concurrent.
    assert order[0].endswith("-enter")
    assert order[1].endswith("-enter")


async def test_lock_contention_no_starvation(engine: AsyncEngine) -> None:
    # TE-10 — N writers contend for one workspace lock. All must complete
    # exactly once (no deadlock, no starvation); advisory serialises them.
    lock = PgWorkspaceLock(create_async_session_factory(engine))
    tenant, user = uuid4(), uuid4()
    n_workers = 10
    completed: list[int] = []

    async def worker(idx: int) -> None:
        async with lock.acquire(tenant_id=tenant, user_id=user):
            completed.append(idx)
            await asyncio.sleep(0.02)

    await asyncio.gather(*(worker(i) for i in range(n_workers)))
    assert sorted(completed) == list(range(n_workers))


async def test_acquire_bumps_last_write_at(postgres_container: PostgresContainer) -> None:
    # RT-6 Tier B (RT-ADR-20) — a completed write under the lock records
    # last_write_at so a paused approval can detect workspace drift. Needs the
    # schema (the schema-less lock tests above silently no-op the best-effort bump).
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    eng = create_async_engine_from_config(
        DatabaseConfig(dsn=_async_dsn(postgres_container), pgbouncer_mode=False)
    )
    try:
        sf = create_async_session_factory(eng)
        store = SqlUserWorkspaceStore(sf)
        lock = PgWorkspaceLock(sf)
        tenant, user = uuid4(), uuid4()

        # A fresh workspace row starts unbumped.
        await store.resolve(tenant_id=tenant, user_id=user)
        before = await store.get(tenant_id=tenant, user_id=user)
        assert before is not None and before.last_write_at is None

        # A successful write under the lock bumps it.
        async with lock.acquire(tenant_id=tenant, user_id=user):
            pass
        after = await store.get(tenant_id=tenant, user_id=user)
        assert after is not None and after.last_write_at is not None
    finally:
        await eng.dispose()


async def test_lock_distinct_workspaces_scale_concurrently(engine: AsyncEngine) -> None:
    # TE-10 — distinct workspaces must NOT serialise: their holds overlap.
    lock = PgWorkspaceLock(create_async_session_factory(engine))
    tenant = uuid4()
    users = [uuid4() for _ in range(5)]
    state = {"cur": 0, "max": 0}

    async def worker(u: object) -> None:
        async with lock.acquire(tenant_id=tenant, user_id=u):  # type: ignore[arg-type]
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
            await asyncio.sleep(0.1)
            state["cur"] -= 1

    await asyncio.gather(*(worker(u) for u in users))
    assert state["max"] >= 2  # genuine concurrency across workspaces


async def test_conversation_seed_lock_serialises_one_conversation_only(
    engine: AsyncEngine,
) -> None:
    """B-236 —— 开场复制按对话互斥:同一个对话的两个持有者串行,不同对话并发。"""
    lock = PgConversationSeedLock(create_async_session_factory(engine))
    tenant = uuid4()
    same, other = f"{tenant}:{uuid4()}", f"{tenant}:{uuid4()}"
    order: list[str] = []

    async def worker(name: str, key: str) -> None:
        async with lock.acquire(key=key):
            order.append(f"{name}-enter")
            await asyncio.sleep(_HOLD_S)
            order.append(f"{name}-exit")

    await asyncio.gather(worker("A", same), worker("B", same))
    assert order in (
        ["A-enter", "A-exit", "B-enter", "B-exit"],
        ["B-enter", "B-exit", "A-enter", "A-exit"],
    )
    order.clear()
    await asyncio.gather(worker("A", same), worker("C", other))
    assert order[:2] in (["A-enter", "C-enter"], ["C-enter", "A-enter"])


async def test_conversation_seed_lock_does_not_hold_the_users_write_lock(
    engine: AsyncEngine,
) -> None:
    """独立 classid:复制期间同一用户的写(``PgWorkspaceLock``)照常进行。"""
    factory = create_async_session_factory(engine)
    seed, write = PgConversationSeedLock(factory), PgWorkspaceLock(factory)
    tenant, user = uuid4(), uuid4()
    entered = asyncio.Event()

    async def seeding() -> None:
        async with seed.acquire(key=f"{tenant}:{uuid4()}"):
            entered.set()
            await asyncio.sleep(_HOLD_S * 3)

    task = asyncio.create_task(seeding())
    await entered.wait()
    async with asyncio.timeout(_HOLD_S):
        async with write.acquire(tenant_id=tenant, user_id=user):
            pass
    _ = await task
