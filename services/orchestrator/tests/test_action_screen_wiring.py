"""PI-3b — action screening wired into tools_node (block / approval / degrade)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig

from expert_work.common.conversation_channel import HIDE_FROM_UI
from expert_work.runtime.cancellation import CancellationToken
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    ActionVerdict,
    FakeActionJudge,
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
)
from orchestrator.graph_builder.builder import _first_misaligned_action

_ALIGNED = ActionVerdict(aligned=True, reason="ok")
_MISALIGNED = ActionVerdict(aligned=False, reason="off-task")


@dataclass
class _EchoTool:
    name: str = "echo"
    is_read_only: bool = True

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="echoes", is_read_only=self.is_read_only)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        return ToolResult(content=f"echo: {args.get('q', '')}")


@dataclass
class _ScriptedLLM:
    responses: list[AIMessage]
    calls: int = field(default=0)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[object]
    ) -> AIMessage:
        del messages, tools
        idx = self.calls
        self.calls += 1
        return self.responses[idx]


def _tool_call_then_done() -> _ScriptedLLM:
    return _ScriptedLLM(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "echo", "args": {"q": "hi"}, "id": "tc-1", "type": "tool_call"}
                ],
            ),
            AIMessage(content="done"),
        ]
    )


async def _run(
    *,
    action_screen: Literal["off", "block", "approval"],
    judge: object | None,
    on_error: Literal["open", "closed"] = "open",
) -> dict[str, Any]:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        compiled = runner.compile(
            build_react_graph(
                llm_caller=_tool_call_then_done(),
                tool_registry=registry,
                action_judge=judge,  # type: ignore[arg-type]
                action_screen=action_screen,
                action_screen_on_error=on_error,
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4()), "run_id": "r1"}}
        return await compiled.ainvoke(
            {"messages": [HumanMessage(content="echo hi")], "step_count": 0, "max_steps": 5},
            config=cfg,
        )


def _tool_messages(state: dict[str, Any]) -> list[ToolMessage]:
    return [m for m in state["messages"] if isinstance(m, ToolMessage)]


@pytest.mark.asyncio
async def test_aligned_action_dispatches_normally() -> None:
    state = await _run(action_screen="block", judge=FakeActionJudge(verdict=_ALIGNED))
    tms = _tool_messages(state)
    assert tms and "echo: hi" in str(tms[0].content)


@pytest.mark.asyncio
async def test_misaligned_block_denies_the_tool() -> None:
    state = await _run(action_screen="block", judge=FakeActionJudge(verdict=_MISALIGNED))
    tms = _tool_messages(state)
    assert tms, "expected a denial ToolMessage"
    assert tms[0].status == "error"
    assert "action screening" in str(tms[0].content)
    assert "echo: hi" not in str(tms[0].content)  # the tool never ran


@pytest.mark.asyncio
async def test_misaligned_approval_pauses() -> None:
    state = await _run(action_screen="approval", judge=FakeActionJudge(verdict=_MISALIGNED))
    # routed to the approval gate → paused, no tool dispatched
    assert state.get("pending_approval") is not None
    assert not _tool_messages(state)


@pytest.mark.asyncio
async def test_judge_failure_fail_open_dispatches() -> None:
    state = await _run(action_screen="block", judge=FakeActionJudge(raises=True), on_error="open")
    tms = _tool_messages(state)
    assert tms and "echo: hi" in str(tms[0].content)


@pytest.mark.asyncio
async def test_judge_failure_fail_closed_denies() -> None:
    state = await _run(action_screen="block", judge=FakeActionJudge(raises=True), on_error="closed")
    tms = _tool_messages(state)
    assert tms and tms[0].status == "error"
    assert "action screening" in str(tms[0].content)


@pytest.mark.asyncio
async def test_off_skips_screening_even_with_misaligned_judge() -> None:
    # action_screen off → judge never consulted, tool dispatches.
    state = await _run(action_screen="off", judge=FakeActionJudge(verdict=_MISALIGNED))
    tms = _tool_messages(state)
    assert tms and "echo: hi" in str(tms[0].content)


@dataclass
class _RecordingActionJudge:
    """Per-tool-name verdict + records which calls were judged — lets us assert
    the screen judges every proposed call (no early break) and picks the lowest
    misaligned index."""

    bad: set[str]
    calls: list[str] = field(default_factory=list)

    async def judge_action(
        self, *, user_request: str, tool_name: str, tool_args: Mapping[str, Any]
    ) -> ActionVerdict:
        del user_request, tool_args
        self.calls.append(tool_name)
        return ActionVerdict(aligned=tool_name not in self.bad, reason="")


@pytest.mark.asyncio
async def test_first_misaligned_action_judges_all_and_returns_lowest_bad() -> None:
    judge = _RecordingActionJudge(bad={"b"})
    tool_calls = [{"name": n, "args": {}} for n in ("a", "b", "c")]
    idx = await _first_misaligned_action(
        tool_calls,
        [HumanMessage(content="hi")],
        judge=judge,
        on_error="open",
        token=CancellationToken(),
    )
    assert idx == 1  # "b" is the first misaligned
    assert sorted(judge.calls) == ["a", "b", "c"]  # every call judged (concurrent, no break)


@pytest.mark.asyncio
async def test_first_misaligned_action_all_aligned_returns_none() -> None:
    judge = _RecordingActionJudge(bad=set())
    tool_calls = [{"name": n, "args": {}} for n in ("a", "b")]
    idx = await _first_misaligned_action(
        tool_calls,
        [HumanMessage(content="hi")],
        judge=judge,
        on_error="open",
        token=CancellationToken(),
    )
    assert idx is None


@dataclass
class _RequestRecordingActionJudge:
    requests: list[str] = field(default_factory=list)

    async def judge_action(
        self, *, user_request: str, tool_name: str, tool_args: Mapping[str, Any]
    ) -> ActionVerdict:
        del tool_name, tool_args
        self.requests.append(user_request)
        return _ALIGNED


@pytest.mark.asyncio
async def test_screening_baseline_is_the_user_message_not_a_hidden_block_after_it() -> None:
    """B-67 —— 同一个取基准的 helper 的第二个消费方:工具调用按用户消息判对齐。"""
    judge = _RequestRecordingActionJudge()
    await _first_misaligned_action(
        [{"name": "a", "args": {}}],
        [
            SystemMessage(content="sys"),
            HumanMessage(content="summarise the ticket"),
            HumanMessage(content="PLATFORM-INPUTS-BLOCK", additional_kwargs={HIDE_FROM_UI: True}),
        ],
        judge=judge,
        on_error="open",
        token=CancellationToken(),
    )
    assert judge.requests == ["summarise the ticket"]
