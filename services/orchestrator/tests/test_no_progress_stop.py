"""No-progress stop — graceful wrap-up when the agent is stuck in a loop.

The loop-detection middleware already flags identical tool-call repeats and
arms ``escalate_next`` (one higher-effort turn to break out). This extends
that signal: agent_node counts loop trips per run (``loop_trip_count``, a
clean turn in between does not reset it) and, at the Nth trip — N =
``policies.max_no_progress``, or the platform rule of 2 when it is 0 —
forces the same tool-less graceful wrap-up ``max_steps`` uses, stopping
early instead of grinding to ``max_steps`` while making no progress.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.middleware import (
    LOOP_REMINDER_MARK,
    LoopDetectionMiddleware,
    MiddlewareChain,
)
from orchestrator import AgentState, GraphRunner, ToolRegistry, build_react_graph
from orchestrator.graph_builder.builder import _exit_reason_for, _should_continue
from orchestrator.llm.providers.anthropic import _to_anthropic_messages
from orchestrator.tools._guards import GUARD_SINK_KEY
from orchestrator.tools.registry import ToolContext, ToolResult, ToolSpec


@dataclass
class _ScriptedLLM:
    responses: list[AIMessage]
    calls: int = 0
    seen_tool_counts: list[int] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages
        self.seen_tool_counts.append(len(list(tools)))
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


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
    return {"name": "probe", "args": {"q": "same"}, "id": call_id, "type": "tool_call"}


async def _invoke(graph, payload: dict[str, Any], thread_id: str) -> AgentState:
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        return await compiled.ainvoke(payload, config=cfg)


@pytest.mark.asyncio
async def test_streak_at_threshold_forces_toolless_wrapup() -> None:
    """trips >= max_no_progress → tool-less wrap-up turn, no tool dispatch.

    ``budget_exhausted`` forces ``tools=[]`` on the wrap-up turn, so the
    scripted LLM sees zero tools and the registered tool never dispatches.
    """
    tool = _EchoTool()
    registry = ToolRegistry()
    registry.register(tool)
    llm = _ScriptedLLM(responses=[AIMessage(content="here is what I have")])
    graph = build_react_graph(llm_caller=llm, tool_registry=registry)
    await _invoke(
        graph,
        {
            "messages": [HumanMessage(content="go")],
            "step_count": 0,
            "max_steps": 20,
            "loop_trip_count": 2,
            "max_no_progress": 2,
        },
        "nps-stuck",
    )
    assert llm.seen_tool_counts == [0]
    assert tool.dispatched == 0


@pytest.mark.asyncio
async def test_below_threshold_runs_normally() -> None:
    """trips < max_no_progress → no wrap-up, tools still offered."""
    tool = _EchoTool()
    registry = ToolRegistry()
    registry.register(tool)
    llm = _ScriptedLLM(responses=[AIMessage(content="done")])
    graph = build_react_graph(llm_caller=llm, tool_registry=registry)
    await _invoke(
        graph,
        {
            "messages": [HumanMessage(content="go")],
            "step_count": 0,
            "max_steps": 20,
            "loop_trip_count": 1,
            "max_no_progress": 2,
        },
        "nps-ok",
    )
    assert llm.seen_tool_counts == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(("trips", "tool_counts"), [(1, [1]), (2, [0])])
async def test_zero_uses_the_platform_rule_of_two_trips_per_run(
    trips: int, tool_counts: list[int]
) -> None:
    """B-226 ④ —— ``max_no_progress == 0``(默认)不再是「关闭」:同一次运行里第二次原地
    打转就走无进展收尾。按本 run 累计的次数算, 不看是否连续。"""
    registry = ToolRegistry()
    registry.register(_EchoTool())
    llm = _ScriptedLLM(responses=[AIMessage(content="done")])
    graph = build_react_graph(llm_caller=llm, tool_registry=registry)
    events: list[dict[str, Any]] = []

    async def _sink(frame: dict[str, Any]) -> None:
        events.append(frame)

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(graph)
        cfg: RunnableConfig = {
            "configurable": {"thread_id": f"nps-default-{trips}", GUARD_SINK_KEY: _sink}
        }
        state = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="go")],
                "step_count": 0,
                "max_steps": 20,
                "loop_trip_count": trips,
                "max_no_progress": 0,
            },
            config=cfg,
        )
    assert llm.seen_tool_counts == tool_counts
    assert state["exit_reason"] == ("no_progress" if trips == 2 else "text_response")
    if trips == 2:
        assert events == [
            {"kind": "tripped", "guard": "no_progress", "detail": {"streak": 2, "max": 2}}
        ]


@dataclass
class _PatternLLM(_ScriptedLLM):
    """按 ``pattern`` 循环发工具调用(每次新建消息);没给工具(收尾轮)就答一句话。"""

    pattern: tuple[str, ...] = ("probe",)
    responses: list[AIMessage] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages
        self.seen_tool_counts.append(len(list(tools)))
        self.calls += 1
        if not list(tools):
            return AIMessage(content="wrap-up")
        name = self.pattern[(self.calls - 1) % len(self.pattern)]
        return AIMessage(
            content="",
            tool_calls=[
                {"name": name, "args": {"p": "x"}, "id": f"c{self.calls}", "type": "tool_call"}
            ],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pattern", "calls"),
    [(("A", "A", "A", "B"), 8), (("A", "A", "A", "B", "B", "B"), 7)],
    ids=["AAAB", "AAABBB"],
)
async def test_default_rule_counts_trips_across_clean_turns(
    pattern: tuple[str, ...], calls: int
) -> None:
    """B-226 ④ —— 打转一次、换个调用、再打转:中间那步干净的不清零, 第二次被拦下就收尾,
    不会这样循环到步数上限(以前按「连续」算, 这两种形态都跑满 30 步)。"""
    registry = ToolRegistry()
    registry.register(_EchoTool(name="A"))
    registry.register(_EchoTool(name="B"))
    llm = _PatternLLM(pattern=pattern)
    state = await _invoke(
        _looping_graph(llm, registry),
        {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 30},
        f"nps-cycle-{''.join(pattern)}",
    )
    assert llm.calls == calls
    assert llm.seen_tool_counts[-1] == 0
    assert state["exit_reason"] == "no_progress"
    assert state["loop_trip_count"] == 2


@pytest.mark.asyncio
async def test_a_configured_cap_also_counts_trips_per_run() -> None:
    """``max_no_progress = N`` = 这次运行第 N 次原地打转就收尾, 同样按本 run 累计:控制台建的
    智能体默认写 4, 按「连续」算的话 AAAB 循环一直清零、跑满步数上限。这里第 4 次拦下就收尾
    (拦在第 3、7、11、15 次调用, 第 16 次是收尾轮)。"""
    registry = ToolRegistry()
    registry.register(_EchoTool(name="A"))
    registry.register(_EchoTool(name="B"))
    llm = _PatternLLM(pattern=("A", "A", "A", "B"))
    events: list[dict[str, Any]] = []

    async def _sink(frame: dict[str, Any]) -> None:
        events.append(frame)

    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(_looping_graph(llm, registry))
        cfg: RunnableConfig = {"configurable": {"thread_id": "nps-cfg-4", GUARD_SINK_KEY: _sink}}
        state = await compiled.ainvoke(
            {
                "messages": [HumanMessage(content="go")],
                "step_count": 0,
                "max_steps": 30,
                "max_no_progress": 4,
            },
            config=cfg,
        )
    assert llm.calls == 16
    assert llm.seen_tool_counts[-1] == 0
    assert state["exit_reason"] == "no_progress"
    assert state["loop_trip_count"] == 4
    assert events == [
        {"kind": "tripped", "guard": "no_progress", "detail": {"streak": 4, "max": 4}}
    ]


@dataclass
class _RecordingLLM(_ScriptedLLM):
    seen: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        self.seen.append(list(messages))
        return await super().__call__(messages=messages, tools=tools)


@dataclass
class _RepeatingLLM(_ScriptedLLM):
    """Emits the same call every turn, as a fresh message each time (a reused
    message object would get its id assigned once and replace itself in history)."""

    responses: list[AIMessage] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        del messages
        self.seen_tool_counts.append(len(list(tools)))
        self.calls += 1
        return AIMessage(content="", tool_calls=[_tc(f"same-{self.calls}")])


def _looping_graph(llm: _ScriptedLLM, registry: ToolRegistry) -> Any:
    return build_react_graph(
        llm_caller=llm,
        tool_registry=registry,
        after_llm_chain=MiddlewareChain.from_middlewares(
            "after_llm_call", [LoopDetectionMiddleware()]
        ),
    )


def test_a_trailing_loop_reminder_is_not_an_exit() -> None:
    """B-226 ④ —— 打转那一轮落的是「被清空的回复 + 提醒」:路由回 agent, 不盖出口章
    (盖了 ``text_response`` 的话, run 若在下一步出错, 读到的是一个假的「自然答完」)。"""
    reminder = HumanMessage(content="loop", additional_kwargs={LOOP_REMINDER_MARK: True})
    cleared = AIMessage(content="")
    state: AgentState = {"messages": [cleared, reminder], "step_count": 3, "max_steps": 20}
    assert _should_continue(state) == "agent"
    assert _exit_reason_for([cleared, reminder], budget_reason=None) is None
    assert _exit_reason_for([cleared, reminder], budget_reason="max_steps") == "max_steps"
    assert _exit_reason_for([cleared], budget_reason=None) == "text_response"


@pytest.mark.asyncio
async def test_loop_trip_is_answered_in_the_same_run() -> None:
    """B-226 ④ —— 第三次相同调用被清掉之后, 提醒在**同一个 run** 里交给模型(以前 run 直接以
    被清空的那条回复「正常结束」, 提醒要等下一轮才看得到)。模型据此作答, 出口是
    ``text_response``;这次运行记了 1 次打转。"""
    registry = ToolRegistry()
    registry.register(_EchoTool())
    llm = _RecordingLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("a")]),
            AIMessage(content="", tool_calls=[_tc("b")]),
            AIMessage(content="", tool_calls=[_tc("c")]),
            AIMessage(content="the status never changed"),
        ]
    )
    state = await _invoke(
        _looping_graph(llm, registry),
        {
            "messages": [HumanMessage(content="go")],
            "step_count": 0,
            "max_steps": 20,
            "max_no_progress": 3,
        },
        "nps-same-run",
    )
    assert llm.calls == 4
    reminder = llm.seen[3][-1]
    assert isinstance(reminder, HumanMessage)
    assert reminder.additional_kwargs.get(LOOP_REMINDER_MARK) is True
    assert state["messages"][-1].content == "the status never changed"
    assert state["exit_reason"] == "text_response"
    assert state["loop_trip_count"] == 1


@pytest.mark.asyncio
async def test_the_prompt_after_a_trip_has_no_empty_assistant_message_for_anthropic() -> None:
    """B-226 ④ —— 提醒在同一个 run 里交回模型时, 被清空工具调用的那条回复若没有正文就不落库:
    Anthropic 拒收不在末尾的空 assistant 消息。"""
    registry = ToolRegistry()
    registry.register(_EchoTool())
    llm = _RecordingLLM(
        responses=[
            AIMessage(content="", tool_calls=[_tc("a")]),
            AIMessage(content="", tool_calls=[_tc("b")]),
            AIMessage(content="", tool_calls=[_tc("c")]),
            AIMessage(content="still pending"),
        ]
    )
    await _invoke(
        _looping_graph(llm, registry),
        {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 20},
        "nps-anthropic",
    )
    _system, mapped, _anchors = await _to_anthropic_messages(llm.seen[3], None)
    assistants = [m for m in mapped if m["role"] == "assistant"]
    assert len(assistants) == 2  # the two calls that ran; the cleared third is gone
    assert all(m["content"] not in ("", []) for m in assistants)
    assert mapped[-1]["role"] == "user"


@pytest.mark.asyncio
async def test_a_model_that_keeps_repeating_ends_via_the_no_progress_wrapup() -> None:
    """B-226 ④ 的边界:提醒交回模型之后模型照旧重复 —— 每次都再撞一次, streak 累加,
    到 ``max_no_progress`` 走无工具收尾, 不会无限循环。"""
    tool = _EchoTool()
    registry = ToolRegistry()
    registry.register(tool)
    llm = _RepeatingLLM()
    state = await _invoke(
        _looping_graph(llm, registry),
        {
            "messages": [HumanMessage(content="go")],
            "step_count": 0,
            "max_steps": 20,
            "max_no_progress": 2,
        },
        "nps-bounded",
    )
    # 2 calls run, trip (streak 1), trip again (streak 2), then the tool-less wrap-up.
    assert llm.calls == 5
    assert tool.dispatched == 2
    assert llm.seen_tool_counts[-1] == 0
    assert state["exit_reason"] == "no_progress"


@pytest.mark.asyncio
async def test_with_the_default_cap_a_second_trip_ends_the_run() -> None:
    """B-226 ④ —— 默认(``max_no_progress`` 未设 = 0):一次作答机会, 第二次被拦下即收尾,
    不会一路空转到步数上限(以前 6 步的上限要 7 次调用)。"""
    tool = _EchoTool()
    registry = ToolRegistry()
    registry.register(tool)
    llm = _RepeatingLLM()
    state = await _invoke(
        _looping_graph(llm, registry),
        {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 20},
        "nps-default-bound",
    )
    # 2 calls run, trip (streak 1), trip again (streak 2), then the tool-less wrap-up.
    assert llm.calls == 5
    assert tool.dispatched == 2
    assert llm.seen_tool_counts[-1] == 0
    assert state["exit_reason"] == "no_progress"
