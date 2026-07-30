"""Reflect node — Stream J.2 (self-critique / self-correction).

When a manifest carries a ``reflection:`` block the factory inserts a
``reflect`` node between the agent and the run's end:

::

    agent ─(no tool_calls)→ reflect ─accept→ END
      ▲                        │
      └────────── revise ───────┘

Before the run would finish, ``reflect`` makes one LLM call that judges
whether the task is genuinely done. ``accept`` ends the run; ``revise``
appends the critique as feedback and routes the agent back to keep
working. A per-run ``budget`` caps the reflect↔agent loop.

For ``plan_execute`` graphs a ``revise`` verdict may also carry a fresh
set of steps — the node rewrites ``AgentState.plan`` so the agent
executes against the corrected plan (the J.1 replan path).

Reflection is *advisory self-correction* and orthogonal to the
``loop_detection`` middleware (mechanical-repetition guard). Any
unparseable reflection reply fails safe to ``accept`` so the loop is
always bounded (Mini-ADR J-5a).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from expert_work.common.observability import ExpertWorkComponent, expert_work_span
from expert_work.protocol import Plan, PlanStep, Reflection
from orchestrator.graph_builder._config import cancellation_token, current_run_id
from orchestrator.llm import LLMCaller
from orchestrator.state import AgentState

logger = logging.getLogger(__name__)

#: A reflect graph node: takes state + config, returns state updates.
ReflectNode = Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]

#: Per-message cap when rendering the trajectory for the reflect prompt.
_TRAJECTORY_CHAR_CAP = 1000

_REFLECT_SYSTEM = (
    "You are a reflection module. Given a task and the agent's trajectory "
    "so far, judge whether the task has been satisfactorily completed. Be "
    "strict — catch premature endings, wrong or incomplete answers, and "
    "skipped requirements. Respond with ONLY a JSON object, no prose and no "
    "code fences:\n"
    '{"verdict": "accept" | "revise", "critique": "<concise reasoning>"}\n'
    'Use "accept" only when the result genuinely satisfies the task. When a '
    "plan is shown and it has gone stale, additionally include "
    '"revised_steps": ["<step>", ...] with a corrected plan.'
)


def _message_text(message: BaseMessage) -> str:
    content = message.content
    return content if isinstance(content, str) else str(content)


def _extract_json_object(text: str) -> str | None:
    """Return the outermost ``{...}`` span of ``text``, or ``None``."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    return text[start : end + 1]


def _original_task(messages: list[BaseMessage]) -> str:
    """The user's original task — the *first* ``HumanMessage`` (later ones
    may be reflection feedback injected by an earlier ``revise``)."""
    for message in messages:
        if isinstance(message, HumanMessage):
            return _message_text(message)
    return _message_text(messages[0]) if messages else ""


def _render_trajectory(messages: list[BaseMessage]) -> str:
    lines: list[str] = []
    for message in messages:
        text = _message_text(message).strip()
        if len(text) > _TRAJECTORY_CHAR_CAP:
            text = text[:_TRAJECTORY_CHAR_CAP] + "...[truncated]"
        lines.append(f"[{message.type}] {text}")
    return "\n".join(lines)


def _build_reflect_prompt(messages: list[BaseMessage], plan: Plan | None) -> str:
    parts = [
        f"Task:\n{_original_task(messages)}",
        "",
        f"Trajectory so far:\n{_render_trajectory(messages)}",
    ]
    if plan is not None:
        plan_text = "\n".join(f"- {step.description}" for step in plan.steps)
        parts += ["", f"Current plan:\n{plan_text}"]
    parts += ["", "Has the task been satisfactorily completed?"]
    return "\n".join(parts)


