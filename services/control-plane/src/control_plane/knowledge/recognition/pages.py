"""B-209 §5.2 / §5.3 —— PDF 逐页判定与扫描页渲染。

在解析进程池里跑(PyMuPDF 不是线程安全的,见 ``ingestion.parse_document_in_pool``),
所以这里只有可 pickle 的入参 / 返回值、没有异步。

**逐页判定**:每页算三个信号,任一成立即判「要识别」,并记下原因:

1. 几乎没字且有图:去空白后字数 < 50,且页内有图片对象或非矩形矢量路径(纯空白页不判);
2. 整页是图:图片放置框的并集(裁到页面内)覆盖 ≥ 80% 的页面;
3. 文字层是乱码:私有区 / U+FFFD / CID 字符占比 ≥ 30%。

扫描件自带的不可见 OCR 文字层(≥ 50 字)**保留**,这页不再识别(省钱;质量差的层会被信号 3
抓到)。一页判了要识别就整页识别,不和文字层混拼。

**渲染**:200 DPI、长边上限 2,000 px(模型声明了像素上限的再按它缩)、JPEG 质量 85。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

import pymupdf
import pymupdf4llm

from control_plane.knowledge.parsing import DocumentParseError, parse_document
from expert_work.protocol import IngestErrorCode, PageDecision, PageReason

#: Signal 1 / hidden text layer — characters (whitespace removed).
SPARSE_CHARS = 50
#: Signal 2 — share of the page the images' placement boxes cover. Above
#: WeKnora's 0.5: on the B-209 corpus 0.5 also took 623 template pages (real
#: text over a background image), where the text layer reads better.
FULL_PAGE_IMAGE_COVERAGE = 0.8
#: Signal 2 looks at no more image placements than this (the union is
#: quadratic in them); a page with more is recognized as a whole.
MAX_IMAGE_PLACEMENTS = 500
#: Signal 3 — share of private-use / U+FFFD / CID characters (RAGFlow's 30 %).
GARBLED_SHARE = 0.3

RENDER_DPI = 200
RENDER_MAX_EDGE_PX = 2000
RENDER_JPEG_QUALITY = 85

#: ``get_texttrace`` span type of invisible text (PDF text render mode 3) —
#: what a scanner's OCR layer is written as.
_INVISIBLE_TEXT = 3
_CID = re.compile(r"\(cid:\d+\)")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class PdfPage:
    """One page's decision. ``text`` is the page's text-layer Markdown
    (pymupdf4llm) when it is used, empty for a page sent to recognition."""

    index: int
    decision: PageDecision
    reason: PageReason | None
    chars: int
    images: int
    text: str = ""


@dataclass(frozen=True)
class PdfAnalysis:
    """A PDF's per-page decisions. ``markdown`` is the whole document's
    Markdown exactly as :func:`parse_document` produces it when no page needs
    recognition (so a text-only PDF ingests exactly as before), else ``None``
    and each text page carries its own ``text``."""

    pages: tuple[PdfPage, ...]
    markdown: str | None

    @property
    def ocr_pages(self) -> tuple[PdfPage, ...]:
        return tuple(page for page in self.pages if page.decision is PageDecision.OCR)


def render_params(max_pixels: int | None = None) -> str:
    """The rendering settings as a string — part of the recognition cache key:
    a page rendered differently is a different image."""
    pixels = f";max_pixels={max_pixels}" if max_pixels else ""
    return f"dpi={RENDER_DPI};edge={RENDER_MAX_EDGE_PX};jpeg={RENDER_JPEG_QUALITY}{pixels}"


def analyse_pdf(filename: str, raw: bytes) -> PdfAnalysis:
    """Decide every page of a PDF (see module doc).

    A PDF with no page to recognize parses exactly as before: ``markdown`` is
    :func:`parse_document`'s output, its MarkItDown fallback and its errors
    included. Analysis never fails a document that parsed before: a PDF
    PyMuPDF cannot walk falls back to :func:`parse_document` (no page
    records), a page whose signals cannot be read is kept as a text page.

    :raises DocumentParseError: only as :func:`parse_document` raises it."""
    try:
        doc = pymupdf.open(stream=raw, filetype="pdf")
    except Exception:
        return PdfAnalysis(pages=(), markdown=parse_document(filename, raw))
    try:
        try:
            pages = [_decide_or_text(doc, index) for index in range(doc.page_count)]
        except Exception:
            return PdfAnalysis(pages=(), markdown=parse_document(filename, raw))
        if not any(page.decision is PageDecision.OCR for page in pages):
            return PdfAnalysis(pages=tuple(pages), markdown=parse_document(filename, raw))
        texts = _page_texts(doc, pages)
    finally:
        doc.close()
    return PdfAnalysis(
        pages=tuple(
            page
            if page.decision is PageDecision.OCR
            else PdfPage(
                index=page.index,
                decision=page.decision,
                reason=page.reason,
                chars=page.chars,
                images=page.images,
                text=texts.get(page.index, ""),
            )
            for page in pages
        ),
        markdown=None,
    )


def _decide_or_text(doc: pymupdf.Document, index: int) -> PdfPage:
    """:func:`_decide` for one page; a page PyMuPDF cannot inspect is a text
    page (what it was before B-209)."""
    try:
        return _decide(doc[index])
    except Exception:
        return PdfPage(index, PageDecision.TEXT, None, 0, 0)


def _page_texts(doc: pymupdf.Document, pages: Sequence[PdfPage]) -> dict[int, str]:
    """Each kept page's Markdown (pymupdf4llm page chunks). A text page with no
    chunk text — the chunked parse failed, or skipped the page — falls back to
    PyMuPDF's plain text, so it never goes silently empty."""
    try:
        chunks = pymupdf4llm.to_markdown(doc, page_chunks=True)
    except Exception:
        chunks = []
    by_index: dict[int, str] = {}
    for position, chunk in enumerate(chunks):
        metadata = chunk.get("metadata") or {}
        number = metadata.get("page_number") if isinstance(metadata, dict) else None
        index = number - 1 if isinstance(number, int) and number >= 1 else position
        by_index[index] = str(chunk.get("text") or "")
    texts: dict[int, str] = {}
    for page in pages:
        if page.decision is PageDecision.OCR:
            continue
        text = by_index.get(page.index, "")
        if not text.strip() and page.decision is PageDecision.TEXT:
            text = _plain_text(doc, page.index)
        texts[page.index] = text
    return texts


