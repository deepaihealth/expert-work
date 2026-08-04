"""Integration tests for ``SqlSandboxInstanceStore`` against a real Postgres

(波 1 Task 7 — Agent Sandbox warm-session CAS, migration 0141).

The concurrent-claim test is the one thing an in-memory fake structurally
cannot verify: whether the ``sandbox_instance (tenant_id, user_id) WHERE
state = 'IN_USE' AND destroyed_at IS NULL AND user_id IS NOT NULL`` partial
unique index (migration 0141) actually serialises two real, simultaneous
transactions into exactly one winner.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import update
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.models import SandboxInstanceRow
from expert_work.persistence.sandbox_instance_store import _IDLE_TTL_S, SqlSandboxInstanceStore

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def store(postgres_container: PostgresContainer) -> Iterator[SqlSandboxInstanceStore]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    factory = create_async_session_factory(engine)
    yield SqlSandboxInstanceStore(factory)


@pytest.mark.asyncio
async def test_claim_warm_first_caller_wins(store: SqlSandboxInstanceStore) -> None:
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()

    result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id)

    assert result is None
    assert await store.get_container_id(sandbox_id=sandbox_id) is None


@pytest.mark.asyncio
async def test_claim_warm_second_caller_sees_ready_container(
    store: SqlSandboxInstanceStore,
) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    first_id = uuid4()
    assert await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=first_id) is None
    await store.set_container_id(sandbox_id=first_id, container_id="sbx-ready")

    second_id = uuid4()
    result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=second_id)

    # Review fix (Important-3): the loser gets back the WINNER's real row
    # id (first_id), not its own second_id — acquire() needs a persisted id
    # to hand its caller, and second_id was never inserted anywhere.
    assert result == (first_id, "sbx-ready")
    # The loser's own row was never inserted (its INSERT conflicted).
    assert await store.get_container_id(sandbox_id=second_id) is None


@pytest.mark.asyncio
async def test_claim_warm_second_caller_raises_when_winner_not_ready(
    store: SqlSandboxInstanceStore,
) -> None:
    """task-7-report.md's correction to the brief/design-spec prose: a
    conflicting claim whose owner hasn't finished ``create()`` yet
    (``container_id`` still NULL — E2B cold start is 35-40s, a wide,
    common window) must fail loudly, not hand back an unusable value or
    let the caller silently build a second sandbox for this session.
    """
    tenant_id, user_id = uuid4(), uuid4()
    first_id = uuid4()
    assert await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=first_id) is None
    # Deliberately do NOT call set_container_id — simulates "still creating".

    with pytest.raises(RuntimeError, match="already being created"):
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4())


@pytest.mark.asyncio
async def test_concurrent_claim_warm_exactly_one_winner(store: SqlSandboxInstanceStore) -> None:
    """The real-concurrency case Task 7's brief calls out: two coroutines
    ``claim_warm`` the SAME ``(tenant, user)`` at once, only one may win the
    partial unique index. Neither has created anything yet, so per
    ``SqlSandboxInstanceStore.claim_warm``'s documented contract the loser
    observes a not-ready claim and raises — the in-memory fake cannot prove
    this (it never races two real transactions against the same DB
    constraint), only a real Postgres can.
    """
    tenant_id, user_id = uuid4(), uuid4()
    sandbox_a, sandbox_b = uuid4(), uuid4()

    results = await asyncio.gather(
        store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_a),
        store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_b),
        return_exceptions=True,
    )

    winners = [r for r in results if r is None]
    losers = [r for r in results if isinstance(r, RuntimeError)]
    assert len(winners) == 1, f"exactly one caller should win the claim, got {results!r}"
    assert len(losers) == 1, f"the other should observe a not-ready claim, got {results!r}"
    # No stray winner-row leaked from the losing attempt.
    won_id = sandbox_a if results[0] is None else sandbox_b
    lost_id = sandbox_b if won_id is sandbox_a else sandbox_a
    assert await store.get_container_id(sandbox_id=won_id) is None
    assert await store.get_container_id(sandbox_id=lost_id) is None


@pytest.mark.asyncio
async def test_concurrent_claim_warm_after_ready_all_see_same_winner(
    store: SqlSandboxInstanceStore,
) -> None:
    """Once a warm session is ready, N concurrent late-arriving claims must
    all resolve to the SAME container_id — no duplicate row, no crash."""
    tenant_id, user_id = uuid4(), uuid4()
    winner_id = uuid4()
    won = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=winner_id)
    assert won is None
    await store.set_container_id(sandbox_id=winner_id, container_id="sbx-warm")

    results = await asyncio.gather(
        *(
            store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4())
            for _ in range(5)
        )
    )

    assert results == [(winner_id, "sbx-warm")] * 5


@pytest.mark.asyncio
async def test_drop_warm_frees_slot_for_new_claim(store: SqlSandboxInstanceStore) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    first_id = uuid4()
    assert await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=first_id) is None

    await store.drop_warm(tenant_id=tenant_id, user_id=user_id)

    second_id = uuid4()
    result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=second_id)
    assert result is None, "dropping the dead claim must free the 0141 partial-index slot"


@pytest.mark.asyncio
async def test_mark_destroyed_frees_slot_for_new_claim(store: SqlSandboxInstanceStore) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    first_id = uuid4()
    assert await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=first_id) is None
    await store.set_container_id(sandbox_id=first_id, container_id="sbx-1")

    await store.mark_destroyed(sandbox_id=first_id, reason="ops")

    assert await store.get_container_id(sandbox_id=first_id) == "sbx-1", (
        "mark_destroyed keeps the historical container_id — only state/"
        "destroyed_at/destroy_reason transition"
    )
    second_id = uuid4()
    result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=second_id)
    assert result is None, "destroying the old session must free the slot for a fresh claim"


@pytest.mark.asyncio
async def test_get_container_id_unknown_sandbox_returns_none(
    store: SqlSandboxInstanceStore,
) -> None:
    assert await store.get_container_id(sandbox_id=uuid4()) is None


# ---------------------------------------------------------------------------
# 波 1 Task 9 —— list_active(only_idle), AgentSandboxClient.reap 用。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_active_excludes_still_creating_and_destroyed_rows(
    store: SqlSandboxInstanceStore,
) -> None:
    """还没回填 container_id(还在创建中)、已经 mark_destroyed 的行,两种
    模式(``only_idle`` True/False)都不该出现在 ``list_active()`` 里 ——
    前者连不上也没什么可 kill,后者已经不是"活跃"会话。

    断言只看"我自己建的两个 id 在不在结果里",不比对整个返回列表 ——
    ``postgres_container`` 是 session 级 fixture(见
    ``conftest.py``/``test_sql_knowledge_store.py`` 里同样的说明),同一个
    库被这个文件里其它测试共用,``list_active()`` 又是故意不按 tenant 限定
    的全表扫(``reap()`` 就是要扫全平台),断言"结果为空列表"在这个文件的
    执行顺序下会被之前测试遗留的行(比如
    ``test_claim_warm_second_caller_sees_ready_container`` 留下的
    ``sbx-ready``)误伤。
    """
    tenant_id, user_id = uuid4(), uuid4()
    still_creating = uuid4()
    assert (
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=still_creating)
        is None
    )
    # 不调用 set_container_id —— 模拟"还在创建中"。

    other_tenant, other_user = uuid4(), uuid4()
    destroyed = uuid4()
    assert (
        await store.claim_warm(tenant_id=other_tenant, user_id=other_user, sandbox_id=destroyed)
        is None
    )
    await store.set_container_id(sandbox_id=destroyed, container_id="sbx-destroyed")
    await store.mark_destroyed(sandbox_id=destroyed, reason="ops")

    force_ids = {sid for sid, _ in await store.list_active(only_idle=False)}
    idle_ids = {sid for sid, _ in await store.list_active(only_idle=True)}
    assert still_creating not in force_ids
    assert destroyed not in force_ids
    assert still_creating not in idle_ids
    assert destroyed not in idle_ids


@pytest.mark.asyncio
async def test_list_active_force_mode_returns_every_ready_active_row(
    store: SqlSandboxInstanceStore,
) -> None:
    """``only_idle=False``(``force`` 路径)返回所有 ready 的活跃行,不看
    ``acquired_at``/``last_used_at`` 时间戳——刚 acquire 的一行也照样在内。

    见上一条测试的 docstring:断言只看自己建的这一个 id,不对整个返回列表
    做相等比较(session 级共享容器,别的测试遗留的行也会出现在无租户限定
    的全表扫里)。
    """
    tenant_id, user_id = uuid4(), uuid4()
    sandbox_id = uuid4()
    won = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id)
    assert won is None
    await store.set_container_id(sandbox_id=sandbox_id, container_id="sbx-1")

    force_active = dict(await store.list_active(only_idle=False))
    assert force_active.get(sandbox_id) == "sbx-1"
    # 刚 acquire,远没到空闲 TTL —— only_idle=True 不该包含这一行。
    idle_ids = {sid for sid, _ in await store.list_active(only_idle=True)}
    assert sandbox_id not in idle_ids


@pytest.mark.asyncio
async def test_list_active_only_idle_uses_last_used_at_falling_back_to_acquired_at(
    store: SqlSandboxInstanceStore, postgres_container: PostgresContainer
) -> None:
    """口径同本地 docker-supervisor 自己的 reaper(``_session_idle``,见
    ``sandbox_supervisor/store.py``):以 ``last_used_at`` 为准,缺失才退回
    ``acquired_at``。三行:

    - ``stale``:``acquired_at`` 很久以前,没有 ``last_used_at`` → 空闲。
    - ``fresh_no_use``:``acquired_at`` 刚刚,没有 ``last_used_at`` → 不空闲。
    - ``stale_acquire_but_used``:``acquired_at`` 很久以前但 ``last_used_at``
      是刚刚 → 不空闲(``last_used_at`` 优先于 ``acquired_at``,不是"取更早
      的那个")。

    store 的公开 API 没有任何方法能回填 ``last_used_at``(见
    ``SandboxInstanceStore`` Protocol docstring 的"已知缺口")——这里用
    ``test_sql_knowledge_store.py`` 同款的 raw ``UPDATE`` backdate 手法直接
    改列,绕过公开 API 制造"很久以前"这个公开 API 达不到的前置状态。
    """
    names = ("stale", "fresh_no_use", "stale_acquire_but_used")
    sandbox_ids: dict[str, UUID] = {}
    for name in names:
        tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
        assert (
            await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id)
            is None
        )
        await store.set_container_id(sandbox_id=sandbox_id, container_id=f"sbx-{name}")
        sandbox_ids[name] = sandbox_id

    long_ago = datetime.now(UTC) - timedelta(seconds=_IDLE_TTL_S * 2)
    recent = datetime.now(UTC)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        async with engine.begin() as conn:
            await conn.execute(
                update(SandboxInstanceRow)
                .where(SandboxInstanceRow.id == sandbox_ids["stale"])
                .values(acquired_at=long_ago)
            )
            await conn.execute(
                update(SandboxInstanceRow)
                .where(SandboxInstanceRow.id == sandbox_ids["stale_acquire_but_used"])
                .values(acquired_at=long_ago, last_used_at=recent)
            )
    finally:
        await engine.dispose()

    # 只看这三个自建 id 里谁在结果里,不对整个返回列表做相等比较(session
    # 级共享容器,理由同上面两条测试的 docstring)。
    idle_ids = {sandbox_id for sandbox_id, _ in await store.list_active(only_idle=True)}
    assert sandbox_ids["stale"] in idle_ids
    assert sandbox_ids["fresh_no_use"] not in idle_ids
    assert sandbox_ids["stale_acquire_but_used"] not in idle_ids
