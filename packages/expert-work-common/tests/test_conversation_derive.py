"""derive_run_items —— 一轮消息 + 辅助信号 → 条目列表。

三条产出路径(实时 SSE / 单 run 回放 / 会话历史)共用这一个推导,所以这里
钉住的是对外可见的形状与顺序,不是某一条路径的实现细节。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from expert_work.common.conversation_derive import derive_run_items
from expert_work.common.conversation_items import ITEM_TYPES
from expert_work.common.message_stamp import STAMP_CREATED_AT, STAMP_RUN_ID
from expert_work.common.spotlight import spotlight_untrusted

RUN = "run-1"


def _types(items: list[Any]) -> list[str]:
    return [i.TYPE for i in items]


def _tool_call(call_id: str, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": call_id, "name": name, "args": args or {}, "type": "tool_call"}


class _StubMessage:
    """回放路径喂进来的不是 LangChain 类,而是从 ``updates`` 帧反序列化的对象。"""

    def __init__(self, **fields: Any) -> None:
        self.additional_kwargs: dict[str, Any] = {}
        for key, value in fields.items():
            setattr(self, key, value)


class _StubToolCall:
    def __init__(self, call_id: str, name: str, args: dict[str, Any]) -> None:
        self.id = call_id
        self.name = name
        self.args = args


def _full_run() -> list[Any]:
    """一轮典型的完整对话:提问 → 说明 + 调工具 → 工具结果 → 作答。"""
    return [
        SystemMessage(content="你是助手"),
        HumanMessage(content="北京天气"),
        AIMessage(content="我查一下", tool_calls=[_tool_call("c1", "weather", {"city": "北京"})]),
        ToolMessage(content="晴 30°C", tool_call_id="c1", name="weather"),
        AIMessage(content="北京今天晴,30 度"),
    ]


# --- 七种条目 -------------------------------------------------------------


def test_all_seven_item_types_can_be_produced() -> None:
    items = derive_run_items(
        run_id=RUN,
        messages=_full_run(),
        plan={"goal": "查天气", "steps": [{"title": "调用天气工具"}]},
        approvals=[
            {
                "request_id": "apr_1",
                "node": "tools",
                "reason_kind": "policy_gate",
                "action_summary": "调用天气工具",
                "proposed_args": {"city": "北京"},
                "requested_at": "2026-08-25T01:00:00+00:00",
                "timeout_at": "2026-08-26T01:00:00+00:00",
            }
        ],
        error="deadline exceeded",
    )
    assert set(_types(items)) == ITEM_TYPES


def test_system_messages_produce_nothing() -> None:
    items = derive_run_items(run_id=RUN, messages=[SystemMessage(content="你是助手")])
    assert items == []


def test_user_message_carries_text() -> None:
    (item,) = derive_run_items(run_id=RUN, messages=[HumanMessage(content="北京天气")])
    assert item.TYPE == "user_message"
    assert item.to_wire()["content"] == "北京天气"
    assert item.to_wire()["attachments"] == []


def test_user_message_keeps_non_text_blocks_as_attachments() -> None:
    """只发了一张图的那一轮:正文是空的,但内容不是空的。"""
    msg = HumanMessage(
        content=[{"type": "image_ref", "ref": "expert_work://image/abc"}],
    )
    (item,) = derive_run_items(run_id=RUN, messages=[msg])
    wire = item.to_wire()
    assert wire["type"] == "user_message"
    assert wire["content"] == ""
    assert wire["attachments"] == [{"type": "image_ref", "ref": "expert_work://image/abc"}]


def test_user_message_splits_text_and_attachments() -> None:
    msg = HumanMessage(
        content=[
            {"type": "text", "text": "这张图里是什么"},
            {"type": "image_ref", "ref": "expert_work://image/abc"},
        ],
    )
    (item,) = derive_run_items(run_id=RUN, messages=[msg])
    wire = item.to_wire()
    assert wire["content"] == "这张图里是什么"
    assert wire["attachments"] == [{"type": "image_ref", "ref": "expert_work://image/abc"}]


def test_blank_user_message_without_attachments_produces_nothing() -> None:
    assert derive_run_items(run_id=RUN, messages=[HumanMessage(content="   ")]) == []


def test_tool_result_fields_come_from_the_tool_message() -> None:
    msg = ToolMessage(
        content="炸了",
        tool_call_id="c1",
        name="weather",
        status="error",
        artifact={"truncated": True},
    )
    (item,) = derive_run_items(run_id=RUN, messages=[msg])
    wire = item.to_wire()
    assert wire["type"] == "tool_result"
    assert wire["call_id"] == "c1"
    assert wire["name"] == "weather"
    assert wire["status"] == "error"
    assert wire["content"] == "炸了"
    assert wire["artifact"] == {"truncated": True}


def test_tool_result_status_defaults_to_success() -> None:
    """LangChain 的 ToolMessage 自带 ``status="success"``,所以这条只证明它
    原样透出;真正走到默认值的是下面那条(回放路径的对象可能压根没这个字段)。"""
    (item,) = derive_run_items(
        run_id=RUN, messages=[ToolMessage(content="ok", tool_call_id="c1", name="t")]
    )
    assert item.to_wire()["status"] == "success"


def test_tool_result_status_defaults_to_success_when_the_field_is_absent() -> None:
    messages = [_StubMessage(type="tool", content="ok", tool_call_id="c1", name="t")]
    (item,) = derive_run_items(run_id=RUN, messages=messages)
    assert item.to_wire()["status"] == "success"


def test_plan_item_carries_goal_and_steps() -> None:
    items = derive_run_items(
        run_id=RUN,
        messages=[],
        plan={"goal": "查天气", "steps": [{"title": "第一步"}, {"title": "第二步"}]},
    )
    wire = items[0].to_wire()
    assert wire["type"] == "plan"
    assert wire["goal"] == "查天气"
    assert wire["steps"] == [{"title": "第一步"}, {"title": "第二步"}]


def test_approval_item_mirrors_the_frame() -> None:
    frame = {
        "run_id": RUN,
        "thread_id": "t1",
        "request_id": "apr_5f3a",
        "node": "tools",
        "reason_kind": "policy_gate",
        "action_summary": "即将发送邮件",
        "proposed_args": {"to": "finance@example.com"},
        "requested_at": "2026-08-15T03:43:14+00:00",
        "timeout_at": "2026-08-16T03:43:14+00:00",
        "binding_digest": "9b1f3c7a",
    }
    (item,) = derive_run_items(run_id=RUN, messages=[], approvals=[frame])
    wire = item.to_wire()
    assert wire["request_id"] == "apr_5f3a"
    assert wire["node"] == "tools"
    assert wire["reason_kind"] == "policy_gate"
    assert wire["action_summary"] == "即将发送邮件"
    assert wire["proposed_args"] == {"to": "finance@example.com"}
    assert wire["requested_at"] == "2026-08-15T03:43:14+00:00"
    assert wire["timeout_at"] == "2026-08-16T03:43:14+00:00"
    # 平台内部的绑定校验值不外泄,决策此刻还没有。
    assert "binding_digest" not in wire
    assert "decision" not in wire
    # 审批的产生时刻 = requested_at,客户端按 created_at 排序不必开特例。
    assert wire["created_at"] == "2026-08-15T03:43:14+00:00"


def test_error_item_carries_the_message() -> None:
    (item,) = derive_run_items(run_id=RUN, messages=[], error="deadline exceeded")
    assert item.to_wire() == {
        "id": f"{RUN}:0",
        "type": "error",
        "run_id": RUN,
        "created_at": None,
        "message": "deadline exceeded",
    }


# --- tool_calls -----------------------------------------------------------


def test_one_ai_message_with_many_tool_calls_yields_one_item_each() -> None:
    msg = AIMessage(
        content="并行查两个城市",
        tool_calls=[
            _tool_call("c1", "weather", {"city": "北京"}),
            _tool_call("c2", "weather", {"city": "上海"}),
            _tool_call("c3", "weather", {"city": "广州"}),
        ],
    )
    items = derive_run_items(run_id=RUN, messages=[msg])
    assert _types(items) == ["assistant_message", "tool_call", "tool_call", "tool_call"]
    assert [i.to_wire()["call_id"] for i in items[1:]] == ["c1", "c2", "c3"]
    assert [i.to_wire()["args"] for i in items[1:]] == [
        {"city": "北京"},
        {"city": "上海"},
        {"city": "广州"},
    ]
    # 每个 tool_call 各占一个 id 子序号。
    assert [i.id for i in items] == [f"{RUN}:{n}" for n in range(4)]


def test_tool_call_and_result_pair_on_call_id_not_position() -> None:
    """并行工具的结果可能乱序回来,客户端只能靠 call_id 配对。"""
    messages = [
        AIMessage(
            content="",
            tool_calls=[_tool_call("c1", "slow"), _tool_call("c2", "fast")],
        ),
        ToolMessage(content="fast 先回", tool_call_id="c2", name="fast"),
        ToolMessage(content="slow 后回", tool_call_id="c1", name="slow"),
    ]
    items = derive_run_items(run_id=RUN, messages=messages)
    calls = {i.to_wire()["call_id"]: i for i in items if i.TYPE == "tool_call"}
    results = {i.to_wire()["call_id"]: i for i in items if i.TYPE == "tool_result"}
    assert set(calls) == set(results) == {"c1", "c2"}
    assert calls["c1"].to_wire()["name"] == "slow"
    assert results["c1"].to_wire()["content"] == "slow 后回"
    assert results["c2"].to_wire()["content"] == "fast 先回"


def test_blank_assistant_text_with_tool_calls_yields_only_tool_calls() -> None:
    """空气泡不该渲染,但那一步工具确实发生了。"""
    messages = [
        HumanMessage(content="北京天气"),
        AIMessage(content="", tool_calls=[_tool_call("c1", "weather")]),
        ToolMessage(content="晴", tool_call_id="c1", name="weather"),
        AIMessage(content="   ", tool_calls=[_tool_call("c2", "weather")]),
    ]
    items = derive_run_items(run_id=RUN, messages=messages)
    assert _types(items) == ["user_message", "tool_call", "tool_result", "tool_call"]


def test_duck_typed_messages_derive_the_same_shapes() -> None:
    """推导按鸭子类型读消息 —— 不绑 LangChain 的具体类。"""
    messages = [
        _StubMessage(type="human", content="北京天气"),
        _StubMessage(type="ai", content="查到了", tool_calls=[_tool_call("c1", "weather")]),
        _StubMessage(
            type="tool", content="晴", tool_call_id="c1", name="weather", status="success"
        ),
    ]
    items = derive_run_items(run_id=RUN, messages=messages)
    assert _types(items) == ["user_message", "assistant_message", "tool_call", "tool_result"]
    assert items[2].to_wire()["call_id"] == "c1"


def test_tool_call_fields_read_object_shaped_calls() -> None:
    messages = [
        _StubMessage(type="ai", content="", tool_calls=[_StubToolCall("c1", "weather", {"n": 1})])
    ]
    (item,) = derive_run_items(run_id=RUN, messages=messages)
    assert item.to_wire()["call_id"] == "c1"
    assert item.to_wire()["name"] == "weather"
    assert item.to_wire()["args"] == {"n": 1}


def test_tool_call_fields_degrade_on_a_malformed_call() -> None:
    """字段缺失 / 类型不对不许炸掉整轮的读取 —— 历史读取宁可不完整也不能报错。"""
    messages = [
        _StubMessage(type="ai", content="", tool_calls=[{"name": None, "args": "不是字典"}])
    ]
    (item,) = derive_run_items(run_id=RUN, messages=messages)
    assert item.to_wire() == {
        "id": f"{RUN}:0",
        "type": "tool_call",
        "run_id": RUN,
        "created_at": None,
        "call_id": "",
        "name": "",
        "args": {},
    }


# --- channel --------------------------------------------------------------


def test_channel_final_only_for_the_last_bubble_without_tool_calls() -> None:
    items = derive_run_items(run_id=RUN, messages=_full_run())
    channels = [i.to_wire()["channel"] for i in items if i.TYPE == "assistant_message"]
    assert channels == ["commentary", "final"]


def test_channel_commentary_when_the_last_bubble_still_calls_tools() -> None:
    """一轮停在「还要调工具」上(比如撞了 max_steps),那条不是终稿。"""
    messages = [
        HumanMessage(content="北京天气"),
        AIMessage(content="我再查一次", tool_calls=[_tool_call("c1", "weather")]),
    ]
    items = derive_run_items(run_id=RUN, messages=messages)
    assert items[1].to_wire()["channel"] == "commentary"


def test_scheduled_delivery_opens_its_own_segment() -> None:
    """定时投递不能把用户那一段的 final 抢走。"""
    messages = [
        HumanMessage(content="北京天气"),
        AIMessage(content="北京今天晴"),
        AIMessage(
            content="[定时] 明天有雨", additional_kwargs={"expert_work_scheduled_delivery": True}
        ),
    ]
    items = derive_run_items(run_id=RUN, messages=messages)
    channels = [i.to_wire()["channel"] for i in items if i.TYPE == "assistant_message"]
    assert channels == ["final", "final"]


def test_channel_values_stay_inside_the_vocabulary() -> None:
    items = derive_run_items(run_id=RUN, messages=_full_run())
    for item in items:
        if item.TYPE == "assistant_message":
            assert item.to_wire()["channel"] in {"final", "commentary"}


# --- 隐藏消息 -------------------------------------------------------------


def test_hidden_scaffolding_never_reaches_the_items() -> None:
    """CM-1 的 <recovery-advisory> 之类是编排层写给模型自己看的。"""
    messages = [
        HumanMessage(content="北京天气"),
        HumanMessage(
            content="<recovery-advisory>工具失败了,换个思路",
            additional_kwargs={"expert_work_hide_from_ui": True},
        ),
        AIMessage(content="北京今天晴"),
    ]
    items = derive_run_items(run_id=RUN, messages=messages)
    assert _types(items) == ["user_message", "assistant_message"]
    assert all("recovery-advisory" not in str(i.to_wire()) for i in items)


def test_hidden_message_does_not_steal_final_from_the_real_answer() -> None:
    """隐藏行排在最后时,前面那条真答案仍然是终稿。"""
    messages = [
        HumanMessage(content="北京天气"),
        AIMessage(content="北京今天晴"),
        HumanMessage(
            content="[Reflection] 再检查一遍",
            additional_kwargs={"expert_work_hide_from_ui": True},
        ),
    ]
    items = derive_run_items(run_id=RUN, messages=messages)
    assert _types(items) == ["user_message", "assistant_message"]
    assert items[1].to_wire()["channel"] == "final"


def test_hidden_tool_result_is_excluded_too() -> None:
    messages = [
        ToolMessage(
            content="内部结果",
            tool_call_id="c1",
            name="t",
            additional_kwargs={"expert_work_hide_from_ui": True},
        )
    ]
    assert derive_run_items(run_id=RUN, messages=messages) == []


# --- 时间戳 ---------------------------------------------------------------


def test_created_at_comes_from_the_write_side_stamp() -> None:
    messages = [
        HumanMessage(
            content="北京天气",
            additional_kwargs={
                STAMP_CREATED_AT: "2026-08-25T01:00:00+00:00",
                STAMP_RUN_ID: RUN,
            },
        )
    ]
    (item,) = derive_run_items(run_id=RUN, messages=messages)
    assert item.to_wire()["created_at"] == "2026-08-25T01:00:00+00:00"


def test_missing_stamp_degrades_to_null_instead_of_a_made_up_time() -> None:
    (item,) = derive_run_items(run_id=RUN, messages=[HumanMessage(content="北京天气")])
    assert item.to_wire()["created_at"] is None


def test_corrupt_stamp_degrades_to_null() -> None:
    messages = [HumanMessage(content="北京天气", additional_kwargs={STAMP_CREATED_AT: 12345})]
    (item,) = derive_run_items(run_id=RUN, messages=messages)
    assert item.to_wire()["created_at"] is None


def test_tool_calls_inherit_their_messages_timestamp() -> None:
    messages = [
        AIMessage(
            content="",
            tool_calls=[_tool_call("c1", "t"), _tool_call("c2", "t")],
            additional_kwargs={STAMP_CREATED_AT: "2026-08-25T01:00:00+00:00"},
        )
    ]
    items = derive_run_items(run_id=RUN, messages=messages)
    assert [i.to_wire()["created_at"] for i in items] == ["2026-08-25T01:00:00+00:00"] * 2


# --- 防注入包装还原 -------------------------------------------------------


def test_tool_result_content_is_restored_from_the_injection_wrapping() -> None:
    """工具结果在内部带 spotlight 包装,直接显示是乱码。"""
    raw = "搜索结果 有效"
    wrapped = spotlight_untrusted(raw, nonce="0ce9b28d1a1e")
    (item,) = derive_run_items(
        run_id=RUN, messages=[ToolMessage(content=wrapped, tool_call_id="c1", name="search")]
    )
    content = item.to_wire()["content"]
    assert content == raw
    assert "UNTRUSTED" not in content
    assert "▁" not in content


def test_tool_result_keeps_the_trusted_overflow_footer() -> None:
    """溢出脚注是平台自己写的,排在围栏之外,不该被还原一起吃掉。"""
    wrapped = spotlight_untrusted("前 200 字…", nonce="n1")
    footer = "\n\n[full output saved to workspace://out.txt]"
    (item,) = derive_run_items(
        run_id=RUN,
        messages=[ToolMessage(content=wrapped + footer, tool_call_id="c1", name="bash")],
    )
    assert item.to_wire()["content"] == "前 200 字…" + footer


def test_unwrapped_tool_result_passes_through_unchanged() -> None:
    """关掉 spotlight 防御的 agent,工具结果本来就是裸的。"""
    (item,) = derive_run_items(
        run_id=RUN, messages=[ToolMessage(content="裸结果\n第二行", tool_call_id="c1", name="t")]
    )
    assert item.to_wire()["content"] == "裸结果\n第二行"


# --- 顺序 -----------------------------------------------------------------


def test_items_follow_message_order() -> None:
    items = derive_run_items(run_id=RUN, messages=_full_run())
    assert _types(items) == [
        "user_message",
        "assistant_message",
        "tool_call",
        "tool_result",
        "assistant_message",
    ]


def test_plan_sits_after_the_user_message_and_before_the_first_assistant_output() -> None:
    items = derive_run_items(run_id=RUN, messages=_full_run(), plan={"goal": "查天气"})
    assert _types(items)[:3] == ["user_message", "plan", "assistant_message"]


def test_plan_lands_at_the_end_when_the_run_produced_nothing_else() -> None:
    items = derive_run_items(
        run_id=RUN, messages=[HumanMessage(content="北京天气")], plan={"goal": "查天气"}
    )
    assert _types(items) == ["user_message", "plan"]


def test_error_sits_last_after_everything_the_run_did_produce() -> None:
    items = derive_run_items(run_id=RUN, messages=_full_run(), error="deadline exceeded")
    assert _types(items)[-1] == "error"
    assert _types(items)[0] == "user_message"


def test_approvals_sit_before_the_trailing_error() -> None:
    items = derive_run_items(
        run_id=RUN,
        messages=_full_run(),
        approvals=[
            {"request_id": "apr_1", "node": "tools", "reason_kind": "policy_gate"},
            {"request_id": "apr_2", "node": "tools", "reason_kind": "missing_info"},
        ],
        error="deadline exceeded",
    )
    assert _types(items)[-3:] == ["approval", "approval", "error"]
    assert [i.to_wire()["request_id"] for i in items if i.TYPE == "approval"] == [
        "apr_1",
        "apr_2",
    ]


def test_no_auxiliary_signals_means_no_auxiliary_items() -> None:
    items = derive_run_items(run_id=RUN, messages=_full_run())
    assert "plan" not in _types(items)
    assert "approval" not in _types(items)
    assert "error" not in _types(items)


# --- id -------------------------------------------------------------------


def test_ids_are_the_run_id_plus_a_dense_zero_based_index() -> None:
    items = derive_run_items(
        run_id=RUN,
        messages=_full_run(),
        plan={"goal": "查天气"},
        approvals=[{"request_id": "apr_1"}],
        error="炸了",
    )
    assert [i.id for i in items] == [f"{RUN}:{n}" for n in range(len(items))]


def test_ids_are_unique_within_one_derivation() -> None:
    items = derive_run_items(
        run_id=RUN,
        messages=[
            HumanMessage(content="并行"),
            AIMessage(content="", tool_calls=[_tool_call("c1", "t"), _tool_call("c2", "t")]),
            ToolMessage(content="a", tool_call_id="c1", name="t"),
            ToolMessage(content="b", tool_call_id="c2", name="t"),
            AIMessage(content="好了"),
        ],
        plan={"goal": "g"},
        error="炸了",
    )
    ids = [i.id for i in items]
    assert len(set(ids)) == len(ids)


def test_the_same_input_derives_the_same_ids_twice() -> None:
    """「同一查询可重复」是 id 唯一的另一半承诺。"""
    messages = _full_run()
    first = derive_run_items(run_id=RUN, messages=messages, plan={"goal": "g"})
    second = derive_run_items(run_id=RUN, messages=messages, plan={"goal": "g"})
    assert [i.id for i in first] == [i.id for i in second]


def test_run_id_is_stamped_on_every_item() -> None:
    items = derive_run_items(
        run_id=RUN,
        messages=_full_run(),
        plan={"goal": "g"},
        approvals=[{"request_id": "apr_1"}],
        error="炸了",
    )
    assert {i.run_id for i in items} == {RUN}
