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
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

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
from orchestrator.tools import EditFileTool, SandboxOutcome, ToolContext, WriteFileTool

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


async def test_two_new_layout_conversations_of_one_user_run_concurrently(
    engine: AsyncEngine,
) -> None:
    """B-236 follow-up —— 同一用户的两个新布局对话各有各的 /workspace,写锁不互相挡。

    10-30 生产:一个用户名下 15 个并发 run 排在同一把按用户的锁上,100 字节的写等 3~6 分钟。
    """
    lock = PgWorkspaceLock(create_async_session_factory(engine))
    tenant, user = uuid4(), uuid4()
    conv_a, conv_b = uuid4(), uuid4()
    order: list[str] = []

    async def worker(name: str, conv: object) -> None:
        async with lock.acquire(tenant_id=tenant, user_id=user, conversation_id=conv):  # type: ignore[arg-type]
            order.append(f"{name}-enter")
            await asyncio.sleep(_HOLD_S)
            order.append(f"{name}-exit")

    await asyncio.gather(worker("A", conv_a), worker("B", conv_b))
    assert order[:2] in (["A-enter", "B-enter"], ["B-enter", "A-enter"])


async def test_one_new_layout_conversation_still_serialises(engine: AsyncEngine) -> None:
    """同一个对话(及其子代)共用一个 /workspace:一次 bash 的中间文件仍要按对话互斥。"""
    lock = PgWorkspaceLock(create_async_session_factory(engine))
    tenant, user, conv = uuid4(), uuid4(), uuid4()
    order: list[str] = []

    async def worker(name: str) -> None:
        async with lock.acquire(tenant_id=tenant, user_id=user, conversation_id=conv):
            order.append(f"{name}-enter")
            await asyncio.sleep(_HOLD_S)
            order.append(f"{name}-exit")

    await asyncio.gather(worker("A"), worker("B"))
    assert order in (
        ["A-enter", "A-exit", "B-enter", "B-exit"],
        ["B-enter", "B-exit", "A-enter", "A-exit"],
    )


async def test_two_old_layout_conversations_of_one_user_still_serialise(
    engine: AsyncEngine,
) -> None:
    """老布局的对话共用 agent 根:不带对话 id 的写仍按用户串行。"""
    lock = PgWorkspaceLock(create_async_session_factory(engine))
    tenant, user = uuid4(), uuid4()
    order: list[str] = []

    async def worker(name: str) -> None:
        async with lock.acquire(tenant_id=tenant, user_id=user, conversation_id=None):
            order.append(f"{name}-enter")
            await asyncio.sleep(_HOLD_S)
            order.append(f"{name}-exit")

    await asyncio.gather(worker("A"), worker("B"))
    assert order in (
        ["A-enter", "A-exit", "B-enter", "B-exit"],
        ["B-enter", "B-exit", "A-enter", "A-exit"],
    )


async def test_conversation_write_bumps_the_users_last_write_at(
    postgres_container: PostgresContainer,
) -> None:
    """RT-6 的 last_write_at 仍按 (tenant, user) 记,与锁按不按对话无关。"""
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
        await store.resolve(tenant_id=tenant, user_id=user)

        async with lock.acquire(tenant_id=tenant, user_id=user, conversation_id=uuid4()):
            pass
        after = await store.get(tenant_id=tenant, user_id=user)
        assert after is not None and after.last_write_at is not None
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# The agent-wide area, end to end through the real file tools: two new-layout
# conversations of one user editing the same /agent file serialise (no lost
# edit); their writes into their own /workspace still run side by side.
# ---------------------------------------------------------------------------

_OK_WRITE = '{"ok": true, "content_hash": "h", "size": 1, "path": "a.md"}'


