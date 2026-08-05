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
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.models import SandboxInstanceRow
from expert_work.persistence.sandbox_instance_store import (
    _IDLE_TTL_S,
    _REASON_STUCK_CREATE_TAKEOVER,
    _STUCK_CREATE_TTL_S,
    AGENT_SANDBOX_IMAGE_REF,
    SqlSandboxInstanceStore,
)

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
async def test_mark_destroyed_frees_the_slot_of_a_row_that_never_got_a_container(
    store: SqlSandboxInstanceStore,
) -> None:
    """再审 Important-1 —— 这条接替了删掉的 ``test_drop_warm_frees_slot_for_new_claim``。

    ``drop_warm``(按 ``(tenant, user)`` 坐标删)已经删除:C-1 的接管让"槽位
    当前的主人不是我"成为可达状态,按坐标删会误伤接管者。所有清理点改走
    ``mark_destroyed(sandbox_id=...)``,所以要证的不变式变成"按 id 的终态写
    对一个 ``container_id`` 还是 NULL 的行**同样**腾得出 0141 槽位"——真
    Postgres 才验得了这一点(0141 是部分唯一索引,内存实现不存在这个约束)。
    """
    tenant_id, user_id = uuid4(), uuid4()
    first_id = uuid4()
    assert await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=first_id) is None

    await store.mark_destroyed(sandbox_id=first_id, reason="warm_reconnect_failed")

    second_id = uuid4()
    result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=second_id)
    assert result is None, "clearing the dead row by id must free the 0141 partial-index slot"


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
# 波 1 Task 10 —— create_ephemeral(不带 user_id 的临时沙箱)。
#
# Task 10 契约测试真连 E2B 测试集群跑通契约测试时实测揪出的 bug:
# acquire(user_id=None) 从未经过 claim_warm,过去没有任何方法给这类行做初始
# INSERT——acquire 结尾无条件的 set_container_id 是对一个从未插入的行做
# UPDATE,SQL 是静默 0 行生效。全仓所有既有测试(FakeInstanceStore 与两个
# 生产 store)的 acquire 调用此前无一例外都传了 user_id,这条路径完全没有
# 测试覆盖过,直到契约测试第一次用真实 store 跑通"acquire 不带 user_id 再
# exec"这条链路才当场炸穿。完整理由见
# orchestrator.tools.sandbox_instance_store.SandboxInstanceStore.create_ephemeral 的
# docstring。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_ephemeral_row_is_then_updatable(store: SqlSandboxInstanceStore) -> None:
    """核心回归测试:``create_ephemeral`` 必须真的 INSERT 一行,不只是"不
    报错"——用 ``set_container_id`` 后能否读回来证明行真的存在。如果没有
    INSERT,``set_container_id`` 是静默 0 行 UPDATE,``get_container_id``
    永远读到 ``None``,跟"这行压根不存在"没法区分——这正是生产 bug 的症状
    本身,断言必须验证"能读到写入的值",不能只验证"不抛异常"。
    """
    tenant_id, sandbox_id = uuid4(), uuid4()

    await store.create_ephemeral(tenant_id=tenant_id, sandbox_id=sandbox_id)
    await store.set_container_id(sandbox_id=sandbox_id, container_id="sbx-ephemeral")

    assert await store.get_container_id(sandbox_id=sandbox_id) == "sbx-ephemeral"


@pytest.mark.asyncio
async def test_create_ephemeral_rows_do_not_conflict_across_calls(
    store: SqlSandboxInstanceStore,
) -> None:
    """迁移 0141 的部分唯一索引条件显式排除 ``user_id IS NULL`` 的行——同一
    个 tenant 建多个临时沙箱不该像热会话那样撞 CAS 冲突。"""
    tenant_id = uuid4()
    first_id, second_id = uuid4(), uuid4()

    await store.create_ephemeral(tenant_id=tenant_id, sandbox_id=first_id)
    await store.create_ephemeral(tenant_id=tenant_id, sandbox_id=second_id)
    await store.set_container_id(sandbox_id=first_id, container_id="sbx-a")
    await store.set_container_id(sandbox_id=second_id, container_id="sbx-b")

    assert await store.get_container_id(sandbox_id=first_id) == "sbx-a"
    assert await store.get_container_id(sandbox_id=second_id) == "sbx-b"


