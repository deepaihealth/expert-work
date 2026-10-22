"""B-209 PR-2 §5.4 —— 取图:Word / 幻灯片(表格里的图、重复图标、WMF、图片大小上限)、
PDF 文字页(只读元数据不解码、像素上限、短名单、按位置落图、双栏、从上到下)、扫描页按区域
裁图(真实 glm-ocr 回答的坐标),以及正文里 base64 不外泄、找图的扫描是线性的。

这些函数都在解析进程池里跑;这里直接调(不起进程池、不联网)。文件现场合成。
"""

from __future__ import annotations

import base64
import io
import time
import zipfile
from typing import Any
from uuid import uuid4

import pymupdf
import pytest
from PIL import Image

from control_plane.knowledge.parsing import (
    DocumentParseError,
    find_data_uri_pictures,
    parse_document,
    scrub_data_uris,
    strip_picture_markup,
)
from control_plane.knowledge.recognition import figure_extract, figure_images, figure_pdf
from control_plane.knowledge.recognition.clients import LayoutParsingClient
from control_plane.knowledge.recognition.figure_extract import (
    bound_office_media,
    parse_office_figures,
)
from control_plane.knowledge.recognition.figure_filters import MAX_CANDIDATES
from control_plane.knowledge.recognition.figure_markup import (
    figure_numbers,
    quote_block,
    resolve_figures,
)
from control_plane.knowledge.recognition.figure_pdf import (
    collect_pdf_figures,
    insert_at_positions,
    place_figures,
)
from control_plane.knowledge.recognition.figure_scan import crop_scanned_figures
from control_plane.knowledge.recognition.pages import render_pages
from control_plane.knowledge.recognition.recognizer import (
    DocumentRecognizer,
    ModelChoice,
    RecognitionSettings,
)
from expert_work.persistence import InMemoryKnowledgeStore
from tests.figure_test_images import flat_png, noise_png
from tests.glm_ocr_recorded import CHART_BOX, PAGE_SIZE, PDF_PAGE_POINTS, RESPONSE, page_image
from tests.knowledge_form_fixtures import _DOCX_DRAWING, _docx, picture_docx, picture_pptx

_BLOCK = quote_block("0123456789abcdef", "A chair photo.")


def _cell(text: str) -> str:
    return f"<w:tc><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"


def _table_docx(picture: bytes) -> bytes:
    rows = (
        "<w:tbl>"
        f"<w:tr>{_cell('Name')}{_cell('Photo')}{_cell('Price')}</w:tr>"
        f"<w:tr>{_cell('Chair')}<w:tc>{_DOCX_DRAWING}</w:tc>{_cell('100')}</w:tr>"
        f"<w:tr>{_cell('Desk')}{_cell('-')}{_cell('200')}</w:tr>"
        "</w:tbl>"
    )
    return _docx(rows + "<w:p><w:r><w:t>After the table.</w:t></w:r></w:p>", image=picture)


def _table_rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("|")]


# ---------------------------------------------------------------------------
# Word / slides
# ---------------------------------------------------------------------------


def test_word_pictures_become_placeholders_and_the_markup_is_gone() -> None:
    raw = picture_docx(noise_png(500, 400), before="设备说明", after="以上是外观")
    parsed = parse_office_figures(raw, ".docx")
    assert parsed.figures.extracted == 1 and len(parsed.figures.candidates) == 1
    assert "data:image" not in parsed.markdown
    figure = parsed.figures.candidates[0]
    assert (figure.width, figure.height) == (500, 400)
    text = resolve_figures(parsed.markdown, {figure.number: "> 图"})
    assert text.index("设备说明") < text.index("> 图") < text.index("以上是外观")


