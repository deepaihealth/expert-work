"""PR-A.3 §十.3 — agent_node 把「LLM 调用起点 → 第一个非空 delta」写进
AIMessage.additional_kwargs["first_token_ms"];没流式就不写。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolRegistry, build_react_graph
from orchestrator.graph_builder._config import TOKEN_SINK_KEY
from orchestrator.llm.providers._streaming import LLMDelta


@dataclass
class _SlowFirstTokenLLM:
    first_token_delay_s: float
    streams: bool = True
    calls: int = field(default=0)

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[object],
        on_delta: Callable[[LLMDelta], Awaitable[None]] | None = None,
    ) -> AIMessage:
        del messages, tools
        self.calls += 1
        if self.streams and on_delta is not None:
            await asyncio.sleep(self.first_token_delay_s)
            await on_delta(LLMDelta(content="answer"))
        return AIMessage(content="answer")


async def _first_ai_additional_kwargs(llm: Any, *, with_sink: bool) -> dict[str, Any]:
    async def capture(_frame: dict[str, Any]) -> None:
        return None

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=ToolRegistry())
        )
        configurable: dict[str, Any] = {"thread_id": str(uuid4())}
        if with_sink:
            configurable[TOKEN_SINK_KEY] = capture
        cfg: RunnableConfig = {"configurable": configurable}
        async for chunk in compiled.astream(
            {"messages": [HumanMessage(content="hi")], "step_count": 0, "max_steps": 3},
            config=cfg,
            stream_mode="updates",
        ):
            for _node, ch in chunk.items():
                if not isinstance(ch, dict):
                    continue
                for m in ch.get("messages", []) or []:
                    if isinstance(m, AIMessage):
                        return dict(m.additional_kwargs)
    raise AssertionError("no AIMessage in updates")


@pytest.mark.asyncio
async def test_first_token_ms_written_when_streaming() -> None:
    ak = await _first_ai_additional_kwargs(_SlowFirstTokenLLM(0.03), with_sink=True)
    assert isinstance(ak.get("first_token_ms"), int)
    assert ak["first_token_ms"] >= 25  # 30ms 睡眠,允许计时抖动


@pytest.mark.asyncio
async def test_first_token_ms_absent_without_sink_or_without_stream() -> None:
    no_sink = await _first_ai_additional_kwargs(_SlowFirstTokenLLM(0.0), with_sink=False)
    assert "first_token_ms" not in no_sink
    no_stream = await _first_ai_additional_kwargs(
        _SlowFirstTokenLLM(0.0, streams=False), with_sink=True
    )
    assert "first_token_ms" not in no_stream
