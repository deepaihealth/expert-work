"""``run_agent`` 的 ``finally`` 把会话消息条数派给 recorder(P2 Task 7)。

挂 ``finally`` 而不是挂控制面那 6 个 ``run_agent`` 启动点 —— 一处覆盖全部
调用方 **与全部终局分支**。所以这里除了「正常结束」还专门压了异常终局:
分支覆盖正是选 ``finally`` 的理由,不测就等于没验证这个决策。

第一条用真 graph(``build_react_graph`` + ``GraphRunner`` +
``make_checkpointer("memory")``,照 ``test_sse.py``
``test_run_agent_over_real_react_graph`` 的同款用法):recorder 拿到的必须是
真跑出来的终局 ``messages``,手工 fixture 对齐出来的「像是对的」不算数。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.runs import DisconnectMode, RunManager, RunRecord
from expert_work.runtime.stream_bridge import END_SENTINEL, InMemoryStreamBridge
from orchestrator import GraphRunner, ToolRegistry, ToolSpec, build_react_graph
from orchestrator.llm.providers._streaming import LLMDelta
from orchestrator.sse import run_agent

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _CapturingRecorder:
    """记下每次 ``record`` 的入参 —— 断言的是「真的被调到了、带着什么」。"""

    calls: list[tuple[UUID, UUID, list[BaseMessage]]] = field(default_factory=list)

    async def record(
        self, *, thread_id: UUID, tenant_id: UUID, messages: Sequence[BaseMessage]
    ) -> None:
        self.calls.append((thread_id, tenant_id, list(messages)))


@dataclass
class _StateSnapshot:
    values: dict[str, Any]


@dataclass
class _ScriptedGraph:
    """``astream`` 吐 chunk 后可选地抛异常;``aget_state`` 给终局 state。"""

    chunks: list[Any] = field(default_factory=list)
    raise_with: BaseException | None = None
    final_messages: list[Any] = field(default_factory=list)

    async def astream(
        self, input: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del input, config, stream_mode
        for chunk in self.chunks:
            yield chunk
        if self.raise_with is not None:
            raise self.raise_with

    async def aget_state(self, config: Any) -> _StateSnapshot:
        del config
        return _StateSnapshot(values={"messages": list(self.final_messages)})


@dataclass
class _EchoLLM:
    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        on_delta: Callable[[LLMDelta], Awaitable[None]] | None = None,
    ) -> AIMessage:
        del tools, on_delta
        return AIMessage(content=f"echo: {messages[-1].content}")


async def _new_record(rm: RunManager) -> RunRecord:
    return await rm.create(
        run_id=uuid4(),
        thread_id=uuid4(),
        tenant_id=uuid4(),
        on_disconnect=DisconnectMode.CANCEL,
    )


async def _drain(bridge: InMemoryStreamBridge, run_id: UUID) -> None:
    async for entry in bridge.subscribe(run_id, heartbeat_interval=5.0):
        if entry is END_SENTINEL:
            break


async def _drain_thread_stats_tasks() -> None:
    """等 fire-and-forget 的派发任务跑完(它自吞异常,所以 gather 收着)。"""
    from orchestrator.sse import _BACKGROUND_THREAD_STATS_TASKS

    for task in list(_BACKGROUND_THREAD_STATS_TASKS):
        await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# 真 graph —— recorder 拿到的是真跑出来的终局 messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatches_real_final_messages_after_run() -> None:
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    recorder = _CapturingRecorder()
    tenant_in_config = uuid4()

    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        graph = runner.compile(
            build_react_graph(llm_caller=_EchoLLM(), tool_registry=ToolRegistry())
        )
        await run_agent(
            bridge=bridge,
            run_manager=rm,
            record=record,
            graph=graph,
            graph_input={
                "messages": [HumanMessage(content="ping")],
                "step_count": 0,
                "max_steps": 5,
            },
            config={
                "configurable": {
                    "thread_id": uuid4().hex,
                    "tenant_id": str(tenant_in_config),
                }
            },
            thread_stats_recorder=recorder,
        )
        await _drain(bridge, record.run_id)
        # checkpointer 还活着的时候才能读终局 state —— 在 with 内 drain。
        await _drain_thread_stats_tasks()

    assert len(recorder.calls) == 1
    thread_id, tenant_id, messages = recorder.calls[0]
    assert thread_id == record.thread_id
    # config 里的 tenant_id 优先于 record 上的(与 trajectory 派发同规则)。
    assert tenant_id == tenant_in_config
    # 真 graph 跑出来的终局:用户那条 + 助手那条,都在里面。
    assert [m.type for m in messages] == ["human", "ai"]
    assert messages[0].content == "ping"
    assert messages[1].content == "echo: ping"


# ---------------------------------------------------------------------------
# 终局分支覆盖 —— 挂 finally 的全部理由
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatches_on_failed_run_too() -> None:
    """graph 抛异常的终局同样派发 —— 失败的 run 也改变了会话内容。"""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    recorder = _CapturingRecorder()
    graph = _ScriptedGraph(
        chunks=[{"agent": {"step_count": 1}}],
        raise_with=RuntimeError("boom"),
        final_messages=[HumanMessage(content="hi"), AIMessage(content="partial")],
    )

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        thread_stats_recorder=recorder,
    )
    await _drain(bridge, record.run_id)
    await _drain_thread_stats_tasks()

    assert len(recorder.calls) == 1
    # config 没有 tenant_id 时回落到 record 上的租户。
    assert recorder.calls[0][1] == record.tenant_id
    assert len(recorder.calls[0][2]) == 2


@pytest.mark.asyncio
async def test_dispatches_on_cancelled_run() -> None:
    """协作取消(abort_event)的终局也派发。"""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    record.abort_event.set()
    recorder = _CapturingRecorder()
    graph = _ScriptedGraph(
        chunks=[{"agent": {"step_count": 1}}],
        final_messages=[HumanMessage(content="hi")],
    )

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        thread_stats_recorder=recorder,
    )
    await _drain(bridge, record.run_id)
    await _drain_thread_stats_tasks()

    assert len(recorder.calls) == 1


# ---------------------------------------------------------------------------
# 「取不到 state」≠「会话是空的」
# ---------------------------------------------------------------------------


@dataclass
class _BrokenStateGraph:
    """``aget_state`` 抛异常 —— checkpointer 抖动 / 连接断的现场。"""

    async def astream(
        self, input: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del input, config, stream_mode
        yield {"agent": {"step_count": 1}}

    async def aget_state(self, config: Any) -> Any:
        del config
        msg = "checkpointer broken"
        raise RuntimeError(msg)


@pytest.mark.asyncio
async def test_state_fetch_failure_skips_the_write_entirely() -> None:
    """读不到终局 state 时**根本不调 recorder** —— 而不是拿一个空 messages
    去调,把会话计数刷成 0。用错误数据盖掉正确数据比保留旧值糟得多。"""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    recorder = _CapturingRecorder()

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_BrokenStateGraph(),  # type: ignore[arg-type]
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        thread_stats_recorder=recorder,
    )
    await _drain(bridge, record.run_id)
    await _drain_thread_stats_tasks()

    assert recorder.calls == []


@pytest.mark.asyncio
async def test_empty_state_still_writes_zero() -> None:
    """读成功但真的没有消息 —— 该写 0 就写 0,别把这条也一起跳掉。"""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    recorder = _CapturingRecorder()
    graph = _ScriptedGraph(chunks=[{"agent": {"step_count": 1}}], final_messages=[])

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        thread_stats_recorder=recorder,
    )
    await _drain(bridge, record.run_id)
    await _drain_thread_stats_tasks()

    assert len(recorder.calls) == 1
    assert recorder.calls[0][2] == []


# ---------------------------------------------------------------------------
# 未接线 / 慢 recorder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_recorder_is_a_noop() -> None:
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    graph = _ScriptedGraph(chunks=[{"agent": {"step_count": 1}}])

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
    )
    await _drain(bridge, record.run_id)


@pytest.mark.asyncio
async def test_slow_recorder_does_not_block_run_completion() -> None:
    """派发是 fire-and-forget:recorder 卡住 60s 也不能拖住 run 的终局。"""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)

    class _HangingRecorder:
        async def record(self, **kwargs: object) -> None:
            del kwargs
            await asyncio.sleep(60)

    graph = _ScriptedGraph(
        chunks=[{"agent": {"step_count": 1}}],
        final_messages=[HumanMessage(content="hi")],
    )

    # 若派发被 inline await,下面这行会挂到 wait_for 超时。
    await asyncio.wait_for(
        run_agent(
            bridge=bridge,
            run_manager=rm,
            record=record,
            graph=graph,
            graph_input={},
            config={"configurable": {"thread_id": str(record.thread_id)}},
            thread_stats_recorder=_HangingRecorder(),
        ),
        timeout=2.0,
    )
    await _drain(bridge, record.run_id)
    # 别把 60s 的挂起任务留给后面的用例。
    from orchestrator.sse import _BACKGROUND_THREAD_STATS_TASKS

    for task in list(_BACKGROUND_THREAD_STATS_TASKS):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
