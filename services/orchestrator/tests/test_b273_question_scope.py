"""B-273 V1 —— is an ``ask_image(region="auto")`` question about the whole image?

E1 (11-03): on whole-image questions (count / total / highest ...) a single band's partial
answer was used 54 times out of 111, wrong and not flagged. Routing by wording: 100% on the
75 measured questions, 0 false positives on 79 lookup questions. Every question below is a
generic synthetic one (E1's synthetic set and hand-written probes), no tenant content.
"""

from __future__ import annotations

import pytest

from orchestrator.tools.question_scope import is_count_question, is_whole_image_question

WHOLE = [
    # E1 synthetic whole-image questions
    "这段聊天里护士一共提醒了几次「复查」(消息里含「复查」二字)?",
    "这段聊天里客户一共报了几次血压?",
    "表中状态为「待复核」的一共有几行?",
    "表中指标值大于 20 的一共有几行?",
    "表中指标值最高的是哪个姓名?",
    "备注为「退费」的那几行,金额(元)合计是多少?",
    "表中指标值最低的是哪个姓名?",
    "这份清单一共有多少项?",
    "图中数值最大的是哪一项(站点名)?",
    "图中所有站点里最大的数值是多少?",
    "这页报告一共分为几个区(A区、B区……)?",
    # hand-written probes
    "哪个月的数值最高?",
    "最高的是哪一行?",
    "表里收缩压超过140的有几次?",
    "列表里有多少条记录?",
    "How many rows are marked pending?",
    "Which station has the highest rate?",
    "金额加起来是多少?",
    "统计一下待复核的行",
    "What is the sum of the amounts?",
    "这段聊天里护士提醒过复查吗,提醒了几回?",
    "平均每天走多少步?",
    "按金额排名前三的是哪些?",
    "体重的变化趋势怎么样?",
    "两次测量的差值是多少?",
    "What is the average score?",
    # 最X before a measurement noun still counts when the question asks 哪 (which)
    "最高分属于哪支队伍?",
    "Which month had the most visits?",
    # an apostrophe is not a quote: "total" here is a real cue
    "What's the total of John's scores?",
]

LOOKUP = [
    # E1 synthetic lookup questions
    "时间为 2026-03-16 12:04 的那条消息里,客户报的空腹血糖是多少?",
    "时间为 2026-03-19 13:57 的那条消息里,客户说走了多少步?",
    "请读出报告中「A区 血常规」里「球蛋白」这一项的「本次结果」数值。",
    "请读出报告中「F区 甲功及其他」里「游离T4」这一项的「本次结果」数值。",
    # guard probes: quoted words, field names, 最X followed by a measurement noun
    "最高血压是多少?",
    "表中「最低值」一栏写的是多少?",
    "最低值一栏的数字是多少?",
    "合计一行的金额是多少?",
    "表中“合计”那一行的数值是多少?",
    "表格里「总数」一列第3行是多少?",
    "平均值一栏写的是多少?",
    "What is the value in the Max column for row 3?",
    "最大摄氧量那一项的结果是多少?",
    "最小血小板体积是多少?",
    "收缩压最高值一栏是多少?",
    "What does the 'Total' row say?",
    'What does the "Average" column show for March?',
    "血红蛋白的结果是多少?",
    "第 3 行的日期是哪一天?",
    "What is the patient's name?",
]


@pytest.mark.parametrize("question", WHOLE)
def test_whole_image_questions_are_routed_to_the_whole_image(question: str) -> None:
    assert is_whole_image_question(question)


@pytest.mark.parametrize("question", LOOKUP)
def test_lookup_questions_still_tile(question: str) -> None:
    assert not is_whole_image_question(question)


def test_full_width_punctuation_is_folded_before_matching() -> None:
    """NFKC: a full-width question mark right after 最高 is still end-of-question punctuation."""
    assert is_whole_image_question("哪一天最高\uff1f")
    assert is_whole_image_question("数值最高的是谁\uff1f")
    assert is_whole_image_question("谁最高\uff1f")


@pytest.mark.parametrize(
    ("question", "count"),
    [
        ("这段聊天里客户一共报了几次血压?", True),
        ("How many rows are marked pending?", True),
        ("这份清单一共有多少项?", True),
        ("表中指标值最高的是哪个姓名?", False),
        ("What is the sum of the amounts?", False),
        ("最高血压是多少?", False),
    ],
)
def test_count_like_questions(question: str, count: bool) -> None:
    assert is_count_question(question) is count


def test_an_empty_question_is_not_whole() -> None:
    assert not is_whole_image_question("")
    assert not is_count_question("   ")