def _plain_text(doc: pymupdf.Document, index: int) -> str:
    try:
        return str(doc[index].get_text())
    except Exception:
        return ""


def render_pages(raw: bytes, indexes: Sequence[int], max_pixels: int | None = None) -> list[bytes]:
    """JPEG bytes of the given pages (0-based), in order — see :func:`render_params`."""
    doc = _open(raw)
    try:
        return [_render(doc[index], max_pixels) for index in indexes]
    finally:
        doc.close()


def _open(raw: bytes) -> pymupdf.Document:
    try:
        return pymupdf.open(stream=raw, filetype="pdf")
    except Exception as exc:
        msg = f"could not open PDF: {exc}"
        raise DocumentParseError(msg, IngestErrorCode.INVALID_FILE) from exc


def _decide(page: pymupdf.Page) -> PdfPage:
    text = page.get_text()
    chars = len(_WHITESPACE.sub("", text))
    images = page.get_image_info()
    index = page.number or 0

    def decided(decision: PageDecision, reason: PageReason | None = None) -> PdfPage:
        return PdfPage(index, decision, reason, chars, len(images))

    if chars and _garbled_share(text) >= GARBLED_SHARE:
        return decided(PageDecision.OCR, PageReason.GARBLED_TEXT)
    if chars < SPARSE_CHARS:
        if images or _has_non_rectangular_path(page):
            return decided(PageDecision.OCR, PageReason.SPARSE_TEXT)
        return decided(PageDecision.TEXT if chars else PageDecision.BLANK)
    if len(images) > MAX_IMAGE_PLACEMENTS:
        # Too many placements to union cheaply: a page drawn as image tiles.
        return decided(PageDecision.OCR, PageReason.MANY_IMAGES)
    if _image_coverage(page.rect, [info["bbox"] for info in images]) >= FULL_PAGE_IMAGE_COVERAGE:
        if _invisible_chars(page) >= SPARSE_CHARS:
            return decided(PageDecision.TEXT, PageReason.HIDDEN_TEXT_LAYER)
        return decided(PageDecision.OCR, PageReason.FULL_PAGE_IMAGE)
    return decided(PageDecision.TEXT)


