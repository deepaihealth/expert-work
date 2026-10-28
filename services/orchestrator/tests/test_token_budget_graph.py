"""B3 — token budget graph wiring (spec:
docs/superpowers/specs/2026-07-20-token-budget-breaker-design.md).

Drives ``agent_node`` through the compiled graph with a shared
:class:`TokenBudget` + guard sink injected via
``config["configurable"]`` (same channel as the compaction / worker-event
sinks — see ``test_precompaction_flush_wiring.py``). Covers: per-step
accumulation, 80% warning (once, with a prompt notice on every step after),
trip into graceful wrap-up (token AND the pre-existing max_steps /
no_progress guards, all guard-visible now), the ``limit=0`` / unwired
no-op path, sink-failure resilience, and ``ToolContext`` / ``_child_config``
propagation.

Also covers the code-review fix for two accumulation-point leaks (the
per-step ``token_budget.add(...)`` used to read only the FINAL response's
``usage_metadata``, after screen/judge/structured post-processing could
already have replaced it):

* a response the PI-2 output screen blocks is swapped for a fresh
  ``REFUSAL_TEXT`` ``AIMessage`` with no ``usage_metadata`` — the real
  billed primary call's tokens must still land in the budget;
* an RT-ADR-4 structured resend is a SECOND real call — both the
  non-conforming primary candidate and the resend must be counted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from expert_work.common.output_screen import REFUSAL_TEXT
from expert_work.protocol import StructuredOutputSpec
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import AgentState, GraphRunner, ToolRegistry, build_react_graph
from orchestrator.graph_builder.builder import _build_tool_context
from orchestrator.tools._child_run import _child_config
from orchestrator.tools._guards import GUARD_SINK_KEY, TOKEN_BUDGET_KEY, TokenBudget
from orchestrator.tools.registry import ToolContext, ToolResult, ToolSpec


@dataclass
class _ScriptedLLM:
    """LLMCaller stub: returns ``responses[call_index]`` on each invocation.

    Records the exact ``messages`` prompt + tool count seen on every call so
    tests can assert on wrap-up / warning prompt injection.
    """

    responses: list[AIMessage]
    calls: int = 0
    seen_messages: list[list[BaseMessage]] = field(default_factory=list)
    seen_tool_counts: list[int] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        idx = self.calls
        self.calls += 1
        self.seen_messages.append(list(messages))
        self.seen_tool_counts.append(len(list(tools)))
        if idx >= len(self.responses):
            raise RuntimeError(f"scripted LLM ran out of responses at call {idx}")
        return self.responses[idx]


@dataclass
class _ScriptedStructuredLLM:
    """LLMCaller stub accepting ``output_schema`` (RT-ADR-4 resend path) —
    ``_ScriptedLLM`` above doesn't take that kwarg since it's only exercised
    off the unstructured path."""

    responses: list[AIMessage]
    calls: int = 0

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AIMessage:
        del messages, tools, output_schema
        idx = self.calls
        self.calls += 1
        if idx >= len(self.responses):
            raise RuntimeError(f"scripted LLM ran out of responses at call {idx}")
        return self.responses[idx]


@dataclass
class _EchoTool:
    name: str = "probe"
    dispatched: int = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="probe")

    async def call(self, args: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        self.dispatched += 1
        return ToolResult(content="ok")


def _tc(call_id: str) -> dict[str, Any]:
    return {"name": "probe", "args": {"q": "x"}, "id": call_id, "type": "tool_call"}


def _usage(total: int) -> dict[str, int]:
    """A minimal ``usage_metadata`` shape whose ``usage_total`` is ``total``."""
    return {"input_tokens": total, "output_tokens": 0, "total_tokens": total}


async def _invoke(
    graph,
    payload: dict[str, Any],
    *,
    thread_id: str,
    token_budget: TokenBudget | None = None,
    guard_sink: Any = None,
) -> AgentState:
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        configurable: dict[str, Any] = {"thread_id": thread_id}
        if token_budget is not None:
            configurable[TOKEN_BUDGET_KEY] = token_budget
        if guard_sink is not None:
            configurable[GUARD_SINK_KEY] = guard_sink
        cfg: RunnableConfig = {"configurable": configurable}
        return await compiled.ainvoke(payload, config=cfg)


def _sink() -> tuple[list[dict[str, Any]], Any]:
    seen: list[dict[str, Any]] = []

    async def _publish(frame: dict[str, Any]) -> None:
        seen.append(frame)

    return seen, _publish


# ---------------------------------------------------------------------------
# 1) Accumulate + trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accumulate_and_trip_forces_token_wrapup() -> None:
    """limit=100, 60 tokens/step. Step1 → spent=60, step2 → spent=120 → step3
    enters wrap-up: prompt carries the token wrap-up wording, the wrap-up
    turn is bound with no tools and its response has no tool_calls (clean
    END), and the guard sink gets one tripped frame with spent/limit."""
    registry = ToolRegistry()
    registry.register(_EchoTool())
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("a")], usage_metadata=_usage(60)),
            AIMessage(content="", tool_calls=[_tc("b")], usage_metadata=_usage(60)),
            AIMessage(content="final answer", usage_metadata=_usage(0)),
        ]
    )
    graph = build_react_graph(llm_caller=llm, tool_registry=registry)
    events, publish = _sink()
    budget = TokenBudget(limit=100)

    state = await _invoke(
        graph,
        {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 20},
        thread_id="tb-trip",
        token_budget=budget,
        guard_sink=publish,
    )

    assert llm.calls == 3
    # Step 3's prompt (the wrap-up turn) carries the token-budget wording and
    # was bound with zero tools.
    assert "token budget" in str(llm.seen_messages[2][-1].content)
    assert llm.seen_tool_counts[2] == 0
    last = state["messages"][-1]
    assert isinstance(last, AIMessage)
    assert not last.tool_calls

    assert events == [
        {
            "kind": "tripped",
            "guard": "token_budget",
            "detail": {"spent": 120, "limit": 100},
        }
    ]


# ---------------------------------------------------------------------------
# 2) 80% warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warning_fires_once_and_annotates_prompt_after_crossing() -> None:
    """limit=1000; three 300-token steps cross 80% (spent=900 after step 3).
    Step 4 fires exactly one warning frame + annotates its prompt with the
    budget notice (system prefix untouched); step 5 re-crosses no new frame."""
    registry = ToolRegistry()
    registry.register(_EchoTool())
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("a")], usage_metadata=_usage(300)),
            AIMessage(content="", tool_calls=[_tc("b")], usage_metadata=_usage(300)),
            AIMessage(content="", tool_calls=[_tc("c")], usage_metadata=_usage(300)),
            AIMessage(content="", tool_calls=[_tc("d")], usage_metadata=_usage(0)),
            AIMessage(content="done", usage_metadata=_usage(0)),
        ]
    )
    graph = build_react_graph(llm_caller=llm, tool_registry=registry)
    events, publish = _sink()
    budget = TokenBudget(limit=1000)
    system = SystemMessage(content="sys prefix")

    state = await _invoke(
        graph,
        {
            "messages": [system, HumanMessage(content="go")],
            "step_count": 0,
            "max_steps": 20,
        },
        thread_id="tb-warn",
        token_budget=budget,
        guard_sink=publish,
    )

    assert llm.calls == 5
    # Step 4 (index 3) is the first call made after crossing 80%.
    step4_prompt = llm.seen_messages[3]
    assert step4_prompt[0] is system
    assert "token budget" in str(step4_prompt[-1].content)
    assert "900" in str(step4_prompt[-1].content)
    # Step 5 still carries the notice (every step after crossing) but does
    # not re-fire the sink.
    step5_prompt = llm.seen_messages[4]
    assert "token budget" in str(step5_prompt[-1].content)

    warnings = [e for e in events if e["kind"] == "warning"]
    assert len(warnings) == 1
    assert warnings[0] == {
        "kind": "warning",
        "guard": "token_budget",
        "detail": {"spent": 900, "limit": 1000},
    }
    assert state["messages"][-1].content == "done"


# ---------------------------------------------------------------------------
# 3) limit=0 / unwired = no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unwired_budget_is_a_pure_noop() -> None:
    """No ``TOKEN_BUDGET_KEY`` injected → zero guard-sink activity and the
    run behaves exactly like the pre-existing (no-budget) react graph path."""
    registry = ToolRegistry()
    registry.register(_EchoTool())
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("a")], usage_metadata=_usage(999_999)),
            AIMessage(content="all done", usage_metadata=_usage(999_999)),
        ]
    )
    graph = build_react_graph(llm_caller=llm, tool_registry=registry)
    events, publish = _sink()

    state = await _invoke(
        graph,
        {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 20},
        thread_id="tb-off",
        token_budget=None,
        guard_sink=publish,
    )

    assert llm.calls == 2
    assert state["step_count"] == 2
    assert state["messages"][-1].content == "all done"
    assert events == []


# ---------------------------------------------------------------------------
# 4) Old guards (max_steps / no_progress) become guard-visible too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_steps_trip_emits_guard_frame() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("a")], usage_metadata=_usage(1)),
            AIMessage(content="", tool_calls=[_tc("b")], usage_metadata=_usage(1)),
            AIMessage(content="wrapped up", usage_metadata=_usage(1)),
        ]
    )
    graph = build_react_graph(llm_caller=llm, tool_registry=registry)
    events, publish = _sink()

    await _invoke(
        graph,
        {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 2},
        thread_id="tb-maxsteps",
        guard_sink=publish,
    )

    # B-226 ② — with 2 steps the whole run sits in the step-notice window, so
    # the one-shot warning lands on the first step, before the trip.
    assert events == [
        {"kind": "warning", "guard": "max_steps", "detail": {"steps": 0, "max": 2}},
        {"kind": "tripped", "guard": "max_steps", "detail": {"steps": 2, "max": 2}},
    ]


@pytest.mark.asyncio
async def test_no_progress_trip_emits_guard_frame() -> None:
    """Mirrors ``test_no_progress_stop.test_streak_at_threshold_forces_toolless_wrapup``
    — a streak already at threshold on entry forces the tool-less wrap-up."""
    tool = _EchoTool()
    registry = ToolRegistry()
    registry.register(tool)
    llm = _ScriptedLLM(responses=[AIMessage(content="here is what I have")])
    graph = build_react_graph(llm_caller=llm, tool_registry=registry)
    events, publish = _sink()

    await _invoke(
        graph,
        {
            "messages": [HumanMessage(content="go")],
            "step_count": 0,
            "max_steps": 20,
            "loop_trip_count": 2,
            "max_no_progress": 2,
        },
        thread_id="tb-noprogress",
        guard_sink=publish,
    )

    assert events == [
        {"kind": "tripped", "guard": "no_progress", "detail": {"streak": 2, "max": 2}}
    ]
    assert tool.dispatched == 0


# ---------------------------------------------------------------------------
# 5) Wrap-up turn does not re-trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tripped_wrapup_ends_cleanly_without_retrigger() -> None:
    """The trip fires on the very first step (usage already >= limit); the
    forced wrap-up turn ends the graph (no tool_calls survive) — exactly one
    tripped frame, exactly two LLM calls (normal turn + wrap-up), no replay."""
    registry = ToolRegistry()
    registry.register(_EchoTool())
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("a")], usage_metadata=_usage(60)),
            AIMessage(content="wrap answer", usage_metadata=_usage(0)),
        ]
    )
    graph = build_react_graph(llm_caller=llm, tool_registry=registry)
    events, publish = _sink()
    budget = TokenBudget(limit=50)

    state = await _invoke(
        graph,
        {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 20},
        thread_id="tb-no-retrigger",
        token_budget=budget,
        guard_sink=publish,
    )

    assert llm.calls == 2
    assert events == [
        {"kind": "tripped", "guard": "token_budget", "detail": {"spent": 60, "limit": 50}}
    ]
    assert state["messages"][-1].content == "wrap answer"


# ---------------------------------------------------------------------------
# 6) Sink failure does not break the run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sink_failure_does_not_break_run() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    llm = _ScriptedLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("a")], usage_metadata=_usage(60)),
            AIMessage(content="wrap answer", usage_metadata=_usage(0)),
        ]
    )
    graph = build_react_graph(llm_caller=llm, tool_registry=registry)

    async def _bad_sink(frame: dict[str, Any]) -> None:
        del frame
        raise RuntimeError("sink boom")

    budget = TokenBudget(limit=50)
    state = await _invoke(
        graph,
        {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 20},
        thread_id="tb-sink-fail",
        token_budget=budget,
        guard_sink=_bad_sink,
    )

    assert state["messages"][-1].content == "wrap answer"


# ---------------------------------------------------------------------------
# 7) ToolContext / _child_config propagation
# ---------------------------------------------------------------------------


def test_build_tool_context_reads_token_budget_and_guard_sink() -> None:
    budget = TokenBudget(limit=100)

    async def _sink_fn(_frame: dict[str, Any]) -> None:
        return None

    config: RunnableConfig = {"configurable": {TOKEN_BUDGET_KEY: budget, GUARD_SINK_KEY: _sink_fn}}
    ctx = _build_tool_context(config)

    assert ctx.token_budget is budget
    assert ctx.guard_sink is _sink_fn


def test_build_tool_context_defaults_none_when_absent() -> None:
    ctx = _build_tool_context({"configurable": {}})
    assert ctx.token_budget is None
    assert ctx.guard_sink is None


def test_child_config_forwards_same_token_budget_and_guard_sink() -> None:
    budget = TokenBudget(limit=100)

    async def _sink_fn(_frame: dict[str, Any]) -> None:
        return None

    ctx = ToolContext(tenant_id=uuid4(), token_budget=budget, guard_sink=_sink_fn)

    child_config = _child_config(ctx, sub_thread_id=uuid4(), sub_run_id=uuid4())

    configurable = child_config["configurable"]
    assert configurable[TOKEN_BUDGET_KEY] is budget
    assert configurable[GUARD_SINK_KEY] is _sink_fn


def test_child_config_omits_keys_when_absent() -> None:
    ctx = ToolContext(tenant_id=uuid4())
    child_config = _child_config(ctx, sub_thread_id=uuid4(), sub_run_id=uuid4())
    configurable = child_config["configurable"]
    assert TOKEN_BUDGET_KEY not in configurable
    assert GUARD_SINK_KEY not in configurable


# ---------------------------------------------------------------------------
# 8) Code-review fix — accumulation point moved off the FINAL response
#    (see module docstring: screen-blocked replacement / structured resend)
# ---------------------------------------------------------------------------

_STRUCT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"score": {"type": "integer"}},
    "required": ["score"],
    "additionalProperties": False,
}
_STRUCT_SPEC = StructuredOutputSpec(schema=_STRUCT_SCHEMA, name="verdict")


@pytest.mark.asyncio
async def test_screen_blocked_response_still_counts_original_usage() -> None:
    """PI-2 output screening (Finding 1): a flagged reply is swapped for a
    fresh ``REFUSAL_TEXT`` ``AIMessage`` that carries no ``usage_metadata``.
    The primary call still really happened and was really billed — its
    tokens must land in the budget rather than vanish because the reply got
    blocked afterward."""
    registry = ToolRegistry()
    registry.register(_EchoTool())
    # Split literal so push protection sees no contiguous provider token.
    leak = "Sure, the key is sk-" + "ant-api03-AbCdEf012345678901234567"
    llm = _ScriptedLLM(responses=[AIMessage(content=leak, usage_metadata=_usage(42))])
    graph = build_react_graph(llm_caller=llm, tool_registry=registry, output_screen=True)
    budget = TokenBudget(limit=100_000)

    state = await _invoke(
        graph,
        {"messages": [HumanMessage(content="hi")], "step_count": 0, "max_steps": 5},
        thread_id="tb-screen-blocked",
        token_budget=budget,
    )

    assert llm.calls == 1
    last = state["messages"][-1]
    assert isinstance(last, AIMessage)
    assert last.content == REFUSAL_TEXT
    # The pre-fix code read the FINAL response's usage_metadata (the refusal
    # has none) and would have left spent at 0.
    assert budget.spent == 42


@pytest.mark.asyncio
async def test_structured_resend_counts_both_calls() -> None:
    """RT-ADR-4 structured resend (Finding 2): a non-conforming primary
    candidate triggers ONE schema-enforced resend — a second real billed
    call. Both must be counted, not just the resend's (previously the
    only one visible to the budget)."""
    registry = ToolRegistry()
    llm = _ScriptedStructuredLLM(
        responses=[
            AIMessage(content="not json", usage_metadata=_usage(50)),
            AIMessage(content='{"score": 4}', usage_metadata=_usage(30)),
        ]
    )
    graph = build_react_graph(llm_caller=llm, tool_registry=registry, output_schema=_STRUCT_SPEC)
    budget = TokenBudget(limit=100_000)

    state = await _invoke(
        graph,
        {"messages": [HumanMessage(content="rate this")], "step_count": 0, "max_steps": 20},
        thread_id="tb-structured-resend",
        token_budget=budget,
    )

    assert llm.calls == 2
    # 50 (non-conforming primary candidate) + 30 (schema-enforced resend).
    assert budget.spent == 80
    last = state["messages"][-1]
    assert isinstance(last, AIMessage)
    assert last.additional_kwargs["parsed"] == {"score": 4}
