"""PPT cover, table of contents and end page, and brand (LOGO / organisation name) placement."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpr.common import BAND_CHROME_LABELS, CLIENT_BAND_TITLE, client_band_items, cover_meta_line
from hpr.logo import trimmed_logo
from hpr.ppt_canvas import BODY, CHROME, DECO, Canvas, _bg, _footer
from hpr.ppt_layout import (
    BAND_ID,
    BODY_TOP,
    BODY_W,
    FOOTER_BOTTOM,
    MARGIN_X,
    SLIDE_H,
    SLIDE_W,
    Ctx,
    LayoutError,
    band_geometry,
    lh,
)
from hpr.prims import InfoBand, Prim
from hpr.style import mix
from hpr.theme import WHITE, on_color

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


def _load_logo(path: Path) -> tuple[io.BytesIO, float, float] | None:
    """LOGO trimmed to its visible (alpha) bounding box, as an in-memory PNG plus its drawn size
    in pt; None when unreadable. The input file is never modified or copied on disk."""
    logo = trimmed_logo(path)
    if logo is None:
        return None
    data, iw, ih = logo
    k = min(LOGO_MAX_W / iw, LOGO_MAX_H / ih)
    return io.BytesIO(data), iw * k, ih * k


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
        logo = _load_logo(path) if path.is_file() else None
        if logo is None:
            warnings.append(
                f"LOGO 文件不存在或无法读取，封面只保留机构名称：{brand.get('logo_path')}"  # noqa: RUF001
            )
        else:
            stream, lw, lh_ = logo
            x0, y0, x1, y1 = box
            x, y = _anchor(logo_pos, lw, lh_, (x0 + pad, y0 + pad, x1 - pad, y1 - pad))
            if plate:
                cv.rect(x - pad, y - pad, lw + 2 * pad, lh_ + 2 * pad, WHITE, rounded=True)
            cv.picture(stream, x, y, lw, lh_, "hpr:logo")
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

# The cover does not scale 1:1 with body type: its sizes are capped at the standard scale
# (a large body scale must not push a normal title / subtitle off the cover), and the title
# steps down twice before a title is reported as too long.
COVER_TITLE_CAP, COVER_SUB_CAP = 24.0, 16.0
COVER_TITLE_STEPS = (12.0, 8.0, 4.0)  # added to the capped title size
TITLE_TOP_BRAND = 32 + LOGO_MAX_H + 2 * PLATE_PAD + 20  # below a top-anchored LOGO (with plate)
TITLE_TOP = 60.0
TITLE_PREFERRED_Y = 150.0
CLIENT_LABEL = "客户"


@dataclass(frozen=True)
class CoverText:
    title_size: float
    title_h: float
    sub_size: float
    sub_h: float
    height: float  # title + gap + subtitle (+ accent rule room)


def _cover_text(content: dict, ctx: Ctx, w: float, max_lines: int, rule: float) -> list[CoverText]:
    """Candidate title / subtitle geometries, largest first (the title shrinks in steps)."""
    t, m = ctx.theme, ctx.m
    sub_size = min(t.heading, COVER_SUB_CAP)
    sub = content.get("subtitle")
    sub_h = m.lines(sub, w, sub_size) * lh(sub_size) if sub else 0.0
    out = []
    for step in COVER_TITLE_STEPS:
        size = min(t.title, COVER_TITLE_CAP) + step
        n = m.lines(content["title"], w, size, True)
        if n <= max_lines:
            title_h = n * lh(size)
            height = title_h + (t.gap_m + sub_h if sub else 0.0) + rule
            out.append(CoverText(size, title_h, sub_size, sub_h, height))
    if not out:
        raise LayoutError("title", f"方案名称过长（封面最多 {max_lines} 行），请缩短")  # noqa: RUF001
    return out


def _place_cover_text(
    cv: Canvas,
    content: dict,
    ctx: Ctx,
    x: float,
    w: float,
    span: tuple[float, float],
    colors: tuple[str, str],
    max_lines: int,
    rule: float = 0.0,
) -> float:
    """Title + subtitle inside ``span`` = (top, bottom), starting at TITLE_PREFERRED_Y and
    moving up (then shrinking the title) as far as needed. Returns the subtitle's bottom."""
    t = ctx.theme
    top, bottom = span
    geo = next(
        (g for g in _cover_text(content, ctx, w, max_lines, rule) if g.height <= bottom - top), None
    )
    if geo is None:
        raise LayoutError(
            "subtitle" if content.get("subtitle") else "title",
            "方案名称/副标题过长，封面放不下，请缩短",  # noqa: RUF001
        )
    y = max(top, min(TITLE_PREFERRED_Y, bottom - geo.height))
    color, sub_color = colors
    cv.text(x, y, w, geo.title_h, content["title"], geo.title_size, color, bold=True, name=CHROME)
    y += geo.title_h
    if content.get("subtitle"):
        y += t.gap_m
        cv.text(x, y, w, geo.sub_h, content["subtitle"], geo.sub_size, sub_color, name=CHROME)
        y += geo.sub_h
    return y


