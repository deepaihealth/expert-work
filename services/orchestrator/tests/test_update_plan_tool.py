"""Unit tests for the ``update_plan`` tool — Stream K.K8."""

from __future__ import annotations

import pytest

from expert_work.protocol import Plan, PlanStep
from orchestrator.tools.registry import ToolContext
from orchestrator.tools.update_plan import UpdatePlanTool

_INITIAL_PLAN = Plan(
    goal="Ship the dogfood agent",
    steps=(
        PlanStep(id="1", description="Read the manifest"),
        PlanStep(id="2", description="Run the planner"),
    ),
)


def _ctx_with_plan(plan: Plan | None = _INITIAL_PLAN) -> ToolContext:
    """Build a context that mimics what ``tools_node`` injects for a
    plan_execute workflow."""
    return ToolContext(plan=plan)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_plan_replaces_steps_and_keeps_original_goal() -> None:
    """The agent supplies new steps; ``ctx.plan.goal`` is preserved."""
    tool = UpdatePlanTool()

    result = await tool.call(
        {
            "steps": [
                "Catch up on the new spec",
                "Generate a fresh implementation",
                "Re-run the validation suite",
            ],
            "reason": "Initial plan diverged after spec was revised mid-run",
        },
        ctx=_ctx_with_plan(),
    )

    # state_updates carries the new Plan onto the K.K8 allowlisted channel.
    new_plan = result.state_updates["plan"]
    assert isinstance(new_plan, Plan)
    assert new_plan.goal == _INITIAL_PLAN.goal  # goal preserved
    assert [s.description for s in new_plan.steps] == [
        "Catch up on the new spec",
        "Generate a fresh implementation",
        "Re-run the validation suite",
    ]
    # Step ids are renumbered 1..n so the agent's system context render
    # stays clean after a revise.
    assert [s.id for s in new_plan.steps] == ["1", "2", "3"]

    # The content / meta surface back to the LLM so it can confirm.
    assert "3 step(s)" in result.content
    assert result.meta["n_steps"] == 3
    assert "diverged" in result.meta["reason"]


# ---------------------------------------------------------------------------
# create-or-replace: no existing plan seeds one (P3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_plan_seeds_a_new_plan_when_context_has_none() -> None:
    """P3 create-or-replace: a react-mode run has no seeded plan; the first
    ``update_plan`` call creates one from the supplied goal + steps rather
    than raising."""
    tool = UpdatePlanTool()
    result = await tool.call(
        {
            "steps": ["Scope the change", "Write the test", "Implement"],
            "reason": "Task turned out to need multiple steps",
            "goal": "Add adaptive planning to the agent",
        },
        ctx=_ctx_with_plan(plan=None),
    )
    new_plan = result.state_updates["plan"]
    assert isinstance(new_plan, Plan)
    assert new_plan.goal == "Add adaptive planning to the agent"
    assert [s.description for s in new_plan.steps] == [
        "Scope the change",
        "Write the test",
        "Implement",
    ]
    assert [s.id for s in new_plan.steps] == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_update_plan_create_without_goal_falls_back_to_first_step() -> None:
    """Create path with no explicit goal: fall back to the first step's
    description so the recitation's ``Goal:`` line is never blank."""
    tool = UpdatePlanTool()
    result = await tool.call(
        {"steps": ["Investigate the bug", "Fix it"], "reason": "multi-step"},
        ctx=_ctx_with_plan(plan=None),
    )
    assert result.state_updates["plan"].goal == "Investigate the bug"


@pytest.mark.asyncio
async def test_update_plan_explicit_goal_overrides_existing_goal() -> None:
    """Revise path: an explicit ``goal`` renames the plan; without it the
    existing goal is preserved (covered by
    test_update_plan_replaces_steps_and_keeps_original_goal)."""
    tool = UpdatePlanTool()
    result = await tool.call(
        {"steps": ["redo"], "reason": "pivot", "goal": "Pursue the new direction"},
        ctx=_ctx_with_plan(),  # _INITIAL_PLAN present
    )
    assert result.state_updates["plan"].goal == "Pursue the new direction"


@pytest.mark.asyncio
async def test_update_plan_description_carries_complexity_guidance() -> None:
    """guidance-in-schema-only: the model learns *when* to plan from the
    tool description, not a system-prompt mutation."""
    spec = UpdatePlanTool().spec
    assert "3+ " in spec.description
    assert "goal" in spec.parameters["properties"]
    assert spec.parameters["required"] == ["steps", "reason"]


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_plan_rejects_empty_steps_array() -> None:
    tool = UpdatePlanTool()
    with pytest.raises(ValueError, match="non-empty 'steps' array"):
        await tool.call({"steps": [], "reason": "x"}, ctx=_ctx_with_plan())


