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


# --------------------------------------------------------------------------- B-300 PR S
# 后台写回在「同一对话没有更早任务」时只提炼开轮消息 ``expert_work_created_at`` 在 30 天内的轮次
# (重做设计 §8.1;没有这个戳的算更早的)。所以每一条往对话里放用户开轮消息、又会落写回任务的路径
# 都必须盖它。会落任务的入口全走下面这几个构造函数(``spawn_run`` —— 控制台 / 调试台 / 对外 API v1
# 的新一轮与 ``:edit`` —— 经 ``build_run_graph_input``;``:regenerate`` 经 ``replay_graph_input``;
# 排队 worker 经 ``graph_input_from_enqueued``);触发器在 ``test_trigger_firing`` 里钉。


class _Built:
    supports_vision = False
    spotlight_nonce = None
    max_steps = 10
    max_no_progress = 3
    system_prompt = "sys"


def _opener(graph_input: dict[str, object]) -> HumanMessage:
    messages = graph_input["messages"]
    assert isinstance(messages, list)
    [human] = [m for m in messages if isinstance(m, HumanMessage)][:1]
    return human


def test_a_new_turn_opener_carries_the_created_at_stamp() -> None:
    from control_plane.api.runs import build_run_graph_input

    gi = build_run_graph_input(
        _Built(), input_text="你好", image_refs=[], untrusted_content=None, run_id=uuid4()
    )
    assert STAMP_CREATED_AT in _opener(gi).additional_kwargs


def test_a_regenerated_turn_opener_is_stamped_again() -> None:
    from langchain_core.messages import SystemMessage

    from control_plane.api.runs import replay_graph_input

    old = HumanMessage(content="你好", id="u-old")  # 盖戳之前的老消息也一样
    gi = replay_graph_input(_Built(), [SystemMessage(content="sys"), old], run_id=uuid4())
    assert STAMP_CREATED_AT in _opener(gi).additional_kwargs


def test_a_queued_turn_opener_is_stamped() -> None:
    from langchain_core.messages import SystemMessage, messages_to_dict

    from control_plane.api._enqueued_input import graph_input_from_enqueued

    fresh = graph_input_from_enqueued(_Built(), {"input": "你好"}, uuid4())
    replay = graph_input_from_enqueued(
        _Built(),
        {"replay_messages": messages_to_dict([SystemMessage(content="sys"), HumanMessage("你好")])},
        uuid4(),
    )
    for gi in (fresh, replay):
        assert STAMP_CREATED_AT in _opener(gi).additional_kwargs


def test_an_api_turn_counts_as_recent_for_the_first_writeback_job() -> None:
    """交叉核对:``spawn_run`` 建的一轮,在「没有更早任务」的 30 天规则下是新段(不会因为没戳被
    当成更早的而什么都不提炼)。"""
    from datetime import timedelta

    from control_plane.api.runs import build_run_graph_input
    from orchestrator.graph_builder.memory import _EXTRACT_INPUT_CHAR_CAP, _extraction_inputs
    from orchestrator.graph_builder.memory_slice import SegmentStart, plan_slice

    gi = build_run_graph_input(
        _Built(), input_text="我住在成都", image_refs=[], untrusted_content=None, run_id=uuid4()
    )
    messages = [*gi["messages"], AIMessage(content="好的")]  # type: ignore[misc]
    since = datetime.now(UTC) - timedelta(days=30)
    plan = plan_slice(
        _extraction_inputs(messages, ()),
        SegmentStart(opened_since=since),
        cap=_EXTRACT_INPUT_CHAR_CAP,
        halves=False,
    )
    [chunk] = plan.chunks
    assert [line.text for line in chunk.user] == ["我住在成都"]
