"""条目模型的契约测试 —— 词表与类必须一一对应,且永远不许单边漂移。

本仓库有过「同一词表分散多处实现然后漂了」的先例:加了类忘了词表(对外
文档少一种类型)、或改了词表忘了类(端点返回一个客户端词表里没有的
``type``)。这一组测试就是钉子。
"""

from __future__ import annotations

from typing import get_args

import pytest

from expert_work.common.conversation_items import (
    CHANNELS,
    ITEM_CLASSES,
    ITEM_TYPES,
    TOOL_STATUSES,
    ApprovalItem,
    AssistantMessageItem,
    ConversationItem,
    ErrorItem,
    PlanItem,
    ToolCallItem,
    ToolResultItem,
    UserMessageItem,
)


def test_item_classes_cover_exactly_the_vocabulary() -> None:
    assert {cls.TYPE for cls in ITEM_CLASSES} == ITEM_TYPES


def test_item_classes_have_no_duplicate_type() -> None:
    """集合相等挡不住「两个类共用一个 TYPE、另一种类型没人实现」。"""
    assert len(ITEM_CLASSES) == len(ITEM_TYPES)


def test_conversation_item_union_matches_item_classes() -> None:
    """返回类型的联合必须与 ITEM_CLASSES 同集合 —— 少一个的类端点返回不出来。"""
    assert set(get_args(ConversationItem)) == set(ITEM_CLASSES)


def _one_of_each() -> list[ConversationItem]:
    return [
        UserMessageItem(id="r:0", run_id="r", created_at=None, content="你好"),
        AssistantMessageItem(
            id="r:1", run_id="r", created_at=None, content="答案", channel="final"
        ),
        ToolCallItem(id="r:2", run_id="r", created_at=None, call_id="c1", name="t", args={}),
        ToolResultItem(
            id="r:3",
            run_id="r",
            created_at=None,
            call_id="c1",
            name="t",
            status="success",
            content="结果",
        ),
        PlanItem(id="r:4", run_id="r", created_at=None, goal="目标"),
        ApprovalItem(
            id="r:5",
            run_id="r",
            created_at=None,
            request_id="apr_1",
            node="tools",
            reason_kind="policy_gate",
            action_summary="发一封邮件",
        ),
        ErrorItem(id="r:6", run_id="r", created_at=None, message="炸了"),
    ]


@pytest.mark.parametrize("item", _one_of_each(), ids=lambda i: i.TYPE)
def test_to_wire_carries_the_four_common_fields_and_its_own_type(
    item: ConversationItem,
) -> None:
    wire = item.to_wire()
    assert wire["type"] == item.TYPE
    assert wire["type"] in ITEM_TYPES
    assert wire["id"] == item.id
    assert wire["run_id"] == item.run_id
    assert "created_at" in wire  # 缺时刻要给 null,不是把键去掉


def test_to_wire_drops_absent_optional_fields_but_keeps_empty_collections() -> None:
    """缺席与「空但确实是这个值」对客户端是两回事。"""
    wire = ToolCallItem(
        id="r:0", run_id="r", created_at=None, call_id="c1", name="t", args={}
    ).to_wire()
    assert "worker" not in wire  # 没有子任务 = 这个概念不适用
    assert wire["args"] == {}  # 有参数这个概念,只是空

    user = UserMessageItem(id="r:1", run_id="r", created_at=None, content="hi").to_wire()
    assert user["attachments"] == []


def test_channel_and_tool_status_vocabularies() -> None:
    assert CHANNELS == {"final", "commentary"}
    assert TOOL_STATUSES == {"success", "error"}


def test_approval_item_carries_the_whole_approval_frame() -> None:
    """审批帧的关键字段一个都不能少 —— 少了客户端就没法判断拒绝的后果、
    也画不出倒计时。``binding_digest`` 有意不在其中。"""
    wire = ApprovalItem(
        id="r:0",
        run_id="r",
        created_at="2026-08-15T03:43:14+00:00",
        request_id="apr_5f3a",
        node="tools",
        reason_kind="policy_gate",
        action_summary="即将发送邮件",
        proposed_args={"to": "a@example.com"},
        requested_at="2026-08-15T03:43:14+00:00",
        timeout_at="2026-08-16T03:43:14+00:00",
    ).to_wire()
    assert wire["request_id"] == "apr_5f3a"
    assert wire["node"] == "tools"
    assert wire["reason_kind"] == "policy_gate"
    assert wire["action_summary"] == "即将发送邮件"
    assert wire["proposed_args"] == {"to": "a@example.com"}
    assert wire["requested_at"] == "2026-08-15T03:43:14+00:00"
    assert wire["timeout_at"] == "2026-08-16T03:43:14+00:00"
    assert "binding_digest" not in wire


def test_approval_decision_absent_until_decided() -> None:
    """live 发出时还没有决策 —— 缺席,而不是编一个「待定」值。"""
    common = {
        "id": "r:0",
        "run_id": "r",
        "created_at": None,
        "request_id": "apr_1",
        "node": "tools",
        "reason_kind": "risk_confirmation",
        "action_summary": "确认",
    }
    assert "decision" not in ApprovalItem(**common).to_wire()  # type: ignore[arg-type]
    assert ApprovalItem(**common, decision="approved").to_wire()["decision"] == "approved"  # type: ignore[arg-type]


def test_approval_timeout_window_is_never_faked() -> None:
    """取不到时间窗就让键缺席 —— 服务端替客户端编一个默认值是明令禁止的。"""
    wire = ApprovalItem(
        id="r:0",
        run_id="r",
        created_at=None,
        request_id="apr_1",
        node="tools",
        reason_kind="missing_info",
        action_summary="补充信息",
    ).to_wire()
    assert "requested_at" not in wire
    assert "timeout_at" not in wire


def test_items_are_immutable() -> None:
    item = ErrorItem(id="r:0", run_id="r", created_at=None, message="炸了")
    with pytest.raises(Exception):  # noqa: B017 — frozen dataclass 抛 FrozenInstanceError
        item.message = "改了"  # type: ignore[misc]
