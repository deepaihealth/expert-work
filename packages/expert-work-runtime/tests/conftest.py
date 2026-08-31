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
        pull=True,
        wait=True,
    )
    try:
        with stack:
            yield stack
    except subprocess.CalledProcessError as exc:
        # X-8 余项:pull 挂了要说清是**哪个镜像**。testcontainers 走 check_call,
        # CalledProcessError 身上没有 output,原样抛出去只剩一句 exit status 1。
        cmd = exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)]
        if "pull" not in " ".join(cmd):
            raise
        pytest.fail(
            "compose 起栈失败在 pull 这一步 —— 带输出重跑的结果:\n"
            + explain_compose_pull_failure(_INFRA_DIR)
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
