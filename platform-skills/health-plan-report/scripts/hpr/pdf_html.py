"""Build the A4 print HTML (weasyprint) for a plan: cover, optional TOC, flowing sections."""

from __future__ import annotations

import base64
import logging
import re
import unicodedata
from html import escape
from pathlib import Path
from typing import Any

from hpr.blocks import section_prims
from hpr.common import cover_meta_items
from hpr.icons import icon_for_section
from hpr.images import image_blocks
from hpr.logo import trimmed_logo
from hpr.pdf_guard import check_cover, check_footer, make_fetcher, raise_for_failed_images
from hpr.pdf_svg import bar_svg, cover_deco_svg, donut_svg, icon_svg, line_svg, timebar_svg
from hpr.ppt_layout import brand_line
from hpr.prims import (
    Bullets,
    Callout,
    CardGrid,
    Chart,
    Columns,
    Image,
    KeyValue,
    Media,
    Paragraph,
    Prim,
    SubHeading,
    Table,
    TimeBars,
    Timeline,
)
from hpr.theme import Theme, donut_colors, on_color

_POS_CLASS = {
    "top-left": "l",
    "top-right": "r",
    "bottom-left": "l",
    "bottom-right": "r",
    "center": "c",
}


_BREAKS_TEXT = frozenset(("Cc", "Cs", "Zl", "Zp"))


def clean(s: str) -> str:
    """Defence in depth: render.py already normalised C0 controls (normalize_content); this also
    covers C1 controls and U+2028 / U+2029, which weasyprint writes into the font subset and the
    whole PDF text layer comes out garbled. They become a space (not \\n, \\t)."""
    return "".join(
        " " if ch not in "\n\t" and unicodedata.category(ch) in _BREAKS_TEXT else ch for ch in s
    )


def e(s: str) -> str:
    return escape(clean(s), quote=True)


def eb(s: str) -> str:
    """Escaped text whose line breaks stay line breaks (escape first, then <br>)."""
    return e(s).replace("\n", "<br>")


_CSS_HEX = frozenset('\\"<>')
_NON_PRINTING = frozenset(("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"))


