"""Stream L.L2 — :class:`ContextCompressor` ↔ ``agent_node`` integration.

Drives the compiled ReAct graph with a configured compressor and asserts
the preflight fires on a large conversation: the LLM sees a compressed
prompt instead of the raw history, and the run completes inside the
declared context_window. A graph without a compressor exhibits the
pre-L2 behaviour (no compression, full history reaches the LLM).
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

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)
from orchestrator.context import ContextCompressor, ContextOverflowError


@dataclass
class _RecordingLLM:
    """Captures the prompt of every call and returns a deterministic reply."""

    summary_text: str = "- bullet recap"
    final_text: str = "done"
    calls: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
    ) -> AIMessage:
        del tools
        self.calls.append(list(messages))
        return AIMessage(content=self.final_text)


@dataclass
class _SummariserOnly:
    """LLMCaller that only ever returns the configured summary body —
    used as the compressor's summariser; never invoked as the agent's
    main caller in these tests."""

    summary_text: str

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
    ) -> AIMessage:
        del messages, tools
        return AIMessage(content=self.summary_text)


def _pad(prefix: str, length: int) -> str:
    """Build a message body of exactly ``length`` chars starting with ``prefix``."""
    return prefix + ("x" * max(0, length - len(prefix)))


# ---------------------------------------------------------------------------
# Preflight triggers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_node_triggers_compression_when_prompt_oversized() -> None:
    """A conversation whose estimated size exceeds the threshold fires
    the compressor before the LLM call — the LLM's prompt carries the
    summary in place of the original middle."""
    summariser = _SummariserOnly(summary_text="- summarised topic")
    compressor = ContextCompressor(
        llm_caller=summariser,
        context_window=600,
        threshold_pct=0.5,
        head_keep=2,
        tail_keep=2,
    )
    agent_llm = _RecordingLLM()
    graph = build_react_graph(
        llm_caller=agent_llm,
        tool_registry=ToolRegistry(),
        context_compressor=compressor,
    )
    # 16 messages x 80 chars = ~320 tokens — over threshold (300).
    history: list[BaseMessage] = [HumanMessage(content=_pad(f"user-{i}-", 80)) for i in range(16)]

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        await compiled.ainvoke(
            {
                "messages": history,
                "step_count": 0,
                "max_steps": 5,
            },
            config=cfg,
        )

    # Exactly one LLM call (no tool calls, immediate done).
    assert len(agent_llm.calls) == 1
    prompt = agent_llm.calls[0]
    # Original history had 16 messages; after compression head(2) +
    # summary(1) + tail(2) = 5.
    assert len(prompt) == 5
    # Summary lives between head and tail as a SystemMessage tagged
    # with the canonical wrapper.
    assert isinstance(prompt[2], SystemMessage)
    assert "<context-summary>" in str(prompt[2].content)
    assert "summarised topic" in str(prompt[2].content)


@pytest.mark.asyncio
async def test_agent_node_skips_compression_when_under_threshold() -> None:
    """A small conversation passes through ``should_compress`` cleanly
    — the LLM sees the full history."""

    @dataclass
    class _NeverSummarise:
        async def __call__(
            self,
            *,
            messages: Sequence[BaseMessage],
            tools: Sequence[ToolSpec],
        ) -> AIMessage:
            del messages, tools
            msg = "compressor.compress must not run on small inputs"
            raise AssertionError(msg)

    compressor = ContextCompressor(
        llm_caller=_NeverSummarise(),
        context_window=10_000,
        threshold_pct=0.9,  # well above the tiny prompt
    )
    agent_llm = _RecordingLLM()
    graph = build_react_graph(
        llm_caller=agent_llm,
        tool_registry=ToolRegistry(),
        context_compressor=compressor,
    )

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="hi")],
                "step_count": 0,
                "max_steps": 5,
            },
            config=cfg,
        )

    # LLM saw the original single message — content matches (LangGraph
    # assigns a fresh id when the message enters checkpointed state,
    # so equality on the full object isn't reliable).
    assert len(agent_llm.calls) == 1
    assert len(agent_llm.calls[0]) == 1
    assert agent_llm.calls[0][0].content == "hi"
    assert isinstance(agent_llm.calls[0][0], HumanMessage)


@pytest.mark.asyncio
async def test_agent_node_without_compressor_runs_pre_l2_path() -> None:
    """``context_compressor=None`` is the opt-out — the graph still
    builds cleanly and the agent_node never preflights. Existing
    callers that haven't yet wired the compressor keep working."""
    agent_llm = _RecordingLLM()
    graph = build_react_graph(
        llm_caller=agent_llm,
        tool_registry=ToolRegistry(),
        context_compressor=None,
    )
    history = [HumanMessage(content=_pad(f"msg-{i}-", 80)) for i in range(50)]

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        await compiled.ainvoke(
            {
                "messages": history,
                "step_count": 0,
                "max_steps": 5,
            },
            config=cfg,
        )

    # The full history reached the LLM — nothing got compressed.
    assert len(agent_llm.calls[0]) == 50


