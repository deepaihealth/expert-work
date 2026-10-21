"""B-209 §5.2 / §5.3 —— PDF 逐页判定(三信号 + 隐藏文字层)与扫描页渲染。

PDF 都在这里现场生成:「扫描页」= 一张写着字的图片铺满页面、没有文字层。
"""

from __future__ import annotations

import io
from typing import Any

import pymupdf
import pytest
from PIL import Image

from control_plane.knowledge.parsing import DocumentParseError, parse_document
from control_plane.knowledge.recognition import pages as pages_module
from control_plane.knowledge.recognition.pages import (
    RENDER_MAX_EDGE_PX,
    analyse_pdf,
    render_pages,
    render_params,
)
from expert_work.protocol import IngestErrorCode, PageDecision, PageReason

_TEXT = "Section 3 covers leave requests. Submit a request three working days ahead. "


def _scan_image() -> bytes:
    """A page-sized picture of some text — what a scanner produces."""
    source = pymupdf.open()
    page = source.new_page()
    page.insert_text((72, 72), "Scanned words on paper " * 6, fontsize=14)
    return bytes(page.get_pixmap(dpi=72).tobytes("png"))


def _pdf(*builders: Any) -> bytes:
    doc = pymupdf.open()
    for build in builders:
        build(doc.new_page())
    return bytes(doc.tobytes())


def _text_page(page: pymupdf.Page) -> None:
    page.insert_text((72, 72), _TEXT * 3, fontsize=9)


def _scanned_page(page: pymupdf.Page) -> None:
    page.insert_image(page.rect, stream=_scan_image())


def _scan_with_footer(page: pymupdf.Page) -> None:
    # A full-page scan with a visible line of text (a generator stamp, a page
    # header) — more than 50 characters, so only the coverage signal sees it.
    page.insert_image(page.rect, stream=_scan_image())
    footer = "Archived copy - records office export, page 1 of 1, generated 2026-10-21"
    page.insert_text((40, 820), footer, fontsize=8)


def _scan_with_ocr_layer(page: pymupdf.Page) -> None:
    page.insert_image(page.rect, stream=_scan_image())
    page.insert_text((72, 100), "Scanned words on paper " * 6, fontsize=11, render_mode=3)


def _blank_page(page: pymupdf.Page) -> None:
    del page


def _chart_page(page: pymupdf.Page) -> None:
    page.draw_bezier((100, 100), (200, 50), (300, 150), (400, 100))


def _table_rules_page(page: pymupdf.Page) -> None:
    page.draw_rect(pymupdf.Rect(50, 50, 500, 300))
    page.draw_line((50, 100), (500, 100))


def _title_with_picture_page(page: pymupdf.Page) -> None:
    # A cover / chapter page: a title under 50 characters and a picture.
    page.insert_text((72, 72), "Chapter 3: Leave policy overview", fontsize=20)
    page.insert_image(pymupdf.Rect(72, 120, 520, 500), stream=_scan_image())


def _bleeding_image_page(page: pymupdf.Page) -> None:
    # A text page whose picture runs off the left edge: only the part on the
    # page (half of it) counts towards coverage.
    page.insert_text((320, 72), _TEXT * 3, fontsize=9)
    page.insert_image(pymupdf.Rect(-600, 0, 297, 842), stream=_scan_image(), keep_proportion=False)


def _small_logo_page(page: pymupdf.Page) -> None:
    # A text page with a small logo: plenty of text, the image covers ~1 %.
    page.insert_text((72, 120), _TEXT * 3, fontsize=9)
    page.insert_image(pymupdf.Rect(500, 20, 560, 60), stream=_scan_image())


@pytest.mark.parametrize(
    ("build", "decision", "reason"),
    [
        (_text_page, PageDecision.TEXT, None),
        (_scanned_page, PageDecision.OCR, PageReason.SPARSE_TEXT),
        (_scan_with_footer, PageDecision.OCR, PageReason.FULL_PAGE_IMAGE),
        (_scan_with_ocr_layer, PageDecision.TEXT, PageReason.HIDDEN_TEXT_LAYER),
        (_blank_page, PageDecision.BLANK, None),
        (_chart_page, PageDecision.OCR, PageReason.SPARSE_TEXT),
        (_table_rules_page, PageDecision.BLANK, None),
        (_small_logo_page, PageDecision.TEXT, None),
        (_title_with_picture_page, PageDecision.OCR, PageReason.SPARSE_TEXT),
        (_bleeding_image_page, PageDecision.TEXT, None),
    ],
)
def test_page_decision(build: Any, decision: PageDecision, reason: PageReason | None) -> None:
    analysis = analyse_pdf("doc.pdf", _pdf(_text_page, build))
    page = analysis.pages[1]
    assert (page.decision, page.reason) == (decision, reason)


