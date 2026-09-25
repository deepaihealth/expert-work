"""Stream CM-9 — limit-hit effort escalation (Mini-ADR CM-J4/J5).

The agent node serves a turn from the pre-built higher-effort caller
when either signal fires: the loop-detection middleware tripped on the
previous response (``escalate_next``), or the step budget crossed 75%
of ``max_steps``. No escalated caller wired → byte-for-byte the
existing path.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.protocol import ModelSpec, Plan
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.middleware import LoopDetectionMiddleware, MiddlewareChain
from orchestrator import AgentState, GraphRunner, ToolRegistry, build_react_graph
from orchestrator.agent_factory import _escalated_model
from orchestrator.tools.error_classifier import ClassifiedToolError
from orchestrator.tools.registry import ToolContext, ToolResult, ToolSpec


@dataclass
class _CountingLLM:
    """Scripted caller that records how many calls it served."""

    responses: list[AIMessage]
    label: str = "base"
    calls: int = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


@dataclass
class _EchoTool:
    name: str = "probe"
    dispatched: int = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="probe")

    async def call(self, args: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        self.dispatched += 1
        return ToolResult(content="ok")


def _tc(call_id: str) -> dict[str, Any]:
    return {"name": "probe", "args": {"q": "same"}, "id": call_id, "type": "tool_call"}


async def _invoke(graph, payload: dict[str, Any], thread_id: str) -> AgentState:
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        return await compiled.ainvoke(payload, config=cfg)


@pytest.mark.asyncio
async def test_budget_signal_switches_to_escalated_caller() -> None:
    base = _CountingLLM(responses=[AIMessage(content="base done")])
    escalated = _CountingLLM(responses=[AIMessage(content="escalated done")], label="esc")
    graph = build_react_graph(
        llm_caller=base,
        escalated_llm_caller=escalated,
        tool_registry=ToolRegistry(),
    )
    # step_count 3 of max_steps 4 → 3*4 >= 4*3 → budget signal fires.
    state = await _invoke(
        graph,
        {"messages": [HumanMessage(content="go")], "step_count": 3, "max_steps": 4},
        "esc-budget",
    )
    assert escalated.calls == 1
    assert base.calls == 0
    assert str(state["messages"][-1].content) == "escalated done"


@pytest.mark.asyncio
async def test_loop_trip_arms_escalation_for_the_next_run() -> None:
    tool = _EchoTool()
    registry = ToolRegistry()
    registry.register(tool)
    # Three identical tool calls trip the loop middleware on the 3rd
    # response — it clears the tool_calls, so the run ends with the
    # reminder and ``escalate_next`` armed in the checkpoint. The NEXT
    # run's first agent step serves from the escalated caller.
    base = _CountingLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("a")]),
            AIMessage(content="", tool_calls=[_tc("b")]),
            AIMessage(content="", tool_calls=[_tc("c")]),
        ]
    )
    escalated = _CountingLLM(responses=[AIMessage(content="escalated final")], label="esc")
    graph = build_react_graph(
        llm_caller=base,
        escalated_llm_caller=escalated,
        tool_registry=registry,
        after_llm_chain=MiddlewareChain.from_middlewares(
            "after_llm_call", [LoopDetectionMiddleware()]
        ),
    )
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {"configurable": {"thread_id": "esc-loop"}}
        first = await compiled.ainvoke(
            {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 20},
            config=cfg,
        )
        # Loop tripped: run ended on the reminder with the signal armed.
        assert first.get("escalate_next") is True
        assert escalated.calls == 0

        second = await compiled.ainvoke(
            {"messages": [HumanMessage(content="try again")], "step_count": 0},
            config=cfg,
        )
    assert escalated.calls == 1
    assert str(second["messages"][-1].content) == "escalated final"
    # Consumed signal resets (the escalated turn had no loop trip).
    assert second.get("escalate_next") is False


@pytest.mark.asyncio
async def test_no_escalated_caller_keeps_base_path() -> None:
    base = _CountingLLM(responses=[AIMessage(content="done")])
    graph = build_react_graph(llm_caller=base, tool_registry=ToolRegistry())
    state = await _invoke(
        graph,
        {"messages": [HumanMessage(content="go")], "step_count": 3, "max_steps": 4},
        "esc-none",
    )
    assert base.calls == 1
    assert str(state["messages"][-1].content) == "done"


# ---------------------------------------------------------------------------
# CM-11 — event-driven escalation: tool error (micro) + plan goal change
# (macro). Dynamic compute allocation — a deep think on a real anomaly or a
# strategy shift, base caller on deterministic steps.
# ---------------------------------------------------------------------------


def _failure(error_class: str, *, tool_name: str = "edit_file") -> ClassifiedToolError:
    return ClassifiedToolError(
        tool_name=tool_name,
        error_class=error_class,  # type: ignore[arg-type]
        summary="boom",
        retryable=error_class == "transient",
        advice="recover",
    )


@pytest.mark.asyncio
async def test_non_transient_tool_error_escalates_this_turn() -> None:
    base = _CountingLLM(responses=[AIMessage(content="base done")])
    escalated = _CountingLLM(responses=[AIMessage(content="escalated done")], label="esc")
    graph = build_react_graph(
        llm_caller=base,
        escalated_llm_caller=escalated,
        tool_registry=ToolRegistry(),
    )
    # A non-transient failure from the previous batch → reason deeply about
    # it this very turn (the recovery advisory lands this turn too).
    state = await _invoke(
        graph,
        {
            "messages": [HumanMessage(content="go")],
            "step_count": 0,
            "max_steps": 20,
            "tool_failures": [_failure("resource_not_found")],
        },
        "esc-tool-error",
    )
    assert escalated.calls == 1
    assert base.calls == 0
    assert str(state["messages"][-1].content) == "escalated done"


@pytest.mark.asyncio
async def test_transient_tool_error_keeps_base_path() -> None:
    base = _CountingLLM(responses=[AIMessage(content="base done")])
    escalated = _CountingLLM(responses=[AIMessage(content="escalated done")], label="esc")
    graph = build_react_graph(
        llm_caller=base,
        escalated_llm_caller=escalated,
        tool_registry=ToolRegistry(),
    )
    # ``transient`` is retryable jitter, not worth a deep think.
    await _invoke(
        graph,
        {
            "messages": [HumanMessage(content="go")],
            "step_count": 0,
            "max_steps": 20,
            "tool_failures": [_failure("transient", tool_name="bash")],
        },
        "esc-tool-transient",
    )
    assert base.calls == 1
    assert escalated.calls == 0


@pytest.mark.asyncio
async def test_plan_goal_change_escalates_and_rebaselines() -> None:
    base = _CountingLLM(responses=[AIMessage(content="base done")])
    escalated = _CountingLLM(responses=[AIMessage(content="escalated done")], label="esc")
    graph = build_react_graph(
        llm_caller=base,
        escalated_llm_caller=escalated,
        tool_registry=ToolRegistry(),
    )
    state = await _invoke(
        graph,
        {
            "messages": [HumanMessage(content="go")],
            "step_count": 0,
            "max_steps": 20,
            "plan": Plan(goal="rewrite in Rust", steps=()),
            "last_plan_goal": "rewrite in Go",
        },
        "esc-goal-change",
    )
    assert escalated.calls == 1
    assert base.calls == 0
    # The new goal becomes the baseline so the next turn does not re-fire.
    assert state.get("last_plan_goal") == "rewrite in Rust"


@pytest.mark.asyncio
async def test_initial_plan_does_not_escalate() -> None:
    base = _CountingLLM(responses=[AIMessage(content="base done")])
    escalated = _CountingLLM(responses=[AIMessage(content="escalated done")], label="esc")
    graph = build_react_graph(
        llm_caller=base,
        escalated_llm_caller=escalated,
        tool_registry=ToolRegistry(),
    )
    # First plan turn: no prior goal to diff against → the planner already
    # did the deep decomposition, so the base caller serves.
    state = await _invoke(
        graph,
        {
            "messages": [HumanMessage(content="go")],
            "step_count": 0,
            "max_steps": 20,
            "plan": Plan(goal="rewrite in Go", steps=()),
        },
        "esc-initial-plan",
    )
    assert base.calls == 1
    assert escalated.calls == 0
    assert state.get("last_plan_goal") == "rewrite in Go"


# ---------------------------------------------------------------------------
# factory — escalated ModelSpec derivation (CM-J4)
# ---------------------------------------------------------------------------


def _model(**overrides: Any) -> ModelSpec:
    return ModelSpec.model_validate(
        {"provider": "anthropic", "name": "claude-sonnet-4-6", **overrides}
    )


def test_escalation_disabled_when_compute_controls_untouched() -> None:
    assert _escalated_model(_model()) is None


def test_escalation_ladder_steps_one_level() -> None:
    assert _escalated_model(_model(adaptive_thinking=True)).effort == "medium"
    assert _escalated_model(_model(effort="low")).effort == "medium"
    assert _escalated_model(_model(effort="medium")).effort == "high"
    assert _escalated_model(_model(effort="high")).effort == "max"


def test_escalation_capped_at_max() -> None:
    assert _escalated_model(_model(effort="max")) is None


def test_escalation_requires_catalog_effort_support() -> None:
    assert _escalated_model(_model(name="claude-haiku-4-5", adaptive_thinking=True)) is None
    # Off-catalog models are not escalated either (capability unknown).
    assert _escalated_model(_model(name="claude-custom-gw", effort="low")) is None


# ---------------------------------------------------------------------------
# CM-10 — cross-vendor escalation (Mini-ADR CM-L6)
# ---------------------------------------------------------------------------


def _vendor(provider: str, name: str, **overrides: Any) -> ModelSpec:
    return ModelSpec.model_validate({"provider": provider, "name": name, **overrides})


def test_escalation_ladder_applies_to_effort_and_budget_vendors() -> None:
    openai = _escalated_model(_vendor("openai", "gpt-5.5", effort="low"))
    assert openai is not None and openai.effort == "medium"
    qwen = _escalated_model(_vendor("qwen", "qwen3.7-max", effort="high"))
    assert qwen is not None and qwen.effort == "max"
    doubao = _escalated_model(_vendor("doubao", "doubao-seed-2.0-pro", adaptive_thinking=True))
    assert doubao is not None and doubao.effort == "medium"
    # Untouched manifests on ladder vendors stay off (CM-9 conservative default).
    assert _escalated_model(_vendor("qwen", "qwen3.7-max")) is None


def test_toggle_vendors_escalate_by_turning_thinking_on() -> None:
    # Untouched manifest -> one hop: enable thinking.
    glm = _escalated_model(_vendor("glm", "glm-5.1"))
    assert glm is not None and glm.effort == "high"
    # Already thinking -> nowhere to go.
    assert _escalated_model(_vendor("glm", "glm-5.1", effort="low")) is None
    assert _escalated_model(_vendor("kimi", "kimi-k2.6", adaptive_thinking=True)) is None


def test_no_thinking_control_models_never_escalate() -> None:
    assert _escalated_model(_vendor("deepseek", "deepseek-reasoner", effort="low")) is None
    assert _escalated_model(_vendor("qwen", "custom-gateway", effort="low")) is None


# ---------------------------------------------------------------------------
# Thinking-Toggle (req #3) — a user-disabled toggle STILL escalates to ON
# for one turn (ephemeral; the stored manifest is untouched).
# ---------------------------------------------------------------------------


def test_disabled_toggle_still_escalates_to_on() -> None:
    # effort vendor (anthropic): off -> on, effort None -> "medium".
    claude = _escalated_model(_model(thinking_enabled=False))
    assert claude is not None and claude.thinking_enabled is True and claude.effort == "medium"
    # effort vendor (compat) + budget vendor: same one-step-up enable.
    openai = _escalated_model(_vendor("openai", "gpt-5.5", thinking_enabled=False))
    assert openai is not None and openai.thinking_enabled is True and openai.effort == "medium"
    qwen = _escalated_model(_vendor("qwen", "qwen3.7-max", thinking_enabled=False))
    assert qwen is not None and qwen.thinking_enabled is True and qwen.effort == "medium"
    # toggle vendor: off -> on ("high" collapses to enabled at translation).
    glm = _escalated_model(_vendor("glm", "glm-5.1", thinking_enabled=False))
    assert glm is not None and glm.thinking_enabled is True and glm.effort == "high"
    # no-knob model never escalates, even if (invalidly) marked disabled.
    assert (
        _escalated_model(_vendor("deepseek", "deepseek-reasoner", thinking_enabled=False)) is None
    )


def test_split_model_with_cap_and_derived_thinking_does_not_escalate() -> None:
    """B-105 —— split(qwen3-max / qwen3-vl-*)设了输出上限、没设思考上限时,思考预算从
    上限按档位推;升档只会把回答挤小(10000 从 high 升 max,回答剩 500),所以不升。"""
    for name in ("qwen3-max", "qwen3-vl-plus", "qwen3-vl-flash"):
        assert _escalated_model(_vendor("qwen", name, max_tokens=10_000, effort="high")) is None
        # 关着思考的也不升(升档 = 开思考,同样从上限里切预算)。
        assert (
            _escalated_model(
                _vendor("qwen", name, max_tokens=10_000, thinking_enabled=False, effort="low")
            )
            is None
        )
    # 显式思考上限:预算固定,升档不挤回答,照常升。
    explicit = _escalated_model(
        _vendor("qwen", "qwen3-max", max_tokens=10_000, thinking_max_tokens=2000, effort="high")
    )
    assert explicit is not None and explicit.effort == "max"
    # 没设输出上限:没有可挤的合计,照常升。
    uncapped = _escalated_model(_vendor("qwen", "qwen3-max", effort="high"))
    assert uncapped is not None and uncapped.effort == "max"
    # 非 split 的通义模型不受影响。
    total = _escalated_model(_vendor("qwen", "qwen3.7-max", max_tokens=10_000, effort="high"))
    assert total is not None and total.effort == "max"