def css_string(s: str) -> str:
    """A single CSS string token: quotes, backslash, angle brackets (no `</style>` breakout) and
    every control / non-printing code point become CSS hex escapes."""
    out = []
    for ch in s:
        if ch in _CSS_HEX or unicodedata.category(ch) in _NON_PRINTING:
            out.append(f"\\{ord(ch):X} ")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _css(content: dict, style: dict, t: Theme) -> str:
    footer_box = "@bottom-center" if style["footer.align"] == "center" else "@bottom-left"
    page_no = (
        '@bottom-right { content: "第 " counter(page) " 页 / 共 " counter(pages) " 页"; '
        f"font-size: {t.caption}pt; color: {t.muted}; }}"
        if style["footer.page_number"]
        else ""
    )
    fg = on_color(t.primary)
    org = (content.get("brand") or {}).get("org_name", "")
    return f"""
@page {{
  size: A4; margin: 22mm 18mm 32mm 18mm;
  @top-left {{ content: {css_string(clean(content["title"]))}; font-size: {t.caption}pt;
  color: {t.muted};
  }}
  @top-right {{ content: {css_string(clean(org))}; font-size: {t.caption}pt; color: {t.muted}; }}
  {footer_box} {{ content: element(pfoot); width: 140mm; vertical-align: top; padding-top: 3mm; }}
  {page_no}
}}
@page cover {{ margin: 0; @top-left {{ content: none; }} @top-right {{ content: none; }}
  @bottom-left {{ content: none; }} @bottom-center {{ content: none; }}
  @bottom-right {{ content: none; }} }}
* {{ box-sizing: border-box; }}
html {{ font-family: "{t.font_pdf}", sans-serif; font-size: {t.body}pt; color: {t.ink}; }}
body {{ margin: 0; background: {t.background}; line-height: 1.55; }}
.num {{ font-variant-numeric: tabular-nums; }}
.pfoot {{ position: running(pfoot); font-size: {t.caption - 0.5}pt; color: {t.muted};
  padding-top: 3mm; border-top: 0.5pt solid {t.line}; line-height: 1.45; }}
.cover {{ page: cover; width: 210mm; height: 297mm; position: relative; overflow: hidden;
  break-after: page;
  display: grid; grid-template-rows: 1fr auto; }}
.cover-band {{ background: {t.primary}; color: {fg}; }}
.cover-minimal {{ background: {t.background}; color: {t.ink}; border-top: 3mm solid {t.primary}; }}
.cover-split {{ background: {t.background}; color: {t.ink}; }}
.cover-split .head {{ background: {t.primary}; color: {fg}; }}
.cover .deco {{ position: absolute; right: -12mm; top: 40mm; width: 120mm; }}
.cover .head {{ position: relative; padding: 18mm 20mm 12mm; }}
.cover .hero {{ padding-top: 45mm; }}
.cover h1 {{ margin: 0; max-width: 150mm; font-size: {t.title + 10}pt; line-height: 1.25; }}
.cover .sub {{ margin-top: 5mm; max-width: 160mm; font-size: {t.heading}pt; opacity: 0.85; }}
.cover-minimal .rule {{ width: 18mm; height: 1.2mm; margin-top: 6mm; background: {t.accent}; }}
.cover .foot {{ position: relative; padding: 10mm 20mm 20mm; }}
.cover-split .foot {{ min-height: 110mm; padding-top: 14mm; }}
.cover .meta {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 5mm 8mm;
  border-top: 0.5pt solid currentColor; padding-top: 5mm; }}
.cover .meta .k {{ font-size: {t.caption}pt; opacity: 0.75; }}
.cover-band .sub, .cover-band .meta .k, .cover-split .head .sub {{ color: {t.on_primary_soft};
  opacity: 1; }}
.cover .meta .v {{ font-weight: 700; }}
.brand-row {{ display: grid; grid-template-columns: 1fr auto 1fr; align-items: center; }}
.foot .brand-row {{ margin-top: 8mm; }}
.brand {{ display: flex; align-items: center; gap: 4mm; font-weight: 700; }}
.brand.l {{ grid-column: 1; justify-self: start; }}
.brand.c {{ grid-column: 2; flex-direction: column; gap: 2mm; }}
.brand.r {{ grid-column: 3; justify-self: end; }}
.brand img {{ display: block; max-height: 14mm; max-width: 38mm; }}
.brand .plate {{ display: block; background: #FFFFFF; border-radius: 2mm; padding: 2.8mm; }}
.cover-minimal .brand, .cover-split .foot .brand {{ color: {t.primary}; }}
.toc {{ break-after: page; }}
.toc h2 {{ font-size: {t.title}pt; }}
.toc a {{ display: block; color: {t.ink}; text-decoration: none; padding: 2mm 0;
  border-bottom: 0.5pt solid {t.line}; }}
.toc a::after {{ content: leader('.') target-counter(attr(href), page); color: {t.muted}; }}
.sec h2 {{ display: flex; align-items: center; gap: 3mm; font-size: {t.title}pt; margin: 10mm 0 4mm;
  padding-bottom: 2mm; border-bottom: 0.5pt solid {t.line}; break-after: avoid; }}
.sec h2 .ic {{ display: inline-block; background: {t.primary}; border-radius: 1.5mm;
  padding: 1.2mm; line-height: 0; }}
h3 {{ font-size: {t.heading}pt; margin: 5mm 0 2mm; break-after: avoid;
  border-left: 1mm solid {t.accent}; padding-left: 2.5mm; }}
p {{ margin: 0 0 3mm; }}
p.boxed {{ background: {t.pale}; padding: 3mm 4mm; border-radius: 1.5mm; break-inside: avoid; }}
ul, ol {{ margin: 0 0 3mm; padding-left: 6mm; }}
li {{ margin-bottom: 1.2mm; }} li::marker {{ color: {t.primary}; }}
ul.checks {{ list-style: none; padding-left: 5mm; }} ul.checks li::before {{ content: "✓ ";
  color: {t.primary}; }}
.grid {{ display: grid; gap: 3mm; margin-bottom: 4mm; }}
.grid.c1 {{ grid-template-columns: 1fr; }} .grid.c2 {{ grid-template-columns: 1fr 1fr; }}
.grid.c3 {{ grid-template-columns: 1fr 1fr 1fr; }}
  .grid.c4 {{ grid-template-columns: 1fr 1fr 1fr 1fr; }}
.card {{ border: 0.5pt solid {t.line}; border-top: 1mm solid {t.primary}; padding: 3mm;
  break-inside: avoid; background: {t.background}; }}
.card.warn {{ border-top-color: {t.out}; }}
.card .t {{ font-size: {t.caption}pt; color: {t.muted}; font-weight: 700; }}
.card .v {{ font-size: {t.title}pt; font-weight: 700; line-height: 1.2; }}
.card .v small {{ font-size: {t.caption}pt; color: {t.muted}; font-weight: 400; margin-left: 1mm; }}
.card .l {{ font-size: {t.small}pt; }}
.tag {{ font-size: {t.small}pt; font-weight: 700; }}
.tone-within {{ color: {t.within_text}; }} .tone-out {{ color: {t.out_text}; }}
  .tone-alert {{ color: {t.alert_text}; }}
.tone-info {{ color: {t.primary_text}; }} .tone-neutral {{ color: {t.muted}; }}
.bar {{ position: relative; height: 3mm; margin-top: 1.5mm; }}
.bar .track {{ position: absolute; left: 0; right: 0; top: 1mm; height: 1mm; background: {t.pale};
  }}
.bar .range {{ position: absolute; top: 1mm; height: 1mm; background: {t.primary_soft}; }}
.bar .mark {{ position: absolute; top: 0; width: 0.6mm; height: 3mm; background: {t.ink}; }}
table {{ width: 100%; border-collapse: collapse; margin-bottom: 4mm; font-size: {t.small}pt; }}
thead {{ display: table-header-group; }}
th {{ background: {t.pale}; color: {t.primary_text}; text-align: left; }}
th, td {{ padding: 1.8mm 2.2mm; border-bottom: 0.5pt solid {t.line}; vertical-align: top;
  overflow-wrap: anywhere; }}
td.hl {{ color: {t.out_text}; font-weight: 700; }}
tr {{ break-inside: avoid; }}
dl.kv {{ display: grid; gap: 1mm 8mm; margin: 0 0 4mm; }}
dl.kv.c2 {{ grid-template-columns: 1fr 1fr; }} dl.kv.c1 {{ grid-template-columns: 1fr; }}
dl.kv div {{ display: grid; grid-template-columns: 34% 1fr; gap: 2mm;
  border-bottom: 0.5pt solid {t.line}; padding: 1.2mm 0; break-inside: avoid; }}
dl.kv dt {{ color: {t.muted}; font-size: {t.small}pt; }} dl.kv dd {{ margin: 0; }}
.callout {{ background: {t.pale}; border-left: 1.2mm solid {t.primary}; padding: 3mm 4mm;
  margin-bottom: 4mm; break-inside: avoid; }}
.callout.warn {{ background: {t.pale_out}; border-left-color: {t.out}; }}
.callout.alert {{ background: {t.pale_alert}; border-left-color: {t.alert}; }}
.callout .ct {{ font-weight: 700; }}
.callout.alert .ct {{ color: {t.alert_text}; }} .callout.warn .ct {{ color: {t.out_text}; }}
.keep {{ break-inside: avoid; }}
.chart {{ break-inside: avoid; margin-bottom: 4mm; }}
.donut {{ display: grid; grid-template-columns: 45mm 1fr; gap: 6mm; align-items: center;
  break-inside: avoid; margin-bottom: 4mm; }}
.donut .ring {{ position: relative; }}
.donut .center {{ position: absolute; left: 0; right: 0; top: 44%; text-align: center;
  font-weight: 700; }}
.donut .sw {{ display: inline-block; width: 3mm; height: 3mm; margin-right: 2mm;
  vertical-align: middle; }}
.timeline {{ display: grid; gap: 4mm; margin-bottom: 4mm; border-top: 0.5pt solid {t.line};
  padding-top: 3mm; }}
.step {{ break-inside: avoid; }}
.step .dot {{ width: 3mm; height: 3mm; border-radius: 50%; background: {t.primary};
  margin-top: -4.6mm; margin-bottom: 1.5mm; }}
.step .lbl {{ font-weight: 700; }}
.step .box {{ background: {t.pale}; padding: 2mm; border-radius: 1.5mm; font-size: {t.small}pt;
  margin-top: 1mm; }}
.cols {{ display: grid; gap: 3mm; margin-bottom: 4mm; }}
.col {{ background: {t.pale}; border-top: 1mm solid {t.primary}; padding: 3mm; break-inside: avoid;
  }}
.col .ch {{ font-weight: 700; font-size: {t.heading}pt; margin-bottom: 1.5mm; }}
.col.tone-within {{ border-top-color: {t.within}; }} .col.tone-out {{ border-top-color: {t.out}; }}
.col.tone-alert {{ border-top-color: {t.alert}; }}
.col ul {{ font-size: {t.small}pt; color: {t.ink}; }}
.media {{ display: grid; grid-template-columns: 62% 1fr; gap: 4mm; background: {t.pale};
  padding: 3mm; break-inside: avoid; margin-bottom: 4mm; }}
.media .mn {{ color: {t.primary_text}; font-weight: 700; font-size: {t.heading}pt; }}
.media a {{ display: block; background: {t.background}; border: 0.5pt solid {t.line};
  border-radius: 1.5mm;
  padding: 3mm; color: {t.primary_text}; font-weight: 700; text-decoration: none; }}
.media .url {{ margin-top: 1.5mm; font-size: {t.caption}pt; color: {t.muted};
  overflow-wrap: anywhere; }}
figure {{ margin: 0 0 4mm; break-inside: avoid; }} figure img {{ max-width: 100%;
  max-height: 110mm; }}
figcaption {{ font-size: {t.caption}pt; color: {t.muted}; }}
.sleep {{ display: grid; grid-template-columns: 14mm 1fr 32mm; gap: 3mm; align-items: center;
  margin-bottom: 2mm; }}
.sleep .lab {{ color: {t.muted}; font-weight: 700; }}
"""


