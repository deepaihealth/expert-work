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
2. ``repair_unanswered_tail`` —— 新一轮开跑前按历史的形状收口上一轮:悬空的工具
   调用逐个补结果、审批通道清零、检查点不再有待执行的节点;那一轮还在进行时不动。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.common.message_stamp import STAMP_RUN_ID
from expert_work.protocol import canonical_args_digest
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    APPROVAL_TURN_RESET,
    ActionVerdict,
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
    pending_request_binding,
    repair_unanswered_tail,
    sanitize_dangling_tool_calls,
)
from orchestrator.approval_turn import (
    UNRECORDED_TOOL_CALL_CONTENT,
    VOIDED_APPROVAL_CONTENT,
    voided_turn_update,
)
from orchestrator.graph_builder._approval import _stable_request_id, apply_resume_decision
from orchestrator.tools.approval import ASK_FOR_APPROVAL_TOOL, AskForApprovalTool

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


def _thread_cfg() -> RunnableConfig:
    """收口用的配置只带会话 —— 与控制面一致。带上新一轮的 ``run_id`` 的话,LangGraph
    会把紧随其后、同 ``run_id`` 的图输入当成重入同一次运行而丢掉。"""
    return {"configurable": {"thread_id": _THREAD}}


def _bare_input(text: str) -> dict[str, Any]:
    """**不带**审批清零键的图输入 —— 模拟漏写清零的入口。"""
    return {"messages": [HumanMessage(content=text)], "step_count": 0, "max_steps": 6}


@dataclass
class _Graph:
    compiled: Any
    llm: _ScriptedLLM
    gated: _RecordingTool
    free: _RecordingTool


