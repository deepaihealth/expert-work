"""Presentation rules shared by the PPT and PDF writers, so the two formats cannot drift."""

from __future__ import annotations

import math


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