def _logo_img(content: dict, base_dir: Path, warnings: list[str]) -> str:
    raw = (content.get("brand") or {}).get("logo_path")
    if not raw:
        return ""
    p = Path(raw)
    p = p if p.is_absolute() else base_dir / p
    logo = trimmed_logo(p) if p.is_file() else None
    if logo is None:
        warnings.append(f"LOGO 文件不存在或无法读取，封面只保留机构名称：{raw}")  # noqa: RUF001
        return ""
    return f'<img src="data:image/png;base64,{base64.b64encode(logo[0]).decode()}" alt="">'


def _brand_rows(content: dict, style: dict, logo: str, dark: dict[bool, bool]) -> tuple[str, str]:
    """(top row, bottom row) of the cover brand area; ``dark[top]`` says whether that row lies on
    a dark area, where the LOGO sits on a white plate."""
    org = (content.get("brand") or {}).get("org_name")
    lp, op = style["brand.logo_position"], style["brand.org_position"]
    groups: dict[str, list[str]] = {}
    if logo:
        groups.setdefault(lp, []).append("logo")
    if org:
        groups.setdefault(op, []).append("org")
    rows: dict[bool, list[str]] = {True: [], False: []}
    for pos, what in groups.items():
        top = not pos.startswith("bottom")
        parts = []
        for w in what:
            if w == "org":
                parts.append(f"<span>{e(org or '')}</span>")
            else:
                parts.append(f'<span class="plate">{logo}</span>' if dark[top] else logo)
        rows[top].append(f'<div class="brand {_POS_CLASS[pos]}">{"".join(parts)}</div>')
    return tuple(  # type: ignore[return-value]
        f'<div class="brand-row">{"".join(rows[k])}</div>' if rows[k] else "" for k in (True, False)
    )


