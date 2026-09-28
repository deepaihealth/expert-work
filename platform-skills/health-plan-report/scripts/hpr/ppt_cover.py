"""PPT cover, table of contents and end page, and brand (LOGO / organisation name) placement."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hpr.common import cover_meta_items
from hpr.logo import trimmed_logo
from hpr.ppt_canvas import CHROME, DECO, Canvas, _bg, _footer
from hpr.ppt_layout import (
    BODY_TOP,
    BODY_W,
    FOOTER_BOTTOM,
    MARGIN_X,
    SLIDE_H,
    SLIDE_W,
    Ctx,
    LayoutError,
    lh,
)
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
    items = cover_meta_items(content)
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
            (fg, t.on_primary_soft),
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
    _cover_title(cv, content, ctx, 56, 150, 520, (fg, t.on_primary_soft), 3, (limit, _META_BELOW))
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
    _meta_grid(cv, geo, ctx, 56, meta_bottom, meta_w, (t.on_primary_soft, fg, soft))


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
