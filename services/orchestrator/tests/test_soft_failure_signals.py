"""B-226 ③ — each tool's own structured result says whether the call failed.

A soft failure comes back as a normal :class:`ToolResult` (no exception), so before
B-226 the tools node read it as a success — and a success *clears* an earlier
failure of the same ledger key. These pin the mechanical signal each tool sets on
``ToolResult.failed``, and whether it is neutral (``failure_transient``: neither
records nor clears). The tools-node side is ``test_soft_failure_ledger.py``.

Only structured fields are read (exit code, ``isError`` + ``readOnlyHint``, HTTP
status + method, the child's own exit), never the text of a result. Ties go to
under-reporting (B-84).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from orchestrator.errors import MaxStepsExceededError
from orchestrator.tools import HTTPTool, SandboxOutcome, ToolContext, ToolResult
from orchestrator.tools.error_classifier import ClassifiedToolError
from orchestrator.tools.mcp import MCPTool, MCPToolDef, RecordingMCPClient
from orchestrator.tools.sandbox import format_sandbox_outcome
from orchestrator.tools.spawn_worker import WorkerSpawnBudget
from orchestrator.tools.subagent import SubAgentTool

from .test_child_run_approval import _pending
from .test_child_run_exits import _HangingGraph
from .test_child_run_exits import _run as _run_child
from .test_delegation_gate import _SUB, _FakeGate, _RecordingBuilder
from .test_delegation_gate import _answer_graph as _gate_answer_graph
from .test_delegation_gate import _built as _built_gate_child
from .test_http_tool import _allowlist, _tenant_ctx
from .test_spawn_worker import _answer_graph, _ctx, _RecordingWorkerBuilder, _tool
from .test_spawn_worker import _built as _built_worker
from .test_worker_event_bridge import _collecting_ctx, _StreamingGraph

_OK = (False, False)
_FAILED = (True, False)
_NEUTRAL = (True, True)


def _soft(result: ToolResult) -> tuple[bool, bool]:
    return result.failed, result.failure_transient


# ---------------------------------------------------------------------------
# ToolResult — ``failure_transient`` only means something on a failed result
# ---------------------------------------------------------------------------


def test_transient_without_failed_is_rejected() -> None:
    with pytest.raises(ValueError, match="failed"):
        ToolResult(content="x", failure_transient=True)


def test_a_plain_result_is_not_failed() -> None:
    assert _soft(ToolResult(content="x")) == _OK


# ---------------------------------------------------------------------------
# exec_python / bash — exit code and timeout (shared ``format_sandbox_outcome``)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "stderr", "exit_code", "timed_out", "expected"),
    [
        pytest.param("ok\n", "", 0, False, _OK, id="exit-0"),
        pytest.param("", "", 0, False, _OK, id="exit-0-no-output"),
        pytest.param("", "boom\n", 2, False, _FAILED, id="exit-2"),
        pytest.param("", "", 127, False, _FAILED, id="exit-127-no-output"),
        pytest.param("", "Traceback ...\n", 1, False, _FAILED, id="exit-1-with-stderr"),
        pytest.param("partial\n", "warn\n", 1, False, _FAILED, id="exit-1-with-both"),
        # A predicate that answered "no": grep -q / test print nothing, grep -c prints
        # "0", diff prints the difference — none of them write to stderr. Neutral: it
        # neither records nor clears (``python x.py 2>&1`` crashing with exit 1 looks
        # the same and must not erase an earlier failure).
        pytest.param("", "", 1, False, _NEUTRAL, id="silent-predicate"),
        pytest.param("0\n", "", 1, False, _NEUTRAL, id="predicate-with-stdout"),
        pytest.param("", "", 1, True, _FAILED, id="predicate-but-timed-out"),
        pytest.param("0\n", "", 2, False, _FAILED, id="exit-2-stdout-only"),
        pytest.param("", "", -1, True, _FAILED, id="timeout"),
    ],
)
def test_sandbox_outcome_follows_exit_code_and_timeout(
    stdout: str, stderr: str, exit_code: int, timed_out: bool, expected: tuple[bool, bool]
) -> None:
    outcome = SandboxOutcome(stdout=stdout, stderr=stderr, exit_code=exit_code, timed_out=timed_out)

    assert _soft(format_sandbox_outcome(outcome, output_char_cap=1_000)) == expected


# ---------------------------------------------------------------------------
# MCP — the server's own ``isError``, unless the server marks the tool read-only
# ---------------------------------------------------------------------------


def _mcp_tool(*, is_error: bool, read_only: bool | None) -> MCPTool:
    tool_def = MCPToolDef(
        name="lookup", description="d", input_schema={"type": "object"}, read_only=read_only
    )
    client = RecordingMCPClient(
        tools=(tool_def,),
        responses={"lookup": "record 7 is locked"},
        error_tools=frozenset({"lookup"}) if is_error else frozenset(),
    )
    return MCPTool(client=client, tool_def=tool_def, server_name="crm")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_error", "read_only", "expected"),
    [
        pytest.param(True, None, _FAILED, id="isError-unannotated"),
        pytest.param(True, False, _FAILED, id="isError-writes"),
        # readOnlyHint=true: a lookup that failed is not undone work, nor a success —
        # neutral, like a native read-only failure (B-164).
        pytest.param(True, True, _NEUTRAL, id="isError-read-only"),
        pytest.param(False, True, _OK, id="ok-read-only"),
        pytest.param(False, None, _OK, id="ok"),
    ],
)
async def test_mcp_is_error_fails_unless_the_tool_is_read_only(
    is_error: bool, read_only: bool | None, expected: tuple[bool, bool]
) -> None:
    tool = _mcp_tool(is_error=is_error, read_only=read_only)

    result = await tool.call({}, ctx=ToolContext(tenant_id=uuid4()))

    assert result.meta["is_error"] is is_error
    assert _soft(result) == expected


# ---------------------------------------------------------------------------
# http — response status, only for a method that changes something
# ---------------------------------------------------------------------------


def _status_client(status: int) -> Callable[[], httpx.AsyncClient]:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="body")

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)

    return factory


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "status", "expected"),
    [
        pytest.param("POST", 200, _OK, id="POST-200"),
        pytest.param("POST", 302, _OK, id="POST-302"),
        pytest.param("POST", 404, _FAILED, id="POST-404"),
        pytest.param("PUT", 409, _FAILED, id="PUT-409"),
        pytest.param("PATCH", 429, _FAILED, id="PATCH-429"),
        pytest.param("DELETE", 400, _FAILED, id="DELETE-400"),
        pytest.param("POST", 500, _NEUTRAL, id="POST-500-transient"),
        pytest.param("DELETE", 503, _NEUTRAL, id="DELETE-503-transient"),
        # A lookup that found nothing / failed is not undone work (cf. B-164); the http
        # spec has no read-only flag, so this is decided per method.
        pytest.param("GET", 404, _OK, id="GET-404"),
        pytest.param("GET", 503, _OK, id="GET-503"),
        pytest.param("HEAD", 404, _OK, id="HEAD-404"),
        pytest.param("OPTIONS", 405, _OK, id="OPTIONS-405"),
        pytest.param(None, 404, _OK, id="default-method-is-GET"),
    ],
)
async def test_http_status_400_and_up_fails_only_a_changing_method(
    method: str | None, status: int, expected: tuple[bool, bool]
) -> None:
    tool = HTTPTool(allowlist_provider=_allowlist(), client_factory=_status_client(status))
    args: dict[str, Any] = {"url": "https://api.example.com/x"}
    if method is not None:
        args["method"] = method

    result = await tool.call(args, ctx=_tenant_ctx())

    assert result.meta["status_code"] == status
    assert _soft(result) == expected


# ---------------------------------------------------------------------------
# Delegated child runs — every exit that did not finish the job
# ---------------------------------------------------------------------------


def _child(final: dict[str, Any], *, raise_with: BaseException | None = None) -> _StreamingGraph:
    return _StreamingGraph([], final, raise_with=raise_with)


def _ledger_entry() -> ClassifiedToolError:
    return ClassifiedToolError(
        tool_name="exec_python", error_class="unknown", summary="s", retryable=False, advice="a"
    )


_ANSWER = [AIMessage(content="the answer")]

_CHILD_EXITS = [
    pytest.param(
        {"messages": _ANSWER, "step_count": 2, "exit_reason": "text_response"},
        False,
        id="natural-answer",
    ),
    pytest.param(
        {
            "messages": _ANSWER,
            "step_count": 2,
            "exit_reason": "text_response",
            "unresolved_failures": [],
        },
        False,
        id="natural-answer-empty-ledger",
    ),
    # No exit record (no B-85 stamp) = no verdict, not a failure.
    pytest.param({"messages": _ANSWER, "step_count": 2}, False, id="no-exit-record"),
    pytest.param(
        {
            "messages": _ANSWER,
            "step_count": 2,
            "exit_reason": "text_response",
            "unresolved_failures": [_ledger_entry()],
        },
        True,
        id="child-not-completed",
    ),
    pytest.param(
        {"messages": _ANSWER, "step_count": 5, "exit_reason": "max_steps"},
        True,
        id="budget-wrap-up",
    ),
    pytest.param(
        {"messages": _ANSWER, "step_count": 5, "exit_reason": "token_budget"},
        True,
        id="token-budget-wrap-up",
    ),
    pytest.param({"messages": [HumanMessage(content="t")], "step_count": 1}, True, id="no-answer"),
    pytest.param(
        {"messages": _ANSWER, "step_count": 1, "pending_approval": _pending()},
        True,
        id="approval-blocked",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("final", "failed"), _CHILD_EXITS)
async def test_child_run_failed_follows_its_exit(final: dict[str, Any], failed: bool) -> None:
    result = await _run_child(_child(final), _collecting_ctx([]))

    assert _soft(result) == (failed, False)


@pytest.mark.asyncio
async def test_child_that_hits_its_step_limit_is_failed() -> None:
    graph = _child({}, raise_with=MaxStepsExceededError(step_count=5, max_steps=5))

    result = await _run_child(graph, _collecting_ctx([]))

    assert result.meta.get("subagent_max_steps") is True
    assert _soft(result) == _FAILED


@pytest.mark.asyncio
async def test_child_stopped_by_the_run_deadline_is_failed() -> None:
    graph = _HangingGraph(progress={"messages": [AIMessage(content="half")], "step_count": 1})
    ctx = ToolContext(tenant_id=uuid4(), run_id=uuid4(), deadline_at=time.monotonic() + 0.05)

    result = await _run_child(graph, ctx)

    assert result.meta.get("subagent_deadline") is True
    assert _soft(result) == _FAILED


# ---------------------------------------------------------------------------
# Delegation refusals — neutral: a saturated gate is jitter, and the per-run budget
# only grows, so a refusal could never be settled within the run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_refused_by_the_per_run_budget_is_neutral() -> None:
    budget = WorkerSpawnBudget(max_per_run=1, max_concurrent=4)
    tool = _tool(_RecordingWorkerBuilder(built=_built_worker(_answer_graph("ok"))))
    ctx = _ctx(worker_spawn_budget=budget)

    first = await tool.call({"task": "a"}, ctx=ctx)
    refused = await tool.call({"task": "b"}, ctx=ctx)

    assert _soft(first) == _OK
    assert refused.meta.get("spawn_worker_blocked") is True
    assert _soft(refused) == _NEUTRAL


@pytest.mark.asyncio
async def test_spawn_refused_by_a_saturated_gate_is_neutral() -> None:
    tool = _tool(_RecordingWorkerBuilder(built=_built_worker(_answer_graph("ok"))))

    result = await tool.call({"task": "x"}, ctx=_ctx(delegation_gate=_FakeGate(admit=False)))

    assert result.meta["reason"] == "global_gate_timeout"
    assert _soft(result) == _NEUTRAL


@pytest.mark.asyncio
async def test_subagent_refused_by_a_saturated_gate_is_neutral() -> None:
    builder = _RecordingBuilder(built=_built_gate_child(_gate_answer_graph("ok")))
    tool = SubAgentTool(subagent=_SUB, builder=builder, child_depth=1)

    result = await tool.call({"task": "x"}, ctx=_ctx(delegation_gate=_FakeGate(admit=False)))

    assert result.meta["reason"] == "global_gate_timeout"
    assert _soft(result) == _NEUTRAL