def _cover(content: dict, style: dict, t: Theme, base_dir: Path, warnings: list[str]) -> str:
    """Cover in normal flow: a two-row grid, head (brand row, title, subtitle) over foot (meta
    grid, bottom brand row), so title, subtitle and meta can never overlap. The decoration lives
    inside .head: weasyprint lays out an abspos child of a grid container as a grid item."""
    variant = style["cover.variant"]
    logo = _logo_img(content, base_dir, warnings)
    dark = {True: variant in ("band", "split"), False: variant == "band"}
    top_row, bottom_row = _brand_rows(content, style, logo, dark)
    deco = (
        f'<div class="deco">{cover_deco_svg(t, on_color(t.primary))}</div>'
        if variant == "band"
        else ""
    )
    sub = f'<div class="sub">{e(content["subtitle"])}</div>' if content.get("subtitle") else ""
    rule = '<div class="rule"></div>' if variant == "minimal" else ""
    cells = "".join(
        f'<div><div class="k">{e(k)}</div><div class="v num">{e(v)}</div></div>'
        for k, v in cover_meta_items(content)
    )
    return (
        f'<section class="cover cover-{variant}">'
        f'<div class="head">{deco}{top_row}<div class="hero">'
        f"<h1>{e(content['title'])}</h1>{sub}{rule}</div></div>"
        f'<div class="foot"><div class="meta">{cells}</div>{bottom_row}</div></section>'
    )


