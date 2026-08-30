"""Unit tests for J.4 sub-agent delegation — scaffold + ``SubAgentTool``."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from expert_work.protocol import SubAgentSpec
from expert_work.runtime.cancellation import (
    CANCELLATION_TOKEN_KEY,
    CancellationToken,
    RunCancelledError,
)
from orchestrator import GraphRunner, ToolRegistry, build_react_graph
from orchestrator.agent_factory import BuiltAgent
from orchestrator.errors import MaxStepsExceededError
from orchestrator.tools import (
    MAX_SUBAGENT_DEPTH,
    ChildAgentBuilder,
    SubAgentTool,
    Tool,
    ToolBlockedError,
    ToolContext,
    ToolEnv,
)

_SUB = SubAgentSpec(
    name="researcher",
    agent_ref="deep-researcher@1.0.0",
    description="Delegates deep research subtasks.",
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeGraph:
    """Fake compiled child graph — records each ``ainvoke`` and returns or
    raises a scripted outcome."""

    result: dict[str, Any] | None = None
    raises: BaseException | None = None
    calls: list[tuple[Any, Any]] = field(default_factory=list)

    async def ainvoke(self, state: Any, config: Any) -> Any:
        self.calls.append((state, config))
        if self.raises is not None:
            raise self.raises
        return self.result

    async def astream(
        self, state: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del stream_mode
        result = await self.ainvoke(state, config)
        yield ("values", result)


@dataclass
class _RecordingBuilder:
    """Conforms to :class:`ChildAgentBuilder`; records its keyword args and
    returns a scripted :class:`BuiltAgent` (or raises)."""

    built: BuiltAgent | None = None
    raises: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        depth: int,
        oauth_user_id: str | None = None,
    ) -> BuiltAgent:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "name": name,
                "version": version,
                "depth": depth,
                "oauth_user_id": oauth_user_id,
            }
        )
        if self.raises is not None:
            raise self.raises
        if self.built is None:
            raise RuntimeError("test misconfigured: _RecordingBuilder has no BuiltAgent")
        return self.built


def _built(
    graph: _FakeGraph, *, system_prompt: str = "child prompt", max_steps: int = 5
) -> BuiltAgent:
    return BuiltAgent(graph=graph, system_prompt=system_prompt, max_steps=max_steps)  # type: ignore[arg-type]


def _ctx(*, tenant_id: UUID | None = None, **kw: Any) -> ToolContext:
    return ToolContext(
        tenant_id=uuid4() if tenant_id is None else tenant_id,
        cancellation_token=CancellationToken(),
        **kw,
    )


# ---------------------------------------------------------------------------
# Scaffold — ChildAgentBuilder protocol + depth cap
# ---------------------------------------------------------------------------


def test_max_subagent_depth_is_3() -> None:
    assert MAX_SUBAGENT_DEPTH == 3


def test_child_agent_builder_protocol_accepts_conforming_callable() -> None:
    # runtime_checkable — a class with an async __call__ satisfies the
    # Protocol, so the control-plane's injected callback type-checks.
    assert isinstance(_RecordingBuilder(), ChildAgentBuilder)


def test_child_agent_builder_protocol_rejects_non_callable() -> None:
    assert not isinstance(object(), ChildAgentBuilder)


def test_tool_env_child_agent_builder_defaults_none() -> None:
    # An empty ToolEnv has no sub-agent builder — a manifest declaring
    # subagents against it raises AgentFactoryError (wired in J.4 PR4).
    assert ToolEnv().child_agent_builder is None


def test_tool_env_carries_child_agent_builder() -> None:
    builder = _RecordingBuilder()
    assert ToolEnv(child_agent_builder=builder).child_agent_builder is builder


# ---------------------------------------------------------------------------
# SubAgentTool.spec
# ---------------------------------------------------------------------------


def test_subagent_tool_satisfies_tool_protocol() -> None:
    tool = SubAgentTool(subagent=_SUB, builder=_RecordingBuilder(), child_depth=1)
    assert isinstance(tool, Tool)


def test_spec_exposes_subagent_name_and_task_param() -> None:
    spec = SubAgentTool(subagent=_SUB, builder=_RecordingBuilder(), child_depth=1).spec
    assert spec.name == "researcher"
    assert spec.description == _SUB.description
    assert spec.parameters["required"] == ["task"]
    assert "task" in spec.parameters["properties"]


# ---------------------------------------------------------------------------
# SubAgentTool.call — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_returns_child_final_answer() -> None:
    graph = _FakeGraph(
        result={"messages": [HumanMessage(content="task"), AIMessage(content="delegated answer")]}
    )
    builder = _RecordingBuilder(built=_built(graph))
    tool = SubAgentTool(subagent=_SUB, builder=builder, child_depth=2)
    ctx = _ctx()

    result = await tool.call({"task": "do research"}, ctx=ctx)

    assert result.content == "delegated answer"
    assert result.meta["subagent"] == "researcher"
    # agent_ref resolved + child depth handed to the builder verbatim.
    assert builder.calls == [
        {
            "tenant_id": ctx.tenant_id,
            "name": "deep-researcher",
            "version": "1.0.0",
            "depth": 2,
            "oauth_user_id": ctx.oauth_user_id,
        }
    ]


@pytest.mark.asyncio
async def test_call_seeds_child_input_with_prompt_and_task() -> None:
    graph = _FakeGraph(result={"messages": [AIMessage(content="ok")]})
    tool = SubAgentTool(
        subagent=_SUB, builder=_RecordingBuilder(built=_built(graph, max_steps=7)), child_depth=1
    )

    await tool.call({"task": "  summarise the doc  "}, ctx=_ctx())

    state, _config = graph.calls[0]
    assert isinstance(state["messages"][0], SystemMessage)
    assert state["messages"][0].content == "child prompt"
    assert isinstance(state["messages"][1], HumanMessage)
    assert state["messages"][1].content == "summarise the doc"  # trimmed
    assert state["step_count"] == 0
    assert state["max_steps"] == 7


@pytest.mark.asyncio
async def test_child_run_shares_parent_cancellation_token() -> None:
    graph = _FakeGraph(result={"messages": [AIMessage(content="ok")]})
    tool = SubAgentTool(
        subagent=_SUB, builder=_RecordingBuilder(built=_built(graph)), child_depth=1
    )
    token = CancellationToken()
    tenant = uuid4()
    user = uuid4()

    await tool.call(
        {"task": "x"},
        ctx=ToolContext(
            tenant_id=tenant,
            user_id=user,
            oauth_user_id="kc-subject-x",
            cancellation_token=token,
        ),
    )

    _state, config = graph.calls[0]
    configurable = config["configurable"]
    # Parent token shared verbatim — a parent cancel reaches the child.
    assert configurable[CANCELLATION_TOKEN_KEY] is token
    assert configurable["tenant_id"] == str(tenant)
    assert configurable["user_id"] == str(user)
    # MCP-OAUTH (OA-3b-后续) — OAuth subject threads into the child config so the
    # child resolves the same per-user OAuth pool.
    assert configurable["oauth_user_id"] == "kc-subject-x"
    # Fresh child thread / run — delegation is one-shot.
    assert configurable["thread_id"] != configurable["run_id"]


# ---------------------------------------------------------------------------
# SubAgentTool.call — guards + error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_requires_tenant_binding() -> None:
    tool = SubAgentTool(subagent=_SUB, builder=_RecordingBuilder(), child_depth=1)
    ctx = ToolContext(tenant_id=None, cancellation_token=CancellationToken())
    with pytest.raises(ToolBlockedError, match="tenant binding"):
        await tool.call({"task": "x"}, ctx=ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_args", [{}, {"task": ""}, {"task": "   "}, {"task": 123}])
async def test_call_rejects_missing_task(bad_args: dict[str, Any]) -> None:
    tool = SubAgentTool(subagent=_SUB, builder=_RecordingBuilder(), child_depth=1)
    with pytest.raises(ValueError, match="non-empty 'task'"):
        await tool.call(bad_args, ctx=_ctx())


@pytest.mark.asyncio
async def test_builder_error_propagates() -> None:
    # An unresolvable agent_ref (deleted / not found) is left to propagate
    # — the parent's tools node turns it into a ToolMessage error.
    builder = _RecordingBuilder(raises=KeyError("agent_ref not found"))
    tool = SubAgentTool(subagent=_SUB, builder=builder, child_depth=1)
    with pytest.raises(KeyError):
        await tool.call({"task": "x"}, ctx=_ctx())


@pytest.mark.asyncio
async def test_child_max_steps_returns_partial_note() -> None:
    graph = _FakeGraph(raises=MaxStepsExceededError(step_count=5, max_steps=5))
    tool = SubAgentTool(
        subagent=_SUB, builder=_RecordingBuilder(built=_built(graph)), child_depth=1
    )

    result = await tool.call({"task": "x"}, ctx=_ctx())

    # Out-of-steps is a partial result, not a tool failure.
    assert result.meta["subagent_max_steps"] is True
    assert "step limit" in result.content


@pytest.mark.asyncio
async def test_child_cancellation_propagates() -> None:
    # A cancel tears the whole run down — it must NOT be swallowed into a
    # normal ToolResult the way max_steps is.
    graph = _FakeGraph(raises=RunCancelledError("run cancelled"))
    tool = SubAgentTool(
        subagent=_SUB, builder=_RecordingBuilder(built=_built(graph)), child_depth=1
    )
    with pytest.raises(RunCancelledError):
        await tool.call({"task": "x"}, ctx=_ctx())


@pytest.mark.asyncio
async def test_child_with_no_ai_message_returns_empty_note() -> None:
    graph = _FakeGraph(result={"messages": [HumanMessage(content="task")]})
    tool = SubAgentTool(
        subagent=_SUB, builder=_RecordingBuilder(built=_built(graph)), child_depth=1
    )

    result = await tool.call({"task": "x"}, ctx=_ctx())

    assert result.meta["subagent_empty"] is True
    assert "no answer" in result.content


@pytest.mark.asyncio
async def test_call_picks_last_ai_message() -> None:
    messages: list[BaseMessage] = [
        AIMessage(content="first thought"),
        HumanMessage(content="tool result"),
        AIMessage(content="final answer"),
    ]
    graph = _FakeGraph(result={"messages": messages})
    tool = SubAgentTool(
        subagent=_SUB, builder=_RecordingBuilder(built=_built(graph)), child_depth=1
    )

    result = await tool.call({"task": "x"}, ctx=_ctx())

    assert result.content == "final answer"


# ---------------------------------------------------------------------------
# End-to-end — a parent agent delegates to a child through the real graph
# ---------------------------------------------------------------------------


async def _child_llm(*, messages: Any, tools: Any) -> AIMessage:
    """Child agent's LLM — echoes the delegated task, no tool calls."""
    del tools
    human = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
    task = human.content if human is not None else "?"
    return AIMessage(content=f"CHILD HANDLED: {task}", id="child-ai")