@pytest.mark.asyncio
async def test_ephemeral_row_is_visible_to_list_active_once_ready(
    store: SqlSandboxInstanceStore,
) -> None:
    """独立于 exec/destroy 之外的另一个后果:修复前临时沙箱永远不会出现在
    ``list_active()`` 里,``reap(force=True)`` 找不到它们——资源永久泄漏
    (沙箱在 E2B 那边继续跑,``sandbox_instance`` 里却没有任何记录)。"""
    tenant_id, sandbox_id = uuid4(), uuid4()
    await store.create_ephemeral(tenant_id=tenant_id, sandbox_id=sandbox_id)
    await store.set_container_id(sandbox_id=sandbox_id, container_id="sbx-reapable")

    force_ids = {sid for sid, _ in await store.list_active(only_idle=False)}
    assert sandbox_id in force_ids


@pytest.mark.asyncio
async def test_ephemeral_row_still_creating_is_excluded_from_list_active(
    store: SqlSandboxInstanceStore,
) -> None:
    """镜像 ``test_list_active_excludes_still_creating_and_destroyed_rows``
    对热会话行的覆盖:临时沙箱 ``container_id`` 仍是 NULL(还在冷启窗口)
    时,两种 ``only_idle`` 模式都不该把它算进"活跃"——语义与热会话行完全
    一致,``list_active`` 的 WHERE 子句本就不区分 ``user_id`` 是否为空。"""
    tenant_id, sandbox_id = uuid4(), uuid4()
    await store.create_ephemeral(tenant_id=tenant_id, sandbox_id=sandbox_id)
    # 不调用 set_container_id —— 模拟"还在创建中"。

    force_ids = {sid for sid, _ in await store.list_active(only_idle=False)}
    idle_ids = {sid for sid, _ in await store.list_active(only_idle=True)}
    assert sandbox_id not in force_ids
    assert sandbox_id not in idle_ids


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


# ---------------------------------------------------------------------------
# 独立审查 Important-1/2(task-9-report.md 有完整记录)。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_touch_and_get_container_id_returns_container_id(
    store: SqlSandboxInstanceStore,
) -> None:
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    won = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id)
    assert won is None
    await store.set_container_id(sandbox_id=sandbox_id, container_id="sbx-touch")

    result = await store.touch_and_get_container_id(sandbox_id=sandbox_id)

    assert result == "sbx-touch"


@pytest.mark.asyncio
async def test_touch_and_get_container_id_unknown_sandbox_returns_none(
    store: SqlSandboxInstanceStore,
) -> None:
    assert await store.touch_and_get_container_id(sandbox_id=uuid4()) is None


@pytest.mark.asyncio
async def test_touch_and_get_container_id_makes_row_no_longer_idle(
    store: SqlSandboxInstanceStore, postgres_container: PostgresContainer
) -> None:
    """Important-2 的直接payoff:``exec()`` 用这个方法一次往返内完成"读
    container_id" + "推进 last_used_at",不再需要分两次 round trip——这里
    验证的是它对 ``list_active(only_idle=True)`` 的实际效果,不只是"字段被
    写了"这种字面断言:一行 ``acquired_at`` 很久以前、本该被判定空闲的行,
    touch 之后必须不再被判定为空闲(否则一个持续被 exec 的热会话仍然会被
    误杀,Important-2 就白修了)。
    """
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    won = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id)
    assert won is None
    await store.set_container_id(sandbox_id=sandbox_id, container_id="sbx-touch-idle")

    long_ago = datetime.now(UTC) - timedelta(seconds=_IDLE_TTL_S * 2)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        async with engine.begin() as conn:
            await conn.execute(
                update(SandboxInstanceRow)
                .where(SandboxInstanceRow.id == sandbox_id)
                .values(acquired_at=long_ago)
            )
    finally:
        await engine.dispose()

    idle_before = {sid for sid, _ in await store.list_active(only_idle=True)}
    assert sandbox_id in idle_before, "acquired_at 很久以前、没有 last_used_at —— 该被判空闲"

    container_id = await store.touch_and_get_container_id(sandbox_id=sandbox_id)
    assert container_id == "sbx-touch-idle"

    idle_after = {sid for sid, _ in await store.list_active(only_idle=True)}
    assert sandbox_id not in idle_after, "touch 之后 last_used_at 是刚刚 —— 不该再被判空闲"