def _parse_reflection(text: str, *, plan: Plan | None) -> tuple[Reflection, Plan | None]:
    """Parse the reflect LLM reply into a verdict + optional revised plan.

    Any malformed reply fails safe to ``accept`` so the reflect↔agent
    loop is always bounded (Mini-ADR J-5a).
    """
    raw = _extract_json_object(text)
    if raw is not None:
        try:
            data = json.loads(raw)
            verdict = str(data["verdict"]).strip().lower()
            critique = str(data.get("critique", "")).strip()
            if verdict in ("accept", "revise"):
                reflection = Reflection(
                    verdict=cast(Literal["accept", "revise"], verdict),
                    critique=critique or verdict,
                )
                revised_plan = None
                if verdict == "revise" and plan is not None:
                    revised_plan = _parse_revised_plan(data.get("revised_steps"), plan=plan)
                return reflection, revised_plan
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            # Malformed reply — fall through to the fail-safe accept below.
            pass
    logger.warning("reflect.parse_failed — accepting to keep the loop bounded")
    return Reflection(verdict="accept", critique="reflection reply was unparseable"), None


def _parse_revised_plan(steps_raw: object, *, plan: Plan) -> Plan | None:
    if not isinstance(steps_raw, list):
        return None
    steps: list[PlanStep] = []
    for index, raw_step in enumerate(steps_raw, start=1):
        description = str(raw_step).strip()
        if description:
            steps.append(PlanStep(id=str(index), description=description))
    if not steps:
        return None
    return Plan(goal=plan.goal, steps=tuple(steps))


def make_reflect_node(llm_caller: LLMCaller, *, budget: int, deadline_s: int = 30) -> ReflectNode:
    """Build the ``reflect`` graph node bound to ``llm_caller``.

    ``budget`` caps the reflection LLM calls per run; once reached the
    node force-accepts without calling the LLM so the run terminates.

    ``deadline_s`` (Stream K.K9) is a wall-clock cap on a single reflect
    LLM call. When the provider hangs past this many seconds the node
    fails safe to ``accept`` — orthogonal to the cancellation token,
    which only fires on client disconnect.
    """

    async def reflect_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        token = cancellation_token(config)
        token.raise_if_cancelled()

        # ``reflections`` accumulates across every run on a checkpointed
        # thread, so the budget counts only *this* run's reflections.
        run_id = current_run_id(config)
        this_run = sum(1 for r in state.get("reflections", []) if r.run_id == run_id)
        if this_run >= budget:
            return {
                "reflections": [
                    Reflection(
                        run_id=run_id,
                        verdict="accept",
                        critique="reflection budget exhausted",
                    )
                ]
            }

        messages = list(state["messages"])
        plan = state.get("plan")
        reflect_messages: list[BaseMessage] = [
            SystemMessage(content=_REFLECT_SYSTEM),
            HumanMessage(content=_build_reflect_prompt(messages, plan)),
        ]
        # Stream K.K9 — wrap the LLM call in ``asyncio.wait_for`` so a
        # provider that never returns can't lock the run. On timeout we
        # force-accept (same fail-safe shape as the unparseable-reply
        # path above): a hung reflection should not block the run from
        # ending.
        try:
            with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "reflect"):
                response = await asyncio.wait_for(
                    token.run_cancellable(llm_caller(messages=reflect_messages, tools=[])),
                    timeout=deadline_s,
                )
        except TimeoutError:
            logger.warning(
                "reflect.timeout deadline_s=%d — accepting to keep the run bounded",
                deadline_s,
            )
            return {
                "reflections": [
                    Reflection(
                        run_id=run_id,
                        verdict="accept",
                        critique=f"reflection timed out after {deadline_s}s",
                    )
                ]
            }
        parsed, revised_plan = _parse_reflection(_message_text(response), plan=plan)
        reflection = parsed.model_copy(update={"run_id": run_id})
        logger.info("reflect.verdict=%s", reflection.verdict)

        updates: dict[str, Any] = {"reflections": [reflection]}
        if reflection.verdict == "revise":
            updates["messages"] = [
                HumanMessage(
                    content=f"[Reflection] {reflection.critique}\n\n"
                    "Address the feedback above, then continue.",
                    # Orchestrator-authored scaffolding (RT-ADR-9): keep it
                    # in-prompt and in the faithful record, but out of the UI
                    # bubble view / GET /messages — an unhidden feedback row
                    # renders as a user message the user never sent.
                    additional_kwargs={"expert_work_hide_from_ui": True},
                )
            ]
            if revised_plan is not None:
                updates["plan"] = revised_plan
        return updates

    return reflect_node
