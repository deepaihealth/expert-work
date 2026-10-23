"""B-209 PR-2 §5.4 —— PDF 文字页上的插图(在解析进程池里跑,PyMuPDF)。

* **只读元数据,不解码**:``page.get_images(full=True)`` 给出本页每个图片对象的原始尺寸、
  ``page.get_image_bbox`` 给出放置框,``/Length`` 给出压缩后字节数 —— 都不解码图片
  (``get_image_info`` 会把每张图按原分辨率解码:一张 2 万 x 2 万的灰度图要 2 秒、400 MB,
  安全复核 S-H1)。超过 :data:`~.figure_images.MAX_DECODE_PIXELS` 的图不碰,计 ``unsupported``。
* 单文档过滤(第 1~4 道)按元数据做,再按面积留一个短名单
  (:data:`~.figure_filters.MAX_CANDIDATES`),**只有短名单里的图**才渲染:按放置框裁本页的渲染
  (原始分辨率,长边不超过 2,000 px;盖在图上的文字标注也在里面)。同一张图(压缩流相同)在几个
  对象里重复出现的,按内容合并后再按「≥ 3 页」过滤一次。行内图片(没有对象号)不处理。
* **落位**:本页文字块里,与图在水平方向上重叠、且完全在图上方的最后一块(双栏版面取图所在那一
  栏;没有重叠的块时看全部块),用它的末尾一小段去本页 Markdown 里找,插在那一段之后的段落边界
  上(不挪到页尾);找不到时按「图上方的字数占全页字数」的比例落到最近的段落边界。同一段落边界上
  有几张图时按从上到下的顺序。落位在入库协程里做(纯函数),这里只交出图的位置和本页文字块。
* 带隐藏文字层的扫描页上的图就是这一页本身,调用方不把这种页交过来。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pymupdf
from PIL import Image

from control_plane.knowledge.recognition import figure_images
from control_plane.knowledge.recognition.figure_extract import FigureExtraction
from control_plane.knowledge.recognition.figure_filters import (
    MAX_CANDIDATES,
    REPEATED_MIN,
    FigureCandidate,
    first_failed_filter,
)
from control_plane.knowledge.recognition.figure_markup import placeholder

#: Clip renders at most this many pixels per PDF point (8x = 576 DPI).
_MAX_ZOOM = 8.0
#: A text block counts as above the image when it ends this close to the
#: image's top edge (points).
_ABOVE_TOLERANCE = 2.0
#: Characters of the block above an image used to find it in the Markdown.
_ANCHOR_CHARS = 24
#: Markdown markup / bullets left out when matching block text to Markdown
#: (with whitespace): pymupdf4llm adds the one, PDFs draw the other.
_MARKUP = frozenset("*_#>|`~•·-")

#: ``(top, bottom, x0, x1, text)`` of a text block, in PDF points.
Block = tuple[float, float, float, float, str]
#: ``(top, x0, x1, figure number)`` of a figure placed on a page.
Spot = tuple[float, float, float, int]


@dataclass(frozen=True)
class _Image:
    page: int  # 0-based
    xref: int
    rect: tuple[float, float, float, float]
    share: float
    width: int
    height: int
    size: int
    content: str  # sha256 of the raw image stream (and its soft mask)


@dataclass(frozen=True)
class PdfFigures:
    """Figures of a PDF's text pages, where each goes (page, 0-based → spots
    in reading order) and those pages' text blocks (for placement)."""

    figures: FigureExtraction
    placements: Mapping[int, tuple[Spot, ...]]
    blocks: Mapping[int, tuple[Block, ...]]


NO_PDF_FIGURES = PdfFigures(figures=FigureExtraction(), placements={}, blocks={})


def collect_pdf_figures(raw: bytes, pages: Sequence[int]) -> PdfFigures:
    """The figures on the given text pages (0-based) of a PDF (see module
    doc). A page or an image PyMuPDF cannot read is skipped (counted)."""
    doc = pymupdf.open(stream=raw, filetype="pdf")
    try:
        return _collect(doc, pages)
    finally:
        doc.close()


