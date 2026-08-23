"""Tests for the J.8 approval gate — Stream J.8-step2 (Mini-ADR J-24).

Two layers:

* ``_approval`` helper units — ``find_approval_target`` /
  ``build_approval_request`` (pure functions, no graph).
* graph integration — a manifest-gated tool / an ``ask_for_approval``
  call pauses the run: ``tools_node`` writes ``pending_approval`` and
  the graph routes to END without dispatching.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.protocol import canonical_args_digest
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
from orchestrator.graph_builder._approval import (
    apply_resume_decision,
    build_approval_request,
    find_approval_target,
)
from orchestrator.tools.approval import ASK_FOR_APPROVAL_TOOL, AskForApprovalTool

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedLLM:
    responses: list[AIMessage]
    calls: int = 0

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        idx = self.calls
        self.calls += 1
        if idx >= len(self.responses):
            raise RuntimeError(f"scripted LLM ran out at call {idx}")
        return self.responses[idx]


@dataclass
class _ScriptedTool:
    name: str
    result: str = "tool-ran"
    dispatched: int = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=f"scripted {self.name}")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        self.dispatched += 1
        return ToolResult(content=self.result)


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


async def _run(
    llm: _ScriptedLLM,
    registry: ToolRegistry,
    *,
    approval_required_tools: frozenset[str] = frozenset(),
) -> AgentState:
    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=registry,
                approval_required_tools=approval_required_tools,
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": "t", "run_id": "r-1"}}
        return await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="start")],
                "step_count": 0,
                "max_steps": 5,
            },
            config=cfg,
        )


async def _run_pause_then_resume(
    llm: _ScriptedLLM,
    registry: ToolRegistry,
    *,
    approval_required_tools: frozenset[str],
    resume: dict[str, Any],
) -> AgentState:
    """Drive a run to its approval pause, apply ``resume`` via
    ``aupdate_state`` (as the resume endpoint does), then continue."""
    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=registry,
                approval_required_tools=approval_required_tools,
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": "t", "run_id": "r-1"}}
        paused = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="start")],
                "step_count": 0,
                "max_steps": 5,
            },
            config=cfg,
        )
        assert paused.get("pending_approval") is not None
        # Resume endpoint's move: write the verdict, re-position as_node="agent".
        await compiled.aupdate_state(
            cfg,
            {"pending_approval": None, "approval_resume": resume},
            as_node="agent",
        )
        return await compiled.ainvoke(None, config=cfg)


# ---------------------------------------------------------------------------
# _approval helper units
# ---------------------------------------------------------------------------


def test_find_approval_target_declarative_gate() -> None:
    calls = [
        _tool_call("safe_tool", {}, "tc-1"),
        _tool_call("send_email", {"to": "x"}, "tc-2"),
    ]
    target = find_approval_target(calls, frozenset({"send_email"}))
    assert target is not None
    assert target.index == 1
    assert target.is_agent_initiated is False


def test_find_approval_target_ask_for_approval() -> None:
    calls = [_tool_call(ASK_FOR_APPROVAL_TOOL, {"action_summary": "x"}, "tc-1")]
    target = find_approval_target(calls, frozenset())
    assert target is not None
    assert target.is_agent_initiated is True


def test_find_approval_target_none_when_nothing_gated() -> None:
    calls = [_tool_call("safe_tool", {}, "tc-1")]
    assert find_approval_target(calls, frozenset({"send_email"})) is None


def test_find_approval_target_returns_first_hit() -> None:
    """M0 pauses on the *first* gated call in the turn."""
    calls = [
        _tool_call("send_email", {}, "tc-1"),
        _tool_call(ASK_FOR_APPROVAL_TOOL, {}, "tc-2"),
    ]
    target = find_approval_target(calls, frozenset({"send_email"}))
    assert target is not None
    assert target.index == 0


def test_build_approval_request_policy_gate() -> None:
    calls = [_tool_call("send_email", {"to": "ops@x.com"}, "tc-1")]
    target = find_approval_target(calls, frozenset({"send_email"}))
    assert target is not None
    req = build_approval_request(target, thread_id="r-1", timeout_s=3600)
    assert req.reason_kind == "policy_gate"
    assert req.proposed_args == {"to": "ops@x.com"}
    assert "send_email" in req.action_summary
    assert (req.timeout_at - req.requested_at).total_seconds() == 3600
    assert req.request_id.startswith("approval:")
    # RT-6 Tier A (RT-ADR-19) — mint binds the proposed args.
    assert req.binding_digest == canonical_args_digest({"to": "ops@x.com"})


def test_build_approval_request_unbound_when_bind_false() -> None:
    """RT-6 — the action-screen path mints unbound (bind=False → empty digest).

    Its target is chosen by the judge, not find_approval_target, so the resume
    re-scan would verify the wrong call; leaving it unbound skips verification.
    """
    calls = [_tool_call("send_email", {"to": "ops@x.com"}, "tc-1")]
    target = find_approval_target(calls, frozenset({"send_email"}))
    assert target is not None
    req = build_approval_request(target, thread_id="r-1", timeout_s=60, bind=False)
    assert req.binding_digest == ""


def test_build_approval_request_agent_initiated() -> None:
    calls = [
        _tool_call(
            ASK_FOR_APPROVAL_TOOL,
            {
                "reason_kind": "approach_choice",
                "action_summary": "delete the temp dir?",
                "proposed_args": {"path": "workdir/scratch"},
            },
            "tc-1",
        )
    ]
    target = find_approval_target(calls, frozenset())
    assert target is not None
    req = build_approval_request(target, thread_id="r-1", timeout_s=86400)
    assert req.reason_kind == "approach_choice"
    assert req.action_summary == "delete the temp dir?"
    assert req.proposed_args == {"path": "workdir/scratch"}


def test_build_approval_request_clarification_uses_short_timeout() -> None:
    """B-20 — a clarification kind deadlines on clarification_timeout_s."""
    calls = [
        _tool_call(
            ASK_FOR_APPROVAL_TOOL,
            {"reason_kind": "missing_info", "action_summary": "which quarter?"},
            "tc-1",
        )
    ]
    target = find_approval_target(calls, frozenset())
    assert target is not None
    req = build_approval_request(
        target, thread_id="r-1", timeout_s=86400, clarification_timeout_s=300
    )
    assert req.reason_kind == "missing_info"
    assert (req.timeout_at - req.requested_at).total_seconds() == 300


def test_build_approval_request_risk_confirmation_keeps_safety_timeout() -> None:
    """B-20 — risk_confirmation is safety-class: clarification timeout ignored."""
    calls = [
        _tool_call(
            ASK_FOR_APPROVAL_TOOL,
            {"reason_kind": "risk_confirmation", "action_summary": "delete all?"},
            "tc-1",
        )
    ]
    target = find_approval_target(calls, frozenset())
    assert target is not None
    req = build_approval_request(
        target, thread_id="r-1", timeout_s=86400, clarification_timeout_s=300
    )
    assert (req.timeout_at - req.requested_at).total_seconds() == 86400


def test_build_approval_request_policy_gate_keeps_safety_timeout() -> None:
    calls = [_tool_call("send_email", {"to": "x"}, "tc-1")]
    target = find_approval_target(calls, frozenset({"send_email"}))
    assert target is not None
    req = build_approval_request(
        target, thread_id="r-1", timeout_s=7200, clarification_timeout_s=300
    )
    assert (req.timeout_at - req.requested_at).total_seconds() == 7200


def test_build_approval_request_clarification_without_override_keeps_timeout() -> None:
    """Callers that don't pass clarification_timeout_s keep the old behavior."""
    calls = [_tool_call(ASK_FOR_APPROVAL_TOOL, {"reason_kind": "approach_choice"}, "tc-1")]
    target = find_approval_target(calls, frozenset())
    assert target is not None
    req = build_approval_request(target, thread_id="r-1", timeout_s=86400)
    assert (req.timeout_at - req.requested_at).total_seconds() == 86400


