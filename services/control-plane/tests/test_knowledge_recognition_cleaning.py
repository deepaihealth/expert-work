"""B-209 §5.3 —— 识别输出的清洗与校验。"""

from __future__ import annotations

import pytest

from control_plane.knowledge.recognition.cleaning import (
    NO_TEXT_SENTENCE,
    clean_reading,
    html_table_to_markdown,
    is_no_text_reply,
    reading_failure,
)

# A layout-parsing reply's table (spans and entities as the vendor writes them).
_LAYOUT_TABLE = (
    "<table><tr><th colspan='2'>检验项目</th><th>结果</th></tr>"
    "<tr><td rowspan='2'>血糖</td><td>空腹</td><td>5.6 &amp; &lt;7.0</td></tr>"
    "<tr><td>餐后</td><td>7|8</td></tr></table>"
)


def test_html_table_becomes_a_markdown_table_with_spans_written_out() -> None:
    assert html_table_to_markdown(_LAYOUT_TABLE).splitlines() == [
        "| 检验项目 | 检验项目 | 结果 |",
        "| --- | --- | --- |",
        "| 血糖 | 空腹 | 5.6 & <7.0 |",
        "| 血糖 | 餐后 | 7\\|8 |",
    ]


def test_short_rows_are_padded_to_the_widest_row() -> None:
    table = "<table><tr><td>a</td><td>b</td><td>c</td></tr><tr><td>d</td></tr></table>"
    assert html_table_to_markdown(table).splitlines()[-1] == "| d |  |  |"


def test_table_inside_a_reading_is_replaced_in_place() -> None:
    reading = f"## 化验结果\n{_LAYOUT_TABLE}\n以上结果仅供参考。"
    cleaned = clean_reading(reading)
    assert "<table" not in cleaned
    assert cleaned.startswith("## 化验结果\n\n| 检验项目 | 检验项目 | 结果 |")
    assert cleaned.endswith("| 血糖 | 餐后 | 7\\|8 |\n\n以上结果仅供参考。")


def test_code_fences_are_removed_and_content_kept() -> None:
    assert clean_reading("```markdown\n# 第三章\n正文一行\n```") == "# 第三章\n正文一行"


def test_layout_image_regions_are_dropped() -> None:
    reading = "图 1 所示流程\n\n![](page=0,bbox=[57, 199, 995, 637])\n\n下一段"
    assert clean_reading(reading) == "图 1 所示流程\n\n下一段"


def test_latex_wrapped_plain_text_is_restored() -> None:
    assert clean_reading("药品:$\\text{盐酸二甲双胍片}$") == "药品:盐酸二甲双胍片"
    assert clean_reading("单位 \\(\\mathrm{mmol/L}\\)") == "单位 mmol/L"


def test_real_math_and_currency_are_left_alone() -> None:
    assert clean_reading("浓度 $10^{6}$ 个/mL") == "浓度 $10^{6}$ 个/mL"
    assert clean_reading("价格 $5 和 $10") == "价格 $5 和 $10"


def test_stock_phrases_are_dropped_whole_or_trailing() -> None:
    assert clean_reading("No text content.") == ""
    assert clean_reading(NO_TEXT_SENTENCE) == ""
    assert clean_reading("第一段正文\n\nNo text content.") == "第一段正文"
    # Only a whole trailing line is a stock phrase; text that mentions one stays.
    kept = "说明:若显示 No text content. 请重新扫描"
    assert clean_reading(kept) == kept


def test_no_text_reply_is_recognised() -> None:
    assert is_no_text_reply(f"  {NO_TEXT_SENTENCE}\n")
    assert is_no_text_reply("```\nNo text content.\n```")
    assert not is_no_text_reply(f"标题\n{NO_TEXT_SENTENCE}")
    assert not is_no_text_reply("")


_NORMAL_PAGE = "\n".join(
    f"第{n}条 员工每月可申请一次调休,需提前三个工作日在系统中提交申请并经主管审批。"
    for n in range(1, 12)
)


def test_a_normal_reading_passes() -> None:
    assert reading_failure(_NORMAL_PAGE, finish_reason="stop", layout_blocks=None) is None
    assert reading_failure(_NORMAL_PAGE, finish_reason=None, layout_blocks=11) is None


@pytest.mark.parametrize(
    ("text", "finish_reason", "layout_blocks", "reason"),
    [
        (_NORMAL_PAGE, "length", None, "truncated output"),
        ("  \n", "stop", None, "empty output"),
        ("", None, 3, "empty output"),
        (
            "表 3 汇总如下。" + "血压正常,继续观察。" * 60,
            "stop",
            None,
            "repeated output",
        ),
        ("只识别出几个字", None, 40, "7 characters for 40 layout blocks"),
    ],
)
def test_reading_failures(
    text: str, finish_reason: str | None, layout_blocks: int | None, reason: str
) -> None:
    assert reading_failure(text, finish_reason=finish_reason, layout_blocks=layout_blocks) == reason


def test_blank_form_rows_are_not_a_loop() -> None:
    form = "| 日期 | 签名 |\n| --- | --- |\n" + "|  |  |\n" * 80
    assert reading_failure(form, finish_reason="stop", layout_blocks=None) is None


def test_a_repeat_covering_less_than_eighty_percent_passes() -> None:
    text = _NORMAL_PAGE + "\n" + "附注。" * 20
    assert reading_failure(text, finish_reason="stop", layout_blocks=None) is None