def _build(cp: Any, script: list[AIMessage], **graph_kwargs: Any) -> _Graph:
    llm = _ScriptedLLM(script=script)
    gated, free = _RecordingTool(_GATED), _RecordingTool(_FREE)
    registry = ToolRegistry()
    registry.register(gated)
    registry.register(free)
    registry.register(AskForApprovalTool())
    compiled = GraphRunner(checkpointer=cp).compile(
        build_react_graph(
            llm_caller=llm,
            tool_registry=registry,
            approval_required_tools=frozenset({_GATED}),
            **graph_kwargs,
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


@dataclass
class _BlockEcho:
    """action screening 替身:只判 ``echo`` 调用不对齐。"""

    async def judge_action(
        self, *, user_request: str, tool_name: str, tool_args: Mapping[str, Any]
    ) -> ActionVerdict:
        del user_request, tool_args
        return ActionVerdict(aligned=tool_name != _FREE, reason="test")


@pytest.mark.asyncio
async def test_stale_pending_approval_does_not_end_a_blocked_turn() -> None:
    """B 的调用被 action screening 阻断:照常回到 agent 重新规划,不带着 A 的请求收场。"""
    async with make_checkpointer("memory") as cp:
        g = _build(
            cp,
            [
                _gated_turn("A", "tc-a"),
                AIMessage(content="", tool_calls=[_call(_FREE, {"x": 4}, "tc-free")]),
            ],
            action_judge=_BlockEcho(),
            action_screen="block",
        )
        await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        calls_before_b = len(g.llm.prompts)

        state = await g.compiled.ainvoke(_bare_input("b"), config=_cfg("run-b"))

        assert g.free.seen == []
        assert state.get("pending_approval") is None
        assert len(g.llm.prompts) - calls_before_b == 2
        assert state["messages"][-1].content == "done"


@pytest.mark.asyncio
async def test_agent_question_rejected_after_an_earlier_veto_loops_back_to_the_agent() -> None:
    """上一轮声明式拒绝留下 ``approval_outcome``;本轮 agent 自己发起的确认被拒,应回到 agent。"""
    ask = AIMessage(
        content="",
        tool_calls=[
            _call(
                ASK_FOR_APPROVAL_TOOL,
                {"reason_kind": "approach_choice", "action_summary": "which one?"},
                "tc-ask",
            )
        ],
    )
    async with make_checkpointer("memory") as cp:
        g = _build(cp, [_gated_turn("A", "tc-a"), ask])
        await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        await g.compiled.aupdate_state(
            _cfg("run-a"),
            {"pending_approval": None, "approval_resume": {"decision": "reject"}},
            as_node="agent",
        )
        assert (await g.compiled.ainvoke(None, config=_cfg("run-a-cont")))[
            "approval_outcome"
        ] == "rejected"
        paused = await g.compiled.ainvoke(_bare_input("b"), config=_cfg("run-b"))
        assert paused["pending_approval"].reason_kind == "approach_choice"
        await g.compiled.aupdate_state(
            _cfg("run-b"),
            {"pending_approval": None, "approval_resume": {"decision": "reject"}},
            as_node="agent",
        )
        calls_before = len(g.llm.prompts)

        state = await g.compiled.ainvoke(None, config=_cfg("run-b-cont"))

        assert len(g.llm.prompts) - calls_before == 1
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
# 2. repair_unanswered_tail —— 按历史的形状收口上一轮
# ---------------------------------------------------------------------------


@dataclass
class _ContentFor:
    """记下被问到的 run;``answers`` 里没有的 run 视为还在进行。

    ``waiting`` 是检查点里还有未用掉的裁定时的回答(默认:续跑还在,不补)。
    """

    answers: dict[str, str]
    waiting: dict[str, str] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)
    asked_waiting: list[str] = field(default_factory=list)

    async def __call__(self, run_id: str, /, *, verdict_waiting: bool) -> str | None:
        if verdict_waiting:
            self.asked_waiting.append(run_id)
            return self.waiting.get(run_id)
        self.asked.append(run_id)
        return self.answers.get(run_id)


@pytest.mark.asyncio
async def test_repair_answers_every_dangling_call_and_ends_the_turn() -> None:
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
        content_for = _ContentFor({"run-a": VOIDED_APPROVAL_CONTENT})

        closed = await repair_unanswered_tail(g.compiled, _thread_cfg(), content_for=content_for)

        assert closed == 2
        assert content_for.asked == ["run-a"]
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
async def test_repair_is_idempotent() -> None:
    async with make_checkpointer("memory") as cp:
        g = _build(cp, [_gated_turn("A", "tc-a")])
        await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        content_for = _ContentFor({"run-a": VOIDED_APPROVAL_CONTENT})
        assert await repair_unanswered_tail(g.compiled, _thread_cfg(), content_for=content_for) == 1
        before = await g.compiled.aget_state(_cfg("run-a"))

        assert await repair_unanswered_tail(g.compiled, _thread_cfg(), content_for=content_for) == 0

        after = await g.compiled.aget_state(_cfg("run-a"))
        assert after.config == before.config, "第二次收口不该再写检查点"
        assert content_for.asked == ["run-a"], "尾巴已经不是助手消息,不该再去问"


@pytest.mark.asyncio
async def test_repair_leaves_a_turn_that_is_still_going() -> None:
    """调用方说这一轮还没结束(``None``)—— 一个字节都不写。"""
    async with make_checkpointer("memory") as cp:
        g = _build(cp, [_gated_turn("A", "tc-a")])
        await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        before = await g.compiled.aget_state(_cfg("run-a"))
        content_for = _ContentFor({})

        assert await repair_unanswered_tail(g.compiled, _thread_cfg(), content_for=content_for) == 0

        assert content_for.asked == ["run-a"]
        assert (await g.compiled.aget_state(_cfg("run-a"))).config == before.config


async def _approve_a(g: _Graph, paused: dict[str, Any]) -> None:
    """控制面写裁定的那一步:带上被批请求的身份。"""
    values = (await g.compiled.aget_state(_cfg("run-a"))).values
    binding = pending_request_binding(values)
    assert binding is not None
    await g.compiled.aupdate_state(
        _cfg("run-a"),
        {
            "pending_approval": None,
            "approval_resume": {
                "decision": "approve",
                "binding_digest": paused["pending_approval"].binding_digest,
                **binding,
            },
        },
        as_node="agent",
    )


@pytest.mark.asyncio
async def test_repair_leaves_a_verdict_whose_continuation_is_still_going() -> None:
    """裁定已经写进检查点、续跑还在:不补,续跑照常执行批准的调用。"""
    async with make_checkpointer("memory") as cp:
        g = _build(cp, [_gated_turn("A", "tc-a")])
        paused = await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        await _approve_a(g, paused)
        before = await g.compiled.aget_state(_cfg("run-a"))
        content_for = _ContentFor({"run-a": VOIDED_APPROVAL_CONTENT})

        assert await repair_unanswered_tail(g.compiled, _thread_cfg(), content_for=content_for) == 0

        assert (content_for.asked, content_for.asked_waiting) == ([], ["run-a"])
        assert (await g.compiled.aget_state(_cfg("run-a"))).config == before.config
        await g.compiled.ainvoke(None, config=_cfg("run-a-cont"))
        assert g.gated.seen == [{"key": "A"}]


@pytest.mark.asyncio
async def test_repair_clears_a_verdict_whose_continuation_is_over() -> None:
    """续跑没用掉裁定就结束了(取消在第一步之前):补上结果、清掉过期裁定,之后的轮次都完整。"""
    async with make_checkpointer("memory") as cp:
        g = _build(cp, [_gated_turn("A", "tc-a")])
        paused = await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        await _approve_a(g, paused)
        content_for = _ContentFor({}, waiting={"run-a": UNRECORDED_TOOL_CALL_CONTENT})

        assert await repair_unanswered_tail(g.compiled, _thread_cfg(), content_for=content_for) == 1

        snap = await g.compiled.aget_state(_cfg("run-a"))
        assert snap.next == ()
        assert snap.values.get("approval_resume") is None
        assert snap.values["messages"][-1].content == UNRECORDED_TOOL_CALL_CONTENT
        state = await g.compiled.ainvoke(
            {**_bare_input("b"), **APPROVAL_TURN_RESET}, config=_cfg("run-b")
        )
        assert sanitize_dangling_tool_calls(g.llm.prompts[-1]) == []
        assert state["messages"][-1].content == "done"
        assert g.gated.seen == []


# ---------------------------------------------------------------------------
# 3. 裁定只能用在它批的那一轮
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_request_carries_the_target_call() -> None:
    async with make_checkpointer("memory") as cp:
        two_calls = AIMessage(
            content="",
            tool_calls=[_call(_FREE, {"x": 1}, "tc-free"), _call(_GATED, {"key": "A"}, "tc-a")],
        )
        g = _build(cp, [two_calls])
        paused = await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        request = paused["pending_approval"]
        assert (request.tool_call_id, request.tool_call_index) == ("tc-a", 1)
        binding = pending_request_binding((await g.compiled.aget_state(_cfg("run-a"))).values)
        assert binding == {
            "request_id": request.request_id,
            "action_summary": request.action_summary,
            "tool_call_id": "tc-a",
            "tool_call_index": 1,
        }
    assert pending_request_binding({"pending_approval": {"request_id": "r"}}) == {
        "request_id": "r",
        "action_summary": None,
        "tool_call_id": "",
        "tool_call_index": None,
    }
    assert pending_request_binding({"pending_approval": None}) is None
    assert pending_request_binding({}) is None


@pytest.mark.asyncio
async def test_action_screen_approval_carries_the_judged_call() -> None:
    async with make_checkpointer("memory") as cp:
        turn = AIMessage(
            content="",
            tool_calls=[_call(_GATED, {"key": "A"}, "tc-a"), _call(_FREE, {"x": 1}, "tc-free")],
        )
        g = _build(cp, [turn], action_judge=_BlockEcho(), action_screen="approval")
        paused = await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        request = paused["pending_approval"]
        assert (request.tool_call_id, request.tool_call_index) == ("tc-free", 1)


@pytest.mark.parametrize(
    "b_call", [("B", "tc-b"), ("A", "tc-a")], ids=["new-call", "same-id-and-args"]
)
@pytest.mark.asyncio
async def test_a_verdict_landing_on_another_turn_dispatches_nothing(
    b_call: tuple[str, str],
) -> None:
    """A 的裁定写到了 B 的检查点上(B 已经停在自己的审批门):按绑定漂移处理,什么都不执行。

    第二组里 B 的调用 id 与参数都与 A 相同(有的兼容厂商每轮都从同一个 id 数起),
    只有按 run 戳换算的请求身份能分开。
    """
    key, call_id = b_call
    async with make_checkpointer("memory") as cp:
        g = _build(cp, [_gated_turn("A", "tc-a"), _gated_turn(key, call_id)])
        paused_a = await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        binding_a = pending_request_binding((await g.compiled.aget_state(_cfg("run-a"))).values)
        assert binding_a is not None
        paused_b = await g.compiled.ainvoke(
            {**_bare_input("b"), **APPROVAL_TURN_RESET}, config=_cfg("run-b")
        )
        assert paused_b["pending_approval"].request_id != binding_a["request_id"]
        await g.compiled.aupdate_state(
            _cfg("run-a"),
            {
                "pending_approval": None,
                "approval_resume": {
                    "decision": "approve",
                    "binding_digest": paused_a["pending_approval"].binding_digest,
                    **binding_a,
                },
            },
            as_node="agent",
        )

        state = await g.compiled.ainvoke(None, config=_cfg("run-a-cont"))

        assert g.gated.seen == []
        assert state["approval_outcome"] == "rejected"
        tail = state["messages"][-1]
        assert isinstance(tail, ToolMessage) and tail.tool_call_id == call_id
        assert "binding drift" in str(tail.content)


@pytest.mark.asyncio
async def test_a_verdict_for_this_turn_still_applies() -> None:
    """身份对得上:批准照常执行,改参照常按改后的参数执行。"""
    async with make_checkpointer("memory") as cp:
        g = _build(cp, [_gated_turn("A", "tc-a")])
        paused = await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        await _approve_a(g, paused)
        await g.compiled.ainvoke(None, config=_cfg("run-a-cont"))
        assert g.gated.seen == [{"key": "A"}]

    async with make_checkpointer("memory") as cp:
        g = _build(cp, [_gated_turn("A", "tc-a")])
        await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        binding = pending_request_binding((await g.compiled.aget_state(_cfg("run-a"))).values)
        assert binding is not None
        await g.compiled.aupdate_state(
            _cfg("run-a"),
            {
                "pending_approval": None,
                "approval_resume": {
                    "decision": "modify",
                    "modified_args": {"key": "X"},
                    "binding_digest": canonical_args_digest({"key": "X"}),
                    **binding,
                },
            },
            as_node="agent",
        )
        await g.compiled.ainvoke(None, config=_cfg("run-a-cont"))
        assert g.gated.seen == [{"key": "X"}]


def test_verdict_identity_checks_each_part() -> None:
    calls = [_call(_GATED, {"key": "A"}, "tc-a")]
    summary = "approval-gated tool 'lookup'"
    request_id = _stable_request_id("run-a", "tools", summary)
    good = {
        "decision": "reject",
        "request_id": request_id,
        "action_summary": summary,
        "tool_call_id": "tc-a",
        "tool_call_index": 0,
    }
    gated = frozenset({_GATED})
    ok = apply_resume_decision(calls, gated, good, turn_run_id="run-a")
    assert not ok.binding_drift and ok.terminal
    for bad, run_id in (
        (good, "run-b"),
        ({**good, "tool_call_id": "tc-other"}, "run-a"),
        ({**good, "tool_call_index": 3}, "run-a"),
        ({**good, "tool_call_index": None}, "run-a"),
    ):
        outcome = apply_resume_decision(calls, gated, bad, turn_run_id=run_id)
        assert outcome.binding_drift and outcome.terminal and outcome.tool_calls == []
    # 旧裁定(没有身份)与没有 run 戳的旧消息:跳过身份核对,不按漂移处理。旧裁定还
    # 没有绑定摘要,证明不了请求是声明式的,所以什么都不放行(B-76)。
    legacy = apply_resume_decision(calls, gated, {"decision": "approve"}, turn_run_id="run-b")
    assert not legacy.binding_drift and set(legacy.withheld) == {0}
    no_stamp = {k: v for k, v in good.items() if k not in ("tool_call_id", "tool_call_index")}
    assert not apply_resume_decision(calls, gated, no_stamp, turn_run_id=None).binding_drift


@pytest.mark.asyncio
async def test_repair_closes_a_turn_that_ended_before_its_tools_ran() -> None:
    """取消落在「模型给出调用」与「工具执行」之间:检查点里没有待审批,尾巴照样悬空。"""
    async with make_checkpointer("memory") as cp:
        g = _build(cp, [AIMessage(content="a-done")])
        await g.compiled.ainvoke(_bare_input("a"), config=_cfg("run-a"))
        cancelled = AIMessage(
            content="",
            tool_calls=[_call(_FREE, {"x": 5}, "tc-x")],
            additional_kwargs={STAMP_RUN_ID: "run-x"},
        )
        await g.compiled.aupdate_state(_cfg("run-x"), {"messages": [cancelled]}, as_node="agent")
        content_for = _ContentFor({"run-x": UNRECORDED_TOOL_CALL_CONTENT})

        assert await repair_unanswered_tail(g.compiled, _thread_cfg(), content_for=content_for) == 1

        next_turn = {**_bare_input("b"), **APPROVAL_TURN_RESET}
        state = await g.compiled.ainvoke(next_turn, config=_cfg("run-b"))
        assert sanitize_dangling_tool_calls(g.llm.prompts[-1]) == []
        assert [m.content for m in g.llm.prompts[-1] if isinstance(m, ToolMessage)] == [
            UNRECORDED_TOOL_CALL_CONTENT
        ]
        assert state["messages"][-1].content == "done"
        assert g.free.seen == []


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
    assert [m.content for m in update["messages"]] == [VOIDED_APPROVAL_CONTENT]
    custom = voided_turn_update([stamped], run_id="run-a", content=UNRECORDED_TOOL_CALL_CONTENT)
    assert custom is not None
    assert [m.content for m in custom["messages"]] == [UNRECORDED_TOOL_CALL_CONTENT]