def test_build_approval_request_unknown_reason_kind_falls_back() -> None:
    """A bogus agent-supplied reason_kind degrades to risk_confirmation."""
    calls = [_tool_call(ASK_FOR_APPROVAL_TOOL, {"reason_kind": "bogus"}, "tc-1")]
    target = find_approval_target(calls, frozenset())
    assert target is not None
    req = build_approval_request(target, thread_id="r-1", timeout_s=86400)
    assert req.reason_kind == "risk_confirmation"


def test_request_id_is_stable_for_same_inputs() -> None:
    calls = [_tool_call("send_email", {"to": "x"}, "tc-1")]
    target = find_approval_target(calls, frozenset({"send_email"}))
    assert target is not None
    a = build_approval_request(target, thread_id="r-1", timeout_s=60)
    b = build_approval_request(target, thread_id="r-1", timeout_s=60)
    assert a.request_id == b.request_id


# ---------------------------------------------------------------------------
# RT-6 Tier A — apply_resume_decision binding verification (RT-ADR-19)
# ---------------------------------------------------------------------------

_GATED = frozenset({"send_email"})


def test_resume_approve_matching_digest_dispatches() -> None:
    """Approve with the mint digest → dispatch the gated call unchanged."""
    calls = [_tool_call("send_email", {"to": "ops@x.com"}, "tc-1")]
    digest = canonical_args_digest({"to": "ops@x.com"})
    outcome = apply_resume_decision(
        calls, _GATED, {"decision": "approve", "binding_digest": digest}
    )
    assert not outcome.binding_drift
    assert outcome.reject_messages == []
    assert outcome.tool_calls[0]["args"] == {"to": "ops@x.com"}


