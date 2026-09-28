"""Draw a paginated plan onto a 16:9 deck (python-pptx native objects, visual direction A)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Emu, Pt

from hpr.blocks import section_prims
from hpr.icons import ICONS
from hpr.measure import LINE, Measurer
from hpr.ppt_charts import add_chart, donut_colors
from hpr.ppt_layout import (
    BODY_TOP,
    BODY_W,
    DONUT_H,
    FOOTER_BOTTOM,
    MARGIN_X,
    SLIDE_H,
    SLIDE_W,
    STRIPE,
    Ctx,
    LayoutError,
    Page,
    Placed,
    brand_line,
    callout_title_height,
    card_height,
    card_parts,
    column_height,
    column_title_height,
    columns_rows,
    donut_legend_heights,
    grid_col_w,
    grid_rows,
    image_size,
    kv_geometry,
    kv_rows,
    lh,
    make_ctx,
    media_geometry,
    paginate,
    step_height,
    table_geometry,
    timeline_rows,
)
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
    SubHeading,
    Table,
    TimeBars,
    Timeline,
)
from hpr.style import mix
from hpr.theme import WHITE, Theme, build_theme, on_color

BODY, CHROME, DECO = "hpr:body", "hpr:chrome", "hpr:deco"
NO_GRID_STYLE = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"
_ALIGN = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER, "right": PP_ALIGN.RIGHT}
_TONE = {"within": "within", "out": "out", "alert": "alert", "info": "primary", "neutral": "muted"}


def emu(pt: float) -> Emu:
    return Emu(round(pt * 12700))


def rgb(hex_: str) -> RGBColor:
    return RGBColor.from_string(hex_[1:])


def _set_font(run: Any, t: Theme, size: float, color: str, bold: bool) -> None:
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = rgb(color)
    run.font.name = t.font_latin
    rpr = run.font._element
    ea = rpr.find(qn("a:ea"))
    if ea is None:
        ea = OxmlElement("a:ea")
        rpr.insert_element_before(
            ea, "a:cs", "a:sym", "a:hlinkClick", "a:hlinkMouseOver", "a:rtl", "a:extLst"
        )
    ea.set("typeface", t.font_cn)


def _drop_style(shp: Any) -> None:
    """Remove the theme style reference so no renderer adds the theme's shadow/effects."""
    style = shp._element.find(qn("p:style"))
    if style is not None:
        shp._element.remove(style)


class Canvas:
    def __init__(self, slide: Any, theme: Theme) -> None:
        self.slide = slide
        self.t = theme

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        fill: str | None,
        *,
        line: str | None = None,
        rounded: bool = False,
        name: str = DECO,
    ) -> Any:
        kind = MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE
        shp = self.slide.shapes.add_shape(kind, emu(x), emu(y), emu(w), emu(h))
        shp.name = name
        if rounded:
            shp.adjustments[0] = min(0.5, 4.0 / max(1.0, min(w, h)))
        if fill:
            shp.fill.solid()
            shp.fill.fore_color.rgb = rgb(fill)
        else:
            shp.fill.background()
        if line:
            shp.line.color.rgb = rgb(line)
            shp.line.width = Pt(0.75)
        else:
            shp.line.fill.background()
        shp.shadow.inherit = False
        _drop_style(shp)
        return shp

    def oval(
        self, x: float, y: float, w: float, h: float, fill: str | None, line: str | None = None
    ) -> Any:
        shp = self.slide.shapes.add_shape(MSO_SHAPE.OVAL, emu(x), emu(y), emu(w), emu(h))
        shp.name = DECO
        if fill:
            shp.fill.solid()
            shp.fill.fore_color.rgb = rgb(fill)
        else:
            shp.fill.background()
        if line:
            shp.line.color.rgb = rgb(line)
            shp.line.width = Pt(0.75)
        else:
            shp.line.fill.background()
        shp.shadow.inherit = False
        _drop_style(shp)
        return shp

    def text(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        text: str,
        size: float,
        color: str,
        *,
        bold: bool = False,
        align: str = "left",
        name: str = BODY,
        link: str | None = None,
    ) -> Any:
        tb = self.slide.shapes.add_textbox(emu(x), emu(y), emu(w), emu(h))
        tb.name = name
        tf = tb.text_frame
        tf.word_wrap = True
        tf.auto_size = MSO_AUTO_SIZE.NONE
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        tf.vertical_anchor = MSO_ANCHOR.TOP
        for i, para_text in enumerate(text.split("\n")):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = _ALIGN[align]
            p.line_spacing = Pt(size * LINE)
            r = p.add_run()
            r.text = para_text
            _set_font(r, self.t, size, color, bold)
            if link:
                r.hyperlink.address = link
        return tb

    def polyline(self, pts: list[tuple[float, float]], color: str, width: float) -> Any:
        (x0, y0), rest = pts[0], pts[1:]
        builder = self.slide.shapes.build_freeform(emu(x0), emu(y0), scale=1.0)
        builder.add_line_segments([(emu(x), emu(y)) for x, y in rest], close=False)
        shp = builder.convert_to_shape()
        shp.name = DECO
        shp.fill.background()
        shp.line.color.rgb = rgb(color)
        shp.line.width = Pt(width)
        shp.shadow.inherit = False
        _drop_style(shp)
        return shp

    def icon(self, name: str, x: float, y: float, size: float, color: str) -> None:
        k = size / 24.0
        for pl in ICONS[name]:
            self.polyline([(x + px * k, y + py * k) for px, py in pl], color, 1.5)

    def picture(self, path: Path, x: float, y: float, w: float, h: float, name: str) -> Any:
        pic = self.slide.shapes.add_picture(str(path), emu(x), emu(y), emu(w), emu(h))
        pic.name = name
        return pic


