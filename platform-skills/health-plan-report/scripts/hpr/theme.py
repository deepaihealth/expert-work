"""Design tokens for visual direction A (「临床专业」), derived from the resolved style."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from hpr.style import INK, contrast, mix

WITHIN = "#2E7D5B"
OUT = "#B86A0C"
ALERT = "#B42318"
WHITE = "#FFFFFF"

_SIZES = {
    "pptx": {"standard": (24, 16, 14, 11), "large": (26, 18, 16, 13), "compact": (23, 15, 13, 10)},
    "pdf": {
        "standard": (20, 13, 10.5, 8.5),
        "large": (21.5, 14.5, 12, 10),
        "compact": (19.5, 12.5, 10, 8),
    },
}
_GAPS = {"airy": (6, 12, 18, 28), "standard": (4, 8, 12, 20), "compact": (3, 6, 9, 14)}


@dataclass(frozen=True)
class Theme:
    fmt: str
    primary: str
    accent: str
    background: str
    ink: str
    muted: str
    line: str
    pale: str
    primary_soft: str
    within: str
    out: str
    alert: str
    pale_out: str
    pale_alert: str
    title: float
    heading: float
    body: float
    caption: float
    gap_xs: float
    gap_s: float
    gap_m: float
    gap_l: float
    font_cn: str = "微软雅黑"
    font_latin: str = "Arial"
    font_pdf: str = "Noto Sans CJK SC"

    @property
    def small(self) -> float:
        return max(self.caption, self.body - 1)


def on_color(bg: str) -> str:
    return WHITE if contrast(WHITE, bg) >= contrast(INK, bg) else INK


def donut_colors(t: Theme) -> list[str]:
    """Slice colours for nutrition donuts, shared by the PPT and PDF writers."""
    return [
        t.primary,
        t.accent,
        t.primary_soft,
        mix(t.primary, t.background, 0.75),
        t.muted,
        t.within,
    ]


def build_theme(style: dict[str, Any], fmt: Literal["pptx", "pdf"]) -> Theme:
    primary = style["color.primary"]
    bg = style["color.background"]
    title, heading, body, caption = _SIZES[fmt][style["type.scale"]]
    xs, s, m, lg = _GAPS[style["layout.density"]]
    return Theme(
        fmt=fmt,
        primary=primary,
        accent=style["color.accent"],
        background=bg,
        ink=INK,
        muted=mix(INK, bg, 0.40),
        line=mix(primary, bg, 0.85),
        pale=mix(primary, bg, 0.94),
        primary_soft=mix(primary, WHITE, 0.55),
        within=WITHIN,
        out=OUT,
        alert=ALERT,
        pale_out=mix(OUT, WHITE, 0.90),
        pale_alert=mix(ALERT, WHITE, 0.92),
        title=title,
        heading=heading,
        body=body,
        caption=caption,
        gap_xs=xs,
        gap_s=s,
        gap_m=m,
        gap_l=lg,
    )
