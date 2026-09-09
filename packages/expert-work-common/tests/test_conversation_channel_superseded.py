"""P-1 —— ``visible_turns`` 对被取代轮 / 墓碑的投影。"""

from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.messages import AIMessage, HumanMessage

from expert_work.common.conversation_channel import visible_turns
from expert_work.common.supersede import mark_superseded, tombstone_message

NOW = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)


def _thread() -> list[object]:
    old = [
        mark_superseded(m, new_run_id="r3", now=NOW)
        for m in (HumanMessage(content="U2"), AIMessage(content="A2"))
    ]
    return [
        HumanMessage(content="U1"),
        AIMessage(content="A1"),
        *old,
        HumanMessage(content="U3"),
        AIMessage(content="A3"),
    ]


def test_default_keeps_superseded_turns_with_marker() -> None:
    turns = visible_turns(_thread())
    assert [(t.seq, t.role, t.superseded_by) for t in turns] == [
        (0, "user", None),
        (1, "assistant", None),
        (2, "user", "r3"),
        (3, "assistant", "r3"),
        (4, "user", None),
        (5, "assistant", None),
    ]
    assert [t.channel for t in turns if t.role == "assistant"] == ["final", "final", "final"]


def test_include_superseded_false_drops_them_without_moving_seq() -> None:
    turns = visible_turns(_thread(), include_superseded=False)
    assert [t.seq for t in turns] == [0, 1, 4, 5]


def test_tombstone_is_emitted_empty_and_does_not_steal_final() -> None:
    msgs = _thread()
    msgs[2], msgs[3] = tombstone_message(msgs[2]), tombstone_message(msgs[3])  # type: ignore[arg-type]
    turns = visible_turns(msgs)
    stones = [t for t in turns if t.tombstone]
    assert [(t.seq, t.role, t.text, t.channel, t.superseded_by) for t in stones] == [
        (2, "user", "", None, "r3"),
        (3, "assistant", "", None, "r3"),
    ]
    # A1 仍是自己段落的 final(墓碑不算「后面还有一行」)
    assert next(t for t in turns if t.seq == 1).channel == "final"
    assert [t.seq for t in turns] == [0, 1, 2, 3, 4, 5]
