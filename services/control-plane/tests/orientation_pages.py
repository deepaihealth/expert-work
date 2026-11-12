"""B-301 PR 4 —— synthetic scanned pages for the orientation tests (no tenant content, no photos).

Every picture is drawn here, deterministically from a seed: vector content on a PyMuPDF
page in PyMuPDF's built-in fonts (so it draws the same on any machine — CI has no CJK
system fonts), rasterised at :data:`SCAN_DPI` like a scanner, sometimes given a scanner's
skew, noise and JPEG. "Photos" are generated colour fields, never real photographs.

:func:`scanned_pdf` places each picture on a page of its own with no text layer —
a page the parser decides "needs recognition". A turned page is the upright picture
turned clockwise (:func:`turned`) before it is placed; the page takes its shape.
"""

from __future__ import annotations

import io
import math
import random
from collections.abc import Callable, Sequence

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFilter

#: Resolution the pictures are "scanned" at.
SCAN_DPI = 100
_A4P = (595.0, 842.0)
_A4L = (842.0, 595.0)
_A3L = (1191.0, 842.0)
_SLIDE = (960.0, 540.0)

_ZH = (
    "本制度适用于公司全体员工及外包服务人员。各部门应当在每季度末完成内部检查并提交书面报告。"
    "对于检查中发现的问题,责任部门须在十五个工作日内完成整改。设备维护记录应保存不少于三年。"
    "申请人提交材料后,审核人员应在五个工作日内给出意见。产品使用前请仔细阅读本说明书。"
    "本仪器应放置在干燥通风处,避免阳光直射。如遇异常情况,请立即切断电源并联系售后服务中心。"
    "会议决定成立专项工作小组,负责推进年度重点项目的落实。各单位要高度重视安全生产工作。"
    "第一条为规范管理流程,提高工作效率,特制定本办法。第二条本办法自发布之日起施行。"
    "检验结果仅对来样负责,复印件未加盖检验专用章无效。合同双方应本着诚信原则履行各自义务。"
)
_EN = (
    "This agreement is made between the parties named below and takes effect on signature",
    "The supplier shall deliver the goods within thirty days of receiving the purchase order",
    "All measurements were taken at room temperature unless otherwise stated in the table",
    "Please read these instructions carefully before installing the device",
    "The committee reviewed the annual budget and approved the revised spending plan",
    "Results are reported as the mean of three independent experiments",
    "Maintenance records must be kept for at least three years and made available on request",
    "In the event of a dispute both parties agree to seek an amicable settlement first",
)
_TITLES_ZH = (
    "年度工作报告",
    "产品使用说明书",
    "项目建设方案",
    "质量管理手册",
    "培训课程",
    "季度经营分析",
)
_TITLES_EN = (
    "Annual Report",
    "User Manual",
    "Project Proposal",
    "Quality Handbook",
    "Field Survey",
)
_FORM_ZH = (
    "姓名",
    "性别",
    "出生日期",
    "联系电话",
    "工作单位",
    "地址",
    "申请事项",
    "审核意见",
    "日期",
)
_FORM_EN = ("Name", "Gender", "Date of birth", "Phone", "Employer", "Address", "Request", "Date")

_FONTS: dict[str, pymupdf.Font] = {}


def _font(name: str) -> pymupdf.Font:
    if name not in _FONTS:
        _FONTS[name] = pymupdf.Font(name)
    return _FONTS[name]


def _latin(r: random.Random) -> pymupdf.Font:
    return _font(r.choice(("helv", "tiro", "cour", "hebo", "tibo")))