def _who_height(content: dict, ctx: Ctx, w: float) -> float:
    t, m = ctx.theme, ctx.m
    size = min(t.heading, COVER_SUB_CAP)
    return lh(t.caption) + m.lines(content["client"]["name"], w, size, True) * lh(size)


def _who(
    cv: Canvas, content: dict, ctx: Ctx, x: float, y: float, w: float, colors: tuple[str, str]
) -> None:
    """The client's name under a quiet 「客户」 label."""
    t, m = ctx.theme, ctx.m
    size = min(t.heading, COVER_SUB_CAP)
    name = content["client"]["name"]
    label_color, color = colors
    cv.text(x, y, w, lh(t.caption), CLIENT_LABEL, t.caption, label_color, name=CHROME)
    h = m.lines(name, w, size, True) * lh(size)
    cv.text(x, y + lh(t.caption), w, h, name, size, color, bold=True, name=CHROME)


def _meta_line(
    cv: Canvas, content: dict, ctx: Ctx, x: float, bottom: float, w: float, color: str
) -> float:
    """generated date · manager, one quiet line ending at ``bottom``; returns its top."""
    t, m = ctx.theme, ctx.m
    line = cover_meta_line(content)
    h = m.lines(line, w, t.caption) * lh(t.caption)
    cv.text(x, bottom - h, w, h, line, t.caption, color, name=CHROME)
    return bottom - h


