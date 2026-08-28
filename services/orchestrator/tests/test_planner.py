"""Unit tests for the planner node — Stream J.1 (task decomposition).

Covers the plan parser's tolerance + fallback, plan rendering, the
``planner`` graph node, and the end-to-end ``plan_execute`` graph
(``START → planner → agent``) using a scripted ``LLMCaller``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from expert_work.protocol import Plan
from expert_work.runtime.cancellation import (
    CANCELLATION_TOKEN_KEY,
    CancellationToken,
    RunCancelledError,
)
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import GraphRunner, ToolRegistry, build_react_graph, make_planner_node
from orchestrator.graph_builder import parse_plan, render_plan
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


# ---------------------------------------------------------------------------
# parse_plan
# ---------------------------------------------------------------------------


def test_parse_plan_clean_json() -> None:
    plan = parse_plan(
        '{"goal": "ship the feature", "steps": ["design", "build", "test"]}',
        fallback_goal="fb",
    )
    assert plan.goal == "ship the feature"
    assert [s.description for s in plan.steps] == ["design", "build", "test"]
    assert [s.id for s in plan.steps] == ["1", "2", "3"]


def test_parse_plan_tolerates_prose_and_fences() -> None:
    text = 'Sure! Here is the plan:\n```json\n{"goal": "g", "steps": ["a", "b"]}\n```\nGood luck.'
    plan = parse_plan(text, fallback_goal="fb")
    assert plan.goal == "g"
    assert len(plan.steps) == 2


def test_parse_plan_drops_blank_steps() -> None:
    plan = parse_plan('{"goal": "g", "steps": ["real", "   ", ""]}', fallback_goal="fb")
    assert [s.description for s in plan.steps] == ["real"]


@pytest.mark.parametrize(
    "text",
    [
        "no json here at all",
        '{"goal": "g"}',  # missing steps
        '{"steps": ["a"]}',  # missing goal
        '{"goal": "g", "steps": []}',  # empty steps
        "{ this is not valid json }",
    ],
)
def test_parse_plan_falls_back_on_bad_input(text: str) -> None:
    plan = parse_plan(text, fallback_goal="the original task")
    assert plan.goal == "the original task"
    assert len(plan.steps) == 1
    assert plan.steps[0].description == "the original task"


# ---------------------------------------------------------------------------
# render_plan
# ---------------------------------------------------------------------------


def test_render_plan_lists_goal_and_numbered_steps() -> None:
    plan = Plan.model_validate({"goal": "win", "steps": [{"id": "1", "description": "do a"}]})
    rendered = render_plan(plan)
    assert "Execution plan" in rendered
    assert "Goal: win" in rendered
    assert "1. do a" in rendered


def test_render_plan_shows_status_checkboxes() -> None:
    # Stream CM-0 (N1) — recitation reflects progress so attention focuses on
    # what is still pending.
    plan = Plan.model_validate(
        {
            "goal": "win",
            "steps": [
                {"id": "1", "description": "done step", "status": "completed"},
                {"id": "2", "description": "active step", "status": "in_progress"},
                {"id": "3", "description": "todo step"},  # pending
            ],
        }
    )
    rendered = render_plan(plan)
    lines = rendered.splitlines()
    assert any("[x] 1. done step" in line for line in lines)
    assert any("[~] 2. active step" in line for line in lines)
    assert any("[ ] 3. todo step" in line for line in lines)


# ---------------------------------------------------------------------------
# planner node
# ---------------------------------------------------------------------------


def _state(task: str) -> dict[str, object]:
    return {
        "messages": [SystemMessage(content="you are helpful"), HumanMessage(content=task)],
        "step_count": 0,
        "max_steps": 5,
    }


@pytest.mark.asyncio
async def test_planner_node_builds_plan_from_llm() -> None:
    llm = _RecordingLLM(responses=[AIMessage(content='{"goal": "g", "steps": ["a", "b"]}')])
    node = make_planner_node(llm)

    out = await node(_state("decompose me"), {"configurable": {}})  # type: ignore[arg-type]

    plan = out["plan"]
    assert isinstance(plan, Plan)
    assert [s.description for s in plan.steps] == ["a", "b"]
    # The planner prompt carries the user's task.
    assert any("decompose me" in str(m.content) for m in llm.calls[0])


@pytest.mark.asyncio
async def test_planner_node_falls_back_on_garbage_llm_reply() -> None:
    llm = _RecordingLLM(responses=[AIMessage(content="I cannot help with that")])
    node = make_planner_node(llm)

    out = await node(_state("the real task"), {"configurable": {}})  # type: ignore[arg-type]

    plan = out["plan"]
    assert isinstance(plan, Plan)
    assert plan.goal == "the real task"
    assert len(plan.steps) == 1


@pytest.mark.asyncio
async def test_planner_node_honours_cancellation() -> None:
    token = CancellationToken()
    token.cancel()
    node = make_planner_node(_RecordingLLM(responses=[AIMessage(content="{}")]))

    config: RunnableConfig = {"configurable": {CANCELLATION_TOKEN_KEY: token}}
    with pytest.raises(RunCancelledError):
        await node(_state("task"), config)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# plan_execute graph — START → planner → agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_execute_graph_runs_planner_then_agent() -> None:
    llm = _RecordingLLM(
        responses=[
            AIMessage(content='{"goal": "do X", "steps": ["step one", "step two"]}'),
            AIMessage(content="finished"),
        ]
    )
    graph = build_react_graph(
        llm_caller=llm,
        tool_registry=ToolRegistry(),
        planner_node=make_planner_node(llm),
    )
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        result = await compiled.ainvoke(
            {
                "messages": [SystemMessage(content="you help"), HumanMessage(content="do X")],
                "step_count": 0,
                "max_steps": 5,
            },
            config={"configurable": {"thread_id": "plan-t1"}},
        )

    # The planner ran and stored a structured plan.
    assert result["plan"].goal == "do X"
    assert len(result["plan"].steps) == 2
    # Two LLM calls: planner, then the agent step.
    assert len(llm.calls) == 2
    # Stream L.L1 — the agent's prompt keeps the original system
    # message byte-stable, and the rendered plan rides on a tail
    # HumanMessage. Pre-L1 the plan was concatenated into the leading
    # SystemMessage; that broke the Anthropic prompt-cache prefix.
    agent_prompt = llm.calls[1]
    assert isinstance(agent_prompt[0], SystemMessage)
    system_text = str(agent_prompt[0].content)
    assert system_text == "you help"  # byte-stable: no plan content here
    # Plan body now lives in a tail HumanMessage.
    tail = agent_prompt[-1]
    assert isinstance(tail, HumanMessage)
    plan_text = str(tail.content)
    assert "Execution plan" in plan_text
    assert "step one" in plan_text


# ---------------------------------------------------------------------------
# update_plan tool — Stream K.K8 (in-run replan path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_plan_tool_promotes_new_plan_into_state() -> None:
    """Agent calls ``update_plan`` → ``tools_node`` writes the new plan
    onto ``AgentState.plan`` via the K.K8 allowlisted state channel."""
    from orchestrator.tools.update_plan import UpdatePlanTool

    llm = _RecordingLLM(
        responses=[
            # 1) planner — initial plan
            AIMessage(content='{"goal": "ship X", "steps": ["a", "b"]}'),
            # 2) agent — calls update_plan
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "update_plan",
                        "args": {
                            "steps": ["revised one", "revised two", "revised three"],
                            "reason": "spec changed mid-run",
                        },
                        "id": "tc-up-1",
                        "type": "tool_call",
                    }
                ],
            ),
            # 3) agent — final answer
            AIMessage(content="done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(UpdatePlanTool())  # implicit-tool wiring done in factory

    graph = build_react_graph(
        llm_caller=llm,
        tool_registry=registry,
        planner_node=make_planner_node(llm),
    )
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        result = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="ship X")],
                "step_count": 0,
                "max_steps": 5,
            },
            config={"configurable": {"thread_id": "update-plan-e2e"}},
        )

    # The new plan replaces the initial one (3 steps, revised content),
    # but the original goal is preserved by the tool.
    assert result["plan"].goal == "ship X"
    assert len(result["plan"].steps) == 3
    assert [s.description for s in result["plan"].steps] == [
        "revised one",
        "revised two",
        "revised three",
    ]


@pytest.mark.asyncio
async def test_react_graph_without_planner_has_no_plan() -> None:
    """A plain ReAct graph (no planner) leaves ``plan`` unset."""
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
            config={"configurable": {"thread_id": "react-t1"}},
        )
    assert result.get("plan") is None


# ---------------------------------------------------------------------------
# B-35 — plan_first planner variant (execution markers)
# ---------------------------------------------------------------------------


def test_parse_plan_object_steps_carry_execution() -> None:
    plan = parse_plan(
        '{"goal": "g", "steps": ['
        '{"description": "fetch A", "execution": "delegate"}, '
        '"decide and write"]}',
        fallback_goal="fb",
    )
    assert plan.steps[0].execution == "delegate"
    assert plan.steps[0].description == "fetch A"
    assert plan.steps[1].execution == "inline"


def test_parse_plan_bogus_execution_degrades_to_inline() -> None:
    plan = parse_plan(
        '{"goal": "g", "steps": [{"description": "a", "execution": "bogus"}]}',
        fallback_goal="fb",
    )
    assert plan.steps[0].execution == "inline"


def test_parse_plan_object_step_without_description_dropped() -> None:
    plan = parse_plan(
        '{"goal": "g", "steps": [{"execution": "delegate"}, "real"]}',
        fallback_goal="fb",
    )
    assert [s.description for s in plan.steps] == ["real"]


@pytest.mark.asyncio
async def test_plan_first_planner_prompt_asks_for_execution_markers() -> None:
    llm = _RecordingLLM(
        responses=[
            AIMessage(
                content='{"goal": "g", "steps": [{"description": "a", "execution": "delegate"}]}'
            )
        ]
    )
    node = make_planner_node(llm, plan_first=True)
    result = await node(_state("do the thing"), {"configurable": {"thread_id": "pf-1"}})
    system_text = str(llm.calls[0][0].content)
    assert "execution" in system_text
    assert "delegate" in system_text
    # 层 0 形状判据进 planner prompt。
    assert "independent" in system_text
    assert result["plan"].steps[0].execution == "delegate"


@pytest.mark.asyncio
async def test_default_planner_prompt_unchanged_without_plan_first() -> None:
    """standard 模式 planner prompt 零变化(不提 execution)。"""
    llm = _RecordingLLM(responses=[AIMessage(content='{"goal": "g", "steps": ["a"]}')])
    node = make_planner_node(llm)
    await node(_state("do the thing"), {"configurable": {"thread_id": "pf-2"}})
    system_text = str(llm.calls[0][0].content)
    assert "execution" not in system_text
    assert "delegate" not in system_text