@pytest.mark.parametrize("described", [True, False], ids=["described", "pending"])
def test_a_picture_in_a_table_cell_keeps_the_table_whole(described: bool) -> None:
    # Review H1: the cell gets an in-line reference, the quote block goes after
    # the table; with nothing described the table reads as it would without it.
    parsed = parse_office_figures(_table_docx(noise_png(500, 400)), ".docx")
    (figure,) = parsed.figures.candidates
    text = resolve_figures(parsed.markdown, {figure.number: _BLOCK} if described else {})
    rows = _table_rows(text)
    assert len(rows) == 5  # header, separator and three rows: nothing split out
    assert all(row.count("|") == 4 for row in rows)
    chair = next(row for row in rows if "Chair" in row)
    if described:
        assert "\u3014图 0123456789abcdef\u3015" in chair
        assert text.index("| Desk") < text.index(_BLOCK) < text.index("After the table.")
    else:
        assert "\u3014" not in text
        assert chair.replace(" ", "") == "|Chair||100|"


def test_word_without_the_figure_step_drops_pictures_not_markup() -> None:
    raw = picture_docx(noise_png(500, 400), before="设备说明", after="以上是外观")
    markdown = parse_document("说明.docx", raw)
    assert "data:image" not in markdown and "![" not in markdown
    assert "设备说明" in markdown and "以上是外观" in markdown


@pytest.mark.parametrize(
    "alt",
    ["x" * 2100, "a ](b) c", "A picture containing text&#xA;&#xA;Description generated"],
    ids=["long-alt", "bracket-alt", "multiline-alt"],
)
def test_no_base64_ever_reaches_the_text(alt: str) -> None:
    # Review M1: alt text the picture patterns cannot read must not let the
    # base64 bytes through — with or without the figure step.
    drawing = _DOCX_DRAWING.replace(
        '<wp:docPr id="1" name="scan"/>', f'<wp:docPr id="1" name="scan" descr="{alt}"/>'
    )
    picture = noise_png(300, 200)
    raw = _docx(f"<w:p><w:r><w:t>Before</w:t></w:r></w:p>{drawing}", image=picture)
    payload = base64.b64encode(picture).decode()[:64]
    for text in (parse_document("a.docx", raw), parse_office_figures(raw, ".docx").markdown):
        assert payload not in text


def test_scrubbing_leaves_short_or_plain_text_alone() -> None:
    text = "see data:image/png;base64,QUJD and https://example.com/a.png"
    assert scrub_data_uris(text) == text
    long = "x ![a](data:image/png;base64," + "A" * 200 + ") y"
    assert "AAAA" not in scrub_data_uris(long)


def test_finding_pictures_is_linear() -> None:
    # Security review S-L5: no backtracking blow-up on hostile alt text.
    hostile = [
        "![" * 400_000,
        "](data:" * 300_000,
        ("![" + "a" * 1999 + "](data:") * 2_000,
        "](" * 500_000,
        "![](data:" * 20_000,  # many pictures: no pairing of each with every opener
    ]
    started = time.perf_counter()
    for text in hostile:
        find_data_uri_pictures(text)
        strip_picture_markup(text)
    assert time.perf_counter() - started < 2.0


def test_a_picture_referenced_three_times_is_a_repeated_icon() -> None:
    raw = picture_docx(noise_png(200, 200), before="一", after="二", repeat=3)
    parsed = parse_office_figures(raw, ".docx")
    assert parsed.figures.extracted == 1
    assert parsed.figures.filtered == 1 and parsed.figures.candidates == ()
    assert "\ue000" not in parsed.markdown


def test_small_and_tiny_pictures_are_filtered() -> None:
    parsed = parse_office_figures(picture_docx(flat_png(600, 600), before="正文"), ".docx")
    assert parsed.figures.filtered == 1 and parsed.figures.candidates == ()


def test_a_slide_wmf_is_skipped_and_counted() -> None:
    parsed = parse_office_figures(picture_pptx(noise_png(500, 400), extra_wmf=True), ".pptx")
    assert parsed.figures.extracted == 2
    assert parsed.figures.unsupported == 1
    assert len(parsed.figures.candidates) == 1
    assert "data:" not in parsed.markdown


