"""B-301 PR 4 —— 扫描页方向小模型本身:模型文件、预处理、角度约定、把握线,及设计 §9.4 的 D1 / D2 /
D3 / D5(合成图,不调付费模型)。

* D1 / D2 / D3 的页现场合成(:mod:`tests.orientation_pages`,PyMuPDF 自带字体,任何机器上画得
  一样);及格线按 2026-11-12 在这套合成页上实测的数写死(见各用例)。
* D5 的 11 张图来自开工前的合成集(``orientation_fixtures/``:短边 256 的渲染图的中间 256 见方,
  与整张图喂给模型时裁出的 224 见方逐像素相同;没有照片),基准是 Paddle 原版模型的输出。
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from pathlib import Path

import numpy as np
import pymupdf
import pytest
from PIL import Image
from pymupdf import mupdf

from control_plane.knowledge.parsed_size import analyse_pdf_within_cap
from control_plane.knowledge.recognition import orientation
from control_plane.knowledge.recognition.orientation import (
    FAILED,
    MODEL_FILE,
    MODEL_SHA256,
    THRESHOLD,
    OnnxPageClassifier,
    Orientation,
    OrientationModelError,
    classify_page,
    preprocess,
    reading_suffix,
    small_render,
)
from control_plane.knowledge.recognition.pages import analyse_pdf, render_pages
from expert_work.protocol import PageDecision
from tests import orientation_pages

_FIXTURES = Path(__file__).with_name("orientation_fixtures")


@pytest.fixture(scope="module")
def classifier() -> OnnxPageClassifier:
    return OnnxPageClassifier()


def _classify(classifier: OnnxPageClassifier, picture: Image.Image) -> Orientation:
    """Classify ``picture`` the way an ingest does: as a scanned PDF page, drawn small."""
    doc = pymupdf.open(stream=orientation_pages.scanned_pdf([picture]), filetype="pdf")
    try:
        return classify_page(doc[0], classifier)
    finally:
        doc.close()


# -- the model file -------------------------------------------------------------------------


def test_the_model_file_is_the_pinned_unmodified_official_export() -> None:
    data = MODEL_FILE.read_bytes()
    assert len(data) == 6_788_069
    assert hashlib.sha256(data).hexdigest() == MODEL_SHA256
    assert MODEL_SHA256 == "af9a0a4f317ff0709ce752067807f819cb15d883f8ecad89f28df1c6ee2d9c92"
    folder = MODEL_FILE.parent
    assert "Apache License" in (folder / "LICENSE").read_text()
    notice = (folder / "NOTICE").read_text()
    for fact in (
        MODEL_SHA256,
        "7330ab7039123e46af2dc03154b9969aa412c61d",  # the ONNX repository's commit
        "d3b95a6dff5fe8a94f2748e12b61cb26818a0df8",  # the Paddle weights' commit
        "UNMODIFIED",
        "Apache License, Version 2.0",
    ):
        assert fact in notice


def test_the_image_build_loads_the_model_and_runs_one_inference() -> None:
    # Fail loud at build time (as the tokenizer files are fetched there): a model left
    # out of the package, with another hash or that will not run fails the build.
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    _, builder, runtime = dockerfile.split("\nFROM ")  # both stages: built, then copied
    for stage in (builder, runtime):
        (check,) = [line for line in stage.splitlines() if "OnnxPageClassifier()" in line]
        assert check.startswith("RUN /app/.venv/bin/python -c ")
        assert ".classify(" in check and "assert " in check


def test_a_model_file_that_is_not_the_pinned_one_is_refused(tmp_path: Path) -> None:
    data = bytearray(MODEL_FILE.read_bytes())
    data[-1] ^= 0xFF
    tampered = tmp_path / "model.onnx"
    tampered.write_bytes(bytes(data))
    with pytest.raises(OrientationModelError, match="expected"):
        OnnxPageClassifier(tampered)
    with pytest.raises(OrientationModelError, match="not readable"):
        OnnxPageClassifier(tmp_path / "missing.onnx")


def test_the_session_is_single_threaded_and_preallocates_nothing(
    classifier: OnnxPageClassifier,
) -> None:
    options = classifier._session.get_session_options()
    assert (options.intra_op_num_threads, options.inter_op_num_threads) == (1, 1)
    assert not options.enable_mem_pattern
    assert not options.enable_cpu_mem_arena
    assert classifier._session.get_providers() == ["CPUExecutionProvider"]


def test_a_process_whose_model_will_not_load_fails_every_page_and_tries_again_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The file on disk is not the one pinned (a swapped / damaged model): refused.
    monkeypatch.setattr(orientation, "_classifier", None)
    monkeypatch.setattr(orientation, "MODEL_SHA256", "0" * 64)
    unavailable = orientation.process_classifier()
    image = np.full((362, 256, 3), 255, np.uint8)
    with pytest.raises(OrientationModelError, match="expected 0000"):
        unavailable.classify(image)
    assert not orientation.session_loaded()  # not kept: the next document tries again
    monkeypatch.setattr(orientation, "MODEL_SHA256", MODEL_SHA256)
    assert isinstance(orientation.process_classifier(), OnnxPageClassifier)
    assert orientation.session_loaded()


def test_a_model_that_will_not_load_fails_every_page_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(orientation, "_classifier", None)
    monkeypatch.setattr(orientation, "MODEL_SHA256", "0" * 64)
    raw = orientation_pages.scanned_pdf(
        [orientation_pages.turned(orientation_pages.document("en", i), 90) for i in range(3)]
    )
    with caplog.at_level("WARNING", logger=orientation.logger.name):
        analysis = analyse_pdf_within_cap("a.pdf", raw, 10_000_000, 1000)
    assert [page.orientation for page in analysis.pages] == [FAILED] * 3
    warnings = [r for r in caplog.records if r.name == orientation.logger.name]
    assert [r.getMessage() for r in warnings] == ["knowledge.page_orientation_model_unavailable"]


# -- preprocessing and the angle convention --------------------------------------------------


def test_preprocessing_takes_the_centre_square_of_the_short_side_256_render() -> None:
    image = np.zeros((363, 256, 3), np.uint8)
    image[(363 - 224) // 2 : (363 - 224) // 2 + 224, 16:240] = 255
    tensor = preprocess(image)
    assert tensor.shape == (1, 3, 224, 224) and tensor.dtype == np.float32
    white = (1 - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225])
    assert np.allclose(tensor[0, :, 0, 0], white, atol=1e-5)
    assert np.allclose(tensor[0, :, -1, -1], white, atol=1e-5)
    # Not drawn at 256: scaled first (short side to 256), then the same crop.
    assert preprocess(np.zeros((726, 512, 3), np.uint8)).shape == (1, 3, 224, 224)


def test_the_small_render_has_a_short_side_of_256() -> None:
    picture = orientation_pages.turned(orientation_pages.document("en", 0), 90)
    doc = pymupdf.open(stream=orientation_pages.scanned_pdf([picture]), filetype="pdf")
    rendered = small_render(doc[0])
    doc.close()
    assert min(rendered.shape[:2]) == 256 and rendered.shape[2] == 3


def _whole_page(page: pymupdf.Page) -> np.ndarray:
    """The page drawn whole at a short side of 256 (what the model's input was cut from
    when the baseline was made)."""
    zoom = 256 / min(page.rect.width, page.rect.height)
    pixmap = page.get_pixmap(
        matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csRGB
    )
    return np.frombuffer(pixmap.samples, np.uint8).reshape(pixmap.height, pixmap.width, 3)


def _page_of(
    picture: Image.Image, width: float, height: float
) -> tuple[pymupdf.Document, pymupdf.Page]:
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    out = io.BytesIO()
    picture.save(out, format="JPEG")
    page.insert_image(page.rect, stream=out.getvalue(), keep_proportion=False)
    return doc, page


@pytest.mark.parametrize(("width", "height"), [(595, 842), (842, 595), (390, 690), (1376, 1110)])
def test_a_page_of_ordinary_shape_is_drawn_small_exactly_as_whole(
    width: float, height: float
) -> None:
    doc, page = _page_of(orientation_pages.document("zh", 3), width, height)
    small = small_render(page)
    assert np.array_equal(small, _whole_page(page))
    doc.close()


def test_a_long_thin_page_is_drawn_only_around_its_centre(classifier: OnnxPageClassifier) -> None:
    # Drawn whole, this strip would be 256 x 4,096 pixels; the model only ever looks
    # at the centre 224 square of it.
    doc, page = _page_of(
        orientation_pages.turned(orientation_pages.document("en", 2), 90), 1600, 100
    )
    small, whole = small_render(page), _whole_page(page)
    assert small.shape == (256, 256, 3) and whole.shape == (256, 4096, 3)
    centre = whole[:, (4096 - 256) // 2 : (4096 + 256) // 2]
    assert np.abs(centre.astype(int) - small.astype(int)).mean() < 1  # same pixels, near enough
    assert classifier.classify(small).angle == classifier.classify(whole).angle
    doc.close()


@pytest.mark.parametrize("clockwise", [90, 180, 270])
def test_a_turned_page_renders_upright(classifier: OnnxPageClassifier, clockwise: int) -> None:
    # The model's label L means the content is turned L degrees clockwise; the
    # render undoes it (``prerotate(-L)``): the page then reads upright, and looks
    # like the upright original.
    upright = orientation_pages.document("form", 1)
    raw = orientation_pages.scanned_pdf([upright, orientation_pages.turned(upright, clockwise)])
    found = _classify(classifier, orientation_pages.turned(upright, clockwise))
    assert found.rotation == clockwise
    original, fixed = (
        np.asarray(Image.open(_jpeg(image)).convert("L"), np.float32)
        for image in render_pages(raw, [0, 1], None, {1: found.rotation})
    )
    assert original.shape == fixed.shape
    assert np.abs(original - fixed).mean() < 8  # same page (JPEG / resampling aside)
    wrong = render_pages(raw, [1], None, {1: (found.rotation + 180) % 360})[0]
    assert (
        np.abs(original - np.asarray(Image.open(_jpeg(wrong)).convert("L"), np.float32)).mean() > 8
    )
    turned_back = _classify(
        classifier, Image.open(_jpeg(render_pages(raw, [1], None, {1: clockwise})[0]))
    )
    assert turned_back.angle == 0


def _jpeg(data: bytes) -> io.BytesIO:
    return io.BytesIO(data)


# -- the threshold ----------------------------------------------------------------------------


class _Says:
    """A fake classifier that says the same about every page."""

    def __init__(self, angle: int, confidence: float) -> None:
        self.answer = Orientation(angle=angle, confidence=confidence, model="fake-model")
        self.calls = 0

    def classify(self, image: np.ndarray) -> Orientation:
        assert min(image.shape[:2]) == 256
        self.calls += 1
        return self.answer


def _scan(*pictures: Image.Image) -> bytes:
    return orientation_pages.scanned_pdf(list(pictures))


def test_a_page_is_turned_from_a_confidence_of_0_90_not_below() -> None:
    raw = _scan(orientation_pages.document("zh", 1))
    below = analyse_pdf("a.pdf", raw, _Says(90, 0.89)).pages[0]
    at = analyse_pdf("a.pdf", raw, _Says(90, 0.90)).pages[0]
    assert below.orientation == Orientation(90, 0.89, "fake-model")
    assert (below.rotation, at.rotation) == (0, 90)
    assert THRESHOLD == 0.9
    assert reading_suffix(below.orientation) == ""
    assert reading_suffix(at.orientation) == ";rotate=90;orientation_model=fake-model"


def test_only_pages_to_recognize_are_asked() -> None:
    text = pymupdf.open()
    text.new_page().insert_text((72, 72), "A page with a text layer of its own. " * 6, fontsize=9)
    scan = pymupdf.open(stream=_scan(orientation_pages.document("en", 1)), filetype="pdf")
    text.insert_pdf(scan)
    says = _Says(180, 0.93)
    analysis = analyse_pdf("mixed.pdf", bytes(text.tobytes()), says)
    assert [p.decision for p in analysis.pages] == [PageDecision.TEXT, PageDecision.OCR]
    assert [p.orientation for p in analysis.pages] == [None, says.answer]
    assert says.calls == 1
    assert analysis.rotations() == {1: 180}


class _Breaks:
    def classify(self, image: np.ndarray) -> Orientation:
        raise RuntimeError("inference failed")


def test_a_classifier_that_fails_leaves_the_page_as_it_is() -> None:
    analysis = analyse_pdf("a.pdf", _scan(orientation_pages.document("zh", 2)), _Breaks())
    assert analysis.pages[0].decision is PageDecision.OCR
    assert analysis.pages[0].orientation == FAILED
    assert analysis.rotations() == {}


def test_with_the_switch_off_the_worker_asks_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden() -> None:
        raise AssertionError("the classifier was asked")

    monkeypatch.setattr("control_plane.knowledge.parsed_size.process_classifier", forbidden)
    raw = _scan(orientation_pages.turned(orientation_pages.document("zh", 1), 90))
    analysis = analyse_pdf_within_cap("a.pdf", raw, 10_000_000, 0)
    assert [p.orientation for p in analysis.pages] == [None]
    assert orientation.page_orientation_enabled()  # the platform default is on


def test_the_platform_switch_is_on_by_default_and_one_env_var_turns_it_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from control_plane.settings import Settings

    monkeypatch.delenv("EXPERT_WORK_KNOWLEDGE_SCAN_PAGE_ORIENTATION", raising=False)
    assert Settings().knowledge_scan_page_orientation is True
    monkeypatch.setenv("EXPERT_WORK_KNOWLEDGE_SCAN_PAGE_ORIENTATION", "false")
    assert Settings().knowledge_scan_page_orientation is False


def _store_size() -> int:
    """Bytes held in PyMuPDF's resource store (decoded images and the like)."""
    buffer = mupdf.fz_new_buffer(1024)
    out = mupdf.FzOutput(buffer)
    out.fz_debug_store()
    out.fz_close_output()
    dump = mupdf.fz_buffer_extract(buffer).decode(errors="replace")
    return int(re.findall(r"size=(\d+), actual size", dump)[-1])


