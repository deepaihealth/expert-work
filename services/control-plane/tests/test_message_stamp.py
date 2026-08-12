"""写入侧盖戳 helper(P2 Task 3)。"""

from datetime import UTC, datetime
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage

from expert_work.common.message_stamp import (
    STAMP_CREATED_AT,
    STAMP_RUN_ID,
    stamp_message,
    stamp_messages,
)


def test_stamp_adds_both_keys() -> None:
    now = datetime(2026, 8, 12, 3, 4, 5, tzinfo=UTC)
    rid = str(uuid4())
    out = stamp_message(HumanMessage(content="你好"), run_id=rid, now=now)
    assert out.additional_kwargs[STAMP_CREATED_AT] == now.isoformat()
    assert out.additional_kwargs[STAMP_RUN_ID] == rid


def test_stamp_does_not_mutate_original() -> None:
    original = HumanMessage(content="你好")
    stamp_message(original, run_id="r", now=datetime.now(UTC))
    assert STAMP_CREATED_AT not in original.additional_kwargs


def test_stamp_preserves_existing_kwargs() -> None:
    original = AIMessage(content="答案", additional_kwargs={"expert_work_hide_from_ui": True})
    out = stamp_message(original, run_id="r", now=datetime.now(UTC))
    assert out.additional_kwargs["expert_work_hide_from_ui"] is True
    assert out.additional_kwargs[STAMP_RUN_ID] == "r"


def test_stamp_messages_stamps_all() -> None:
    now = datetime.now(UTC)
    out = stamp_messages([HumanMessage(content="a"), AIMessage(content="b")], run_id="r", now=now)
    assert all(m.additional_kwargs[STAMP_RUN_ID] == "r" for m in out)


def test_build_run_graph_input_stamps_human_only() -> None:
    from control_plane.api.runs import build_run_graph_input

    class _Built:
        supports_vision = False
        spotlight_nonce = None
        max_steps = 10
        max_no_progress = 3
        system_prompt = "sys"

    rid = uuid4()
    gi = build_run_graph_input(
        _Built(), input_text="你好", image_refs=[], untrusted_content=None, run_id=rid
    )
    system, human = gi["messages"]
    assert STAMP_RUN_ID not in system.additional_kwargs
    assert human.additional_kwargs[STAMP_RUN_ID] == str(rid)
