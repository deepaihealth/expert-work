"""PPT layout engine in points: measure primitives, split the splittable ones, paginate
sections onto 16:9 slides (merge short sections, continue long ones under the same title)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from hpr.common import column_widths
from hpr.icons import icon_for_section
from hpr.images import IMAGE_ERRORS, strict_images
from hpr.measure import LINE, SAFETY, Measurer
from hpr.prims import (
    Bullets,
    Callout,
    Card,
    CardGrid,
    Chart,
    Column,
    Columns,
    Image,
    KeyValue,
    Media,
    Paragraph,
    Prim,
    Step,
    SubHeading,
    Table,
    TimeBars,
    Timeline,
)
from hpr.theme import Theme

SLIDE_W, SLIDE_H = 960.0, 540.0
MARGIN_X = 40.0
BODY_TOP = 80.0
BODY_W = SLIDE_W - 2 * MARGIN_X
FOOTER_BOTTOM = 526.0
NEXT_SECTION_MIN_ROOM = 0.35  # start the next section in-page only if >= 35% of the body is left
CHART_H = 200.0
DONUT_H = 180.0
BAR_H = 10.0
IMAGE_MAX_H = 260.0
STRIPE = 3.0
MAX_DISCLAIMER_LINES = 4


class LayoutError(Exception):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


@dataclass(frozen=True)
class Ctx:
    theme: Theme
    m: Measurer
    base_dir: Path
    body_bottom: float


def lh(size: float) -> float:
    return size * LINE


def brand_line(brand: dict) -> str:
    return "  ｜  ".join(v for v in (brand.get("org_name"), brand.get("footer_signature")) if v)  # noqa: RUF001


def make_ctx(content: dict, theme: Theme, m: Measurer, base_dir: Path) -> Ctx:
    brand = content.get("brand") or {}
    t = theme
    h = 0.0
    line1 = brand_line(brand)
    if line1:
        h += m.lines(line1, BODY_W, t.caption) * lh(t.caption)
    if brand.get("disclaimer"):
        n = m.lines(brand["disclaimer"], BODY_W, t.caption)
        if n > MAX_DISCLAIMER_LINES:
            raise LayoutError(
                "brand.disclaimer",
                f"免责声明过长（{n} 行），页脚最多 {MAX_DISCLAIMER_LINES} 行",  # noqa: RUF001
            )
        h += n * lh(t.caption)
    body_bottom = FOOTER_BOTTOM - h - t.gap_m - t.gap_s
    return Ctx(theme, m, base_dir, body_bottom)


# ---------- cards ----------


@dataclass(frozen=True)
class Part:
    kind: str  # title | value | line | tag | bar
    text: str
    size: float
    bold: bool
    height: float


def card_parts(card: Card, inner: float, ctx: Ctx) -> list[Part]:
    t, m = ctx.theme, ctx.m
    parts: list[Part] = []
    title = f"{card.badge}  {card.title}".strip() if card.badge else card.title
    if title:
        parts.append(
            Part(
                "title",
                title,
                t.caption,
                True,
                m.lines(title, inner, t.caption, True) * lh(t.caption),
            )
        )
    if card.value or card.unit:
        text = f"{card.value} {card.unit}".strip()
        parts.append(
            Part("value", text, t.title, True, m.lines(text, inner, t.title, True) * lh(t.title))
        )
    for ln in card.lines:
        parts.append(Part("line", ln, t.small, False, m.lines(ln, inner, t.small) * lh(t.small)))
    if card.tag:
        parts.append(
            Part(
                "tag",
                card.tag.text,
                t.small,
                True,
                m.lines(card.tag.text, inner, t.small, True) * lh(t.small),
            )
        )
    if card.bar:
        parts.append(Part("bar", "", 0, False, BAR_H))
    return parts


def card_height(card: Card, w: float, ctx: Ctx) -> float:
    t = ctx.theme
    parts = card_parts(card, w - 2 * t.gap_m, ctx)
    return STRIPE + 2 * t.gap_m + sum(p.height for p in parts) + t.gap_xs * max(0, len(parts) - 1)


def grid_rows(grid: CardGrid) -> list[tuple[Card, ...]]:
    return [grid.cards[i : i + grid.cols] for i in range(0, len(grid.cards), grid.cols)]


def grid_col_w(grid: CardGrid, w: float, ctx: Ctx) -> float:
    return (w - (grid.cols - 1) * ctx.theme.gap_m) / grid.cols


def _grid_row_heights(grid: CardGrid, w: float, ctx: Ctx) -> list[float]:
    cw = grid_col_w(grid, w, ctx)
    return [max(card_height(c, cw, ctx) for c in row) for row in grid_rows(grid)]


# ---------- tables ----------


@dataclass(frozen=True)
class TableGeo:
    col_w: list[float]
    pad_x: float
    pad_y: float
    header_h: float
    row_h: list[float]


def table_geometry(tbl: Table, w: float, ctx: Ctx) -> TableGeo:
    t, m = ctx.theme, ctx.m

    def bold_cell(i: int, value: str) -> bool:
        return tbl.highlight_col == i and value not in ("", "—")

    pad_x, pad_y = t.gap_s, t.gap_xs + 2

    def need(_i: int, text: str) -> float:  # measured bold: headers and highlights are bold
        return m.width(text, t.small, True) * SAFETY + 2 * pad_x + 1

    col_w = column_widths(tbl.columns, tbl.rows, w, need)

    def row_height(cells: tuple[str, ...], size: float, header: bool) -> float:
        return (
            max(
                m.lines(c, cw - 2 * pad_x, size, header or bold_cell(i, c))
                for i, (c, cw) in enumerate(zip(cells, col_w, strict=True))
            )
            * lh(size)
            + 2 * pad_y
        )

    header_h = row_height(tbl.columns, t.small, True)
    row_h = [row_height(r, t.small, False) for r in tbl.rows]
    return TableGeo(col_w, pad_x, pad_y, header_h, row_h)


# ---------- key-value ----------


def kv_rows(kv: KeyValue) -> list[tuple[tuple[str, str], ...]]:
    return [kv.pairs[i : i + kv.cols] for i in range(0, len(kv.pairs), kv.cols)]


def kv_geometry(kv: KeyValue, w: float, ctx: Ctx) -> tuple[float, float, list[float]]:
    t, m = ctx.theme, ctx.m
    pair_w = (w - (kv.cols - 1) * t.gap_l) / kv.cols
    label_w = pair_w * 0.34
    value_w = pair_w - label_w - t.gap_s
    heights = []
    for row in kv_rows(kv):
        heights.append(
            max(
                max(
                    m.lines(k, label_w, t.small) * lh(t.small),
                    m.lines(v, value_w, t.body) * lh(t.body),
                )
                for k, v in row
            )
            + t.gap_s
        )
    return pair_w, label_w, heights


# ---------- timeline / columns ----------


def timeline_rows(tl: Timeline) -> list[tuple[Step, ...]]:
    per = 5
    return [tl.steps[i : i + per] for i in range(0, len(tl.steps), per)]


def _step_w(row_len: int, w: float, ctx: Ctx) -> float:
    return (w - (row_len - 1) * ctx.theme.gap_m) / row_len


def step_height(step: Step, step_w: float, ctx: Ctx) -> float:
    t, m = ctx.theme, ctx.m
    inner = step_w - 2 * t.gap_s
    label = m.lines(step.label, step_w, t.body, True) * lh(t.body)
    body = sum(m.lines(ln, inner, t.small) * lh(t.small) for ln in step.lines)
    box = (2 * t.gap_s + body) if step.lines else 0.0
    return 12 + t.gap_s + label + t.gap_xs + box


def _timeline_row_heights(tl: Timeline, w: float, ctx: Ctx) -> list[float]:
    return [
        max(step_height(s, _step_w(len(row), w, ctx), ctx) for s in row)
        for row in timeline_rows(tl)
    ]


def columns_rows(cols: Columns) -> list[tuple[Column, ...]]:
    per = len(cols.columns) if len(cols.columns) <= 4 else 3
    return [cols.columns[i : i + per] for i in range(0, len(cols.columns), per)]


def column_height(col: Column, col_w: float, ctx: Ctx) -> float:
    t, m = ctx.theme, ctx.m
    inner = col_w - 2 * t.gap_m
    items = sum(m.lines(it, inner - t.small, t.small) * lh(t.small) for it in col.items)
    return (
        STRIPE
        + 2 * t.gap_m
        + column_title_height(col, col_w, ctx)
        + t.gap_s
        + items
        + t.gap_xs * max(0, len(col.items) - 1)
    )


def column_title_height(col: Column, col_w: float, ctx: Ctx) -> float:
    t = ctx.theme
    return ctx.m.lines(col.title, col_w - 2 * t.gap_m, t.heading, True) * lh(t.heading)


# ---------- callout / media / donut ----------


def callout_title_height(co: Callout, w: float, ctx: Ctx) -> float:
    """Height of the title slot (0 without title); ``w`` is the callout's inner text width."""
    t = ctx.theme
    return ctx.m.lines(co.title, w, t.body, True) * lh(t.body) if co.title else 0.0


