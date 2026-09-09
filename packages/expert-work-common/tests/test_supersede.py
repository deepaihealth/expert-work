"""P-1 —— 标记 / 分组 / 整轮过滤(common 唯一实现)。"""

from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from expert_work.common.conversation_channel import (
    SUPERSEDED_AT,
    SUPERSEDED_BY,
    TOMBSTONE,
    is_superseded,
    is_tombstone,
    superseded_by,
)
from expert_work.common.message_stamp import STAMP_RUN_ID
from expert_work.common.supersede import (
    filter_superseded_turns,
    group_messages_by_run,
    mark_superseded,
    stamped_run_id,
    tombstone_message,
)

NOW = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)


def _turn(run_id: str, *, text: str, marked: bool = False) -> list[object]:
    """一轮:System(无戳)+ Human(戳)+ AI(tool_calls,戳)+ Tool(无戳)+ AI(final,戳)。"""
    msgs: list[object] = [
        SystemMessage(content="sys"),
        HumanMessage(content=f"U-{text}", additional_kwargs={STAMP_RUN_ID: run_id}),
        AIMessage(
            content="",
            tool_calls=[{"name": "t", "args": {}, "id": f"c-{run_id}", "type": "tool_call"}],
            additional_kwargs={STAMP_RUN_ID: run_id},
        ),
        ToolMessage(content="ok", tool_call_id=f"c-{run_id}"),
        AIMessage(content=f"A-{text}", additional_kwargs={STAMP_RUN_ID: run_id}),
    ]
    if marked:
        msgs = [mark_superseded(m, new_run_id="r-new", now=NOW) for m in msgs]  # type: ignore[arg-type]
    return msgs


def test_mark_superseded_keeps_id_and_existing_kwargs() -> None:
    msg = HumanMessage(content="x", id="id-1", additional_kwargs={STAMP_RUN_ID: "r1"})
    out = mark_superseded(msg, new_run_id="r2", now=NOW)
    assert out.id == "id-1"
    assert out.additional_kwargs[STAMP_RUN_ID] == "r1"
    assert out.additional_kwargs[SUPERSEDED_BY] == "r2"
    assert out.additional_kwargs[SUPERSEDED_AT] == NOW.isoformat()
    assert superseded_by(out) == "r2"
    assert is_superseded(out)
    assert msg.additional_kwargs.get(SUPERSEDED_BY) is None  # 原对象不动


def test_tombstone_clears_content_and_tool_calls_but_keeps_id_and_marks() -> None:
    ai = mark_superseded(
        AIMessage(
            content="A",
            id="id-9",
            tool_calls=[{"name": "t", "args": {}, "id": "c", "type": "tool_call"}],
        ),
        new_run_id="r2",
        now=NOW,
    )
    stone = tombstone_message(ai)
    assert stone.id == "id-9"
    assert stone.content == ""
    assert stone.tool_calls == []  # type: ignore[attr-defined]
    assert stone.additional_kwargs[TOMBSTONE] is True
    assert superseded_by(stone) == "r2"
    assert is_tombstone(stone)


def test_group_messages_by_run_attaches_tool_results_to_previous_stamped_run() -> None:
    msgs = [*_turn("r1", text="one"), *_turn("r2", text="two")]
    grouped = group_messages_by_run(msgs)
    assert set(grouped) == {"r1", "r2"}
    assert [type(m).__name__ for m in grouped["r2"]] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
    ]
    assert stamped_run_id(msgs[3]) is None  # ToolMessage 无戳


def test_filter_drops_marked_turn_entirely_and_keeps_others() -> None:
    msgs = [*_turn("r1", text="one"), *_turn("r2", text="two", marked=True)]
    kept = filter_superseded_turns(msgs)
    assert kept == msgs[:5]


def test_filter_drops_unmarked_tool_result_when_its_run_is_marked() -> None:
    """同进同出:哪怕某条 ToolMessage 漏了标记,它所在轮的其它消息带标记就整轮剔除。"""
    turn = _turn("r2", text="two")
    marked = [
        mark_superseded(m, new_run_id="r-new", now=NOW) if i != 3 else m  # type: ignore[arg-type]
        for i, m in enumerate(turn)
    ]
    kept = filter_superseded_turns([*_turn("r1", text="one"), *marked])
    assert not any(isinstance(m, ToolMessage) and m.tool_call_id == "c-r2" for m in kept)
    assert len(kept) == 5


def test_filter_drops_tombstones() -> None:
    stones = [tombstone_message(m) for m in _turn("r2", text="two", marked=True)]  # type: ignore[arg-type]
    assert filter_superseded_turns([*_turn("r1", text="one"), *stones]) == _turn("r1", text="one")