class _ParentLLM:
    """Parent agent's LLM — delegates on the first step, then finishes."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *, messages: Any, tools: Any) -> AIMessage:
        del messages, tools
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                id="parent-ai-1",
                tool_calls=[
                    {"name": "researcher", "args": {"task": "investigate X"}, "id": "tc-1"}
                ],
            )
        return AIMessage(content="PARENT DONE", id="parent-ai-2")


@pytest.mark.asyncio
async def test_parent_delegates_to_child_through_real_graph() -> None:
    # Build a real child agent graph over a fake LLM.
    child_graph = GraphRunner(checkpointer=InMemorySaver()).compile(
        build_react_graph(llm_caller=_child_llm, tool_registry=ToolRegistry())
    )
    child = BuiltAgent(graph=child_graph, system_prompt="child prompt", max_steps=5)

    # Parent registry carries one SubAgentTool resolving to that child.
    registry = ToolRegistry()
    registry.register(
        SubAgentTool(subagent=_SUB, builder=_RecordingBuilder(built=child), child_depth=1)
    )
    parent_graph = GraphRunner(checkpointer=InMemorySaver()).compile(
        build_react_graph(llm_caller=_ParentLLM(), tool_registry=registry)
    )

    result = await parent_graph.ainvoke(
        {"messages": [HumanMessage(content="delegate this")], "step_count": 0, "max_steps": 5},
        {"configurable": {"thread_id": str(uuid4()), "tenant_id": str(uuid4())}},
    )

    messages = result["messages"]
    # The child's answer flowed back as the SubAgentTool's ToolMessage.
    tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].content == "CHILD HANDLED: investigate X"
    # The parent then reasoned over it and finished.
    assert isinstance(messages[-1], AIMessage)
    assert messages[-1].content == "PARENT DONE"


# ---------------------------------------------------------------------------
# Mini-ADR J-21 — sub-agent trajectory + budget telemetry
# ---------------------------------------------------------------------------


@dataclass
class _FakeRecorder:
    """Captures :class:`TrajectoryRecord`\\s the SubAgentTool dispatches.

    Mirrors the surface :meth:`SubAgentTool._dispatch_trajectory` consumes
    so we can assert without a real ObjectStore. ``.record()`` is the only
    method called.
    """

    records: list[Any] = field(default_factory=list)

    async def record(self, record: Any) -> None:
        self.records.append(record)


@dataclass
class _StatefulGraph:
    """``_FakeGraph`` with a stub ``aget_state`` so the J-21 partial-fetch
    path has something to read after a max_steps / cancellation."""

    result: dict[str, Any] | None = None
    raises: BaseException | None = None
    snapshot_values: dict[str, Any] | None = None
    calls: list[tuple[Any, Any]] = field(default_factory=list)

    async def ainvoke(self, state: Any, config: Any) -> Any:
        self.calls.append((state, config))
        if self.raises is not None:
            raise self.raises
        return self.result

    async def astream(
        self, state: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del stream_mode
        result = await self.ainvoke(state, config)
        yield ("values", result)

    async def aget_state(self, config: Any) -> Any:
        del config
        values = self.snapshot_values or {}

        @dataclass
        class _Snapshot:
            values: dict[str, Any]

        return _Snapshot(values=values)


@pytest.mark.asyncio
async def test_call_emits_budget_telemetry_on_success() -> None:
    """Mini-ADR J-21 — successful child run writes the three counters."""
    graph = _FakeGraph(
        result={
            "messages": [
                HumanMessage(content="task"),
                AIMessage(content="thinking"),
                AIMessage(content="delegated answer"),
            ],
            "step_count": 4,
        }
    )
    tool = SubAgentTool(
        subagent=_SUB, builder=_RecordingBuilder(built=_built(graph)), child_depth=1
    )

    result = await tool.call({"task": "do research"}, ctx=_ctx())

    assert result.meta["subagent"] == "researcher"
    assert result.meta["iteration_used"] == 4
    # Two AIMessages in the trajectory.
    assert result.meta["llm_call_count"] == 2
    assert isinstance(result.meta["wall_clock_ms"], int)
    assert result.meta["wall_clock_ms"] >= 0


@pytest.mark.asyncio
async def test_call_dispatches_trajectory_on_success() -> None:
    """Mini-ADR J-21 — the L7 record lands with outcome=success + sub_thread_id."""
    msgs = [HumanMessage(content="task"), AIMessage(content="delegated answer")]
    graph = _FakeGraph(result={"messages": msgs, "step_count": 2})
    recorder = _FakeRecorder()
    tool = SubAgentTool(
        subagent=_SUB,
        builder=_RecordingBuilder(built=_built(graph)),
        child_depth=2,
        trajectory_recorder=recorder,  # type: ignore[arg-type]
    )

    result = await tool.call({"task": "x"}, ctx=_ctx())

    # Let the fire-and-forget task drain.
    await asyncio.sleep(0)
    assert len(recorder.records) == 1
    rec = recorder.records[0]
    assert rec.outcome == "success"
    assert rec.step_count == 2
    assert rec.metadata["subagent_name"] == "researcher"
    assert rec.metadata["subagent_ref"] == "deep-researcher@1.0.0"
    assert rec.metadata["child_depth"] == 2
    # sub_thread_id keyed by the same UUID the child_config used.
    _state, child_config = graph.calls[0]
    assert str(rec.thread_id) == child_config["configurable"]["thread_id"]
    assert str(rec.run_id) == child_config["configurable"]["run_id"]
    # Parent meta still carries the answer.
    assert result.content == "delegated answer"


@pytest.mark.asyncio
async def test_call_dispatches_trajectory_on_max_steps() -> None:
    """Mini-ADR J-21 — max_steps lands as outcome=max_steps + partial messages."""
    partial_msgs = [HumanMessage(content="task"), AIMessage(content="partial think")]
    graph = _StatefulGraph(
        raises=MaxStepsExceededError(step_count=5, max_steps=5),
        snapshot_values={"messages": partial_msgs, "step_count": 5},
    )
    recorder = _FakeRecorder()
    tool = SubAgentTool(
        subagent=_SUB,
        builder=_RecordingBuilder(built=_built(graph)),  # type: ignore[arg-type]
        child_depth=1,
        trajectory_recorder=recorder,  # type: ignore[arg-type]
    )

    result = await tool.call({"task": "x"}, ctx=_ctx())

    await asyncio.sleep(0)
    assert result.meta["subagent_max_steps"] is True
    assert result.meta["iteration_used"] == 5
    assert result.meta["llm_call_count"] == 1
    assert len(recorder.records) == 1
    assert recorder.records[0].outcome == "max_steps"
    assert recorder.records[0].step_count == 5


@pytest.mark.asyncio
async def test_call_dispatches_trajectory_on_cancellation() -> None:
    """Mini-ADR J-21 — cancellation still dispatches outcome=cancelled before re-raising."""
    partial_msgs = [HumanMessage(content="task")]
    graph = _StatefulGraph(
        raises=RunCancelledError("run cancelled"),
        snapshot_values={"messages": partial_msgs, "step_count": 1},
    )
    recorder = _FakeRecorder()
    tool = SubAgentTool(
        subagent=_SUB,
        builder=_RecordingBuilder(built=_built(graph)),  # type: ignore[arg-type]
        child_depth=1,
        trajectory_recorder=recorder,  # type: ignore[arg-type]
    )

    with pytest.raises(RunCancelledError):
        await tool.call({"task": "x"}, ctx=_ctx())

    await asyncio.sleep(0)
    assert len(recorder.records) == 1
    assert recorder.records[0].outcome == "cancelled"
    assert recorder.records[0].step_count == 1


@pytest.mark.asyncio
async def test_call_without_recorder_still_emits_telemetry() -> None:
    """Mini-ADR J-21 — ``trajectory_recorder=None`` is valid; meta still carries
    the budget counters, no ObjectStore writes."""
    graph = _FakeGraph(result={"messages": [AIMessage(content="answer")], "step_count": 1})
    tool = SubAgentTool(
        subagent=_SUB,
        builder=_RecordingBuilder(built=_built(graph)),
        child_depth=1,
        trajectory_recorder=None,
    )

    result = await tool.call({"task": "x"}, ctx=_ctx())

    assert result.meta["iteration_used"] == 1
    assert result.meta["llm_call_count"] == 1
    assert "wall_clock_ms" in result.meta


@pytest.mark.asyncio
async def test_call_partial_fetch_handles_missing_aget_state() -> None:
    """Graphs without ``aget_state`` fall back to empty messages on max_steps."""
    graph = _FakeGraph(raises=MaxStepsExceededError(step_count=3, max_steps=3))
    recorder = _FakeRecorder()
    tool = SubAgentTool(
        subagent=_SUB,
        builder=_RecordingBuilder(built=_built(graph)),
        child_depth=1,
        trajectory_recorder=recorder,  # type: ignore[arg-type]
    )

    result = await tool.call({"task": "x"}, ctx=_ctx())
    await asyncio.sleep(0)

    assert result.meta["subagent_max_steps"] is True
    # No messages → no AIMessage → 0 LLM calls; iteration_used falls back to 0.
    assert result.meta["llm_call_count"] == 0
    assert result.meta["iteration_used"] == 0
    assert recorder.records[0].outcome == "max_steps"
    assert recorder.records[0].messages == []


# ---------------------------------------------------------------------------
# Mini-ADR J-40 — sub-agent invocation entries in ToolResult.state_updates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_emits_invocation_on_success() -> None:
    """Mini-ADR J-40 — success path emits SubAgentInvocation with status=COMPLETED."""
    from expert_work.protocol import SubagentStatus

    graph = _FakeGraph(
        result={
            "messages": [
                HumanMessage(content="task"),
                AIMessage(content="delegated answer"),
            ],
            "step_count": 2,
        }
    )
    tool = SubAgentTool(
        subagent=_SUB, builder=_RecordingBuilder(built=_built(graph)), child_depth=2
    )

    result = await tool.call({"task": "x"}, ctx=_ctx())

    invocations = result.state_updates.get("subagent_invocations")
    assert isinstance(invocations, list) and len(invocations) == 1
    inv = invocations[0]
    assert inv.status is SubagentStatus.COMPLETED
    assert inv.name == "researcher"
    assert inv.agent_ref == "deep-researcher@1.0.0"
    assert inv.child_depth == 2
    assert inv.result_excerpt == "delegated answer"
    assert inv.error is None
    assert inv.iteration_used == 2
    assert inv.llm_call_count == 1
    assert inv.wall_clock_ms >= 0
    # task_id matches sub_run_id used in child_config
    _state, child_config = graph.calls[0]
    assert str(inv.task_id) == child_config["configurable"]["run_id"]
    assert str(inv.sub_thread_id) == child_config["configurable"]["thread_id"]


@pytest.mark.asyncio
async def test_call_emits_invocation_on_max_steps() -> None:
    """Mini-ADR J-40 — max_steps path emits SubAgentInvocation with status=FAILED + error."""
    from expert_work.protocol import SubagentStatus

    graph = _StatefulGraph(
        raises=MaxStepsExceededError(step_count=5, max_steps=5),
        snapshot_values={
            "messages": [HumanMessage(content="task"), AIMessage(content="partial")],
            "step_count": 5,
        },
    )
    tool = SubAgentTool(
        subagent=_SUB,
        builder=_RecordingBuilder(built=_built(graph)),  # type: ignore[arg-type]
        child_depth=1,
    )

    result = await tool.call({"task": "x"}, ctx=_ctx())

    invocations = result.state_updates.get("subagent_invocations")
    assert len(invocations) == 1
    inv = invocations[0]
    assert inv.status is SubagentStatus.FAILED
    assert inv.iteration_used == 5
    assert inv.llm_call_count == 1
    assert "step limit" in (inv.error or "")
    assert inv.result_excerpt == ""


@pytest.mark.asyncio
async def test_call_emits_invocation_on_empty_answer() -> None:
    """Status=COMPLETED with empty result_excerpt when child produced no AIMessage."""
    from expert_work.protocol import SubagentStatus

    graph = _FakeGraph(result={"messages": [HumanMessage(content="task")], "step_count": 1})
    tool = SubAgentTool(
        subagent=_SUB, builder=_RecordingBuilder(built=_built(graph)), child_depth=1
    )

    result = await tool.call({"task": "x"}, ctx=_ctx())

    invocations = result.state_updates.get("subagent_invocations")
    assert len(invocations) == 1
    assert invocations[0].status is SubagentStatus.COMPLETED
    assert invocations[0].result_excerpt == ""
    assert result.meta.get("subagent_empty") is True


@pytest.mark.asyncio
async def test_cancellation_does_not_emit_invocation() -> None:
    """Cancelled path raises before state_updates is built — parent state
    sees no entry; L7 trajectory still records the cancelled sub-run (PR #220)."""
    graph = _StatefulGraph(
        raises=RunCancelledError("run cancelled"),
        snapshot_values={"messages": [HumanMessage(content="task")], "step_count": 1},
    )
    recorder = _FakeRecorder()
    tool = SubAgentTool(
        subagent=_SUB,
        builder=_RecordingBuilder(built=_built(graph)),  # type: ignore[arg-type]
        child_depth=1,
        trajectory_recorder=recorder,  # type: ignore[arg-type]
    )

    with pytest.raises(RunCancelledError):
        await tool.call({"task": "x"}, ctx=_ctx())

    # No state_updates because no ToolResult was returned; L7 trajectory
    # still captured the cancelled outcome for J.13 eval.
    await asyncio.sleep(0)
    assert recorder.records[0].outcome == "cancelled"


@pytest.mark.asyncio
async def test_state_updates_uses_allowlisted_key() -> None:
    """The state_updates key must be ``subagent_invocations`` so the
    tools_node TOOL_ALLOWED_STATE_KEYS check lets it through."""
    from orchestrator.tools.registry import TOOL_ALLOWED_STATE_KEYS

    assert "subagent_invocations" in TOOL_ALLOWED_STATE_KEYS

    graph = _FakeGraph(result={"messages": [AIMessage(content="ok")], "step_count": 1})
    tool = SubAgentTool(
        subagent=_SUB, builder=_RecordingBuilder(built=_built(graph)), child_depth=1
    )
    result = await tool.call({"task": "x"}, ctx=_ctx())
    assert set(result.state_updates).issubset(TOOL_ALLOWED_STATE_KEYS)


# ---------------------------------------------------------------------------
# Mini-ADR J-40 — ToolSpec.is_parallel_safe + fan-out via plan_stages
# ---------------------------------------------------------------------------


def test_subagent_tool_spec_is_parallel_safe() -> None:
    """``is_parallel_safe=True`` lets plan_stages place sibling delegations
    in the same stage (independent thread_ids + sandbox sessions)."""
    spec = SubAgentTool(subagent=_SUB, builder=_RecordingBuilder(), child_depth=1).spec
    assert spec.is_parallel_safe is True
    # SubAgentTool still mutates parent state (subagent_invocations), so it
    # is NOT is_read_only.
    assert spec.is_read_only is False


def test_plan_stages_groups_sibling_subagent_calls_into_one_stage() -> None:
    """Mini-ADR J-40 — three sibling SubAgentTool calls share a single
    stage (concurrency 3), not three serial stages."""
    from orchestrator.tools.scheduling import plan_stages

    sub_a = SubAgentTool(
        subagent=SubAgentSpec(name="alpha", agent_ref="alpha@1.0.0", description="alpha sub"),
        builder=_RecordingBuilder(),
        child_depth=1,
    )
    sub_b = SubAgentTool(
        subagent=SubAgentSpec(name="beta", agent_ref="beta@1.0.0", description="beta sub"),
        builder=_RecordingBuilder(),
        child_depth=1,
    )
    sub_c = SubAgentTool(
        subagent=SubAgentSpec(name="gamma", agent_ref="gamma@1.0.0", description="gamma sub"),
        builder=_RecordingBuilder(),
        child_depth=1,
    )
    specs = {sub.spec.name: sub.spec for sub in (sub_a, sub_b, sub_c)}
    calls = [
        {"id": "tc-1", "name": "alpha", "args": {"task": "x"}},
        {"id": "tc-2", "name": "beta", "args": {"task": "y"}},
        {"id": "tc-3", "name": "gamma", "args": {"task": "z"}},
    ]

    stages = plan_stages(calls, specs)

    assert len(stages) == 1
    assert {call.name for call in stages[0]} == {"alpha", "beta", "gamma"}


# ---------------------------------------------------------------------------
# Mini-ADR J-40 — global deadline propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_config_inherits_parent_deadline_unchanged() -> None:
    """Mini-ADR J-40 — child config carries the same ``deadline_at`` value
    the parent passed in (no per-level reset)."""
    graph = _FakeGraph(result={"messages": [AIMessage(content="ok")], "step_count": 1})
    tool = SubAgentTool(
        subagent=_SUB, builder=_RecordingBuilder(built=_built(graph)), child_depth=1
    )
    import time as _time

    parent_deadline = _time.monotonic() + 30.0

    await tool.call(
        {"task": "x"},
        ctx=ToolContext(
            tenant_id=uuid4(),
            cancellation_token=CancellationToken(),
            deadline_at=parent_deadline,
        ),
    )

    _state, child_config = graph.calls[0]
    assert child_config["configurable"]["deadline_at"] == parent_deadline


@pytest.mark.asyncio
async def test_call_refuses_delegation_when_deadline_expired() -> None:
    """A ``deadline_at`` already in the past short-circuits to
    ``RunCancelledError`` without invoking the child graph."""
    graph = _FakeGraph(result={"messages": [AIMessage(content="ok")], "step_count": 1})
    builder = _RecordingBuilder(built=_built(graph))
    tool = SubAgentTool(subagent=_SUB, builder=builder, child_depth=1)
    import time as _time

    expired = _time.monotonic() - 5.0

    with pytest.raises(RunCancelledError, match="deadline"):
        await tool.call(
            {"task": "x"},
            ctx=ToolContext(
                tenant_id=uuid4(),
                cancellation_token=CancellationToken(),
                deadline_at=expired,
            ),
        )
    # Child graph must not be invoked.
    assert graph.calls == []


@pytest.mark.asyncio
async def test_child_config_omits_deadline_when_parent_has_none() -> None:
    """No ``policies.run_deadline_s`` configured → child config has no
    ``deadline_at`` key (vs an explicit ``None`` which would still set
    the key); cleaner than leaking the absence as a sentinel."""
    graph = _FakeGraph(result={"messages": [AIMessage(content="ok")], "step_count": 1})
    tool = SubAgentTool(
        subagent=_SUB, builder=_RecordingBuilder(built=_built(graph)), child_depth=1
    )

    await tool.call({"task": "x"}, ctx=_ctx())  # _ctx() has deadline_at=None

    _state, child_config = graph.calls[0]
    assert "deadline_at" not in child_config["configurable"]


# ---------------------------------------------------------------------------
# Mini-ADR J-40 — cycle detection at build time
# ---------------------------------------------------------------------------


def _make_spec_with_subagents(name: str, *children: str) -> Any:
    """Build a minimal ``AgentSpec`` referencing the given child names."""
    from expert_work.protocol import AgentSpec

    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": name, "version": "1.0.0", "tenant": "test-tenant"},
            "spec": {
                "tenant_config": {},
                "model": {
                    "provider": "anthropic",
                    "name": "claude-sonnet-4-6",
                    "api_key_ref": "secret://test",
                },
                "system_prompt": {"template": "you are an agent"},
                "sandbox": {
                    "resources": {"cpu": "1.0", "memory": "1Gi"},
                    "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
                    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
                },
                "subagents": [
                    {
                        "name": child,
                        "agent_ref": f"{child}@1.0.0",
                        "description": f"{child} sub-agent",
                    }
                    for child in children
                ],
            },
        }
    )


