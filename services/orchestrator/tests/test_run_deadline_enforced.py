"""B-302 PR 1 —— 每轮时限真的生效(spec ``2026-11-11-b302-runaway-reverify-design.md`` §4.1)。

时限按「这一轮实际运行了多少秒」算,记在检查点里(``run_active_s`` / ``run_clock_mark`` /
``run_clock_segment`` 三个键,永远一起写):等人审批、实例挂掉的时间不算,孤儿恢复接着算。
到 80% 每步尾部一行提醒、只发一次 ``warning`` 帧、压住步数预警那句「继续做」;到 100% 走与
步数用完相同的无工具收尾,``exit_reason="deadline"``,主运行发一条 ``tripped``。子代不记账,
只认父算好的两个绝对时刻,到父的 80% 就地收尾、不发帧;过了 80% 不再派新的子代。

时间用假时钟:``run_clock.now`` 换成可推进的计数器,模型调用 / 工具执行各自推进它。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from expert_work.runtime.cancellation import CancellationToken
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    ActionVerdict,
    BuiltAgent,
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
    pending_request_binding,
)
from orchestrator.graph_builder.builder import budget_exit_reason, build_wrapup_instruction
from orchestrator.tools import run_clock
from orchestrator.tools._child_run import _child_config, run_child_to_result
from orchestrator.tools._guards import GUARD_SINK_KEY
from orchestrator.tools.spawn_worker import SpawnWorkerTool
from orchestrator.tools.subagent import SubAgentTool

from .test_subagent import _SUB, _RecordingBuilder
from .test_subagent import _built as _sub_built
from .test_subagent import _FakeGraph as _SubFakeGraph

_CLOCK_KEYS = (run_clock.ACTIVE_S_KEY, run_clock.MARK_KEY, run_clock.SEGMENT_KEY)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


@dataclass
class _Clock:
    t: float = 10_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(run_clock, "now", fake)
    return fake


@dataclass
class _TimedLLM:
    """按脚本回;每次调用把假时钟推进脚本里写的秒数(一次模型调用花的时间)。"""

    script: list[tuple[AIMessage | Exception, float]]
    clock: _Clock | None = None
    prompts: list[list[BaseMessage]] = field(default_factory=list)
    tool_counts: list[int] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any
    ) -> AIMessage:
        self.prompts.append(list(messages))
        self.tool_counts.append(len(list(tools)))
        reply, cost = self.script.pop(0) if self.script else (AIMessage(content="done"), 0.0)
        if self.clock is not None:
            self.clock.advance(cost)
        if isinstance(reply, Exception):
            raise reply
        return reply


@dataclass
class _Probe:
    """记下派发时拿到的 ToolContext;可选地推进假时钟(一次工具执行花的时间)。"""

    name: str = "probe"
    clock: _Clock | None = None
    cost: float = 0.0
    read_only: bool = True
    seen: list[ToolContext] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description="probe", is_read_only=self.read_only)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args
        self.seen.append(ctx)
        if self.clock is not None:
            self.clock.advance(self.cost)
        return ToolResult(content="ok")


def _call(call_id: str, name: str = "probe") -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": {}, "id": call_id, "type": "tool_call"}]
    )


def _segment(limit_s: float, segment: str, started_at: float) -> dict[str, Any]:
    return {
        run_clock.RUN_LIMIT_KEY: limit_s,
        run_clock.RUN_SEGMENT_KEY: segment,
        run_clock.RUN_SEGMENT_STARTED_KEY: started_at,
    }


def _sink() -> tuple[list[dict[str, Any]], Any]:
    seen: list[dict[str, Any]] = []

    async def _publish(frame: dict[str, Any]) -> None:
        seen.append(frame)

    return seen, _publish


def _tail(prompt: Sequence[BaseMessage]) -> str:
    return str(prompt[-1].content)


async def _run(
    llm: _TimedLLM,
    *,
    tools: Sequence[Any] = (),
    configurable: Mapping[str, Any],
    max_steps: int = 20,
    **graph_kwargs: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """跑完一轮,返回 (终态, guard 帧, agent 节点的每次状态更新)。"""
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    events, publish = _sink()
    agent_updates: list[dict[str, Any]] = []
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry, **graph_kwargs)
        )
        cfg: RunnableConfig = {
            "configurable": {"thread_id": str(uuid4()), GUARD_SINK_KEY: publish, **configurable}
        }
        payload = {
            "messages": [HumanMessage(content="go")],
            "step_count": 0,
            "max_steps": max_steps,
        }
        async for chunk in compiled.astream(payload, config=cfg, stream_mode="updates"):
            update = chunk.get("agent") if isinstance(chunk, Mapping) else None
            if isinstance(update, Mapping):
                agent_updates.append(dict(update))
        state = (await compiled.aget_state(cfg)).values
    return dict(state), events, agent_updates


# ---------------------------------------------------------------------------
# T1-1 —— 到点:无工具收尾,exit_reason=deadline
# ---------------------------------------------------------------------------


async def test_t1_1_reaching_the_limit_wraps_up_without_tools(clock: _Clock) -> None:
    # 第 1 步模型调用花 700 秒,时限 600 秒;步数远没到。第 2 步桩照样回工具调用,必须被剥掉。
    llm = _TimedLLM([(_call("a"), 700.0), (_call("b"), 5.0)], clock=clock)
    state, events, _ = await _run(
        llm, tools=[_Probe()], configurable=_segment(600, "seg-a", clock.t)
    )

    assert llm.tool_counts == [1, 0]
    last = state["messages"][-1]
    assert isinstance(last, AIMessage) and not last.tool_calls
    assert state["exit_reason"] == "deadline"
    assert "reached its time limit (10 minutes)" in _tail(llm.prompts[1])
    assert events == [
        {"kind": "tripped", "guard": "deadline", "detail": {"used_s": 700, "limit_s": 600}}
    ]


# ---------------------------------------------------------------------------
# T1-2 —— 80%:尾部提醒、只发一次 warning、压住步数预警
# ---------------------------------------------------------------------------


async def test_t1_2_crossing_80_percent_notices_once_and_mutes_the_step_notice(
    clock: _Clock,
) -> None:
    # 时限 1000 秒;每步用时 300 / 300 / 300 / 40 / 40 → 第 4 步开头已用 900(跨过 800)。
    # max_steps=6:第 5、6 步本该出步数预警「Continue the task」。
    llm = _TimedLLM(
        [
            (_call("a"), 300.0),
            (_call("b"), 300.0),
            (_call("c"), 300.0),
            (_call("d"), 40.0),
            (_call("e"), 40.0),
            (AIMessage(content="done"), 0.0),
        ],
        clock=clock,
    )
    state, events, _ = await _run(
        llm, tools=[_Probe()], configurable=_segment(1000, "seg-a", clock.t), max_steps=6
    )

    assert state["exit_reason"] == "text_response"
    for prompt in llm.prompts[:3]:
        assert "[time limit notice]" not in _tail(prompt)
    for prompt in llm.prompts[3:]:
        tail = _tail(prompt)
        assert "[time limit notice]" in tail
        assert "Do not start new checks, re-reads or verification passes." in tail
        assert "In your reply, say plainly what was not checked." in tail
        assert "Continue the task" not in tail, "两条意思相反的提醒同时出现"
        assert "[step budget notice]" not in tail
    assert "working for 15 of its 16.7 minutes" in _tail(llm.prompts[3])
    assert events == [
        {"kind": "warning", "guard": "deadline", "detail": {"used_s": 900, "limit_s": 1000}}
    ]
    assert state[run_clock.WARNED_KEY] is True


# ---------------------------------------------------------------------------
# T1-3 —— 子代:只认父的两个绝对时刻,到父的 80% 就地收尾、不发帧、不记账
# ---------------------------------------------------------------------------


def test_t1_3_child_config_carries_the_two_instants_and_no_clock() -> None:
    ctx = ToolContext(
        tenant_id=uuid4(), run_id=uuid4(), deadline_at=5000.0, deadline_notice_at=4000.0
    )
    configurable = _child_config(ctx, sub_thread_id=uuid4(), sub_run_id=uuid4())["configurable"]

    assert configurable[run_clock.DEADLINE_AT_KEY] == 5000.0
    assert configurable[run_clock.DEADLINE_NOTICE_AT_KEY] == 4000.0
    for key in (
        run_clock.RUN_LIMIT_KEY,
        run_clock.RUN_SEGMENT_KEY,
        run_clock.RUN_SEGMENT_STARTED_KEY,
    ):
        assert key not in configurable


@dataclass
class _CapturingGraph:
    """包住一张真图,记下子代最后的 state。"""

    inner: Any
    final: dict[str, Any] = field(default_factory=dict)

    async def astream(
        self, state: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        async for part in self.inner.astream(state, config, stream_mode=stream_mode):
            mode, chunk = part
            if mode == "values":
                self.final = dict(chunk)
            yield part


async def test_t1_3_child_past_parents_80_percent_wraps_up_silently() -> None:
    llm = _TimedLLM([(_call("a"), 0.0), (_call("b"), 0.0)])
    registry = ToolRegistry()
    registry.register(_Probe())
    graph = _CapturingGraph(
        GraphRunner(checkpointer=InMemorySaver()).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry)
        )
    )
    frames, publish = _sink()
    now = time.monotonic()
    ctx = ToolContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        cancellation_token=CancellationToken(),
        deadline_notice_at=now - 1.0,  # 父已过 80%
        deadline_at=now + 600.0,  # 父的 100% 还远
        guard_sink=publish,
    )

    result = await run_child_to_result(
        child=BuiltAgent(graph=graph, system_prompt="child", max_steps=5),  # type: ignore[arg-type]
        task="t",
        ctx=ctx,
        child_depth=1,
        label="spawn_worker",
        agent_ref="dynamic:general",
        trajectory_recorder=None,
        trajectory_metadata={},
    )

    assert llm.tool_counts == [0], "子代照跑了工具"
    assert str(result.content).startswith(
        "[worker stopped early: reached the time limit of the run that delegated it"
    )
    assert result.meta.get("subagent_exit_reason") == "deadline"
    assert frames == [], "子代收尾发了 guard 帧"
    assert graph.final.get("exit_reason") == "deadline"
    for key in (*_CLOCK_KEYS, run_clock.WARNED_KEY):
        assert key not in graph.final, f"子代自己记账了:{key}"


# ---------------------------------------------------------------------------
# T1-4 —— 过了 80% 不再派子代 / worker
# ---------------------------------------------------------------------------


def _late_ctx() -> ToolContext:
    now = time.monotonic()
    return ToolContext(
        tenant_id=uuid4(),
        cancellation_token=CancellationToken(),
        deadline_notice_at=now - 1.0,
        deadline_at=now + 600.0,
    )


async def test_t1_4_subagent_is_refused_past_80_percent() -> None:
    graph = _SubFakeGraph(result={"messages": [AIMessage(content="ok")], "step_count": 1})
    builder = _RecordingBuilder(built=_sub_built(graph))
    tool = SubAgentTool(subagent=_SUB, builder=builder, child_depth=1)

    result = await tool.call({"task": "x"}, ctx=_late_ctx())

    assert "final phase" in str(result.content)
    assert builder.calls == [] and graph.calls == []


async def test_t1_4_spawn_worker_is_refused_past_80_percent() -> None:
    from .test_spawn_worker import _answer_graph, _RecordingWorkerBuilder
    from .test_spawn_worker import _built as _worker_built

    graph = _answer_graph("ok")
    builder = _RecordingWorkerBuilder(built=_worker_built(graph))
    tool = SpawnWorkerTool(builder=builder, child_depth=1)

    result = await tool.call({"task": "x"}, ctx=_late_ctx())

    assert "final phase" in str(result.content)
    assert builder.calls == [] and graph.calls == []


def _expired_ctx() -> ToolContext:
    """模型调用中途已越过 100%:80% 与截止时刻都在过去。"""
    now = time.monotonic()
    return ToolContext(
        tenant_id=uuid4(),
        cancellation_token=CancellationToken(),
        deadline_notice_at=now - 300.0,
        deadline_at=now - 1.0,
    )


async def test_t1_4_subagent_past_100_percent_is_soft_refused_not_cancelled() -> None:
    """守卫:咬「已过截止」的硬拒绝排在 80% 软拒绝之前 —— 那样会抛 RunCancelledError,
    在失败台账里留一条 unknown(审查 L1)。"""
    graph = _SubFakeGraph(result={"messages": [AIMessage(content="ok")], "step_count": 1})
    builder = _RecordingBuilder(built=_sub_built(graph))
    tool = SubAgentTool(subagent=_SUB, builder=builder, child_depth=1)

    result = await tool.call({"task": "x"}, ctx=_expired_ctx())

    assert "final phase" in str(result.content)
    assert result.failed and result.failure_transient
    assert builder.calls == [] and graph.calls == []


async def test_t1_4_spawn_worker_past_100_percent_is_soft_refused_not_cancelled() -> None:
    """守卫:同上,``spawn_worker`` 那一处的顺序。"""
    from .test_spawn_worker import _answer_graph, _RecordingWorkerBuilder
    from .test_spawn_worker import _built as _worker_built

    graph = _answer_graph("ok")
    builder = _RecordingWorkerBuilder(built=_worker_built(graph))
    tool = SpawnWorkerTool(builder=builder, child_depth=1)

    result = await tool.call({"task": "x"}, ctx=_expired_ctx())

    assert "final phase" in str(result.content)
    assert result.failed and result.failure_transient
    assert builder.calls == [] and graph.calls == []


# ---------------------------------------------------------------------------
# T1-6 —— 审批暂停 30 分钟不算;暂停那步的模型调用、续跑后被批工具都算
# ---------------------------------------------------------------------------


@dataclass
class _GatedTool:
    clock: _Clock
    cost: float
    ran: int = 0

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="send", description="gated", is_read_only=False)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del args, ctx
        self.ran += 1
        self.clock.advance(self.cost)
        return ToolResult(content="sent")


async def test_t1_6_approval_wait_is_not_counted(clock: _Clock) -> None:
    gated = _GatedTool(clock=clock, cost=120.0)
    llm = _TimedLLM([(_call("a", "send"), 60.0), (AIMessage(content="done"), 30.0)], clock=clock)
    registry = ToolRegistry()
    registry.register(gated)
    thread = str(uuid4())
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm, tool_registry=registry, approval_required_tools=frozenset({"send"})
            )
        )
        first: RunnableConfig = {
            "configurable": {"thread_id": thread, "run_id": "r0", **_segment(3600, "A", clock.t)}
        }
        paused = await compiled.ainvoke(
            {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 6},
            config=first,
        )
        assert paused["pending_approval"] is not None and gated.ran == 0
        assert paused[run_clock.ACTIVE_S_KEY] == pytest.approx(60.0)

        clock.advance(1800.0)  # 等人审批 30 分钟
        binding = pending_request_binding((await compiled.aget_state(first)).values)
        assert binding is not None
        await compiled.aupdate_state(
            first,
            {
                "pending_approval": None,
                "approval_resume": {
                    "decision": "approve",
                    "modified_args": None,
                    "reason": None,
                    "binding_digest": paused["pending_approval"].binding_digest,
                    **binding,
                },
            },
            as_node="agent",
        )
        resumed: RunnableConfig = {
            "configurable": {"thread_id": thread, "run_id": "r1", **_segment(3600, "B", clock.t)}
        }
        state = await compiled.ainvoke(None, config=resumed)

    assert gated.ran == 1
    assert state["messages"][-1].content == "done"
    # 60(暂停那步的模型调用)+ 120(续跑后被批的工具)+ 30(最后一步),不含 1800 的等待。
    assert state[run_clock.ACTIVE_S_KEY] == pytest.approx(210.0)
    assert state[run_clock.SEGMENT_KEY] == "B"


# ---------------------------------------------------------------------------
# T1-7 —— 孤儿恢复:从检查点接着加,不归零
# ---------------------------------------------------------------------------


async def test_t1_7_orphan_reclaim_keeps_counting(clock: _Clock) -> None:
    # 第 1 步 60 秒 → 工具 100 秒 → 第 2 步调用中途「实例挂了」。换实例(新的本段)接着跑。
    llm = _TimedLLM(
        [
            (_call("a"), 60.0),
            (RuntimeError("pod died"), 50.0),
            (AIMessage(content="done"), 40.0),
        ],
        clock=clock,
    )
    registry = ToolRegistry()
    registry.register(_Probe(clock=clock, cost=100.0))
    thread = str(uuid4())
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(llm_caller=llm, tool_registry=registry)
        )
        first: RunnableConfig = {
            "configurable": {"thread_id": thread, **_segment(3600, "A", clock.t)}
        }
        with pytest.raises(RuntimeError, match="pod died"):
            await compiled.ainvoke(
                {"messages": [HumanMessage(content="go")], "step_count": 0, "max_steps": 6},
                config=first,
            )
        clock.advance(1000.0)  # 实例挂着、没人跑的时间
        reclaimed: RunnableConfig = {
            "configurable": {"thread_id": thread, **_segment(3600, "B", clock.t)}
        }
        state = await compiled.ainvoke(None, config=reclaimed)

    assert state["messages"][-1].content == "done"
    # 检查点里的 60 秒还在(最后一次记账之后的工具与挂掉前那次调用不计),加上恢复后的 40 秒。
    assert state[run_clock.ACTIVE_S_KEY] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# T1-7b —— 工具上下文的截止时刻 = 上次记账时刻 + (时限 - 已用);三键同进同出
# ---------------------------------------------------------------------------


@dataclass
class _SlowJudge:
    """动作审查替身:放行一切,但花掉一些时间(把「上次记账」与「现在」拉开)。"""

    clock: _Clock
    cost: float

    async def judge_action(
        self, *, user_request: str, tool_name: str, tool_args: Mapping[str, Any]
    ) -> ActionVerdict:
        del user_request, tool_name, tool_args
        self.clock.advance(self.cost)
        return ActionVerdict(aligned=True, reason="test")


async def test_t1_7b_tool_deadline_does_not_give_away_the_model_call(clock: _Clock) -> None:
    start = clock.t
    probe = _Probe()
    llm = _TimedLLM([(_call("a"), 300.0), (AIMessage(content="done"), 0.0)], clock=clock)
    state, _, updates = await _run(
        llm,
        tools=[probe],
        configurable=_segment(3600, "A", start),
        action_judge=_SlowJudge(clock=clock, cost=120.0),
        action_screen="block",
    )

    (ctx,) = probe.seen
    # 上次记账在 start+300、已用 300 → 截止 = start+300+(3600-300) = start+3600;
    # 「现在 + 剩余」会是 start+420+3300 = start+3720。
    assert ctx.deadline_at == pytest.approx(start + 3600.0)
    assert ctx.deadline_notice_at == pytest.approx(start + 2880.0)
    assert state["messages"][-1].content == "done"
    assert updates, "没拿到 agent 节点的状态更新"
    for update in updates:
        present = {key for key in _CLOCK_KEYS if key in update}
        assert present == set(_CLOCK_KEYS), f"记账键没有一起写:{sorted(present)}"


async def test_t1_7b_no_segment_token_means_no_clock(clock: _Clock) -> None:
    probe = _Probe()
    llm = _TimedLLM([(_call("a"), 99_999.0), (AIMessage(content="done"), 0.0)], clock=clock)
    state, events, updates = await _run(llm, tools=[probe], configurable={})

    (ctx,) = probe.seen
    assert ctx.deadline_at is None and ctx.deadline_notice_at is None
    assert state["messages"][-1].content == "done"
    assert events == []
    for update in updates:
        assert not any(key in update for key in _CLOCK_KEYS), "无令牌路径记了账"


# ---------------------------------------------------------------------------
# 取值与文字
# ---------------------------------------------------------------------------


def test_budget_reason_order_puts_deadline_last() -> None:
    assert (
        budget_exit_reason(
            max_steps=3, step_count=3, stuck=True, token_tripped=True, deadline_tripped=True
        )
        == "max_steps"
    )
    assert (
        budget_exit_reason(
            max_steps=0, step_count=0, stuck=False, token_tripped=True, deadline_tripped=True
        )
        == "token_budget"
    )
    assert (
        budget_exit_reason(
            max_steps=0, step_count=0, stuck=False, token_tripped=False, deadline_tripped=True
        )
        == "deadline"
    )


def test_wrapup_names_the_time_limit() -> None:
    text = build_wrapup_instruction(
        reason="deadline",
        max_steps=48,
        limit_minutes=90,
        unresolved_failures=0,
        artifacts=None,
        open_plan_steps=None,
    )
    assert "reached its time limit (90 minutes) and tools are no longer available" in text
    child = build_wrapup_instruction(
        reason="deadline",
        max_steps=48,
        limit_minutes=None,
        unresolved_failures=0,
        artifacts=None,
        open_plan_steps=None,
    )
    assert "reached its time limit and tools are no longer available" in child


def test_segment_config_writes_all_three_values_or_nothing(clock: _Clock) -> None:
    first = run_clock.run_segment_config(5400)
    second = run_clock.run_segment_config(5400)
    assert first[run_clock.RUN_LIMIT_KEY] == 5400
    assert first[run_clock.RUN_SEGMENT_STARTED_KEY] == clock.t
    assert first[run_clock.RUN_SEGMENT_KEY] != second[run_clock.RUN_SEGMENT_KEY]
    assert run_clock.run_segment_config(0) == {}


def test_turn_reset_clears_all_clock_keys_together() -> None:
    assert dict(run_clock.RUN_CLOCK_TURN_RESET) == {
        run_clock.ACTIVE_S_KEY: 0.0,
        run_clock.MARK_KEY: None,
        run_clock.SEGMENT_KEY: None,
        run_clock.WARNED_KEY: False,
    }
