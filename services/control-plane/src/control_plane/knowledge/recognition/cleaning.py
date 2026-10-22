"""B-209 §5.3 —— 识别输出的清洗与校验(纯函数,不碰网络)。

清洗:去代码围栏;HTML 表格转 Markdown 表(``colspan`` 展开、``rowspan`` 下延、实体解码);
去掉版面解析接口留下的图片占位(``![](page=0,bbox=[…])``,图片在 PR-2 处理);LaTeX 包着的
纯文本还原;去掉模型的固定空话(「No text content.」等,含出现在正文末尾的情况)。

校验:超长、空输出、截断(``finish_reason=length``)、复读(结尾的长周期重复占全文 ≥ 80%)、
字数远低于版面里有字的块合计的字数 —— 任一条成立,这次识别算失败,换兜底模型。

**输入不可信**(厂商的回答):原始回答超过 :data:`MAX_READING_BYTES` 直接算失败、不清洗;
合并单元格最多展开 :data:`_MAX_SPAN` 格、一张表最多展开 :data:`_MAX_TABLE_CELLS` 格且不超过
HTML 的 :data:`_MAX_TABLE_GROWTH` 倍(超了按单元格原文写出,不展开);所有正则都有长度上限、
表格按顺序线性扫描 —— 清洗时间与内存都随输入线性增长(复核 H1:3.3 KB 的回答曾展开成 10 MB)。

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

#: A raw reading larger than this (UTF-8 bytes) is a failed reading and never
#: cleaned — a dense page reads to ~10 KB.
MAX_READING_BYTES = 64 * 1024
#: A cleaned page longer than this (characters) is a failed reading.
MAX_PAGE_TEXT_CHARS = 30_000

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

#: Validation — a reading with fewer characters than this share of the text
#: the vendor's own layout model found in the page's text blocks lost most of
#: the page.
_MIN_LAYOUT_SHARE = 0.5
#: Validation — a repeated tail covering this share of the reading is a
#: degenerate loop (olmOCR / WeKnora both check for it).
_REPEAT_SHARE = 0.8
#: Shorter readings are not checked for repetition: a short page can be all
#: dots or dashes legitimately.
_REPEAT_MIN_CHARS = 200
#: A repeating unit needs this many word characters — 30 identical empty
#: rows of a blank form (``| | |``) are not a loop.
_REPEAT_UNIT_MIN_WORD_CHARS = 2

#: Cells a merged cell is written into at most, per direction.
_MAX_SPAN = 20
#: Cells one table may expand to; a bigger one is written as its cells' text.
_MAX_TABLE_CELLS = 2_000
#: A Markdown table longer than this many times its HTML (plus
#: :data:`_TABLE_GROWTH_ALLOWANCE` characters — a small table's separator
#: row alone can outgrow it) is written as its cells' text instead:
#: expansion is for readability, not volume.
_MAX_TABLE_GROWTH = 4
_TABLE_GROWTH_ALLOWANCE = 2_000
#: Longest LaTeX span unwrapped — bounded so the scan stays linear.
_MAX_MATH_SPAN = 200

_FENCE_LINE = re.compile(r"^[ \t]*```[\w-]*[ \t]*$", re.MULTILINE)
_TABLE_OPEN = re.compile(r"<table\b", re.IGNORECASE)
_TABLE_CLOSE = re.compile(r"</table\s{0,20}>", re.IGNORECASE)
#: Layout-parsing image region markers, e.g. ``![](page=0,bbox=[57, 199, 995, 637])``.
_IMAGE_REGION = re.compile(
    r"!\[[^\]\n]{0,200}\]\(\s{0,10}page=\d{1,5}\s{0,10},\s{0,10}bbox=\[[^\]\n]{0,200}\]\s{0,10}\)"
)
_MATH_SPAN = re.compile(
    rf"\$(?!\$)([^$\n]{{1,{_MAX_MATH_SPAN}}})(?<!\\)\$|\\\(([^\n]{{1,{_MAX_MATH_SPAN}}}?)\\\)"
)
_TEXT_COMMAND = re.compile(r"\\(?:text|mathrm|textbf|textit|mathbf|textrm)\s*\{([^{}]*)\}")
_LATEX_LEFTOVER = re.compile(r"[\\^_{}]")
_HTML_TAG = re.compile(r"<[^<>]{0,500}>")
_BLANK_RUNS = re.compile(r"\n{3,}")
_WHITESPACE = re.compile(r"\s+")
_WORD_CHAR = re.compile(r"\w")


def reading_too_large(text: str) -> bool:
    """A raw reading over :data:`MAX_READING_BYTES` — failed, not cleaned."""
    return len(text) > MAX_READING_BYTES or len(text.encode("utf-8", "replace")) > MAX_READING_BYTES


def clean_reading(text: str) -> str:
    """A model's page reading cleaned into Markdown body text (see module doc).
    Callers check :func:`reading_too_large` first."""
    cleaned = _FENCE_LINE.sub("", text)
    cleaned = _replace_tables(cleaned)
    cleaned = _IMAGE_REGION.sub("", cleaned)
    cleaned = _MATH_SPAN.sub(_restore_plain_math, cleaned)
    cleaned = _strip_boilerplate(cleaned)
    return _BLANK_RUNS.sub("\n\n", cleaned).strip()


def is_no_text_reply(text: str) -> bool:
    """The model said the page has no text (the prompt's fixed sentence or a
    stock phrase, nothing else)."""
    stripped = _FENCE_LINE.sub("", text).strip()
    return stripped in _BOILERPLATE


def layout_text_chars(blocks: list[str]) -> int:
    """Characters of text in a layout model's text blocks (markup and
    whitespace dropped) — what a faithful reading should roughly hold."""
    return sum(len(_WHITESPACE.sub("", _HTML_TAG.sub("", block))) for block in blocks)


def reading_failure(
    text: str, *, finish_reason: str | None, layout_chars: int | None
) -> str | None:
    """Why a cleaned reading cannot be kept — a short code (``truncated`` /
    ``empty`` / ``oversized`` / ``repeated`` / ``short_for_layout``) — or
    ``None`` when it can.

    ``finish_reason`` is the chat API's (``None`` for an API without one);
    ``layout_chars`` the text a layout-parsing API found in its text blocks
    (:func:`layout_text_chars`; ``None`` when it reports none)."""
    if finish_reason == "length":
        return "truncated"
    if not text.strip():
        return "empty"
    if len(text) > MAX_PAGE_TEXT_CHARS:
        return "oversized"
    if _repeated_tail(text):
        return "repeated"
    chars = len(_WHITESPACE.sub("", text))
    if layout_chars and chars < layout_chars * _MIN_LAYOUT_SHARE:
        return "short_for_layout"
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


def _replace_tables(text: str) -> str:
    """Every ``<table>…</table>`` replaced by its Markdown, in one forward
    scan (an unclosed ``<table`` ends the scan: no later table can close)."""
    parts: list[str] = []
    position = 0
    while True:
        opened = _TABLE_OPEN.search(text, position)
        if opened is None:
            break
        closed = _TABLE_CLOSE.search(text, opened.end())
        if closed is None:
            break
        parts.append(text[position : opened.start()])
        parts.append(f"\n\n{html_table_to_markdown(text[opened.start() : closed.end()])}\n\n")
        position = closed.end()
    parts.append(text[position:])
    return "".join(parts)


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
        return max(1, min(int((raw or "1")[:6]), _MAX_SPAN))
    except ValueError:
        return 1


def html_table_to_markdown(html: str) -> str:
    """One HTML ``<table>`` as a Markdown table: the first row is the header,
    a cell spanning several columns / rows is written into each of them (a
    retrieved row keeps its meaning without the merged layout), entities are
    decoded, ``|`` inside a cell is escaped. A table that would expand past
    :data:`_MAX_TABLE_CELLS` cells or :data:`_MAX_TABLE_GROWTH` times its
    HTML is written as its cells' text, one row per line, unexpanded."""
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    grid = _expand_spans(parser.rows, budget=_MAX_TABLE_CELLS)
    if grid is None:
        return _plain_cells(parser.rows)
    if not grid:
        return ""
    width = max(len(row) for row in grid)
    lines = [_md_row([*row, *([""] * (width - len(row)))]) for row in grid]
    separator = "| " + " | ".join(["---"] * width) + " |"
    markdown = "\n".join([lines[0], separator, *lines[1:]])
    if len(markdown) > _MAX_TABLE_GROWTH * len(html) + _TABLE_GROWTH_ALLOWANCE:
        return _plain_cells(parser.rows)
    return markdown