def _cover(
    prs: Any, content: dict, style: dict, ctx: Ctx, base_dir: Path, warnings: list[str]
) -> None:
    """Cover: LOGO / org name, title, subtitle, client name and one meta line. Nothing else
    (client facts, period and data basis are in the 「客户信息」 band on the first body page)."""
    t = ctx.theme
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    cv = Canvas(slide, t)
    variant = style["cover.variant"]
    pos = (style["brand.logo_position"], style["brand.org_position"])
    bottom_brand = any(p.startswith("bottom") for p in pos)
    top_brand = any(not p.startswith("bottom") for p in pos)
    foot = SLIDE_H - 40 - (BRAND_BAND_H if bottom_brand else 0)
    top = TITLE_TOP_BRAND if top_brand else TITLE_TOP
    if variant == "split":
        panel_w = SLIDE_W * 0.42
        _bg(slide, t.background)
        cv.rect(0, 0, panel_w, SLIDE_H, t.primary)
        colors = (on_color(t.primary), t.on_primary_soft)
        _place_cover_text(cv, content, ctx, 40, panel_w - 80, (TITLE_TOP, SLIDE_H - 40), colors, 4)
        rx, rw = panel_w + 40, SLIDE_W - panel_w - 80
        _place_brand(
            cv, content, style, ctx, base_dir, t.ink, (rx, 32, SLIDE_W - 40, SLIDE_H - 24), warnings
        )
        meta_top = _meta_line(cv, content, ctx, rx, foot, rw, t.muted)
        who_y = meta_top - t.gap_l - _who_height(content, ctx, rw)
        _who(cv, content, ctx, rx, who_y, rw, (t.muted, t.ink))
        return
    x = 56.0
    minimal = variant == "minimal"
    w = 620.0 if minimal else 540.0
    fg = t.ink if minimal else on_color(t.primary)
    soft = t.muted if minimal else t.on_primary_soft
    meta_top = foot - lh(t.caption) * ctx.m.lines(cover_meta_line(content), w, t.caption)
    who_h = _who_height(content, ctx, w)
    text_bottom = meta_top - t.gap_l - who_h - t.gap_l
    if minimal:
        _bg(slide, t.background)
        cv.rect(0, 0, SLIDE_W, 6, t.primary)
    else:
        _bg(slide, t.primary)
        ring = mix(t.primary, fg, 0.25)
        for r in (165.0, 125.0, 85.0):  # quiet rings; no data-like shapes on the cover
            cv.oval(790 - r, 270 - r, 2 * r, 2 * r, None, line=ring)
    rule = t.gap_m + 4 if minimal else 0.0
    y = _place_cover_text(cv, content, ctx, x, w, (top, text_bottom), (fg, soft), 3, rule)
    if minimal:
        cv.rect(x, y + t.gap_m, 64, 4, t.accent)
    brand_color = t.primary if minimal else fg
    _place_brand(
        cv,
        content,
        style,
        ctx,
        base_dir,
        brand_color,
        (x, 32, SLIDE_W - x, SLIDE_H - 24),
        warnings,
        plate=not minimal,
    )
    _who(cv, content, ctx, x, meta_top - t.gap_l - who_h, w, (soft, fg))
    _meta_line(cv, content, ctx, x, foot, w, soft)


# ---------- client-info band ----------


def band_section(content: dict) -> list[tuple[dict, list[tuple[Prim, str]]]]:
    """The 「客户信息」 band as a leading pseudo-section (first body page, after the TOC);
    empty when the caller gave no facts, period or data basis."""
    items = client_band_items(content)
    if not items:
        return []
    sec = {"id": BAND_ID, "title": CLIENT_BAND_TITLE, "blocks": [{"kind": "profile"}]}
    return [(sec, [(InfoBand(tuple(items)), "client.facts")])]


def draw_band(cv: Canvas, x: float, y: float, w: float, band: InfoBand, ctx: Ctx) -> None:
    t = ctx.theme
    geo = band_geometry(band, w, ctx)
    cv.rect(x, y, w, geo.height, t.pale, rounded=True)
    rows = [band.items[i : i + geo.cols] for i in range(0, len(band.items), geo.cols)]
    cy = y + geo.pad
    lift = max(0.0, lh(t.body) - lh(t.caption)) * 0.6  # label on the value's baseline
    for row, rh in zip(rows, geo.row_h, strict=True):
        cx = x + geo.pad
        for (label, value), cw, lab in zip(row, geo.col_w, geo.label_w, strict=False):
            role = CHROME if label in BAND_CHROME_LABELS else BODY
            need = ctx.m.lines(label, lab, t.caption) * lh(t.caption)
            down = min(lift, rh - need)  # a wrapped (long) label starts at the row top
            cv.text(cx, cy + down, lab, rh - down, label, t.caption, t.muted, name=role)
            vx, vw = cx + lab + geo.inner, cw - lab - geo.inner
            cv.text(vx, cy, vw, rh, value, t.body, t.ink, bold=True)
            cx += cw + geo.gap
        cy += rh + t.gap_s


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
            t.on_primary_soft,
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
            t.on_primary_soft,
            align="center",
            name=CHROME,
        )