def test_oversized_package_pictures_never_reach_markitdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Security review S-H2: pictures over the per-picture / per-file limits are
    # replaced before MarkItDown base64-expands them (limits lowered here).
    big, small = noise_png(400, 300, seed=1), noise_png(200, 150, seed=2)
    monkeypatch.setattr(figure_extract, "MAX_IMAGE_BYTES", len(big) - 1)
    raw = picture_docx(big, before="Intro")
    bounded, skipped = bound_office_media(raw)
    assert skipped == 1
    with zipfile.ZipFile(io.BytesIO(bounded)) as package:
        assert package.read("word/media/image1.png") != big
    parsed = parse_office_figures(raw, ".docx")
    assert (parsed.figures.extracted, parsed.figures.unsupported) == (1, 1)
    assert parsed.figures.candidates == ()
    # The total budget: the largest pictures go first.
    monkeypatch.setattr(figure_extract, "MAX_IMAGE_BYTES", 10 * len(big))
    monkeypatch.setattr(figure_extract, "MAX_OFFICE_MEDIA_BYTES", len(small))
    _, skipped = bound_office_media(picture_docx(big))
    assert skipped == 1
    assert bound_office_media(picture_docx(small)) == (picture_docx(small), 0)
    assert bound_office_media(b"not a zip") == (b"not a zip", 0)


def _moved_picture(raw: bytes, extra: dict[str, bytes] | None = None) -> bytes:
    """``raw`` (a one-picture Word file) with its picture moved out of
    ``word/media/`` (the relationship pointing at the new place), plus
    ``extra`` parts."""
    source = zipfile.ZipFile(io.BytesIO(raw))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as package:
        for item in source.infolist():
            data = source.read(item)
            name = item.filename
            if name == "word/media/image1.png":
                name = "word/elsewhere/image1.png"
            elif name == "word/_rels/document.xml.rels":
                data = data.replace(b"media/image1.png", b"elsewhere/image1.png")
            package.writestr(name, data)
        for name, data in (extra or {}).items():
            package.writestr(name, data)
    return out.getvalue()


def test_a_picture_outside_the_media_folder_is_bounded_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Re-review M1: relationships can point anywhere in the package — every
    # part but the markup is bounded by its declared size.
    big = noise_png(400, 300, seed=1)
    monkeypatch.setattr(figure_extract, "MAX_IMAGE_BYTES", len(big) - 1)
    raw = _moved_picture(
        picture_docx(big, before="Intro"), {"word/embeddings/blob.bin": b"\0" * len(big)}
    )
    bounded, pictures = bound_office_media(raw)
    assert pictures == 1  # the .bin part is replaced too, but is no picture
    with zipfile.ZipFile(io.BytesIO(bounded)) as package:
        assert package.read("word/elsewhere/image1.png") != big
        assert package.read("word/embeddings/blob.bin") != b"\0" * len(big)
        assert package.read("word/document.xml") == zipfile.ZipFile(io.BytesIO(raw)).read(
            "word/document.xml"
        )
    parsed = parse_office_figures(raw, ".docx")
    assert (parsed.figures.extracted, parsed.figures.unsupported) == (1, 1)
    assert "Intro" in parsed.markdown


def test_a_package_that_unpacks_too_large_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = picture_docx(noise_png(400, 300), before="Intro")
    monkeypatch.setattr(figure_extract, "MAX_OFFICE_PACKAGE_BYTES", 10_000)
    with pytest.raises(DocumentParseError, match="unpacks"):
        bound_office_media(raw)


# ---------------------------------------------------------------------------
# PDF text pages
# ---------------------------------------------------------------------------


def _pdf(*pages: list[tuple[str, Any]]) -> bytes:
    """Each page: ``("text", (x, y, words))`` / ``("image", (rect, png))``."""
    doc = pymupdf.open()
    for items in pages:
        page = doc.new_page()
        for kind, spec in items:
            if kind == "text":
                x, y, words = spec
                page.insert_text((x, y), words, fontsize=11)
            else:
                rect, png = spec
                page.insert_image(pymupdf.Rect(rect), stream=png)
    return bytes(doc.tobytes())