def _plain_cells(rows: list[list[_Cell]]) -> str:
    return "\n".join(
        line for line in (" ".join(cell.text for cell in row if cell.text) for row in rows) if line
    )


def _expand_spans(rows: list[list[_Cell]], *, budget: int) -> list[list[str]] | None:
    """The grid with spans written out, or ``None`` past ``budget`` cells."""
    grid: list[list[str]] = []
    carried: dict[int, tuple[int, str]] = {}  # column → (rows still to fill, text)
    cells_written = 0
    for row in rows:
        out: list[str] = []
        cells = list(reversed(row))
        column = 0
        while cells or (carried and max(carried) >= column):
            if column in carried:
                remaining, text = carried.pop(column)
                if remaining > 1:
                    carried[column] = (remaining - 1, text)
                out.append(text)
                column += 1
            elif cells:
                cell = cells.pop()
                for offset in range(cell.colspan):
                    out.append(cell.text)
                    if cell.rowspan > 1:
                        carried[column + offset] = (cell.rowspan - 1, cell.text)
                column += cell.colspan
            else:  # a gap before a cell carried down from above
                out.append("")
                column += 1
            if len(out) + cells_written > budget:
                return None
        cells_written += len(out)
        if out:
            grid.append(out)
    return grid


def _md_row(cells: list[str]) -> str:
    return "| " + " | ".join(cell.replace("|", "\\|") for cell in cells) + " |"
