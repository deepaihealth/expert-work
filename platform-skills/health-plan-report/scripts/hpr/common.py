"""Presentation rules shared by the PPT and PDF writers, so the two formats cannot drift."""

from __future__ import annotations


def bar_domain(values: tuple[float, ...] | list[float]) -> tuple[float, float]:
    """Value-axis range for a bar chart: always contains zero (bars grow from a zero baseline,
    negatives hang below it) and pads 15% beyond the data on the side(s) that have data."""
    lo, hi = min(min(values), 0.0), max(max(values), 0.0)
    pad = (hi - lo) * 0.15 or 1.0
    return (lo - pad if lo < 0 else 0.0), (hi + pad if hi > 0 or lo == 0 else 0.0)