@pytest.mark.asyncio
async def test_list_stuck_creating_ignores_fresh_still_creating_row(
    store: SqlSandboxInstanceStore,
) -> None:
    """Important-1:刚 ``claim_warm``、``container_id`` 还没回填的行,正常
    还在合法的 35-40s E2B 冷启窗口内,不该被判定成"孤儿"——只有远超这个
    窗口(``_STUCK_CREATE_TTL_S``,数分钟量级)的行才算。"""
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    won = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id)
    assert won is None
    # 不调用 set_container_id —— 模拟正常、还在冷启窗口内的创建过程。

    stuck_ids = set(await store.list_stuck_creating())
    assert sandbox_id not in stuck_ids


@pytest.mark.asyncio
async def test_list_stuck_creating_returns_old_null_container_row(
    store: SqlSandboxInstanceStore, postgres_container: PostgresContainer
) -> None:
    """Important-1 的核心场景:编排进程在 ``claim_warm`` 提交行之后、
    ``set_container_id`` 回填之前死掉(pod OOM-kill / 驱逐 / 滚动更新)——
    这行永远不会再有 ``container_id``,也永远不会出现在
    ``list_active()`` 的任何一种模式里(见其测试)。只能靠 ``acquired_at``
    远超合法冷启窗口来识别,直接造一行"死在两步之间"的状态验证。
    """
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    won = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id)
    assert won is None
    # 不调用 set_container_id —— 模拟"死在两次写之间"。

    long_ago = datetime.now(UTC) - timedelta(seconds=_STUCK_CREATE_TTL_S * 2)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        async with engine.begin() as conn:
            await conn.execute(
                update(SandboxInstanceRow)
                .where(SandboxInstanceRow.id == sandbox_id)
                .values(acquired_at=long_ago)
            )
    finally:
        await engine.dispose()

    stuck_ids = set(await store.list_stuck_creating())
    assert sandbox_id in stuck_ids

    # force=True 的 reap 清理这类孤儿的做法(见 AgentSandboxClient.reap):
    # 没有容器可连,直接 mark_destroyed —— 验证清完之后槽位真的放出来了。
    await store.mark_destroyed(sandbox_id=sandbox_id, reason="reap_orphaned_create")
    second_id = uuid4()
    result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=second_id)
    assert result is None, "清掉孤儿行之后,这个 (tenant, user) 必须能重新正常 claim"


@pytest.mark.asyncio
async def test_list_stuck_creating_excludes_ready_row(
    store: SqlSandboxInstanceStore, postgres_container: PostgresContainer
) -> None:
    """已经回填了 ``container_id``(ready)的行,不管 ``acquired_at`` 多老
    都不算"孤儿创建中"。``acquired_at`` 也 backdate 到远超阈值——如果只留
    "刚 claim_warm、还新鲜"这一个理由,这条测试测不出"container_id IS
    NULL"这个过滤条件是不是真的存在(该行本来就会因为"太新"被排除,与
    container_id 是否为空无关;首次实现时用变异验证过这个坑:单独去掉
    container_id 过滤条件,不 backdate 的版本三条全绿,咬不住)。
    """
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    won = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id)
    assert won is None
    await store.set_container_id(sandbox_id=sandbox_id, container_id="sbx-ready-not-stuck")

    long_ago = datetime.now(UTC) - timedelta(seconds=_STUCK_CREATE_TTL_S * 2)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        async with engine.begin() as conn:
            await conn.execute(
                update(SandboxInstanceRow)
                .where(SandboxInstanceRow.id == sandbox_id)
                .values(acquired_at=long_ago)
            )
    finally:
        await engine.dispose()

    stuck_ids = set(await store.list_stuck_creating())
    assert sandbox_id not in stuck_ids