def test_resume_approve_tampered_args_is_binding_drift() -> None:
    """Checkpoint tool_call args changed after approval → terminal integrity veto."""
    # The human approved {"to": "ops@x.com"}, but the checkpointed call now
    # carries a different recipient (tamper / replay / bug).
    tampered = [_tool_call("send_email", {"to": "attacker@evil.com"}, "tc-1")]
    approved_digest = canonical_args_digest({"to": "ops@x.com"})
    outcome = apply_resume_decision(
        tampered, _GATED, {"decision": "approve", "binding_digest": approved_digest}
    )
    assert outcome.binding_drift is True
    assert outcome.terminal is True
    assert outcome.tool_calls == []
    assert len(outcome.reject_messages) == 1
    assert "binding drift" in str(outcome.reject_messages[0].content)


def test_resume_modify_binds_to_modified_args() -> None:
    """Modify verifies against the digest of the modified args → dispatches."""
    calls = [_tool_call("send_email", {"to": "danger@evil.com"}, "tc-1")]
    modified = {"to": "safe@example.com"}
    outcome = apply_resume_decision(
        calls,
        _GATED,
        {
            "decision": "modify",
            "modified_args": modified,
            "binding_digest": canonical_args_digest(modified),
        },
    )
    assert not outcome.binding_drift
    assert outcome.tool_calls[0]["args"] == modified


def test_resume_modify_digest_mismatch_is_drift() -> None:
    """Modify whose args do not match the bound digest → integrity veto."""
    calls = [_tool_call("send_email", {"to": "x"}, "tc-1")]
    outcome = apply_resume_decision(
        calls,
        _GATED,
        {
            "decision": "modify",
            "modified_args": {"to": "safe@example.com"},
            "binding_digest": canonical_args_digest({"to": "different@example.com"}),
        },
    )
    assert outcome.binding_drift is True
    assert outcome.terminal is True


def test_resume_empty_digest_skips_verification() -> None:
    """A legacy / pre-feature row (empty digest) is dispatched unverified."""
    calls = [_tool_call("send_email", {"to": "whatever"}, "tc-1")]
    outcome = apply_resume_decision(calls, _GATED, {"decision": "approve", "binding_digest": ""})
    assert not outcome.binding_drift
    assert outcome.tool_calls[0]["args"] == {"to": "whatever"}


def test_resume_agent_initiated_is_not_verified() -> None:
    """ask_for_approval has no downstream execution → verification skipped."""
    calls = [
        _tool_call(
            ASK_FOR_APPROVAL_TOOL,
            {"reason_kind": "approach_choice", "action_summary": "?"},
            "tc-1",
        )
    ]
    # A mismatched digest must NOT trip drift for the agent-initiated pause.
    outcome = apply_resume_decision(
        calls, frozenset(), {"decision": "approve", "binding_digest": "deadbeef"}
    )
    assert not outcome.binding_drift


# ---------------------------------------------------------------------------
# ask_for_approval tool spec
# ---------------------------------------------------------------------------


def test_ask_for_approval_tool_spec() -> None:
    spec = AskForApprovalTool().spec
    assert spec.name == ASK_FOR_APPROVAL_TOOL
    assert "reason_kind" in spec.parameters["properties"]
    assert spec.parameters["required"] == ["reason_kind", "action_summary"]


