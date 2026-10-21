"""B-209 §5.3 —— 识别输出的清洗与校验(纯函数,不碰网络)。

清洗:去代码围栏;HTML 表格转 Markdown 表(``colspan`` 展开、``rowspan`` 下延、实体解码);
去掉版面解析接口留下的图片占位(``![](page=0,bbox=[…])``,图片在 PR-2 处理);LaTeX 包着的
纯文本还原;去掉模型的固定空话(「No text content.」等,含出现在正文末尾的情况)。

校验:空输出、截断(``finish_reason=length``)、复读(结尾的长周期重复占全文 ≥ 80%)、字数
远低于版面预期(版面块数乘下限)—— 任一条成立,这次识别算失败,换兜底模型。

刻意**不做**的清洗:删纯数字行 / 1~3 位数字行(WeKnora 的做法,化验单 / 报表的数会丢,设计 §8)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

#: The fixed sentence the recognition prompt asks for when a page has no text
#: (``chat_vlm``). A reply that is exactly this is a valid, empty reading —
#: not a failure.
#: Written with escapes: the full-width tortoise-shell brackets make it a
#: string no real page holds.
NO_TEXT_SENTENCE = "\u3014本页无文字\u3015"

#: Stock phrases models emit instead of / after the page text (seen in the
#: B-209 model test: a vendor OCR model appended "No text content." to 4 of
#: 40 pages). Removed when the whole reply is one, or a trailing line is one.
_BOILERPLATE = frozenset(
    {
        NO_TEXT_SENTENCE,
        "[本页无文字]",
        "本页无文字",
        "本页无文字。",
        "No text content.",
        "No text content",
        "No text.",
        "No text found.",
    }
)

#: Validation — a reading with fewer characters than ``layout blocks * this``
#: lost most of what the vendor's own layout model found on the page.
_MIN_CHARS_PER_LAYOUT_BLOCK = 4
#: Validation — a repeated tail covering this share of the reading is a
#: degenerate loop (olmOCR / WeKnora both check for it).
_REPEAT_SHARE = 0.8
#: Shorter readings are not checked for repetition: a short page can be all
#: dots or dashes legitimately.
_REPEAT_MIN_CHARS = 200
#: A repeating unit needs this many word characters — 30 identical empty
#: rows of a blank form (``| | |``) are not a loop.
_REPEAT_UNIT_MIN_WORD_CHARS = 2

_FENCE_LINE = re.compile(r"^[ \t]*```[\w-]*[ \t]*$", re.MULTILINE)
_TABLE = re.compile(r"<table\b.*?</table\s*>", re.IGNORECASE | re.DOTALL)
#: Layout-parsing image region markers, e.g. ``![](page=0,bbox=[57, 199, 995, 637])``.
_IMAGE_REGION = re.compile(r"!\[[^\]]*\]\(\s*page=\d+\s*,\s*bbox=\[[^\]]*\]\s*\)")
_MATH_SPAN = re.compile(r"\$(?!\$)(.+?)(?<!\\)\$|\\\((.+?)\\\)", re.DOTALL)
_TEXT_COMMAND = re.compile(r"\\(?:text|mathrm|textbf|textit|mathbf|textrm)\s*\{([^{}]*)\}")
_LATEX_LEFTOVER = re.compile(r"[\\^_{}]")
_BLANK_RUNS = re.compile(r"\n{3,}")
_WHITESPACE = re.compile(r"\s+")
_WORD_CHAR = re.compile(r"\w")


def clean_reading(text: str) -> str:
    """A model's page reading cleaned into Markdown body text (see module doc)."""
    cleaned = _FENCE_LINE.sub("", text)
    cleaned = _TABLE.sub(lambda match: f"\n\n{html_table_to_markdown(match.group(0))}\n\n", cleaned)
    cleaned = _IMAGE_REGION.sub("", cleaned)
    cleaned = _MATH_SPAN.sub(_restore_plain_math, cleaned)
    cleaned = _strip_boilerplate(cleaned)
    return _BLANK_RUNS.sub("\n\n", cleaned).strip()


def is_no_text_reply(text: str) -> bool:
    """The model said the page has no text (the prompt's fixed sentence or a
    stock phrase, nothing else)."""
    stripped = _FENCE_LINE.sub("", text).strip()
    return stripped in _BOILERPLATE


def reading_failure(
    text: str, *, finish_reason: str | None, layout_blocks: int | None
) -> str | None:
    """Why a cleaned reading cannot be kept, or ``None`` when it can.

    ``finish_reason`` is the chat API's (``None`` for an API without one);
    ``layout_blocks`` the number of text blocks a layout-parsing API found
    (``None`` when it reports none)."""
    if finish_reason == "length":
        return "truncated output"
    if not text.strip():
        return "empty output"
    if _repeated_tail(text):
        return "repeated output"
    chars = len(_WHITESPACE.sub("", text))
    if layout_blocks and chars < layout_blocks * _MIN_CHARS_PER_LAYOUT_BLOCK:
        return f"{chars} characters for {layout_blocks} layout blocks"
    return None


