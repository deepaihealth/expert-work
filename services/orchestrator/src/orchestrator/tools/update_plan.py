"""``update_plan`` tool — Stream K.K8.

When the manifest's ``workflow.type`` is ``plan_execute`` the
``planner`` node (Stream J.1) writes an initial :class:`Plan` into
``AgentState`` once at the start of the run; ``agent_node`` then
renders that plan into the system context every step. Without an
in-run mutation entry the agent has no way to revise the plan when
execution diverges from it — STREAM-K-DESIGN § 3.K8 calls this the
"plan_execute closure gap".

This tool is the closure: the agent can call ``update_plan`` with a
fresh ordered list of steps; the tools node promotes the new
:class:`Plan` onto ``AgentState.plan`` via the
:data:`~orchestrator.tools.registry.TOOL_ALLOWED_STATE_KEYS` channel.
The reflect node's existing ``revise`` path (Stream J.2) already
covers reflective replans; ``update_plan`` adds the agent-initiated
path.

The tool is implicit — never declared in the manifest. Since P3 the
factory registers it for **every** agent (not just ``plan_execute``):
a react-mode agent calls it to *create* a plan on demand, a
plan_execute agent's planner node seeds an initial plan that this tool
then *replaces*.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from expert_work.protocol import Plan, PlanStep
from expert_work.protocol.plan import PlanStepExecution, PlanStepStatus
from orchestrator.tools.registry import ToolContext, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

#: Stream CM-0 (N1) — valid per-step statuses the agent may set so the plan
#: recitation reflects progress.
_VALID_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "completed"})
#: B-35 — per-step dispatch marker; inert outside plan_first.
_VALID_EXECUTIONS: frozenset[str] = frozenset({"delegate", "inline"})
_STATUS_BOX = {"pending": " ", "in_progress": "~", "completed": "x"}

#: Caps on plan size to keep the rendered system context bounded. The
#: J.1 planner uses the same shape — keep these in step with
#: ``planner.py``'s soft caps so the agent can't sneak past the limits
#: by going through ``update_plan``.
_MAX_STEPS: int = 20
_MAX_STEP_DESCRIPTION_CHARS: int = 500


@dataclass(frozen=True)
class UpdatePlanTool:
    """``update_plan(steps, reason, goal=None)`` — agent-initiated create-or-replace.

    Creates the run's :class:`Plan` if none exists yet, otherwise
    replaces it with a new ordered set of steps. The replacement is
    *complete* (not a patch) — modelling partial diffs would add a lot
    of surface for arguable gain. ``reason`` is captured for trace /
    audit only; it is not rendered back to the agent.
    """

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="update_plan",
            description=(
                "Create or replace your plan with an ordered list of steps. "
                "Use this when a task needs 3+ distinct steps or spans "
                "multiple tools; skip it for simple one-shot tasks. Mark "
                "steps completed / in_progress as you go so the recitation "
                "tracks progress."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "anyOf": [
                                {"type": "string"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "description": {"type": "string"},
                                        # B-34 — moonshot 严格校验:enum 节点必须显式
                                        # 声明 type,否则 kimi 400 拒掉整个请求(真栈
                                        # run 0de32ed5 实证)。
                                        "status": {
                                            "type": "string",
                                            "enum": ["pending", "in_progress", "completed"],
                                        },
                                        # B-35 — plan_first dispatch marker.
                                        "execution": {
                                            "type": "string",
                                            "enum": ["delegate", "inline"],
                                            "description": (
                                                "How the step should be executed: "
                                                "'delegate' = hand it to an ephemeral "
                                                "worker (spawn_worker), 'inline' "
                                                "(default) = do it yourself."
                                            ),
                                        },
                                    },
                                    "required": ["description"],
                                },
                            ]
                        },
                        "minItems": 1,
                        "maxItems": _MAX_STEPS,
                        "description": (
                            "Ordered list of steps for the plan. Each "
                            "entry is either a short imperative description "
                            "string, or an object {description, status} where "
                            "status is pending / in_progress / completed — use "
                            "the object form to mark progress as you go."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Why the plan is being revised — recorded for "
                            "the trace, not fed back to the agent."
                        ),
                    },
                    "goal": {
                        "type": "string",
                        "description": (
                            "One-sentence restatement of what the plan "
                            "achieves. Provide it when first creating a plan; "
                            "on a later revise it is optional — the existing "
                            "goal is kept unless you pass a new one."
                        ),
                    },
                },
                "required": ["steps", "reason"],
            },
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        # P3 create-or-replace — a react-mode run reaches this tool with no
        # seeded plan (``ctx.plan is None``); the first call *creates* the
        # plan instead of raising. plan_execute runs still arrive with the
        # planner node's initial plan and this call *replaces* it.
        steps_raw = args.get("steps")
        reason = str(args.get("reason", "")).strip()
        goal_arg = str(args.get("goal", "")).strip()

        if not isinstance(steps_raw, list) or not steps_raw:
            msg = "update_plan requires a non-empty 'steps' array"
            raise ValueError(msg)
        if not reason:
            msg = "update_plan requires a non-empty 'reason' string"
            raise ValueError(msg)

        # Trim each step + drop empties. The schema's minItems=1 already
        # rejects an empty array, but a list of empty strings would
        # produce an unusable plan; surface it as a value error so the
        # LLM gets feedback rather than the run silently accepting a
        # blank plan.
        cleaned: list[PlanStep] = []
        for index, raw_step in enumerate(steps_raw, start=1):
            # Stream CM-0 (N1) — a step is either a bare description string
            # (status defaults to pending) or a {description, status} object so
            # the agent can mark progress; an invalid status falls back to
            # pending rather than rejecting the whole replan.
            status: PlanStepStatus = "pending"
            execution: PlanStepExecution = "inline"
            if isinstance(raw_step, Mapping):
                description = str(raw_step.get("description", "")).strip()
                if raw_step.get("status") in _VALID_STATUSES:
                    status = cast(PlanStepStatus, raw_step["status"])
                # B-35 — same philosophy as status: a bogus value degrades
                # to the default instead of rejecting the whole replan.
                if raw_step.get("execution") in _VALID_EXECUTIONS:
                    execution = cast(PlanStepExecution, raw_step["execution"])
            else:
                description = str(raw_step).strip()
            if not description:
                continue
            if len(description) > _MAX_STEP_DESCRIPTION_CHARS:
                description = description[:_MAX_STEP_DESCRIPTION_CHARS] + "…"
            cleaned.append(
                PlanStep(id=str(index), description=description, status=status, execution=execution)
            )

        if not cleaned:
            msg = "update_plan requires at least one non-empty step description"
            raise ValueError(msg)
        if len(cleaned) > _MAX_STEPS:
            cleaned = cleaned[:_MAX_STEPS]

        # Goal source (P3): an explicit ``goal`` arg wins (create, or rename
        # on revise); else keep the existing plan's goal (revise); else — a
        # create with no goal — fall back to the first step so the
        # recitation's ``Goal:`` line is never blank.
        if goal_arg:
            goal = goal_arg
        elif ctx.plan is not None:
            goal = ctx.plan.goal
        else:
            goal = cleaned[0].description
        new_plan = Plan(goal=goal, steps=tuple(cleaned))
        logger.info("update_plan.applied n_steps=%d reason=%r", len(cleaned), reason)

        rendered = "\n".join(
            f"- [{_STATUS_BOX.get(step.status, ' ')}] {step.id}. {step.description}"
            for step in cleaned
        )
        # Stream L.L5 — internal-chain housekeeping. update_plan is a
        # tool the agent calls *between* real progress steps; charging
        # it against the user-visible iteration budget makes
        # plan_execute agents starve themselves on every revise. Refund
        # exactly one iteration so the budget reflects user-visible
        # work, not implementation detail. Mini-ADR L-5.
        return ToolResult(
            content=f"Plan revised to {len(cleaned)} step(s):\n{rendered}",
            meta={"n_steps": len(cleaned), "reason": reason},
            state_updates={"plan": new_plan},
            refund_iterations=1,
        )
