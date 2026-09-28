"""Presentation rules shared by the PPT and PDF writers, so the two formats cannot drift."""

from __future__ import annotations


def bar_domain(values: tuple[float, ...] | list[float]) -> tuple[float, float]:
    """Value-axis range for a bar chart: always contains zero (bars grow from a zero baseline,
    negatives hang below it) and pads 15% beyond the data on the side(s) that have data."""
    lo, hi = min(min(values), 0.0), max(max(values), 0.0)
    pad = (hi - lo) * 0.15 or 1.0
    return (lo - pad if lo < 0 else 0.0), (hi + pad if hi > 0 or lo == 0 else 0.0)


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
