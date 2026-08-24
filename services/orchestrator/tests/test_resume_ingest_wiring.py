"""Stream CM-8 (Mini-ADR CM-I4) — workspace ingest on the approval resume path.

The J.8 resume re-enters the graph at ``tools`` (``aupdate_state(as_node=
"agent")``) and skips the entry chain, so the CM-0 run-start ingest never
saw a PLAN.md edited *during* the pause. The tools node now runs the
ingest once on its resume branch — for approve and reject alike (a human
edit is not voided by the verdict).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

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
from orchestrator.context import render_plan_md
from orchestrator.graph_builder import make_workspace_ingest_node
from orchestrator.tools.sandbox import RecordingSandboxRuntime, SandboxOutcome


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
class _GatedTool:
    name: str = "deploy"
    dispatched: int = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="scripted deploy")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        self.dispatched += 1
        return ToolResult(content="deployed")


def _db_plan() -> Plan:
    return Plan(
        goal="ship the feature",
        steps=(
            PlanStep(id="1", description="write tests", status="completed"),
            PlanStep(id="2", description="implement", status="in_progress"),
        ),
    )


def _edited_plan() -> Plan:
    return Plan(
        goal="ship the feature",
        steps=(
            PlanStep(id="1", description="write tests", status="completed"),
            PlanStep(id="2", description="implement", status="completed"),
        ),
    )


def _read_envelope(content: str) -> SandboxOutcome:
    return SandboxOutcome(
        stdout=json.dumps(
            {"ok": True, "content": content, "content_hash": "h", "size": len(content)}
        ),
        stderr="",
        exit_code=0,
        timed_out=False,
    )


async def _pause_edit_resume(
    *,
    resume: dict[str, Any],
    wire_ingest: bool = True,
) -> tuple[AgentState, _GatedTool]:
    """Run to the approval pause, 'edit' PLAN.md, resume with ``resume``."""
    tool = _GatedTool()
    registry = ToolRegistry()
    registry.register(tool)
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "deploy", "args": {}, "id": "tc-1", "type": "tool_call"}],
            ),
            AIMessage(content="done"),
        ]
    )
    client = RecordingSandboxRuntime(outcome=_read_envelope(render_plan_md(_db_plan())))
    node = make_workspace_ingest_node(client=client) if wire_ingest else None
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=registry,
                approval_required_tools=frozenset({"deploy"}),
                workspace_ingest_node=node,
            )
        )
        cfg: RunnableConfig = {
            "configurable": {
                "thread_id": "99999999-8888-7777-6666-555544443333",
                "tenant_id": str(uuid4()),
                "user_id": str(uuid4()),
                "run_id": str(uuid4()),
            }
        }
        paused = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="go")],
                "step_count": 0,
                "max_steps": 5,
                "plan": _db_plan(),
            },
            config=cfg,
        )
        assert paused.get("pending_approval") is not None
        # The human edits PLAN.md while the run is paused.
        client.outcome = _read_envelope(render_plan_md(_edited_plan()))
        # The resume endpoint's move: verdict in, re-positioned at agent.
        await compiled.aupdate_state(
            cfg,
            {"pending_approval": None, "approval_resume": resume},
            as_node="agent",
        )
        final = await compiled.ainvoke(None, config=cfg)
        return final, tool


async def test_resume_approve_ingests_pause_edit() -> None:
    final, tool = await _pause_edit_resume(resume={"decision": "approve"})
    assert tool.dispatched == 1
    plan = final.get("plan")
    assert plan is not None
    assert [s.status for s in plan.steps] == ["completed", "completed"]


async def test_resume_reject_still_ingests_pause_edit() -> None:
    final, tool = await _pause_edit_resume(resume={"decision": "reject"})
    # Declarative gate reject terminates the run without dispatching…
    assert tool.dispatched == 0
    # …but the human's PLAN.md edit is not voided by the verdict.
    plan = final.get("plan")
    assert plan is not None
    assert [s.status for s in plan.steps] == ["completed", "completed"]


async def test_resume_without_ingest_wiring_keeps_db_plan() -> None:
    final, tool = await _pause_edit_resume(resume={"decision": "approve"}, wire_ingest=False)
    assert tool.dispatched == 1
    plan = final.get("plan")
    assert plan is not None
    # No ingest node → pre-CM-8 behaviour, the DB plan stands.
    assert [s.status for s in plan.steps] == ["completed", "in_progress"]