# ---------- chrome ----------


def _footer(cv: Canvas, content: dict, style: dict, ctx: Ctx) -> None:
    t, m = ctx.theme, ctx.m
    brand = content.get("brand") or {}
    line1 = brand_line(brand)
    line1_h = m.lines(line1, BODY_W, t.caption) * lh(t.caption) if line1 else 0.0
    disc = brand.get("disclaimer", "")
    n_disc = m.lines(disc, BODY_W, t.caption) if disc else 0
    text_h = line1_h + n_disc * lh(t.caption)
    if not text_h:
        return
    top = FOOTER_BOTTOM - text_h
    cv.rect(MARGIN_X, top - t.gap_s, BODY_W, 0.75, t.line)
    align = style["footer.align"]
    y = top
    if line1:
        cv.text(MARGIN_X, y, BODY_W, line1_h, line1, t.caption, t.muted, align=align, name=CHROME)
        y += line1_h
    if disc:
        cv.text(
            MARGIN_X,
            y,
            BODY_W,
            n_disc * lh(t.caption),
            disc,
            t.caption,
            t.muted,
            align=align,
            name=CHROME,
        )


def _header(cv: Canvas, page: Page, number: str, style: dict, ctx: Ctx) -> None:
    t = ctx.theme
    cv.rect(0, 0, SLIDE_W * 0.6, 4, t.primary)
    cv.rect(SLIDE_W * 0.6, 0, SLIDE_W * 0.4, 4, t.accent)
    x = MARGIN_X
    if style["section.icons"]:
        cv.rect(MARGIN_X, 22, 28, 28, t.primary, rounded=True)
        cv.icon(page.icon, MARGIN_X + 5, 27, 18, on_color(t.primary))
        x = MARGIN_X + 40
    title_w = SLIDE_W - MARGIN_X - x - 100
    cv.text(
        x,
        36 - lh(t.title) / 2,
        title_w,
        lh(t.title),
        page.title,
        t.title,
        t.ink,
        bold=True,
        name=CHROME,
    )
    if style["footer.page_number"]:
        cv.text(
            SLIDE_W - MARGIN_X - 90,
            36 - lh(t.caption) / 2,
            90,
            lh(t.caption),
            number,
            t.caption,
            t.muted,
            align="right",
            name=DECO,
        )
    cv.rect(MARGIN_X, 62, BODY_W, 0.75, t.line)


def _check_titles(content: dict, ctx: Ctx, style: dict) -> None:
    t, m = ctx.theme, ctx.m
    title_w = SLIDE_W - 2 * MARGIN_X - 100 - (40 if style["section.icons"] else 0)
    for si, sec in enumerate(content["sections"]):
        if m.lines(sec["title"] + "（续）", title_w, t.title, True) > 1:  # noqa: RUF001
            raise LayoutError(f"sections[{si}].title", "章节标题过长，页标题只能一行，请缩短")  # noqa: RUF001


# ---------- brand placement ----------


def _logo_path(content: dict, base_dir: Path) -> Path | None:
    raw = (content.get("brand") or {}).get("logo_path")
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() else base_dir / p


LOGO_MAX_W, LOGO_MAX_H = 140.0, 56.0
PLATE_PAD = 8.0
BRAND_BAND_H = 76.0  # vertical room kept free for a bottom-anchored logo (with plate) or org name


def _logo_size(path: Path) -> tuple[float, float] | None:
    from PIL import Image as PILImage

    try:
        with PILImage.open(path) as im:
            iw, ih = im.size
    except (OSError, ValueError):
        return None
    k = min(LOGO_MAX_W / iw, LOGO_MAX_H / ih)
    return iw * k, ih * k


def _anchor(
    pos: str, w: float, h: float, box: tuple[float, float, float, float]
) -> tuple[float, float]:
    x0, y0, x1, y1 = box
    return {
        "top-left": (x0, y0),
        "top-right": (x1 - w, y0),
        "bottom-left": (x0, y1 - h),
        "bottom-right": (x1 - w, y1 - h),
        "center": ((x0 + x1 - w) / 2, y0),
    }[pos]