# ---------------------------------------------------------------------------
# Overflow surfaces as run failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsummarisable_overflow_raises_context_overflow_error() -> None:
    """A conversation whose head + tail alone exceed the threshold
    cannot be saved by compression. Mini-ADR L-2 surfaces this as a
    ContextOverflowError so the orchestrator writes a clean run-failed
    audit row rather than silently letting the prompt blow past the
    upstream window."""

    @dataclass
    class _NeverCalled:
        async def __call__(
            self,
            *,
            messages: Sequence[BaseMessage],
            tools: Sequence[ToolSpec],
        ) -> AIMessage:
            del messages, tools
            msg = "summariser must not be invoked"
            raise AssertionError(msg)

    compressor = ContextCompressor(
        llm_caller=_NeverCalled(),
        context_window=100,
        threshold_pct=0.5,  # threshold 50
        head_keep=5,
        tail_keep=5,
    )
    # 10 messages x 80 chars = 800 / 4 = 200 tokens; head_keep + tail_keep
    # cover the whole list → no middle to summarise.
    history = [HumanMessage(content=_pad(f"m-{i}-", 80)) for i in range(10)]
    agent_llm = _RecordingLLM()
    graph = build_react_graph(
        llm_caller=agent_llm,
        tool_registry=ToolRegistry(),
        context_compressor=compressor,
    )

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        with pytest.raises(ContextOverflowError):
            await compiled.ainvoke(
                {
                    "messages": history,
                    "step_count": 0,
                    "max_steps": 5,
                },
                config=cfg,
            )


# ---------------------------------------------------------------------------
# Interaction with other L invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compressor_preserves_leading_system_message_byte_stable() -> None:
    """L1 byte-stable invariant + L2 compression coexist: the leading
    SystemMessage stays the same object after compression."""
    summariser = _SummariserOnly(summary_text="- summary")
    compressor = ContextCompressor(
        llm_caller=summariser,
        context_window=600,
        threshold_pct=0.4,
        head_keep=2,
        tail_keep=2,
    )
    agent_llm = _RecordingLLM()
    graph = build_react_graph(
        llm_caller=agent_llm,
        tool_registry=ToolRegistry(),
        context_compressor=compressor,
    )
    system = SystemMessage(content="static prompt — must stay byte-stable")
    history: list[BaseMessage] = [system]
    history.extend(HumanMessage(content=_pad(f"m-{i}-", 80)) for i in range(16))

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        await compiled.ainvoke(
            {
                "messages": history,
                "step_count": 0,
                "max_steps": 5,
            },
            config=cfg,
        )

    prompt = agent_llm.calls[0]
    # The leading SystemMessage is still the original instance.
    assert prompt[0] is system


@pytest.mark.asyncio
async def test_compressor_handles_tool_message_in_middle() -> None:
    """ReAct conversations interleave AIMessage with ToolMessage; the
    compressor flattens both into the transcript and preserves the
    head + tail windows verbatim."""
    summariser = _SummariserOnly(summary_text="- middle done")
    compressor = ContextCompressor(
        llm_caller=summariser,
        context_window=600,
        threshold_pct=0.5,
        head_keep=2,
        tail_keep=2,
    )
    agent_llm = _RecordingLLM()
    graph = build_react_graph(
        llm_caller=agent_llm,
        tool_registry=ToolRegistry(),
        context_compressor=compressor,
    )
    head1 = HumanMessage(content=_pad("h1-", 80))
    head2 = AIMessage(content=_pad("h2-", 80))
    # 14 middle messages x 80 chars → ~280 tokens for middle alone;
    # with head + tail the total clears the 0.5 threshold of 600.
    middle = [ToolMessage(content=_pad(f"t-{i}-", 80), tool_call_id=f"tc-{i}") for i in range(14)]
    tail_pre = AIMessage(content=_pad("tail-pre-", 80))
    tail_final = HumanMessage(content=_pad("tail-final-", 80))
    history = [head1, head2, *middle, tail_pre, tail_final]

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        await compiled.ainvoke(
            {
                "messages": history,
                "step_count": 0,
                "max_steps": 5,
            },
            config=cfg,
        )

    prompt = agent_llm.calls[0]
    assert prompt[0] is head1
    assert prompt[1] is head2
    assert isinstance(prompt[2], SystemMessage)
    assert "middle done" in str(prompt[2].content)
    assert prompt[-2] is tail_pre
    assert prompt[-1] is tail_final


