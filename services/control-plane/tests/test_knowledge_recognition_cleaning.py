"""B-209 §5.3 —— 识别输出的清洗与校验。"""

from __future__ import annotations

import gc
import time

import pytest

from control_plane.knowledge.recognition.cleaning import (
    MAX_PAGE_TEXT_CHARS,
    MAX_READING_BYTES,
    NO_TEXT_SENTENCE,
    clean_reading,
    html_table_to_markdown,
    is_no_text_reply,
    layout_text_chars,
    reading_failure,
    reading_too_large,
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
    assert reading_failure(_NORMAL_PAGE, finish_reason="stop", layout_chars=None) is None
    chars = len("".join(_NORMAL_PAGE.split()))
    assert reading_failure(_NORMAL_PAGE, finish_reason=None, layout_chars=chars) is None


@pytest.mark.parametrize(
    ("text", "finish_reason", "layout_chars", "reason"),
    [
        (_NORMAL_PAGE, "length", None, "truncated"),
        ("  \n", "stop", None, "empty"),
        ("", None, 30, "empty"),
        ("表 3 汇总如下。" + "血压正常,继续观察。" * 60, "stop", None, "repeated"),
        ("只识别出几个字", None, 400, "short_for_layout"),
        ("字" * (MAX_PAGE_TEXT_CHARS + 1), "stop", None, "oversized"),
    ],
)
def test_reading_failures(
    text: str, finish_reason: str | None, layout_chars: int | None, reason: str
) -> None:
    assert reading_failure(text, finish_reason=finish_reason, layout_chars=layout_chars) == reason


def test_blank_form_rows_are_not_a_loop() -> None:
    form = "| 日期 | 签名 |\n| --- | --- |\n" + "|  |  |\n" * 80
    assert reading_failure(form, finish_reason="stop", layout_chars=None) is None


def test_a_repeat_covering_less_than_eighty_percent_passes() -> None:
    text = _NORMAL_PAGE + "\n" + "附注。" * 20
    assert reading_failure(text, finish_reason="stop", layout_chars=None) is None


def test_a_form_page_of_short_labels_is_not_too_short_for_its_layout() -> None:
    # 40 blocks of two-character labels: the layout holds 80 characters of text,
    # the reading all of them — not "short" because each block is short.
    labels = ["姓名", "性别", "年龄", "电话"] * 10
    reading = "\n".join(labels)
    assert layout_text_chars(labels) == 80
    assert reading_failure(reading, finish_reason=None, layout_chars=80) is None
    # Image blocks carry no text; markup inside a block is not text either.
    assert layout_text_chars(["<table><tr><td>空腹</td></tr></table>", ""]) == 2


# ---------------------------------------------------------------------------
# H1 —— 厂商回答不可信:清洗的时间与内存随输入线性增长
# ---------------------------------------------------------------------------


def _span_bomb(cells: int, text: str = "A") -> str:
    """The reviewer's PoC shape: one row of huge colspan/rowspan cells and 99
    empty rows (3.3 KB of it expanded to 10 MB before the fix)."""
    cell = f'<td colspan="100" rowspan="100">{text}</td>'
    return "```html\n<table><tr>" + cell * cells + "</tr>" + "<tr></tr>" * 99 + "</table>\n```"


@pytest.mark.parametrize(("cells", "text"), [(30, "A"), (100, "A"), (300, "A"), (30, "A" * 300)])
def test_span_bombs_stay_small(cells: int, text: str) -> None:
    reading = _span_bomb(cells, text)
    started = time.perf_counter()
    cleaned = clean_reading(reading)
    # Expansion stops at the cell budget instead of building the whole grid first.
    assert time.perf_counter() - started < 0.5
    assert len(cleaned) <= 4 * len(reading) + 2_000


def test_a_span_is_clamped() -> None:
    row = html_table_to_markdown('<table><tr><td colspan="999">x</td></tr></table>')
    assert row.splitlines()[0] == "| " + " | ".join(["x"] * 20) + " |"


def test_a_table_past_the_cell_budget_is_written_as_cell_text() -> None:
    # 101 rows x 20 columns = 2,020 cells > 2,000, no spans, so its Markdown is
    # no larger than its HTML: only the cell budget turns it into cell text.
    row = "<tr>" + "<td>c</td>" * 20 + "</tr>"
    written = html_table_to_markdown(f"<table>{row * 101}</table>")
    assert "|" not in written
    assert written.splitlines() == [" ".join(["c"] * 20)] * 101


def test_a_table_growing_past_four_times_its_html_is_written_as_cell_text() -> None:
    # 10 cells x colspan 20 = 200 cells (under the budget), each a long text:
    # the Markdown would be ~20x the HTML.
    cells = "".join(f'<td colspan="20">{"word" * 50}{i}</td>' for i in range(10))
    table = f"<table><tr>{cells}</tr></table>"
    written = html_table_to_markdown(table)
    assert len(written) <= 4 * len(table) + 2_000
    assert written == " ".join(f"{'word' * 50}{i}" for i in range(10))


@pytest.mark.parametrize(
    "reading",
    [
        "\\(" * (MAX_READING_BYTES // 2),  # unclosed math delimiters
        "<table " * (MAX_READING_BYTES // 7),  # unclosed tables
        "![" * (MAX_READING_BYTES // 2),  # unclosed image markers
        "$a" * (MAX_READING_BYTES // 2),
    ],
    ids=["math", "table", "image", "dollar"],
)
def test_degenerate_markup_is_cleaned_in_linear_time(reading: str) -> None:
    # The largest reading that still gets cleaned; each runs in ~0.05 s or less,
    # a quadratic pattern takes 0.5 s and more. Timed in this thread's CPU time
    # with automatic GC off: a wall-clock bound went red under `-n auto` when a
    # full collection (0.2-0.45 s in a long-lived xdist worker) or a busy
    # runner landed inside the call.
    assert not reading_too_large(reading)
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        started = time.thread_time()
        clean_reading(reading)
        spent = time.thread_time() - started
    finally:
        if gc_was_enabled:
            gc.enable()
    assert spent < 0.25


def test_a_raw_reading_past_64_kib_is_too_large() -> None:
    assert not reading_too_large("字" * (MAX_READING_BYTES // 3))
    assert reading_too_large("字" * (MAX_READING_BYTES // 3 + 1))
    assert reading_too_large("a" * (MAX_READING_BYTES + 1))