def _place_brand(
    cv: Canvas,
    content: dict,
    style: dict,
    ctx: Ctx,
    base_dir: Path,
    color: str,
    box: tuple[float, float, float, float],
    warnings: list[str],
    *,
    plate: bool = False,
) -> None:
    """Place LOGO and org name inside ``box``; ``plate`` puts the LOGO on a white rounded plate
    (used where the LOGO would sit on a dark area)."""
    t, m = ctx.theme, ctx.m
    brand = content.get("brand") or {}
    logo_pos, org_pos = style["brand.logo_position"], style["brand.org_position"]
    pad = PLATE_PAD if plate else 0.0
    logo_box: tuple[float, float, float, float] | None = None
    path = _logo_path(content, base_dir)
    if path is not None:
        size = _logo_size(path) if path.is_file() else None
        if size is None:
            warnings.append(
                f"LOGO 文件不存在或无法读取，封面只保留机构名称：{brand.get('logo_path')}"  # noqa: RUF001
            )
        else:
            lw, lh_ = size
            x0, y0, x1, y1 = box
            x, y = _anchor(logo_pos, lw, lh_, (x0 + pad, y0 + pad, x1 - pad, y1 - pad))
            if plate:
                cv.rect(x - pad, y - pad, lw + 2 * pad, lh_ + 2 * pad, WHITE, rounded=True)
            cv.picture(path, x, y, lw, lh_, "hpr:logo")
            logo_box = (x, y, lw, lh_)
    org = brand.get("org_name")
    if not org:
        return
    ow = min(m.width(org, t.body, True) * 1.2 + 4, 360.0)
    oh = m.lines(org, ow, t.body, True) * lh(t.body)
    x, y = _anchor(org_pos, ow, oh, box)
    if logo_box and org_pos == logo_pos:
        lx, ly, lw, lh_ = logo_box
        y = ly + (lh_ - oh) / 2
        if org_pos in ("top-right", "bottom-right"):
            x = lx - pad - 12 - ow
        elif org_pos == "center":
            y = ly + lh_ + pad + 8
        else:
            x = lx + lw + pad + 12
    cv.text(x, y, ow, oh, org, t.body, color, bold=True, name=CHROME)


# ---------- cover / toc / end ----------


def _meta_items(content: dict) -> list[tuple[str, str]]:
    items = [("客户", content["client"]["name"])]
    items += [(f["label"], f["value"]) for f in content["client"].get("facts", [])]
    period = content.get("period")
    if period:
        items.append(("阶段", period["label"]))
    if content.get("data_basis"):
        items.append(("数据依据", content["data_basis"]))
    items.append(("生成日期", content["generated_at"]))
    mgr = content.get("manager")
    if mgr:
        items.append((mgr.get("title") or "负责人", mgr["name"]))
    return items


def _cover_title(
    cv: Canvas,
    content: dict,
    ctx: Ctx,
    x: float,
    y: float,
    w: float,
    colors: tuple[str, str],
    max_lines: int,
    limit: tuple[float, str],
) -> float:
    """Draw title + subtitle from ``y``; both must end above ``limit`` = (y, what lies below)."""
    t, m = ctx.theme, ctx.m
    color, sub_color = colors
    bottom, below = limit
    size = t.title + 12
    n = m.lines(content["title"], w, size, True)
    if n > max_lines:
        raise LayoutError("title", f"方案名称过长（封面最多 {max_lines} 行），请缩短")  # noqa: RUF001
    title_h = n * lh(size)
    if y + title_h > bottom:
        raise LayoutError("title", f"方案名称过长，封面上会压住{below}，请缩短")  # noqa: RUF001
    k = m.lines(content["subtitle"], w, t.heading) if content.get("subtitle") else 0
    sub_top = y + title_h + t.gap_m
    if k and sub_top + k * lh(t.heading) > bottom:
        raise LayoutError(
            "subtitle",
            f"副标题过长，封面上会压住{below}，请缩短副标题或减少封面信息项",  # noqa: RUF001
        )
    cv.text(x, y, w, title_h, content["title"], size, color, bold=True, name=CHROME)
    y = sub_top
    if k:
        cv.text(x, y, w, k * lh(t.heading), content["subtitle"], t.heading, sub_color, name=CHROME)
        y += k * lh(t.heading)
    return y


@dataclass(frozen=True)
class MetaGeo:
    col_w: float
    rows: list[list[tuple[str, str]]]
    label_h: list[float]
    row_h: list[float]
    height: float  # grid height, excluding the rule drawn gap_m above it


def _meta_geometry(items: list[tuple[str, str]], ctx: Ctx, w: float) -> MetaGeo:
    t, m = ctx.theme, ctx.m
    per = 4
    cw = (w - (per - 1) * t.gap_l) / per
    rows = [items[i : i + per] for i in range(0, len(items), per)]
    if len(rows) > 3:
        raise LayoutError("client.facts", "封面信息过多（最多 12 项），请精简或移到正文")  # noqa: RUF001
    label_h = [max(m.lines(k, cw, t.caption) * lh(t.caption) for k, _ in row) for row in rows]
    row_h = [
        lab + max(m.lines(v, cw, t.body, True) * lh(t.body) for _, v in row)
        for row, lab in zip(rows, label_h, strict=True)
    ]
    return MetaGeo(cw, rows, label_h, row_h, sum(row_h) + t.gap_m * (len(rows) - 1))


