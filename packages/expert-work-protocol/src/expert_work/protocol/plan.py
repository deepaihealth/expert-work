"""Agent planning models — Stream J.1 (task decomposition).

A :class:`Plan` is an ordered decomposition of the user's task produced
by the orchestrator's ``planner`` graph node before the ReAct loop runs
(``WorkflowSpec.type == "plan_execute"``). It is carried on
``AgentState.plan`` — checkpointed — and rendered into the agent's
system context so each ReAct step executes against it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Stream CM-0 (Mini-ADR CM-A5) — lifecycle of one plan step, surfaced as a
#: ``TODO.md`` checkbox by the workspace projection. Defaults to ``pending``
#: so existing planner output (which never set a status) is unaffected.
PlanStepStatus = Literal["pending", "in_progress", "completed"]

#: B-35 — how a step is meant to be executed under ``plan_first``:
#: ``delegate`` = dispatched to an ephemeral worker via ``spawn_worker``;
#: ``inline`` (default, and the only behavior outside plan_first) = the
#: main loop executes it itself. Defaulting keeps every pre-B-35
#: checkpoint / SSE payload parsing unchanged.
PlanStepExecution = Literal["delegate", "inline"]


class PlanStep(BaseModel):
    """One concrete step of a :class:`Plan`."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="stable step identifier, e.g. '1'")
    description: str = Field(description="what this step accomplishes")
    status: PlanStepStatus = Field(
        default="pending",
        description="Stream CM-0 — execution state, projected to TODO.md.",
    )
    execution: PlanStepExecution = Field(
        default="inline",
        description="B-35 — plan_first dispatch marker; inert outside plan_first.",
    )


class Plan(BaseModel):
    """An ordered task decomposition produced by the planner node."""

    model_config = ConfigDict(frozen=True)

    goal: str = Field(description="one-sentence restatement of the task")
    steps: tuple[PlanStep, ...] = Field(description="ordered steps to carry out")