@dataclass(frozen=True)
class MediaGeo:
    text_w: float
    name_h: float
    box_w: float
    link_text: str
    link_h: float


def media_geometry(md: Media, w: float, ctx: Ctx) -> MediaGeo:
    t, m = ctx.theme, ctx.m
    text_w = w * 0.62 - 2 * t.gap_m
    box_w = w * 0.38 - t.gap_m
    label = f"查看示范/产品详情：{md.name}"  # noqa: RUF001
    return MediaGeo(
        text_w,
        m.lines(md.name, text_w, t.heading, True) * lh(t.heading),
        box_w,
        label,
        m.lines(label, box_w - 2 * t.gap_s, t.body, True) * lh(t.body),
    )


def donut_legend_heights(ch: Chart, w: float, ctx: Ctx) -> list[float]:
    """Heights of the legend lines next to a DONUT_H donut in a primitive of width ``w``."""
    t = ctx.theme
    lw = w - DONUT_H - t.gap_l - 18
    return [ctx.m.lines(line, lw, t.body) * lh(t.body) for line in ch.legend]


# ---------- image ----------


def image_size(img: Image, w: float, ctx: Ctx) -> tuple[float, float]:
    from PIL import Image as PILImage

    path = Path(img.path)
    path = path if path.is_absolute() else ctx.base_dir / path
    try:
        with strict_images(), PILImage.open(path) as im:
            iw, ih = im.size
    except IMAGE_ERRORS as exc:
        raise LayoutError(img.path, f"找不到图片文件或无法读取：{exc}") from exc  # noqa: RUF001
    box_w = min(w, 520.0)
    h = min(IMAGE_MAX_H, box_w * ih / iw)
    return h * iw / ih, h