# ---------------------------------------------------------------------------
# graph integration — the gate pauses the run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_declarative_gate_pauses_run() -> None:
    """A tool named in ``approval_required_tools`` pauses before dispatch."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tool_call("send_email", {"to": "x"}, "tc-1")]),
        ]
    )
    registry = ToolRegistry()
    email_tool = _ScriptedTool(name="send_email")
    registry.register(email_tool)

    state = await _run(llm, registry, approval_required_tools=frozenset({"send_email"}))

    pending = state.get("pending_approval")
    assert pending is not None
    assert pending.reason_kind == "policy_gate"
    # The gated tool was NOT dispatched — the run paused first.
    assert email_tool.dispatched == 0
    # Exactly one LLM call: agent → tools(pause) → END.
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_ungated_tool_runs_normally() -> None:
    """A tool NOT in the gate list dispatches + the loop continues."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tool_call("search", {"q": "x"}, "tc-1")]),
            AIMessage(content="done"),
        ]
    )
    registry = ToolRegistry()
    search_tool = _ScriptedTool(name="search")
    registry.register(search_tool)

    state = await _run(llm, registry, approval_required_tools=frozenset({"send_email"}))

    assert state.get("pending_approval") is None
    assert search_tool.dispatched == 1
    assert state["messages"][-1].content == "done"


@dataclass
class _IrreversibleTool:
    """A scripted tool that declares ``side_effect="irreversible"`` (TE-4)."""

    name: str
    dispatched: int = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="d", side_effect="irreversible")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        self.dispatched += 1
        return ToolResult(content="ran")


@pytest.mark.asyncio
async def test_irreversible_tool_does_not_auto_gate() -> None:
    """Approval gating is config-driven only: a tool declaring
    ``side_effect="irreversible"`` runs normally unless it is listed in
    ``approval_required_tools``. The platform no longer force-gates irreversible
    tools — sandbox isolation, serial scheduling (L.L6/TE-8) and per-tool audit
    (TE-2) remain the safety floor; the gate is the operator's opt-in."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="", tool_calls=[_tool_call("bash", {"cmd": "pip install x"}, "tc-1")]
            ),
            AIMessage(content="done"),
        ]
    )
    registry = ToolRegistry()
    danger = _IrreversibleTool(name="bash")
    registry.register(danger)

    # Empty manifest gate list — ``irreversible`` alone must NOT pause.
    state = await _run(llm, registry, approval_required_tools=frozenset())

    assert state.get("pending_approval") is None
    assert danger.dispatched == 1  # ran without approval
    assert state["messages"][-1].content == "done"


@pytest.mark.asyncio
async def test_irreversible_tool_gates_when_configured() -> None:
    """When the irreversible tool IS listed in ``approval_required_tools`` it
    pauses like any other declaratively-gated tool."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="", tool_calls=[_tool_call("bash", {"cmd": "pip install x"}, "tc-1")]
            ),
        ]
    )
    registry = ToolRegistry()
    danger = _IrreversibleTool(name="bash")
    registry.register(danger)

    state = await _run(llm, registry, approval_required_tools=frozenset({"bash"}))

    pending = state.get("pending_approval")
    assert pending is not None
    assert pending.reason_kind == "policy_gate"
    assert danger.dispatched == 0  # paused before dispatch


@pytest.mark.asyncio
async def test_ask_for_approval_call_pauses_run() -> None:
    """An ``ask_for_approval`` call pauses even with no declarative gate."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(
                        ASK_FOR_APPROVAL_TOOL,
                        {"reason_kind": "risk_confirmation", "action_summary": "ok?"},
                        "tc-1",
                    )
                ],
            ),
        ]
    )
    registry = ToolRegistry()
    registry.register(AskForApprovalTool())

    state = await _run(llm, registry)

    pending = state.get("pending_approval")
    assert pending is not None
    assert pending.reason_kind == "risk_confirmation"
    assert pending.action_summary == "ok?"


# ---------------------------------------------------------------------------
# resume — decision application (Stream J.8-step3b)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_approve_dispatches_the_gated_tool() -> None:
    """An ``approve`` verdict resumes the run + runs the gated tool."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tool_call("send_email", {"to": "x"}, "tc-1")]),
            AIMessage(content="email sent"),
        ]
    )
    registry = ToolRegistry()
    email_tool = _ScriptedTool(name="send_email", result="delivered")
    registry.register(email_tool)

    state = await _run_pause_then_resume(
        llm,
        registry,
        approval_required_tools=frozenset({"send_email"}),
        resume={"decision": "approve", "modified_args": None},
    )
    # Gated tool ran; run continued to a final answer.
    assert email_tool.dispatched == 1
    assert state.get("pending_approval") is None
    assert state.get("approval_resume") is None
    assert state["messages"][-1].content == "email sent"


