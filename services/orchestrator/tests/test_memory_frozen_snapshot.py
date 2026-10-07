"""Capability Uplift Sprint #8 — memory frozen snapshot (Mini-ADR U-8).

Exercises both ``_inject_memories`` modes through the full ReAct graph:

- ``per_session`` (default): memory block lands at ``messages[1]`` with
  ``additional_kwargs["expert_work_cache_anchor"] = True`` and stays at that
  position across every turn — the prefix
  ``[system, task, memories]`` is cacheable by the Anthropic adapter.
- ``per_turn`` (legacy): memory block lands at the tail every turn
  (the pre-Sprint-#8 J.3 behavior); no cache anchor.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig

from expert_work.protocol import MemoryItem
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)
from orchestrator.graph_builder.builder import _inject_memories


@dataclass
class _RecordingLLM:
    responses: list[AIMessage]
    calls: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del tools
        self.calls.append(list(messages))
        idx = len(self.calls) - 1
        if idx >= len(self.responses):
            raise RuntimeError(f"scripted LLM ran out at call {idx}")
        return self.responses[idx]


@dataclass
class _NoopTool:
    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="noop", description="noop", is_read_only=True)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content="ok")


def _memory(content: str) -> MemoryItem:
    return MemoryItem(
        id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        kind="fact",
        content=content,
        embedding=(),
    )


def _find_memory_message(messages: Sequence[BaseMessage]) -> tuple[int, BaseMessage] | None:
    # B-162 —— ``per_turn`` 记忆追加在最后一条消息(用户消息 / 工具结果)末尾。
    for idx, msg in enumerate(messages):
        if (
            isinstance(msg, HumanMessage | ToolMessage)
            and isinstance(msg.content, str)
            and "Relevant memories" in msg.content
        ):
            return idx, msg
    return None


# ---------------------------------------------------------------------------
# _inject_memories — unit-level (no graph)
# ---------------------------------------------------------------------------


def test_inject_per_session_lands_at_position_one_with_anchor() -> None:
    messages: list[BaseMessage] = [
        SystemMessage(content="system"),
        HumanMessage(content="task"),
    ]
    out = _inject_memories(messages, [_memory("user likes tea")], mode="per_session")
    assert len(out) == 3
    assert isinstance(out[0], SystemMessage)
    assert isinstance(out[1], HumanMessage)
    assert "Relevant memories" in str(out[1].content)
    assert out[1].additional_kwargs.get("expert_work_cache_anchor") is True
    # The original user task slides to position 2.
    assert isinstance(out[2], HumanMessage)
    assert out[2].content == "task"


def test_inject_per_turn_lands_at_tail_without_anchor() -> None:
    messages: list[BaseMessage] = [
        SystemMessage(content="system"),
        HumanMessage(content="task"),
        AIMessage(content="a1"),
    ]
    out = _inject_memories(messages, [_memory("user likes tea")], mode="per_turn")
    assert len(out) == 4
    assert isinstance(out[-1], HumanMessage)
    assert "Relevant memories" in str(out[-1].content)
    # Legacy mode: no cache anchor.
    assert out[-1].additional_kwargs.get("expert_work_cache_anchor") is None


def test_inject_default_mode_is_per_session() -> None:
    """Mini-ADR U-8: per_session is the platform default."""
    messages: list[BaseMessage] = [
        SystemMessage(content="system"),
        HumanMessage(content="task"),
    ]
    out = _inject_memories(messages, [_memory("x")])  # no mode= → default
    assert out[1].additional_kwargs.get("expert_work_cache_anchor") is True


def test_inject_empty_messages_returns_block_only() -> None:
    out = _inject_memories([], [_memory("x")], mode="per_session")
    assert len(out) == 1
    assert isinstance(out[0], HumanMessage)
    assert out[0].additional_kwargs.get("expert_work_cache_anchor") is True


# ---------------------------------------------------------------------------
# Multi-turn position stability (the key property for cache hit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_session_memory_position_stable_across_turns() -> None:
    """Sprint #8 core invariant: with ``per_session`` mode, the memory
    block sits at ``messages[1]`` on every LLM call — the prefix
    ``[system, memories]`` is byte-stable so Anthropic prompt cache
    covers it across the whole session."""
    llm = _RecordingLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "noop", "args": {}, "id": "tc-1", "type": "tool_call"}],
            ),
            AIMessage(content="done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_NoopTool())

    graph = build_react_graph(
        llm_caller=llm,
        tool_registry=registry,
        memory_recall_mode="per_session",
    )
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        await compiled.ainvoke(
            {
                "messages": [
                    SystemMessage(content="you are helpful"),
                    HumanMessage(content="start"),
                ],
                "step_count": 0,
                "max_steps": 5,
                "recalled_memories": [_memory("user prefers concise replies")],
            },
            config=cfg,
        )

    assert len(llm.calls) == 2
    # Memory block must be at index 1 on BOTH calls.
    for turn, call in enumerate(llm.calls):
        loc = _find_memory_message(call)
        assert loc is not None, f"memory missing on turn {turn}"
        idx, block = loc
        assert idx == 1, f"per_session memory drifted to index {idx} on turn {turn}"
        assert block.additional_kwargs.get("expert_work_cache_anchor") is True


@pytest.mark.asyncio
async def test_per_turn_memory_position_drifts_across_turns() -> None:
    """Legacy mode contract: ``per_turn`` keeps the J.3 tail behavior —
    memory is always the LAST message, so its absolute position drifts
    as AI/Tool messages accumulate. This is the cache-defeating
    behavior Sprint #8 exists to fix."""
    llm = _RecordingLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "noop", "args": {}, "id": "tc-1", "type": "tool_call"}],
            ),
            AIMessage(content="done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_NoopTool())

    graph = build_react_graph(
        llm_caller=llm,
        tool_registry=registry,
        memory_recall_mode="per_turn",
    )
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        await compiled.ainvoke(
            {
                "messages": [
                    SystemMessage(content="you are helpful"),
                    HumanMessage(content="start"),
                ],
                "step_count": 0,
                "max_steps": 5,
                "recalled_memories": [_memory("user prefers concise replies")],
            },
            config=cfg,
        )

    assert len(llm.calls) == 2
    positions = []
    for call in llm.calls:
        loc = _find_memory_message(call)
        assert loc is not None
        idx, block = loc
        positions.append(idx)
        # Legacy mode never sets the anchor flag.
        assert block.additional_kwargs.get("expert_work_cache_anchor") is None
    # Turn 1 had [system, start, memories]; turn 2 has [system, start,
    # AI, ToolMessage, memories] — different absolute indices.
    assert positions[0] != positions[1], f"per_turn should drift across turns; got {positions}"


@pytest.mark.asyncio
async def test_empty_recalled_memories_injects_nothing() -> None:
    """Both modes no-op when there's nothing to inject."""
    # One LLM response per outer-loop iteration; the graph terminates
    # after the first agent reply (no tool calls).
    llm = _RecordingLLM(responses=[AIMessage(content="done"), AIMessage(content="done")])

    for mode in ("per_session", "per_turn"):
        graph = build_react_graph(
            llm_caller=llm,
            tool_registry=ToolRegistry(),
            memory_recall_mode=mode,  # type: ignore[arg-type]
        )
        async with make_checkpointer("memory") as cp:
            compiled = GraphRunner(checkpointer=cp).compile(graph)
            cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
            await compiled.ainvoke(
                {
                    "messages": [
                        SystemMessage(content="hi"),
                        HumanMessage(content="task"),
                    ],
                    "step_count": 0,
                    "max_steps": 1,
                    "recalled_memories": [],
                },
                config=cfg,
            )

    for call in llm.calls:
        assert _find_memory_message(call) is None