# ---------- measure ----------


def measure(prim: Prim, w: float, ctx: Ctx) -> float:
    t, m = ctx.theme, ctx.m
    if isinstance(prim, SubHeading):
        return m.lines(prim.text, w, t.heading, True) * lh(t.heading) + t.gap_xs
    if isinstance(prim, Paragraph):
        pad = t.gap_m if prim.boxed else 0.0
        return m.lines(prim.text, w - 2 * pad, t.body) * lh(t.body) + 2 * pad
    if isinstance(prim, Bullets):
        indent = t.body * 1.4
        return sum(m.lines(it, w - indent, t.body) * lh(t.body) for it in prim.items) + t.gap_xs * (
            len(prim.items) - 1
        )
    if isinstance(prim, CardGrid):
        rows = _grid_row_heights(prim, w, ctx)
        return sum(rows) + t.gap_m * (len(rows) - 1)
    if isinstance(prim, Table):
        geo = table_geometry(prim, w, ctx)
        return geo.header_h + sum(geo.row_h)
    if isinstance(prim, KeyValue):
        return sum(kv_geometry(prim, w, ctx)[2])
    if isinstance(prim, Callout):
        inner = w - 2 * t.gap_m - 4
        title = (callout_title_height(prim, inner, ctx) + t.gap_xs) if prim.title else 0.0
        return 2 * t.gap_m + title + m.lines(prim.text, inner, t.body) * lh(t.body)
    if isinstance(prim, Chart):
        if prim.kind == "donut":
            legend = donut_legend_heights(prim, w, ctx)
            return max(DONUT_H, sum(legend) + t.gap_xs * len(legend))
        return CHART_H
    if isinstance(prim, Timeline):
        rows = _timeline_row_heights(prim, w, ctx)
        return sum(rows) + t.gap_l * (len(rows) - 1)
    if isinstance(prim, Columns):
        heights = []
        for row in columns_rows(prim):
            cw = (w - (len(row) - 1) * t.gap_m) / len(row)
            heights.append(max(column_height(c, cw, ctx) for c in row))
        return sum(heights) + t.gap_m * (len(heights) - 1)
    if isinstance(prim, Media):
        geo = media_geometry(prim, w, ctx)
        body = geo.name_h + t.gap_xs + m.lines(prim.description, geo.text_w, t.body) * lh(t.body)
        link_box = geo.link_h + 2 * t.gap_s
        return max(96.0, body + 2 * t.gap_m, link_box + 2 * t.gap_m)
    if isinstance(prim, Image):
        _, h = image_size(prim, w, ctx)
        cap = (
            (t.gap_xs + m.lines(prim.caption, w, t.caption) * lh(t.caption))
            if prim.caption
            else 0.0
        )
        return h + cap
    if isinstance(prim, TimeBars):
        return len(prim.rows) * (lh(t.body) + t.gap_s) + lh(t.caption) + t.gap_s
    raise TypeError(f"unknown primitive {type(prim).__name__}")