# ---------------------------------------------------------------------------
# 全分支终审 Critical-1 —— claim_warm 接管过期的"卡在创建中"行。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_warm_takes_over_a_stale_mid_create_row(
    store: SqlSandboxInstanceStore, postgres_container: PostgresContainer
) -> None:
    """Critical-1 的核心场景:编排进程死在 ``claim_warm`` 提交行与
    ``set_container_id`` 回填之间(pod OOM-kill / 驱逐 / 滚动更新),那行
    永远停在 ``IN_USE`` + ``container_id=NULL``。

    修复前:该 ``(tenant, user)`` 之后**每一次** ``claim_warm`` 都撞
    "already being created" 的 raise,而仓内没有任何自动路径会解开它
    (波 1 验收期间真的发生过一次,靠人工 force-reap 才恢复)。
    修复后:超过 ``_STUCK_CREATE_TTL_S`` 的这类行被接管——清掉、重试
    INSERT,本次调用正常拿到槽位(返回 ``None`` = 我是新赢家)。

    ``test_claim_warm_second_caller_raises_when_winner_not_ready`` 是这条的
    反向:同样的 NULL-container 行,但**还新鲜**(在合法冷启窗口内)时仍
    然 raise,不会被接管。两条一起钉住"接管必须带时限"这个不变式。
    """
    tenant_id, user_id = uuid4(), uuid4()
    orphan_id = uuid4()
    assert (
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=orphan_id) is None
    )
    # 不调用 set_container_id —— 模拟"死在两次写之间";再 backdate
    # acquired_at 到远超阈值,公开 API 造不出"很久以前"这个前置状态。
    long_ago = datetime.now(UTC) - timedelta(seconds=_STUCK_CREATE_TTL_S * 2)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        async with engine.begin() as conn:
            await conn.execute(
                update(SandboxInstanceRow)
                .where(SandboxInstanceRow.id == orphan_id)
                .values(acquired_at=long_ago)
            )
    finally:
        await engine.dispose()

    newcomer_id = uuid4()
    result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=newcomer_id)

    assert result is None, "过期的孤儿行必须被接管,这次调用应当成为新赢家"
    # 新行真的插进去了(不是"raise 没发生"这种弱断言):它现在是这个
    # (tenant, user) 唯一的活跃行,且 container_id 待回填。
    assert await store.get_container_id(sandbox_id=newcomer_id) is None
    await store.set_container_id(sandbox_id=newcomer_id, container_id="sbx-after-takeover")
    assert await store.get_container_id(sandbox_id=newcomer_id) == "sbx-after-takeover"


@pytest.mark.asyncio
async def test_claim_warm_takeover_marks_the_orphan_with_its_own_reason(
    store: SqlSandboxInstanceStore, postgres_container: PostgresContainer
) -> None:
    """接管走的是终态写(腾出 0141 部分唯一索引的槽位),并且写的是一个
    **专属** ``destroy_reason``——运维读这一列时要能分清槽位是被谁清的:
    ``warm_reconnect_failed``(重连失败)/ ``reap_orphaned_create``(强制清扫)/ 这里
    的 ``stuck_create_takeover``(后来的 acquire 自己发现并接管,三者里唯一
    不需要人工动作也不依赖 reaper 周期的一条)。
    """
    tenant_id, user_id, orphan_id = uuid4(), uuid4(), uuid4()
    assert (
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=orphan_id) is None
    )
    long_ago = datetime.now(UTC) - timedelta(seconds=_STUCK_CREATE_TTL_S * 2)
    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        async with engine.begin() as conn:
            await conn.execute(
                update(SandboxInstanceRow)
                .where(SandboxInstanceRow.id == orphan_id)
                .values(acquired_at=long_ago)
            )

        assert (
            await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4()) is None
        )

        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(
                        SandboxInstanceRow.state,
                        SandboxInstanceRow.destroyed_at,
                        SandboxInstanceRow.destroy_reason,
                    ).where(SandboxInstanceRow.id == orphan_id)
                )
            ).one()
    finally:
        await engine.dispose()

    state, destroyed_at, destroy_reason = row
    assert state == "DESTROYED"
    assert destroyed_at is not None
    assert destroy_reason == _REASON_STUCK_CREATE_TAKEOVER


# ---------------------------------------------------------------------------
# 再审 Important-2 —— set_container_id 对"行不在/已销毁"的处置。
#
# 这是两个 store 语义分歧最要命的一处:SQL 侧原来是裸 UPDATE ... WHERE id,
# 0 行照样提交、照样返回 None(成功),而内存实现是 KeyError。C-1 的接管与
# acquire 的重建路径都让这个输入变成**可达**的,两个后端于是在同一个生产输入
# 上走了相反的分支。现在两边都 raise —— 下面三条钉 SQL 侧,内存侧的同名三条
# 在 test_in_memory_sandbox_instance_store.py。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_container_id_rejects_a_row_that_never_existed(
    store: SqlSandboxInstanceStore,
) -> None:
    """从未 INSERT 过的 id:此前是 0 行 UPDATE + 静默成功。

    静默的代价不是"少写一行":调用方 ``acquire`` 会带着一个哪儿都不存在的
    ``sandbox_id`` 正常返回,而它的 microVM 已经在平台上跑着 —— destroy /
    reap / list_stuck_creating 三条回收路径全都按行找,谁也看不见它。
    """
    with pytest.raises(RuntimeError, match="is gone"):
        await store.set_container_id(sandbox_id=uuid4(), container_id="sbx-nowhere")