def _meta_grid(
    cv: Canvas,
    geo: MetaGeo,
    ctx: Ctx,
    x: float,
    bottom: float,
    w: float,
    colors: tuple[str, str, str],
) -> None:
    t = ctx.theme
    label_color, value_color, rule = colors
    y = bottom - geo.height
    cv.rect(x, y - t.gap_m, w, 0.75, rule)
    for row, lab, rh in zip(geo.rows, geo.label_h, geo.row_h, strict=True):
        for i, (label, value) in enumerate(row):
            cx = x + i * (geo.col_w + t.gap_l)
            cv.text(cx, y, geo.col_w, lab, label, t.caption, label_color, name=CHROME)
            cv.text(
                cx, y + lab, geo.col_w, rh - lab, value, t.body, value_color, bold=True, name=CHROME
            )
        y += rh + t.gap_m


def _bg(slide: Any, color: str) -> None:
    slide.background.fill.solid()
    slide.background.fill.fore_color.rgb = rgb(color)


_META_BELOW = "下方的客户信息区"


def _cover(
    prs: Any, content: dict, style: dict, ctx: Ctx, base_dir: Path, warnings: list[str]
) -> None:
    t = ctx.theme
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    cv = Canvas(slide, t)
    variant = style["cover.variant"]
    bottom_brand = style["brand.logo_position"].startswith("bottom") or style[
        "brand.org_position"
    ].startswith("bottom")
    meta_bottom = SLIDE_H - 40 - (BRAND_BAND_H if bottom_brand else 0)
    items = _meta_items(content)
    if variant == "split":
        panel_w = SLIDE_W * 0.42
        meta_w = SLIDE_W - panel_w - 80
        geo = _meta_geometry(items, ctx, meta_w)
        _bg(slide, t.background)
        cv.rect(0, 0, panel_w, SLIDE_H, t.primary)
        fg = on_color(t.primary)
        _cover_title(
            cv,
            content,
            ctx,
            40,
            150,
            panel_w - 80,
            (fg, mix(fg, t.primary, 0.25)),
            4,
            (SLIDE_H - 40, "左侧色块底边"),
        )
        _place_brand(
            cv,
            content,
            style,
            ctx,
            base_dir,
            t.ink,
            (panel_w + 40, 32, SLIDE_W - 40, SLIDE_H - 24),
            warnings,
        )
        _meta_grid(cv, geo, ctx, panel_w + 40, meta_bottom, meta_w, (t.muted, t.ink, t.line))
        return
    if variant == "minimal":
        meta_w = SLIDE_W - 112
        geo = _meta_geometry(items, ctx, meta_w)
        limit = meta_bottom - geo.height - t.gap_m - t.gap_l
        _bg(slide, t.background)
        cv.rect(0, 0, SLIDE_W, 6, t.primary)
        # the accent bar under the subtitle needs gap_m + 4 of the room above the meta grid
        y = _cover_title(
            cv,
            content,
            ctx,
            56,
            170,
            620,
            (t.ink, t.muted),
            3,
            (limit - t.gap_m - 4, _META_BELOW),
        )
        cv.rect(56, y + t.gap_m, 64, 4, t.accent)
        _place_brand(
            cv,
            content,
            style,
            ctx,
            base_dir,
            t.primary,
            (56, 32, SLIDE_W - 56, SLIDE_H - 24),
            warnings,
        )
        _meta_grid(cv, geo, ctx, 56, meta_bottom, meta_w, (t.muted, t.ink, t.line))
        return
    meta_w = 560.0
    geo = _meta_geometry(items, ctx, meta_w)
    limit = meta_bottom - geo.height - t.gap_m - t.gap_l
    _bg(slide, t.primary)
    fg = on_color(t.primary)
    soft = mix(t.primary, fg, 0.25)
    for r in (165.0, 125.0, 85.0):
        cv.oval(790 - r, 270 - r, 2 * r, 2 * r, None, line=soft)
    # decoration stays right of the meta grid (x 56..616) and the title column (..576)
    cv.polyline(
        [(640, 330), (690, 318), (730, 336), (775, 290), (820, 298), (870, 250), (955, 225)],
        t.accent,
        2.2,
    )
    _cover_title(
        cv, content, ctx, 56, 150, 520, (fg, mix(fg, t.primary, 0.25)), 3, (limit, _META_BELOW)
    )
    _place_brand(
        cv,
        content,
        style,
        ctx,
        base_dir,
        fg,
        (56, 32, SLIDE_W - 56, SLIDE_H - 24),
        warnings,
        plate=True,
    )
    _meta_grid(cv, geo, ctx, 56, meta_bottom, meta_w, (soft, fg, soft))


def _show_toc(content: dict, style: dict) -> bool:
    return style["toc"] == "on" or (style["toc"] == "auto" and len(content["sections"]) >= 8)


def _toc(prs: Any, content: dict, style: dict, ctx: Ctx) -> None:
    t, m = ctx.theme, ctx.m
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(slide, t.background)
    cv = Canvas(slide, t)
    cv.rect(0, 0, SLIDE_W * 0.6, 4, t.primary)
    cv.rect(SLIDE_W * 0.6, 0, SLIDE_W * 0.4, 4, t.accent)
    cv.text(
        MARGIN_X,
        36 - lh(t.title) / 2,
        400,
        lh(t.title),
        "目录",
        t.title,
        t.ink,
        bold=True,
        name=DECO,
    )
    cv.rect(MARGIN_X, 62, BODY_W, 0.75, t.line)
    col_w = (BODY_W - t.gap_l) / 2
    per_col = (len(content["sections"]) + 1) // 2
    for i, sec in enumerate(content["sections"]):
        col, row = divmod(i, per_col)
        x = MARGIN_X + col * (col_w + t.gap_l)
        y = BODY_TOP + 10 + row * (lh(t.heading) + t.gap_m)
        cv.text(x, y, 44, lh(t.heading), f"{i + 1:02d}", t.heading, t.primary, bold=True, name=DECO)
        if m.lines(sec["title"], col_w - 52, t.heading) > 1:
            raise LayoutError(f"sections[{i}].title", "章节标题过长，目录只能一行，请缩短")  # noqa: RUF001
        cv.text(x + 52, y, col_w - 52, lh(t.heading), sec["title"], t.heading, t.ink, name=CHROME)
    _footer(cv, content, style, ctx)


