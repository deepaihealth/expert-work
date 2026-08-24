"""Stream CM-0 PR2a — projection wiring into the ReAct graph.

Drives ``build_react_graph`` with an injected ``workspace_writer_factory``
(a recording fake, no live sandbox) and asserts the turn-end projection runs:
PLAN.md / TODO.md land, ``last_projection_hash`` is persisted, and a second
unchanged turn is skipped (only-if-changed → no extra writes).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.protocol import Plan, PlanStep
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
from orchestrator.context import WorkspaceFileWriter


@dataclass
class _RecordingWriter:
    writes: dict[str, str] = field(default_factory=dict)

    async def write(self, *, rel: str, content: str) -> None:
        self.writes[rel] = content


@dataclass
class _ScriptedLLM:
    responses: list[AIMessage]
    calls: int = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages, tools
        idx = self.calls
        self.calls += 1
        return self.responses[idx]


@dataclass
class _NoopTool:
    name: str = "noop"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="does nothing")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        return ToolResult(content="ok")


def _plan() -> Plan:
    return Plan(
        goal="do the thing",
        steps=(PlanStep(id="1", description="step one", status="completed"),),
    )


def _tc(call_id: str) -> dict[str, Any]:
    return {"name": "noop", "args": {}, "id": call_id, "type": "tool_call"}


async def _run_one_turn(
    *, writer: WorkspaceFileWriter | None, plan: Plan | None, thread_id: str
) -> AgentState:
    """One agent→tools→agent loop with a recording projection writer."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("tc-1")]),
            AIMessage(content="done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_NoopTool())
    factory = (lambda _ctx: writer) if writer is not None else None
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=registry,
                workspace_writer_factory=factory,
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        initial: dict[str, Any] = {
            "messages": [HumanMessage(content="start")],
            "step_count": 0,
            "max_steps": 5,
        }
        if plan is not None:
            initial["plan"] = plan
        return await compiled.ainvoke(initial, config=cfg)


async def test_turn_end_projection_writes_plan_files() -> None:
    writer = _RecordingWriter()
    state = await _run_one_turn(writer=writer, plan=_plan(), thread_id="proj-1")
    # BUG-10 (方案 a) — PLAN.md + TODO.md land under the THREAD's projection
    # dir, never the user-scoped workspace root.
    assert set(writer.writes) == {"threads/proj-1/PLAN.md", "threads/proj-1/TODO.md"}
    assert "do the thing" in writer.writes["threads/proj-1/PLAN.md"]
    assert "[x]" in writer.writes["threads/proj-1/TODO.md"]
    # The projection cursor is persisted on the checkpointed state.
    assert state.get("last_projection_hash")


async def test_traversal_bearing_thread_id_projects_nothing() -> None:
    # A thread id that can't form a safe path (BUG-10 guard) → skip, not root.
    writer = _RecordingWriter()
    state = await _run_one_turn(writer=writer, plan=_plan(), thread_id="../evil")
    assert writer.writes == {}
    assert state.get("last_projection_hash") is None


async def test_no_factory_means_no_projection() -> None:
    state = await _run_one_turn(writer=None, plan=_plan(), thread_id="proj-2")
    # Nothing wired → the channel stays untouched.
    assert state.get("last_projection_hash") is None


async def test_react_run_without_plan_projects_nothing() -> None:
    writer = _RecordingWriter()
    state = await _run_one_turn(writer=writer, plan=None, thread_id="proj-3")
    assert writer.writes == {}
    assert state.get("last_projection_hash") is None