# ---------- split ----------


def _fit_count(heights: list[float], gap: float, avail: float) -> int:
    used, n = 0.0, 0
    for i, h in enumerate(heights):
        need = h + (gap if i else 0.0)
        if used + need > avail:
            break
        used += need
        n += 1
    return n


MIN_TABLE_ROWS = 2  # a split table keeps at least this many rows on each side
MIN_PARA_LINES = 3  # a split paragraph keeps at least this many lines on each side


def keep_apart(k: int, n: int, least: int, force: bool) -> int:
    """How many of ``n`` units to put before a break when ``k`` fit: both sides keep at least
    ``least`` units, else 0 (the element moves whole). ``force`` (the element starts a page and
    still does not fit) relaxes only what cannot be honoured."""
    if k >= n:
        return n
    if k < 1:
        return 0
    kept = min(k, n - least)
    if kept >= least:
        return kept
    if not force:
        return 0
    return kept if kept >= 1 else k


def split(
    prim: Prim, w: float, avail: float, ctx: Ctx, *, force: bool = False
) -> tuple[Prim | None, Prim | None]:
    """Split so that head fits in ``avail``. (None, prim) when nothing fits, or when a split
    would leave a stub (see keep_apart); ``force`` accepts a stub rather than no split."""
    t, m = ctx.theme, ctx.m
    if isinstance(prim, Paragraph):
        pad = t.gap_m if prim.boxed else 0.0
        fit = int((avail - 2 * pad) // lh(t.body))
        total = m.lines(prim.text, w - 2 * pad, t.body)
        n = keep_apart(fit, total, MIN_PARA_LINES, force) if fit >= 1 else 0
        if n < 1:
            return None, prim
        head, tail = m.split_text(prim.text, w - 2 * pad, t.body, n)
        return replace(prim, text=head), (replace(prim, text=tail) if tail else None)
    if isinstance(prim, Bullets):
        indent = t.body * 1.4
        hs = [m.lines(it, w - indent, t.body) * lh(t.body) for it in prim.items]
        k = _fit_count(hs, t.gap_xs, avail)
        return _cut(prim, "items", k)
    if isinstance(prim, CardGrid):
        rows = grid_rows(prim)
        k = _fit_count(_grid_row_heights(prim, w, ctx), t.gap_m, avail)
        if k == 0:
            return None, prim
        head = replace(prim, cards=tuple(c for row in rows[:k] for c in row))
        rest = tuple(c for row in rows[k:] for c in row)
        return head, (replace(prim, cards=rest) if rest else None)
    if isinstance(prim, Table):
        geo = table_geometry(prim, w, ctx)
        k = _fit_count(geo.row_h, 0.0, avail - geo.header_h)
        k = keep_apart(k, len(prim.rows), MIN_TABLE_ROWS, force) if k else 0
        return _cut(prim, "rows", k)
    if isinstance(prim, KeyValue):
        rows = kv_rows(prim)
        k = _fit_count(kv_geometry(prim, w, ctx)[2], 0.0, avail)
        if k == 0:
            return None, prim
        head = replace(prim, pairs=tuple(p for row in rows[:k] for p in row))
        rest = tuple(p for row in rows[k:] for p in row)
        return head, (replace(prim, pairs=rest) if rest else None)
    if isinstance(prim, Timeline):
        rows = timeline_rows(prim)
        k = _fit_count(_timeline_row_heights(prim, w, ctx), t.gap_l, avail)
        if k == 0:
            return None, prim
        head = replace(prim, steps=tuple(s for row in rows[:k] for s in row))
        rest = tuple(s for row in rows[k:] for s in row)
        return head, (replace(prim, steps=rest) if rest else None)
    return (prim, None) if measure(prim, w, ctx) <= avail else (None, prim)


def _cut(prim: Prim, attr: str, k: int) -> tuple[Prim | None, Prim | None]:
    seq = getattr(prim, attr)
    if k == 0:
        return None, prim
    head = replace(prim, **{attr: seq[:k]})
    tail = replace(prim, **{attr: seq[k:]}) if k < len(seq) else None
    return head, tail


def splittable(prim: Prim) -> bool:
    return isinstance(prim, (Paragraph, Bullets, CardGrid, Table, KeyValue, Timeline))


# ---------- paginate ----------


@dataclass
class Placed:
    prim: Prim
    x: float
    y: float
    w: float
    h: float
    path: str


@dataclass
class Page:
    title: str
    icon: str
    section_id: str
    continued: bool
    placed: list[Placed] = field(default_factory=list)


def paginate(sections: list[tuple[dict, list[tuple[Prim, str]]]], ctx: Ctx) -> list[Page]:
    t = ctx.theme
    bottom = ctx.body_bottom
    avail_total = bottom - BODY_TOP
    pages: list[Page] = []
    cur: Page | None = None
    y = BODY_TOP

    def new_page(sec: dict, continued: bool) -> Page:
        pg = Page(
            sec["title"],
            icon_for_section(sec),
            sec["id"],
            continued,
        )
        pages.append(pg)
        return pg

    def min_head(prim: Prim) -> float:
        """The least of ``prim`` that may end a page (keep_apart's minimum head)."""
        full = measure(prim, BODY_W, ctx)
        if isinstance(prim, Table):
            geo = table_geometry(prim, BODY_W, ctx)
            return min(full, geo.header_h + sum(geo.row_h[:MIN_TABLE_ROWS]))
        if isinstance(prim, Paragraph):
            pad = 2 * t.gap_m if prim.boxed else 0.0
            return min(full, MIN_PARA_LINES * lh(t.body) + pad)
        return min(full, 2 * lh(t.body) + 2 * t.gap_m) if splittable(prim) else full

    def captioned(i: int, items: list[tuple[Prim, str]]) -> bool:
        """items[i] is a chart followed by its own caption paragraph (same block)."""
        return (
            isinstance(items[i][0], Chart)
            and i + 1 < len(items)
            and isinstance(items[i + 1][0], Paragraph)
            and items[i + 1][1] == items[i][1]
        )

    def lead_need(i: int, items: list[tuple[Prim, str]]) -> float:
        """Minimum room item i needs at the bottom of a page, including a caption it keeps."""
        need = min_head(items[i][0])
        if captioned(i, items):
            need += t.gap_l + min_head(items[i + 1][0])
        return need

    def head_fits(prim: Prim, room: float) -> bool:
        if min_head(prim) > room:
            return False
        if measure(prim, BODY_W, ctx) <= room:
            return True
        return splittable(prim) and split(prim, BODY_W, room, ctx)[0] is not None

    def lead_fits(items: list[tuple[Prim, str]], room: float) -> bool:
        """The section's first item (a sub-heading together with what follows it) fits in room."""
        if not items:
            return room >= 0
        first = items[0][0]
        if isinstance(first, SubHeading) and len(items) > 1:
            rest = room - measure(first, BODY_W, ctx) - t.gap_l
            return head_fits(items[1][0], rest)
        return head_fits(first, room)

    for si, (sec, items) in enumerate(sections):
        head = SubHeading(sec["title"])
        head_h = measure(head, BODY_W, ctx)
        if (
            cur is not None
            and cur.placed
            and bottom - y >= NEXT_SECTION_MIN_ROOM * avail_total
            and lead_fits(items, bottom - y - t.gap_l - head_h)
        ):
            y += t.gap_l
            cur.placed.append(Placed(head, MARGIN_X, y, BODY_W, head_h, f"sections[{si}]"))
            y += head_h
        else:
            cur = new_page(sec, continued=False)
            y = BODY_TOP
        for idx, (prim, path) in enumerate(items):
            if idx:
                y += t.gap_l
            pending: Prim | None = prim
            while pending is not None:
                h = measure(pending, BODY_W, ctx)
                need = h
                if isinstance(pending, SubHeading) and idx + 1 < len(items):
                    need = h + t.gap_l + lead_need(idx + 1, items)  # keep heading with what follows
                elif pending is prim and captioned(idx, items):
                    need = h + t.gap_l + min_head(items[idx + 1][0])  # keep chart with caption
                at_top = y <= BODY_TOP + 0.01
                if y + need <= bottom or (at_top and h <= avail_total):
                    cur.placed.append(Placed(pending, MARGIN_X, y, BODY_W, h, path))
                    y += h
                    pending = None
                    continue
                if splittable(pending):
                    head_part, tail = split(pending, BODY_W, bottom - y, ctx)
                    if head_part is None and at_top:
                        head_part, tail = split(pending, BODY_W, bottom - y, ctx, force=True)
                    if head_part is None and at_top:
                        raise LayoutError(path, "单个条目超过一页，请把内容拆小")  # noqa: RUF001
                    if head_part is not None:
                        hh = measure(head_part, BODY_W, ctx)
                        cur.placed.append(Placed(head_part, MARGIN_X, y, BODY_W, hh, path))
                    pending = tail
                elif at_top:
                    raise LayoutError(path, "内容超过一页且无法拆分，请把内容拆小")  # noqa: RUF001
                if pending is not None:
                    cur = new_page(sec, continued=True)
                    y = BODY_TOP
    return pages
