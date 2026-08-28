"""B-35 —— plan_first 结构化分发轮(dispatch turn)tests.

spec: docs/superpowers/specs/2026-08-28-plan-first-execution-design.md §4.2。

判据:``plan_first`` 开 ∧ plan 存在 ``execution=delegate`` 且未完成的步骤
∧ 该 plan 身份(goal + 步骤描述 + execution 标注,不含 status)未分发过
→ 本 agent 轮为分发轮:工具收窄为 {spawn_worker, update_plan} + 注入
hidden 指令。模型纯文本抗拒 → 重试一次(硬指令)→ 仍抗拒 → 降级
(恢复满工具,run 继续,永不因分发轮死掉)。``plan_first`` 关(默认)
= 零收窄零注入零通道写入。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
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
from orchestrator.tools.spawn_worker import SPAWN_WORKER_TOOL_NAME
from orchestrator.tools.update_plan import UpdatePlanTool

_DISPATCH_MARKER = "[structured dispatch]"
_RETRY_MARKER = "[structured dispatch reminder]"
_DEGRADED_MARKER = "[structured dispatch skipped]"
_NARROWED = {SPAWN_WORKER_TOOL_NAME, "update_plan"}


@dataclass
class _RecordingLLM:
    """Scripted LLM that records the tool names + messages of every call."""

    responses: list[AIMessage]
    calls: int = 0
    seen_tools: list[list[str]] = field(default_factory=list)
    seen_messages: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
    ) -> AIMessage:
        self.seen_tools.append([t.name for t in tools])
        self.seen_messages.append(list(messages))
        idx = self.calls
        self.calls += 1
        if idx >= len(self.responses):
            raise RuntimeError(f"scripted LLM ran out at call {idx}")
        return self.responses[idx]


@dataclass
class _StubTool:
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


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(UpdatePlanTool())
    registry.register(_StubTool(name="noop"))
    registry.register(_StubTool(name=SPAWN_WORKER_TOOL_NAME))
    return registry


async def _run(
    llm: _RecordingLLM,
    *,
    thread_id: str,
    plan_first: bool = True,
    initial: dict[str, Any] | None = None,
) -> AgentState:
    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(
            build_react_graph(llm_caller=llm, tool_registry=_registry(), plan_first=plan_first)
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        return await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="start")],
                "step_count": 0,
                "max_steps": 10,
                **(initial or {}),
            },
            config=cfg,
        )


def _hidden_with(state: AgentState, marker: str) -> list[HumanMessage]:
    return [
        m
        for m in state["messages"]
        if isinstance(m, HumanMessage)
        and marker in str(m.content)
        and m.additional_kwargs.get("expert_work_hide_from_ui") is True
    ]


_DELEGATE_STEPS: list[Any] = [
    {"description": "fetch template A details", "execution": "delegate"},
    {"description": "fetch customer B trajectory", "execution": "delegate"},
    {"description": "decide and write the report", "execution": "inline"},
]


# ---------------------------------------------------------------------------
# Happy path — dispatch fires, narrows tools, then restores
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_turn_narrows_tools_and_injects_instruction() -> None:
    llm = _RecordingLLM(
        responses=[
            AIMessage(content="", tool_calls=[_update_plan_call("tc-1", _DELEGATE_STEPS)]),
            AIMessage(
                content="",
                tool_calls=[
                    _tc(SPAWN_WORKER_TOOL_NAME, "tc-2", {"task": "fetch template A"}),
                    _tc(SPAWN_WORKER_TOOL_NAME, "tc-3", {"task": "fetch customer B"}),
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    state = await _run(llm, thread_id="dispatch-happy")

    # Call #2 is the dispatch turn: tools narrowed to spawn_worker + update_plan.
    assert set(llm.seen_tools[1]) == _NARROWED
    # Call #1 (before a plan exists) and #3 (after dispatch) carry the full set.
    assert "noop" in llm.seen_tools[0]
    assert "noop" in llm.seen_tools[2]
    # Exactly one hidden dispatch instruction, naming the delegate steps.
    instructions = _hidden_with(state, _DISPATCH_MARKER)
    assert len(instructions) == 1
    assert "fetch template A details" in str(instructions[0].content)
    assert "self-contained" in str(instructions[0].content)
    # Dedupe hash persisted; the turn is no longer active at run end.
    assert state.get("plan_first_dispatch_plan_hash")
    assert not state.get("plan_first_dispatch_active")


@pytest.mark.asyncio
async def test_dispatch_not_triggered_without_delegate_steps() -> None:
    llm = _RecordingLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[_update_plan_call("tc-1", ["step a", "step b", "step c"])],
            ),
            AIMessage(content="done"),
        ]
    )
    state = await _run(llm, thread_id="dispatch-none")

    assert all("noop" in tools for tools in llm.seen_tools)
    assert _hidden_with(state, _DISPATCH_MARKER) == []
    assert state.get("plan_first_dispatch_plan_hash") is None


# ---------------------------------------------------------------------------
# Switch off — byte-identical inertness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_first_disabled_is_inert() -> None:
    """默认关:即使 plan 带 delegate 标注也零收窄零注入零通道写入。"""
    llm = _RecordingLLM(
        responses=[
            AIMessage(content="", tool_calls=[_update_plan_call("tc-1", _DELEGATE_STEPS)]),
            AIMessage(content="done"),
        ]
    )
    state = await _run(llm, thread_id="dispatch-off", plan_first=False)

    assert all("noop" in tools for tools in llm.seen_tools)
    assert _hidden_with(state, _DISPATCH_MARKER) == []
    assert state.get("plan_first_dispatch_plan_hash") is None
    assert state.get("plan_first_dispatch_active") is None


# ---------------------------------------------------------------------------
# Dedupe — one dispatch per plan identity; structural replan re-fires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_marking_update_does_not_redispatch() -> None:
    """标 status 进度(hash 不含 status)不再进第二个分发轮。"""
    done_steps = [
        {**s, "status": "completed"} if s["execution"] == "delegate" else s for s in _DELEGATE_STEPS
    ]
    llm = _RecordingLLM(
        responses=[
            AIMessage(content="", tool_calls=[_update_plan_call("tc-1", _DELEGATE_STEPS)]),
            AIMessage(
                content="",
                tool_calls=[_tc(SPAWN_WORKER_TOOL_NAME, "tc-2", {"task": "fetch both"})],
            ),
            AIMessage(content="", tool_calls=[_update_plan_call("tc-3", done_steps, "progress")]),
            AIMessage(content="done"),
        ]
    )
    state = await _run(llm, thread_id="dispatch-dedupe")

    assert len(_hidden_with(state, _DISPATCH_MARKER)) == 1
    # Call #4 (after the progress-marking update) keeps the full tool set.
    assert "noop" in llm.seen_tools[3]


@pytest.mark.asyncio
async def test_structural_replan_with_new_delegate_steps_redispatches() -> None:
    replanned = [
        *_DELEGATE_STEPS,
        {"description": "fetch supplier C records", "execution": "delegate"},
    ]
    llm = _RecordingLLM(
        responses=[
            AIMessage(content="", tool_calls=[_update_plan_call("tc-1", _DELEGATE_STEPS)]),
            AIMessage(
                content="",
                tool_calls=[_tc(SPAWN_WORKER_TOOL_NAME, "tc-2", {"task": "fetch both"})],
            ),
            AIMessage(content="", tool_calls=[_update_plan_call("tc-3", replanned, "replan")]),
            AIMessage(
                content="",
                tool_calls=[_tc(SPAWN_WORKER_TOOL_NAME, "tc-4", {"task": "fetch supplier C"})],
            ),
            AIMessage(content="done"),
        ]
    )
    state = await _run(llm, thread_id="dispatch-replan")

    assert len(_hidden_with(state, _DISPATCH_MARKER)) == 2
    # Both dispatch turns (#2 and #4) narrowed; the final turn restored.
    assert set(llm.seen_tools[1]) == _NARROWED
    assert set(llm.seen_tools[3]) == _NARROWED
    assert "noop" in llm.seen_tools[4]


# ---------------------------------------------------------------------------
# Refusal path — retry once, then degrade; the run never dies here
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_refusal_retries_once_then_degrades() -> None:
    llm = _RecordingLLM(
        responses=[
            AIMessage(content="", tool_calls=[_update_plan_call("tc-1", _DELEGATE_STEPS)]),
            AIMessage(content="I'd rather just answer."),
            AIMessage(content="Still answering in prose."),
            AIMessage(content="final answer"),
        ]
    )
    state = await _run(llm, thread_id="dispatch-degrade")

    # Turn #2 (dispatch) and #3 (retry) are narrowed; #4 (degraded) restores.
    assert set(llm.seen_tools[1]) == _NARROWED
    assert set(llm.seen_tools[2]) == _NARROWED
    assert "noop" in llm.seen_tools[3]
    assert len(_hidden_with(state, _RETRY_MARKER)) == 1
    assert len(_hidden_with(state, _DEGRADED_MARKER)) == 1
    # Run ended normally — the final prose answer is the last message.
    assert str(state["messages"][-1].content) == "final answer"
    assert not state.get("plan_first_dispatch_active")


# ---------------------------------------------------------------------------
# Budget wrap-up wins over dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_exhausted_wins_over_dispatch() -> None:
    plan = Plan(
        goal="g",
        steps=(
            PlanStep(id="1", description="a", execution="delegate"),
            PlanStep(id="2", description="b", execution="delegate"),
        ),
    )
    llm = _RecordingLLM(responses=[AIMessage(content="wrapping up")])
    state = await _run(
        llm,
        thread_id="dispatch-budget",
        initial={"plan": plan, "step_count": 10, "max_steps": 10},
    )

    # The wrap-up turn is tool-less — not narrowed to the dispatch pair.
    assert llm.seen_tools[0] == []
    assert _hidden_with(state, _DISPATCH_MARKER) == []
    assert state.get("plan_first_dispatch_plan_hash") is None


# ---------------------------------------------------------------------------
# Re-mark escape hatch (kimi 真栈发现,2026-08-28) — a dispatch turn that
# re-marks every delegate step inline via update_plan (no spawn_worker at
# all) must count as degraded, or the metric shows a healthy gate that was
# in fact talked around.
# ---------------------------------------------------------------------------


def _counter_value(counter: Any) -> float:
    return float(counter._value.get())


@pytest.mark.asyncio
async def test_remarking_all_delegates_inline_counts_as_degraded() -> None:
    from orchestrator.graph_builder.builder import _plan_first_dispatch_degraded_total

    all_inline = [
        {**s, "execution": "inline"} if s["execution"] == "delegate" else s for s in _DELEGATE_STEPS
    ]
    llm = _RecordingLLM(
        responses=[
            AIMessage(content="", tool_calls=[_update_plan_call("tc-1", _DELEGATE_STEPS)]),
            # Dispatch turn: no spawn_worker — just re-mark everything inline.
            AIMessage(content="", tool_calls=[_update_plan_call("tc-2", all_inline, "remark")]),
            AIMessage(content="done"),
        ]
    )
    before = _counter_value(_plan_first_dispatch_degraded_total)
    state = await _run(llm, thread_id="dispatch-remark-escape")
    after = _counter_value(_plan_first_dispatch_degraded_total)

    assert after == before + 1
    # No retry / no second dispatch turn; the run ends normally.
    assert str(state["messages"][-1].content) == "done"
    assert not state.get("plan_first_dispatch_active")


@pytest.mark.asyncio
async def test_dispatch_followed_by_spawn_does_not_count_degraded() -> None:
    """真派发后 worker 干完、步骤标 completed —— 不算绕过。"""
    from orchestrator.graph_builder.builder import _plan_first_dispatch_degraded_total

    done_steps = [
        {**s, "status": "completed"} if s["execution"] == "delegate" else s for s in _DELEGATE_STEPS
    ]
    llm = _RecordingLLM(
        responses=[
            AIMessage(content="", tool_calls=[_update_plan_call("tc-1", _DELEGATE_STEPS)]),
            AIMessage(
                content="",
                tool_calls=[
                    _tc(SPAWN_WORKER_TOOL_NAME, "tc-2", {"task": "fetch A"}),
                    _update_plan_call("tc-3", done_steps, "progress"),
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    before = _counter_value(_plan_first_dispatch_degraded_total)
    await _run(llm, thread_id="dispatch-spawn-progress")
    assert _counter_value(_plan_first_dispatch_degraded_total) == before


def test_dispatch_instruction_narrows_the_remark_escape() -> None:
    """措辞收紧:re-mark 只限写操作/最终拍板;读料/总结/提炼类明确不许改标。"""
    from expert_work.protocol import PlanStep
    from orchestrator.graph_builder.builder import _build_dispatch_instruction

    text = str(
        _build_dispatch_instruction(
            [PlanStep(id="1", description="d", execution="delegate")]
        ).content
    )
    assert "side-effectful writes" in text
    assert "do not re-mark those" in text