def _placed(raw: bytes, texts: dict[int, str]) -> tuple[figure_pdf.PdfFigures, dict[int, str]]:
    found = collect_pdf_figures(raw, sorted(texts))
    return found, place_figures(texts, found)


def test_a_pdf_figure_is_placed_between_the_text_above_and_below_it() -> None:
    raw = _pdf(
        [
            ("text", (72, 80, "Opening paragraph above the picture.")),
            ("image", ((72, 140, 372, 365), noise_png(600, 450))),
            ("text", (72, 420, "Closing paragraph below the picture.")),
        ]
    )
    text = "Opening paragraph above the picture.\n\nClosing paragraph below the picture.\n"
    found, placed = _placed(raw, {0: text})
    (figure,) = found.figures.candidates
    assert (figure.width, figure.height, figure.pages) == (600, 450, (1,))
    resolved = resolve_figures(placed[0], {figure.number: "> FIGURE"})
    assert resolved.index("Opening") < resolved.index("> FIGURE") < resolved.index("Closing")
    assert resolve_figures(placed[0], {}) == text  # nothing described: unchanged


def test_two_figures_in_one_gap_read_top_to_bottom() -> None:
    raw = _pdf(
        [
            ("text", (72, 80, "Words above both pictures on this page.")),
            ("image", ((72, 300, 372, 500), noise_png(600, 400, seed=2))),  # lower, drawn first
            ("image", ((72, 100, 372, 280), noise_png(600, 360, seed=3))),
            ("text", (72, 560, "Words below both pictures.")),
        ]
    )
    text = "Words above both pictures on this page.\n\nWords below both pictures.\n"
    found, placed = _placed(raw, {0: text})
    upper, lower = sorted(found.figures.candidates, key=lambda c: c.height)  # 360 < 400
    order = figure_numbers(placed[0])
    assert order == [upper.number, lower.number]


def test_a_figure_in_the_right_column_follows_the_right_column_text() -> None:
    # Review M6: the block above the picture in its own column, not the
    # other column's last block.
    blocks = [
        (60.0, 80.0, 40.0, 280.0, "Left column first paragraph."),
        (60.0, 80.0, 320.0, 560.0, "Right column opening words."),
        # Lower on the page than the right column's block, still above the picture.
        (84.0, 96.0, 40.0, 280.0, "Left column second paragraph."),
        (500.0, 520.0, 40.0, 280.0, "Left column far down the page."),
    ]
    text = (
        "Left column first paragraph.\n\nLeft column second paragraph.\n\n"
        "Left column far down the page.\n\n"
        "Right column opening words.\n\nRight column second paragraph.\n"
    )
    placed = insert_at_positions(text, blocks, [(100.0, 320.0, 560.0, 1)])
    resolved = resolve_figures(placed, {1: "> F"})
    assert resolved.index("Right column opening") < resolved.index("> F")
    assert resolved.index("> F") < resolved.index("Right column second")


def test_insertion_falls_back_to_the_share_of_text_above() -> None:
    text = "Alpha paragraph.\n\nBeta paragraph.\n\nGamma paragraph."
    blocks = [(0.0, 10.0, 0.0, 500.0, "zzzz"), (20.0, 30.0, 0.0, 500.0, "yyyy")]
    blocks.append((40.0, 50.0, 0.0, 500.0, "xxxx"))
    placed = insert_at_positions(text, blocks, [(35.0, 0.0, 500.0, 1)])
    resolved = resolve_figures(placed, {1: "> F"})
    assert resolved.index("Beta") < resolved.index("> F") < resolved.index("Gamma")