def test_the_small_renders_leave_nothing_in_pymupdfs_store() -> None:
    # Measured 2026-11-12 on a 47 MiB scanned PDF: without dropping them, the small
    # renders' decoded images raised the parse worker's peak by 89~105 MiB.
    pictures = [orientation_pages.turned(orientation_pages.document("zh", i), 90) for i in range(3)]
    raw = _scan(*(p.resize((p.width * 3, p.height * 3)) for p in pictures))  # 300 DPI scans
    pymupdf.TOOLS.store_shrink(100)
    analyse_pdf("a.pdf", raw)
    without = _store_size()
    pymupdf.TOOLS.store_shrink(100)
    analyse_pdf("a.pdf", raw, _Says(90, 0.95))
    with_orientation = _store_size()
    pymupdf.TOOLS.store_shrink(100)
    assert with_orientation <= without


# -- §9.4 acceptance lines (synthetic pages, the real model) ------------------------------------

#: D1 —— turned to the right angle at confidence ≥ 0.9, out of 4 pages x 3 angles per kind,
#: measured on these synthetic pages 2026-11-12 (the 0 wrong angles are the hard line). The
#: pre-work set (other fonts) measured zh 12, en 12, table 11, form 12, vertical 5: pages
#: of vertical Chinese columns are this model's weak spot — the miss is "not turned",
#: never "turned wrong".
_D1_TURNED_RIGHT = {"zh": 12, "en": 12, "table": 10, "form": 12, "vertical": 9}


