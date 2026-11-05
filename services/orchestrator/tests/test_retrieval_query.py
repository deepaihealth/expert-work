"""The retrieval query of a turn's automatic look-ups (memory recall, KB probe).

A follow-up that refers back ("this one", "his blood sugar", "the second item
above", "that day", "and the price?") names nothing a retriever can match; the
referent sits in the previous user message or, often, only in the assistant's
previous answer. The query builder carries both along -- bounded -- when the
message is short or holds a reference / ellipsis marker, and leaves a long
self-contained message alone.
"""

from __future__ import annotations

import logging

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from expert_work.common.conversation_channel import HIDE_FROM_UI, SUPERSEDED_BY
from expert_work.common.spotlight import spotlight_untrusted
from orchestrator.graph_builder.retrieval_query import (
    ASSISTANT_HEAD_CHARS,
    ASSISTANT_TAIL_CHARS,
    FOLLOW_UP_MAX_CHARS,
    HISTORY_MESSAGE_HEAD_CHARS,
    HISTORY_MESSAGE_TAIL_CHARS,
    HISTORY_TURNS,
    REFERENCE_MAX_CHARS,
    USER_CONTEXT_CHARS,
    build_retrieval_query,
    log_retrieval_query,
    recent_history,
)


def _turns(*texts: str) -> list[BaseMessage]:
    """Alternating user / assistant messages, starting with the user."""
    return [
        HumanMessage(content=t) if i % 2 == 0 else AIMessage(content=t) for i, t in enumerate(texts)
    ]


# --------------------------------------------------------------------------- follow-ups

_FOLLOW_UPS = [
    # (previous user message, previous answer, follow-up, referent named only by the answer)
    ("推荐一款适合早餐的食品", "可以试试燕麦片甲, 低糖高纤维", "这个保质期多久", "燕麦片甲"),
    ("帮我看看这周的血压记录", "张三本周血压平均 128/82", "他的血糖呢", "张三"),
    (
        "帮我读一下刚上传的合同",
        "已读取 合同草稿v2.docx, 共 12 条条款",
        "上面那份文件里违约金是怎么约定的, 帮我详细解释一下",
        "合同草稿v2.docx",
    ),
    ("控制血压有哪些办法", "1. 低盐饮食\n2. 规律运动\n3. 定期复查", "第二条具体怎么做", "规律运动"),
    ("我上次复诊是哪天", "您上次复诊是 2026-10-12", "那天医生开了什么药", "2026-10-12"),
    ("最便宜的套餐多少钱", "甲套餐 299 元", "另一个呢", "甲套餐"),
    ("推荐两款血压计", "可以考虑 型号A100 和 型号B200", "这两个哪个更适合老人用", "型号B200"),
    ("Which laptop is lighter?", "The Zeta Z2 weighs 1.1 kg.", "what about its battery?", "Z2"),
    ("Show me two plans", "Plan Alpha costs 10; Plan Beta 20.", "this one", "Alpha"),
    (
        "List three vendors for me",
        "1) Northwind 2) Contoso 3) Fabrikam",
        "Tell me more about the second one, please",
        "Contoso",
    ),
    (
        "Who signed the lease?",
        "It was signed by Maria Lopez on 3 May.",
        "Did she also sign the renewal?",
        "Maria Lopez",
    ),
]


@pytest.mark.parametrize(
    ("asked", "answer", "follow_up", "referent"),
    _FOLLOW_UPS,
    ids=[
        "zh-entity",
        "zh-person",
        "zh-file",
        "zh-answer-item",
        "zh-time",
        "zh-ellipsis",
        "zh-plural",
        "en-pronoun",
        "en-this-one",
        "en-answer-item",
        "en-person",
    ],
)
def test_a_follow_up_carries_the_referent_the_assistant_named(
    asked: str, answer: str, follow_up: str, referent: str
) -> None:
    assert referent not in asked and referent not in follow_up
    query = build_retrieval_query(_turns(asked, answer, follow_up))
    assert query is not None
    assert referent in query.text
    assert query.augmented
    assert query.current == follow_up
    # The user's own words stay, last; the previous user message stays too.
    assert query.text.endswith(follow_up)
    assert asked in query.text


def test_a_short_follow_up_triggers_on_length_alone() -> None:
    query = build_retrieval_query(_turns("甲套餐多少钱", "299 元", "乙套餐价格"))
    assert query is not None
    assert query.trigger == "short"
    assert query.text == "甲套餐多少钱\n299 元\n乙套餐价格"


