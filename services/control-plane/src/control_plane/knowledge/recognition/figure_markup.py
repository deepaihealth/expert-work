"""B-209 PR-2 §5.4 —— 文档内图片在正文里的写法(纯函数)。

* **占位符**:解析阶段在图片的原位置放一个占位符(两端是私用区字符,解析器不会产出这种
  组合),占位符本身不带换行;描述完再换成引用块,没描述成的(被过滤、失败、待描述)换成空 ——
  占位符**一定**在切块前被替换掉(:func:`resolve_figures` / :func:`resolve_in_order`)。只有
  真的放进引用块时才在它前后断段;去掉占位符时只收拢它两边的空行,别处的空行一个不动。
* **表格里的图**:表格的一行里放不下引用块(会把表格拆坏)—— 格子里放一个行内引用
  (:func:`inline_placeholder`,描述成了就换成图片编号,没描述成就是空),引用块放在表格后面。
* **同一张图出现几次**:引用块只放在第一次出现的位置(按文档顺序),别处的同一张图不再重复。
* **引用块**:原图位置插一段引用。第一行是 :data:`FIGURE_LABEL` + 图片 id + 全角龟甲括号的
  右半,后面接一两句描述;图里有大段文字时空一行写 :data:`TEXT_LABEL` 和图中的文字,图表写
  :data:`CHART_LABEL` 和它的数据。:func:`quote_block` 的测试里有完整的例子。
* 图片 id 是原图在对象存储里的编号,分段预览按它(:data:`FIGURE_ID_PATTERN`)显示原图缩略图;
  检索结果只有文字,模型看不到图片本身。

全角龟甲括号(U+3014 / U+3015)在源码里写成转义:真实正文几乎不用它,标签不会和原文混淆。
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence

#: Placeholder delimiters: private-use code points around ``FIG:<n>``.
_OPEN = "\ue000"
_CLOSE = "\ue001"
#: A block placeholder, with the newlines right around it (bounded: linear).
_PLACEHOLDER = re.compile(f"(\\n{{0,8}}){_OPEN}FIG:(\\d{{1,6}}){_CLOSE}(\\n{{0,8}})")
_INLINE_PLACEHOLDER = re.compile(f"{_OPEN}FIGREF:(\\d{{1,6}}){_CLOSE}")
#: A scanned page's image region as the recognizer leaves it in the page text
#: (the region's index on that page), replaced by a figure placeholder or removed.
_REGION_PLACEHOLDER = re.compile(f"(\\n{{0,8}}){_OPEN}REGION:(\\d{{1,4}}){_CLOSE}(\\n{{0,8}})")
_NUMBER = re.compile(f"{_OPEN}FIG:(\\d{{1,6}}){_CLOSE}")

#: Opens a figure's quote block: the bracket, the word for "figure"; the
#: figure id and the closing bracket follow.
FIGURE_LABEL = "\u3014\u56fe"
#: Heads the text read from a figure ("text in the figure").
TEXT_LABEL = "\u3014\u56fe\u4e2d\u6587\u5b57\u3015"
#: Heads a chart's data ("chart data").
CHART_LABEL = "\u3014\u56fe\u8868\u6570\u636e\u3015"
#: The figure id on a quote block's first line — what the admin UI reads.
FIGURE_ID_PATTERN = re.compile("\u3014\u56fe ([0-9a-f]{16})\u3015")


def placeholder(number: int) -> str:
    """The block placeholder of the document's figure ``number``."""
    return f"{_OPEN}FIG:{number}{_CLOSE}"


def inline_placeholder(number: int) -> str:
    """The in-line reference to figure ``number`` (a table cell)."""
    return f"{_OPEN}FIGREF:{number}{_CLOSE}"


def region_placeholder(index: int) -> str:
    """The placeholder of image region ``index`` of a recognized page."""
    return f"{_OPEN}REGION:{index}{_CLOSE}"


def figure_numbers(text: str) -> list[int]:
    """The block placeholders in ``text``, in order."""
    return [int(match) for match in _NUMBER.findall(text)]


def regions_to_figures(text: str, numbers: Mapping[int, int]) -> str:
    """A recognized page's region placeholders turned into figure placeholders
    (region index → figure number); a region with no figure is removed."""

    def swap(match: re.Match[str]) -> str:
        number = numbers.get(int(match.group(2)))
        if number is None:
            return _gap(match.group(1), match.group(3))
        return f"{match.group(1)}{placeholder(number)}{match.group(3)}"

    return _REGION_PLACEHOLDER.sub(swap, text)


def resolve_figures(text: str, blocks: Mapping[int, str], *, placed: Collection[int] = ()) -> str:
    """Every placeholder in ``text`` replaced: a block placeholder by its
    figure's quote block as its own paragraph — only at the figure's first
    occurrence, and not for figures already ``placed`` earlier in the
    document — an in-line one by the figure's id, the rest removed. Never
    leaves a placeholder behind."""
    return resolve_in_order([text], blocks, placed=placed)[0]


def resolve_in_order(
    texts: Sequence[str], blocks: Mapping[int, str], *, placed: Collection[int] = ()
) -> list[str]:
    """:func:`resolve_figures` over a document's texts in reading order (its
    pages): a figure's quote block goes where it first appears."""
    seen = set(placed)
    resolved = []
    for text in texts:

        def swap(match: re.Match[str]) -> str:
            number = int(match.group(2))
            block = blocks.get(number)
            if not block or number in seen:
                return _gap(match.group(1), match.group(3))
            seen.add(number)
            return f"\n\n{block}\n\n"

        def inline(match: re.Match[str]) -> str:
            found = FIGURE_ID_PATTERN.search(blocks.get(int(match.group(1)), ""))
            return f"{FIGURE_LABEL} {found.group(1)}\u3015" if found else ""

        out = _PLACEHOLDER.sub(swap, text)
        out = _INLINE_PLACEHOLDER.sub(inline, out)
        resolved.append(_REGION_PLACEHOLDER.sub(lambda m: _gap(m.group(1), m.group(3)), out))
    return resolved


def _gap(before: str, after: str) -> str:
    """What is left where a placeholder was removed: the line breaks that
    were around it, at most a paragraph break — blank lines are collapsed
    only right there, nowhere else in the text (review M3)."""
    return "\n" * min(2, len(before) + len(after))


def quote_block(figure_id: str, description: str, *, text: str = "", chart: bool = False) -> str:
    """The figure's quote block: the one-line description, then (when there
    is one) the text read from it or, for a chart, its data."""
    lines = [f"> {FIGURE_LABEL} {figure_id}\u3015{_one_line(description)}"]
    body = text.strip()
    if body:
        lines.append(">")
        lines.append(f"> {CHART_LABEL if chart else TEXT_LABEL}")
        lines.extend(f"> {line}".rstrip() for line in body.splitlines())
    return "\n".join(lines)


def _one_line(text: str) -> str:
    return " ".join(text.split())