def _end(prs: Any, content: dict, ctx: Ctx) -> None:
    t, m = ctx.theme, ctx.m
    brand = content.get("brand") or {}
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _bg(slide, t.primary)
    cv = Canvas(slide, t)
    fg = on_color(t.primary)
    y = 200.0
    org = brand["org_name"]
    n = m.lines(org, 700, t.title, True)
    cv.text(130, y, 700, n * lh(t.title), org, t.title, fg, bold=True, align="center", name=CHROME)
    y += n * lh(t.title) + t.gap_m
    if brand.get("footer_signature"):
        k = m.lines(brand["footer_signature"], 700, t.heading)
        cv.text(
            130,
            y,
            700,
            k * lh(t.heading),
            brand["footer_signature"],
            t.heading,
            mix(fg, t.primary, 0.25),
            align="center",
            name=CHROME,
        )
    if brand.get("disclaimer"):
        k = m.lines(brand["disclaimer"], BODY_W, t.caption)
        cv.text(
            MARGIN_X,
            FOOTER_BOTTOM - k * lh(t.caption),
            BODY_W,
            k * lh(t.caption),
            brand["disclaimer"],
            t.caption,
            mix(fg, t.primary, 0.25),
            align="center",
            name=CHROME,
        )


# ---------- primitives ----------


def _tone(t: Theme, tone: str) -> str:
    return getattr(t, _TONE.get(tone, "muted"))


def _draw_grid(cv: Canvas, pl: Placed, g: CardGrid, ctx: Ctx) -> None:
    t = ctx.theme
    cw = grid_col_w(g, pl.w, ctx)
    y = pl.y
    for row in grid_rows(g):
        rh = max(card_height(c, cw, ctx) for c in row)
        for i, card in enumerate(row):
            x = pl.x + i * (cw + t.gap_m)
            stripe = t.out if card.tag and card.tag.tone in ("out", "alert") else t.primary
            cv.rect(x, y, cw, rh, t.background, line=t.line)
            cv.rect(x, y, cw, STRIPE, stripe)
            cy = y + STRIPE + t.gap_m
            inner = cw - 2 * t.gap_m
            for part in card_parts(card, inner, ctx):
                if part.kind == "bar" and card.bar:
                    _range_bar(cv, x + t.gap_m, cy, inner, card.bar, t)
                else:
                    color = {"title": t.muted, "value": t.ink, "line": t.ink}.get(part.kind)
                    if part.kind == "tag" and card.tag:
                        color = _tone(t, card.tag.tone)
                    cv.text(
                        x + t.gap_m,
                        cy,
                        inner,
                        part.height,
                        part.text,
                        part.size,
                        color or t.ink,
                        bold=part.bold,
                    )
                cy += part.height + t.gap_xs
        y += rh + t.gap_m


def _range_bar(cv: Canvas, x: float, y: float, w: float, bar: Any, t: Theme) -> None:
    span = bar.high - bar.low or 1.0
    dmin, dmax = bar.low - 0.6 * span, bar.high + 0.6 * span
    pos = (min(max(bar.value, dmin), dmax) - dmin) / (dmax - dmin)
    cv.rect(x, y + 3, w, 4, t.pale)
    cv.rect(
        x + w * (bar.low - dmin) / (dmax - dmin), y + 3, w * span / (dmax - dmin), 4, t.primary_soft
    )
    cv.rect(x + w * pos - 1, y, 2, 10, t.ink)


def _cell_border_bottom(cell: Any, color: str) -> None:
    tcpr = cell._tc.get_or_add_tcPr()
    for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
        old = tcpr.find(qn(tag))
        if old is not None:
            tcpr.remove(old)
    for i, tag in enumerate(("a:lnL", "a:lnR", "a:lnT", "a:lnB")):
        ln = OxmlElement(tag)
        if tag == "a:lnB":
            ln.set("w", str(int(0.75 * 12700)))
            fill = OxmlElement("a:solidFill")
            clr = OxmlElement("a:srgbClr")
            clr.set("val", color[1:])
            fill.append(clr)
            ln.append(fill)
        else:
            ln.set("w", "0")
            ln.append(OxmlElement("a:noFill"))
        tcpr.insert(i, ln)


