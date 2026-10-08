"""Stream L.L1 — system-prompt byte-stable invariant test.

The Mini-ADR L-1 contract: across multiple agent steps on the same
thread the leading ``SystemMessage`` must stay byte-identical so the
Anthropic prompt-cache prefix is reusable. Per-turn dynamic context
(plan, recalled memories) lives in tail ``HumanMessage`` blocks
appended only for the in-flight LLM call.

This integration test sweeps a multi-step ReAct run (with plan + memory
both active) and asserts the leading SystemMessage hash is identical
across every LLM call. A regression in ``_inject_plan`` /
``_inject_memories`` (e.g., merging back into the system block) would
fail this test immediately.
"""

from __future__ import annotations

import hashlib
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

from expert_work.protocol import MemoryItem, Plan, PlanStep
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)


@dataclass
class _RecordingLLM:
    """Captures every prompt for post-run inspection of system content."""

    responses: list[AIMessage]
    calls: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
    ) -> AIMessage:
        del tools
        self.calls.append(list(messages))
        idx = len(self.calls) - 1
        if idx >= len(self.responses):
            msg = f"scripted LLM ran out at call {idx}"
            raise RuntimeError(msg)
        return self.responses[idx]


def _hash_system(messages: Sequence[BaseMessage]) -> str:
    """SHA-256 of the leading ``SystemMessage``'s content text."""
    if not messages or not isinstance(messages[0], SystemMessage):
        return ""
    content = messages[0].content
    text = content if isinstance(content, str) else str(content)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Plan + memory both present — leading SystemMessage stays byte-stable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_message_byte_stable_with_plan_injection() -> None:
    """A two-step run with a plan injected on every agent call: the
    SystemMessage at messages[0] must be byte-identical across both
    LLM calls. Pre-L1 the plan was merged into system → hash changed
    on every step."""

    @dataclass
    class _NoopTool:
        @property
        def spec(self) -> ToolSpec:
            return ToolSpec(name="noop", description="noop", is_read_only=True)

        async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
            del args, ctx
            return ToolResult(content="ok")

    llm = _RecordingLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "noop", "args": {}, "id": "tc-1", "type": "tool_call"},
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_NoopTool())
    plan = Plan(goal="do X", steps=(PlanStep(id="1", description="poke noop"),))

    graph = build_react_graph(llm_caller=llm, tool_registry=registry)
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
                "plan": plan,
            },
            config=cfg,
        )

    hashes = [_hash_system(call) for call in llm.calls]
    assert len(llm.calls) == 2
    assert hashes[0] == hashes[1], "leading SystemMessage drifted across turns (L-1 invariant)"
    # And the plan body appears in a tail HumanMessage, not the system.
    for call in llm.calls:
        plan_text = "Execution plan"
        assert all(plan_text not in str(m.content) for m in call if isinstance(m, SystemMessage))
        # B-162 —— 追加在最后一条消息(用户消息 / 工具结果)末尾。
        assert any(plan_text in str(m.content) for m in call if not isinstance(m, SystemMessage))


@pytest.mark.asyncio
async def test_system_message_byte_stable_with_memory_injection() -> None:
    """Recalled memories follow the same L-1 rule — they ride a tail
    HumanMessage, system stays byte-stable across turns."""

    @dataclass
    class _NoopTool:
        @property
        def spec(self) -> ToolSpec:
            return ToolSpec(name="noop", description="noop", is_read_only=True)

        async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
            del args, ctx
            return ToolResult(content="ok")

    llm = _RecordingLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "noop", "args": {}, "id": "tc-1", "type": "tool_call"},
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_NoopTool())

    graph = build_react_graph(llm_caller=llm, tool_registry=registry)
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
                "recalled_memories": [
                    MemoryItem(
                        id=uuid4(),
                        tenant_id=uuid4(),
                        user_id=uuid4(),
                        kind="fact",
                        content="user prefers concise replies",
                        embedding=(),
                    ),
                ],
            },
            config=cfg,
        )

    hashes = [_hash_system(call) for call in llm.calls]
    assert len(llm.calls) == 2
    assert hashes[0] == hashes[1]
    for call in llm.calls:
        # Memory content lives in HumanMessage, not SystemMessage.
        memory_text = "Relevant memories"
        assert all(memory_text not in str(m.content) for m in call if isinstance(m, SystemMessage))
        assert any(memory_text in str(m.content) for m in call if isinstance(m, HumanMessage))


@pytest.mark.asyncio
async def test_system_message_byte_stable_with_mutation_advisory() -> None:
    """L1 + L4 interplay: a failed mutation injects an advisory
    HumanMessage but DOES NOT touch the SystemMessage. The L-1 cache
    prefix stays valid even when the advisory fires."""

    @dataclass
    class _FailingSave:
        @property
        def spec(self) -> ToolSpec:
            return ToolSpec(
                name="save_artifact",
                description="failing save",
                parameters={
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                },
                path_args=("name",),
            )

        async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
            del ctx
            msg = f"disk full saving {args['name']!r}"
            raise OSError(msg)

    llm = _RecordingLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_artifact",
                        "args": {"name": "report.md"},
                        "id": "tc-1",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(content="I see the failure"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_FailingSave())

    graph = build_react_graph(llm_caller=llm, tool_registry=registry)
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        await compiled.ainvoke(
            {
                "messages": [
                    SystemMessage(content="you are an editor"),
                    HumanMessage(content="save it"),
                ],
                "step_count": 0,
                "max_steps": 5,
            },
            config=cfg,
        )

    hashes = [_hash_system(call) for call in llm.calls]
    assert len(llm.calls) == 2
    assert hashes[0] == hashes[1]
    # Advisory appears on the second call (after the failure surfaced), at the
    # end of the failed tool result (B-163).
    advisory_in_second = any(
        isinstance(m, ToolMessage)
        and isinstance(m.content, str)
        and "<recovery-advisory>" in m.content
        for m in llm.calls[1]
    )
    assert advisory_in_second
    # And it never landed in the system block.
    assert all(
        "<recovery-advisory>" not in str(m.content)
        for call in llm.calls
        for m in call
        if isinstance(m, SystemMessage)
    )


@pytest.mark.asyncio
async def test_tool_message_in_history_does_not_change_system_hash() -> None:
    """Adding tool messages / AI messages / human messages to the
    conversation history does not change the system_message hash —
    only the per-turn injects could (and L1 forbids that)."""
    # Pre-seed messages list: SystemMessage + multiple history msgs.
    pre = [
        SystemMessage(content="static prompt"),
        HumanMessage(content="t1"),
        AIMessage(content="a1"),
        ToolMessage(content="r1", tool_call_id="tc-1"),
    ]
    # Same SystemMessage content but appended history.
    post = [*pre, HumanMessage(content="t2")]
    assert _hash_system(pre) == _hash_system(post)
    # Sanity — different system text yields different hash.
    pre_alt = [SystemMessage(content="other prompt"), HumanMessage(content="t1")]
    assert _hash_system(pre) != _hash_system(pre_alt)
