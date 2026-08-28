"""Plan / PlanStep model tests — B-35 ``execution`` marker compat."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from expert_work.protocol import Plan, PlanStep


def test_plan_step_execution_defaults_to_inline() -> None:
    step = PlanStep(id="1", description="read the file")
    assert step.execution == "inline"


def test_plan_step_legacy_payload_without_execution_parses() -> None:
    """B-35 前落盘的 checkpoint / SSE 载荷没有 ``execution`` 字段,必须照常读。"""
    plan = Plan.model_validate(
        {
            "goal": "g",
            "steps": [{"id": "1", "description": "d", "status": "in_progress"}],
        }
    )
    assert plan.steps[0].execution == "inline"
    assert plan.steps[0].status == "in_progress"


def test_plan_step_delegate_round_trips() -> None:
    step = PlanStep(id="1", description="d", execution="delegate")
    assert PlanStep.model_validate(step.model_dump()).execution == "delegate"


def test_plan_step_rejects_unknown_execution() -> None:
    with pytest.raises(ValidationError):
        PlanStep(id="1", description="d", execution="bogus")  # type: ignore[arg-type]