def _draw_table(cv: Canvas, pl: Placed, tb: Table, ctx: Ctx) -> None:
    t = ctx.theme
    geo = table_geometry(tb, pl.w, ctx)
    gf = cv.slide.shapes.add_table(
        len(tb.rows) + 1, len(tb.columns), emu(pl.x), emu(pl.y), emu(pl.w), emu(pl.h)
    )
    gf.name = BODY
    table = gf.table
    table.first_row = True
    table.horz_banding = False
    style_id = table._tbl.tblPr.find(qn("a:tableStyleId"))
    if style_id is None:
        style_id = OxmlElement("a:tableStyleId")
        table._tbl.tblPr.append(style_id)
    style_id.text = NO_GRID_STYLE
    for i, w in enumerate(geo.col_w):
        table.columns[i].width = emu(w)
    table.rows[0].height = emu(geo.header_h)
    for r, h in enumerate(geo.row_h, start=1):
        table.rows[r].height = emu(h)
    for r, cells in enumerate([tb.columns, *tb.rows]):
        for c, value in enumerate(cells):
            cell = table.cell(r, c)
            _cell_border_bottom(cell, t.line)
            cell.margin_left = cell.margin_right = emu(geo.pad_x)
            cell.margin_top = cell.margin_bottom = emu(geo.pad_y)
            cell.vertical_anchor = MSO_ANCHOR.TOP
            if r == 0:
                cell.fill.solid()
                cell.fill.fore_color.rgb = rgb(t.pale)
            else:
                cell.fill.background()
            highlight = r > 0 and tb.highlight_col == c and value not in ("", "—")
            color = t.primary if r == 0 else (t.out if highlight else t.ink)
            para = cell.text_frame.paragraphs[0]
            para.line_spacing = Pt(t.small * LINE)
            run = para.add_run()
            run.text = value
            _set_font(run, t, t.small, color, r == 0 or highlight)


def _draw_kv(cv: Canvas, pl: Placed, kv: KeyValue, ctx: Ctx) -> None:
    t = ctx.theme
    pair_w, label_w, heights = kv_geometry(kv, pl.w, ctx)
    y = pl.y
    for row, rh in zip(kv_rows(kv), heights, strict=True):
        for i, (k, v) in enumerate(row):
            x = pl.x + i * (pair_w + t.gap_l)
            cv.text(x, y + 2, label_w, rh - t.gap_s, k, t.small, t.muted)
            cv.text(
                x + label_w + t.gap_s, y, pair_w - label_w - t.gap_s, rh - t.gap_s, v, t.body, t.ink
            )
            cv.rect(x, y + rh - t.gap_s / 2, pair_w, 0.5, t.line)
        y += rh


def _draw_callout(cv: Canvas, pl: Placed, co: Callout, ctx: Ctx) -> None:
    t, m = ctx.theme, ctx.m
    bg = {"alert": t.pale_alert, "warn": t.pale_out}.get(co.tone, t.pale)
    edge = {"alert": t.alert, "warn": t.out}.get(co.tone, t.primary)
    cv.rect(pl.x, pl.y, pl.w, pl.h, bg, rounded=True)
    cv.rect(pl.x, pl.y, 4, pl.h, edge)
    x, y, w = pl.x + 4 + t.gap_m, pl.y + t.gap_m, pl.w - 4 - 2 * t.gap_m
    if co.title:
        th = callout_title_height(co, w, ctx)
        cv.text(x, y, w, th, co.title, t.body, edge, bold=True)
        y += th + t.gap_xs
    cv.text(x, y, w, m.lines(co.text, w, t.body) * lh(t.body), co.text, t.body, t.ink)


def _draw_chart(cv: Canvas, pl: Placed, ch: Chart, ctx: Ctx) -> None:
    t = ctx.theme
    if ch.kind != "donut":
        add_chart(cv.slide, ch, pl.x, pl.y, pl.w, pl.h, t)
        return
    size = min(pl.h, DONUT_H)
    add_chart(cv.slide, ch, pl.x, pl.y, size, size, t)
    if ch.center:
        cv.text(
            pl.x,
            pl.y + size / 2 - lh(t.heading) / 2,
            size,
            lh(t.heading),
            ch.center,
            t.heading,
            t.ink,
            bold=True,
            align="center",
        )
    colors = donut_colors(t)
    heights = donut_legend_heights(ch, pl.w, ctx)
    lx = pl.x + size + t.gap_l
    ly = pl.y + max(0.0, (size - sum(heights) - t.gap_xs * len(heights)) / 2)
    for i, (line, h) in enumerate(zip(ch.legend, heights, strict=True)):
        cv.rect(lx, ly + lh(t.body) / 2 - 5, 10, 10, colors[i % len(colors)])
        cv.text(lx + 18, ly, pl.w - size - t.gap_l - 18, h, line, t.body, t.ink)
        ly += h + t.gap_xs


