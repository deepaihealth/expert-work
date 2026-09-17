"""新一轮与上一轮的待审批 —— 图层面的两件事(班车 2 安全修复)。

1. 审批三件套(``pending_approval`` / ``approval_resume`` / ``approval_outcome``)
   是**一轮之内**的通道。检查点里上一轮留下的值不能影响这一轮:
   * 上一轮停在审批上留下的 ``pending_approval`` 曾让本轮 ``tools_node`` 跳过
     action screening 与审批门,门控工具直接执行,本轮又带着上一轮的审批请求
     以 PAUSED 收场;
   * 上一轮声明式拒绝留下的 ``approval_outcome="rejected"`` 让本轮第一批工具
     一跑完就结束,agent 看不到工具结果。
   这里故意用**不带清零键**的图输入驱动(模拟漏写清零的入口),证的是
   ``tools_node`` 自己这道防线;入口清零由 control-plane 的测试证。
2. ``close_voided_turn`` —— 作废一次待审批时收口那一轮:悬空的工具调用逐个补
   结果、审批通道清零、检查点不再有待执行的节点。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.common.message_stamp import STAMP_RUN_ID
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    APPROVAL_TURN_RESET,
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
    close_voided_turn,
    sanitize_dangling_tool_calls,
)
from orchestrator.approval_turn import VOIDED_APPROVAL_CONTENT, voided_turn_update

_GATED = "lookup"
_FREE = "echo"
_THREAD = "thread-1"


def _call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


@dataclass
class _ScriptedLLM:
    """按脚本逐条回;脚本用完就给一句收尾(不带工具调用)。记下每次看到的 prompt。"""

    script: list[AIMessage]
    prompts: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any
    ) -> AIMessage:
        del tools
        self.prompts.append(list(messages))
        if self.script:
            return self.script.pop(0)
        return AIMessage(content="done")


@dataclass
class _RecordingTool:
    name: str
    seen: list[dict[str, Any]] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=f"recording {self.name}")

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        self.seen.append(dict(args))
        return ToolResult(content=f"{self.name}-ran")


def _cfg(run_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": _THREAD, "run_id": run_id}}


def _bare_input(text: str) -> dict[str, Any]:
    """**不带**审批清零键的图输入 —— 模拟漏写清零的入口。"""
    return {"messages": [HumanMessage(content=text)], "step_count": 0, "max_steps": 6}


@dataclass
class _Graph:
    compiled: Any
    llm: _ScriptedLLM
    gated: _RecordingTool
    free: _RecordingTool


def _build(cp: Any, script: list[AIMessage]) -> _Graph:
    llm = _ScriptedLLM(script=script)
    gated, free = _RecordingTool(_GATED), _RecordingTool(_FREE)
    registry = ToolRegistry()
    registry.register(gated)
    registry.register(free)
    compiled = GraphRunner(checkpointer=cp).compile(
        build_react_graph(
            llm_caller=llm,
            tool_registry=registry,
            approval_required_tools=frozenset({_GATED}),
        )
    )
    return _Graph(compiled=compiled, llm=llm, gated=gated, free=free)


def _gated_turn(key: str, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[_call(_GATED, {"key": key}, call_id)])


# ---------------------------------------------------------------------------
# 1. tools_node 不继承上一轮的审批通道
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_pending_approval_does_not_open_the_gate_for_a_new_turn() -> None:
    """A 停在审批上;B(不同 run)的门控调用必须照样过审批门,带 B 自己的参数。"""
    async with make_checkpointer("memory") as cp:
        g = _build(cp, [_gated_turn("A", "tc-a"), _gated_turn("B", "tc-b")])
        paused = await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        assert paused["pending_approval"].proposed_args == {"key": "A"}

        state = await g.compiled.ainvoke(_bare_input("b"), config=_cfg("run-b"))

        assert g.gated.seen == [], "门控工具在没有审批的情况下被执行了"
        pending = state["pending_approval"]
        assert pending is not None
        assert pending.proposed_args == {"key": "B"}
        assert pending.request_id != paused["pending_approval"].request_id


@pytest.mark.asyncio
async def test_stale_pending_approval_does_not_end_an_ungated_turn_as_paused() -> None:
    """B 只调非门控工具:照常执行、agent 看到结果收尾,不带着 A 的审批请求结束。"""
    async with make_checkpointer("memory") as cp:
        g = _build(
            cp,
            [
                _gated_turn("A", "tc-a"),
                AIMessage(content="", tool_calls=[_call(_FREE, {"x": 1}, "tc-free")]),
            ],
        )
        await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        calls_before_b = len(g.llm.prompts)

        state = await g.compiled.ainvoke(_bare_input("b"), config=_cfg("run-b"))

        assert g.free.seen == [{"x": 1}]
        assert state.get("pending_approval") is None
        # B 一共问了两次模型:发起工具调用一次,看到工具结果收尾一次。
        assert len(g.llm.prompts) - calls_before_b == 2
        assert state["messages"][-1].content == "done"


@pytest.mark.asyncio
async def test_turn_after_a_declarative_reject_is_not_cut_short() -> None:
    """上一轮被声明式拒绝(``approval_outcome="rejected"``)后,下一轮不能在第一批工具后就结束。"""
    async with make_checkpointer("memory") as cp:
        g = _build(
            cp,
            [
                _gated_turn("A", "tc-a"),
                AIMessage(content="", tool_calls=[_call(_FREE, {"x": 2}, "tc-free")]),
            ],
        )
        await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        await g.compiled.aupdate_state(
            _cfg("run-a"),
            {"pending_approval": None, "approval_resume": {"decision": "reject"}},
            as_node="agent",
        )
        rejected = await g.compiled.ainvoke(None, config=_cfg("run-a-cont"))
        assert rejected["approval_outcome"] == "rejected"
        calls_before_b = len(g.llm.prompts)

        state = await g.compiled.ainvoke(_bare_input("b"), config=_cfg("run-b"))

        assert g.free.seen == [{"x": 2}]
        assert len(g.llm.prompts) - calls_before_b == 2
        assert state["messages"][-1].content == "done"
        assert state.get("approval_outcome") is None


@pytest.mark.asyncio
async def test_approval_resume_still_dispatches_the_approved_call() -> None:
    """续跑不受影响:批准后门控调用照常执行一次,然后收尾。"""
    async with make_checkpointer("memory") as cp:
        g = _build(cp, [_gated_turn("A", "tc-a")])
        paused = await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        await g.compiled.aupdate_state(
            _cfg("run-a"),
            {
                "pending_approval": None,
                "approval_resume": {
                    "decision": "approve",
                    "binding_digest": paused["pending_approval"].binding_digest,
                },
            },
            as_node="agent",
        )
        state = await g.compiled.ainvoke(None, config=_cfg("run-a-cont"))

        assert g.gated.seen == [{"key": "A"}]
        assert state.get("pending_approval") is None
        assert state["messages"][-1].content == "done"


def test_turn_reset_covers_all_three_approval_channels() -> None:
    assert dict(APPROVAL_TURN_RESET) == {
        "pending_approval": None,
        "approval_resume": None,
        "approval_outcome": None,
    }


# ---------------------------------------------------------------------------
# 2. close_voided_turn —— 收口被作废的那一轮
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_voided_turn_answers_every_dangling_call_and_ends_the_turn() -> None:
    async with make_checkpointer("memory") as cp:
        two_calls = AIMessage(
            content="",
            tool_calls=[
                _call(_FREE, {"x": 3}, "tc-free"),
                _call(_GATED, {"key": "A"}, "tc-a"),
            ],
        )
        g = _build(cp, [two_calls, _gated_turn("B", "tc-b")])
        await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))

        closed = await close_voided_turn(g.compiled, _cfg("run-a"), run_id="run-a")

        assert closed == 2
        snap = await g.compiled.aget_state(_cfg("run-a"))
        assert snap.next == ()
        values = snap.values
        assert values.get("pending_approval") is None
        assert values.get("approval_resume") is None
        assert values["approval_outcome"] == "rejected"
        tail = values["messages"][-2:]
        assert [m.tool_call_id for m in tail] == ["tc-free", "tc-a"]
        assert all(isinstance(m, ToolMessage) and m.status == "error" for m in tail)
        assert all(m.content == VOIDED_APPROVAL_CONTENT for m in tail)
        assert g.free.seen == [] and g.gated.seen == []

        # 下一轮(带清零键的正常入口)看到的历史里每个工具调用都有结果,
        # 严格校验配对的模型厂商不会拒绝这段历史。
        next_turn = {**_bare_input("b"), **APPROVAL_TURN_RESET}
        state = await g.compiled.ainvoke(next_turn, config=_cfg("run-b"))
        assert sanitize_dangling_tool_calls(g.llm.prompts[-1]) == []
        assert state["pending_approval"].proposed_args == {"key": "B"}
        assert g.gated.seen == []


@pytest.mark.asyncio
async def test_close_voided_turn_is_idempotent() -> None:
    async with make_checkpointer("memory") as cp:
        g = _build(cp, [_gated_turn("A", "tc-a")])
        await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        assert await close_voided_turn(g.compiled, _cfg("run-a"), run_id="run-a") == 1
        before = await g.compiled.aget_state(_cfg("run-a"))

        assert await close_voided_turn(g.compiled, _cfg("run-a"), run_id="run-a") == 0

        after = await g.compiled.aget_state(_cfg("run-a"))
        assert after.config == before.config, "第二次收口不该再写检查点"


@pytest.mark.asyncio
async def test_close_voided_turn_leaves_a_thread_whose_tail_belongs_to_another_run() -> None:
    """尾巴不是被作废那一轮的助手消息(已经有更新的一轮)—— 一个字节都不写。"""
    async with make_checkpointer("memory") as cp:
        g = _build(cp, [_gated_turn("A", "tc-a"), _gated_turn("B", "tc-b")])
        await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        await g.compiled.ainvoke({**_bare_input("b"), **APPROVAL_TURN_RESET}, config=_cfg("run-b"))
        before = await g.compiled.aget_state(_cfg("run-b"))

        assert await close_voided_turn(g.compiled, _cfg("run-b"), run_id="run-a") == 0

        after = await g.compiled.aget_state(_cfg("run-b"))
        assert after.config == before.config


def test_voided_turn_update_matches_only_the_voided_runs_tool_calls() -> None:
    stamped = AIMessage(
        content="",
        tool_calls=[_call(_GATED, {"key": "A"}, "tc-a")],
        additional_kwargs={STAMP_RUN_ID: "run-a"},
    )
    assert voided_turn_update([stamped], run_id="run-other") is None
    assert voided_turn_update([AIMessage(content="plain")], run_id="run-a") is None
    assert voided_turn_update([], run_id="run-a") is None
    update = voided_turn_update([HumanMessage(content="hi"), stamped], run_id="run-a")
    assert update is not None
    assert update["approval_outcome"] == "rejected"
    assert [m.tool_call_id for m in update["messages"]] == ["tc-a"]
