"""PPT layout engine in points: measure primitives, split the splittable ones, paginate
sections onto 16:9 slides (merge short sections, continue long ones with「（续）」)."""  # noqa: RUF002

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from hpr.icons import icon_for_section
from hpr.measure import LINE, Measurer
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
MERGE_FILL = 0.85
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


def make_ctx(content: dict, theme: Theme, m: Measurer, base_dir: Path) -> Ctx:
    brand = content.get("brand") or {}
    t = theme
    h = 0.0
    if brand.get("org_name") or brand.get("footer_signature"):
        h += lh(t.caption)
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
    n = len(tbl.columns)
    longest = [
        max([m.width(tbl.columns[i], t.small, True)] + [m.width(r[i], t.small) for r in tbl.rows])
        for i in range(n)
    ]
    total = sum(longest) or 1.0
    floor_w = w * 0.12
    raw = [max(floor_w, w * x / total) for x in longest]
    scale = w / sum(raw)
    col_w = [x * scale for x in raw]
    pad_x, pad_y = t.gap_s, t.gap_xs + 2

    def row_height(cells: tuple[str, ...], size: float, bold: bool) -> float:
        return (
            max(m.lines(c, cw - 2 * pad_x, size, bold) for c, cw in zip(cells, col_w, strict=True))
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
        + lh(t.heading)
        + t.gap_s
        + items
        + t.gap_xs * max(0, len(col.items) - 1)
    )


# ---------- image ----------


def image_size(img: Image, w: float, ctx: Ctx) -> tuple[float, float]:
    from PIL import Image as PILImage

    path = Path(img.path)
    path = path if path.is_absolute() else ctx.base_dir / path
    try:
        with PILImage.open(path) as im:
            iw, ih = im.size
    except (OSError, ValueError) as exc:
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
        title = (lh(t.body) + t.gap_xs) if prim.title else 0.0
        return 2 * t.gap_m + title + m.lines(prim.text, inner, t.body) * lh(t.body)
    if isinstance(prim, Chart):
        if prim.kind == "donut":
            return max(DONUT_H, len(prim.legend) * (lh(t.body) + t.gap_xs))
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
        text_w = w * 0.62 - 2 * t.gap_m
        body = lh(t.heading) + t.gap_xs + m.lines(prim.description, text_w, t.body) * lh(t.body)
        return max(96.0, body + 2 * t.gap_m)
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


def split(prim: Prim, w: float, avail: float, ctx: Ctx) -> tuple[Prim | None, Prim | None]:
    """Split so that head fits in ``avail``. (None, prim) when nothing fits."""
    t, m = ctx.theme, ctx.m
    if isinstance(prim, Paragraph):
        pad = t.gap_m if prim.boxed else 0.0
        n = int((avail - 2 * pad) // lh(t.body))
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
            sec["title"] + ("（续）" if continued else ""),  # noqa: RUF001
            icon_for_section(sec),
            sec["id"],
            continued,
        )
        pages.append(pg)
        return pg

    def min_head(prim: Prim) -> float:
        full = measure(prim, BODY_W, ctx)
        return min(full, 2 * lh(t.body) + 2 * t.gap_m) if splittable(prim) else full

    for si, (sec, items) in enumerate(sections):
        total = sum(measure(p, BODY_W, ctx) for p, _ in items) + t.gap_l * max(0, len(items) - 1)
        head = SubHeading(sec["title"])
        head_h = measure(head, BODY_W, ctx)
        if (
            cur is not None
            and cur.placed
            and (y - BODY_TOP) + t.gap_l + head_h + total <= MERGE_FILL * avail_total
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
                    need = (
                        h + t.gap_l + min_head(items[idx + 1][0])
                    )  # keep heading with what follows
                at_top = y <= BODY_TOP + 0.01
                if y + need <= bottom or (at_top and h <= avail_total):
                    cur.placed.append(Placed(pending, MARGIN_X, y, BODY_W, h, path))
                    y += h
                    pending = None
                    continue
                if splittable(pending):
                    head_part, tail = split(pending, BODY_W, bottom - y, ctx)
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