def _draw_timeline(cv: Canvas, pl: Placed, tl: Timeline, ctx: Ctx) -> None:
    t, m = ctx.theme, ctx.m
    y = pl.y
    for row in timeline_rows(tl):
        sw = (pl.w - (len(row) - 1) * t.gap_m) / len(row)
        rh = max(step_height(s, sw, ctx) for s in row)
        cv.rect(pl.x, y + 5.5, pl.w, 1, t.line)
        for i, step in enumerate(row):
            x = pl.x + i * (sw + t.gap_m)
            cv.oval(x, y, 12, 12, t.primary)
            ly = y + 12 + t.gap_s
            label_h = m.lines(step.label, sw, t.body, True) * lh(t.body)
            cv.text(x, ly, sw, label_h, step.label, t.body, t.ink, bold=True)
            if step.lines:
                by = ly + label_h + t.gap_xs
                cv.rect(x, by, sw, y + rh - by, t.pale, rounded=True)
                cy = by + t.gap_s
                for ln in step.lines:
                    h = m.lines(ln, sw - 2 * t.gap_s, t.small) * lh(t.small)
                    cv.text(x + t.gap_s, cy, sw - 2 * t.gap_s, h, ln, t.small, t.ink)
                    cy += h
        y += rh + t.gap_l


def _draw_columns(cv: Canvas, pl: Placed, cols: Columns, ctx: Ctx) -> None:
    t, m = ctx.theme, ctx.m
    y = pl.y
    for row in columns_rows(cols):
        cw = (pl.w - (len(row) - 1) * t.gap_m) / len(row)
        rh = max(column_height(c, cw, ctx) for c in row)
        for i, col in enumerate(row):
            x = pl.x + i * (cw + t.gap_m)
            color = _tone(t, col.tone) if col.tone != "neutral" else t.primary
            cv.rect(x, y, cw, rh, t.pale, rounded=True)
            cv.rect(x, y, cw, STRIPE, color)
            cy = y + STRIPE + t.gap_m
            title_h = column_title_height(col, cw, ctx)
            cv.text(
                x + t.gap_m, cy, cw - 2 * t.gap_m, title_h, col.title, t.heading, color, bold=True
            )
            cy += title_h + t.gap_s
            inner = cw - 2 * t.gap_m
            for item in col.items:
                h = m.lines(item, inner - t.small, t.small) * lh(t.small)
                cv.text(
                    x + t.gap_m, cy, t.small, lh(t.small), "·", t.small, color, bold=True, name=DECO
                )
                cv.text(x + t.gap_m + t.small, cy, inner - t.small, h, item, t.small, t.ink)
                cy += h + t.gap_xs
        y += rh + t.gap_m


def _draw_media(
    cv: Canvas, pl: Placed, md: Media, ctx: Ctx, base_dir: Path, warnings: list[str]
) -> None:
    t, m = ctx.theme, ctx.m
    cv.rect(pl.x, pl.y, pl.w, pl.h, t.pale, rounded=True)
    geo = media_geometry(md, pl.w, ctx)
    tx, tw = pl.x + t.gap_m, geo.text_w
    cv.text(tx, pl.y + t.gap_m, tw, geo.name_h, md.name, t.heading, t.primary, bold=True)
    dy = pl.y + t.gap_m + geo.name_h + t.gap_xs
    cv.text(
        tx, dy, tw, m.lines(md.description, tw, t.body) * lh(t.body), md.description, t.body, t.ink
    )
    bx = pl.x + pl.w * 0.62
    bw, bh = geo.box_w, pl.h - 2 * t.gap_m
    by = pl.y + t.gap_m
    cv.rect(bx, by, bw, bh, t.background, line=t.line, rounded=True)
    label, link_h = geo.link_text, geo.link_h
    if md.media_path:
        path = Path(md.media_path)
        path = path if path.is_absolute() else base_dir / path
        movie_h = bh - link_h - 2 * t.gap_s
        if path.suffix.lower() == ".mp4" and path.is_file() and movie_h > 30:
            try:
                mv = cv.slide.shapes.add_movie(
                    str(path),
                    emu(bx + t.gap_s),
                    emu(by + t.gap_s),
                    emu(bw - 2 * t.gap_s),
                    emu(movie_h),
                    mime_type="video/mp4",
                )
                mv.name = "hpr:media"
            except Exception as exc:  # any embed failure degrades to the link
                warnings.append(f"视频未能嵌入，已改为可点击链接：{md.name}（{exc}）")  # noqa: RUF001
        else:
            warnings.append(f"视频文件不可用，已改为可点击链接：{md.name}")  # noqa: RUF001
    cv.text(
        bx + t.gap_s,
        by + bh - link_h - t.gap_s,
        bw - 2 * t.gap_s,
        link_h,
        label,
        t.body,
        t.primary,
        bold=True,
        name=CHROME,
        link=md.url,
    )


def _draw_image(cv: Canvas, pl: Placed, im: Image, ctx: Ctx) -> None:
    t, m = ctx.theme, ctx.m
    w, h = image_size(im, pl.w, ctx)
    path = Path(im.path)
    path = path if path.is_absolute() else ctx.base_dir / path
    cv.picture(path, pl.x, pl.y, w, h, "hpr:image")
    if im.caption:
        ch = m.lines(im.caption, pl.w, t.caption) * lh(t.caption)
        cv.text(pl.x, pl.y + h + t.gap_xs, pl.w, ch, im.caption, t.caption, t.muted)


def _minutes(hhmm: str) -> int:
    h, mm = (int(x) for x in hhmm.split(":"))
    total = h * 60 + mm
    return total + 24 * 60 if total < 18 * 60 else total


