"""Shared fixtures for the ``expert-work-runtime`` test suite.

The docker-compose stack used by both ``test_minio_integration.py`` and
``test_minio_object_lock_integration.py`` is session-scoped so MinIO is
booted exactly once per pytest run. Previously each file owned its own
module-scoped fixture which meant the stack was torn down and re-spun
between files — slower and prone to a race where the second ``up``
collided with a not-yet-fully-stopped container.

本文件另有一组 **store 行为契约**(``assert_*`` 系列 fixture):同一条语义的
断言只写一份,内存实现与 SQL 实现各自把自己的 store 喂进来。本仓库有过
「SQL 与内存 store 谓词分歧」的命门级教训 —— 两边各写各的断言时,内存版在
单测里全绿、SQL 版在真库上给出另一种结果,没有任何测试会红。契约住在
conftest 而不是单独模块,是因为 pytest 的 ``--import-mode=importlib`` 不把
测试目录放进 ``sys.path``,同目录的两个测试文件 import 不到彼此的兄弟模块。
"""

from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from testcontainers.compose import DockerCompose

from expert_work.runtime.runs import (
    DisconnectMode,
    RunEventStore,
    RunInfo,
    RunStatus,
    RunStore,
    make_event_record,
)
from expert_work.testing import explain_compose_pull_failure

_INFRA_DIR = Path(__file__).resolve().parents[3] / "infra"


@pytest.fixture(scope="session")
def compose_stack() -> Iterator[DockerCompose]:
    """Boot the infra/docker-compose stack for the full pytest session."""
    stack = DockerCompose(
        context=str(_INFRA_DIR),
        compose_file_name="docker-compose.yml",
        # 只要这几个服务 —— 不写就是整份 compose。``pull=True`` 会把默认
        # profile 的每个镜像都拉一遍(实测四个),包括这条测试压根用不到的
        # ``mock-upstream``;2026-08-31 那天 integration 连红三次,日志逐字是
        # ``mock-upstream Pulling`` → ``toomanyrequests: Rate exceeded``。
        # 每多拉一个用不到的镜像,就多一次踩限流的机会。
        # 本 fixture 是 session 级共享:minio 两个模块 + postgres 备份模块。
        services=["postgres", "minio"],
        # ``pull=False``:镜像由 CI 的预拉步骤(tools/ci/prepull_images.py,带退避)
        # 先放进本机;``pull=True`` 会再去 registry 核对 manifest,限流一到照样
        # 失败,本地缓存等于白拉。镜像真不在时 ``up`` 自己会拉。
        pull=False,
        wait=True,
    )
    try:
        with stack:
            yield stack
    except subprocess.CalledProcessError as exc:
        # X-8 余项:起栈挂了要说清是**哪个镜像**。testcontainers 走 check_call,
        # CalledProcessError 身上没有 output,原样抛出去只剩一句 exit status 1。
        # 重跑 pull 只拉本 fixture 那几个服务 —— 拉整份 compose 会自己再撞一次限流。
        cmd = exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)]
        pytest.fail(
            f"compose 起栈失败({' '.join(cmd)[:80]})—— 带输出重跑 pull 的结果:\n"
            + explain_compose_pull_failure(_INFRA_DIR, services=["postgres", "minio"])
        )


# ---------------------------------------------------------------------------
# store 行为契约 —— 见本文件 docstring
# ---------------------------------------------------------------------------

_BASE = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)

#: 条目接口只读这三种辅助帧。
_AUX = ("plan", "approval", "error")


def _info(*, run_id: UUID, tenant_id: UUID, thread_id: UUID, created_at: datetime) -> RunInfo:
    return RunInfo(
        run_id=run_id,
        tenant_id=tenant_id,
        thread_id=thread_id,
        user_id=None,
        status=RunStatus.SUCCESS,
        on_disconnect=DisconnectMode.CANCEL,
        is_resume=False,
        error=None,
        created_at=created_at,
        updated_at=created_at,
        finished_at=None,
    )


