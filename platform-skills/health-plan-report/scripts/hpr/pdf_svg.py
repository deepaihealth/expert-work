"""Inline SVG for the PDF: charts, icons, sleep bars and cover decoration (theme colours)."""

from __future__ import annotations

import math
from html import escape
from typing import Any

from hpr.common import bar_domain, clock_minutes, nice_ticks, tick_label
from hpr.icons import ICONS
from hpr.prims import Chart
from hpr.style import mix
from hpr.theme import Theme, donut_colors


def _num(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _scale(values: list[float], lo_pad: float = 0.15) -> tuple[float, float]:
    lo, hi = min(values), max(values)
    pad = (hi - lo) * lo_pad or 1.0
    return lo - pad, hi + pad


def _text_w(s: str, size: float) -> float:
    return sum(size if ord(c) > 0x2E7F else size * 0.55 for c in s)


def _legend(entries: list[tuple[str, str, bool]], x: float, y: float, t: Theme) -> str:
    """Legend row: (label, colour, dashed) → line swatch + label, left to right from x."""
    out = []
    for label, color, dashed in entries:
        dash = ' stroke-dasharray="4 3"' if dashed else ""
        out.append(
            f'<line x1="{x:.1f}" y1="{y - 3:.1f}" x2="{x + 18:.1f}" y2="{y - 3:.1f}" '
            f'stroke="{color}" stroke-width="{1 if dashed else 2}"{dash}/>'
        )
        out.append(
            f'<text x="{x + 23:.1f}" y="{y:.1f}" font-size="9" fill="{t.muted}">'
            f"{escape(label)}</text>"
        )
        x += 23 + _text_w(label, 9) + 18
    return "".join(out)


def _value_axis(
    vmin: float, vmax: float, step: float, left: float, right: float, py: Any, t: Theme
) -> list[str]:
    """Gridline + label at every nice tick (hpr.common.nice_ticks) from vmin to vmax."""
    out = []
    for k in range(round((vmax - vmin) / step) + 1):
        v = vmin + k * step
        out.append(
            f'<line x1="{left}" y1="{py(v):.1f}" x2="{right}" y2="{py(v):.1f}" stroke="{t.line}" '
            f'stroke-width="0.6"/>'
        )
        out.append(
            f'<text x="{left - 6}" y="{py(v) + 3:.1f}" font-size="9" text-anchor="end" '
            f'fill="{t.muted}">{tick_label(v, step)}</text>'
        )
    return out


def line_svg(prim: Chart, t: Theme) -> str:
    """Line chart with the target band; the legend names the series by its unit and labels the
    bounds 「目标下限 / 目标上限」 like the PPT chart legend."""
    w, h, left, right, top, bottom = 600, 240, 42, 590, 12, 190
    vals = list(prim.values) + [v for v in (prim.low, prim.high) if v is not None]
    vmin, vmax, step = nice_ticks(*_scale(vals))
    n = len(prim.values)

    def px(i: int) -> float:
        return left + (right - left) * (i / (n - 1) if n > 1 else 0.5)

    def py(v: float) -> float:
        return bottom - (bottom - top) * (v - vmin) / (vmax - vmin)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="100%">']
    out += _value_axis(vmin, vmax, step, left, right, py, t)
    if prim.low is not None and prim.high is not None:
        out.append(
            f'<rect x="{left}" y="{py(prim.high):.1f}" width="{right - left}" '
            f'height="{py(prim.low) - py(prim.high):.1f}" '
            f'fill="{t.within}" fill-opacity="0.12"/>'
        )
    for bound in (prim.low, prim.high):
        if bound is not None:
            out.append(
                f'<line x1="{left}" y1="{py(bound):.1f}" x2="{right}" y2="{py(bound):.1f}" '
                f'stroke="{t.within}" '
                f'stroke-width="1" stroke-dasharray="4 3"/>'
            )
    pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(prim.values))
    out.append(f'<polyline points="{pts}" fill="none" stroke="{t.primary}" stroke-width="2"/>')
    for i, v in enumerate(prim.values):
        out.append(f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="2.6" fill="{t.primary}"/>')
    last = prim.values[-1]
    out.append(
        f'<text x="{px(n - 1):.1f}" y="{py(last) - 8:.1f}" font-size="10" font-weight="700" '
        f'text-anchor="end" '
        f'fill="{t.primary}">{_num(last)}</text>'
    )
    for i in sorted({0, n // 2, n - 1}):
        # end labels anchor inward so they stay inside the viewBox
        anchor = "start" if i == 0 and n > 1 else "end" if i == n - 1 and n > 1 else "middle"
        out.append(
            f'<text x="{px(i):.1f}" y="{bottom + 20}" font-size="9" text-anchor="{anchor}" '
            f'fill="{t.muted}">'
            f"{escape(prim.categories[i])}</text>"
        )
    legend = [(prim.unit or "数值", t.primary, False)]
    legend += [
        (f"{name} {_num(v)}", t.within, True)
        for name, v in (("目标下限", prim.low), ("目标上限", prim.high))
        if v is not None
    ]
    out.append(_legend(legend, left, h - 4, t))
    out.append("</svg>")
    return "".join(out)


def bar_svg(prim: Chart, t: Theme) -> str:
    """Bar chart with a value axis (5 gridlines + tick labels), a zero baseline that negative
    bars hang from, and each bar's value printed at its end — same domain as the PPT chart."""
    w, h, left, right, top, bottom = 600, 220, 42, 590, 12, 190
    vmin, vmax, step = nice_ticks(*bar_domain(prim.values))
    n = len(prim.values)
    slot = (right - left) / n

    def py(v: float) -> float:
        return bottom - (bottom - top) * (v - vmin) / (vmax - vmin)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="100%">']
    out += _value_axis(vmin, vmax, step, left, right, py, t)
    zero = py(0.0)
    for i, v in enumerate(prim.values):
        x = left + i * slot + slot * 0.2
        y0, y1 = sorted((zero, py(v)))
        out.append(
            f'<rect x="{x:.1f}" y="{y0:.1f}" width="{slot * 0.6:.1f}" height="{y1 - y0:.1f}" '
            f'fill="{t.primary}"/>'
        )
        label_y = py(v) - 4 if v >= 0 else py(v) + 11
        out.append(
            f'<text x="{x + slot * 0.3:.1f}" y="{label_y:.1f}" font-size="9" font-weight="700" '
            f'text-anchor="middle" fill="{t.ink}">{_num(v)}</text>'
        )
        out.append(
            f'<text x="{x + slot * 0.3:.1f}" y="{h - 10}" font-size="9" text-anchor="middle" '
            f'fill="{t.muted}">'
            f"{escape(prim.categories[i])}</text>"
        )
    out.append(
        f'<line data-zero="1" x1="{left}" y1="{zero:.1f}" x2="{right}" y2="{zero:.1f}" '
        f'stroke="{t.muted}" stroke-width="0.8"/>'
    )
    out.append("</svg>")
    return "".join(out)


def donut_svg(prim: Chart, t: Theme) -> str:
    """Ring slices as arc paths (dash-array rings leave hairline seams in PDF viewers)."""
    r = 34.0
    total = sum(prim.values) or 1.0
    colors = donut_colors(t)
    out = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100%">']
    start = 0.0
    for i, v in enumerate(prim.values):
        frac = v / total
        color = colors[i % len(colors)]
        if frac >= 0.9999:
            out.append(
                f'<circle cx="50" cy="50" r="{r}" fill="none" stroke="{color}" stroke-width="13"/>'
            )
        elif frac > 0:
            a0, a1 = 2 * math.pi * start, 2 * math.pi * (start + frac)
            x0, y0 = 50 + r * math.sin(a0), 50 - r * math.cos(a0)
            x1, y1 = 50 + r * math.sin(a1), 50 - r * math.cos(a1)
            large = 1 if frac > 0.5 else 0
            out.append(
                f'<path d="M{x0:.2f},{y0:.2f} A{r},{r} 0 {large} 1 {x1:.2f},{y1:.2f}" fill="none" '
                f'stroke="{color}" stroke-width="13"/>'
            )
        start += frac
    out.append("</svg>")
    return "".join(out)


def icon_svg(name: str, color: str, size_mm: float) -> str:
    lines = "".join(
        f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in pl)}" fill="none" '
        f'stroke="{color}" '
        f'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'
        for pl in ICONS[name]
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="{size_mm}mm" '
        f'height="{size_mm}mm">{lines}</svg>'
    )


def timebar_svg(bed: str, wake: str, color: str, t: Theme) -> str:
    start, span = 18 * 60, 18 * 60
    b, w = clock_minutes(bed), clock_minutes(wake)
    if w <= b:
        w += 24 * 60
    x0 = 300 * max(0, min(span, b - start)) / span
    x1 = 300 * max(0, min(span, w - start)) / span
    ticks = "".join(
        f'<line x1="{300 * k / 6:.1f}" y1="0" x2="{300 * k / 6:.1f}" y2="14" stroke="{t.line}" '
        f'stroke-width="0.5"/>'
        for k in range(7)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 14" width="100%">'
        f'<rect x="0" y="5" width="300" height="4" fill="{t.pale}"/>{ticks}'
        f'<rect x="{x0:.1f}" y="2" width="{max(3.0, x1 - x0):.1f}" height="10" rx="3" '
        f'fill="{color}"/></svg>'
    )


def cover_deco_svg(t: Theme, fg: str) -> str:
    soft = mix(t.primary, fg, 0.25)
    circles = "".join(
        f'<circle cx="160" cy="120" r="{r}" fill="none" stroke="{soft}" stroke-width="0.6"/>'
        for r in (110, 80, 50)
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 210 240" width="100%">{circles}'
        f'<polyline points="0,170 40,162 62,174 88,146 112,152 136,122 162,128 210,98" fill="none" '
        f'stroke="{t.accent}" stroke-width="1.6"/></svg>'
    )