def test_a_marker_in_a_longer_message_triggers() -> None:
    follow_up = "上面那份文件里违约金是怎么约定的, 帮我详细解释一下"
    assert len(follow_up) > FOLLOW_UP_MAX_CHARS
    query = build_retrieval_query(_turns("读一下合同", "已读取", follow_up))
    assert query is not None
    assert query.trigger == "reference"


# --------------------------------------------------------------------------- left alone


@pytest.mark.parametrize(
    "message",
    [
        "请帮我制定一份适合二型糖尿病患者的一周早餐食谱, 每天热量控制在四百千卡左右",
        "Please draft a one-week breakfast plan for a type 2 diabetic, about 400 kcal a day",
    ],
    ids=["zh", "en"],
)
def test_a_long_self_contained_message_is_unchanged(message: str) -> None:
    query = build_retrieval_query(_turns("推荐一款适合早餐的食品", "燕麦片甲", message))
    assert query is not None
    assert query.text == message
    assert not query.augmented
    assert query.trigger == "none"


def test_a_very_long_message_is_unchanged_even_with_a_marker() -> None:
    """A paste or a detailed request names its own subject; carrying the last
    turn along would only drown it."""
    message = "这份材料请帮我总结一下要点\u3002" + "背景说明" * REFERENCE_MAX_CHARS
    query = build_retrieval_query(_turns("上一个问题", "上一个回答", message))
    assert query is not None
    assert query.text == message
    assert not query.augmented


@pytest.mark.parametrize(
    "message",
    [
        # Words that are not references: which / other / before / should / this week.
        "有哪些适合糖尿病人吃的低糖水果, 请帮我列举一下并说明理由",
        "哪种运动方式最适合五十岁以上的高血压患者长期坚持",
        "之前体检血糖偏高现在应该注意什么饮食问题呢",
        "其他还有什么运动适合高血压人群呢请详细说说",
        "这周开始我想每天快走半小时, 有什么需要注意的",
        "血糖高的人早餐应该怎么吃才比较合理呢请给建议",
        "What is the recommended daily salt intake for adults with hypertension?",
        "Is it safe to take ibuprofen together with blood pressure medication?",
    ],
)
def test_a_standalone_question_is_not_taken_for_a_follow_up(message: str) -> None:
    assert len(message) > FOLLOW_UP_MAX_CHARS
    query = build_retrieval_query(_turns("推荐一款早餐食品", "燕麦片甲", message))
    assert query is not None
    assert query.trigger == "none"
    assert query.text == message


@pytest.mark.parametrize(
    "message",
    [
        "那我们换个话题\uff0c最近睡眠不好怎么办",
        "另外我想问一下体检要空腹吗",
        "换个话题",
        "By the way, how much water should I drink?",
        "Different question: is coffee bad for sleep?",
    ],
)
def test_a_topic_switch_carries_nothing(message: str) -> None:
    query = build_retrieval_query(_turns("推荐一款早餐食品", "燕麦片甲", message))
    assert query is not None
    assert not query.augmented
    assert query.text == message


@pytest.mark.parametrize("message", ["好的", "谢谢!", "继续", "OK", "thanks"])
def test_small_talk_carries_nothing(message: str) -> None:
    query = build_retrieval_query(_turns("推荐一款早餐食品", "燕麦片甲", message))
    assert query is not None
    assert not query.augmented


@pytest.mark.parametrize("message", ["A20261105-8841", "订单 202611058841", "QXA-N7"])
def test_an_identifier_alone_carries_nothing(message: str) -> None:
    """An id names its own subject; the previous turn would only dilute it."""
    query = build_retrieval_query(_turns("推荐一款早餐食品", "燕麦片甲", message))
    assert query is not None
    assert not query.augmented
    assert query.text == message


def test_the_keyword_side_puts_the_current_message_first() -> None:
    query = build_retrieval_query(_turns("张三的血压", "张三血压正常", "他的血糖呢"))
    assert query is not None
    assert query.context == "张三的血压\n张三血压正常"
    assert query.text == "张三的血压\n张三血压正常\n他的血糖呢"
    assert query.keyword_text == "他的血糖呢\n张三的血压\n张三血压正常"
    plain = build_retrieval_query(
        _turns("x", "y", "请帮我制定一份适合二型糖尿病患者的一周早餐食谱")
    )
    assert plain is not None
    assert plain.keyword_text == plain.text == plain.current


def test_the_first_message_has_nothing_to_carry() -> None:
    query = build_retrieval_query([SystemMessage(content="sys"), HumanMessage(content="这个呢")])
    assert query is not None
    assert query.text == "这个呢"
    assert query.trigger == "short"
    assert not query.augmented