def _strip_boilerplate(text: str) -> str:
    if text.strip() in _BOILERPLATE:
        return ""
    lines = text.rstrip().split("\n")
    while lines and lines[-1].strip() in _BOILERPLATE:
        lines.pop()
    return "\n".join(lines)


def _restore_plain_math(match: re.Match[str]) -> str:
    """``$\\text{盐酸二甲双胍}$`` → ``盐酸二甲双胍``: math delimiters around text
    that is no math. Real math (``$10^{6}$``) and a ``$`` used as a currency
    sign (no ``\\text``-like command inside) are left alone."""
    dollar, paren = match.group(1), match.group(2)
    inner = dollar if dollar is not None else paren
    unwrapped = _TEXT_COMMAND.sub(lambda m: m.group(1), inner)
    had_command = unwrapped != inner
    if (dollar is not None and not had_command) or _LATEX_LEFTOVER.search(unwrapped):
        return match.group(0)
    return unwrapped.strip()


def _repeated_tail(text: str) -> bool:
    """The reading ends in one unit repeated (at least three times) over
    :data:`_REPEAT_SHARE` of its length. Z-function on the reversed,
    whitespace-normalised text: ``z[p]`` is how far the text stays
    ``p``-periodic going back from its end — O(n)."""
    flat = _WHITESPACE.sub(" ", text).strip()
    size = len(flat)
    if size < _REPEAT_MIN_CHARS:
        return False
    rev = flat[::-1]
    z = _z_function(rev)
    for period in range(1, size // 3 + 1):
        covered = z[period] + period
        if covered < _REPEAT_SHARE * size or covered < 3 * period:
            continue
        unit = rev[:period]
        if len(_WORD_CHAR.findall(unit)) >= _REPEAT_UNIT_MIN_WORD_CHARS:
            return True
    return False


def _z_function(s: str) -> list[int]:
    size = len(s)
    z = [0] * size
    if size:
        z[0] = size
    left = right = 0
    for i in range(1, size):
        if i < right:
            z[i] = min(right - i, z[i - left])
        while i + z[i] < size and s[z[i]] == s[i + z[i]]:
            z[i] += 1
        if i + z[i] > right:
            left, right = i, i + z[i]
    return z


# ---------------------------------------------------------------------------
# HTML table → Markdown table
# ---------------------------------------------------------------------------


@dataclass
class _Cell:
    text: str
    colspan: int = 1
    rowspan: int = 1


class _TableParser(HTMLParser):
    """Collects a table's rows of cells (text + spans); entities decoded."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_Cell]] = []
        self._cell: _Cell | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            if not self.rows:
                self.rows.append([])
            spans = dict(attrs)
            self._cell = _Cell("", _span(spans.get("colspan")), _span(spans.get("rowspan")))
            self._parts = []
        elif tag == "br" and self._cell is not None:
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None:
            self._cell.text = _WHITESPACE.sub(" ", "".join(self._parts)).strip()
            self.rows[-1].append(self._cell)
            self._cell = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._parts.append(data)


def _span(raw: str | None) -> int:
    try:
        return max(1, min(int(raw or "1"), 100))
    except ValueError:
        return 1


def html_table_to_markdown(html: str) -> str:
    """One HTML ``<table>`` as a Markdown table: the first row is the header,
    a cell spanning several columns / rows is written into each of them (a
    retrieved row keeps its meaning without the merged layout), entities are
    decoded, ``|`` inside a cell is escaped."""
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    grid = _expand_spans(parser.rows)
    if not grid:
        return ""
    width = max(len(row) for row in grid)
    lines = [_md_row([*row, *([""] * (width - len(row)))]) for row in grid]
    separator = "| " + " | ".join(["---"] * width) + " |"
    return "\n".join([lines[0], separator, *lines[1:]])


def _expand_spans(rows: list[list[_Cell]]) -> list[list[str]]:
    grid: list[list[str]] = []
    carried: dict[int, tuple[int, str]] = {}  # column → (rows still to fill, text)
    for row in rows:
        out: list[str] = []
        cells = list(row)
        column = 0
        while cells or any(col >= column for col in carried):
            if column in carried:
                remaining, text = carried.pop(column)
                if remaining > 1:
                    carried[column] = (remaining - 1, text)
                out.append(text)
                column += 1
            elif cells:
                cell = cells.pop(0)
                for offset in range(cell.colspan):
                    out.append(cell.text)
                    if cell.rowspan > 1:
                        carried[column + offset] = (cell.rowspan - 1, cell.text)
                column += cell.colspan
            else:  # a gap before a cell carried down from above
                out.append("")
                column += 1
        if out:
            grid.append(out)
    return grid


def _md_row(cells: list[str]) -> str:
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"
