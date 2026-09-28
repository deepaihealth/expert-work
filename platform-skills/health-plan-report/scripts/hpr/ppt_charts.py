"""Native, editable PowerPoint charts for trend and nutrition primitives."""

from __future__ import annotations

from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import (
    XL_CHART_TYPE,
    XL_LEGEND_POSITION,
    XL_MARKER_STYLE,
    XL_TICK_LABEL_POSITION,
)
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Emu, Pt

from hpr.common import bar_domain, nice_ticks, tick_decimals
from hpr.prims import Chart
from hpr.theme import Theme, donut_colors


def _emu(pt: float) -> Emu:
    return Emu(round(pt * 12700))


def _rgb(hex_: str) -> RGBColor:
    return RGBColor.from_string(hex_[1:])


def add_chart(slide, prim: Chart, x: float, y: float, w: float, h: float, t: Theme):  # type: ignore[no-untyped-def]
    if prim.kind == "donut":
        return _donut(slide, prim, x, y, min(w, h), t)
    cd = CategoryChartData()
    cd.categories = list(prim.categories)
    cd.add_series(prim.unit or "数值", list(prim.values))
    has_bounds = prim.kind == "line" and (prim.low is not None or prim.high is not None)
    if prim.kind == "line":
        if prim.low is not None:
            cd.add_series("目标下限", [prim.low] * len(prim.values))
        if prim.high is not None:
            cd.add_series("目标上限", [prim.high] * len(prim.values))
    ctype = XL_CHART_TYPE.LINE_MARKERS if prim.kind == "line" else XL_CHART_TYPE.COLUMN_CLUSTERED
    gf = slide.shapes.add_chart(ctype, _emu(x), _emu(y), _emu(w), _emu(h), cd)
    gf.name = "hpr:chart"
    ch = gf.chart
    ch.has_title = False
    ch.font.size = Pt(t.caption)
    ch.font.name = t.font_latin
    ch.font.color.rgb = _rgb(t.muted)
    ch.has_legend = has_bounds
    if has_bounds:
        ch.legend.position = XL_LEGEND_POSITION.BOTTOM
        ch.legend.include_in_layout = False
    values = list(prim.values) + [v for v in (prim.low, prim.high) if v is not None]
    lo, hi = min(values), max(values)
    pad = (hi - lo) * 0.15 or 1.0
    domain = bar_domain(prim.values) if prim.kind == "bar" else (lo - pad, hi + pad)
    vmin, vmax, step = nice_ticks(*domain)
    va = ch.value_axis
    va.has_major_gridlines = True
    va.major_gridlines.format.line.color.rgb = _rgb(t.line)
    va.format.line.fill.background()
    # same nice ticks as the PDF SVG (never rounded to a fixed precision: that collapsed
    # small-magnitude data to min == max)
    va.minimum_scale, va.maximum_scale, va.major_unit = vmin, vmax, step
    d = tick_decimals(step)
    va.tick_labels.number_format = "0" + ("." + "0" * d if d else "")
    va.tick_labels.number_format_is_linked = False
    va.tick_labels.font.size = Pt(t.caption)
    ca = ch.category_axis
    ca.format.line.color.rgb = _rgb(t.line)
    ca.tick_labels.font.size = Pt(t.caption)
    plot = ch.plots[0]
    first = plot.series[0]
    if prim.kind == "line":
        first.smooth = False
        first.format.line.color.rgb = _rgb(t.primary)
        first.format.line.width = Pt(2)
        first.marker.style = XL_MARKER_STYLE.CIRCLE
        first.marker.size = 5
        first.marker.format.fill.solid()
        first.marker.format.fill.fore_color.rgb = _rgb(t.primary)
        first.marker.format.line.color.rgb = _rgb(t.primary)
        for s in list(plot.series)[1:]:
            s.smooth = False
            s.format.line.color.rgb = _rgb(t.within)
            s.format.line.width = Pt(1.25)
            s.format.line.dash_style = MSO_LINE_DASH_STYLE.DASH
            s.marker.style = XL_MARKER_STYLE.NONE
    else:
        plot.gap_width = 80
        first.format.fill.solid()
        first.format.fill.fore_color.rgb = _rgb(t.primary)
        # explicit: LibreOffice otherwise draws negative columns upward
        first.invert_if_negative = False
        ca.tick_label_position = XL_TICK_LABEL_POSITION.LOW  # dates below negative bars
    return gf


def _donut(slide, prim: Chart, x: float, y: float, size: float, t: Theme):  # type: ignore[no-untyped-def]
    cd = CategoryChartData()
    cd.categories = list(prim.categories)
    cd.add_series("占比", list(prim.values))
    gf = slide.shapes.add_chart(
        XL_CHART_TYPE.DOUGHNUT, _emu(x), _emu(y), _emu(size), _emu(size), cd
    )
    gf.name = "hpr:chart"
    ch = gf.chart
    ch.has_title = False
    ch.has_legend = False
    plot = ch.plots[0]
    plot.has_data_labels = False
    colors = donut_colors(t)
    for i, point in enumerate(plot.series[0].points):
        point.format.fill.solid()
        point.format.fill.fore_color.rgb = _rgb(colors[i % len(colors)])
        point.format.line.color.rgb = _rgb(t.background)
    hole = plot._element.find(qn("c:holeSize"))
    if hole is None:
        hole = OxmlElement("c:holeSize")
        plot._element.append(hole)
    hole.set("val", "62")
    return gf