def _card(c: Any) -> str:
    warn = " warn" if c.tag and c.tag.tone in ("out", "alert") else ""
    out = [f'<div class="card{warn}">']
    title = f"{c.badge}  {c.title}".strip() if c.badge else c.title
    if title:
        out.append(f'<div class="t">{e(title)}</div>')
    if c.value or c.unit:
        unit = f"<small>{e(c.unit)}</small>" if c.unit else ""
        out.append(f'<div class="v num">{e(c.value)}{unit}</div>')
    out += [f'<div class="l">{eb(ln)}</div>' for ln in c.lines]
    if c.tag:
        out.append(f'<div class="tag tone-{c.tag.tone}">{e(c.tag.text)}</div>')
    if c.bar:
        span = c.bar.high - c.bar.low or 1.0
        dmin, dmax = c.bar.low - 0.6 * span, c.bar.high + 0.6 * span
        pos = (min(max(c.bar.value, dmin), dmax) - dmin) / (dmax - dmin) * 100
        left = (c.bar.low - dmin) / (dmax - dmin) * 100
        width = span / (dmax - dmin) * 100
        out.append(
            f'<div class="bar"><div class="track"></div><div class="range" '
            f'style="left:{left:.1f}%;width:{width:.1f}%">'
            f'</div><div class="mark" style="left:{pos:.1f}%"></div></div>'
        )
    out.append("</div>")
    return "".join(out)


