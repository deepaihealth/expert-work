"""extract_turns —— transcript 抽取的纯函数形态(P2 Task 1)。

``channel`` 的判定本体已挪到
:func:`expert_work.common.conversation_channel.visible_turns`,好让对话条目
(``conversation_derive``)与本函数共用同一份实现。本文件因此还负责两件事:
钉住搬家前后 ``extract_turns`` 的行为一字不变,以及钉住「整条 thread 上算
channel」与「单轮上算 channel」到底等不等价。
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from control_plane.transcript import extract_turns
from expert_work.common.conversation_derive import derive_run_items
from expert_work.common.message_stamp import STAMP_CREATED_AT, STAMP_RUN_ID


def test_extract_turns_keeps_only_human_and_ai_text() -> None:
    raw = [
        SystemMessage(content="你是助手"),
        HumanMessage(content="你好"),
        AIMessage(content="", tool_calls=[{"id": "1", "name": "t", "args": {}}]),
        ToolMessage(content="结果", tool_call_id="1"),
        AIMessage(content="答案"),
    ]
    turns = extract_turns(raw, include_hidden=False)
    assert [(t.seq, t.role, t.content) for t in turns] == [
        (1, "user", "你好"),
        (4, "assistant", "答案"),
    ]
    assert turns[1].channel == "final"


def test_extract_turns_hidden_filter() -> None:
    raw = [
        HumanMessage(content="你好"),
        HumanMessage(
            content="<recovery-advisory>", additional_kwargs={"expert_work_hide_from_ui": True}
        ),
        AIMessage(content="答案"),
    ]
    assert len(extract_turns(raw, include_hidden=True)) == 3
    assert len(extract_turns(raw, include_hidden=False)) == 2


def test_extract_turns_reads_stamps() -> None:
    now = datetime(2026, 8, 12, 1, 2, 3, tzinfo=UTC)
    rid = uuid4()
    raw = [
        HumanMessage(
            content="你好",
            additional_kwargs={STAMP_CREATED_AT: now.isoformat(), STAMP_RUN_ID: str(rid)},
        ),
        AIMessage(content="没戳的老消息"),
    ]
    turns = extract_turns(raw, include_hidden=False)
    assert turns[0].created_at == now
    assert turns[0].run_id == rid
    assert turns[1].created_at is None
    assert turns[1].run_id is None


def test_extract_turns_tolerates_corrupt_stamp() -> None:
    """坏戳退化成 None,绝不让一条脏消息炸掉整个会话的读取。"""
    raw = [
        HumanMessage(
            content="你好",
            additional_kwargs={STAMP_CREATED_AT: "不是时间", STAMP_RUN_ID: "不是uuid"},
        )
    ]
    turn = extract_turns(raw, include_hidden=False)[0]
    assert turn.created_at is None
    assert turn.run_id is None


# --- channel 判定(搬去 common 之后的回归网)-------------------------------


def _channels(raw: list[Any], *, include_hidden: bool = False) -> list[str | None]:
    return [
        t.channel
        for t in extract_turns(raw, include_hidden=include_hidden)
        if t.role == "assistant"
    ]


def _tool_call(call_id: str, name: str) -> dict[str, Any]:
    return {"id": call_id, "name": name, "args": {}, "type": "tool_call"}


def test_extract_turns_channel_commentary_for_a_turn_with_tool_calls() -> None:
    raw = [
        HumanMessage(content="北京天气"),
        AIMessage(content="我查一下", tool_calls=[_tool_call("c1", "weather")]),
        ToolMessage(content="晴", tool_call_id="c1", name="weather"),
        AIMessage(content="北京今天晴"),
    ]
    assert _channels(raw) == ["commentary", "final"]


def test_extract_turns_channel_commentary_for_a_turn_that_is_not_last_in_segment() -> None:
    raw = [
        HumanMessage(content="北京天气"),
        AIMessage(content="先说一句"),
        AIMessage(content="北京今天晴"),
    ]
    assert _channels(raw) == ["commentary", "final"]


def test_extract_turns_channel_scheduled_delivery_opens_its_own_segment() -> None:
    """定时投递自己开一段,不能把用户那一段的 final 抢走。"""
    raw = [
        HumanMessage(content="北京天气"),
        AIMessage(content="北京今天晴"),
        AIMessage(
            content="[定时] 明天有雨",
            additional_kwargs={"expert_work_scheduled_delivery": True},
        ),
    ]
    assert _channels(raw) == ["final", "final"]


def test_extract_turns_channel_hidden_row_after_the_answer() -> None:
    """隐藏行不开段落,但它在忠实视图里仍然是「后面还有一行」——
    所以同一条助手轮在两种视图下的 channel 确实会不同。这是既有行为,钉住
    它,免得日后有人把某一侧当 bug 顺手改掉。"""
    raw = [
        HumanMessage(content="北京天气"),
        AIMessage(content="北京今天晴"),
        HumanMessage(
            content="[Reflection] 再检查一遍",
            additional_kwargs={"expert_work_hide_from_ui": True},
        ),
    ]
    assert _channels(raw, include_hidden=True) == ["commentary"]
    assert _channels(raw, include_hidden=False) == ["final"]


# --- 单轮 vs 整 thread ----------------------------------------------------


def _run_one() -> list[Any]:
    return [
        HumanMessage(content="北京天气"),
        AIMessage(content="我查一下", tool_calls=[_tool_call("c1", "weather")]),
        ToolMessage(content="晴", tool_call_id="c1", name="weather"),
        AIMessage(content="北京今天晴"),
    ]


def _assistant_channels(items: list[Any]) -> list[str]:
    return [i.to_wire()["channel"] for i in items if i.TYPE == "assistant_message"]


def test_single_run_channel_matches_thread_channel_when_the_next_run_opens_a_segment() -> None:
    """常态:下一轮由用户消息(或定时投递)开头,两种算法给出同一个答案。

    平台今天所有的开轮写入点都满足这个前提 —— 用户发起(runs.py
    ``build_run_graph_input``)与触发器发起(trigger_firing)都写一条非隐藏
    HumanMessage,定时投递(trigger_delivery)写一条带
    ``expert_work_scheduled_delivery`` 的 AIMessage。
    """
    run2 = [HumanMessage(content="上海呢"), AIMessage(content="上海有雨")]
    thread = [*_run_one(), *run2]

    per_run = _assistant_channels(derive_run_items(run_id="r1", messages=_run_one()))
    per_run += _assistant_channels(derive_run_items(run_id="r2", messages=run2))

    assert _channels(thread) == per_run == ["commentary", "final", "final"]


def test_single_run_channel_diverges_when_the_next_run_opens_no_segment() -> None:
    """两者**不等价**:单轮算法把「这一轮结束」当段落结束,整 thread 算法只
    认下一条自己开段的消息。

    审批续跑(``runs.py`` 的 continuation run,``graph_input=None``)就是一轮
    不写新用户消息的开头。今天这条路径上停在审批前的那条助手消息必然带
    ``tool_calls``(带了才会被拦),所以两种算法都判 commentary,分歧还观察
    不到;但规则本身是不同的,别把「现在看不出差别」当成「等价」。
    """
    run1 = [HumanMessage(content="发这封邮件"), AIMessage(content="好的,我来发")]
    run2 = [AIMessage(content="发完了")]

    assert _channels([*run1, *run2]) == ["commentary", "final"]
    assert _assistant_channels(derive_run_items(run_id="r1", messages=run1)) == ["final"]
    assert _assistant_channels(derive_run_items(run_id="r2", messages=run2)) == ["final"]


def test_items_channel_follows_the_ui_view_not_the_faithful_view() -> None:
    """条目是对外的产品表示,所以隐藏脚手架一律不算「后面还有一行」——
    与 ``include_hidden=False`` 一侧对齐。"""
    run = [
        HumanMessage(content="北京天气"),
        AIMessage(content="北京今天晴"),
        HumanMessage(
            content="[Reflection] 再检查一遍",
            additional_kwargs={"expert_work_hide_from_ui": True},
        ),
    ]
    assert _assistant_channels(derive_run_items(run_id="r1", messages=run)) == _channels(
        run, include_hidden=False
    )