async def _assert_event_names_filter(store: RunEventStore, *, run_id: UUID) -> None:
    """``RunEventStore.list(event_names=…)`` 的全部语义。

    调用方负责让 ``run_id`` 在自己的后端上合法(SQL 侧要先建 ``agent_run``
    行,``run_event`` 是它的子表)。
    """
    await store.append_batch(
        [
            make_event_record(run_id=run_id, seq=0, event_name="metadata", data={"i": 0}),
            make_event_record(run_id=run_id, seq=1, event_name="plan", data={"i": 1}),
            make_event_record(run_id=run_id, seq=2, event_name="updates", data={"i": 2}),
            make_event_record(run_id=run_id, seq=3, event_name="approval", data={"i": 3}),
            make_event_record(run_id=run_id, seq=4, event_name="updates", data={"i": 4}),
            make_event_record(run_id=run_id, seq=5, event_name="error", data={"i": 5}),
        ]
    )

    # 先钉住「不过滤时六条都在」。少了这一条,下面每一句都可能是在空结果上
    # 恒真 —— store 整体失灵时它们同样全绿。
    assert [r.event_name for r in await store.list(run_id=run_id)] == [
        "metadata",
        "plan",
        "updates",
        "approval",
        "updates",
        "error",
    ]

    picked = await store.list(run_id=run_id, event_names=_AUX)
    assert [(r.seq, r.event_name) for r in picked] == [
        (1, "plan"),
        (3, "approval"),
        (5, "error"),
    ]

    assert [r.seq for r in await store.list(run_id=run_id, event_names=["updates"])] == [2, 4]

    # 一个谁都不匹配的名字给空,不是「忽略过滤给全部」。
    assert list(await store.list(run_id=run_id, event_names=["no-such-event"])) == []

    # 空集合同样给空 —— 与 ``RunStore.list_for_tenant`` 的 ``thread_ids`` 同
    # 规则。静默把空谓词当「不过滤」会把调用方的查询悄悄放大成整条流。
    assert list(await store.list(run_id=run_id, event_names=[])) == []

    # 过滤在 ``limit`` **之前**生效:要两条辅助帧就给两条辅助帧。先截断再
    # 过滤的实现会给 ``[1]``(整流头两条里只有 plan 匹配)。
    assert [r.seq for r in await store.list(run_id=run_id, event_names=_AUX, limit=2)] == [1, 3]

    # ``since_seq`` 与名字过滤同时生效,不是二选一。
    assert [r.seq for r in await store.list(run_id=run_id, since_seq=1, event_names=_AUX)] == [3, 5]


async def _assert_keyset_before(store: RunStore) -> None:
    """``RunStore.list_for_tenant(before=…)`` 的 keyset 分页语义。

    造一对 ``created_at`` 完全相同的 run,并让它们跨在页边界上 —— 只按
    ``created_at`` 比较的游标会把其中一条永久跳过,而这正是分页最不该出的错。
    """
    tenant_id, thread_id = uuid4(), uuid4()
    # ``run_id`` 显式构造成有序值:并列那一对要靠它定序,随机 UUID 会让断言
    # 时红时绿。
    oldest, tie_low, tie_high, newest = (
        UUID(int=1),
        UUID(int=2),
        UUID(int=3),
        UUID(int=4),
    )
    tie_at = _BASE + timedelta(minutes=1)
    for run_id, created_at in (
        (oldest, _BASE),
        (tie_low, tie_at),
        (tie_high, tie_at),
        (newest, _BASE + timedelta(minutes=2)),
    ):
        await store.create(
            _info(run_id=run_id, tenant_id=tenant_id, thread_id=thread_id, created_at=created_at)
        )

    # 排序:新→旧,并列时按 run_id 降序。游标键与排序键必须是同一个。
    everything = await store.list_for_tenant(tenant_id=tenant_id, thread_ids=[thread_id])
    assert [r.run_id for r in everything] == [newest, tie_high, tie_low, oldest]

    page1 = await store.list_for_tenant(tenant_id=tenant_id, thread_ids=[thread_id], limit=2)
    assert [r.run_id for r in page1] == [newest, tie_high]

    cursor = page1[-1]
    page2 = await store.list_for_tenant(
        tenant_id=tenant_id,
        thread_ids=[thread_id],
        limit=2,
        before=(cursor.created_at, cursor.run_id),
    )
    # 并列的另一条必须出现在下一页:按 ``created_at < tie_at`` 翻页会给
    # ``[oldest]``,``tie_low`` 从此再也读不到。
    assert [r.run_id for r in page2] == [tie_low, oldest]

    # 两页合起来不重不漏。
    assert [r.run_id for r in page1] + [r.run_id for r in page2] == [r.run_id for r in everything]

    # 游标那一行自己是排除的(严格更早)。
    after_oldest = await store.list_for_tenant(
        tenant_id=tenant_id,
        thread_ids=[thread_id],
        limit=10,
        before=(_BASE, oldest),
    )
    assert list(after_oldest) == []


@pytest.fixture
def event_names_filter_contract() -> Callable[..., Awaitable[None]]:
    """``RunEventStore.list(event_names=…)`` 的契约断言,两个实现共用。"""
    return _assert_event_names_filter


@pytest.fixture
def keyset_before_contract() -> Callable[..., Awaitable[None]]:
    """``RunStore.list_for_tenant(before=…)`` 的契约断言,两个实现共用。"""
    return _assert_keyset_before


# --- B-139 —— 同一会话串行执行:存储层场景,内存版与 SQL 版共用 -----------------