def _prim(p: Prim, t: Theme, base_dir: Path) -> str:
    if isinstance(p, SubHeading):
        return f"<h3>{e(p.text)}</h3>"
    if isinstance(p, Paragraph):
        cls = ' class="boxed"' if p.boxed else ""
        return f"<p{cls}>{eb(p.text)}</p>"
    if isinstance(p, Bullets):
        tag = "ol" if p.style == "numbers" else "ul"
        cls = ' class="checks"' if p.style == "checks" else ""
        return f"<{tag}{cls}>" + "".join(f"<li>{eb(i)}</li>" for i in p.items) + f"</{tag}>"
    if isinstance(p, CardGrid):
        return f'<div class="grid c{p.cols}">' + "".join(_card(c) for c in p.cards) + "</div>"
    if isinstance(p, Table):
        head = "".join(f"<th>{e(c)}</th>" for c in p.columns)
        rows = "".join(
            "<tr>"
            + "".join(
                f"<td "
                f'class="{"hl" if i == p.highlight_col and v not in ("", "—") else ""}">'
                f"{eb(v)}</td>"
                for i, v in enumerate(r)
            )
            + "</tr>"
            for r in p.rows
        )
        return f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>"
    if isinstance(p, KeyValue):
        return (
            f'<dl class="kv c{p.cols}">'
            + "".join(f"<div><dt>{e(k)}</dt><dd>{eb(v)}</dd></div>" for k, v in p.pairs)
            + "</dl>"
        )
    if isinstance(p, Callout):
        title = f'<div class="ct">{e(p.title)}</div>' if p.title else ""
        return f'<div class="callout {p.tone}">{title}<div>{eb(p.text)}</div></div>'
    if isinstance(p, Chart):
        if p.kind == "donut":
            colors = donut_colors(t)
            legend = "".join(
                f'<div><span class="sw" style="background:{colors[i % len(colors)]}">'
                f"</span>{e(x)}</div>"
                for i, x in enumerate(p.legend)
            )
            center = f'<div class="center">{e(p.center)}</div>' if p.center else ""
            return (
                f'<div class="donut"><div class="ring">{donut_svg(p, t)}{center}</div>'
                f"<div>{legend}</div></div>"
            )
        return f'<div class="chart">{line_svg(p, t) if p.kind == "line" else bar_svg(p, t)}</div>'
    if isinstance(p, Timeline):
        per = min(5, len(p.steps))
        steps = "".join(
            f'<div class="step"><div class="dot"></div><div class="lbl">{e(s.label)}</div>'
            + (f'<div class="box">{"<br>".join(eb(x) for x in s.lines)}</div>' if s.lines else "")
            + "</div>"
            for s in p.steps
        )
        return (
            f'<div class="timeline" style="grid-template-columns: repeat({per}, 1fr)">{steps}</div>'
        )
    if isinstance(p, Columns):
        per = len(p.columns) if len(p.columns) <= 4 else 3
        cols = "".join(
            f'<div class="col tone-{c.tone}"><div class="ch">{e(c.title)}</div><ul>'
            + "".join(f"<li>{eb(i)}</li>" for i in c.items)
            + "</ul></div>"
            for c in p.columns
        )
        return f'<div class="cols" style="grid-template-columns: repeat({per}, 1fr)">{cols}</div>'
    if isinstance(p, Media):
        return (
            f'<div class="media"><div><div class="mn">{e(p.name)}</div>'
            f"<div>{e(p.description)}</div></div>"
            f'<div><a href="{e(p.url)}">查看示范/产品详情：{e(p.name)}</a>'  # noqa: RUF001
            f'<div class="url">{e(p.url)}</div></div></div>'
        )
    if isinstance(p, Image):
        path = Path(p.path)
        path = path if path.is_absolute() else base_dir / path
        cap = f"<figcaption>{e(p.caption)}</figcaption>" if p.caption else ""
        return f'<figure><img src="{e(path.resolve().as_uri())}" alt="">{cap}</figure>'
    if isinstance(p, TimeBars):
        rows = []
        for i, (label, bed, wake) in enumerate(p.rows):
            color = t.primary if i == len(p.rows) - 1 else t.primary_soft
            rows.append(
                f'<div class="sleep"><div class="lab">{e(label)}</div>'
                f"<div>{timebar_svg(bed, wake, color, t)}</div>"
                f'<div class="num">{e(bed)} – {e(wake)}</div></div>'  # noqa: RUF001
            )
        return "".join(rows)
    raise TypeError(type(p).__name__)