def _garbled_share(text: str) -> float:
    flat = _WHITESPACE.sub("", text)
    if not flat:
        return 0.0
    cid = sum(len(match) for match in _CID.findall(flat))
    odd = sum(1 for char in _CID.sub("", flat) if _is_garbled(char))
    return (cid + odd) / len(flat)


def _is_garbled(char: str) -> bool:
    code = ord(char)
    return (
        code == 0xFFFD
        or 0xE000 <= code <= 0xF8FF
        or 0xF0000 <= code <= 0xFFFFD
        or 0x100000 <= code <= 0x10FFFD
    )


def _invisible_chars(page: pymupdf.Page) -> int:
    return sum(
        len(span.get("chars") or ())
        for span in page.get_texttrace()
        if span.get("type") == _INVISIBLE_TEXT
    )


def _has_non_rectangular_path(page: pymupdf.Page) -> bool:
    """A vector drawing that is not a box or a ruler: a curve or a slanted
    line (charts, logos, text converted to outlines). Table rules and frames
    are axis-aligned and do not count."""
    for path in page.get_drawings():
        for item in path.get("items", ()):
            kind = item[0]
            if kind == "c":
                return True
            if kind == "l":
                start, end = item[1], item[2]
                if abs(start.x - end.x) > 0.5 and abs(start.y - end.y) > 0.5:
                    return True
    return False


def _image_coverage(page_rect: pymupdf.Rect, boxes: Sequence[Sequence[float]]) -> float:
    """Share of the page covered by the union of the image boxes (clipped)."""
    area = float(page_rect.width * page_rect.height)
    if area <= 0:
        return 0.0
    clipped = []
    for x0, y0, x1, y1 in (tuple(box)[:4] for box in boxes):
        left, right = max(x0, page_rect.x0), min(x1, page_rect.x1)
        top, bottom = max(y0, page_rect.y0), min(y1, page_rect.y1)
        if right > left and bottom > top:
            clipped.append((left, top, right, bottom))
    return _union_area(clipped) / area


def _union_area(boxes: Sequence[tuple[float, float, float, float]]) -> float:
    """Exact area of a union of rectangles: sweep the x-slabs between box
    edges, merge each slab's y-intervals."""
    xs = sorted({x for box in boxes for x in (box[0], box[2])})
    total = 0.0
    for left, right in pairwise(xs):
        spans = sorted((box[1], box[3]) for box in boxes if box[0] <= left and box[2] >= right)
        covered, reach = 0.0, float("-inf")
        for top, bottom in spans:
            if bottom <= reach:
                continue
            covered += bottom - max(top, reach)
            reach = bottom
        total += covered * (right - left)
    return total


def _render(page: pymupdf.Page, max_pixels: int | None) -> bytes:
    zoom = RENDER_DPI / 72
    longest = max(page.rect.width, page.rect.height)
    if longest * zoom > RENDER_MAX_EDGE_PX:
        zoom = RENDER_MAX_EDGE_PX / longest
    if max_pixels:
        pixels = page.rect.width * page.rect.height * zoom * zoom
        if pixels > max_pixels:
            zoom *= (max_pixels / pixels) ** 0.5
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return bytes(pixmap.tobytes("jpeg", jpg_quality=RENDER_JPEG_QUALITY))
