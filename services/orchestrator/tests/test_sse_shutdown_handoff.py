"""B-80 —— 优雅关机把在跑的这一轮交给别的副本,而不是收成 INTERRUPTED。

热接管本来就有(``OrphanSweep`` 从 durable checkpoint 续跑),判据是
``status='running' AND lease_until < now``。缺口在关机这一侧:进程关机取消在跑的
任务时,``run_agent`` 的兜底分支主动把行写成 ``interrupted``,孤儿扫描于是永远
看不见它 —— 设计好的接管路径被自己关掉了。

这一组测试钉住三件事:
1. 可安全重放的一轮,关机时交出所有权(行留 RUNNING、租约作废、**不发 end 帧**);
2. 悬空批次里有不可重放的工具时,照旧收成 INTERRUPTED(「只交接安全的」);
3. 关机窗口里被**用户**取消的一轮不会被这条路径复活。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunStore,
    RunManager,
    RunRecord,
    RunStatus,
)
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator.sse import run_agent


@dataclass
class _SlowGraph:
    """一直产出 chunk 直到被协作取消 —— 模拟"关机时还在跑"的那一轮。

    ``state_values`` 是 ``aget_state`` 的返回,``replay_is_safe`` 读的就是它:
    尾部 ``AIMessage`` 的 tool_calls 有没有对应的 ``ToolMessage``。
    """

    state_values: dict[str, Any] = field(default_factory=dict)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    chunk_delay_s: float = 0.05

    async def astream(
        self, input: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del input, config, stream_mode
        self.started.set()
        n = 0
        while True:
            await asyncio.sleep(self.chunk_delay_s)
            n += 1
            yield {"agent": {"step_count": n}}

    async def aget_state(self, config: Any) -> Any:
        del config
        return SimpleNamespace(values=dict(self.state_values))


def _clean_tail() -> dict[str, Any]:
    """尾部没有悬空工具批次 → 续跑只重放一次纯 LLM 调用 → 可安全交接。"""
    return {"messages": [HumanMessage(content="hi"), AIMessage(content="done")]}


def _dangling_tail(tool_name: str) -> dict[str, Any]:
    """尾部有一个没被应答的工具调用 → 续跑会重新派发它。"""
    return {
        "messages": [
            HumanMessage(content="hi"),
            AIMessage(
                content="",
                tool_calls=[{"id": "call-1", "name": tool_name, "args": {}}],
            ),
        ]
    }


def _answered_tail(tool_name: str) -> dict[str, Any]:
    return {
        "messages": [
            HumanMessage(content="hi"),
            AIMessage(
                content="",
                tool_calls=[{"id": "call-1", "name": tool_name, "args": {}}],
            ),
            ToolMessage(content="ok", tool_call_id="call-1"),
        ]
    }


async def _new_record(rm: RunManager) -> RunRecord:
    return await rm.create(
        run_id=uuid4(),
        thread_id=uuid4(),
        tenant_id=uuid4(),
        on_disconnect=DisconnectMode.CANCEL,
    )


async def _end_was_published(bridge: InMemoryStreamBridge, run_id: Any) -> bool:
    """这一轮有没有发过终局 end 帧。

    不能用 ``subscribe`` 去等 —— 交接出去的一轮**不会**发 end 帧,等就是挂死。
    直接看桥上这条流的终局标志(``publish_end`` 置的那个)。
    """
    stream = bridge._streams.get(run_id)
    return stream is not None and stream.ended


async def _run_until_shutdown(
    *,
    store: InMemoryRunStore,
    graph: _SlowGraph,
    tool_replay_safe: Any = None,
    cancel_user_run: bool = False,
) -> tuple[RunManager, RunRecord, InMemoryStreamBridge]:
    """跑起来 → 等它真的在跑 → 置关机标志 → 等 run_agent 自己收口。"""
    bridge = InMemoryStreamBridge()
    rm = RunManager(store, instance_id="inst-a", lease_ttl_s=30.0)
    record = await _new_record(rm)
    task = asyncio.create_task(
        run_agent(
            bridge=bridge,
            run_manager=rm,
            record=record,
            graph=graph,
            graph_input={"messages": []},
            config={},
            tool_replay_safe=tool_replay_safe,
        )
    )
    await asyncio.wait_for(graph.started.wait(), timeout=5.0)
    if cancel_user_run:
        await rm.cancel(record.run_id, reason="user")
    rm.mark_shutting_down()
    try:
        # 可交接的那一轮由哨兵在 1 秒内置 abort_event 自行收口;不可交接的会
        # 一直跑下去,由关机收口上限硬停 —— 这里用取消模拟「到点硬停」。
        await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
    except TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return rm, record, bridge


@pytest.mark.asyncio
async def test_safe_run_is_handed_off_instead_of_interrupted() -> None:
    store = InMemoryRunStore()
    graph = _SlowGraph(state_values=_clean_tail())
    _rm, record, bridge = await _run_until_shutdown(store=store, graph=graph)

    row = await store.get(run_id=record.run_id, tenant_id=record.tenant_id)
    assert row is not None
    assert row.status is RunStatus.RUNNING, "交接不是终局写 —— 孤儿扫描只捡 running"
    assert row.error is None
    assert row.finished_at is None
    # 租约已作废 → 下一个 tick 就是孤儿候选,活着的副本从 checkpoint 接管。
    assert row.lease_until is not None
    from datetime import timedelta

    orphans = await store.list_orphans(now=row.lease_until + timedelta(seconds=1), limit=10)
    assert [r.run_id for r in orphans] == [record.run_id]
    # 这一轮没有结束 —— 发了 end 帧,重连的客户端会看到一次假终局。
    assert not await _end_was_published(bridge, record.run_id)


@pytest.mark.asyncio
async def test_handoff_counter_counts_only_real_handoffs() -> None:
    """``expert_work_run_handed_off_total`` 是滚动发布期间唯一的正向信号。

    刻意做成独立计数器而不是 ``session_outcome`` 的新标签值:那个词表必须闭合在
    对外四值里(`test_end_status_vocabulary_round_trip` 是那道闸),而「交接」不是
    终局 —— 这一轮没结束,它在别的副本上继续。
    """
    from orchestrator.sse import _run_handed_off_total

    def _value() -> float:
        return _run_handed_off_total._value.get()  # type: ignore[attr-defined]

    before = _value()
    await _run_until_shutdown(
        store=InMemoryRunStore(), graph=_SlowGraph(state_values=_clean_tail())
    )
    after_safe = _value()
    assert after_safe == before + 1

    # 不可交接的那一轮不该记数。
    await _run_until_shutdown(
        store=InMemoryRunStore(),
        graph=_SlowGraph(state_values=_dangling_tail("send_email")),
        tool_replay_safe=lambda name: name != "send_email",
    )
    assert _value() == after_safe


@pytest.mark.asyncio
async def test_run_with_unsafe_dangling_tool_is_not_handed_off() -> None:
    """「只交接安全的」:悬空批次里有不可重放的工具 → 照旧收成 INTERRUPTED。"""
    store = InMemoryRunStore()
    graph = _SlowGraph(state_values=_dangling_tail("send_email"))
    _rm, record, _bridge = await _run_until_shutdown(
        store=store, graph=graph, tool_replay_safe=lambda name: name != "send_email"
    )

    row = await store.get(run_id=record.run_id, tenant_id=record.tenant_id)
    assert row is not None
    assert row.status is RunStatus.INTERRUPTED
    from datetime import timedelta

    assert (
        await store.list_orphans(
            now=(row.lease_until or row.updated_at) + timedelta(seconds=60), limit=10
        )
        == []
    )


@pytest.mark.asyncio
async def test_run_with_safe_dangling_tool_is_handed_off() -> None:
    """悬空批次全部可重放 → 仍然交接(与重试守卫同一判据)。"""
    store = InMemoryRunStore()
    graph = _SlowGraph(state_values=_dangling_tail("read_file"))
    _rm, record, _bridge = await _run_until_shutdown(
        store=store, graph=graph, tool_replay_safe=lambda name: name == "read_file"
    )

    row = await store.get(run_id=record.run_id, tenant_id=record.tenant_id)
    assert row is not None and row.status is RunStatus.RUNNING


@pytest.mark.asyncio
async def test_missing_replay_resolver_falls_back_to_interrupted() -> None:
    """fail-closed:有悬空批次但拿不到可重放判据 → 不交接。"""
    store = InMemoryRunStore()
    graph = _SlowGraph(state_values=_dangling_tail("whatever"))
    _rm, record, _bridge = await _run_until_shutdown(
        store=store, graph=graph, tool_replay_safe=None
    )

    row = await store.get(run_id=record.run_id, tenant_id=record.tenant_id)
    assert row is not None and row.status is RunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_user_cancelled_run_is_not_revived_by_the_handoff_path() -> None:
    """关机窗口里被用户取消的一轮:``hand_off`` 的 CAS 必须落空。"""
    store = InMemoryRunStore()
    graph = _SlowGraph(state_values=_answered_tail("read_file"))
    _rm, record, _bridge = await _run_until_shutdown(store=store, graph=graph, cancel_user_run=True)

    row = await store.get(run_id=record.run_id, tenant_id=record.tenant_id)
    assert row is not None
    assert row.status is RunStatus.INTERRUPTED, "用户取消的一轮不能被复活"
    assert row.error == "user"
    # 真正要钉的不变式:孤儿扫描不会把它捡起来重跑。只断言状态是不够的 ——
    # ``abandon_lease`` 不碰 status,即便它误放行,这一行的 status 断言照样绿。
    from datetime import timedelta

    far_future = (row.lease_until or row.updated_at) + timedelta(days=1)
    assert await store.list_orphans(now=far_future, limit=10) == []


@pytest.mark.asyncio
async def test_no_handoff_when_not_shutting_down() -> None:
    """没在关机时,取消照旧是取消 —— 标志是唯一的开关。"""
    store = InMemoryRunStore()
    bridge = InMemoryStreamBridge()
    rm = RunManager(store, instance_id="inst-a")
    record = await _new_record(rm)
    graph = _SlowGraph(state_values=_clean_tail())
    task = asyncio.create_task(
        run_agent(
            bridge=bridge,
            run_manager=rm,
            record=record,
            graph=graph,
            graph_input={"messages": []},
            config={},
        )
    )
    await asyncio.wait_for(graph.started.wait(), timeout=5.0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    row = await store.get(run_id=record.run_id, tenant_id=record.tenant_id)
    assert row is not None and row.status is RunStatus.INTERRUPTED