def test_detect_subagent_cycle_passes_acyclic_graph() -> None:
    """A → B → C: no cycle, no exception."""
    from orchestrator.agent_factory import detect_subagent_cycle
    from orchestrator.errors import AgentFactoryError  # noqa: F401  - imported for type ref

    specs = {
        "alpha": _make_spec_with_subagents("alpha", "beta"),
        "beta": _make_spec_with_subagents("beta", "gamma"),
        "gamma": _make_spec_with_subagents("gamma"),
    }
    detect_subagent_cycle(specs["alpha"], resolve=lambda n, v: specs.get(n))


def test_detect_subagent_cycle_detects_two_node_cycle() -> None:
    """A → B → A: AgentFactoryError with cycle path."""
    from orchestrator.agent_factory import detect_subagent_cycle
    from orchestrator.errors import AgentFactoryError

    specs = {
        "alpha": _make_spec_with_subagents("alpha", "beta"),
        "beta": _make_spec_with_subagents("beta", "alpha"),
    }
    with pytest.raises(AgentFactoryError, match=r"alpha .* beta .* alpha"):
        detect_subagent_cycle(specs["alpha"], resolve=lambda n, v: specs.get(n))


def test_detect_subagent_cycle_detects_three_node_cycle() -> None:
    """A → B → C → A."""
    from orchestrator.agent_factory import detect_subagent_cycle
    from orchestrator.errors import AgentFactoryError

    specs = {
        "alpha": _make_spec_with_subagents("alpha", "beta"),
        "beta": _make_spec_with_subagents("beta", "gamma"),
        "gamma": _make_spec_with_subagents("gamma", "alpha"),
    }
    with pytest.raises(AgentFactoryError, match="cycle"):
        detect_subagent_cycle(specs["alpha"], resolve=lambda n, v: specs.get(n))


