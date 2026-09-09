"""P-1 —— 定轮退到慢路径时必须留下信号(评审要求)。

``locate_turn`` 拿不到 checkpointer 的 psycopg 池就回退到 ``aget_state_history``。
那条路在真 Postgres 上比两步取法慢一个数量级、还要多拉几十 MB(Task 0 实测
810-950 ms / 52 MB)。没有告警的话,vendor 改个属性名就是**静默**降级 —— 只有
有人专门去看耗时才发现。

两个方向都钉住:配置说 postgres 却拿不到池 → 必须告警;``memory`` 后端走
history 是预期形态(单测 / 本地)→ 必须安静。判据取**配置**,不取 saver 类名 ——
要防的正是「类没变、属性名变了」。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from control_plane import supersede as supersede_mod
from control_plane.supersede import _checkpoint_pool, locate_turn


class _NoPoolSaver:
    """生产形态被 vendor 改名后的样子:类还在,``.conn`` 已经不是池了。"""

    conn = object()


class _Graph:
    """只提供 ``locate_turn`` 用得到的两件事:checkpointer 与历史。"""

    def __init__(self, checkpointer: Any) -> None:
        self.checkpointer = checkpointer

    async def aget_state_history(self, config: RunnableConfig, **_: Any) -> Any:
        # 空历史 → locate_turn 走 bounds is None 那一支,不需要真快照。
        return
        yield  # pragma: no cover - 让它成为 async generator


@pytest.fixture(autouse=True)
def _reset_once_flag() -> Any:
    supersede_mod._slow_path_warned = False
    yield
    supersede_mod._slow_path_warned = False


def _no_config() -> str | None:
    """具名而非 lambda —— 仓库规矩:lambda 在 mypy 下会挂 ``no-untyped-call``。"""
    return None


def _cfg() -> RunnableConfig:
    return {"configurable": {"thread_id": str(uuid4()), "tenant_id": str(uuid4())}}


async def _locate(graph: Any) -> Any:
    return await locate_turn(graph, _cfg(), run_ids=[uuid4()], current_len=0, current_plan=None)


@pytest.mark.asyncio
async def test_warns_when_config_says_postgres_but_pool_is_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("EXPERT_WORK_CHECKPOINTER_BACKEND", "postgres")
    monkeypatch.setenv("EXPERT_WORK_CHECKPOINTER_DSN", "postgresql://x/y")
    graph = _Graph(_NoPoolSaver())
    # 先证兄弟事实:这套形态确实取不到池,否则下面断言的是空气。
    assert _checkpoint_pool(graph) is None

    with caplog.at_level(logging.WARNING, logger=supersede_mod.__name__):
        await _locate(graph)

    hits = [r for r in caplog.records if "checkpoint_pool_unavailable" in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in caplog.records]
    # 带上类名才定位得到是哪一层变了。
    assert "_NoPoolSaver" in hits[0].getMessage()


@pytest.mark.asyncio
async def test_warns_only_once_across_repeated_supersedes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """一条就够定位;每次取代都打会把日志刷成噪声。"""
    monkeypatch.setenv("EXPERT_WORK_CHECKPOINTER_BACKEND", "postgres")
    monkeypatch.setenv("EXPERT_WORK_CHECKPOINTER_DSN", "postgresql://x/y")
    graph = _Graph(_NoPoolSaver())

    with caplog.at_level(logging.WARNING, logger=supersede_mod.__name__):
        for _ in range(3):
            await _locate(graph)

    hits = [r for r in caplog.records if "checkpoint_pool_unavailable" in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_memory_backend_is_the_expected_shape_and_stays_quiet(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """内存 saver 走 history 是设计,不是降级 —— 一条告警都不该有。"""
    monkeypatch.setenv("EXPERT_WORK_CHECKPOINTER_BACKEND", "memory")
    monkeypatch.delenv("EXPERT_WORK_CHECKPOINTER_DSN", raising=False)
    graph = _Graph(InMemorySaver())
    assert _checkpoint_pool(graph) is None  # 同样取不到池 —— 区别只在配置

    with caplog.at_level(logging.WARNING, logger=supersede_mod.__name__):
        await _locate(graph)

    assert [r for r in caplog.records if "checkpoint_pool_unavailable" in r.getMessage()] == []


@pytest.mark.asyncio
async def test_unreadable_config_stays_quiet(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """读不出配置就别猜 —— 一条诊断日志不值得冒误报的险,更不该把 supersede 弄挂。"""
    monkeypatch.setattr(supersede_mod, "_configured_checkpointer_backend", _no_config)
    graph = _Graph(_NoPoolSaver())

    with caplog.at_level(logging.WARNING, logger=supersede_mod.__name__):
        await _locate(graph)

    assert [r for r in caplog.records if "checkpoint_pool_unavailable" in r.getMessage()] == []
