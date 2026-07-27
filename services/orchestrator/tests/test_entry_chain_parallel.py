"""二期 P1.3 —— memory_recall 与 plan 分支(planner→workspace_ingest)并行。

并发证明用事件握手:recall 节点 await 一个只有 ingest 节点才 set 的事件。
旧线性拓扑(recall 先于 ingest)下 recall 永远等不到 → wait_for 超时;
并行拓扑下两节点同一 superstep,握手成功。先红后绿。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolRegistry, build_react_graph
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