def build_html(content: dict, style: dict, theme: Theme, base_dir: Path) -> tuple[str, list[str]]:
    warnings: list[str] = []
    t = theme
    parts = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        f"<style>{_css(content, style, t)}</style></head><body>",
        _pfoot(content),
        _cover(content, style, t, base_dir, warnings),
    ]
    if style["toc"] == "on" or (style["toc"] == "auto" and len(content["sections"]) >= 6):
        links = "".join(
            f'<a href="#sec-{e(s["id"])}">{i:02d}　{e(s["title"])}</a>'
            for i, s in enumerate(content["sections"], start=1)
        )
        parts.append(f'<section class="toc"><h2>目录</h2>{links}</section>')
    fg = on_color(t.primary)
    for sec, items in section_prims(content, style):
        icon = (
            f'<span class="ic">{icon_svg(icon_for_section(sec), fg, 4.5)}</span>'
            if style["section.icons"]
            else ""
        )
        body = _body(items, t, base_dir)
        parts.append(
            f'<section class="sec" id="sec-{e(sec["id"])}">'
            f"<h2>{icon}{e(sec['title'])}</h2>{body}</section>"
        )
    parts.append("</body></html>")
    return "".join(parts), warnings


def _body(items: list[tuple[Prim, str]], t: Theme, base_dir: Path) -> str:
    """Section body; a chart and the caption paragraph of the same block stay on one page."""
    out: list[str] = []
    i = 0
    while i < len(items):
        p, path = items[i]
        nxt = items[i + 1] if i + 1 < len(items) else None
        if isinstance(p, Chart) and nxt and isinstance(nxt[0], Paragraph) and nxt[1] == path:
            out.append(
                f'<div class="keep">{_prim(p, t, base_dir)}{_prim(nxt[0], t, base_dir)}</div>'
            )
            i += 2
            continue
        out.append(_prim(p, t, base_dir))
        i += 1
    return "".join(out)


def _pfoot(content: dict) -> str:
    brand = content.get("brand") or {}
    lines = [x for x in (brand_line(brand), brand.get("disclaimer", "")) if x]
    return f'<div class="pfoot">{"<br>".join(e(x) for x in lines)}</div>' if lines else ""


class PdfUnavailableError(Exception):
    """weasyprint is not installed in this environment."""


_FAILED_URL = re.compile(r"""Failed to load \w+ at (?:'([^']*)'|"([^"]*)")""")


class _Collect(logging.Handler):
    """weasyprint's resource-loading failures (it logs and skips them) become render warnings;
    environment chatter (e.g. font-subsetting backend notices) is left to the log."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []
        self.failed: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "Failed to load" in msg:
            self.messages.append(f"PDF 资源未能加载：{msg}")  # noqa: RUF001
            m = _FAILED_URL.search(msg)
            if m:
                self.failed.append(m.group(1) if m.group(1) is not None else m.group(2))


def render_pdf(content: dict, style: dict, out_path: Path, base_dir: Path) -> list[str]:
    images = image_blocks(content, style, base_dir)
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise PdfUnavailableError(
            "当前环境缺少 weasyprint，无法生成 PDF（沙箱镜像已预装）"  # noqa: RUF001
        ) from exc
    from hpr.theme import build_theme

    fetcher = make_fetcher(base_dir, frozenset(images))
    html, warnings = build_html(content, style, build_theme(style, "pdf"), base_dir)
    logger = logging.getLogger("weasyprint")
    collect = _Collect()
    logger.addHandler(collect)
    try:
        document = HTML(string=html, base_url=str(base_dir), url_fetcher=fetcher).render()
        check_cover(document)
        check_footer(document)
        raise_for_failed_images(collect.failed, images)
        document.write_pdf(str(out_path))
    finally:
        logger.removeHandler(collect)
    return warnings + collect.messages