def _collect(doc: pymupdf.Document, pages: Sequence[int]) -> PdfFigures:
    images = [image for index in pages for image in _page_images(doc, index)]
    by_xref: dict[int, list[_Image]] = {}
    for image in images:
        by_xref.setdefault(image.xref, []).append(image)
    filtered = unsupported = 0
    passing: list[list[_Image]] = []
    for placed in by_xref.values():
        first = placed[0]
        if first.width * first.height > figure_images.MAX_DECODE_PIXELS:
            unsupported += 1
        elif first_failed_filter(
            width=first.width,
            height=first.height,
            size_bytes=first.size,
            occurrences=len({image.page for image in placed}),
            page_share=max(image.share for image in placed),
        ):
            filtered += 1
        else:
            passing.append(placed)
    passing.sort(key=lambda placed: -placed[0].width * placed[0].height)
    shortlist, capped = passing[:MAX_CANDIDATES], len(passing) - MAX_CANDIDATES
    # The same picture embedded as several objects: one figure, its pages joined.
    merged: dict[str, list[_Image]] = {}
    for placed in shortlist:
        merged.setdefault(placed[0].content, []).extend(placed)
    candidates: list[FigureCandidate] = []
    numbers: dict[str, int] = {}
    for content, placed in sorted(merged.items(), key=lambda item: _reading_order(item[1][0])):
        if len({image.page for image in placed}) >= REPEATED_MIN:
            filtered += 1
            continue
        first = min(placed, key=_reading_order)
        rendered = _render_clip(doc, first)
        if rendered is None:
            unsupported += 1
            continue
        numbers[content] = len(candidates) + 1
        candidates.append(
            FigureCandidate(
                number=len(candidates) + 1,
                image=rendered.jpeg,
                width=first.width,
                height=first.height,
                pixel_hash=f"pdf:{content}",
                pages=tuple(sorted({image.page + 1 for image in placed})),
            )
        )
    spots: dict[int, list[Spot]] = {}
    for content, placed in merged.items():
        number = numbers.get(content)
        if number is None:
            continue
        for image in placed:
            page_spots = spots.setdefault(image.page, [])
            if all(spot[3] != number for spot in page_spots):
                page_spots.append((image.rect[1], image.rect[0], image.rect[2], number))
    return PdfFigures(
        figures=FigureExtraction(
            candidates=tuple(candidates),
            extracted=len(by_xref),
            filtered=filtered,
            unsupported=unsupported,
            capped=max(0, capped),
        ),
        placements={page: tuple(sorted(found)) for page, found in spots.items()},
        blocks={page: _text_blocks(doc, page) for page in spots},
    )


def _reading_order(image: _Image) -> tuple[int, float, float]:
    return image.page, image.rect[1], image.rect[0]


def _page_images(doc: pymupdf.Document, index: int) -> list[_Image]:
    """The page's image objects, read without decoding them."""
    try:
        page = doc[index]
        items = page.get_images(full=True)
        page_rect = page.rect
    except Exception:
        return []
    area = float(page_rect.width * page_rect.height) or 1.0
    found = []
    for item in items:
        try:
            xref, smask, width, height = int(item[0]), int(item[1]), int(item[2]), int(item[3])
            box = pymupdf.Rect(page.get_image_bbox(item)) & page_rect
            if xref <= 0 or box.is_empty or box.is_infinite:
                continue
            found.append(
                _Image(
                    page=index,
                    xref=xref,
                    rect=(float(box.x0), float(box.y0), float(box.x1), float(box.y1)),
                    share=float(box.width * box.height) / area,
                    width=width,
                    height=height,
                    size=_stream_length(doc, xref),
                    content=_content_hash(doc, xref, smask),
                )
            )
        except Exception:  # noqa: S112 — one unreadable image never fails the page
            continue
    return found


def _stream_length(doc: pymupdf.Document, xref: int) -> int:
    kind, value = doc.xref_get_key(xref, "Length")
    try:
        return int(value) if kind == "int" else len(doc.xref_stream_raw(xref) or b"")
    except ValueError:
        return 0


