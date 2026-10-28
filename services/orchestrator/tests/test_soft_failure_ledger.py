"""B-226 ③⑤ — soft tool failures and refused actions enter the run's failure ledger.

③ A tool can fail without raising: a script exits non-zero, an MCP server answers
``isError``, a delegated child stops early. Before B-226 the tools node read that
as a success — and a success with the same ledger key ``pop``s an earlier failure,
so "the last script failed" was recorded as ``completed=true`` (46 runs in 30 days
on the test environment). The tool's own structured signal is
``ToolResult.failed`` (``test_soft_failure_signals.py``); here the tools node turns
it into a ledger entry.

⑤ A call a policy stopped before it ran (action screening ``block``, a rejected
approval request that does not end the run) returned early and never touched the
ledger: the action never happened and nothing recorded it.

Driven through the real graph, like ``test_run_exit_reason.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.runs.schemas import compute_completed
from orchestrator import (
    ActionVerdict,
    AgentState,
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
    pending_request_binding,
)
from orchestrator.graph_builder import builder as builder_mod
from orchestrator.graph_builder.platform_context import RECOVERY_ADVISORY_MARK
from orchestrator.tools import (
    BashTool,
    ExecPythonTool,
    HTTPTool,
    RecordingSandboxRuntime,
    SandboxOutcome,
)
from orchestrator.tools.approval import ASK_FOR_APPROVAL_TOOL, AskForApprovalTool
from orchestrator.tools.mcp import MCPCallResult, MCPTool, MCPToolDef, RecordingMCPClient
from orchestrator.tools.sandbox import format_sandbox_outcome
from orchestrator.tools.spawn_worker import SpawnWorkerTool, WorkerSpawnBudget

from .test_spawn_worker import _answer_graph, _FakeGraph, _RecordingWorkerBuilder
from .test_spawn_worker import _built as _built_worker

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


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
        return self.responses[min(idx, len(self.responses) - 1)]


@dataclass
class _ScriptedTool:
    """Returns (or raises) ``outcomes[i]`` on its i-th call; the last one repeats."""

    name: str = "exec_python"
    outcomes: list[ToolResult | Exception] = field(
        default_factory=lambda: [ToolResult(content="ok")]
    )
    read_only: bool = False
    calls: int = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description="scripted tool",
            is_read_only=self.read_only,
            side_effect="read_only" if self.read_only else None,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@dataclass
class _NamedJudge:
    """Action screening double: misaligned for ``blocked`` names, the first ``times`` rulings."""

    blocked: frozenset[str]
    times: int = 1_000
    rulings: int = 0

    async def judge_action(
        self, *, user_request: str, tool_name: str, tool_args: Mapping[str, Any]
    ) -> ActionVerdict:
        del user_request, tool_args
        if tool_name in self.blocked and self.rulings < self.times:
            self.rulings += 1
            return ActionVerdict(aligned=False, reason="off-task")
        return ActionVerdict(aligned=True, reason="ok")


_FAILED = ToolResult(content="stderr:\nboom\n\nexit_code: 2", failed=True)
_TRANSIENT = ToolResult(content="HTTP 503", failed=True, failure_transient=True)
_OK = ToolResult(content="exit_code: 0")


def _tc(name: str, call_id: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"name": name, "args": args or {}, "id": call_id, "type": "tool_call"}


def _calls(*batches: list[dict[str, Any]], final: str = "done") -> _ScriptedLLM:
    return _ScriptedLLM(
        responses=[AIMessage(content="", tool_calls=batch) for batch in batches]
        + [AIMessage(content=final)]
    )


def _graph_input() -> dict[str, Any]:
    return {"messages": [HumanMessage(content="start")], "step_count": 0, "max_steps": 8}


async def _run(
    llm: _ScriptedLLM,
    registry: ToolRegistry,
    *,
    extra_cfg: Mapping[str, Any] | None = None,
    **graph_kw: Any,
) -> AgentState:
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry, **graph_kw)
        )
        configurable = {"thread_id": str(uuid4()), "run_id": "r-1", "tenant_id": str(uuid4())}
        cfg: RunnableConfig = {"configurable": {**configurable, **(extra_cfg or {})}}
        return await compiled.ainvoke(_graph_input(), config=cfg)


async def _run_with_verdicts(
    llm: _ScriptedLLM,
    registry: ToolRegistry,
    verdicts: Sequence[Mapping[str, Any]],
    **graph_kw: Any,
) -> AgentState:
    """Run to each approval pause in turn, answer it the way the resume endpoint does."""
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry, **graph_kw)
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4()), "run_id": "r-1"}}
        state = await compiled.ainvoke(_graph_input(), config=cfg)
        for resume in verdicts:
            request = state.get("pending_approval")
            assert request is not None, "precondition: the run paused on an approval"
            binding = pending_request_binding((await compiled.aget_state(cfg)).values)
            assert binding is not None
            verdict = {"binding_digest": request.binding_digest, **binding, **resume}
            await compiled.aupdate_state(
                cfg, {"pending_approval": None, "approval_resume": verdict}, as_node="agent"
            )
            state = await compiled.ainvoke(None, config=cfg)
        return state


def _registry(*tools: Any) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _ledger(state: AgentState) -> list[tuple[str, str | None, str]]:
    return [(f.tool_name, f.path, f.error_class) for f in state.get("unresolved_failures", [])]


def _completed(state: AgentState) -> bool:
    return compute_completed(
        exit_reason=str(state.get("exit_reason")),
        unresolved_failures=state.get("unresolved_failures", []),
    )


def _tool_messages(state: AgentState) -> list[ToolMessage]:
    return [m for m in state["messages"] if isinstance(m, ToolMessage)]


def _advised(state: AgentState) -> list[ToolMessage]:
    return [m for m in _tool_messages(state) if m.additional_kwargs.get(RECOVERY_ADVISORY_MARK)]


# ---------------------------------------------------------------------------
# ③ Soft failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_soft_failure_as_the_last_call_leaves_the_run_not_completed() -> None:
    state = await _run(
        _calls([_tc("exec_python", "tc-1")]), _registry(_ScriptedTool(outcomes=[_FAILED]))
    )

    assert state.get("exit_reason") == "text_response", "precondition: the model stopped by itself"
    assert _ledger(state) == [("exec_python", None, "unknown")]
    assert _completed(state) is False
    # The wire status is unchanged: the result is still a normal tool result.
    assert [m.status for m in _tool_messages(state)] == ["success"]


@pytest.mark.asyncio
async def test_a_soft_failure_no_longer_erases_an_earlier_failure_of_the_same_tool() -> None:
    """The measured shape: a raised failure, then a retry that "returned" non-zero.

    Read as a success, the second call cleared the first (same key) — the run that
    never got its script to work came out ``completed=true``.
    """
    tool = _ScriptedTool(outcomes=[OSError("sandbox exploded"), _FAILED])
    state = await _run(
        _calls([_tc("exec_python", "tc-1")], [_tc("exec_python", "tc-2")]), _registry(tool)
    )

    assert tool.calls == 2
    assert [f.summary for f in state.get("unresolved_failures", [])] == ["sandbox exploded"]
    assert _completed(state) is False


@pytest.mark.asyncio
async def test_a_later_success_of_the_same_tool_clears_a_soft_failure() -> None:
    """Last-wins per key stays: the script failed, was fixed, and ran clean."""
    tool = _ScriptedTool(outcomes=[_FAILED, _OK])
    state = await _run(
        _calls([_tc("exec_python", "tc-1")], [_tc("exec_python", "tc-2")]), _registry(tool)
    )

    assert _ledger(state) == []
    assert _completed(state) is True


@pytest.mark.asyncio
async def test_a_transient_soft_failure_is_not_recorded() -> None:
    state = await _run(
        _calls([_tc("http", "tc-1")]), _registry(_ScriptedTool(name="http", outcomes=[_TRANSIENT]))
    )

    assert _ledger(state) == []
    assert _completed(state) is True


@pytest.mark.asyncio
async def test_a_transient_soft_failure_does_not_clear_an_earlier_failure() -> None:
    tool = _ScriptedTool(name="http", outcomes=[_FAILED, _TRANSIENT])
    state = await _run(_calls([_tc("http", "tc-1")], [_tc("http", "tc-2")]), _registry(tool))

    assert _ledger(state) == [("http", None, "unknown")]


@pytest.mark.asyncio
async def test_a_soft_failure_gets_no_advisory_and_no_error_count() -> None:
    """Ledger only. The result already says what failed (exit code, status line, the
    server's or the worker's own words), and the ``unknown`` advice would claim "the call
    never got far enough to look" — false for a call that ran. It also stays out of
    ``tool_failures`` (CM-11 escalation) and the error counter."""
    counter = builder_mod._cm_tool_error_total.labels(error_class="unknown", tool="exec_python")
    before = counter._value.get()

    state = await _run(
        _calls([_tc("exec_python", "tc-1")]), _registry(_ScriptedTool(outcomes=[_FAILED]))
    )

    assert _ledger(state) == [("exec_python", None, "unknown")], "precondition: it is recorded"
    assert _advised(state) == []
    assert counter._value.get() == before


@pytest.mark.asyncio
async def test_a_worker_wrap_up_is_recorded_without_an_advisory() -> None:
    """A budget wrap-up is a usable partial answer: "The call failed …" would be wrong."""
    child = _built_worker(
        _FakeGraph(
            result={
                "messages": [AIMessage(content="partial findings")],
                "step_count": 5,
                "exit_reason": "max_steps",
            }
        )
    )
    tool = SpawnWorkerTool(builder=_RecordingWorkerBuilder(built=child), child_depth=1)
    state = await _run(_calls([_tc("spawn_worker", "tc-1", {"task": "a"})]), _registry(tool))

    assert "[worker stopped early" in str(_tool_messages(state)[0].content), "precondition"
    assert _ledger(state) == [("spawn_worker", None, "unknown")]
    assert _advised(state) == []


@pytest.mark.asyncio
async def test_a_refused_spawn_neither_records_nor_clears() -> None:
    """The per-run worker budget only grows, and the refusal tells the model to finish
    with what it has: nothing in the run could settle it, so it is neutral."""
    tool = SpawnWorkerTool(
        builder=_RecordingWorkerBuilder(built=_built_worker(_answer_graph("ok"))), child_depth=1
    )
    budget = WorkerSpawnBudget(max_per_run=1, max_concurrent=4)
    state = await _run(
        _calls(
            [_tc("spawn_worker", "tc-1", {"task": "a"}), _tc("spawn_worker", "tc-2", {"task": "b"})]
        ),
        _registry(tool),
        extra_cfg={"worker_spawn_budget": budget},
    )

    assert "worker budget" in str(_tool_messages(state)[1].content), "precondition: refused"
    assert _ledger(state) == []
    assert _completed(state) is True


@pytest.mark.asyncio
async def test_a_no_predicate_neither_records_nor_clears() -> None:
    """Exit 1 with nothing on stderr is a predicate that said "no" — or ``python x.py 2>&1``
    crashing. Neutral: it must not erase the earlier bash failure."""
    failed = format_sandbox_outcome(
        SandboxOutcome(stdout="", stderr="No such file\n", exit_code=2, timed_out=False), 1_000
    )
    said_no = format_sandbox_outcome(
        SandboxOutcome(stdout="Traceback ...\n", stderr="", exit_code=1, timed_out=False), 1_000
    )
    tool = _ScriptedTool(name="bash", outcomes=[failed, said_no])
    state = await _run(_calls([_tc("bash", "tc-1")], [_tc("bash", "tc-2")]), _registry(tool))

    assert tool.calls == 2
    assert _ledger(state) == [("bash", None, "unknown")]
    assert _completed(state) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "ledger"),
    [
        pytest.param(
            SandboxOutcome(stdout="", stderr="No such file\n", exit_code=2, timed_out=False),
            [("bash", None, "unknown")],
            id="exit-2",
        ),
        # A grep with no match as the last call: the predicate answered "no".
        pytest.param(
            SandboxOutcome(stdout="", stderr="", exit_code=1, timed_out=False),
            [],
            id="silent-predicate",
        ),
        # grep -c with no match prints "0": still a predicate, nothing on stderr.
        pytest.param(
            SandboxOutcome(stdout="0\n", stderr="", exit_code=1, timed_out=False),
            [],
            id="predicate-with-stdout",
        ),
    ],
)
async def test_bash_exit_code_reaches_the_ledger_through_the_real_tool(
    outcome: SandboxOutcome, ledger: list[tuple[str, str | None, str]]
) -> None:
    tool = BashTool(client=RecordingSandboxRuntime(outcome=outcome))
    state = await _run(_calls([_tc("bash", "tc-1", {"command": "grep -q x f"})]), _registry(tool))

    assert _ledger(state) == ledger
    assert _completed(state) is (not ledger)


@pytest.mark.asyncio
async def test_exec_python_exit_code_reaches_the_ledger_through_the_real_tool() -> None:
    outcome = SandboxOutcome(stdout="", stderr="Traceback\n", exit_code=1, timed_out=False)
    tool = ExecPythonTool(client=RecordingSandboxRuntime(outcome=outcome))
    state = await _run(_calls([_tc("exec_python", "tc-1", {"code": "1/0"})]), _registry(tool))

    assert _ledger(state) == [("exec_python", None, "unknown")]
    assert _advised(state) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_only", "recorded"),
    [
        pytest.param(None, True, id="unannotated"),
        # readOnlyHint=true: a lookup answering isError (e.g. "not found") is not debt.
        pytest.param(True, False, id="read-only"),
    ],
)
async def test_mcp_is_error_reaches_the_ledger_unless_read_only(
    read_only: bool | None, recorded: bool
) -> None:
    tool_def = MCPToolDef(
        name="lookup", description="d", input_schema={"type": "object"}, read_only=read_only
    )
    client = RecordingMCPClient(
        tools=(tool_def,), responses={"lookup": "locked"}, error_tools=frozenset({"lookup"})
    )
    tool = MCPTool(client=client, tool_def=tool_def, server_name="crm")
    state = await _run(_calls([_tc(tool.spec.name, "tc-1")]), _registry(tool))

    assert _ledger(state) == ([(tool.spec.name, None, "unknown")] if recorded else [])
    assert _advised(state) == []
    assert _completed(state) is (not recorded)


@dataclass
class _RaiseThenIsError:
    """MCP client double: the first call raises, every later call answers ``isError``."""

    calls: int = 0

    async def list_tools(self) -> Sequence[MCPToolDef]:
        return ()

    async def call_tool(self, name: str, args: Mapping[str, Any]) -> MCPCallResult:
        del name, args
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("crm backend exploded")
        return MCPCallResult(content="no such customer", is_error=True)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_a_read_only_is_error_does_not_clear_an_earlier_failure() -> None:
    """Neutral, not a success: the lookup that raised never got its answer."""
    tool_def = MCPToolDef(
        name="get_customer", description="d", input_schema={"type": "object"}, read_only=True
    )
    client = _RaiseThenIsError()
    tool = MCPTool(client=client, tool_def=tool_def, server_name="crm")
    state = await _run(
        _calls([_tc(tool.spec.name, "tc-1")], [_tc(tool.spec.name, "tc-2")]), _registry(tool)
    )

    assert client.calls == 2
    assert [f.summary for f in state.get("unresolved_failures", [])] == ["crm backend exploded"]
    assert _completed(state) is False


async def _no_allowlist(_tenant_id: UUID | None) -> Sequence[str]:
    return ()


def _http_404() -> httpx.AsyncClient:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="no such record")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "ledger"),
    [
        pytest.param("POST", [("http", None, "unknown")], id="POST-404"),
        # A lookup that found nothing is not undone work (cf. B-164).
        pytest.param("GET", [], id="GET-404"),
    ],
)
async def test_http_404_reaches_the_ledger_only_for_a_changing_method(
    method: str, ledger: list[tuple[str, str | None, str]]
) -> None:
    tool = HTTPTool(allowlist_provider=_no_allowlist, client_factory=_http_404)
    call = _tc("http", "tc-1", {"method": method, "url": "https://api.example.com/r/7"})
    state = await _run(_calls([call]), _registry(tool))

    assert "HTTP 404" in str(_tool_messages(state)[0].content), "precondition: the call ran"
    assert _ledger(state) == ledger
    assert _completed(state) is (not ledger)


# ---------------------------------------------------------------------------
# ⑤ Refused actions — action screening ``block``
# ---------------------------------------------------------------------------


def _screened(judge: _NamedJudge) -> dict[str, Any]:
    return {"action_judge": judge, "action_screen": "block"}


@pytest.mark.asyncio
async def test_a_blocked_action_is_recorded_as_refused() -> None:
    save = _ScriptedTool(name="save_artifact")
    state = await _run(
        _calls([_tc("save_artifact", "tc-1", {"name": "x.md"})], final="I could not save it"),
        _registry(save),
        **_screened(_NamedJudge(blocked=frozenset({"save_artifact"}))),
    )

    assert save.calls == 0, "precondition: the call never ran"
    assert [m.status for m in _tool_messages(state)] == ["error"], "the block message is unchanged"
    assert _ledger(state) == [("save_artifact", "x.md", "blocked_by_policy")]
    assert _completed(state) is False


@pytest.mark.asyncio
async def test_only_the_misaligned_call_of_a_blocked_turn_is_recorded() -> None:
    """The rest of the turn was not run because of it — not run is not failed (B-76)."""
    state = await _run(
        _calls([_tc("send_note", "tc-1"), _tc("save_artifact", "tc-2", {"name": "x.md"})]),
        _registry(_ScriptedTool(name="send_note"), _ScriptedTool(name="save_artifact")),
        **_screened(_NamedJudge(blocked=frozenset({"save_artifact"}))),
    )

    assert len(_tool_messages(state)) == 2, "precondition: the whole turn was denied"
    assert _ledger(state) == [("save_artifact", "x.md", "blocked_by_policy")]


@pytest.mark.asyncio
async def test_a_blocked_read_only_call_stays_out_of_the_ledger() -> None:
    """Same rule as any read-only failure (B-164): a read produces no deliverable."""
    state = await _run(
        _calls([_tc("web_search", "tc-1")]),
        _registry(_ScriptedTool(name="web_search", read_only=True)),
        **_screened(_NamedJudge(blocked=frozenset({"web_search"}))),
    )

    assert [m.status for m in _tool_messages(state)] == ["error"], "precondition: blocked"
    assert _ledger(state) == []


@pytest.mark.asyncio
async def test_a_blocked_write_is_cleared_by_a_later_write_of_the_same_artifact() -> None:
    """Keyed by the resource, like any write failure: the artifact got written after all."""
    save = _ScriptedTool(name="save_artifact")
    state = await _run(
        _calls(
            [_tc("save_artifact", "tc-1", {"name": "x.md"})],
            [_tc("save_artifact", "tc-2", {"name": "x.md"})],
        ),
        _registry(save),
        **_screened(_NamedJudge(blocked=frozenset({"save_artifact"}), times=1)),
    )

    assert save.calls == 1, "precondition: the second call ran"
    assert _ledger(state) == []
    assert _completed(state) is True


# ---------------------------------------------------------------------------
# ⑤ Refused actions — a rejected approval request that does not end the run
# ---------------------------------------------------------------------------


def _ask(call_id: str, kind: str = "risk_confirmation") -> dict[str, Any]:
    return _tc(
        ASK_FOR_APPROVAL_TOOL,
        call_id,
        {"reason_kind": kind, "action_summary": "delete the old records"},
    )


_REJECT = {"decision": "reject", "modified_args": None, "reason": "no"}
_APPROVE = {"decision": "approve", "modified_args": None}


@pytest.mark.asyncio
async def test_a_rejected_ask_for_approval_is_recorded_as_refused() -> None:
    state = await _run_with_verdicts(
        _calls([_ask("tc-1")], final="understood, nothing was deleted"),
        _registry(AskForApprovalTool()),
        [_REJECT],
    )

    assert state.get("approval_outcome") is None, "precondition: the reject did not end the run"
    assert state.get("exit_reason") == "text_response"
    assert _ledger(state) == [(ASK_FOR_APPROVAL_TOOL, None, "blocked_by_policy")]
    assert _completed(state) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["approach_choice", "missing_info", "ambiguous_requirement"])
async def test_a_rejected_question_is_an_answer_not_a_refusal(kind: str) -> None:
    """Asked "plan A or B?", rejected with "use B": the reviewer answered, nothing was refused."""
    state = await _run_with_verdicts(
        _calls([_ask("tc-1", kind)], final="going with plan B"),
        _registry(AskForApprovalTool()),
        [_REJECT],
    )

    assert state.get("approval_outcome") is None, "precondition: the reject did not end the run"
    assert _ledger(state) == []
    assert _completed(state) is True


@pytest.mark.asyncio
async def test_an_unknown_reason_kind_is_read_as_the_request_showed_it() -> None:
    """The approval request coerces an unknown kind to ``risk_confirmation`` — the
    ledger reads the same kind the reviewer was shown."""
    state = await _run_with_verdicts(
        _calls([_ask("tc-1", "whatever")]), _registry(AskForApprovalTool()), [_REJECT]
    )

    assert _ledger(state) == [(ASK_FOR_APPROVAL_TOOL, None, "blocked_by_policy")]


@pytest.mark.asyncio
async def test_approving_the_question_later_clears_the_rejection() -> None:
    state = await _run_with_verdicts(
        _calls([_ask("tc-1")], [_ask("tc-2")]),
        _registry(AskForApprovalTool()),
        [_REJECT, _APPROVE],
    )

    assert state.get("exit_reason") == "text_response"
    assert _ledger(state) == []
    assert _completed(state) is True


@pytest.mark.asyncio
async def test_approving_a_question_does_not_clear_a_refused_action() -> None:
    """Only an approved ``risk_confirmation`` settles a refused one: approving an
    unrelated "table or paragraph?" says nothing about the delete that was turned down."""
    state = await _run_with_verdicts(
        _calls([_ask("tc-1")], [_ask("tc-2", "approach_choice")]),
        _registry(AskForApprovalTool()),
        [_REJECT, _APPROVE],
    )

    assert state.get("exit_reason") == "text_response"
    assert _ledger(state) == [(ASK_FOR_APPROVAL_TOOL, None, "blocked_by_policy")]
    assert _completed(state) is False


@pytest.mark.asyncio
async def test_the_refused_call_is_the_one_the_request_showed() -> None:
    """The judge picked the second call; the first (a read, aligned) is not the refusal."""
    save = _ScriptedTool(name="save_artifact")
    state = await _run_with_verdicts(
        _calls([_tc("web_search", "tc-1"), _tc("save_artifact", "tc-2", {"name": "x.md"})]),
        _registry(_ScriptedTool(name="web_search", read_only=True), save),
        [_REJECT],
        action_judge=_NamedJudge(blocked=frozenset({"save_artifact"})),
        action_screen="approval",
    )

    assert save.calls == 0
    assert state.get("approval_outcome") is None, "precondition: non-terminal"
    assert _ledger(state) == [("save_artifact", "x.md", "blocked_by_policy")]


@pytest.mark.asyncio
async def test_a_rejected_action_screen_approval_is_recorded_as_refused() -> None:
    """``action_screen: approval`` pauses on the judge's pick, which need not be a gated
    tool — the reject is non-terminal, and the call it showed never ran."""
    save = _ScriptedTool(name="save_artifact")
    state = await _run_with_verdicts(
        _calls([_tc("save_artifact", "tc-1", {"name": "x.md"})]),
        _registry(save),
        [_REJECT],
        action_judge=_NamedJudge(blocked=frozenset({"save_artifact"})),
        action_screen="approval",
    )

    assert save.calls == 0
    assert state.get("approval_outcome") is None, "precondition: non-terminal"
    assert _ledger(state) == [("save_artifact", "x.md", "blocked_by_policy")]


@pytest.mark.asyncio
async def test_a_call_withheld_by_an_approval_stays_out_of_the_ledger() -> None:
    """B-76: the approval released only the call it showed; the other one was not run."""
    email = _ScriptedTool(name="send_email")
    save = _ScriptedTool(name="save_artifact")
    state = await _run_with_verdicts(
        _calls([_tc("send_email", "tc-1"), _tc("save_artifact", "tc-2", {"name": "x.md"})]),
        _registry(email, save),
        [_APPROVE],
        approval_required_tools=frozenset({"send_email"}),
    )

    assert (email.calls, save.calls) == (1, 0), "precondition: only the approved call ran"
    assert _ledger(state) == []
    assert _completed(state) is True


@pytest.mark.asyncio
async def test_a_withheld_call_does_not_clear_an_earlier_failure_of_its_key() -> None:
    """Not run is not failed — and not succeeded either: an earlier failed write of
    ``x.md`` stays when the retry is withheld by the approval of another call."""
    email = _ScriptedTool(name="send_email")
    save = _ScriptedTool(name="save_artifact", outcomes=[OSError("disk full"), _OK])
    state = await _run_with_verdicts(
        _calls(
            [_tc("save_artifact", "tc-1", {"name": "x.md"})],
            [_tc("send_email", "tc-2"), _tc("save_artifact", "tc-3", {"name": "x.md"})],
        ),
        _registry(email, save),
        [_APPROVE],
        approval_required_tools=frozenset({"send_email"}),
    )

    assert (email.calls, save.calls) == (1, 1), "precondition: the retry was withheld"
    assert _ledger(state) == [("save_artifact", "x.md", "mutation_not_landed")]
    assert _completed(state) is False
