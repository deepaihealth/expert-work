"""derive_run_items —— 一轮消息 + 辅助信号 → 条目列表。

三条产出路径(实时 SSE / 单 run 回放 / 会话历史)共用这一个推导,所以这里
钉住的是对外可见的形状与顺序,不是某一条路径的实现细节。

**每个用例都跑两种消息形态**:会话历史读 checkpoint,拿到 ``BaseMessage``
对象;实时 SSE 与单 run 回放喂的是 ``updates`` 帧,里面的消息在
``orchestrator/sse.py`` 的 ``_to_jsonable`` 里已经变成 ``model_dump()`` 的
dict。只测一种形态曾经让「dict 进来产出空列表且不报错」活了下来 —— 参数化
是为了让下次新增路径时不必再靠人记得补测。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from expert_work.common.conversation_derive import derive_run_items
from expert_work.common.conversation_items import ITEM_TYPES, AuxFrame, ConversationItem
from expert_work.common.message_stamp import STAMP_CREATED_AT, STAMP_RUN_ID
from expert_work.common.spotlight import spotlight_untrusted

RUN = "run-1"

#: 参数化后的推导入口(见 :func:`derive` fixture)。
_Derive = Callable[..., list[ConversationItem]]


def _as_dicts(messages: Sequence[Any]) -> list[dict[str, Any]]:
    """对象形态 → 真实的 ``model_dump()``。

    **不手写字典**:手写的键集合会和 LangChain 真正 dump 出来的漂,那样测的
    就是我以为的形状,不是流上真实的形状。
    """
    for msg in messages:
        assert hasattr(msg, "model_dump"), f"{type(msg).__name__} 不是 LangChain 消息,dump 不了"
    return [msg.model_dump() for msg in messages]


@pytest.fixture(params=["object", "dict"])
def derive(request: pytest.FixtureRequest) -> _Derive:
    """把每个用例各跑一遍对象形态与 dict 形态。"""
    form: str = request.param

    def _run(*, messages: Sequence[Any], **kwargs: Any) -> list[ConversationItem]:
        payload = _as_dicts(messages) if form == "dict" else messages
        return derive_run_items(messages=payload, **kwargs)

    return _run


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


def test_all_seven_item_types_can_be_produced(derive: _Derive) -> None:
    items = derive(
        run_id=RUN,
        messages=_full_run(),
        plan=AuxFrame(data={"goal": "查天气", "steps": [{"title": "调用天气工具"}]}),
        approvals=[
            AuxFrame(
                data={
                    "request_id": "apr_1",
                    "node": "tools",
                    "reason_kind": "policy_gate",
                    "action_summary": "调用天气工具",
                    "proposed_args": {"city": "北京"},
                    "requested_at": "2026-08-25T01:00:00+00:00",
                    "timeout_at": "2026-08-26T01:00:00+00:00",
                }
            )
        ],
        error=AuxFrame(data={"message": "deadline exceeded"}),
    )
    assert set(_types(items)) == ITEM_TYPES


def test_system_messages_produce_nothing(derive: _Derive) -> None:
    """否定断言先证兄弟渲染 —— 只断言「结果是空的」时,推导整体失灵也算通过。"""
    messages = [SystemMessage(content="你是助手"), HumanMessage(content="北京天气")]
    assert _types(derive(run_id=RUN, messages=messages)) == ["user_message"]


def test_user_message_carries_text(derive: _Derive) -> None:
    (item,) = derive(run_id=RUN, messages=[HumanMessage(content="北京天气")])
    assert item.TYPE == "user_message"
    assert item.to_wire()["content"] == "北京天气"
    assert item.to_wire()["attachments"] == []


def test_user_message_keeps_non_text_blocks_as_attachments(derive: _Derive) -> None:
    """只发了一张图的那一轮:正文是空的,但内容不是空的。"""
    msg = HumanMessage(
        content=[{"type": "image_ref", "ref": "expert_work://image/abc"}],
    )
    (item,) = derive(run_id=RUN, messages=[msg])
    wire = item.to_wire()
    assert wire["type"] == "user_message"
    assert wire["content"] == ""
    assert wire["attachments"] == [{"type": "image_ref", "ref": "expert_work://image/abc"}]


def test_user_message_splits_text_and_attachments(derive: _Derive) -> None:
    msg = HumanMessage(
        content=[
            {"type": "text", "text": "这张图里是什么"},
            {"type": "image_ref", "ref": "expert_work://image/abc"},
        ],
    )
    (item,) = derive(run_id=RUN, messages=[msg])
    wire = item.to_wire()
    assert wire["content"] == "这张图里是什么"
    assert wire["attachments"] == [{"type": "image_ref", "ref": "expert_work://image/abc"}]


def test_blank_user_message_without_attachments_produces_nothing(derive: _Derive) -> None:
    messages = [HumanMessage(content="   "), AIMessage(content="在的")]
    assert _types(derive(run_id=RUN, messages=messages)) == ["assistant_message"]


def test_tool_result_fields_come_from_the_tool_message(derive: _Derive) -> None:
    msg = ToolMessage(
        content="炸了",
        tool_call_id="c1",
        name="weather",
        status="error",
        artifact={"truncated": True},
    )
    (item,) = derive(run_id=RUN, messages=[msg])
    wire = item.to_wire()
    assert wire["type"] == "tool_result"
    assert wire["call_id"] == "c1"
    assert wire["name"] == "weather"
    assert wire["status"] == "error"
    assert wire["content"] == "炸了"
    assert wire["artifact"] == {"truncated": True}


def test_tool_result_status_defaults_to_success(derive: _Derive) -> None:
    """LangChain 的 ToolMessage 自带 ``status="success"``,所以这条只证明它
    原样透出;真正走到默认值的是下面那条(回放路径的对象可能压根没这个字段)。"""
    (item,) = derive(run_id=RUN, messages=[ToolMessage(content="ok", tool_call_id="c1", name="t")])
    assert item.to_wire()["status"] == "success"


def test_tool_result_status_defaults_to_success_when_the_field_is_absent() -> None:
    messages = [_StubMessage(type="tool", content="ok", tool_call_id="c1", name="t")]
    (item,) = derive_run_items(run_id=RUN, messages=messages)
    assert item.to_wire()["status"] == "success"


def test_unnamed_tool_result_still_carries_an_empty_name(derive: _Derive) -> None:
    """``name`` 是 tool_result 的必有字段(``str``),取不到时给 ``""`` 而不是
    让键消失 —— 客户端解构 ``item.name`` 不该拿到 undefined。

    ``model_dump()`` 会把没设的字段落成显式 ``None``,所以这条同时钉住访问器
    「显式 None 归一成缺席默认值」那一步。
    """
    (item,) = derive(run_id=RUN, messages=[ToolMessage(content="ok", tool_call_id="c1")])
    wire = item.to_wire()
    assert wire["name"] == ""


def test_tool_result_carries_duration_ms(derive: _Derive) -> None:
    """工具卡上的耗时 —— 派发处量的墙钟,写在 ToolMessage 的 kwargs 里。"""
    msg = ToolMessage(
        content="ok", tool_call_id="c1", name="weather", additional_kwargs={"duration_ms": 842}
    )
    (item,) = derive(run_id=RUN, messages=[msg])
    assert item.to_wire()["duration_ms"] == 842


def test_tool_result_without_duration_omits_the_field(derive: _Derive) -> None:
    """没量到时长的结果(老消息)不该渲染成「耗时 0 毫秒」。"""
    (item,) = derive(run_id=RUN, messages=[ToolMessage(content="ok", tool_call_id="c1", name="t")])
    assert "duration_ms" not in item.to_wire()


def test_tool_result_ignores_a_non_integer_duration(derive: _Derive) -> None:
    """``bool`` 是 ``int`` 的子类 —— 一个写成 True 的脏值不许变成「1 毫秒」。"""
    for dirty in (True, "842", 8.42, None):
        msg = ToolMessage(
            content="ok",
            tool_call_id="c1",
            name="t",
            additional_kwargs={"duration_ms": dirty},
        )
        (item,) = derive(run_id=RUN, messages=[msg])
        assert "duration_ms" not in item.to_wire(), dirty


def test_plan_item_carries_goal_and_steps(derive: _Derive) -> None:
    items = derive(
        run_id=RUN,
        messages=[],
        plan=AuxFrame(data={"goal": "查天气", "steps": [{"title": "第一步"}, {"title": "第二步"}]}),
    )
    wire = items[0].to_wire()
    assert wire["type"] == "plan"
    assert wire["goal"] == "查天气"
    assert wire["steps"] == [{"title": "第一步"}, {"title": "第二步"}]


def test_approval_item_mirrors_the_frame(derive: _Derive) -> None:
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
    (item,) = derive(run_id=RUN, messages=[], approvals=[AuxFrame(data=frame)])
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


# --- AuxFrame 的时刻透传(三种辅助条目 created_at 的唯一来源)-------------


def test_plan_created_at_comes_from_the_frame(derive: _Derive) -> None:
    items = derive(
        run_id=RUN,
        messages=[],
        plan=AuxFrame(data={"goal": "查天气"}, created_at="2026-08-25T01:00:00+00:00"),
    )
    assert items[0].to_wire()["created_at"] == "2026-08-25T01:00:00+00:00"


def test_error_created_at_comes_from_the_frame(derive: _Derive) -> None:
    items = derive(
        run_id=RUN,
        messages=[],
        error=AuxFrame(data={"message": "炸了"}, created_at="2026-08-25T01:00:09+00:00"),
    )
    assert items[0].to_wire()["created_at"] == "2026-08-25T01:00:09+00:00"


def test_approval_created_at_comes_from_the_frame(derive: _Derive) -> None:
    items = derive(
        run_id=RUN,
        messages=[],
        approvals=[AuxFrame(data={"request_id": "apr_1"}, created_at="2026-08-25T01:00:05+00:00")],
    )
    assert items[0].to_wire()["created_at"] == "2026-08-25T01:00:05+00:00"


def test_approval_created_at_falls_back_to_requested_at(derive: _Derive) -> None:
    """审批是唯一在 payload 里自带时刻的一种,调用方没给落库时刻时别白丢。"""
    items = derive(
        run_id=RUN,
        messages=[],
        approvals=[
            AuxFrame(data={"request_id": "apr_1", "requested_at": "2026-08-15T03:43:14+00:00"})
        ],
    )
    assert items[0].to_wire()["created_at"] == "2026-08-15T03:43:14+00:00"


def test_frame_created_at_wins_over_requested_at(derive: _Derive) -> None:
    """两个都有时以帧的落库时刻为准 —— 三种辅助条目一个口径。"""
    items = derive(
        run_id=RUN,
        messages=[],
        approvals=[
            AuxFrame(
                data={"request_id": "apr_1", "requested_at": "2026-08-15T03:43:14+00:00"},
                created_at="2026-08-15T03:43:15+00:00",
            )
        ],
    )
    wire = items[0].to_wire()
    assert wire["created_at"] == "2026-08-15T03:43:15+00:00"
    # 专有字段仍然是审批自己的那个时刻,没被公共字段顶掉。
    assert wire["requested_at"] == "2026-08-15T03:43:14+00:00"


def test_aux_items_without_a_frame_timestamp_report_null(derive: _Derive) -> None:
    """调用方不带时刻进来就报缺席,绝不拿别的时刻凑一个。"""
    items = derive(
        run_id=RUN,
        messages=[],
        plan=AuxFrame(data={"goal": "g"}),
        approvals=[AuxFrame(data={"request_id": "apr_1"})],
        error=AuxFrame(data={"message": "炸了"}),
    )
    assert [i.to_wire()["created_at"] for i in items] == [None, None, None]


def test_error_item_carries_the_message(derive: _Derive) -> None:
    (item,) = derive(run_id=RUN, messages=[], error=AuxFrame(data={"message": "deadline exceeded"}))
    assert item.to_wire() == {
        "id": f"{RUN}:0",
        "type": "error",
        "run_id": RUN,
        "created_at": None,
        "message": "deadline exceeded",
    }


def test_error_item_carries_the_exception_name(derive: _Derive) -> None:
    """``MaxStepsExceededError`` 是对外文档承诺过语义的取值(撞步数上限,不是
    平台故障),丢了它客户端就分不出这一类失败。"""
    (item,) = derive(
        run_id=RUN,
        messages=[],
        error=AuxFrame(data={"message": "step limit", "name": "MaxStepsExceededError"}),
    )
    assert item.to_wire()["name"] == "MaxStepsExceededError"


# --- tool_calls -----------------------------------------------------------


def test_one_ai_message_with_many_tool_calls_yields_one_item_each(derive: _Derive) -> None:
    msg = AIMessage(
        content="并行查两个城市",
        tool_calls=[
            _tool_call("c1", "weather", {"city": "北京"}),
            _tool_call("c2", "weather", {"city": "上海"}),
            _tool_call("c3", "weather", {"city": "广州"}),
        ],
    )
    items = derive(run_id=RUN, messages=[msg])
    assert _types(items) == ["assistant_message", "tool_call", "tool_call", "tool_call"]
    assert [i.to_wire()["call_id"] for i in items[1:]] == ["c1", "c2", "c3"]
    assert [i.to_wire()["args"] for i in items[1:]] == [
        {"city": "北京"},
        {"city": "上海"},
        {"city": "广州"},
    ]
    # 每个 tool_call 各占一个 id 子序号。
    assert [i.id for i in items] == [f"{RUN}:{n}" for n in range(4)]


def test_tool_call_and_result_pair_on_call_id_not_position(derive: _Derive) -> None:
    """并行工具的结果可能乱序回来,客户端只能靠 call_id 配对。"""
    messages = [
        AIMessage(
            content="",
            tool_calls=[_tool_call("c1", "slow"), _tool_call("c2", "fast")],
        ),
        ToolMessage(content="fast 先回", tool_call_id="c2", name="fast"),
        ToolMessage(content="slow 后回", tool_call_id="c1", name="slow"),
    ]
    items = derive(run_id=RUN, messages=messages)
    calls = {i.to_wire()["call_id"]: i for i in items if i.TYPE == "tool_call"}
    results = {i.to_wire()["call_id"]: i for i in items if i.TYPE == "tool_result"}
    assert set(calls) == set(results) == {"c1", "c2"}
    assert calls["c1"].to_wire()["name"] == "slow"
    assert results["c1"].to_wire()["content"] == "slow 后回"
    assert results["c2"].to_wire()["content"] == "fast 先回"


def test_blank_assistant_text_with_tool_calls_yields_only_tool_calls(derive: _Derive) -> None:
    """空气泡不该渲染,但那一步工具确实发生了。"""
    messages = [
        HumanMessage(content="北京天气"),
        AIMessage(content="", tool_calls=[_tool_call("c1", "weather")]),
        ToolMessage(content="晴", tool_call_id="c1", name="weather"),
        AIMessage(content="   ", tool_calls=[_tool_call("c2", "weather")]),
    ]
    items = derive(run_id=RUN, messages=messages)
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


def test_channel_final_only_for_the_last_bubble_without_tool_calls(derive: _Derive) -> None:
    items = derive(run_id=RUN, messages=_full_run())
    channels = [i.to_wire()["channel"] for i in items if i.TYPE == "assistant_message"]
    assert channels == ["commentary", "final"]


def test_channel_commentary_when_the_last_bubble_still_calls_tools(derive: _Derive) -> None:
    """一轮停在「还要调工具」上(比如撞了 max_steps),那条不是终稿。"""
    messages = [
        HumanMessage(content="北京天气"),
        AIMessage(content="我再查一次", tool_calls=[_tool_call("c1", "weather")]),
    ]
    items = derive(run_id=RUN, messages=messages)
    assert items[1].to_wire()["channel"] == "commentary"


def test_scheduled_delivery_opens_its_own_segment(derive: _Derive) -> None:
    """定时投递不能把用户那一段的 final 抢走。"""
    messages = [
        HumanMessage(content="北京天气"),
        AIMessage(content="北京今天晴"),
        AIMessage(
            content="[定时] 明天有雨", additional_kwargs={"expert_work_scheduled_delivery": True}
        ),
    ]
    items = derive(run_id=RUN, messages=messages)
    channels = [i.to_wire()["channel"] for i in items if i.TYPE == "assistant_message"]
    assert channels == ["final", "final"]


def test_channel_values_stay_inside_the_vocabulary(derive: _Derive) -> None:
    items = derive(run_id=RUN, messages=_full_run())
    channels = [i.to_wire()["channel"] for i in items if i.TYPE == "assistant_message"]
    assert len(channels) == 2  # 先证真有助手轮,否则「全都合法」是空转
    assert set(channels) <= {"final", "commentary"}


# --- 隐藏消息 -------------------------------------------------------------


def test_hidden_scaffolding_never_reaches_the_items(derive: _Derive) -> None:
    """CM-1 的 <recovery-advisory> 之类是编排层写给模型自己看的。"""
    messages = [
        HumanMessage(content="北京天气"),
        HumanMessage(
            content="<recovery-advisory>工具失败了,换个思路",
            additional_kwargs={"expert_work_hide_from_ui": True},
        ),
        AIMessage(content="北京今天晴"),
    ]
    items = derive(run_id=RUN, messages=messages)
    assert _types(items) == ["user_message", "assistant_message"]
    assert all("recovery-advisory" not in str(i.to_wire()) for i in items)


def test_hidden_message_does_not_steal_final_from_the_real_answer(derive: _Derive) -> None:
    """隐藏行排在最后时,前面那条真答案仍然是终稿。"""
    messages = [
        HumanMessage(content="北京天气"),
        AIMessage(content="北京今天晴"),
        HumanMessage(
            content="[Reflection] 再检查一遍",
            additional_kwargs={"expert_work_hide_from_ui": True},
        ),
    ]
    items = derive(run_id=RUN, messages=messages)
    assert _types(items) == ["user_message", "assistant_message"]
    assert items[1].to_wire()["channel"] == "final"


def test_hidden_tool_result_is_excluded_too(derive: _Derive) -> None:
    messages = [
        ToolMessage(
            content="内部结果",
            tool_call_id="c1",
            name="t",
            additional_kwargs={"expert_work_hide_from_ui": True},
        ),
        ToolMessage(content="公开结果", tool_call_id="c2", name="t"),
    ]
    items = derive(run_id=RUN, messages=messages)
    assert [i.to_wire()["content"] for i in items] == ["公开结果"]


# --- 时间戳 ---------------------------------------------------------------


def test_created_at_comes_from_the_write_side_stamp(derive: _Derive) -> None:
    messages = [
        HumanMessage(
            content="北京天气",
            additional_kwargs={
                STAMP_CREATED_AT: "2026-08-25T01:00:00+00:00",
                STAMP_RUN_ID: RUN,
            },
        )
    ]
    (item,) = derive(run_id=RUN, messages=messages)
    assert item.to_wire()["created_at"] == "2026-08-25T01:00:00+00:00"


def test_missing_stamp_degrades_to_null_instead_of_a_made_up_time(derive: _Derive) -> None:
    (item,) = derive(run_id=RUN, messages=[HumanMessage(content="北京天气")])
    assert item.to_wire()["created_at"] is None


def test_corrupt_stamp_degrades_to_null(derive: _Derive) -> None:
    messages = [HumanMessage(content="北京天气", additional_kwargs={STAMP_CREATED_AT: 12345})]
    (item,) = derive(run_id=RUN, messages=messages)
    assert item.to_wire()["created_at"] is None


def test_tool_calls_inherit_their_messages_timestamp(derive: _Derive) -> None:
    messages = [
        AIMessage(
            content="",
            tool_calls=[_tool_call("c1", "t"), _tool_call("c2", "t")],
            additional_kwargs={STAMP_CREATED_AT: "2026-08-25T01:00:00+00:00"},
        )
    ]
    items = derive(run_id=RUN, messages=messages)
    assert [i.to_wire()["created_at"] for i in items] == ["2026-08-25T01:00:00+00:00"] * 2


# --- 防注入包装还原 -------------------------------------------------------


def test_tool_result_content_is_restored_from_the_injection_wrapping(derive: _Derive) -> None:
    """工具结果在内部带 spotlight 包装,直接显示是乱码。"""
    raw = "搜索结果 有效"
    wrapped = spotlight_untrusted(raw, nonce="0ce9b28d1a1e")
    (item,) = derive(
        run_id=RUN, messages=[ToolMessage(content=wrapped, tool_call_id="c1", name="search")]
    )
    content = item.to_wire()["content"]
    assert content == raw
    assert "UNTRUSTED" not in content
    assert "▁" not in content


def test_tool_result_keeps_the_trusted_overflow_footer(derive: _Derive) -> None:
    """溢出脚注是平台自己写的,排在围栏之外,不该被还原一起吃掉。"""
    wrapped = spotlight_untrusted("前 200 字…", nonce="n1")
    footer = "\n\n[full output saved to workspace://out.txt]"
    (item,) = derive(
        run_id=RUN,
        messages=[ToolMessage(content=wrapped + footer, tool_call_id="c1", name="bash")],
    )
    assert item.to_wire()["content"] == "前 200 字…" + footer


def test_unwrapped_tool_result_passes_through_unchanged(derive: _Derive) -> None:
    """关掉 spotlight 防御的 agent,工具结果本来就是裸的。"""
    (item,) = derive(
        run_id=RUN, messages=[ToolMessage(content="裸结果\n第二行", tool_call_id="c1", name="t")]
    )
    assert item.to_wire()["content"] == "裸结果\n第二行"


# --- 顺序 -----------------------------------------------------------------


def test_items_follow_message_order(derive: _Derive) -> None:
    items = derive(run_id=RUN, messages=_full_run())
    assert _types(items) == [
        "user_message",
        "assistant_message",
        "tool_call",
        "tool_result",
        "assistant_message",
    ]


def test_plan_sits_after_the_user_message_and_before_the_first_assistant_output(
    derive: _Derive,
) -> None:
    items = derive(run_id=RUN, messages=_full_run(), plan=AuxFrame(data={"goal": "查天气"}))
    assert _types(items)[:3] == ["user_message", "plan", "assistant_message"]


def test_plan_lands_at_the_end_when_the_run_produced_nothing_else(derive: _Derive) -> None:
    items = derive(
        run_id=RUN,
        messages=[HumanMessage(content="北京天气")],
        plan=AuxFrame(data={"goal": "查天气"}),
    )
    assert _types(items) == ["user_message", "plan"]


def test_error_sits_last_after_everything_the_run_did_produce(derive: _Derive) -> None:
    items = derive(
        run_id=RUN, messages=_full_run(), error=AuxFrame(data={"message": "deadline exceeded"})
    )
    assert _types(items)[-1] == "error"
    assert _types(items)[0] == "user_message"


def test_approvals_sit_before_the_trailing_error(derive: _Derive) -> None:
    items = derive(
        run_id=RUN,
        messages=_full_run(),
        approvals=[
            AuxFrame(data={"request_id": "apr_1", "node": "tools", "reason_kind": "policy_gate"}),
            AuxFrame(data={"request_id": "apr_2", "node": "tools", "reason_kind": "missing_info"}),
        ],
        error=AuxFrame(data={"message": "deadline exceeded"}),
    )
    assert len(items) == 8  # 只剩辅助条目时末三条也对得上,先锁总数
    assert _types(items)[-3:] == ["approval", "approval", "error"]
    assert [i.to_wire()["request_id"] for i in items if i.TYPE == "approval"] == [
        "apr_1",
        "apr_2",
    ]


def test_no_auxiliary_signals_means_no_auxiliary_items(derive: _Derive) -> None:
    items = derive(run_id=RUN, messages=_full_run())
    # 断言完整类型序列而不是三条「不包含」—— 后者在推导整体失灵时同样成立。
    assert _types(items) == [
        "user_message",
        "assistant_message",
        "tool_call",
        "tool_result",
        "assistant_message",
    ]


# --- id -------------------------------------------------------------------


def test_ids_are_the_run_id_plus_a_dense_zero_based_index(derive: _Derive) -> None:
    items = derive(
        run_id=RUN,
        messages=_full_run(),
        plan=AuxFrame(data={"goal": "查天气"}),
        approvals=[AuxFrame(data={"request_id": "apr_1"})],
        error=AuxFrame(data={"message": "炸了"}),
    )
    # 先锁条数:``range(len(items))`` 自己会跟着结果缩水,空列表下恒真。
    assert len(items) == 8
    assert [i.id for i in items] == [f"{RUN}:{n}" for n in range(len(items))]


def test_ids_are_unique_within_one_derivation(derive: _Derive) -> None:
    items = derive(
        run_id=RUN,
        messages=[
            HumanMessage(content="并行"),
            AIMessage(content="", tool_calls=[_tool_call("c1", "t"), _tool_call("c2", "t")]),
            ToolMessage(content="a", tool_call_id="c1", name="t"),
            ToolMessage(content="b", tool_call_id="c2", name="t"),
            AIMessage(content="好了"),
        ],
        plan=AuxFrame(data={"goal": "g"}),
        error=AuxFrame(data={"message": "炸了"}),
    )
    ids = [i.id for i in items]
    assert len(ids) == 8  # 空列表里「id 都不重复」是空转
    assert len(set(ids)) == len(ids)


def test_the_same_input_derives_the_same_ids_twice(derive: _Derive) -> None:
    """「同一查询可重复」是 id 唯一的另一半承诺。"""
    messages = _full_run()
    first = derive(run_id=RUN, messages=messages, plan=AuxFrame(data={"goal": "g"}))
    second = derive(run_id=RUN, messages=messages, plan=AuxFrame(data={"goal": "g"}))
    assert len(first) == 6  # 两次都空也「相等」,先证真推出了东西
    assert [i.id for i in first] == [i.id for i in second]


def test_run_id_is_stamped_on_every_item(derive: _Derive) -> None:
    items = derive(
        run_id=RUN,
        messages=_full_run(),
        plan=AuxFrame(data={"goal": "g"}),
        approvals=[AuxFrame(data={"request_id": "apr_1"})],
        error=AuxFrame(data={"message": "炸了"}),
    )
    assert len(items) == 8  # 只剩辅助条目时这条也成立,所以先锁条数
    assert {i.run_id for i in items} == {RUN}


# --- 两种消息形态同源 -----------------------------------------------------


def test_object_and_dict_forms_derive_identical_items() -> None:
    """同一批消息,对象形态与 ``model_dump()`` 形态必须推出**完全相等**的条目。

    这是三条产出路径同源的最小保证:会话历史读 checkpoint 的对象,实时与
    回放读 ``updates`` 帧里的 dict。两边只要有一个字段读法不同,第三方就会
    看到「实时和刷新后不一样」;而读不到时的失败方式是**静默少内容**,不是
    报错 —— 所以只能靠断言相等来兜。
    """
    messages = [
        *_full_run(),
        HumanMessage(
            content=[
                {"type": "text", "text": "看这张图"},
                {"type": "image_ref", "ref": "expert_work://image/abc"},
            ],
            additional_kwargs={STAMP_CREATED_AT: "2026-08-25T01:00:00+00:00"},
        ),
        HumanMessage(
            content="<recovery-advisory>", additional_kwargs={"expert_work_hide_from_ui": True}
        ),
        AIMessage(
            content="并行查两个",
            tool_calls=[_tool_call("c2", "weather", {"city": "上海"}), _tool_call("c3", "search")],
            additional_kwargs={STAMP_CREATED_AT: "2026-08-25T01:00:01+00:00"},
        ),
        ToolMessage(
            content=spotlight_untrusted("上海有雨", nonce="n1"),
            tool_call_id="c2",
            name="weather",
            status="error",
            artifact={"truncated": True},
            additional_kwargs={"duration_ms": 42},
        ),
        AIMessage(
            content="[定时] 明天有雨",
            additional_kwargs={"expert_work_scheduled_delivery": True},
        ),
    ]
    aux = {
        "plan": AuxFrame(data={"goal": "查天气", "steps": [{"title": "第一步"}]}, created_at="t0"),
        "approvals": [AuxFrame(data={"request_id": "apr_1"}, created_at="t1")],
        "error": AuxFrame(
            data={"message": "炸了", "name": "MaxStepsExceededError"}, created_at="t2"
        ),
    }

    from_objects = derive_run_items(run_id=RUN, messages=messages, **aux)  # type: ignore[arg-type]
    from_dicts = derive_run_items(run_id=RUN, messages=_as_dicts(messages), **aux)  # type: ignore[arg-type]

    # 先证这批消息真的产出了东西 —— 否则 [] == [] 也「相等」。
    assert len(from_objects) > 10
    assert from_objects == from_dicts
