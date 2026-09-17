"""B-76 —— 批准只放行审批单上那一个调用(班车 2 安全修复)。

审批单(``build_approval_request``)只展示这一轮里的**一个**调用。以前批准 / 改参
续跑时整轮调用都会执行,于是:

1. 同一轮两个门控调用,审批单只展示第一个,批准后第二个也跟着执行;
2. 模型自己发起的 ``ask_for_approval`` 与一个门控调用同轮,批准的是模型写的摘要,
   门控调用顺带执行(模型可控的绕过);
3. action screening 送审的是最低下标的不对齐调用,批准后其它调用(包括别的不对齐
   调用、门控调用)都执行;改参改的是 ``find_approval_target`` 找到的那条,可能根本
   不是审批单上那条。

修复后:只有审批单上那条执行;同轮其它调用各回一条「没有执行、需要就重新调用」的
工具结果,续跑照常回到模型。没有执行不算工具失败:不进恢复提示、不计失败指标、
不写派发审计、不触发手抄守卫。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from prometheus_client import REGISTRY

from expert_work.protocol import AuditEntry, canonical_args_digest
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.checkpointer import make_checkpointer
from orchestrator import (
    ActionVerdict,
    GraphRunner,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    build_react_graph,
    pending_request_binding,
)
from orchestrator.graph_builder._approval import (
    WITHHELD_CALL_CONTENT,
    apply_resume_decision,
)
from orchestrator.graph_builder._config import AUDIT_LOGGER_KEY
from orchestrator.sse import PROMPT_INPUTS_KEY
from orchestrator.tools.approval import ASK_FOR_APPROVAL_TOOL, AskForApprovalTool

LOGO = "https://files.example.com/brand/cover-1726394851207.png"
RETYPED = LOGO.replace("1726394851207", "17263948512077")


def _call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


@dataclass
class _LLM:
    """按脚本逐条回;脚本用完就收尾。记下每一步看到的消息。"""

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
class _Tool:
    name: str
    seen: list[dict[str, Any]] = field(default_factory=list)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=f"recording {self.name}", is_read_only=False)

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        del ctx
        self.seen.append(dict(args))
        return ToolResult(content=f"{self.name}-ran")


class _RecordingAuditLogger(AuditLogger):
    """图里按 ``isinstance(AuditLogger)`` 取审计 sink,所以继承,只覆盖 ``write``。"""

    def __init__(self) -> None:  # 不调 super():不需要 store / redactor / fallback
        self.entries: list[AuditEntry] = []

    async def write(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


@dataclass
class _JudgeByName:
    """action screening 替身:名字在 ``bad`` 里的调用判为不对齐。"""

    bad: frozenset[str]

    async def judge_action(
        self, *, user_request: str, tool_name: str, tool_args: Mapping[str, Any]
    ) -> ActionVerdict:
        del user_request, tool_args
        return ActionVerdict(aligned=tool_name not in self.bad, reason="test")


@dataclass
class _Outcome:
    paused: dict[str, Any]
    state: dict[str, Any]
    llm: _LLM
    tools: dict[str, _Tool]

    def tool_messages(self) -> list[ToolMessage]:
        return [m for m in self.state["messages"] if isinstance(m, ToolMessage)]


async def _pause_then_resume(
    turn: AIMessage,
    tool_names: Sequence[str],
    *,
    gated: frozenset[str] = frozenset(),
    decision: str = "approve",
    modified_args: dict[str, Any] | None = None,
    legacy: str | None = None,
    verdict_extra: Mapping[str, Any] | None = None,
    configurable: Mapping[str, Any] | None = None,
    **graph_kwargs: Any,
) -> _Outcome:
    """跑到审批暂停,照控制面的做法写裁定,再续跑。

    ``legacy`` —— 请求是班车 2 之前铸的(审批跨过了发布):

    * ``"old-request"``:新控制面写的裁定,带请求身份,但请求本身没有调用下标;
    * ``"old-writer"``:旧副本写的裁定,不带任何请求身份。
    """
    llm = _LLM(script=[turn])
    tools = {name: _Tool(name) for name in tool_names}
    registry = ToolRegistry()
    for tool in tools.values():
        registry.register(tool)
    registry.register(AskForApprovalTool())
    base: dict[str, Any] = {"thread_id": str(uuid4()), **(configurable or {})}
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=registry,
                approval_required_tools=gated,
                **graph_kwargs,
            )
        )
        cfg: RunnableConfig = {"configurable": {**base, "run_id": "run-a"}}
        paused = await compiled.ainvoke(
            {"messages": [HumanMessage(content="start")], "step_count": 0, "max_steps": 6},
            config=cfg,
        )
        request = paused["pending_approval"]
        assert request is not None
        assert all(not tool.seen for tool in tools.values()), "暂停前就执行了"
        binding = pending_request_binding((await compiled.aget_state(cfg)).values)
        assert binding is not None
        if legacy == "old-request":
            binding = {**binding, "tool_call_id": "", "tool_call_index": None}
        elif legacy == "old-writer":
            binding = {}
        digest = (
            canonical_args_digest(modified_args or {})
            if decision == "modify"
            else request.binding_digest
        )
        resume: dict[str, Any] = {
            "decision": decision,
            "modified_args": modified_args,
            "reason": None,
            "binding_digest": digest,
            **binding,
            **(verdict_extra or {}),
        }
        await compiled.aupdate_state(
            cfg, {"pending_approval": None, "approval_resume": resume}, as_node="agent"
        )
        state = await compiled.ainvoke(
            None, config={"configurable": {**base, "run_id": "run-a-cont"}}
        )
    return _Outcome(paused=paused, state=state, llm=llm, tools=tools)


def _assert_withheld(message: ToolMessage, *, call_id: str, name: str) -> None:
    assert message.tool_call_id == call_id
    assert message.name == name
    assert message.content == WITHHELD_CALL_CONTENT


# ---------------------------------------------------------------------------
# 探针复现的两种绕过
# ---------------------------------------------------------------------------


_VERDICT_SHAPES = pytest.mark.parametrize(
    "legacy", [None, "old-request", "old-writer"], ids=["verdict", "old-request", "old-writer"]
)


@_VERDICT_SHAPES
async def test_second_gated_call_does_not_ride_on_the_first_approval(legacy: str | None) -> None:
    turn = AIMessage(
        content="",
        tool_calls=[
            _call("send_email", {"to": "boss@example.com"}, "tc-1"),
            _call("delete_records", {"scope": "ALL"}, "tc-2"),
        ],
    )
    out = await _pause_then_resume(
        turn,
        ["send_email", "delete_records"],
        gated=frozenset({"send_email", "delete_records"}),
        legacy=legacy,
    )
    assert out.paused["pending_approval"].proposed_args == {"to": "boss@example.com"}

    assert out.tools["send_email"].seen == [{"to": "boss@example.com"}]
    assert out.tools["delete_records"].seen == [], "第二个门控调用没经审批就执行了"
    sent, held = out.tool_messages()
    assert sent.tool_call_id == "tc-1" and sent.content == "send_email-ran"
    _assert_withheld(held, call_id="tc-2", name="delete_records")
    # 续跑回到模型:模型看到了两条结果,给出下一步。
    assert out.state["messages"][-1].content == "done"
    assert [m.tool_call_id for m in out.llm.prompts[-1] if isinstance(m, ToolMessage)] == [
        "tc-1",
        "tc-2",
    ]
    assert out.state.get("approval_outcome") is None


@_VERDICT_SHAPES
async def test_agent_question_does_not_release_a_gated_call(legacy: str | None) -> None:
    turn = AIMessage(
        content="",
        tool_calls=[
            _call(
                ASK_FOR_APPROVAL_TOOL,
                {"reason_kind": "risk_confirmation", "action_summary": "Continue with the report?"},
                "tc-1",
            ),
            _call("delete_records", {"scope": "ALL"}, "tc-2"),
        ],
    )
    out = await _pause_then_resume(
        turn, ["delete_records"], gated=frozenset({"delete_records"}), legacy=legacy
    )
    assert out.paused["pending_approval"].action_summary == "Continue with the report?"

    assert out.tools["delete_records"].seen == [], "门控调用搭着模型自己的确认执行了"
    asked, held = out.tool_messages()
    assert asked.tool_call_id == "tc-1"
    _assert_withheld(held, call_id="tc-2", name="delete_records")
    assert out.state["messages"][-1].content == "done"


# ---------------------------------------------------------------------------
# action screening 送审:只放行 bad_idx 那条
# ---------------------------------------------------------------------------

#: 下标 0 是审批单上的那条(最低的不对齐下标);下标 1 是门控调用 ——
#: ``find_approval_target`` 会找到它,而它从没被送审;下标 2 是另一个不对齐调用。
_SCREENED_TURN = [
    ("echo_a", {"q": "a"}, "tc-a"),
    ("lookup", {"key": "k"}, "tc-l"),
    ("echo_b", {"q": "b"}, "tc-b"),
]


def _screened_turn() -> AIMessage:
    return AIMessage(content="", tool_calls=[_call(*spec) for spec in _SCREENED_TURN])


@pytest.mark.parametrize(
    ("decision", "modified", "expected_args"),
    [
        ("approve", None, {"q": "a"}),
        ("modify", {"q": "fixed"}, {"q": "fixed"}),
    ],
    ids=["approve", "modify"],
)
async def test_action_screen_verdict_releases_only_the_judged_call(
    decision: str, modified: dict[str, Any] | None, expected_args: dict[str, Any]
) -> None:
    out = await _pause_then_resume(
        _screened_turn(),
        ["echo_a", "lookup", "echo_b"],
        gated=frozenset({"lookup"}),
        decision=decision,
        modified_args=modified,
        action_judge=_JudgeByName(bad=frozenset({"echo_a", "echo_b"})),
        action_screen="approval",
    )
    request = out.paused["pending_approval"]
    assert (request.tool_call_id, request.tool_call_index) == ("tc-a", 0)

    assert out.tools["echo_a"].seen == [expected_args]
    assert out.tools["lookup"].seen == [], "没送审的门控调用执行了"
    assert out.tools["echo_b"].seen == [], "另一个不对齐调用执行了"
    ran, held_lookup, held_b = out.tool_messages()
    assert ran.tool_call_id == "tc-a" and ran.content == "echo_a-ran"
    _assert_withheld(held_lookup, call_id="tc-l", name="lookup")
    _assert_withheld(held_b, call_id="tc-b", name="echo_b")
    assert out.state["messages"][-1].content == "done"


@pytest.mark.parametrize("gated", [frozenset(), frozenset({"lookup"})], ids=["no-gate", "gate"])
@pytest.mark.parametrize(
    ("decision", "modified"),
    [("approve", None), ("modify", {"q": "fixed"})],
    ids=["approve", "modify"],
)
@pytest.mark.parametrize("legacy", ["old-request", "old-writer"])
async def test_legacy_action_screen_verdict_runs_nothing(
    legacy: str, decision: str, modified: dict[str, Any] | None, gated: frozenset[str]
) -> None:
    """请求没有下标时只能回退到声明式扫描,而 action screening 的请求不是扫描出来的:
    扫描找到的门控调用(``lookup``)从没被送审。批准的裁定摘要为空(送审铸造不绑定),
    改参的裁定摘要是改后参数的、证明不了什么 —— 两种都不能信,什么都不执行。"""
    out = await _pause_then_resume(
        _screened_turn(),
        ["echo_a", "lookup", "echo_b"],
        gated=gated,
        decision=decision,
        modified_args=modified,
        legacy=legacy,
        action_judge=_JudgeByName(bad=frozenset({"echo_a", "echo_b"})),
        action_screen="approval",
    )
    assert out.paused["pending_approval"].binding_digest == ""
    assert all(not tool.seen for tool in out.tools.values())
    for message, (name, _, call_id) in zip(out.tool_messages(), _SCREENED_TURN, strict=True):
        _assert_withheld(message, call_id=call_id, name=name)
    assert out.state["messages"][-1].content == "done"


@pytest.mark.parametrize("legacy", ["old-request", "old-writer"])
async def test_legacy_declarative_modify(legacy: str) -> None:
    """声明式请求的改参:新控制面带着请求摘要,对得上 → 按改后参数执行;旧副本不带
    摘要,证明不了请求是声明式的 → 什么都不执行,模型重发后再审一次。"""
    turn = AIMessage(
        content="",
        tool_calls=[_call("echo", {"q": 1}, "tc-0"), _call("send_email", {"to": "x"}, "tc-1")],
    )
    out = await _pause_then_resume(
        turn,
        ["echo", "send_email"],
        gated=frozenset({"send_email"}),
        decision="modify",
        modified_args={"to": "safe"},
        legacy=legacy,
    )
    assert out.tools["echo"].seen == []
    if legacy == "old-request":
        assert out.tools["send_email"].seen == [{"to": "safe"}]
    else:
        assert out.tools["send_email"].seen == []
        _assert_withheld(out.tool_messages()[1], call_id="tc-1", name="send_email")


# ---------------------------------------------------------------------------
# 同轮的非门控调用、顺序
# ---------------------------------------------------------------------------


async def test_ungated_sibling_is_withheld_and_results_keep_call_order() -> None:
    turn = AIMessage(
        content="",
        tool_calls=[
            _call("lookup", {"q": 1}, "tc-0"),
            _call("send_email", {"to": "x"}, "tc-1"),
            _call("echo", {"q": 2}, "tc-2"),
        ],
    )
    out = await _pause_then_resume(
        turn, ["lookup", "send_email", "echo"], gated=frozenset({"send_email"})
    )
    assert out.paused["pending_approval"].tool_call_index == 1

    assert out.tools["send_email"].seen == [{"to": "x"}]
    assert out.tools["lookup"].seen == [] and out.tools["echo"].seen == []
    messages = out.tool_messages()
    assert [m.tool_call_id for m in messages] == ["tc-0", "tc-1", "tc-2"]
    assert [m.content for m in messages] == [
        WITHHELD_CALL_CONTENT,
        "send_email-ran",
        WITHHELD_CALL_CONTENT,
    ]
    # 紧跟在那条助手消息后面,一条不少。
    tail = out.state["messages"][-5:]
    assert isinstance(tail[0], AIMessage) and len(tail[0].tool_calls) == 3
    assert tail[1:4] == messages


async def test_out_of_range_index_runs_nothing() -> None:
    """裁定带下标但指不到这一轮的任何调用(调用 id 缺失时身份核对跳过这一项)。"""
    turn = AIMessage(content="", tool_calls=[_call("send_email", {"to": "x"}, "tc-1")])
    out = await _pause_then_resume(
        turn,
        ["send_email"],
        gated=frozenset({"send_email"}),
        verdict_extra={"tool_call_id": "", "tool_call_index": 7},
    )
    assert out.tools["send_email"].seen == []
    (held,) = out.tool_messages()
    _assert_withheld(held, call_id="tc-1", name="send_email")


# ---------------------------------------------------------------------------
# 没有执行 ≠ 工具失败
# ---------------------------------------------------------------------------


def _sample(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels=labels) or 0.0


async def test_withheld_call_is_not_a_failure_nor_audited_nor_guarded() -> None:
    """被扣下的 ``save_artifact`` 若按失败分类,会被当成「写入没落地」进恢复提示;
    被扣下的 ``exec_python`` 里有手抄 URL,若先走守卫就会写一行 ``tool:blocked``。"""
    turn = AIMessage(
        content="",
        tool_calls=[
            _call("send_email", {"to": "x"}, "tc-1"),
            _call("save_artifact", {"name": "report.md", "content": "x"}, "tc-2"),
            _call("exec_python", {"code": f"get('{RETYPED}')"}, "tc-3"),
        ],
    )
    audit = _RecordingAuditLogger()
    not_landed = {"error_class": "mutation_not_landed", "tool": "save_artifact"}
    blocked = {"tool": "exec_python", "outcome": "blocked"}
    not_landed_before = _sample("expert_work_cm_tool_error_total", not_landed)
    blocked_before = _sample("expert_work_tool_call_total", blocked)

    out = await _pause_then_resume(
        turn,
        ["send_email", "save_artifact", "exec_python"],
        gated=frozenset({"send_email"}),
        trusted_input_names=frozenset({"org_logo"}),
        configurable={
            "tenant_id": str(uuid4()),
            AUDIT_LOGGER_KEY: audit,
            PROMPT_INPUTS_KEY: {"org_logo": LOGO},
        },
    )

    assert out.tools["send_email"].seen == [{"to": "x"}]
    assert out.tools["save_artifact"].seen == [] and out.tools["exec_python"].seen == []
    _, held_save, held_exec = out.tool_messages()
    _assert_withheld(held_save, call_id="tc-2", name="save_artifact")
    _assert_withheld(held_exec, call_id="tc-3", name="exec_python")
    # 审计只有被批那条的派发行。
    assert [(e.action.value, e.details.get("call_id")) for e in audit.entries] == [
        ("tool:call", "tc-1")
    ]
    # 模型下一步看不到恢复提示;失败 / 拦截计数都没动。
    assert not [
        m
        for m in out.llm.prompts[-1]
        if isinstance(m, HumanMessage) and "<recovery-advisory>" in str(m.content)
    ]
    assert _sample("expert_work_cm_tool_error_total", not_landed) == not_landed_before
    assert _sample("expert_work_tool_call_total", blocked) == blocked_before


# ---------------------------------------------------------------------------
# apply_resume_decision 单元
# ---------------------------------------------------------------------------

_GATED = frozenset({"send_email"})


def _two_calls() -> list[dict[str, Any]]:
    return [_call("lookup", {"q": 1}, "tc-0"), _call("send_email", {"to": "x"}, "tc-1")]


def test_verdict_index_picks_the_released_call() -> None:
    calls = _two_calls()
    outcome = apply_resume_decision(
        calls, _GATED, {"decision": "approve", "tool_call_id": "tc-0", "tool_call_index": 0}
    )
    assert [c["id"] for c in outcome.tool_calls] == ["tc-0", "tc-1"]
    assert set(outcome.withheld) == {1}
    assert outcome.reject_messages == [] and not outcome.terminal


_SEND_SUMMARY = "approval-gated tool 'send_email'"


def test_legacy_declarative_verdict_trusts_the_scan() -> None:
    """没有下标:声明式扫描找到的门控调用,在裁定能证明请求是声明式时才放行。"""
    bound = canonical_args_digest({"to": "x"})
    approve = apply_resume_decision(
        _two_calls(), _GATED, {"decision": "approve", "binding_digest": bound}
    )
    assert set(approve.withheld) == {0} and not approve.binding_drift
    # 参数与绑定对不上 → 照旧是完整性否决。
    drift = apply_resume_decision(
        _two_calls(),
        _GATED,
        {"decision": "approve", "binding_digest": canonical_args_digest({"to": "other"})},
    )
    assert drift.binding_drift and drift.terminal
    modify = {
        "decision": "modify",
        "modified_args": {"to": "y"},
        "binding_digest": canonical_args_digest({"to": "y"}),
        "action_summary": _SEND_SUMMARY,
    }
    modified = apply_resume_decision(_two_calls(), _GATED, modify)
    assert set(modified.withheld) == {0}
    assert [c["args"] for c in modified.tool_calls] == [{"q": 1}, {"to": "y"}]


@pytest.mark.parametrize(
    "verdict",
    [
        {"decision": "approve"},
        {"decision": "approve", "binding_digest": ""},
        {"decision": "approve", "binding_digest": "", "action_summary": _SEND_SUMMARY},
        # 改参带的是改后参数的摘要,证明不了请求是声明式的;得靠请求摘要。
        {
            "decision": "modify",
            "modified_args": {"to": "y"},
            "binding_digest": canonical_args_digest({"to": "y"}),
        },
    ],
    ids=["no-digest", "empty-digest", "empty-digest-with-summary", "modify-without-summary"],
)
def test_legacy_verdict_that_cannot_prove_a_declarative_request_releases_nothing(
    verdict: dict[str, Any],
) -> None:
    outcome = apply_resume_decision(_two_calls(), _GATED, verdict)
    assert set(outcome.withheld) == {0, 1}
    assert [c["args"] for c in outcome.tool_calls] == [{"q": 1}, {"to": "x"}]
    assert not outcome.binding_drift and not outcome.terminal


@pytest.mark.parametrize(
    "summary",
    ["approval-gated tool 'echo'", "Continue?", ""],
    ids=["other-tool", "agent-text", "empty"],
)
def test_legacy_summary_mismatch_releases_nothing(summary: str) -> None:
    verdict = {
        "decision": "approve",
        "binding_digest": canonical_args_digest({"to": "x"}),
        "action_summary": summary,
    }
    outcome = apply_resume_decision(_two_calls(), _GATED, verdict)
    assert set(outcome.withheld) == {0, 1}
    matching = apply_resume_decision(
        _two_calls(), _GATED, {**verdict, "action_summary": _SEND_SUMMARY}
    )
    assert set(matching.withheld) == {0}


def test_legacy_agent_question_is_the_released_call() -> None:
    """``ask_for_approval``:请求展示的就是这条调用本身,不需要绑定来证明。"""
    calls = [
        _call(ASK_FOR_APPROVAL_TOOL, {"action_summary": "go on?"}, "tc-0"),
        _call("send_email", {"to": "x"}, "tc-1"),
    ]
    for verdict in (
        {"decision": "approve", "binding_digest": ""},
        {"decision": "approve", "action_summary": "go on?"},
    ):
        outcome = apply_resume_decision(calls, _GATED, verdict)
        assert set(outcome.withheld) == {1}
    mismatch = apply_resume_decision(
        calls, _GATED, {"decision": "approve", "action_summary": "something else"}
    )
    assert set(mismatch.withheld) == {0, 1}
    # 没给摘要时请求用的默认文案。
    bare = [_call(ASK_FOR_APPROVAL_TOOL, {}, "tc-0")]
    default = apply_resume_decision(
        bare,
        frozenset(),
        {"decision": "approve", "action_summary": "agent requested human approval"},
    )
    assert default.withheld == {}


@pytest.mark.parametrize("decision", ["approve", "modify"])
def test_no_released_call_withholds_everything(decision: str) -> None:
    calls = _two_calls()
    outcome = apply_resume_decision(
        calls, frozenset(), {"decision": decision, "modified_args": {"q": 9}}
    )
    assert set(outcome.withheld) == {0, 1}
    assert [c["args"] for c in outcome.tool_calls] == [{"q": 1}, {"to": "x"}]
    assert not outcome.binding_drift and not outcome.terminal


def test_modify_rewrites_only_the_released_call_and_checks_its_binding() -> None:
    calls = [_call("send_email", {"to": "a"}, "tc-0"), _call("send_email", {"to": "b"}, "tc-1")]
    verdict = {
        "decision": "modify",
        "modified_args": {"to": "safe"},
        "binding_digest": canonical_args_digest({"to": "safe"}),
        "tool_call_id": "tc-1",
        "tool_call_index": 1,
    }
    outcome = apply_resume_decision(calls, _GATED, verdict)
    assert [c["args"] for c in outcome.tool_calls] == [{"to": "a"}, {"to": "safe"}]
    assert set(outcome.withheld) == {0}
    drift = apply_resume_decision(
        calls, _GATED, {**verdict, "binding_digest": canonical_args_digest({"to": "b"})}
    )
    assert drift.binding_drift and drift.terminal and drift.tool_calls == []
    assert drift.withheld == {}


def test_approve_checks_the_binding_of_the_released_call() -> None:
    calls = [_call("send_email", {"to": "a"}, "tc-0"), _call("send_email", {"to": "b"}, "tc-1")]
    verdict = {"decision": "approve", "tool_call_id": "tc-1", "tool_call_index": 1}
    ok = apply_resume_decision(
        calls, _GATED, {**verdict, "binding_digest": canonical_args_digest({"to": "b"})}
    )
    assert not ok.binding_drift and set(ok.withheld) == {0}
    # 对着下标 0 的参数签的摘要 —— 被放行的是下标 1,对不上。
    drift = apply_resume_decision(
        calls, _GATED, {**verdict, "binding_digest": canonical_args_digest({"to": "a"})}
    )
    assert drift.binding_drift and drift.terminal


def test_reject_is_unchanged() -> None:
    outcome = apply_resume_decision(
        _two_calls(),
        _GATED,
        {"decision": "reject", "reason": "no", "tool_call_id": "tc-1", "tool_call_index": 1},
    )
    assert outcome.tool_calls == [] and outcome.withheld == {}
    assert [m.content for m in outcome.reject_messages] == ["[approval rejected] no"] * 2
    assert outcome.terminal