def test_detect_subagent_cycle_skips_unresolvable_refs() -> None:
    """A → B (unresolvable) does NOT raise — the unresolved ref will
    surface as a SubAgentNotFoundError at run time, but cycle detection
    must not synthesise a false positive."""
    from orchestrator.agent_factory import detect_subagent_cycle

    spec_a = _make_spec_with_subagents("alpha", "beta")
    detect_subagent_cycle(spec_a, resolve=lambda _n, _v: None)


def test_detect_subagent_cycle_tolerates_shared_subagents() -> None:
    """Diamond — A → B, A → C, B → D, C → D — no cycle even though D is
    visited via two paths. The ``visited`` set short-circuits the second
    walk."""
    from orchestrator.agent_factory import detect_subagent_cycle

    specs = {
        "alpha": _make_spec_with_subagents("alpha", "beta", "gamma"),
        "beta": _make_spec_with_subagents("beta", "delta"),
        "gamma": _make_spec_with_subagents("gamma", "delta"),
        "delta": _make_spec_with_subagents("delta"),
    }
    detect_subagent_cycle(specs["alpha"], resolve=lambda n, v: specs.get(n))


@pytest.mark.asyncio
async def test_child_config_forwards_the_artifact_recorder() -> None:
    """产物清单契约 —— 记录器下传子代:worker/子 Agent 登记的产物同属本
    run 的交付物,不下传清单会缺项(真栈 run 02ab4cfc 实证 worker 会
    save_artifact)。旧代码 configurable 里无此键,断言必红。"""
    from orchestrator.tools.artifact import ARTIFACT_RECORDER_KEY

    graph = _FakeGraph(result={"messages": [AIMessage(content="ok")], "step_count": 1})
    tool = SubAgentTool(
        subagent=_SUB, builder=_RecordingBuilder(built=_built(graph)), child_depth=1
    )
    recorded: list[dict[str, object]] = []
    # 绑定一次 —— ``recorded.append`` 每次取属性都是新的 bound method,
    # 直接在两处取再 ``is`` 比较恒 False。
    recorder = recorded.append

    await tool.call(
        {"task": "x"},
        ctx=ToolContext(
            tenant_id=uuid4(),
            cancellation_token=CancellationToken(),
            artifact_recorder=recorder,
        ),
    )

    _state, child_config = graph.calls[0]
    assert child_config["configurable"][ARTIFACT_RECORDER_KEY] is recorder


@pytest.mark.asyncio
async def test_static_subagent_seed_also_carries_this_turns_attachments() -> None:
    """注入点放在共享的 ``run_child_to_result`` 而不是 ``spawn_worker``,就是为了
    静态子 Agent 走同一条路 —— 它同样不继承对话,同样看不到 ``[file attached: …]``。
    这条钉住"一处覆盖两条委派路径";若哪天有人把注入挪进 spawn_worker,这里会红。"""
    graph = _FakeGraph(
        result={"messages": [HumanMessage(content="task"), AIMessage(content="done")]}
    )
    tool = SubAgentTool(
        subagent=_SUB, builder=_RecordingBuilder(built=_built(graph)), child_depth=2
    )

    await tool.call({"task": "分析资料内容"}, ctx=_ctx(turn_documents=("uploads/材料.docx",)))

    seed = graph.calls[0][0]["messages"][1].content
    assert "uploads/材料.docx" in seed
    assert seed.startswith("分析资料内容")