@pytest.mark.asyncio
async def test_set_container_id_rejects_an_already_destroyed_row(
    store: SqlSandboxInstanceStore, postgres_container: PostgresContainer
) -> None:
    """已销毁的行同样 raise —— 这正是 C-1 接管之后调用方 A 的处境。

    SQL 与内存在这里的**物理**状态本就不同(SQL 的 mark_destroyed 是 UPDATE,
    行还在表里;内存实现直接把行丢了),所以光靠"按 id 找得到吗"对不齐,
    WHERE 必须显式带上 state/destroyed_at 两个谓词。顺带断言那行没被写脏:
    没有这两个谓词的话 UPDATE 会把 container_id 写到一个终态行上,把它对
    ``get_container_id`` 复活。
    """
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    assert (
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id) is None
    )
    await store.mark_destroyed(sandbox_id=sandbox_id, reason=_REASON_STUCK_CREATE_TAKEOVER)

    with pytest.raises(RuntimeError, match="is gone"):
        await store.set_container_id(sandbox_id=sandbox_id, container_id="sbx-too-late")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        async with engine.begin() as conn:
            container_id = (
                await conn.execute(
                    select(SandboxInstanceRow.container_id).where(
                        SandboxInstanceRow.id == sandbox_id
                    )
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert container_id is None, "终态行不能被回填复活"


@pytest.mark.asyncio
async def test_mark_destroyed_does_not_restamp_a_terminal_row(
    store: SqlSandboxInstanceStore, postgres_container: PostgresContainer
) -> None:
    """第一个写终态的赢,后来者不许覆写 destroy_reason / destroyed_at。

    Important-1 的修法把每个清理点都改成按 sandbox_id 让位,重复调用因此正好
    落在接管路径上:调用方 B 接管 A 的过期槽位、把 A 那行标成
    ``stuck_create_takeover``,随后 A 自己失败,它的清理再把**同一行**盖成
    ``post_create_failed`` —— 抹掉的正是 _REASON_STUCK_CREATE_TAKEOVER 的
    docstring 存在的理由(运维得能看出是哪条路径拆的)。

    内存实现一直是对的(``_rows.pop`` 让第二次调用天然空转),这里是 SQL 补齐,
    方向同 set_container_id / touch_and_get_container_id。没有任何东西按
    destroy_reason 做行为判断,所以这是可诊断性修复不是正确性修复。
    """
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    assert (
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id) is None
    )
    await store.mark_destroyed(sandbox_id=sandbox_id, reason=_REASON_STUCK_CREATE_TAKEOVER)

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        async with engine.begin() as conn:
            first = (
                await conn.execute(
                    select(
                        SandboxInstanceRow.destroy_reason, SandboxInstanceRow.destroyed_at
                    ).where(SandboxInstanceRow.id == sandbox_id)
                )
            ).one()

        await store.mark_destroyed(sandbox_id=sandbox_id, reason="post_create_failed")

        async with engine.begin() as conn:
            second = (
                await conn.execute(
                    select(
                        SandboxInstanceRow.destroy_reason, SandboxInstanceRow.destroyed_at
                    ).where(SandboxInstanceRow.id == sandbox_id)
                )
            ).one()
    finally:
        await engine.dispose()

    assert first.destroy_reason == _REASON_STUCK_CREATE_TAKEOVER
    assert second == first, "终态行不能被第二个清理点重新盖章"


@pytest.mark.asyncio
async def test_touch_and_get_container_id_ignores_a_destroyed_row(
    store: SqlSandboxInstanceStore, postgres_container: PostgresContainer
) -> None:
    """周期清扫把行标 DESTROYED 之后,该 run 的下一次 exec 不能还能跑。

    这条序列在周期清扫(``control_plane.sandbox_reap_worker``)落地之前不可
    达 —— 在那之前只有管理员手工 reap 才可能把一行标成终态。现在一次长工具
    调用的会话空闲超过 ``_IDLE_TTL_S`` 就会被真的扫掉,而 SQL 侧原本那条无
    谓词的 ``UPDATE ... RETURNING`` 会照样把 container_id 交回去:代码继续
    在一个系统认为已经拆掉的沙箱里跑,还顺手把 ``last_used_at``(一个专给
    空闲判定用的字段)盖到终态行上。内存实现从来不会 —— ``_rows`` 里根本
    没有已销毁的行。

    返回 ``None`` 会被 ``_attach`` 翻成 SandboxSupervisorError,是 tools
    节点本来就会处理的可读失败。
    """
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    assert (
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id) is None
    )
    await store.set_container_id(sandbox_id=sandbox_id, container_id="sbx-live")
    assert await store.touch_and_get_container_id(sandbox_id=sandbox_id) == "sbx-live"

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    try:
        async with engine.begin() as conn:
            before = (
                await conn.execute(
                    select(SandboxInstanceRow.last_used_at).where(
                        SandboxInstanceRow.id == sandbox_id
                    )
                )
            ).scalar_one()

        await store.mark_destroyed(sandbox_id=sandbox_id, reason="idle")

        assert await store.touch_and_get_container_id(sandbox_id=sandbox_id) is None

        async with engine.begin() as conn:
            after = (
                await conn.execute(
                    select(SandboxInstanceRow.last_used_at).where(
                        SandboxInstanceRow.id == sandbox_id
                    )
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert after == before, "终态行的 last_used_at 不能再被推进"


@pytest.mark.asyncio
async def test_set_container_id_still_backfills_a_live_row(
    store: SqlSandboxInstanceStore,
) -> None:
    """收紧后的 WHERE 不能误伤正常路径 —— 这是唯一真正跑在热路径上的那条。"""
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    assert (
        await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=sandbox_id) is None
    )

    await store.set_container_id(sandbox_id=sandbox_id, container_id="sbx-live")

    assert await store.get_container_id(sandbox_id=sandbox_id) == "sbx-live"


# ---------------------------------------------------------------------------
# 迁移 0142 —— 0141 部分唯一索引按后端限定(image_ref = 'agent-sandbox')。
# ---------------------------------------------------------------------------


async def _insert_docker_row(
    store: SqlSandboxInstanceStore,
    *,
    tenant_id: UUID,
    user_id: UUID | None,
    state: str = "IN_USE",
    container_id: str | None = "docker-cafe",
    acquired_at: datetime | None = None,
) -> UUID:
    """直插一行 docker-supervisor 形状的行(真实 image_ref/node,非标记值)。"""
    row_id = uuid4()
    async with store._sf() as session:
        session.add(
            SandboxInstanceRow(
                id=row_id,
                tenant_id=tenant_id,
                user_id=user_id,
                workspace_id=None,
                image_ref="registry.example.com/expert-work/sandbox:py312",
                node="dev-host-1",
                container_id=container_id,
                state=state,
                thread_id="thread-1",
                cpu_quota=1,
                memory_mb=1024,
                pids_limit=128,
                timeout_s=300,
                acquired_at=acquired_at or datetime.now(tz=UTC),
            )
        )
        await session.commit()
    return row_id


@pytest.mark.asyncio
async def test_docker_warm_row_does_not_block_agent_claim(
    store: SqlSandboxInstanceStore,
) -> None:
    """0142:docker 后端同 (tenant, user) 的 IN_USE 行不再顶死 agent claim。"""
    tenant_id, user_id = uuid4(), uuid4()
    await _insert_docker_row(store, tenant_id=tenant_id, user_id=user_id)

    result = await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4())

    assert result is None  # agent 侧照常赢得自己的槽位
    async with store._sf() as session:
        count = len(
            (
                await session.execute(
                    select(SandboxInstanceRow.id).where(
                        SandboxInstanceRow.tenant_id == tenant_id,
                        SandboxInstanceRow.state == "IN_USE",
                    )
                )
            ).all()
        )
    assert count == 2  # 两后端各一行,合法共存


@pytest.mark.asyncio
async def test_agent_rows_still_unique_per_tenant_user(
    store: SqlSandboxInstanceStore,
) -> None:
    """0142 没放松 agent 行自己的唯一性:直插第二行 agent IN_USE 必炸。"""
    tenant_id, user_id = uuid4(), uuid4()
    await store.claim_warm(tenant_id=tenant_id, user_id=user_id, sandbox_id=uuid4())

    with pytest.raises(IntegrityError):
        async with store._sf() as session:
            session.add(
                SandboxInstanceRow(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    user_id=user_id,
                    workspace_id=None,
                    image_ref=AGENT_SANDBOX_IMAGE_REF,
                    node=AGENT_SANDBOX_IMAGE_REF,
                    container_id=None,
                    state="IN_USE",
                    thread_id=AGENT_SANDBOX_IMAGE_REF,
                    cpu_quota=0,
                    memory_mb=0,
                    pids_limit=0,
                    timeout_s=0,
                    acquired_at=datetime.now(tz=UTC),
                )
            )
            await session.commit()