@pytest.mark.parametrize("kind", list(orientation_pages.DOCUMENTS))
def test_d1_turned_document_pages_are_turned_to_the_right_angle(
    classifier: OnnxPageClassifier, kind: str
) -> None:
    right, wrong = 0, []
    for seed in range(4):
        upright = orientation_pages.document(kind, seed)
        for clockwise in (90, 180, 270):
            found = _classify(classifier, orientation_pages.turned(upright, clockwise))
            if found.rotation == clockwise:
                right += 1
            elif found.rotation:
                wrong.append((seed, clockwise, found))
    assert wrong == []
    assert right >= _D1_TURNED_RIGHT[kind]


@pytest.mark.parametrize("kind", list(orientation_pages.DOCUMENTS))
def test_d2_upright_document_pages_are_not_turned(
    classifier: OnnxPageClassifier, kind: str
) -> None:
    turned = [
        (seed, found)
        for seed in range(12)
        if (found := _classify(classifier, orientation_pages.document(kind, seed))).rotation
    ]
    assert turned == []


@pytest.mark.parametrize("kind", list(orientation_pages.NONTEXT))
def test_d3_upright_pages_that_are_not_text_are_not_turned(
    classifier: OnnxPageClassifier, kind: str
) -> None:
    # 290 pages over the 12 kinds (photos are generated colour fields, not photographs).
    # Measured 2026-11-12: 0 turned; the highest confidence on a wrong angle was 0.884.
    _, count = orientation_pages.NONTEXT[kind]
    turned = [
        (seed, found)
        for seed in range(count)
        if (found := _classify(classifier, orientation_pages.nontext(kind, seed))).rotation
    ]
    assert turned == []