def test_a_logo_on_three_pages_is_not_a_figure() -> None:
    logo = noise_png(300, 120)
    pages = [
        [
            ("text", (72, 200, f"Page {n} of the letter: " + "words of the letter " * 6)),
            ("image", ((72, 30, 252, 102), logo)),
        ]
        for n in range(3)
    ]
    found = collect_pdf_figures(_pdf(*pages), [0, 1, 2])
    assert (found.figures.extracted, found.figures.filtered) == (1, 1)
    assert found.figures.candidates == ()


def _separate_objects(*pages: list[tuple[str, Any]]) -> bytes:
    """Like :func:`_pdf`, but every page made on its own and then joined, so
    the same picture is a separate image object on each page."""
    joined = pymupdf.open()
    for items in pages:
        joined.insert_pdf(pymupdf.open(stream=_pdf(items), filetype="pdf"))
    return bytes(joined.tobytes())


def test_a_logo_stored_once_per_page_is_still_a_repeated_logo() -> None:
    logo = noise_png(300, 120)
    raw = _separate_objects(
        *(
            [
                ("text", (72, 200, f"Page {n} of the letter: " + "words of the letter " * 6)),
                ("image", ((72, 30, 252, 102), logo)),
            ]
            for n in range(3)
        )
    )
    found = collect_pdf_figures(raw, [0, 1, 2])
    assert found.figures.extracted == 3  # three objects ...
    assert found.figures.candidates == ()  # ... one logo, on three pages


def test_a_repeated_logo_never_takes_a_shortlist_place(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review H3: the per-object filters run before the shortlist — a big logo
    # on every page must not push the one real figure out of it.
    monkeypatch.setattr(figure_pdf, "MAX_CANDIDATES", 1)
    logo = noise_png(500, 400, seed=7)
    pages = [
        [
            ("text", (72, 450, f"Page {n} words of the letter.")),
            ("image", ((72, 30, 372, 270), logo)),
        ]
        for n in range(3)
    ]
    pages[1].append(("image", ((72, 560, 272, 710), noise_png(300, 200, seed=8))))
    found = collect_pdf_figures(_pdf(*pages), [0, 1, 2])
    (figure,) = found.figures.candidates
    assert figure.width == 300 and found.figures.capped == 0


def test_only_a_shortlist_of_office_pictures_is_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review H3 for Word / slides: sizes from the image headers, the
    # shortlist decoded.
    decoded: list[int] = []
    real = figure_images.normalize

    def counting(blob: bytes) -> object:
        decoded.append(len(blob))
        return real(blob)

    monkeypatch.setattr(figure_extract, "MAX_CANDIDATES", 1)
    monkeypatch.setattr(figure_images, "normalize", counting)
    small, big = noise_png(300, 200, seed=1), noise_png(600, 400, seed=2)
    extraction, numbers = figure_extract._office_candidates(
        {"s": (small, "image/png"), "b": (big, "image/png")}, {"s": 1, "b": 1}
    )
    assert decoded == [len(big)]
    assert set(numbers) == {"b"} and (extraction.extracted, extraction.capped) == (2, 1)


def test_huge_images_are_never_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    # Security review S-H1: an image object whose declared size is past the
    # decode ceiling is counted and left alone — no render, no decode.
    rendered: list[object] = []
    monkeypatch.setattr(figure_images, "MAX_DECODE_PIXELS", 599 * 450)
    monkeypatch.setattr(figure_pdf, "_render_clip", lambda doc, image: rendered.append(image))
    raw = _pdf(
        [("text", (72, 80, "Text above.")), ("image", ((72, 140, 372, 365), noise_png(600, 450)))]
    )
    found = collect_pdf_figures(raw, [0])
    assert (found.figures.extracted, found.figures.unsupported) == (1, 1)
    assert rendered == []


def test_only_a_shortlist_of_the_largest_is_rendered(monkeypatch: pytest.MonkeyPatch) -> None:
    # Review H3: filters and the shortlist run on metadata; only the shortlist
    # is rendered (and crosses the process boundary).
    calls: list[int] = []
    real = figure_pdf._render_clip

    def counting(doc: pymupdf.Document, image: figure_pdf._Image) -> object:
        calls.append(image.width)
        return real(doc, image)

    monkeypatch.setattr(figure_pdf, "MAX_CANDIDATES", 3)
    monkeypatch.setattr(figure_pdf, "_render_clip", counting)
    pages = [
        [
            ("text", (72, 60, "Catalogue page words.")),
            ("image", ((72, 100, 372, 330), noise_png(200 + 10 * n, 150, seed=n))),
        ]
        for n in range(6)
    ]
    found = collect_pdf_figures(_pdf(*pages), list(range(6)))
    assert sorted(calls) == [230, 240, 250]
    assert len(found.figures.candidates) == 3 and found.figures.capped == 3
    assert MAX_CANDIDATES == 75  # the shortlist a big document is decoded down to


# ---------------------------------------------------------------------------
# Scanned pages: the vendor's boxes (a recorded glm-ocr answer)
# ---------------------------------------------------------------------------


def _recorded_page_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=PDF_PAGE_POINTS[0], height=PDF_PAGE_POINTS[1])
    page.insert_image(page.rect, stream=page_image())
    return bytes(doc.tobytes())


