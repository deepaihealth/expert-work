"""B-300 ① —— 「用户在对话里输入的文字」的白名单标记(设计 §4.1)。"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from expert_work.common.user_input import (
    USER_INPUT_KEY,
    is_draft_turn,
    mark_draft_turn,
    read_user_input,
    user_input_kwargs,
)


def _marked(
    content: str | list[str | dict[Any, Any]],
    *,
    text: str,
    images: int,
    files: int,
    external_blocks: int,
) -> HumanMessage:
    return HumanMessage(
        content=content,
        additional_kwargs=user_input_kwargs(
            text=text, images=images, files=files, external_blocks=external_blocks
        ),
    )


def test_reads_the_typed_text_and_counts_from_plain_content() -> None:
    msg = _marked(
        "我住在上海\n\n[file attached: uploads/a.pdf]\n\n<<untrusted>>",
        text="我住在上海",
        images=0,
        files=1,
        external_blocks=1,
    )
    got = read_user_input(msg)
    assert got is not None
    assert got.text == "我住在上海"
    assert (got.images, got.files, got.external_blocks) == (0, 1, 1)


def test_reads_the_typed_text_from_content_blocks() -> None:
    msg = _marked(
        [
            {"type": "text", "text": "看看这张图"},
            {"type": "image_ref", "ref": "expert_work://x"},
            {"type": "text", "text": "\n\n[file attached: uploads/a.pdf]"},
        ],
        text="看看这张图",
        images=1,
        files=1,
        external_blocks=0,
    )
    got = read_user_input(msg)
    assert got is not None
    assert got.text == "看看这张图"
    assert got.images == 1


def test_unmarked_messages_are_not_user_input() -> None:
    assert read_user_input(HumanMessage(content="seed written by a scheduled task")) is None
    assert read_user_input(AIMessage(content="hello")) is None


def test_a_mark_that_does_not_fit_the_content_is_ignored() -> None:
    # 标的长度比正文还长、或不是整数:当作没有标记,不进第一次提炼。
    too_long = HumanMessage(
        content="short",
        additional_kwargs={USER_INPUT_KEY: {"text_chars": 99, "images": 0}},
    )
    assert read_user_input(too_long) is None
    garbage = HumanMessage(content="short", additional_kwargs={USER_INPUT_KEY: "5"})
    assert read_user_input(garbage) is None
    negative = HumanMessage(content="short", additional_kwargs={USER_INPUT_KEY: {"text_chars": -1}})
    assert read_user_input(negative) is None
    as_bool = HumanMessage(
        content="short", additional_kwargs={USER_INPUT_KEY: {"text_chars": True}}
    )
    assert read_user_input(as_bool) is None


def test_mark_survives_a_dict_shaped_message() -> None:
    # 回放帧里消息是 model_dump() 的 dict 形态。
    msg = _marked("hi there", text="hi", images=0, files=0, external_blocks=0)
    got = read_user_input(msg.model_dump())
    assert got is not None
    assert got.text == "hi"


def test_draft_turn_mark_round_trips_and_keeps_other_kwargs() -> None:
    msg = _marked("hi", text="hi", images=0, files=0, external_blocks=0)
    marked = mark_draft_turn(msg)
    assert is_draft_turn(marked)
    assert not is_draft_turn(msg)  # 原对象不动
    assert read_user_input(marked) is not None  # 白名单标记还在
    assert is_draft_turn(marked.model_dump())
    assert not is_draft_turn(AIMessage(content="x"))


def test_sent_at_rides_on_the_mark() -> None:
    """审查 MEDIUM-1(11-14):第一次提炼每轮重读整段对话,相对日期要按那条消息自己的发送时间换算 ——
    构造点在标记里记下发送时刻(UTC,ISO 8601)。"""
    mark = user_input_kwargs(
        text="hi", images=0, files=0, external_blocks=0, sent_at="2026-11-14T01:30:00+00:00"
    )
    msg = HumanMessage(content="hi", additional_kwargs=mark)
    got = read_user_input(msg)
    assert got is not None
    assert got.sent_at == "2026-11-14T01:30:00+00:00"
    # 没有(发版前的消息)或不是字符串:None。
    old = HumanMessage(
        content="hi",
        additional_kwargs=user_input_kwargs(text="hi", images=0, files=0, external_blocks=0),
    )
    assert read_user_input(old).sent_at is None  # type: ignore[union-attr]