# ---------------------------------------------------------------------------
# Test fixture sanity — tool stub for non-text content paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compressor_summary_is_a_system_message_not_a_human_message() -> None:
    """Mini-ADR L-2: the summary lands as a ``SystemMessage`` (the
    canonical "context background" channel), NOT a ``HumanMessage``.
    This keeps the L4 mutation advisory's HumanMessage tail position
    from being shadowed by the summary."""
    summariser = _SummariserOnly(summary_text="- top-level facts")
    compressor = ContextCompressor(
        llm_caller=summariser,
        context_window=600,
        threshold_pct=0.4,
        head_keep=1,
        tail_keep=1,
    )

    @dataclass
    class _NoopTool:
        @property
        def spec(self) -> ToolSpec:
            return ToolSpec(name="noop", description="noop", is_read_only=True)

        async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
            del args, ctx
            return ToolResult(content="ok")

    agent_llm = _RecordingLLM()
    registry = ToolRegistry()
    registry.register(_NoopTool())
    graph = build_react_graph(
        llm_caller=agent_llm,
        tool_registry=registry,
        context_compressor=compressor,
    )
    history = [HumanMessage(content=_pad(f"m-{i}-", 80)) for i in range(20)]

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        await compiled.ainvoke(
            {
                "messages": history,
                "step_count": 0,
                "max_steps": 5,
            },
            config=cfg,
        )

    prompt = agent_llm.calls[0]
    # Find the summary entry.
    summary_msgs = [m for m in prompt if isinstance(m, SystemMessage)]
    assert len(summary_msgs) == 1
    assert "<context-summary>" in str(summary_msgs[0].content)


# ---------------------------------------------------------------------------
# B-126 Task 5 — cached summary written back to the checkpoint, reused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_node_reuses_cached_summary_across_turns() -> None:
    """The summary a turn's compression produces is written back into the
    checkpoint (``state["context_summary"]``); the next turn — one short
    HumanMessage added on top of the same over-threshold history — reuses
    it via ``through_id`` instead of calling the summariser LLM again."""

    @dataclass
    class _CountingSummariser:
        summary_text: str = "- topic recap"
        calls: int = 0

        async def __call__(
            self,
            *,
            messages: Sequence[BaseMessage],
            tools: Sequence[ToolSpec],
        ) -> AIMessage:
            del messages, tools
            self.calls += 1
            return AIMessage(content=self.summary_text)

    summariser = _CountingSummariser()
    compressor = ContextCompressor(
        llm_caller=summariser,
        context_window=2000,
        threshold_pct=0.5,
        head_keep=1,
        tail_keep=2,
    )
    agent_llm = _RecordingLLM()
    graph = build_react_graph(
        llm_caller=agent_llm,
        tool_registry=ToolRegistry(),
        context_compressor=compressor,
    )
    # 20 messages x 400 chars = 2000 tokens — over the 1000 threshold.
    history: list[BaseMessage] = [HumanMessage(content=_pad(f"m-{i}-", 400)) for i in range(20)]

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}

        await compiled.ainvoke(
            {"messages": history, "step_count": 0, "max_steps": 5},
            config=cfg,
        )
        assert summariser.calls == 1

        snapshot = await compiled.aget_state(cfg)
        cached = snapshot.values.get("context_summary")
        assert cached and cached.get("through_id")

        calls_before_turn2 = summariser.calls
        # Turn 2 — the raw checkpointed history only grows by turn 1's
        # reply plus this one short message, so it stays over threshold,
        # but the part that pushes it over is already inside the cached
        # summary's coverage.
        await compiled.ainvoke(
            {"messages": [HumanMessage(content="one more short thing")]},
            config=cfg,
        )

        # I3 — pin the EXACT reused sequence: head (m0), the reused summary,
        # every message strictly after through_id in order (m18, m19 — the
        # span between the cached boundary and the old tail), then the new
        # tail (turn 1's own reply + this turn's new message). An off-by-one
        # at the cut point (too small: duplicates through_id's own message
        # back in; too large: drops the first genuinely-new message) changes
        # this sequence without changing ``summariser.calls`` — that's why a
        # call-count-only assertion doesn't catch it.
        prompt = agent_llm.calls[-1]
        assert len(prompt) == 6
        assert prompt[0].content == history[0].content
        assert isinstance(prompt[1], SystemMessage)
        assert "<context-summary>" in str(prompt[1].content)
        assert "topic recap" in str(prompt[1].content)
        assert prompt[2].content == history[18].content
        assert prompt[3].content == history[19].content
        assert prompt[4].content == "done"
        assert prompt[5].content == "one more short thing"

    assert summariser.calls == calls_before_turn2