class _GarbledPage:
    """The text layer of a PDF whose font maps glyphs to private-use code
    points (common in Chinese PDFs) — PyMuPDF cannot write one, so the page
    is faked at the level ``_decide`` reads it."""

    number = 0
    rect = pymupdf.Rect(0, 0, 595, 842)

    def __init__(self, text: str) -> None:
        self._text = text

    def get_text(self) -> str:
        return self._text

    def get_image_info(self) -> list[dict[str, Any]]:
        return []

    def get_texttrace(self) -> list[dict[str, Any]]:
        return []

    def get_drawings(self) -> list[dict[str, Any]]:
        return []


@pytest.mark.parametrize(
    ("text", "garbled"),
    [
        (" 正常文字" * 10, True),  # 3 of 8 non-space chars: 37 %
        ("(cid:12)(cid:7)" * 10 + "readable text " * 4, True),
        ("�" * 20 + "完全正常的一段文字" * 10, False),  # 20 of 110: 18 %
    ],
)
def test_garbled_text_layer_is_recognised(text: str, garbled: bool) -> None:
    page = pages_module._decide(_GarbledPage(text))
    assert (page.reason is PageReason.GARBLED_TEXT) is garbled
    assert (page.decision is PageDecision.OCR) is garbled


def test_text_only_pdf_parses_exactly_as_before() -> None:
    raw = _pdf(_text_page, _blank_page, _small_logo_page)
    analysis = analyse_pdf("doc.pdf", raw)
    assert analysis.ocr_pages == ()
    assert analysis.markdown == parse_document("doc.pdf", raw)


def test_mixed_pdf_keeps_each_text_page_and_leaves_scans_to_recognition() -> None:
    raw = _pdf(_text_page, _scanned_page, _text_page)
    analysis = analyse_pdf("mixed.pdf", raw)
    assert analysis.markdown is None
    assert [page.index for page in analysis.ocr_pages] == [1]
    assert analysis.pages[1].text == ""
    for index in (0, 2):
        assert "Submit a request three working days ahead" in analysis.pages[index].text


def test_scanned_pdf_with_no_text_needs_recognition_instead_of_failing() -> None:
    # Before B-209 this was ``parse_failed`` (the whole document parsed empty).
    raw = _pdf(_scanned_page, _scanned_page)
    with pytest.raises(DocumentParseError):
        parse_document("scan.pdf", raw)
    analysis = analyse_pdf("scan.pdf", raw)
    assert [page.index for page in analysis.ocr_pages] == [0, 1]


def test_a_corrupt_pdf_is_an_invalid_file() -> None:
    with pytest.raises(DocumentParseError) as caught:
        analyse_pdf("broken.pdf", b"%PDF-1.7 not really")
    assert caught.value.code is IngestErrorCode.INVALID_FILE


def _size(jpeg: bytes) -> tuple[int, int]:
    image = Image.open(io.BytesIO(jpeg))
    assert image.format == "JPEG"
    return image.size


def test_render_caps_the_long_edge_at_2000_px() -> None:
    # A4 at 200 DPI would be 1654 x 2339 px.
    (jpeg,) = render_pages(_pdf(_scanned_page), [0])
    width, height = _size(jpeg)
    assert height == RENDER_MAX_EDGE_PX
    assert width == pytest.approx(RENDER_MAX_EDGE_PX * 595 / 842, abs=2)


def test_render_uses_200_dpi_below_the_cap() -> None:
    doc = pymupdf.open()
    doc.new_page(width=288, height=360)  # 4 x 5 inches
    (jpeg,) = render_pages(bytes(doc.tobytes()), [0])
    assert _size(jpeg) == (800, 1000)


def test_render_honours_a_model_pixel_cap() -> None:
    (jpeg,) = render_pages(_pdf(_scanned_page), [0], max_pixels=1_000_000)
    width, height = _size(jpeg)
    assert width * height <= 1_000_000 * 1.01
    assert render_params(1_000_000) != render_params()


def test_render_params_name_every_setting() -> None:
    assert render_params() == "dpi=200;edge=2000;jpeg=85"
