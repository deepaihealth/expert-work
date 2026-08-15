"""计数 recorder —— 口径必须与对外消息端点一致(``include_hidden=False``)。

P2 Task 7。三层各有一条:

1. recorder 自己算得对(隐藏消息 / tool 消息不计)且落到了 store;
2. ``run_agent`` 真带着 recorder 跑完一整轮后,``thread_meta.message_count``
   上是持久化后的值 —— 这条是把 orchestrator 侧的派发与 control-plane 侧的
   实现接起来验的那条,也是本任务唯一能杀掉「``finally`` 里那行 dispatch 被
   注释掉」的变异的测试;
3. ``inject_delivery`` 走在 run 之外,也要把计数同步更新。

另外压一条**重算而非累加**:同一会话跑两轮 run,第二轮之后是全量重算的 4,
不是累加出来的数。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from control_plane.thread_stats import ThreadStatsRecorderImpl
from control_plane.trigger_delivery import inject_delivery
from expert_work.persistence import InMemoryThreadMetaStore
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.runs import DisconnectMode, RunManager
from expert_work.runtime.stream_bridge import InMemoryStreamBridge, is_end
from orchestrator import GraphRunner, ToolRegistry, ToolSpec, build_react_graph
from orchestrator.llm.providers._streaming import LLMDelta
from orchestrator.sse import run_agent


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


async def _drain(bridge: InMemoryStreamBridge, run_id: UUID) -> None:
    async for entry in bridge.subscribe(run_id, heartbeat_interval=5.0):
        if is_end(entry):
            break


async def _drain_thread_stats_tasks() -> None:
    import asyncio

    from orchestrator.sse import _BACKGROUND_THREAD_STATS_TASKS

    for task in list(_BACKGROUND_THREAD_STATS_TASKS):
        await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# 1. recorder 本身
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_records_visible_turn_count() -> None:
    tid, tenant = uuid4(), uuid4()
    threads = InMemoryThreadMetaStore()
    await threads.create(thread_id=tid, tenant_id=tenant, created_by="u")
    recorder = ThreadStatsRecorderImpl(threads=threads)

    await recorder.record(
        thread_id=tid,
        tenant_id=tenant,
        messages=[
            HumanMessage(content="你好"),
            ToolMessage(content="工具结果", tool_call_id="1"),  # 不计
            HumanMessage(
                content="脚手架",
                additional_kwargs={"expert_work_hide_from_ui": True},
            ),  # 不计
            AIMessage(content="答案"),
        ],
    )

    got = await threads.get(tid, tenant_id=tenant)
    assert got is not None
    assert got.message_count == 2


@pytest.mark.asyncio
async def test_record_swallows_store_failure() -> None:
    """best-effort:store 炸了也不能把 run 的终局路径带崩。"""

    class _Boom:
        async def update_message_count(self, *a: object, **k: object) -> bool:
            msg = "boom"
            raise RuntimeError(msg)

    recorder = ThreadStatsRecorderImpl(threads=_Boom())
    await recorder.record(thread_id=uuid4(), tenant_id=uuid4(), messages=[])  # 不抛


@pytest.mark.asyncio
async def test_wrong_tenant_does_not_write() -> None:
    """租户不匹配时 store 返 False;recorder 不得把别家的行改掉。"""
    tid, tenant = uuid4(), uuid4()
    threads = InMemoryThreadMetaStore()
    await threads.create(thread_id=tid, tenant_id=tenant, created_by="u")
    recorder = ThreadStatsRecorderImpl(threads=threads)

    await recorder.record(thread_id=tid, tenant_id=uuid4(), messages=[HumanMessage(content="hi")])

    got = await threads.get(tid, tenant_id=tenant)
    assert got is not None
    assert got.message_count == 0  # create 时的初值,没被改


# ---------------------------------------------------------------------------
# 2. run_agent 终局 → 持久化后的 message_count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_finalization_persists_message_count() -> None:
    """整条链:run_agent 的 ``finally`` → Protocol → control-plane 实现 →
    ``thread_meta.message_count``。断言读的是 store 里持久化后的值。

    第二轮 run 证明是**重算**:两轮之后是 4(全量重算),不是累加出来的数。
    """
    thread_id, tenant_id = uuid4(), uuid4()
    threads = InMemoryThreadMetaStore()
    await threads.create(thread_id=thread_id, tenant_id=tenant_id, created_by="u")
    recorder = ThreadStatsRecorderImpl(threads=threads)

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    counts: list[int | None] = []

    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        graph = runner.compile(
            build_react_graph(llm_caller=_EchoLLM(), tool_registry=ToolRegistry())
        )
        config: dict[str, Any] = {
            "configurable": {"thread_id": str(thread_id), "tenant_id": str(tenant_id)}
        }
        for prompt in ("ping", "pong"):
            record = await rm.create(
                run_id=uuid4(),
                thread_id=thread_id,
                tenant_id=tenant_id,
                on_disconnect=DisconnectMode.CANCEL,
            )
            await run_agent(
                bridge=bridge,
                run_manager=rm,
                record=record,
                graph=graph,
                graph_input={
                    "messages": [HumanMessage(content=prompt)],
                    "step_count": 0,
                    "max_steps": 5,
                },
                config=config,
                thread_stats_recorder=recorder,
            )
            await _drain(bridge, record.run_id)
            await _drain_thread_stats_tasks()
            got = await threads.get(thread_id, tenant_id=tenant_id)
            assert got is not None
            counts.append(got.message_count)

    # 一轮 = 用户 1 + 助手 1;两轮 = 4(重算,不是 2 + 4 之类的累加值)。
    assert counts == [2, 4]


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


@dataclass
class _EmptyStateGraph:
    """``aget_state`` 读得到,但 ``messages`` 是空的 —— 真·空会话。"""

    async def astream(
        self, input: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del input, config, stream_mode
        yield {"agent": {"step_count": 1}}

    async def aget_state(self, config: Any) -> Any:
        del config
        return SimpleNamespace(values={"messages": []})


@pytest.mark.asyncio
async def test_state_fetch_failure_keeps_previous_count() -> None:
    """checkpointer 读不到时,已算好的计数必须**原封不动**。

    先真跑一轮把计数写成 2,再让同一会话跑一轮读不到 state 的 run:计数
    仍是 2,不能被刷成 0。用错误数据盖掉正确数据 = 第三方在会话列表上看到
    一个 50 条消息的会话显示「0 条」,直到它下次真跑 run 才自愈。
    """
    thread_id, tenant_id = uuid4(), uuid4()
    threads = InMemoryThreadMetaStore()
    await threads.create(thread_id=thread_id, tenant_id=tenant_id, created_by="u")
    recorder = ThreadStatsRecorderImpl(threads=threads)
    bridge = InMemoryStreamBridge()
    rm = RunManager()

    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        graph = runner.compile(
            build_react_graph(llm_caller=_EchoLLM(), tool_registry=ToolRegistry())
        )
        config: dict[str, Any] = {
            "configurable": {"thread_id": str(thread_id), "tenant_id": str(tenant_id)}
        }
        record = await rm.create(
            run_id=uuid4(),
            thread_id=thread_id,
            tenant_id=tenant_id,
            on_disconnect=DisconnectMode.CANCEL,
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
            config=config,
            thread_stats_recorder=recorder,
        )
        await _drain(bridge, record.run_id)
        await _drain_thread_stats_tasks()

    before = await threads.get(thread_id, tenant_id=tenant_id)
    assert before is not None
    assert before.message_count == 2, "前置没建立好,后面的断言就没意义了"

    broken_record = await rm.create(
        run_id=uuid4(),
        thread_id=thread_id,
        tenant_id=tenant_id,
        on_disconnect=DisconnectMode.CANCEL,
    )
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=broken_record,
        graph=_BrokenStateGraph(),  # type: ignore[arg-type]
        graph_input={},
        config={"configurable": {"thread_id": str(thread_id), "tenant_id": str(tenant_id)}},
        thread_stats_recorder=recorder,
    )
    await _drain(bridge, broken_record.run_id)
    await _drain_thread_stats_tasks()

    after = await threads.get(thread_id, tenant_id=tenant_id)
    assert after is not None
    assert after.message_count == 2  # 保持原值,没被刷成 0


@pytest.mark.asyncio
async def test_genuinely_empty_thread_is_written_as_zero() -> None:
    """读成功但真没有可见轮次 → 就该写 0(不能连这条也一起跳掉)。

    先把计数种成 7,这样「写 0」是一次真实的变更 —— 否则 ``create`` 的初值
    本来就是 0,断言 ``== 0`` 会是个重言式。
    """
    thread_id, tenant_id = uuid4(), uuid4()
    threads = InMemoryThreadMetaStore()
    await threads.create(thread_id=thread_id, tenant_id=tenant_id, created_by="u")
    assert await threads.update_message_count(thread_id, 7, tenant_id=tenant_id)
    recorder = ThreadStatsRecorderImpl(threads=threads)
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await rm.create(
        run_id=uuid4(),
        thread_id=thread_id,
        tenant_id=tenant_id,
        on_disconnect=DisconnectMode.CANCEL,
    )

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_EmptyStateGraph(),  # type: ignore[arg-type]
        graph_input={},
        config={"configurable": {"thread_id": str(thread_id), "tenant_id": str(tenant_id)}},
        thread_stats_recorder=recorder,
    )
    await _drain(bridge, record.run_id)
    await _drain_thread_stats_tasks()

    got = await threads.get(thread_id, tenant_id=tenant_id)
    assert got is not None
    assert got.message_count == 0


# ---------------------------------------------------------------------------
# 3. run 之外的写点 —— 定时任务投递
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_delivery_updates_count() -> None:
    """``inject_delivery`` 不经 ``run_agent`` 的 ``finally``,自己更新一次。"""
    thread_id, tenant_id = uuid4(), uuid4()
    threads = InMemoryThreadMetaStore()
    await threads.create(thread_id=thread_id, tenant_id=tenant_id, created_by="u")
    recorder = ThreadStatsRecorderImpl(threads=threads)

    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        graph = runner.compile(
            build_react_graph(llm_caller=_EchoLLM(), tool_registry=ToolRegistry())
        )
        await inject_delivery(
            graph,
            thread_id=thread_id,
            tenant_id=tenant_id,
            result_text="定时任务结果",
            source_run_id=uuid4(),
            trigger_id=uuid4(),
            thread_stats_recorder=recorder,
        )

    got = await threads.get(thread_id, tenant_id=tenant_id)
    assert got is not None
    assert got.message_count == 1