def test_d3_covers_at_least_200_pages() -> None:
    assert sum(count for _, count in orientation_pages.NONTEXT.values()) >= 200


def _baseline() -> list[dict[str, str]]:
    with (_FIXTURES / "paddle_baseline.csv").open(newline="") as fh:
        return list(csv.DictReader(fh))


def test_d5_the_onnx_export_agrees_with_the_original_paddle_model(
    classifier: OnnxPageClassifier,
) -> None:
    rows = _baseline()
    assert len(rows) == 11
    decisions: Counter[bool] = Counter()
    for row in rows:
        image = np.asarray(Image.open(_FIXTURES / f"{row['id']}.png").convert("RGB"))
        found = classifier.classify(image)
        paddle_angle, paddle_confidence = int(row["paddle_angle"]), float(row["paddle_confidence"])
        assert found.angle == paddle_angle, row["id"]
        same_side = (found.confidence >= THRESHOLD) == (paddle_confidence >= THRESHOLD)
        assert same_side or abs(found.confidence - paddle_confidence) <= 0.001, row["id"]
        decisions[found.rotation != 0] += 1
    # The set straddles the line: pages turned, pages kept at 0.874 / 0.898 / 0.8998,
    # and wrong angles below it (0.843, 0.887).
    assert decisions[True] == 4 and decisions[False] == 7
