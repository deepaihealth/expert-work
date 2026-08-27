"""动态子智能体委派增强(层 1)— plan-driven delegation nudge tests.

After ``tools_node`` processes an ``update_plan`` call (plan created or
replaced), the harness injects ONE hidden ``[system reminder]``
HumanMessage nudging the agent to consider ``spawn_worker`` — iff the
registry carries ``spawn_worker`` AND the new plan has >= 2 non-completed
steps (degraded criterion: ``PlanStep`` has no parallel-safe field).

Dedupe: one nudge per plan identity (goal + step descriptions, statuses
excluded) — a replan with the same steps, or a pure progress-marking
update, never re-nudges; a structurally new plan may.

Agents without ``spawn_worker`` see strictly zero behaviour change.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    AgentState,
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)
from orchestrator.tools.spawn_worker import SPAWN_WORKER_TOOL_NAME
from orchestrator.tools.update_plan import UpdatePlanTool

_NUDGE_MARKER = "[system reminder] The current plan contains"

# ---------------------------------------------------------------------------
# Test helpers (mirror test_step_count_refund.py)
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedLLM:
    responses: list[AIMessage]
    calls: int = 0

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
    ) -> AIMessage:
        idx = self.calls
        self.calls += 1
        if idx >= len(self.responses):
            raise RuntimeError(f"scripted LLM ran out at call {idx}")
        return self.responses[idx]


@dataclass
class _StubTool:
    """Registry filler — a no-op tool under an arbitrary name (used to put
    ``spawn_worker``'s NAME into the registry without its full config)."""

    name: str

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=f"stub {self.name}")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content="ok")


def _tc(name: str, call_id: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "args": args or {}, "id": call_id, "type": "tool_call"}


def _update_plan_call(call_id: str, steps: list[Any], reason: str = "plan") -> dict[str, Any]:
    return _tc("update_plan", call_id, {"steps": steps, "reason": reason})


def _registry(*, with_spawn_worker: bool = True) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(UpdatePlanTool())
    registry.register(_StubTool(name="noop"))
    if with_spawn_worker:
        registry.register(_StubTool(name=SPAWN_WORKER_TOOL_NAME))
    return registry


async def _run(llm: _ScriptedLLM, registry: ToolRegistry, *, thread_id: str) -> AgentState:
    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(build_react_graph(llm_caller=llm, tool_registry=registry))
        cfg: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        return await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="start")],
                "step_count": 0,
                "max_steps": 10,
            },
            config=cfg,
        )


def _nudges(state: AgentState) -> list[HumanMessage]:
    return [
        m
        for m in state["messages"]
        if isinstance(m, HumanMessage) and _NUDGE_MARKER in str(m.content)
    ]


# ---------------------------------------------------------------------------
# Injection — conditions all met
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nudge_injected_once_and_hidden_from_ui() -> None:
    """spawn_worker present + plan with 3 pending steps → exactly one
    hidden nudge, carrying the pending count."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_update_plan_call("tc-1", ["step a", "step b", "step c"])],
            ),
            AIMessage(content="done"),
        ]
    )
    state = await _run(llm, _registry(), thread_id="nudge-basic")

    nudges = _nudges(state)
    assert len(nudges) == 1
    nudge = nudges[0]
    assert "contains 3 pending items" in str(nudge.content)
    assert "spawn_worker" in str(nudge.content)
    assert nudge.additional_kwargs.get("expert_work_hide_from_ui") is True
    # Dedupe key persisted for the next batch.
    assert state.get("delegation_nudge_plan_hash")


@pytest.mark.asyncio
async def test_in_progress_steps_count_as_pending() -> None:
    """未完成 = pending + in_progress: one in_progress + one pending
    step → threshold met, nudge fires with count 2."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    _update_plan_call(
                        "tc-1",
                        [
                            {"description": "step a", "status": "in_progress"},
                            {"description": "step b", "status": "pending"},
                            {"description": "step c", "status": "completed"},
                        ],
                    )
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    state = await _run(llm, _registry(), thread_id="nudge-in-progress")

    nudges = _nudges(state)
    assert len(nudges) == 1
    assert "contains 2 pending items" in str(nudges[0].content)


# ---------------------------------------------------------------------------
# Dedupe — one nudge per plan identity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_repeat_nudge_while_plan_unchanged() -> None:
    """Loops after the nudge — a plain tool turn, then an update_plan
    re-issuing the SAME steps — never re-nudge: one nudge total."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_update_plan_call("tc-1", ["step a", "step b"])],
            ),
            AIMessage(content="", tool_calls=[_tc("noop", "tc-2")]),
            AIMessage(
                content="",
                tool_calls=[_update_plan_call("tc-3", ["step a", "step b"], reason="re-issue")],
            ),
            AIMessage(content="done"),
        ]
    )
    state = await _run(llm, _registry(), thread_id="nudge-no-repeat")

    assert len(_nudges(state)) == 1


@pytest.mark.asyncio
async def test_progress_marking_does_not_renudge() -> None:
    """Marking a step in_progress via update_plan keeps the plan's
    identity (statuses are excluded from the hash) — no second nudge."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_update_plan_call("tc-1", ["step a", "step b"])],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    _update_plan_call(
                        "tc-2",
                        [
                            {"description": "step a", "status": "in_progress"},
                            {"description": "step b", "status": "pending"},
                        ],
                        reason="mark progress",
                    )
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    state = await _run(llm, _registry(), thread_id="nudge-progress-mark")

    assert len(_nudges(state)) == 1


@pytest.mark.asyncio
async def test_plan_replacement_renudges() -> None:
    """A structurally new plan (different steps) is a new version — the
    nudge may fire again, with the new pending count."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_update_plan_call("tc-1", ["step a", "step b"])],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    _update_plan_call("tc-2", ["step x", "step y", "step z"], reason="replan")
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    state = await _run(llm, _registry(), thread_id="nudge-replace")

    nudges = _nudges(state)
    assert len(nudges) == 2
    assert "contains 2 pending items" in str(nudges[0].content)
    assert "contains 3 pending items" in str(nudges[1].content)


# ---------------------------------------------------------------------------
# Zero-injection guards (strict)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_nudge_below_two_pending_steps() -> None:
    """A plan with a single non-completed step (one pending + one
    completed) never nudges — nothing to parallelise."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    _update_plan_call(
                        "tc-1",
                        [
                            {"description": "step a", "status": "completed"},
                            {"description": "step b", "status": "pending"},
                        ],
                    )
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    state = await _run(llm, _registry(), thread_id="nudge-one-pending")

    assert _nudges(state) == []
    assert state.get("delegation_nudge_plan_hash") is None


@pytest.mark.asyncio
async def test_no_spawn_worker_means_zero_behaviour_change() -> None:
    """Without spawn_worker in the registry the layer is inert — strict:
    no nudge, no dedupe channel, no extra HumanMessage of any kind."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_update_plan_call("tc-1", ["step a", "step b", "step c"])],
            ),
            AIMessage(content="done"),
        ]
    )
    state = await _run(llm, _registry(with_spawn_worker=False), thread_id="nudge-no-tool")

    assert _nudges(state) == []
    assert state.get("delegation_nudge_plan_hash") is None
    # Strict zero-behaviour-change: the only HumanMessage is the user's own.
    human = [m for m in state["messages"] if isinstance(m, HumanMessage)]
    assert [str(m.content) for m in human] == ["start"]
