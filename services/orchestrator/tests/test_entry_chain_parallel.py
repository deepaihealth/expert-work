"""二期 P1.3 —— memory_recall 与 plan 分支(planner→workspace_ingest)并行。

并发证明用事件握手:recall 节点 await 一个只有 ingest 节点才 set 的事件。
旧线性拓扑(recall 先于 ingest)下 recall 永远等不到 → wait_for 超时;
并行拓扑下两节点同一 superstep,握手成功。先红后绿。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolContext, ToolRegistry, ToolResult, build_react_graph
from orchestrator.tools.registry import ToolSpec


@dataclass
class _NoToolCallLLM:
    """Replies with no tool calls so the graph ends after one agent step."""

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        return AIMessage(content="done")


@pytest.mark.asyncio
async def test_memory_recall_runs_concurrently_with_ingest_branch() -> None:
    handshake = asyncio.Event()
    order: list[str] = []

    async def fake_recall(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        del state, config
        order.append("recall:start")
        await asyncio.wait_for(handshake.wait(), timeout=2.0)
        order.append("recall:end")
        return {"recalled_memories": []}

    async def fake_ingest(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        del state, config
        order.append("ingest:start")
        handshake.set()
        return {}

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=_NoToolCallLLM(),
                tool_registry=ToolRegistry(),
                memory_recall_node=fake_recall,  # type: ignore[arg-type]
                workspace_ingest_node=fake_ingest,  # type: ignore[arg-type]
                planner_node=None,
            )
        )
        result = await asyncio.wait_for(
            compiled.ainvoke(
                {"messages": [HumanMessage(content="hi")], "step_count": 0, "max_steps": 5},
                config={"configurable": {"thread_id": str(uuid4())}},
            ),
            timeout=10.0,
        )
    assert "recall:end" in order  # 握手成功 = 两节点确在同一 superstep 并发
    assert result is not None


@dataclass
class _CountingLLM:
    """Records each agent invocation into ``order`` and replies with no tool calls."""

    order: list[str]
    calls: int = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        self.calls += 1
        self.order.append("agent")
        return AIMessage(content="done")


@pytest.mark.asyncio
async def test_three_node_entry_chain_joins_before_single_agent_run() -> None:
    """三节点旗舰配置(recall ∥ planner→ingest)下 agent 只跑一次。

    两分支不等长(planner→ingest 是 2 步):单串 ``add_edge(tail, "agent")``
    是 OR 触发 —— recall 先完成就触发 agent 一次,ingest 完成再触发一次,
    agent 双跑(无 tool_calls 时双 LLM 调用;带 tool_calls 时
    ``InvalidUpdateError``)。列表形式 ``add_edge([a, b], "agent")`` 才建
    AND-join 屏障。先红后绿。
    """
    order: list[str] = []

    async def fake_recall(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        del state, config
        order.append("recall")
        return {"recalled_memories": []}

    async def fake_planner(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        del state, config
        order.append("planner")
        return {}

    async def fake_ingest(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        del state, config
        order.append("ingest")
        return {}

    llm = _CountingLLM(order=order)
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=ToolRegistry(),
                memory_recall_node=fake_recall,  # type: ignore[arg-type]
                planner_node=fake_planner,  # type: ignore[arg-type]
                workspace_ingest_node=fake_ingest,  # type: ignore[arg-type]
            )
        )
        result = await asyncio.wait_for(
            compiled.ainvoke(
                {"messages": [HumanMessage(content="hi")], "step_count": 0, "max_steps": 5},
                config={"configurable": {"thread_id": str(uuid4())}},
            ),
            timeout=10.0,
        )

    assert llm.calls == 1, f"agent 应只跑一次,实跑 {llm.calls} 次(order={order})"
    agent_idx = order.index("agent")
    assert order.index("ingest") < agent_idx  # AND-join:agent 等 ingest 分支完成
    assert order.index("planner") < order.index("ingest")  # 分支内保序
    assert result is not None
    ai_messages = [m for m in result["messages"] if isinstance(m, AIMessage)]
    assert len(ai_messages) == 1  # 无重复 AIMessage


@pytest.mark.asyncio
async def test_barrier_resets_across_turns_on_same_thread() -> None:
    """终审 M-3 回归钉①:AND-join 屏障(NamedBarrierValue)跨轮复位。

    同 thread 连跑 3 轮完整 ainvoke,每轮入口链两分支都要重新汇合、agent
    恰跑 3 次。屏障若跨轮不复位,第 2 轮起 agent 要么等不齐父分支挂死
    (wait_for 超时兜底),要么带着上轮残留状态双触发。回归钉,非新行为。
    """
    order: list[str] = []

    async def fake_recall(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        del state, config
        order.append("recall")
        return {"recalled_memories": []}

    async def fake_planner(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        del state, config
        order.append("planner")
        return {}

    async def fake_ingest(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        del state, config
        order.append("ingest")
        return {}

    llm = _CountingLLM(order=order)
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=ToolRegistry(),
                memory_recall_node=fake_recall,  # type: ignore[arg-type]
                planner_node=fake_planner,  # type: ignore[arg-type]
                workspace_ingest_node=fake_ingest,  # type: ignore[arg-type]
            )
        )
        thread_id = str(uuid4())
        for turn in range(3):
            result = await asyncio.wait_for(
                compiled.ainvoke(
                    {
                        "messages": [HumanMessage(content=f"turn-{turn}")],
                        "step_count": 0,
                        "max_steps": 5,
                    },
                    config={"configurable": {"thread_id": thread_id}},
                ),
                timeout=10.0,
            )
            assert result is not None

    assert llm.calls == 3, f"3 轮应各跑 agent 一次,实跑 {llm.calls} 次(order={order})"
    assert order.count("recall") == 3  # 每轮入口链都重跑、重新汇合
    assert order.count("ingest") == 3


@dataclass
class _ToolOnceLLM:
    """首轮发一个 tool_call,次轮不发 —— 驱动 tools→agent 回边恰好一圈。"""

    calls: int = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{"name": "echo", "args": {}, "id": "tc-1", "type": "tool_call"}],
            )
        return AIMessage(content="done")


@dataclass
class _EchoTool:
    dispatched: int = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="echo", description="echo tool")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        self.dispatched += 1
        return ToolResult(content="ok")


@pytest.mark.asyncio
async def test_barrier_coexists_with_tools_agent_loop_edge() -> None:
    """终审 M-3 回归钉②:AND-join 屏障与 tools→agent 回边共存。

    首轮 LLM 发 tool_call,tools 跑完经回边再触发 agent —— 此时入口链
    分支不会重跑,回边必须能单独触发 agent,而不是等一个本轮不会再满足
    的屏障挂死(wait_for 超时兜底);次轮不发 tool_call,run 正常收尾,
    agent 恰跑 2 次。回归钉,非新行为。
    """
    order: list[str] = []

    async def fake_recall(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        del state, config
        order.append("recall")
        return {"recalled_memories": []}

    async def fake_ingest(state: dict[str, Any], config: RunnableConfig) -> dict[str, Any]:
        del state, config
        order.append("ingest")
        return {}

    llm = _ToolOnceLLM()
    tool = _EchoTool()
    registry = ToolRegistry()
    registry.register(tool)
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=registry,
                memory_recall_node=fake_recall,  # type: ignore[arg-type]
                workspace_ingest_node=fake_ingest,  # type: ignore[arg-type]
                planner_node=None,
            )
        )
        result = await asyncio.wait_for(
            compiled.ainvoke(
                {"messages": [HumanMessage(content="hi")], "step_count": 0, "max_steps": 5},
                config={"configurable": {"thread_id": str(uuid4()), "run_id": "r-1"}},
            ),
            timeout=10.0,
        )

    assert llm.calls == 2, f"agent 应恰跑 2 次(tool 轮 + 收尾轮),实跑 {llm.calls} 次"
    assert tool.dispatched == 1
    assert order.count("recall") == 1  # 回边不重跑入口链
    assert order.count("ingest") == 1
    assert result is not None