def test_no_user_message_no_query() -> None:
    assert build_retrieval_query([SystemMessage(content="sys")]) is None
    assert build_retrieval_query([HumanMessage(content="   ")]) is None


# --------------------------------------------------------------------------- what counts as context


def test_the_answer_is_the_last_text_answer_not_a_tool_call() -> None:
    messages: list[BaseMessage] = [
        HumanMessage(content="推荐一款早餐食品"),
        AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "1"}]),
        AIMessage(content="推荐燕麦片甲"),
        HumanMessage(content="这个多少钱"),
    ]
    query = build_retrieval_query(messages)
    assert query is not None
    assert query.text == "推荐一款早餐食品\n推荐燕麦片甲\n这个多少钱"


def test_hidden_superseded_and_placeholder_messages_are_skipped() -> None:
    messages: list[BaseMessage] = [
        HumanMessage(content="张三的血压"),
        AIMessage(content="张三血压正常"),
        # the latest answer, but replaced by a later run
        AIMessage(content="旧回答 李四", additional_kwargs={SUPERSEDED_BY: "run-2"}),
        HumanMessage(content="他的血糖呢"),
        HumanMessage(content="[本轮输入] x", additional_kwargs={HIDE_FROM_UI: True}),
    ]
    query = build_retrieval_query(messages)
    assert query is not None
    assert query.text == "张三的血压\n张三血压正常\n他的血糖呢"
    placeholder = build_retrieval_query(_turns("张三的血压", "…", "他的血糖呢"))
    assert placeholder is not None
    assert placeholder.text == "张三的血压\n他的血糖呢"


def test_the_spotlight_fence_is_stripped_from_user_text() -> None:
    wrapped = spotlight_untrusted("张三的血压", nonce="abc123def456")
    query = build_retrieval_query(_turns(wrapped, "正常", "他的血糖呢"))
    assert query is not None
    assert query.text == "张三的血压\n正常\n他的血糖呢"


def test_context_is_bounded() -> None:
    asked = "问" * 1000 + "末尾问题"
    answer = "开头实体" + "中" * 5000 + "结尾追问"
    query = build_retrieval_query(_turns(asked, answer, "这个呢"))
    assert query is not None
    assert "末尾问题" in query.text  # user: its end (the question)
    assert "开头实体" in query.text  # assistant: its head (the subject) ...
    assert "结尾追问" in query.text  # ... and its tail (a closing question)
    assert (
        query.context_chars <= USER_CONTEXT_CHARS + ASSISTANT_HEAD_CHARS + ASSISTANT_TAIL_CHARS + 4
    )
    assert len(query.text) <= query.context_chars + len("这个呢") + 2


# --------------------------------------------------------------------------- rewrite history


def test_recent_history_has_both_sides_bounded_and_excludes_the_current_message() -> None:
    texts: list[str] = []
    for turn in range(HISTORY_TURNS + 2):
        texts += [f"用户第{turn}轮", f"助手第{turn}轮"]
    long_answer = "实体名" + "x" * 5000 + "结尾"
    messages = [*_turns(*texts), HumanMessage(content="u-last"), AIMessage(content=long_answer)]
    messages.append(HumanMessage(content="这个呢"))
    history = recent_history(messages)
    assert "[user] u-last" in history
    assert "[assistant] 实体名" in history
    assert "结尾" in history
    assert "这个呢" not in history
    assert "用户第0轮" not in history  # older than the window
    longest = max(len(line) for line in history.split("\n"))
    assert longest <= HISTORY_MESSAGE_HEAD_CHARS + HISTORY_MESSAGE_TAIL_CHARS + 20
    assert history.count("[user]") == HISTORY_TURNS


def test_recent_history_is_empty_on_the_first_message() -> None:
    assert recent_history([HumanMessage(content="你好")]) == ""


# --------------------------------------------------------------------------- observability


def test_the_log_line_has_flags_and_lengths_but_no_user_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    query = build_retrieval_query(_turns("张三的血压", "张三血压正常", "他的血糖呢"))
    assert query is not None
    with caplog.at_level(logging.INFO, logger="orchestrator.graph_builder.retrieval_query"):
        log_retrieval_query("memory_recall", query)
    (record,) = caplog.records
    line = record.getMessage()
    assert "site=memory_recall" in line
    assert "augmented=true" in line
    assert "trigger=short" in line
    assert "张三" not in line and "血糖" not in line