class _Canvas:
    """A vector page being drawn, rasterised by :meth:`picture`."""

    def __init__(self, size: tuple[float, float], paper: tuple[float, ...]) -> None:
        self.doc = pymupdf.open()
        self.page = self.doc.new_page(width=size[0], height=size[1])
        self.page.draw_rect(self.page.rect, color=None, fill=paper)
        self.width, self.height = size

    def text(
        self,
        at: tuple[float, float],
        text: str,
        font: pymupdf.Font,
        size: float,
        color: tuple[float, float, float] = (0.08, 0.08, 0.08),
    ) -> None:
        writer = pymupdf.TextWriter(self.page.rect, color=color)
        writer.append(at, text, font=font, fontsize=size)
        writer.write_text(self.page)

    def lines(
        self,
        x: float,
        y: float,
        width: float,
        bottom: float,
        text: str,
        font: pymupdf.Font,
        size: float,
        leading: float,
        *,
        cjk: bool,
    ) -> float:
        """``text`` wrapped to ``width`` from ``y`` down to ``bottom``; the next free y."""
        for line in _wrap(text, font, size, width, cjk=cjk):
            if y > bottom:
                break
            self.text((x, y), line, font, size)
            y += size * leading
        return y

    def image(self, rect: pymupdf.Rect, picture: Image.Image) -> None:
        out = io.BytesIO()
        picture.save(out, format="PNG")
        self.page.insert_image(rect, stream=out.getvalue(), keep_proportion=False)

    def picture(self) -> Image.Image:
        pixmap = self.page.get_pixmap(dpi=SCAN_DPI, alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        self.doc.close()
        return image


def _wrap(text: str, font: pymupdf.Font, size: float, width: float, *, cjk: bool) -> list[str]:
    lines: list[str] = []
    current = ""
    for token in list(text) if cjk else text.split(" "):
        candidate = current + token if cjk else (f"{current} {token}" if current else token)
        if font.text_length(candidate, fontsize=size) > width and current:
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _paper(r: random.Random) -> tuple[float, float, float]:
    shade = 1 - r.randint(0, 14) / 255
    return (shade, shade, max(0.0, shade - r.randint(0, 8) / 255))


def _zh(r: random.Random, n: int) -> str:
    start = r.randrange(0, len(_ZH) - 1)
    return (_ZH * 4)[start : start + n]


def _en(r: random.Random, sentences: int) -> str:
    return ". ".join(r.choice(_EN) for _ in range(sentences)) + "."


# ---------------------------------------------------------------- documents


def _doc_zh(r: random.Random) -> Image.Image:
    c = _Canvas(_A4P, _paper(r))
    margin = r.randint(54, 90)
    cjk = _font("cjk")
    title = r.choice(_TITLES_ZH)
    size = r.randint(22, 30)
    c.text(((c.width - cjk.text_length(title, fontsize=size)) / 2, margin + size), title, cjk, size)
    body = r.randint(11, 15)
    y = margin + 80.0
    while y < c.height - margin - 30:
        y = c.lines(
            margin,
            y,
            c.width - 2 * margin,
            c.height - margin - 30,
            "  " + _zh(r, r.randint(60, 160)),
            cjk,
            body,
            r.uniform(1.5, 1.9),
            cjk=True,
        )
        y += body
    return c.picture()


def _doc_en(r: random.Random) -> Image.Image:
    c = _Canvas(_A4P, _paper(r))
    margin = r.randint(54, 90)
    font = _latin(r)
    c.text((margin, margin + 24), r.choice(_TITLES_EN), _font("hebo"), r.randint(20, 28))
    body = r.randint(10, 14)
    columns = 2 if r.random() < 0.3 else 1
    width = (c.width - 2 * margin - (columns - 1) * 24) / columns
    for column in range(columns):
        x, y = margin + column * (width + 24), margin + 70.0
        while y < c.height - margin - 20:
            y = c.lines(
                x,
                y,
                width,
                c.height - margin - 20,
                _en(r, r.randint(2, 5)),
                font,
                body,
                1.45,
                cjk=False,
            )
            y += body
    return c.picture()


def _doc_table(r: random.Random) -> Image.Image:
    c = _Canvas(_A4P, _paper(r))
    margin = 66.0
    zh = r.random() < 0.6
    font = _font("cjk") if zh else _latin(r)
    title = r.choice(_TITLES_ZH if zh else _TITLES_EN)
    c.text(((c.width - font.text_length(title, fontsize=22)) / 2, margin + 22), title, font, 22)
    cell = r.randint(9, 12)
    columns, rows = r.randint(4, 7), r.randint(14, 30)
    top = margin + 60
    row_h = min(30.0, (c.height - top - margin) / rows)
    col_w = (c.width - 2 * margin) / columns
    for row in range(rows + 1):
        c.page.draw_line(
            (margin, top + row * row_h), (c.width - margin, top + row * row_h), width=1
        )
    for column in range(columns + 1):
        x = margin + column * col_w
        c.page.draw_line((x, top), (x, top + rows * row_h), width=1)
    for row in range(rows):
        for column in range(columns):
            if row == 0:
                text = _zh(r, r.randint(2, 4)) if zh else r.choice(("Item", "Qty", "Unit", "Price"))
            elif column == 0:
                text = _zh(r, r.randint(2, 5)) if zh else r.choice(_EN).split(" ")[r.randint(0, 3)]
            else:
                text = f"{r.uniform(0, 999):.{r.randint(0, 2)}f}"
            c.text(
                (margin + column * col_w + 4, top + row * row_h + (row_h + cell) / 2 - 1),
                text,
                font,
                cell,
            )
    return c.picture()


def _doc_form(r: random.Random) -> Image.Image:
    c = _Canvas(_A4P, _paper(r))
    margin = 70.0
    zh = r.random() < 0.6
    font = _font("cjk") if zh else _latin(r)
    title = r.choice(_TITLES_ZH) + "申请表" if zh else r.choice(_TITLES_EN) + " Form"
    c.text(((c.width - font.text_length(title, fontsize=23)) / 2, margin + 23), title, font, 23)
    label_size = r.randint(12, 15)
    y = margin + 80
    while y < c.height - margin - 40:
        label = r.choice(_FORM_ZH if zh else _FORM_EN) + ("\uff1a" if zh else ":")
        c.text((margin, y + label_size), label, font, label_size)
        x0 = margin + font.text_length(label, fontsize=label_size) + 10
        c.page.draw_line((x0, y + label_size + 3), (c.width - margin, y + label_size + 3), width=1)
        if r.random() < 0.3:
            for k in range(3):
                box = x0 + 20 + k * 100
                c.page.draw_rect(pymupdf.Rect(box, y, box + label_size, y + label_size), width=1)
                answer = r.choice(("是", "否", "其他") if zh else ("Yes", "No", "Other"))
                c.text((box + label_size + 5, y + label_size), answer, font, label_size)
        y += label_size * r.uniform(2.4, 3.4)
    return c.picture()


def _doc_vertical(r: random.Random) -> Image.Image:
    """A horizontal title, Chinese in vertical columns (right to left), sometimes a few
    horizontal lines of English notes below."""
    c = _Canvas(_A4P, _paper(r))
    margin = 60.0
    cjk = _font("cjk")
    title = r.choice(_TITLES_ZH)
    c.text(((c.width - cjk.text_length(title, fontsize=25)) / 2, margin + 25), title, cjk, 25)
    size = r.randint(14, 18)
    step = size * 1.7
    top = margin + 60
    bottom = c.height - margin - (110 if r.random() < 0.5 else 0)
    text = _zh(r, 2000)
    x, k = c.width - margin - size, 0
    writer = pymupdf.TextWriter(c.page.rect, color=(0, 0, 0))
    while x > margin:
        y = top + size
        while y < bottom:
            writer.append((x, y), text[k % len(text)], font=cjk, fontsize=size)
            y += size * 1.08
            k += 1
        x -= step
    writer.write_text(c.page)
    if bottom < c.height - margin:
        notes = _wrap(_en(r, 3), _font("helv"), 12, c.width - 2 * margin, cjk=False)[:4]
        for i, line in enumerate(notes):
            c.text((margin, bottom + 30 + i * 18), line, _font("helv"), 12)
    return c.picture()


DOCUMENTS: dict[str, Callable[[random.Random], Image.Image]] = {
    "zh": _doc_zh,
    "en": _doc_en,
    "table": _doc_table,
    "form": _doc_form,
    "vertical": _doc_vertical,
}


# ---------------------------------------------------------------- pages that are not text documents


def _photo(r: random.Random, size: tuple[int, int]) -> Image.Image:
    """A photo-like picture: a smooth colour field, a horizon, soft blobs, grain."""
    width, height = size
    rng = np.random.default_rng(r.randrange(1 << 30))
    small = Image.fromarray(rng.integers(30, 230, (r.randint(3, 6), r.randint(3, 6), 3), np.uint8))
    field = np.asarray(small.resize((width, height), Image.Resampling.BICUBIC), np.float32)
    if r.random() < 0.7:
        horizon = int(height * r.uniform(0.25, 0.7))
        sky = np.linspace(
            [r.randint(90, 170), r.randint(140, 200), r.randint(190, 250)], [235, 240, 245], horizon
        )
        field[:horizon] = 0.35 * field[:horizon] + 0.65 * sky[:, None, :]
        ground = np.array([r.randint(40, 130), r.randint(70, 150), r.randint(20, 90)], np.float32)
        field[horizon:] = 0.5 * field[horizon:] + 0.5 * ground
    picture = Image.fromarray(np.clip(field, 0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(picture)
    for _ in range(r.randint(3, 12)):
        x, y = r.randint(0, width), r.randint(0, height)
        rx, ry = r.randint(width // 20, width // 4), r.randint(height // 20, height // 4)
        draw.ellipse(
            (x - rx, y - ry, x + rx, y + ry), fill=tuple(r.randint(0, 255) for _ in range(3))
        )
    picture = picture.filter(ImageFilter.GaussianBlur(r.uniform(2, 8)))
    grain = rng.normal(0, r.uniform(3, 9), (height, width, 3))
    return Image.fromarray(
        np.clip(np.asarray(picture, np.float32) + grain, 0, 255).astype(np.uint8)
    )


def _px(points: float) -> int:
    return max(1, round(points * SCAN_DPI / 72))


def _photo_page(r: random.Random, size: tuple[float, float], margin: float) -> Image.Image:
    c = _Canvas(size, _paper(r))
    rect = pymupdf.Rect(margin, margin, c.width - margin, c.height - margin)
    c.image(rect, _photo(r, (_px(rect.width) // 2, _px(rect.height) // 2)))
    return c.picture()


def _photo_portrait(r: random.Random) -> Image.Image:
    return _photo_page(r, _A4P, 0)


def _photo_landscape(r: random.Random) -> Image.Image:
    return _photo_page(r, _A4L, 0)


def _photo_scan(r: random.Random) -> Image.Image:
    return _photo_page(r, r.choice((_A4P, _A4L)), r.uniform(35, 90))


def _cover(r: random.Random) -> Image.Image:
    c = _Canvas(_A4P, _paper(r))
    if r.random() < 0.6:
        c.image(c.page.rect, _photo(r, (_px(c.width) // 2, _px(c.height) // 2)))
    else:
        top, bottom = [tuple(r.random() for _ in range(3)) for _ in range(2)]
        for band in range(40):
            t = band / 39
            color = tuple(a + (b - a) * t for a, b in zip(top, bottom, strict=True))
            y = c.height * band / 40
            c.page.draw_rect(
                pymupdf.Rect(0, y, c.width, y + c.height / 40 + 1), color=None, fill=color
            )
    zh = r.random() < 0.5
    font = _font("cjk") if zh else _font("hebo")
    y = c.height * r.uniform(0.15, 0.6)
    c.page.draw_rect(
        pymupdf.Rect(0, y - 15, c.width, y + 95),
        color=None,
        fill=tuple(r.random() * 0.3 for _ in range(3)),
    )
    title = r.choice(_TITLES_ZH if zh else _TITLES_EN)
    size = r.randint(40, 58)
    c.text(
        ((c.width - font.text_length(title, fontsize=size)) / 2, y + size),
        title,
        font,
        size,
        color=(1, 1, 1),
    )
    c.text((c.width / 2 - 30, c.height - 70), "2026", _font("helv"), 24, color=(1, 1, 1))
    return c.picture()


def _chart_into(c: _Canvas, rect: pymupdf.Rect, r: random.Random) -> None:
    kind = r.choice(("bar", "barh", "line", "pie", "scatter"))
    rng = np.random.default_rng(r.randrange(1 << 30))
    values = np.abs(rng.normal(50, 20, r.randint(4, 12)))
    inner = pymupdf.Rect(rect.x0 + 30, rect.y0 + 25, rect.x1 - 10, rect.y1 - 20)
    colour = tuple(r.random() * 0.8 for _ in range(3))
    c.text(
        (rect.x0 + rect.width / 2 - 30, rect.y0 + 15),
        r.choice(("Q1-Q4", "2021-2026", "Sales", "Growth", "Share")),
        _font("helv"),
        11,
    )
    if kind == "pie":
        centre = pymupdf.Point(inner.x0 + inner.width / 2, inner.y0 + inner.height / 2)
        radius = min(inner.width, inner.height) / 2.2
        start = pymupdf.Point(centre.x + radius, centre.y)
        total = float(values[:6].sum())
        for value in values[:6]:
            sweep = 360 * float(value) / total
            c.page.draw_sector(
                centre, start, sweep, fill=tuple(r.random() for _ in range(3)), color=(1, 1, 1)
            )
            angle = math.radians(sweep)
            offset = start - centre
            start = pymupdf.Point(
                centre.x + offset.x * math.cos(angle) - offset.y * math.sin(angle),
                centre.y + offset.x * math.sin(angle) + offset.y * math.cos(angle),
            )
        return
    c.page.draw_line(inner.bl, inner.tl, width=1)
    c.page.draw_line(inner.bl, inner.br, width=1)
    top = float(values.max()) * 1.1
    if kind == "scatter":
        for x, y in rng.normal(0, 1, (150, 2)):
            px = inner.x0 + inner.width * (0.5 + x / 6)
            py = inner.y1 - inner.height * (0.5 + y / 6)
            c.page.draw_circle((px, py), 1.5, color=colour, fill=colour)
        return
    step = inner.width / len(values)
    if kind == "line":
        points = [
            (inner.x0 + step * (i + 0.5), inner.y1 - inner.height * float(v) / top)
            for i, v in enumerate(values)
        ]
        c.page.draw_polyline(points, color=colour, width=2)
        return
    for i, value in enumerate(values):
        if kind == "bar":
            bar = pymupdf.Rect(
                inner.x0 + step * i + 2,
                inner.y1 - inner.height * float(value) / top,
                inner.x0 + step * (i + 1) - 2,
                inner.y1,
            )
        else:
            row = inner.height / len(values)
            bar = pymupdf.Rect(
                inner.x0,
                inner.y0 + row * i + 2,
                inner.x0 + inner.width * float(value) / top,
                inner.y0 + row * (i + 1) - 2,
            )
        c.page.draw_rect(bar, color=None, fill=colour)


def _chart(r: random.Random) -> Image.Image:
    c = _Canvas(r.choice((_A4P, _A4L, _SLIDE)), (1, 1, 1))
    if r.random() < 0.5:
        _chart_into(c, pymupdf.Rect(25, 25, c.width - 25, c.height - 25), r)
    else:
        _chart_into(c, pymupdf.Rect(25, 25, c.width - 25, c.height / 2 - 10), r)
        _chart_into(c, pymupdf.Rect(25, c.height / 2 + 10, c.width - 25, c.height - 25), r)
    return c.picture()


def _slide(r: random.Random) -> Image.Image:
    c = _Canvas(_SLIDE, tuple(r.uniform(0.8, 1) for _ in range(3)))
    zh = r.random() < 0.5
    font = _font("cjk") if zh else _font("helv")
    c.page.draw_rect(
        pymupdf.Rect(0, 0, c.width, 78), color=None, fill=tuple(r.random() * 0.45 for _ in range(3))
    )
    c.text((36, 52), r.choice(_TITLES_ZH if zh else _TITLES_EN), font, 32, color=(1, 1, 1))
    kind = r.choice(("bullets", "photo", "chart"))
    right = pymupdf.Rect(c.width / 2 - 20, 100, c.width - 30, c.height - 30)
    if kind == "photo":
        c.image(right, _photo(r, (_px(right.width) // 2, _px(right.height) // 2)))
    elif kind == "chart":
        _chart_into(c, right, r)
    y = 120.0
    for _ in range(r.randint(2, 5)):
        bullet = _zh(r, r.randint(6, 12)) if zh else " ".join(r.choice(_EN).split(" ")[:5])
        c.page.draw_circle((40, y - 6), 4, color=(0, 0, 0), fill=(0, 0, 0))
        c.text((54, y), bullet, font, 18)
        y += 46
    return c.picture()


def _drawing(r: random.Random) -> Image.Image:
    c = _Canvas(r.choice((_A3L, _A4L)), (1, 1, 1))
    w, h = c.width, c.height
    c.page.draw_rect(pymupdf.Rect(15, 15, w - 15, h - 15), width=2)
    block = pymupdf.Rect(w - 300, h - 120, w - 15, h - 15)
    c.page.draw_rect(block, width=1.5)
    for k in range(1, 4):
        c.page.draw_line((block.x0, block.y0 + k * 26), (block.x1, block.y0 + k * 26), width=1)
    c.page.draw_line((block.x0 + 130, block.y0), (block.x0 + 130, block.y1), width=1)
    labels = (
        f"DWG NO. {r.randint(1000, 9999)}",
        f"SCALE 1:{r.choice((1, 2, 5, 10))}",
        "MATERIAL Q235",
        "SHEET 1/1",
    )
    for k, label in enumerate(labels):
        c.text((block.x0 + 5, block.y0 + k * 26 + 18), label, _font("helv"), 10)
    for _ in range(r.randint(2, 4)):
        cx, cy = r.uniform(120, w - 380), r.uniform(100, h - 170)
        rw, rh = r.uniform(70, 180), r.uniform(50, 130)
        c.page.draw_rect(pymupdf.Rect(cx - rw, cy - rh, cx + rw, cy + rh), width=1.5)
        for _ in range(r.randint(1, 4)):
            radius = r.uniform(10, min(rw, rh) / 2)
            ox, oy = cx + r.uniform(-rw / 2, rw / 2), cy + r.uniform(-rh / 2, rh / 2)
            c.page.draw_circle((ox, oy), radius, width=1)
            c.page.draw_line(
                (ox - radius - 8, oy), (ox + radius + 8, oy), width=0.5, dashes="[4 2] 0"
            )
        y = cy + rh + 25
        c.page.draw_line((cx - rw, y), (cx + rw, y), width=0.5)
        c.text((cx - 10, y - 5), str(int(rw)), _font("helv"), 10)
    return c.picture()


def _map(r: random.Random) -> Image.Image:
    c = _Canvas(r.choice((_A4L, _A4P, _A3L)), (1, 1, 1))
    width, height = _px(c.width) // 4, _px(c.height) // 4
    rng = np.random.default_rng(r.randrange(1 << 30))
    seeds = rng.uniform((0, 0), (width, height), (r.randint(12, 30), 2))
    yy, xx = np.mgrid[0:height, 0:width]
    nearest = np.argmin(
        (xx[..., None] - seeds[:, 0]) ** 2 + (yy[..., None] - seeds[:, 1]) ** 2, axis=-1
    )
    palette = rng.integers((150, 170, 120), (250, 250, 230), (len(seeds), 3)).astype(np.uint8)
    c.image(c.page.rect, Image.fromarray(palette[nearest]).filter(ImageFilter.SMOOTH))
    for _ in range(r.randint(4, 10)):
        points = [(r.uniform(0, c.width), r.uniform(0, c.height)) for _ in range(r.randint(3, 6))]
        c.page.draw_polyline(
            points,
            color=r.choice(((0.86, 0.24, 0.16), (0.98, 0.78, 0.2))),
            width=r.uniform(1.5, 4.5),
        )
    river = [(0, r.uniform(0, c.height))] + [
        (r.uniform(0, c.width), r.uniform(0, c.height)) for _ in range(4)
    ]
    c.page.draw_polyline(river, color=(0.27, 0.51, 0.86), width=7)
    for _ in range(r.randint(10, 30)):
        x, y = r.uniform(10, c.width - 60), r.uniform(20, c.height - 20)
        c.page.draw_circle((x, y), 2.5, color=(0, 0, 0), fill=(0, 0, 0))
        c.text((x + 4, y + 4), _zh(r, r.randint(2, 4)), _font("cjk"), 11)
    c.text((c.width - 42, 26), "N", _font("helv"), 12)
    return c.picture()


def _handwriting(r: random.Random) -> Image.Image:
    """Ruled or plain paper with pen strokes in lines — a cursive-like scrawl."""
    c = _Canvas(_A4P, _paper(r))
    if r.random() < 0.6:
        for rule in range(80, int(c.height) - 40, 32):
            c.page.draw_line((40, rule), (c.width - 40, rule), color=(0.6, 0.67, 0.86), width=1)
    ink = r.choice(((0.08, 0.12, 0.47), (0.04, 0.04, 0.04)))
    pen = c.page.new_shape()  # one shape for all strokes: drawing each one apart is slow
    y = 74.0
    while y < c.height - 70:
        x = 50 + r.uniform(-5, 15)
        end = r.uniform(c.width * 0.5, c.width - 60)
        while x < end:
            word = r.uniform(25, 70)
            points = [(x + t * word / 8, y + r.uniform(-6, 2)) for t in range(9)]
            for start in range(0, 8, 2):
                p0, p1, p2 = points[start : start + 3]
                pen.draw_bezier(p0, (p0[0] + 2, p0[1] - 9), (p2[0] - 2, p2[1] - 9), p2)
                pen.draw_line(p0, p1)
            x += word + r.uniform(8, 16)
        y += 32 * r.choice((1, 1, 2))
    pen.finish(color=ink, width=1.3, closePath=False)
    pen.commit()
    return c.picture()


def _stamp(r: random.Random) -> Image.Image:
    """A mostly blank page with a red seal, sometimes a signature and a date."""
    c = _Canvas(r.choice((_A4P, _A4P, _A4L)), _paper(r))
    cx, cy = r.uniform(c.width / 4, 3 * c.width / 4), r.uniform(c.height / 4, 3 * c.height / 4)
    radius = r.uniform(45, 80)
    red = (r.uniform(0.75, 0.9), r.uniform(0.08, 0.24), r.uniform(0.08, 0.24))
    c.page.draw_circle((cx, cy), radius, color=red, width=max(2.0, radius / 18))
    star = [
        (
            cx + radius * 0.35 * math.cos(math.radians(-90 + k * 36)) * (1 if k % 2 == 0 else 0.4),
            cy + radius * 0.35 * math.sin(math.radians(-90 + k * 36)) * (1 if k % 2 == 0 else 0.4),
        )
        for k in range(10)
    ]
    c.page.draw_polyline([*star, star[0]], color=red, fill=red, closePath=True)
    ring = _zh(r, r.randint(8, 14))
    for i, char in enumerate(ring):
        angle = math.radians(-210 + 240 * i / max(len(ring) - 1, 1))
        point = pymupdf.Point(
            cx + radius * 0.72 * math.cos(angle), cy + radius * 0.72 * math.sin(angle)
        )
        c.page.insert_text(
            point,
            char,
            fontsize=max(8.0, radius / 6),
            fontname="china-s",
            color=red,
            morph=(point, pymupdf.Matrix(math.degrees(angle) + 90)),
        )
    if r.random() < 0.5:
        points = [(cx - 100, cy + 100)]
        for _ in range(12):
            points.append((points[-1][0] + r.uniform(5, 20), points[-1][1] + r.uniform(-15, 15)))
        c.page.draw_polyline(points, color=(0.08, 0.08, 0.24), width=2)
    if r.random() < 0.5:
        date = f"2026年{r.randint(1, 12)}月{r.randint(1, 28)}日"
        c.text((cx - 60, cy + radius + 40), date, _font("cjk"), 15)
    return c.picture()


def _collage(r: random.Random) -> Image.Image:
    c = _Canvas(r.choice((_A4P, _A4L)), (1, 1, 1))
    across, down = r.choice(((2, 2), (2, 3), (3, 2), (3, 3)))
    gap = 10.0
    cell_w, cell_h = (c.width - (across + 1) * gap) / across, (c.height - (down + 1) * gap) / down
    for i in range(across):
        for j in range(down):
            rect = pymupdf.Rect(
                gap + i * (cell_w + gap),
                gap + j * (cell_h + gap),
                gap + i * (cell_w + gap) + cell_w,
                gap + j * (cell_h + gap) + cell_h,
            )
            c.image(rect, _photo(r, (_px(cell_w) // 2, _px(cell_h) // 2)))
    return c.picture()


def _poster(r: random.Random) -> Image.Image:
    c = _Canvas(_A4P, tuple(r.random() for _ in range(3)))
    for _ in range(r.randint(5, 15)):
        x, y, size = r.uniform(0, c.width), r.uniform(0, c.height), r.uniform(20, 150)
        fill = tuple(r.random() for _ in range(3))
        if r.random() < 0.5:
            c.page.draw_circle((x, y), size, color=None, fill=fill)
        else:
            c.page.draw_rect(
                pymupdf.Rect(x - size, y - size, x + size, y + size), color=None, fill=fill
            )
    for _ in range(r.randint(1, 4)):
        c.text(
            (r.uniform(50, c.width - 150), r.uniform(80, c.height - 100)),
            f"{r.randint(10, 99)}%",
            _font("hebo"),
            r.uniform(40, 80),
            color=(1, 1, 1),
        )
    return c.picture()


#: Upright pages that are not text documents (D3): kind → (draw, how many).
NONTEXT: dict[str, tuple[Callable[[random.Random], Image.Image], int]] = {
    "photo_portrait": (_photo_portrait, 25),
    "photo_landscape": (_photo_landscape, 25),
    "photo_scan": (_photo_scan, 20),
    "cover": (_cover, 30),
    "slide": (_slide, 30),
    "drawing": (_drawing, 25),
    "map": (_map, 25),
    "chart": (_chart, 30),
    "handwriting": (_handwriting, 25),
    "stamp_blank": (_stamp, 25),
    "collage": (_collage, 15),
    "poster": (_poster, 15),
}


# ---------------------------------------------------------------- scanning


def scanify(picture: Image.Image, r: random.Random) -> Image.Image:
    """Half the time as it is; else a scanner's slight skew, grain and JPEG."""
    if r.random() < 0.5:
        return picture
    skewed = picture.rotate(
        r.uniform(-0.8, 0.8),
        resample=Image.Resampling.BICUBIC,
        expand=False,
        fillcolor=(250, 250, 248),
    )
    rng = np.random.default_rng(r.randrange(1 << 30))
    grain = rng.normal(0, r.uniform(2, 7), (skewed.height, skewed.width, 3))
    noisy = Image.fromarray(
        np.clip(np.asarray(skewed, np.float32) + grain, 0, 255).astype(np.uint8)
    )
    out = io.BytesIO()
    noisy.save(out, format="JPEG", quality=r.randint(60, 85))
    return Image.open(io.BytesIO(out.getvalue())).convert("RGB")


def turned(picture: Image.Image, clockwise: int) -> Image.Image:
    """``picture`` turned ``clockwise`` degrees clockwise (the page takes its shape)."""
    return picture.rotate(-clockwise, expand=True) if clockwise else picture


def document(kind: str, seed: int) -> Image.Image:
    """An upright scanned document page of ``kind`` (:data:`DOCUMENTS`)."""
    r = random.Random(f"doc-{kind}-{seed}")  # noqa: S311 — reproducible test pages, not crypto
    return scanify(DOCUMENTS[kind](r), r)


def nontext(kind: str, seed: int) -> Image.Image:
    """An upright scanned page of ``kind`` (:data:`NONTEXT`) that is not a text document."""
    r = random.Random(f"nontext-{kind}-{seed}")  # noqa: S311 — reproducible test pages
    return scanify(NONTEXT[kind][0](r), r)


def scanned_pdf(pictures: Sequence[Image.Image]) -> bytes:
    """A PDF of one page per picture (at :data:`SCAN_DPI`), with no text layer."""
    doc = pymupdf.open()
    for picture in pictures:
        out = io.BytesIO()
        picture.save(out, format="JPEG", quality=90)
        page = doc.new_page(
            width=picture.width * 72 / SCAN_DPI, height=picture.height * 72 / SCAN_DPI
        )
        page.insert_image(page.rect, stream=out.getvalue())
    raw = bytes(doc.tobytes())
    doc.close()
    return raw