def _draw_timebars(cv: Canvas, pl: Placed, tb: TimeBars, ctx: Ctx) -> None:
    t, m = ctx.theme, ctx.m
    start, span = 18 * 60, 18 * 60
    label_w = 56.0
    ax, aw = pl.x + label_w, pl.w - label_w
    y = pl.y
    for i, (label, bed, wake) in enumerate(tb.rows):
        rh = lh(t.body)
        cv.text(pl.x, y, label_w - 8, rh, label, t.body, t.muted, bold=True, name=DECO)
        b, w_ = _minutes(bed), _minutes(wake)
        if w_ <= b:
            w_ += 24 * 60
        x0 = ax + aw * max(0, min(span, b - start)) / span
        x1 = ax + aw * max(0, min(span, w_ - start)) / span
        color = t.primary if i == len(tb.rows) - 1 else t.primary_soft
        cv.rect(x0, y + 2, max(4.0, x1 - x0), rh - 4, color, rounded=True)
        text = f"{bed} – {wake}"  # noqa: RUF001
        tw = m.width(text, t.small, True) * 1.2 + 4
        inside = tw <= (x1 - x0) - 8
        tx = x0 + 6 if inside else min(x1 + 6, pl.x + pl.w - tw)
        cv.text(
            tx,
            y + (rh - lh(t.small)) / 2,
            tw,
            lh(t.small),
            text,
            t.small,
            on_color(color) if inside else t.ink,
            bold=True,
        )
        y += rh + t.gap_s
    for k in range(0, 7):
        mins = start + k * 180
        hh = (mins // 60) % 24
        tx = ax + aw * k / 6 - 16
        cv.text(
            max(pl.x, min(tx, pl.x + pl.w - 32)),
            y,
            32,
            lh(t.caption),
            f"{hh:02d}:00",
            t.caption,
            t.muted,
            align="center",
            name=DECO,
        )


def _draw(cv: Canvas, pl: Placed, ctx: Ctx, base_dir: Path, warnings: list[str]) -> None:
    t, m = ctx.theme, ctx.m
    p = pl.prim
    if isinstance(p, SubHeading):
        cv.rect(pl.x - 10, pl.y + lh(t.heading) * 0.2, 3, lh(t.heading) * 0.6, t.accent)
        cv.text(pl.x, pl.y, pl.w, pl.h - t.gap_xs, p.text, t.heading, t.ink, bold=True)
    elif isinstance(p, Paragraph):
        pad = t.gap_m if p.boxed else 0.0
        if p.boxed:
            cv.rect(pl.x, pl.y, pl.w, pl.h, t.pale, rounded=True)
        cv.text(pl.x + pad, pl.y + pad, pl.w - 2 * pad, pl.h - 2 * pad, p.text, t.body, t.ink)
    elif isinstance(p, Bullets):
        indent = t.body * 1.4
        y = pl.y
        for n, item in enumerate(p.items, start=1):
            h = m.lines(item, pl.w - indent, t.body) * lh(t.body)
            marker = {"numbers": f"{n}.", "checks": "✓"}.get(p.style, "•")
            cv.text(pl.x, y, indent, lh(t.body), marker, t.body, t.primary, bold=True, name=DECO)
            cv.text(pl.x + indent, y, pl.w - indent, h, item, t.body, t.ink)
            y += h + t.gap_xs
    elif isinstance(p, CardGrid):
        _draw_grid(cv, pl, p, ctx)
    elif isinstance(p, Table):
        _draw_table(cv, pl, p, ctx)
    elif isinstance(p, KeyValue):
        _draw_kv(cv, pl, p, ctx)
    elif isinstance(p, Callout):
        _draw_callout(cv, pl, p, ctx)
    elif isinstance(p, Chart):
        _draw_chart(cv, pl, p, ctx)
    elif isinstance(p, Timeline):
        _draw_timeline(cv, pl, p, ctx)
    elif isinstance(p, Columns):
        _draw_columns(cv, pl, p, ctx)
    elif isinstance(p, Media):
        _draw_media(cv, pl, p, ctx, base_dir, warnings)
    elif isinstance(p, Image):
        _draw_image(cv, pl, p, ctx)
    elif isinstance(p, TimeBars):
        _draw_timebars(cv, pl, p, ctx)
    else:
        raise TypeError(type(p).__name__)


def render_pptx(
    content: dict, style: dict, out_path: Path, base_dir: Path, m: Measurer
) -> list[str]:
    theme = build_theme(style, "pptx")
    ctx = make_ctx(content, theme, m, base_dir)
    _check_titles(content, ctx, style)
    pages = paginate(section_prims(content, style), ctx)
    warnings: list[str] = []
    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)
    toc = _show_toc(content, style)
    end = bool((content.get("brand") or {}).get("org_name"))
    total = 1 + int(toc) + len(pages) + int(end)
    _cover(prs, content, style, ctx, base_dir, warnings)
    if toc:
        _toc(prs, content, style, ctx)
    first_number = 2 + int(toc)
    for i, page in enumerate(pages):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        _bg(slide, theme.background)
        cv = Canvas(slide, theme)
        _header(cv, page, f"{first_number + i} / {total}", style, ctx)
        for pl in page.placed:
            _draw(cv, pl, ctx, base_dir, warnings)
        _footer(cv, content, style, ctx)
    if end:
        _end(prs, content, ctx)
    prs.save(str(out_path))
    return warnings
