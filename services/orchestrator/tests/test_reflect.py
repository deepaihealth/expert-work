"""Unit tests for the reflect node — Stream J.2 (self-critique).

Covers the reflection parser's tolerance + fail-safe, the ``reflect``
graph node (verdict / budget / cancellation), and the end-to-end
reflect↔agent loop using a scripted ``LLMCaller``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from expert_work.protocol import Plan, Reflection
from expert_work.runtime.cancellation import (
    CANCELLATION_TOKEN_KEY,
    CancellationToken,
    RunCancelledError,
)
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolRegistry, build_react_graph, make_reflect_node
from orchestrator.graph_builder.reflect import _parse_reflection
from orchestrator.tools.registry import ToolSpec


@dataclass
class _RecordingLLM:
    """LLMCaller stub: returns scripted responses and records each prompt."""

    responses: list[AIMessage]
    calls: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
    ) -> AIMessage:
        del tools
        idx = len(self.calls)
        self.calls.append(list(messages))
        if idx >= len(self.responses):
            raise RuntimeError(f"scripted LLM ran out of responses at call {idx}")
        return self.responses[idx]


_PLAN = Plan.model_validate({"goal": "g", "steps": [{"id": "1", "description": "old step"}]})


# ---------------------------------------------------------------------------
# _parse_reflection
# ---------------------------------------------------------------------------


def test_parse_reflection_accept() -> None:
    reflection, revised = _parse_reflection(
        '{"verdict": "accept", "critique": "looks good"}', plan=None
    )
    assert reflection.verdict == "accept"
    assert reflection.critique == "looks good"
    assert revised is None


def test_parse_reflection_revise() -> None:
    reflection, revised = _parse_reflection(
        'sure: {"verdict": "revise", "critique": "missed a requirement"}', plan=None
    )
    assert reflection.verdict == "revise"
    assert revised is None


def test_parse_reflection_revise_with_revised_plan() -> None:
    reflection, revised = _parse_reflection(
        '{"verdict": "revise", "critique": "plan stale", "revised_steps": ["new a", "new b"]}',
        plan=_PLAN,
    )
    assert reflection.verdict == "revise"
    assert revised is not None
    assert [s.description for s in revised.steps] == ["new a", "new b"]
    assert revised.goal == "g"


def test_parse_reflection_revised_steps_ignored_without_a_plan() -> None:
    _reflection, revised = _parse_reflection(
        '{"verdict": "revise", "critique": "x", "revised_steps": ["a"]}', plan=None
    )
    assert revised is None


@pytest.mark.parametrize(
    "text",
    [
        "no json here",
        '{"critique": "missing verdict"}',
        '{"verdict": "maybe", "critique": "bad verdict"}',
        "{ not valid json }",
    ],
)
def test_parse_reflection_fails_safe_to_accept(text: str) -> None:
    """An unparseable reflection must accept — never loop forever."""
    reflection, revised = _parse_reflection(text, plan=None)
    assert reflection.verdict == "accept"
    assert revised is None


# ---------------------------------------------------------------------------
# reflect node
# ---------------------------------------------------------------------------


def _state(messages: list[BaseMessage], **extra: object) -> dict[str, object]:
    return {"messages": messages, "step_count": 1, "max_steps": 5, **extra}


@pytest.mark.asyncio
async def test_reflect_node_accept_emits_reflection_only() -> None:
    llm = _RecordingLLM(responses=[AIMessage(content='{"verdict": "accept", "critique": "ok"}')])
    node = make_reflect_node(llm, budget=2)

    out = await node(  # type: ignore[arg-type]
        _state([HumanMessage(content="task"), AIMessage(content="answer")]),
        {"configurable": {}},
    )
    assert [r.verdict for r in out["reflections"]] == ["accept"]
    # accept → no feedback message, no replan.
    assert "messages" not in out
    assert "plan" not in out


@pytest.mark.asyncio
async def test_reflect_node_revise_appends_feedback() -> None:
    llm = _RecordingLLM(
        responses=[AIMessage(content='{"verdict": "revise", "critique": "incomplete"}')]
    )
    node = make_reflect_node(llm, budget=2)

    out = await node(  # type: ignore[arg-type]
        _state([HumanMessage(content="task"), AIMessage(content="weak answer")]),
        {"configurable": {}},
    )
    assert out["reflections"][0].verdict == "revise"
    feedback = out["messages"][0]
    assert isinstance(feedback, HumanMessage)
    assert "incomplete" in str(feedback.content)


@pytest.mark.asyncio
async def test_reflect_node_revise_feedback_hidden_from_ui() -> None:
    """revise 注入的 [Reflection] HumanMessage 必须标 hide_from_ui —— 否则
    /messages 出现假 user 消息,污染第三方对话历史并破坏历史轮 order-pairing。"""
    llm = _RecordingLLM(
        responses=[AIMessage(content='{"verdict": "revise", "critique": "incomplete"}')]
    )
    node = make_reflect_node(llm, budget=2)

    out = await node(  # type: ignore[arg-type]
        _state([HumanMessage(content="task"), AIMessage(content="weak answer")]),
        {"configurable": {}},
    )
    injected = out["messages"][0]
    assert injected.additional_kwargs.get("expert_work_hide_from_ui") is True


@pytest.mark.asyncio
async def test_reflect_node_revise_replans_for_plan_execute() -> None:
    llm = _RecordingLLM(
        responses=[
            AIMessage(
                content='{"verdict": "revise", "critique": "plan stale", '
                '"revised_steps": ["fresh step"]}'
            )
        ]
    )
    node = make_reflect_node(llm, budget=2)

    out = await node(  # type: ignore[arg-type]
        _state([HumanMessage(content="task"), AIMessage(content="x")], plan=_PLAN),
        {"configurable": {}},
    )
    assert out["plan"].steps[0].description == "fresh step"


@pytest.mark.asyncio
async def test_reflect_node_budget_exhausted_force_accepts_without_llm() -> None:
    llm = _RecordingLLM(responses=[])  # must never be called
    node = make_reflect_node(llm, budget=1)

    out = await node(  # type: ignore[arg-type]
        _state(
            [HumanMessage(content="task"), AIMessage(content="answer")],
            reflections=[Reflection(verdict="revise", critique="earlier")],
        ),
        {"configurable": {}},
    )
    assert out["reflections"][0].verdict == "accept"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_reflect_node_honours_cancellation() -> None:
    token = CancellationToken()
    token.cancel()
    node = make_reflect_node(_RecordingLLM(responses=[AIMessage(content="{}")]), budget=2)
    config: RunnableConfig = {"configurable": {CANCELLATION_TOKEN_KEY: token}}
    with pytest.raises(RunCancelledError):
        await node(_state([HumanMessage(content="t")]), config)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# K9 — wall-clock timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflect_node_wallclock_timeout_falls_back_to_accept() -> None:
    """Stream K.K9 — a hung provider must not lock the run.

    The fake caller sleeps 0.5s; ``deadline_s=0.05`` means
    ``asyncio.wait_for`` fires first and the node must force-accept
    (orthogonal to cancellation, which fires only on client disconnect).
    """

    async def _hang(*, messages: list[Any], tools: list[Any]) -> AIMessage:
        del messages, tools  # unused — the caller never gets to act on them
        await asyncio.sleep(0.5)
        return AIMessage(content="late")

    node = make_reflect_node(_hang, budget=2, deadline_s=0.05)  # type: ignore[arg-type]

    out = await node(  # type: ignore[arg-type]
        _state([HumanMessage(content="task"), AIMessage(content="answer")]),
        {"configurable": {}},
    )
    assert out["reflections"][0].verdict == "accept"
    assert "timed out" in out["reflections"][0].critique


@pytest.mark.asyncio
async def test_reflect_node_returns_normally_within_deadline() -> None:
    """Sanity: a snappy LLM call still goes through the timeout wrapper."""
    llm = _RecordingLLM(responses=[AIMessage(content='{"verdict": "accept", "critique": "ok"}')])
    node = make_reflect_node(llm, budget=2, deadline_s=5)

    out = await node(  # type: ignore[arg-type]
        _state([HumanMessage(content="task"), AIMessage(content="answer")]),
        {"configurable": {}},
    )
    assert out["reflections"][0].verdict == "accept"
    assert out["reflections"][0].critique == "ok"


# ---------------------------------------------------------------------------
# reflect↔agent loop — end to end
# ---------------------------------------------------------------------------


async def _run(llm: _RecordingLLM, *, budget: int, max_steps: int = 8) -> dict[str, object]:
    graph = build_react_graph(
        llm_caller=llm,
        tool_registry=ToolRegistry(),
        reflect_node=make_reflect_node(llm, budget=budget),
    )
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        return await compiled.ainvoke(
            {
                "messages": [SystemMessage(content="help"), HumanMessage(content="do it")],
                "step_count": 0,
                "max_steps": max_steps,
            },
            config={"configurable": {"thread_id": "reflect-e2e"}},
        )


@pytest.mark.asyncio
async def test_reflect_loop_revise_then_accept() -> None:
    llm = _RecordingLLM(
        responses=[
            AIMessage(content="draft answer"),  # agent
            AIMessage(content='{"verdict": "revise", "critique": "add detail"}'),  # reflect
            AIMessage(content="final answer"),  # agent (after feedback)
            AIMessage(content='{"verdict": "accept", "critique": "good"}'),  # reflect
        ]
    )
    result = await _run(llm, budget=3)

    assert [r.verdict for r in result["reflections"]] == ["revise", "accept"]
    assert result["messages"][-1].content == "final answer"
    assert len(llm.calls) == 4


@pytest.mark.asyncio
async def test_reflect_budget_caps_the_loop() -> None:
    """Once the budget is hit the reflect node force-accepts and the run
    ends even though the LLM keeps saying revise."""
    llm = _RecordingLLM(
        responses=[
            AIMessage(content="answer 1"),  # agent
            AIMessage(content='{"verdict": "revise", "critique": "again"}'),  # reflect (real)
            AIMessage(content="answer 2"),  # agent
            # reflect entry 2 sees budget=1 reached → force-accept, no LLM call
        ]
    )
    result = await _run(llm, budget=1)

    # One real reflection + one budget-forced accept.
    assert [r.verdict for r in result["reflections"]] == ["revise", "accept"]
    assert result["messages"][-1].content == "answer 2"
    assert len(llm.calls) == 3


@pytest.mark.asyncio
async def test_reflect_budget_is_per_run_not_per_thread() -> None:
    """``reflections`` accumulates in the thread checkpoint, but the
    budget must reset each run — run 2 reflects with a fresh budget."""
    llm = _RecordingLLM(
        responses=[
            # run 1, budget=1 — one real reflection then a forced accept
            AIMessage(content="r1 a1"),
            AIMessage(content='{"verdict": "revise", "critique": "more"}'),
            AIMessage(content="r1 a2"),
            # run 2 — must get a fresh budget and reflect again
            AIMessage(content="r2 a1"),
            AIMessage(content='{"verdict": "revise", "critique": "more"}'),
            AIMessage(content="r2 a2"),
        ]
    )
    graph = build_react_graph(
        llm_caller=llm,
        tool_registry=ToolRegistry(),
        reflect_node=make_reflect_node(llm, budget=1),
    )
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        await compiled.ainvoke(
            {"messages": [HumanMessage(content="task one")], "step_count": 0, "max_steps": 8},
            config={"configurable": {"thread_id": "T", "run_id": "run-1"}},
        )
        result2 = await compiled.ainvoke(
            {"messages": [HumanMessage(content="task two")], "step_count": 0, "max_steps": 8},
            config={"configurable": {"thread_id": "T", "run_id": "run-2"}},
        )

    # Run 2 actually reflected (revise → agent), not force-accepted on
    # entry. With the per-thread bug run 2 sees run 1's reflections, the
    # budget reads as exhausted, and only 4 LLM calls happen.
    assert len(llm.calls) == 6
    assert result2["messages"][-1].content == "r2 a2"
    run2 = [r for r in result2["reflections"] if r.run_id == "run-2"]
    assert [r.verdict for r in run2] == ["revise", "accept"]


@pytest.mark.asyncio
async def test_react_graph_without_reflect_has_no_reflect_node() -> None:
    llm = _RecordingLLM(responses=[AIMessage(content="done")])
    graph = build_react_graph(llm_caller=llm, tool_registry=ToolRegistry())
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        result = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="hi")],
                "step_count": 0,
                "max_steps": 5,
            },
            config={"configurable": {"thread_id": "no-reflect"}},
        )
    # No reflect node ran — the add-reducer channel stays empty.
    assert not result.get("reflections")
