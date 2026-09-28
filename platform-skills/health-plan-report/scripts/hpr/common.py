"""Presentation rules shared by the PPT and PDF writers, so the two formats cannot drift."""

from __future__ import annotations

import math
import re
from collections.abc import Callable


def bar_domain(values: tuple[float, ...] | list[float]) -> tuple[float, float]:
    """Value-axis range for a bar chart: always contains zero (bars grow from a zero baseline,
    negatives hang below it) and pads 15% beyond the data on the side(s) that have data."""
    lo, hi = min(min(values), 0.0), max(max(values), 0.0)
    pad = (hi - lo) * 0.15 or 1.0
    return (lo - pad if lo < 0 else 0.0), (hi + pad if hi > 0 or lo == 0 else 0.0)


_NICE = (1.0, 2.0, 2.5, 5.0, 10.0)


def nice_ticks(lo: float, hi: float, target: int = 5) -> tuple[float, float, float]:
    """Axis (min, max, step) for the range lo..hi: step is 1 / 2 / 2.5 / 5 x 10^n giving at most
    ``target`` intervals, and min / max are the step multiples just covering lo..hi. PPT and PDF
    both use it, so the native chart and the SVG show the same ticks."""
    raw = (hi - lo) / target
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in _NICE if m * mag >= raw * (1 - 1e-9))
    return (
        round(math.floor(lo / step + 1e-9) * step, 12),
        round(math.ceil(hi / step - 1e-9) * step, 12),
        step,
    )


def tick_decimals(step: float) -> int:
    """Decimals that print every multiple of ``step`` exactly (0.25 -> 2, 0.5 -> 1, 5 -> 0)."""
    for d in range(12):
        if abs(step * 10**d - round(step * 10**d)) < 1e-9:
            return d
    return 12


def tick_label(v: float, step: float) -> str:
    text = f"{v:.{tick_decimals(step)}f}"
    return "0" if float(text) == 0 else text


def cover_meta_items(content: dict) -> list[tuple[str, str]]:
    """The cover's client-info cells, in order (the cover content rule for both formats)."""
    items = [("客户", content["client"]["name"])]
    items += [(f["label"], f["value"]) for f in content["client"].get("facts", [])]
    if content.get("period"):
        items.append(("阶段", content["period"]["label"]))
    if content.get("data_basis"):
        items.append(("数据依据", content["data_basis"]))
    items.append(("生成日期", content["generated_at"]))
    if content.get("manager"):
        items.append((content["manager"].get("title") or "负责人", content["manager"]["name"]))
    return items


def clock_minutes(hhmm: str) -> int:
    """Minutes on the sleep axis, which runs 18:00 → next day 12:00 (times before 18:00 are the
    next morning)."""
    h, m = (int(x) for x in hhmm.split(":"))
    total = h * 60 + m
    return total + 24 * 60 if total < 18 * 60 else total


_CJK = "\u3400-\u9fff\uf900-\ufaff"
_NOBREAK = re.compile(
    # an opening bracket stays with what follows, closing punctuation with what precedes;
    # between them: a number (+ its unit), a latin word, a short CJK label (<= 4 chars)
    rf"[（(《「『【]?"  # noqa: RUF001
    rf"(?:[0-9][0-9A-Za-z.,:%/+~–\-]*(?: ?(?:[A-Za-z]+|[{_CJK}]))?"  # noqa: RUF001
    rf"|[A-Za-z][A-Za-z0-9.,:%/+~–\-_]*"  # noqa: RUF001
    rf"|(?<![{_CJK}])[{_CJK}]{{1,4}}(?![{_CJK}])"
    rf"|\S)"
    rf"[）)》」』】，。、；：！？%]*"  # noqa: RUF001
)


def nobreak_tokens(text: str) -> list[str]:
    """The pieces of ``text`` a line must never break inside: times, dates and numbers with
    their unit, latin words, short CJK labels (<= 4 chars), each with its brackets. Both
    writers size table columns and cover fact cells so that the longest of them fits."""
    return _NOBREAK.findall(text)


SHORT_COLUMN = 0.25  # a column whose longest cell fits in this share of the width never wraps


def column_widths(
    columns: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
    total: float,
    width: Callable[[int, str], float],
) -> list[float]:
    """Table column widths summing to ``total``. ``width(i, text)`` is the room ``text`` needs
    in column i (padding included). Every column first gets its longest unbreakable token, so
    no short cell breaks mid-token; the rest goes to the columns whose full text still wraps,
    in proportion to how much more they need (then, if room is left, to all columns)."""
    cells = [[columns[i]] + [r[i] for r in rows] for i in range(len(columns))]
    need = [
        max(width(i, tok) for c in col for tok in (nobreak_tokens(c) or [c]))
        for i, col in enumerate(cells)
    ]
    want = [
        max(need[i], *(width(i, ln) for c in col for ln in c.split("\n")))
        for i, col in enumerate(cells)
    ]
    if sum(need) >= total:  # cannot honour every token: scale down (a very wide table)
        return [x * total / sum(need) for x in need]
    if sum(want) <= total:  # everything fits on one line: share the slack by width
        return [w + (total - sum(want)) * w / sum(want) for w in want]
    base = list(need)
    for i in sorted(range(len(want)), key=want.__getitem__):  # short columns: whole text
        if want[i] <= total * SHORT_COLUMN and sum(base) - base[i] + want[i] <= total:
            base[i] = want[i]
    extra = total - sum(base)
    gap = [w - b for w, b in zip(want, base, strict=True)]
    return [b + extra * g / sum(gap) for b, g in zip(base, gap, strict=True)]