@pytest.mark.asyncio
async def test_update_plan_rejects_missing_reason() -> None:
    tool = UpdatePlanTool()
    with pytest.raises(ValueError, match="non-empty 'reason'"):
        await tool.call({"steps": ["a"], "reason": "   "}, ctx=_ctx_with_plan())


@pytest.mark.asyncio
async def test_update_plan_rejects_all_blank_step_descriptions() -> None:
    """``minItems`` in the schema doesn't catch a list of empty strings,
    so the tool itself must surface the error so the LLM sees feedback."""
    tool = UpdatePlanTool()
    with pytest.raises(ValueError, match="at least one non-empty step"):
        await tool.call(
            {"steps": ["   ", "\n"], "reason": "broken plan"},
            ctx=_ctx_with_plan(),
        )


@pytest.mark.asyncio
async def test_update_plan_trims_step_descriptions() -> None:
    tool = UpdatePlanTool()
    result = await tool.call(
        {"steps": ["  do thing  ", "next"], "reason": "tidy"},
        ctx=_ctx_with_plan(),
    )
    plan = result.state_updates["plan"]
    assert plan.steps[0].description == "do thing"


# ---------------------------------------------------------------------------
# Stream CM-0 (N1) — per-step status so the agent can mark progress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_plan_accepts_object_steps_with_status() -> None:
    """Object-form steps let the agent set status; the recitation reflects it."""
    tool = UpdatePlanTool()
    result = await tool.call(
        {
            "steps": [
                {"description": "read the spec", "status": "completed"},
                {"description": "implement", "status": "in_progress"},
                {"description": "review"},  # no status → pending
            ],
            "reason": "mark progress",
        },
        ctx=_ctx_with_plan(),
    )
    new_plan = result.state_updates["plan"]
    assert [s.status for s in new_plan.steps] == ["completed", "in_progress", "pending"]


@pytest.mark.asyncio
async def test_update_plan_string_steps_default_to_pending() -> None:
    """Backward compatible: bare string steps stay pending."""
    tool = UpdatePlanTool()
    result = await tool.call({"steps": ["one", "two"], "reason": "plain"}, ctx=_ctx_with_plan())
    new_plan = result.state_updates["plan"]
    assert all(s.status == "pending" for s in new_plan.steps)


@pytest.mark.asyncio
async def test_update_plan_invalid_status_falls_back_to_pending() -> None:
    """A bogus status does not reject the replan — it degrades to pending."""
    tool = UpdatePlanTool()
    result = await tool.call(
        {"steps": [{"description": "x", "status": "bogus"}], "reason": "r"},
        ctx=_ctx_with_plan(),
    )
    assert result.state_updates["plan"].steps[0].status == "pending"


# ---------------------------------------------------------------------------
# B-35 — per-step execution marker (delegate | inline)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_plan_accepts_execution_marker() -> None:
    tool = UpdatePlanTool()
    result = await tool.call(
        {
            "steps": [
                {"description": "fetch template details", "execution": "delegate"},
                {"description": "decide and write", "execution": "inline"},
                "aggregate results",
            ],
            "reason": "mark dispatch",
        },
        ctx=_ctx_with_plan(),
    )
    steps = result.state_updates["plan"].steps
    assert steps[0].execution == "delegate"
    assert steps[1].execution == "inline"
    assert steps[2].execution == "inline"


@pytest.mark.asyncio
async def test_update_plan_invalid_execution_falls_back_to_inline() -> None:
    """与 status 同哲学:坏值降级,不拒掉整次替换。"""
    tool = UpdatePlanTool()
    result = await tool.call(
        {"steps": [{"description": "x", "execution": "bogus"}], "reason": "r"},
        ctx=_ctx_with_plan(),
    )
    assert result.state_updates["plan"].steps[0].execution == "inline"


def test_update_plan_schema_execution_enum_declares_type() -> None:
    """B-34 —— moonshot 严格校验:enum 节点必须显式带 type。"""
    params = UpdatePlanTool().spec.parameters
    obj_variant = params["properties"]["steps"]["items"]["anyOf"][1]
    execution = obj_variant["properties"]["execution"]
    assert execution["type"] == "string"
    assert set(execution["enum"]) == {"delegate", "inline"}
