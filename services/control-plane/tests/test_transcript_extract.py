"""extract_turns —— transcript 抽取的纯函数形态(P2 Task 1)。"""

from datetime import UTC, datetime
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from control_plane.transcript import extract_turns
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
