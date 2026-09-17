"""B-79 —— 被批准的 ``ask_for_approval`` 由平台作答,不派发。

``ask_for_approval`` 是一个提问,不是动作:它的工具体只是一道防御,被执行时回一句
``[tool error] ask_for_approval must be handled by the approval gate …``。以前批准之后
它会被派发,模型读到的是「工具出错」;B-76 之后同一步里的动作又都没有执行,模型要从
这两条互相矛盾的结果里自己判断。现在它得到一条 ``[approved]`` 结果(改参时附上审核人
给的参数),同一步里的其它调用照 B-76 不执行;拒绝不变。
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from orchestrator.graph_builder._approval import (
    APPROVED_QUESTION_CONTENT,
    WITHHELD_CALL_CONTENT,
    apply_resume_decision,
)
from orchestrator.tools.approval import ASK_FOR_APPROVAL_TOOL

from .test_approval_only_approved_call import _call, _pause_then_resume

_QUESTION = {
    "reason_kind": "risk_confirmation",
    "action_summary": "Send the report to the customer?",
    "proposed_args": {"to": "cust@example.com"},
}


def _question_calls() -> list[dict[str, Any]]:
    return [
        _call(ASK_FOR_APPROVAL_TOOL, dict(_QUESTION), "tc-ask"),
        _call("send_report", {"to": "cust@example.com"}, "tc-send"),
    ]


def _question_turn() -> AIMessage:
    return AIMessage(content="", tool_calls=_question_calls())


def _answers(messages: list[ToolMessage]) -> list[tuple[str, str, Any]]:
    return [(m.tool_call_id, m.status, m.content) for m in messages]


@pytest.mark.parametrize(
    ("decision", "modified", "content"),
    [
        ("approve", None, APPROVED_QUESTION_CONTENT),
        (
            "modify",
            {"to": "ops@example.com", "note": "已审核"},
            APPROVED_QUESTION_CONTENT
            + ' The human set these arguments: {"to":"ops@example.com","note":"已审核"}',
        ),
    ],
    ids=["approve", "modify"],
)
async def test_approved_question_is_answered_and_the_action_is_withheld(
    decision: str, modified: dict[str, Any] | None, content: str
) -> None:
    out = await _pause_then_resume(
        _question_turn(), ["send_report"], decision=decision, modified_args=modified
    )
    assert out.paused["pending_approval"].action_summary == "Send the report to the customer?"

    assert out.tools["send_report"].seen == []
    assert _answers(out.tool_messages()) == [
        ("tc-ask", "success", content),
        ("tc-send", "error", WITHHELD_CALL_CONTENT),
    ]
    # 模型下一步看到的就是这两条,没有「工具出错」。
    seen = [m for m in out.llm.prompts[-1] if isinstance(m, ToolMessage)]
    assert _answers(seen) == _answers(out.tool_messages())
    assert out.state["messages"][-1].content == "done"


async def test_rejected_question_is_unchanged() -> None:
    out = await _pause_then_resume(_question_turn(), ["send_report"], decision="reject")
    assert out.tools["send_report"].seen == []
    assert [(m.status, m.content) for m in out.tool_messages()] == [
        ("error", "[approval rejected] approval rejected by reviewer")
    ] * 2
    # 模型自己发起的确认被拒不终止:回到模型。
    assert out.state.get("approval_outcome") is None
    assert out.state["messages"][-1].content == "done"


def test_answer_is_separate_from_withheld() -> None:
    calls = _question_calls()
    verdict = {"decision": "approve", "tool_call_id": "tc-ask", "tool_call_index": 0}
    outcome = apply_resume_decision(calls, frozenset(), verdict)
    assert set(outcome.withheld) == {1}
    assert {i: (m.tool_call_id, m.name) for i, m in outcome.answered.items()} == {
        0: ("tc-ask", ASK_FOR_APPROVAL_TOOL)
    }
    # 审批单上是别的调用时,不会有这条回答。
    gated = apply_resume_decision(
        calls, frozenset({"send_report"}), {**verdict, "tool_call_index": 1, "tool_call_id": ""}
    )
    assert gated.answered == {} and set(gated.withheld) == {0}
    rejected = apply_resume_decision(calls, frozenset(), {**verdict, "decision": "reject"})
    assert rejected.answered == {} and rejected.withheld == {}


def test_modified_arguments_that_are_not_plain_json_do_not_break_the_answer() -> None:
    # 终审复核 N1 —— 序列化抛错会让续跑卡死在这一步(每次复活都重抛)。
    from datetime import date

    from orchestrator.graph_builder._approval import _approved_question

    message = _approved_question(
        _question_calls()[0], "modify", {"due": date(2026, 9, 24), "tags": {"a"}}
    )
    assert isinstance(message.content, str)
    assert '"due":"2026-09-24"' in message.content


def test_modified_arguments_are_capped_in_the_answer() -> None:
    # 终审复核 N2 —— 审核人填的参数没有长度上限,原样拼进提示词会绕过工具输出预算。
    from orchestrator.graph_builder._approval import (
        APPROVED_ARGS_MAX_CHARS,
        _approved_question,
    )

    message = _approved_question(_question_calls()[0], "modify", {"note": "x" * 2_000_000})
    assert isinstance(message.content, str)
    assert message.content.startswith(APPROVED_QUESTION_CONTENT)
    assert len(message.content) < len(APPROVED_QUESTION_CONTENT) + APPROVED_ARGS_MAX_CHARS + 100
    assert message.content.endswith("…(truncated)")