#: 远离其它测试共用库里的行:``list_queued`` 是跨租户全表扫, 场景只看自己造的行。
_TQ_T0 = datetime(2031, 1, 1, tzinfo=UTC)
_TQ_LEASE = timedelta(seconds=30)


def _row(
    *,
    tenant_id: UUID,
    thread_id: UUID,
    status: RunStatus,
    at: timedelta,
    run_id: UUID | None = None,
    claimed_by: str | None = None,
    lease_until: datetime | None = None,
) -> RunInfo:
    created = _TQ_T0 + at
    return RunInfo(
        run_id=run_id or uuid4(),
        tenant_id=tenant_id,
        thread_id=thread_id,
        user_id=None,
        status=status,
        on_disconnect=DisconnectMode.CONTINUE,
        is_resume=False,
        error=None,
        created_at=created,
        updated_at=created,
        finished_at=None,
        claimed_by=claimed_by,
        lease_until=lease_until,
        enqueued_input={"input": "hi"} if status is RunStatus.QUEUED else None,
    )


async def _queued_ids(store: RunStore, *, now: datetime, mine: set[UUID]) -> list[UUID]:
    return [r.run_id for r in await store.list_queued(limit=1000, now=now) if r.run_id in mine]


async def _claim(store: RunStore, run_id: UUID, *, owner: str, now: datetime) -> RunInfo | None:
    return await store.claim_queued(
        run_id=run_id, new_owner=owner, lease_until=now + _TQ_LEASE, heartbeat_at=now
    )


async def inflight_lists_only_busy_runs_of_the_thread(store: RunStore) -> None:
    tenant, thread, other = uuid4(), uuid4(), uuid4()
    done = _row(tenant_id=tenant, thread_id=thread, status=RunStatus.SUCCESS, at=timedelta(0))
    paused = _row(tenant_id=tenant, thread_id=thread, status=RunStatus.PAUSED, at=timedelta(1))
    running = _row(tenant_id=tenant, thread_id=thread, status=RunStatus.RUNNING, at=timedelta(2))
    queued = _row(tenant_id=tenant, thread_id=thread, status=RunStatus.QUEUED, at=timedelta(3))
    pending = _row(tenant_id=tenant, thread_id=thread, status=RunStatus.PENDING, at=timedelta(4))
    elsewhere = _row(tenant_id=tenant, thread_id=other, status=RunStatus.RUNNING, at=timedelta(0))
    for r in (pending, queued, done, paused, running, elsewhere):
        await store.create(r)

    busy = await store.list_inflight_by_thread(thread_id=thread, tenant_id=tenant)

    # 等审批(PAUSED)不算忙:新一轮会作废那条审批(已拍板的规则)。
    assert [r.run_id for r in busy] == [running.run_id, queued.run_id, pending.run_id]
    assert await store.list_inflight_by_thread(thread_id=thread, tenant_id=uuid4()) == []


async def follower_waits_for_every_earlier_busy_run(store: RunStore) -> None:
    tenant, thread = uuid4(), uuid4()
    leader = _row(tenant_id=tenant, thread_id=thread, status=RunStatus.RUNNING, at=timedelta(0))
    first = _row(tenant_id=tenant, thread_id=thread, status=RunStatus.QUEUED, at=timedelta(1))
    second = _row(tenant_id=tenant, thread_id=thread, status=RunStatus.QUEUED, at=timedelta(2))
    for r in (leader, first, second):
        await store.create(r)
    mine = {first.run_id, second.run_id}
    now = _TQ_T0 + timedelta(minutes=1)

    assert await _queued_ids(store, now=now, mine=mine) == []
    assert await _claim(store, first.run_id, owner="w", now=now) is None

    await store.set_status(
        run_id=leader.run_id, tenant_id=tenant, status=RunStatus.SUCCESS, updated_at=now
    )
    # 只有排在最前的那一轮轮到了;第二轮还要等第一轮。
    assert await _queued_ids(store, now=now, mine=mine) == [first.run_id]
    assert await _claim(store, second.run_id, owner="w", now=now) is None
    claimed = await _claim(store, first.run_id, owner="w", now=now)
    assert claimed is not None and claimed.status is RunStatus.RUNNING

    # 第一轮被领走在跑 —— 仍然挡着第二轮。
    assert await _queued_ids(store, now=now, mine=mine) == []
    await store.set_status(
        run_id=first.run_id, tenant_id=tenant, status=RunStatus.ERROR, updated_at=now
    )
    assert await _queued_ids(store, now=now, mine=mine) == [second.run_id]


