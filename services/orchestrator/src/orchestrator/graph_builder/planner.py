"""Planner node — Stream J.1 (task decomposition).

When a manifest declares ``workflow.type == "plan_execute"`` the
factory front-loads the ReAct loop with a ``planner`` node:

::

    START → planner → agent ⇄ tools → END

The planner makes one LLM call that decomposes the user's task into an
ordered :class:`~expert_work.protocol.Plan`, stores it on
``AgentState.plan``, and the agent node renders it into its system
context every step (see :func:`render_plan` /
``orchestrator.graph_builder.builder``).

The plan is *advisory* — the ReAct loop still drives execution. Mid-run
revision (``update_plan`` / reflection-triggered replan) is a Stream J
follow-up and not part of this node.

Plan extraction is tolerant: the LLM is asked for a bare JSON object,
but prose / code fences around it are stripped, and any parse failure
degrades to a single-step plan wrapping the whole task so the run
always proceeds (Mini-ADR J-3a).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from expert_work.common.observability import ExpertWorkComponent, expert_work_span
from expert_work.protocol import Plan, PlanStep
from orchestrator.graph_builder._config import cancellation_token
from orchestrator.llm import LLMCaller
from orchestrator.state import AgentState

logger = logging.getLogger(__name__)

#: A planner graph node: takes state + config, returns the ``plan`` channel.
PlannerNode = Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]

_PLANNER_SYSTEM = (
    "You are a planning module. Break the given task into a short, ordered "
    "list of concrete steps that an agent will then execute. Keep the plan "
    "minimal — only the steps genuinely needed, no filler. Respond with "
    "ONLY a JSON object, no prose and no code fences:\n"
    '{"goal": "<one-sentence restatement of the task>", '
    '"steps": ["<step 1>", "<step 2>", ...]}'
)

#: B-35 — plan_first variant: each step additionally carries an
#: ``execution`` marker. The delegate criteria mirror the 层 0 shape
#: criteria in the ``spawn_worker`` tool description (domain-free).
_PLANNER_SYSTEM_PLAN_FIRST = (
    "You are a planning module. Break the given task into a short, ordered "
    "list of concrete steps that an agent will then execute. Keep the plan "
    "minimal — only the steps genuinely needed, no filler.\n"
    "Mark each step with how it should be executed:\n"
    '- "delegate" — the step fits one of these shapes, regardless of domain: '
    "three or more similar, mutually independent sub-items; reading long "
    "materials in full where only the conclusions matter; exploratory "
    "search in a large body of content. Delegate-marked steps are handed "
    "to parallel worker sub-agents.\n"
    '- "inline" — everything else, especially small single-step work, '
    "writes, and final decisions (those stay with the main agent).\n"
    "Respond with ONLY a JSON object, no prose and no code fences:\n"
    '{"goal": "<one-sentence restatement of the task>", '
    '"steps": [{"description": "<step 1>", "execution": "delegate"}, '
    '{"description": "<step 2>", "execution": "inline"}, ...]}'
)

_PLANNER_USER = "Task:\n{task}"


def _message_text(message: BaseMessage) -> str:
    """Best-effort plain-text of a message — content may be a str or a
    list of multimodal blocks."""
    content = message.content
    return content if isinstance(content, str) else str(content)


def _extract_task(messages: list[BaseMessage]) -> str:
    """The user's task — the most recent ``HumanMessage`` in the history."""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return _message_text(message)
    return _message_text(messages[-1]) if messages else ""


def _extract_json_object(text: str) -> str | None:
    """Return the outermost ``{...}`` span of ``text``, or ``None``.

    Tolerates prose and ```json fences around the object — the planner
    prompt asks for a bare object but models add wrappers anyway.
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    return text[start : end + 1]


def parse_plan(text: str, *, fallback_goal: str) -> Plan:
    """Parse the planner LLM's reply into a :class:`Plan`.

    Any malformed reply degrades to a single-step plan wrapping
    ``fallback_goal`` so the run always proceeds (Mini-ADR J-3a).
    """
    raw = _extract_json_object(text)
    if raw is not None:
        try:
            data = json.loads(raw)
            goal = str(data["goal"]).strip()
            steps = tuple(
                step
                for step in (
                    _parse_step(index, raw_step)
                    for index, raw_step in enumerate(data["steps"], start=1)
                )
                if step is not None
            )
            if goal and steps:
                return Plan(goal=goal, steps=steps)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("planner.parse_failed — falling back to single-step plan")
    else:
        logger.warning("planner.no_json — falling back to single-step plan")
    return Plan(
        goal=fallback_goal,
        steps=(PlanStep(id="1", description=fallback_goal),),
    )


def _parse_step(index: int, raw_step: Any) -> PlanStep | None:
    """One planner step — a bare description string, or (B-35 plan_first)
    a ``{description, execution}`` object. A bogus ``execution`` degrades
    to ``inline`` (same tolerance philosophy as the rest of the parse);
    a blank description drops the step."""
    execution = "inline"
    if isinstance(raw_step, dict):
        description = str(raw_step.get("description", "")).strip()
        if raw_step.get("execution") in ("delegate", "inline"):
            execution = str(raw_step["execution"])
    else:
        description = str(raw_step).strip()
    if not description:
        return None
    return PlanStep(
        id=str(index),
        description=description,
        execution="delegate" if execution == "delegate" else "inline",
    )


#: Stream CM-0 (N1) — step status → checkbox, so the per-turn recitation
#: shows progress and the model's attention focuses on what is still pending.
_STATUS_BOX = {"pending": " ", "in_progress": "~", "completed": "x"}


def render_plan(plan: Plan) -> str:
    """Render a :class:`Plan` as the tail-recitation block the agent reads each
    turn (Stream J.1 + CM-0 N1). The status checkbox (``[ ]`` / ``[~]`` /
    ``[x]``) keeps progress in recent attention so a long run does not lose
    track of what is already done vs. still pending."""
    lines = ["## Execution plan", f"Goal: {plan.goal}", ""]
    lines.extend(
        f"- [{_STATUS_BOX.get(step.status, ' ')}] {index}. {step.description}"
        for index, step in enumerate(plan.steps, start=1)
    )
    lines.append("")
    lines.append(
        "Work through this plan step by step, marking steps done as you "
        "complete them; adapt it if you discover something that requires a "
        "different approach."
    )
    return "\n".join(lines)


def make_planner_node(llm_caller: LLMCaller, *, plan_first: bool = False) -> PlannerNode:
    """Build the ``planner`` graph node bound to ``llm_caller``.

    ``plan_first`` (B-35) switches to the prompt variant that asks for a
    per-step ``execution`` marker; ``False`` (default) keeps the original
    prompt byte-identical.
    """

    async def planner_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        token = cancellation_token(config)
        token.raise_if_cancelled()

        task = _extract_task(list(state["messages"]))
        plan_messages: list[BaseMessage] = [
            SystemMessage(content=_PLANNER_SYSTEM_PLAN_FIRST if plan_first else _PLANNER_SYSTEM),
            HumanMessage(content=_PLANNER_USER.format(task=task)),
        ]
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "planner"):
            response = await token.run_cancellable(llm_caller(messages=plan_messages, tools=[]))
        plan = parse_plan(_message_text(response), fallback_goal=task)
        logger.info("planner.plan_built steps=%d", len(plan.steps))
        return {"plan": plan}

    return planner_node