@pytest.mark.asyncio
async def test_resume_modify_rewrites_the_tool_args() -> None:
    """A ``modify`` verdict dispatches the gated tool with replaced args."""
    seen_args: dict[str, Any] = {}

    @dataclass
    class _ArgCapturingTool:
        name: str = "send_email"

        @property
        def spec(self) -> ToolSpec:
            return ToolSpec(name=self.name, description="capture")

        async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
            del ctx
            seen_args.update(args)
            return ToolResult(content="ok")

    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_tool_call("send_email", {"to": "danger@evil.com"}, "tc-1")],
            ),
            AIMessage(content="done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_ArgCapturingTool())

    await _run_pause_then_resume(
        llm,
        registry,
        approval_required_tools=frozenset({"send_email"}),
        resume={"decision": "modify", "modified_args": {"to": "safe@example.com"}},
    )
    assert seen_args == {"to": "safe@example.com"}


@pytest.mark.asyncio
async def test_resume_reject_declarative_gate_terminates_run() -> None:
    """A declarative-gate reject does not run the tool + ends the run."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tool_call("send_email", {"to": "x"}, "tc-1")]),
        ]
    )
    registry = ToolRegistry()
    email_tool = _ScriptedTool(name="send_email")
    registry.register(email_tool)

    state = await _run_pause_then_resume(
        llm,
        registry,
        approval_required_tools=frozenset({"send_email"}),
        resume={"decision": "reject", "modified_args": None, "reason": "not allowed"},
    )
    # Gated tool never ran; run terminated (approval_outcome marks it).
    assert email_tool.dispatched == 0
    assert state.get("approval_outcome") == "rejected"
    # A rejection ToolMessage closed out the orphan tool_call.
    rejections = [m for m in state["messages"] if "[approval rejected]" in str(m.content)]
    assert len(rejections) == 1
    # The scripted LLM was only ever called once — the run did not loop.
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_resume_binding_drift_vetoes_the_tool() -> None:
    """RT-6 Tier A — a resume whose binding_digest no longer matches the gated
    call's args is an integrity veto: the tool never runs, the run ends."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tool_call("send_email", {"to": "x"}, "tc-1")]),
        ]
    )
    registry = ToolRegistry()
    email_tool = _ScriptedTool(name="send_email")
    registry.register(email_tool)

    state = await _run_pause_then_resume(
        llm,
        registry,
        approval_required_tools=frozenset({"send_email"}),
        # A digest that cannot match the real args (as if the checkpoint drifted).
        resume={"decision": "approve", "modified_args": None, "binding_digest": "tampered"},
    )
    assert email_tool.dispatched == 0
    assert state.get("approval_outcome") == "rejected"
    drift = [m for m in state["messages"] if "binding drift" in str(m.content)]
    assert len(drift) == 1
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_resume_reject_ask_for_approval_returns_to_agent() -> None:
    """An ask_for_approval reject loops back to the agent (not terminal)."""
    llm = _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call(
                        ASK_FOR_APPROVAL_TOOL,
                        {"reason_kind": "approach_choice", "action_summary": "plan A or B?"},
                        "tc-1",
                    )
                ],
            ),
            AIMessage(content="ok, going with plan B then"),
        ]
    )
    registry = ToolRegistry()
    registry.register(AskForApprovalTool())

    state = await _run_pause_then_resume(
        llm,
        registry,
        approval_required_tools=frozenset(),
        resume={"decision": "reject", "modified_args": None, "reason": "use B"},
    )
    # Not terminal — the agent ran again and produced a final answer.
    assert state.get("approval_outcome") is None
    assert state["messages"][-1].content == "ok, going with plan B then"
    assert llm.calls == 2