async def other_threads_do_not_block(store: RunStore) -> None:
    tenant = uuid4()
    busy = _row(tenant_id=tenant, thread_id=uuid4(), status=RunStatus.RUNNING, at=timedelta(0))
    free = _row(tenant_id=tenant, thread_id=uuid4(), status=RunStatus.QUEUED, at=timedelta(1))
    for r in (busy, free):
        await store.create(r)
    now = _TQ_T0 + timedelta(minutes=1)
    assert await _queued_ids(store, now=now, mine={free.run_id}) == [free.run_id]
    assert await _claim(store, free.run_id, owner="w", now=now) is not None


async def same_instant_runs_order_by_id(store: RunStore) -> None:
    """``created_at`` 相同(应用侧取时间)时按 run id 定先后 —— 不能两条互相等。"""
    tenant, thread = uuid4(), uuid4()
    a, b = sorted((uuid4(), uuid4()))
    for rid in (b, a):
        await store.create(
            _row(
                tenant_id=tenant,
                thread_id=thread,
                status=RunStatus.QUEUED,
                at=timedelta(0),
                run_id=rid,
            )
        )
    now = _TQ_T0 + timedelta(minutes=1)
    assert await _queued_ids(store, now=now, mine={a, b}) == [a]


async def a_live_reservation_keeps_others_out(store: RunStore) -> None:
    tenant, thread = uuid4(), uuid4()
    now = _TQ_T0 + timedelta(minutes=1)
    held = _row(
        tenant_id=tenant,
        thread_id=thread,
        status=RunStatus.QUEUED,
        at=timedelta(0),
        claimed_by="replica-a",
        lease_until=now + _TQ_LEASE,
    )
    await store.create(held)

    assert await _queued_ids(store, now=now, mine={held.run_id}) == []
    assert await _claim(store, held.run_id, owner="replica-b", now=now) is None
    # 预订者自己可以领。
    claimed = await _claim(store, held.run_id, owner="replica-a", now=now)
    assert claimed is not None and claimed.claimed_by == "replica-a"


async def an_expired_reservation_is_fair_game(store: RunStore) -> None:
    tenant, thread = uuid4(), uuid4()
    now = _TQ_T0 + timedelta(minutes=1)
    stale = _row(
        tenant_id=tenant,
        thread_id=thread,
        status=RunStatus.QUEUED,
        at=timedelta(0),
        claimed_by="replica-a",
        lease_until=now - timedelta(seconds=1),
    )
    await store.create(stale)
    assert await _queued_ids(store, now=now, mine={stale.run_id}) == [stale.run_id]
    claimed = await _claim(store, stale.run_id, owner="replica-b", now=now)
    assert claimed is not None and claimed.claimed_by == "replica-b"


async def reservation_renew_and_release(store: RunStore) -> None:
    tenant, thread = uuid4(), uuid4()
    now = _TQ_T0 + timedelta(minutes=1)
    row = _row(
        tenant_id=tenant,
        thread_id=thread,
        status=RunStatus.QUEUED,
        at=timedelta(0),
        claimed_by="replica-a",
        lease_until=now + _TQ_LEASE,
    )
    await store.create(row)
    later = now + timedelta(seconds=20)

    assert await store.renew_queued_reservation(
        run_id=row.run_id, owner="replica-a", lease_until=later + _TQ_LEASE, now=later
    )
    # 别人抢不走一份还有效的预订。
    assert not await store.renew_queued_reservation(
        run_id=row.run_id, owner="replica-b", lease_until=later + _TQ_LEASE, now=later
    )
    # 别人的释放不生效。
    await store.release_queued_reservation(run_id=row.run_id, owner="replica-b")
    assert await _queued_ids(store, now=later, mine={row.run_id}) == []

    await store.release_queued_reservation(run_id=row.run_id, owner="replica-a")
    assert await _queued_ids(store, now=later, mine={row.run_id}) == [row.run_id]

    # 行已不再排队(被取消)—— 续不上。
    await store.request_cancel(
        run_id=row.run_id, tenant_id=tenant, updated_at=later, reason="user_cancel"
    )
    assert not await store.renew_queued_reservation(
        run_id=row.run_id, owner="replica-a", lease_until=later + _TQ_LEASE, now=later
    )


_THREAD_QUEUE_SCENARIOS = (
    inflight_lists_only_busy_runs_of_the_thread,
    follower_waits_for_every_earlier_busy_run,
    other_threads_do_not_block,
    same_instant_runs_order_by_id,
    a_live_reservation_keeps_others_out,
    an_expired_reservation_is_fair_game,
    reservation_renew_and_release,
)


@pytest.fixture
def thread_queue_scenarios() -> tuple[Callable[[RunStore], Awaitable[None]], ...]:
    """B-139 —— 「忙 / 轮到没有 / 预订有效没有」的契约场景,两个实现共用:
    两份实现的谓词必须逐字同义, 否则内存版全绿、线上 SQL 放两轮并跑。"""
    return _THREAD_QUEUE_SCENARIOS