def _content_hash(doc: pymupdf.Document, xref: int, smask: int) -> str:
    """The image's identity across objects and documents: sha256 of its raw
    (still compressed) stream and soft mask — no decoding."""
    digest = hashlib.sha256(doc.xref_stream_raw(xref) or b"")
    if smask > 0:
        digest.update(doc.xref_stream_raw(smask) or b"")
    return digest.hexdigest()


def _text_blocks(doc: pymupdf.Document, index: int) -> tuple[Block, ...]:
    try:
        blocks = doc[index].get_text("blocks")
    except Exception:
        return ()
    found = [
        (float(block[1]), float(block[3]), float(block[0]), float(block[2]), str(block[4]))
        for block in blocks
        if len(block) >= 7 and block[6] == 0 and str(block[4]).strip()
    ]
    return tuple(sorted(found, key=lambda block: (block[0], block[2])))


def _render_clip(doc: pymupdf.Document, image: _Image) -> figure_images.NormalizedImage | None:
    """The figure as it appears on the page, at about its own resolution."""
    try:
        page = doc[image.page]
        clip = pymupdf.Rect(image.rect)
        longest_pt = max(clip.width, clip.height) or 1.0
        zoom = max(image.width, image.height) / longest_pt
        zoom = min(max(zoom, 1.0), _MAX_ZOOM, figure_images.FIGURE_MAX_EDGE_PX / longest_pt)
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
        if pixmap.n != 3:
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pixmap)
        rgb = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    except Exception:
        return None
    return figure_images.from_rgb(rgb)


def place_figures(texts: Mapping[int, str], figures: PdfFigures) -> dict[int, str]:
    """``texts`` (page index → Markdown) with each figure's placeholder at its
    place on its page (pure: the blocks came with ``figures``)."""
    placed = dict(texts)
    for index, spots in figures.placements.items():
        placed[index] = insert_at_positions(
            placed.get(index, ""), figures.blocks.get(index, ()), spots
        )
    return placed


def insert_at_positions(text: str, blocks: Sequence[Block], spots: Sequence[Spot]) -> str:
    """``text`` with ``placeholder(number)`` inserted for each spot (see
    module doc). ``blocks``: the page's text blocks."""
    flat, origin = _flatten(text)
    total = sum(len(_squash(block[4])) for block in blocks) or 1
    cursor = 0
    inserts: list[tuple[int, float, int]] = []
    for top, x0, x1, number in sorted(spots):
        above = [block for block in blocks if block[1] <= top + _ABOVE_TOLERANCE]
        column = [block for block in above if block[2] < x1 and block[3] > x0]
        above = column or above
        if not above:
            inserts.append((0, top, number))
            continue
        anchor = _squash(above[-1][4])[-_ANCHOR_CHARS:]
        found = flat.find(anchor, cursor) if anchor else -1
        if found < 0 and anchor:
            found = flat.rfind(anchor)
        if found >= 0:
            cursor = found + len(anchor)
            position = origin[cursor - 1] + 1
        else:
            share = sum(len(_squash(block[4])) for block in above) / total
            position = int(min(1.0, share) * len(text))
        inserts.append((_paragraph_end(text, position), top, number))
    # Back to front; at one position the lower figure goes in first, so the
    # figures there read top to bottom.
    for position, _, number in sorted(inserts, reverse=True):
        text = f"{text[:position]}{placeholder(number)}{text[position:]}"
    return text


def _skipped(char: str) -> bool:
    return char.isspace() or char in _MARKUP


def _squash(text: str) -> str:
    return "".join(char for char in text if not _skipped(char))


def _flatten(text: str) -> tuple[str, list[int]]:
    """``text`` without whitespace / Markdown markup, and each kept
    character's index in ``text``."""
    origin = [index for index, char in enumerate(text) if not _skipped(char)]
    return "".join(text[index] for index in origin), origin


def _paragraph_end(text: str, position: int) -> int:
    """The end of the paragraph holding ``position`` (just before its blank
    line), or the text's end."""
    if position <= 0:
        return 0
    # From one character back: a position just inside a blank line still
    # belongs to the paragraph before it.
    found = text.find("\n\n", position - 1)
    return len(text) if found < 0 else found