@dataclass
class _SlowExecClient:
    """A sandbox whose exec takes ``_HOLD_S`` and logs enter / exit under ``name``."""

    name: str
    order: list[str]

    async def acquire(self, **_: Any) -> UUID:
        return uuid4()

    async def exec(self, **_: Any) -> SandboxOutcome:
        self.order.append(f"{self.name}-enter")
        await asyncio.sleep(_HOLD_S)
        self.order.append(f"{self.name}-exit")
        return SandboxOutcome(stdout=_OK_WRITE, stderr="", exit_code=0, timed_out=False)

    async def release(self, *, sandbox_id: UUID) -> None:
        return None

    async def destroy(self, *, sandbox_id: UUID, reason: str) -> None:
        return None

    async def reap(self, *, force: bool) -> int:
        return 0


async def _file_write(
    tool_name: str, lock: PgWorkspaceLock, ctx: ToolContext, client: _SlowExecClient, path: str
) -> None:
    if tool_name == "write_file":
        tool: WriteFileTool | EditFileTool = WriteFileTool(client=client, workspace_lock=lock)
        await tool.call({"path": path, "content": "x"}, ctx=ctx)
    else:
        tool = EditFileTool(client=client, workspace_lock=lock)
        await tool.call({"path": path, "old_string": "a", "new_string": "b"}, ctx=ctx)


def _conversation(tenant: UUID, user: UUID) -> ToolContext:
    return ToolContext(
        tenant_id=tenant,
        user_id=user,
        run_id=uuid4(),
        thread_id=uuid4(),
        agent_key="report-agent",
        workspace_layout="conversation",
    )


@pytest.mark.parametrize("tool_name", ["edit_file", "write_file"])
async def test_two_conversations_writing_one_agent_file_serialise(
    engine: AsyncEngine, tool_name: str
) -> None:
    lock = PgWorkspaceLock(create_async_session_factory(engine))
    tenant, user = uuid4(), uuid4()
    order: list[str] = []
    await asyncio.gather(
        _file_write(
            tool_name,
            lock,
            _conversation(tenant, user),
            _SlowExecClient("A", order),
            "/agent/AGENT_NOTES.md",
        ),
        _file_write(
            tool_name,
            lock,
            _conversation(tenant, user),
            _SlowExecClient("B", order),
            "/agent/AGENT_NOTES.md",
        ),
    )
    assert order in (
        ["A-enter", "A-exit", "B-enter", "B-exit"],
        ["B-enter", "B-exit", "A-enter", "A-exit"],
    )


@pytest.mark.parametrize("tool_name", ["edit_file", "write_file"])
async def test_two_conversations_writing_their_own_workspace_run_concurrently(
    engine: AsyncEngine, tool_name: str
) -> None:
    lock = PgWorkspaceLock(create_async_session_factory(engine))
    tenant, user = uuid4(), uuid4()
    order: list[str] = []
    await asyncio.gather(
        _file_write(
            tool_name, lock, _conversation(tenant, user), _SlowExecClient("A", order), "a.md"
        ),
        _file_write(
            tool_name, lock, _conversation(tenant, user), _SlowExecClient("B", order), "a.md"
        ),
    )
    assert order[:2] in (["A-enter", "B-enter"], ["B-enter", "A-enter"])


async def test_own_workspace_write_is_not_blocked_by_an_agent_file_write(
    engine: AsyncEngine,
) -> None:
    # 一个对话正在改 /agent 下的文件(持着 agent 键),另一个对话写自己的 /workspace 照常进行。
    lock = PgWorkspaceLock(create_async_session_factory(engine))
    tenant, user = uuid4(), uuid4()
    order: list[str] = []
    await asyncio.gather(
        _file_write(
            "edit_file",
            lock,
            _conversation(tenant, user),
            _SlowExecClient("A", order),
            "/agent/AGENT_NOTES.md",
        ),
        _file_write(
            "edit_file", lock, _conversation(tenant, user), _SlowExecClient("B", order), "a.md"
        ),
    )
    assert order[:2] in (["A-enter", "B-enter"], ["B-enter", "A-enter"])
