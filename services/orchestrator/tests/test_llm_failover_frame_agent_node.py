"""B-297 PR2 — through the REAL react graph and a real ``LLMRouter``: a
reasoning-only stall on the primary is redone on the backup, the agent step
publishes one ``llm_failover`` frame (via the exact ``FAILOVER_SINK_KEY`` hook
``sse.run_agent`` injects), and the token stream reads attempt 1's reasoning,
a paragraph break, then the backup's output — in that order relative to the
notice."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolRegistry, build_react_graph
from orchestrator.graph_builder._config import FAILOVER_SINK_KEY, TOKEN_SINK_KEY
from orchestrator.llm.failover import StickyRouteStore
from orchestrator.llm.providers._streaming import LLMDelta, OpenAIStreamAssembler
from orchestrator.llm.router import LLMRouter, ProviderHandle


class _Scripted:
    """Yields its deltas; ``None`` = the upstream goes silent (the idle timer
    fires — emulated by raising ``TimeoutError``, no real wait)."""

    def __init__(self, script: list[LLMDelta | None]) -> None:
        self.script = script

    async def stream(
        self, *, messages: Any, tools: Any, output_schema: Any = None
    ) -> AsyncIterator[LLMDelta]:
        del messages, tools, output_schema
        for item in self.script:
            if item is None:
                raise TimeoutError
            yield item

    def new_stream_assembler(self) -> OpenAIStreamAssembler:
        return OpenAIStreamAssembler()


@pytest.mark.asyncio
async def test_agent_step_publishes_the_switch_and_streams_the_redo() -> None:
    primary = _Scripted([LLMDelta(reasoning="x" * 80), None])
    backup = _Scripted(
        [
            LLMDelta(reasoning="backup thinks"),
            LLMDelta(content="ok"),
            LLMDelta(finish_reason="stop"),
        ]
    )
    router = LLMRouter(
        providers=[
            ProviderHandle(provider=primary, key="deepseek:deepseek-flash"),  # type: ignore[arg-type]
            ProviderHandle(provider=backup, key="qwen:qwen3.8-max"),  # type: ignore[arg-type]
        ],
        first_token_timeout_s=60.0,
        idle_timeout_s=45.0,
        sticky_store=StickyRouteStore(),
    )
    seen: list[tuple[str, dict[str, Any]]] = []

    async def on_token(frame: dict[str, Any]) -> None:
        seen.append(("token", frame))

    async def on_failover(frame: dict[str, Any]) -> None:
        seen.append(("llm_failover", frame))

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=router, tool_registry=ToolRegistry())
        )
        cfg: RunnableConfig = {
            "configurable": {
                "thread_id": str(uuid4()),
                TOKEN_SINK_KEY: on_token,
                FAILOVER_SINK_KEY: on_failover,
            }
        }
        final: list[Any] = []
        async for chunk in compiled.astream(
            {"messages": [HumanMessage(content="hi")], "step_count": 0, "max_steps": 5},
            config=cfg,
            stream_mode="updates",
        ):
            agent = chunk.get("agent") if isinstance(chunk, dict) else None
            if isinstance(agent, dict):
                final.extend(agent.get("messages") or [])

    assert [m.content for m in final] == ["ok"]
    assert final[0].additional_kwargs.get("reasoning_content") == "backup thinks"
    notices = [f for kind, f in seen if kind == "llm_failover"]
    assert notices == [
        {
            "step": 1,
            "from_model": "deepseek-flash",
            "to_model": "qwen3.8-max",
            "reason": "reasoning_stall",
        }
    ]
    order = [
        (kind, f.get("channel"), f.get("text"))
        for kind, f in seen
        if kind == "llm_failover" or f.get("text")
    ]
    notice_at = order.index(("llm_failover", None, None))
    assert order[notice_at - 1] == ("token", "reasoning", "\n\n")
    assert ("token", "reasoning", "backup thinks") in order[notice_at + 1 :]
    assert all(f["step"] == 1 for kind, f in seen)