async def test_a_recorded_glm_ocr_box_crops_the_picture_on_the_page() -> None:
    # Review H4: glm-ocr's boxes are pixels of the image sent (recorded: x up
    # to 1482 on a 1600-px page — not a 0..1000 frame); its page size rides
    # along in the cached answer. The crop is the chart, not text.
    import httpx

    raw = _recorded_page_pdf()
    (rendered,) = render_pages(raw, [0])
    with Image.open(io.BytesIO(rendered)) as sent:
        assert sent.size == PAGE_SIZE
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=RESPONSE))
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="s.pdf")

    class _Settings:
        async def recognition_settings(self) -> RecognitionSettings:
            return RecognitionSettings(
                primary=ModelChoice("glm", "glm-ocr"), fallback=None, max_pages=10
            )

    async def keys(*, tenant_id: object, provider: str) -> str:
        return "key"

    async with httpx.AsyncClient(transport=transport) as http:
        recognizer = DocumentRecognizer(
            store=store,
            settings=_Settings(),
            api_keys=keys,
            client_factory=lambda choice, key: LayoutParsingClient(
                url="https://vendor.test/layout", api_key=key, model=choice.model, http=http
            ),
        )
        plan = await recognizer.plan(tenant_id=tenant)
        outcomes = await recognizer.recognize(
            tenant_id=tenant, document_id=document.id, plan=plan, images={0: rendered}, render="r"
        )
    (box,) = outcomes[0].regions
    width, height = PAGE_SIZE
    x0, y0, x1, y1 = CHART_BOX
    assert x0 / width <= box[0] < box[2] <= x1 / width
    assert y0 / height <= box[1] < box[3] <= y1 / height
    cached = next(iter(store._parse_cache.values()))
    assert cached.result["layout_size"] == [1600, 1200]
    scanned = crop_scanned_figures(raw, {0: list(outcomes[0].regions)}, 1)
    (figure,) = scanned.figures.candidates
    with Image.open(io.BytesIO(figure.image)) as crop:
        pixels: list[tuple[int, int, int]] = list(crop.convert("RGB").getdata())
        blue = sum(1 for r, _, b in pixels if b > 150 and r < 100)
        assert blue > crop.width * crop.height * 0.05  # the bars are in it


def test_a_scanned_region_that_will_not_crop_is_counted_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review M7: one bad region never fails the figures, let alone the document.
    def broken(jpeg: bytes, box: object) -> object:
        raise OSError("bad crop")

    monkeypatch.setattr(figure_images, "crop", broken)
    scanned = crop_scanned_figures(_recorded_page_pdf(), {0: [(0.6, 0.25, 0.95, 0.65)]}, 1)
    assert (scanned.figures.extracted, scanned.figures.unsupported) == (1, 1)
    assert scanned.figures.candidates == ()
