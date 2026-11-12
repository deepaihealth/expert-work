"""B-301 PR 4 —— 知识库扫描页方向:入库路径。

横着 / 倒着扫的页,在逐页判定时由方向小模型判出角度(把握 ≥ 0.9 才转),正式渲染时按这个角度
转正再送识别;插图裁剪用同一个角度;转过的页读法摘要带「角度 + 模型文件哈希」,预估与实付对齐。

页面都是现场合成的(:mod:`tests.orientation_pages`);识别 / 看图模型是假的(不联网)。方向小模型
是真的(仓库里的 ONNX 文件),在真实的解析进程池里跑;要换掉它的用例把解析池换成进程内的单线程池。
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import numpy as np
import pymupdf
import pytest
from PIL import Image, ImageDraw
from prometheus_client import REGISTRY

from control_plane.knowledge import ingestion
from control_plane.knowledge.estimate import IngestEstimator
from control_plane.knowledge.ingestion import (
    KnowledgeIngestionRunner,
    ingest_document_bytes,
    parse_locally,
    shutdown_parse_pool,
)
from control_plane.knowledge.parse_snapshot import decode_snapshot, encode_snapshot
from control_plane.knowledge.parse_workers import FIGURE_POOL, PoolBudget
from control_plane.knowledge.parsed_size import analyse_pdf_within_cap
from control_plane.knowledge.parsing import DocumentParseError
from control_plane.knowledge.recognition import figure_images, orientation
from control_plane.knowledge.recognition.clients import Reading, RecognitionClient
from control_plane.knowledge.recognition.figure_pipeline import FigureDescriber
from control_plane.knowledge.recognition.figure_scan import crop_scanned_figures
from control_plane.knowledge.recognition.figure_storage import FigureStorage
from control_plane.knowledge.recognition.orientation import Orientation, PageClassifier
from control_plane.knowledge.recognition.pages import (
    RENDER_DPI,
    RENDER_JPEG_QUALITY,
    RENDER_MAX_EDGE_PX,
    render_pages,
)
from control_plane.knowledge.recognition.recognizer import (
    DocumentRecognizer,
    ModelChoice,
    RecognitionSettings,
)
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.knowledge.base import ClaimedIngestion
from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    DocumentStatus,
)
from expert_work.protocol.knowledge_estimate import EstimateKind
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.llm import FakeEmbedder
from tests import orientation_pages
from tests.figure_test_images import noise_png

#: What the fake recognition model reads off an upright page.
_SENTENCE = "差旅费用凭发票在出差结束后十个工作日内报销"
#: What it makes of a page that is not upright.
_GARBLE = "乱码 lorem 乱码"
_ROTATED = "expert_work_knowledge_page_orientation_total"
_FAILURES = "expert_work_knowledge_page_orientation_failures_total"


@pytest.fixture(scope="module", autouse=True)
def _parse_pool_teardown() -> Iterator[None]:
    yield
    shutdown_parse_pool()


@pytest.fixture
def in_process_parse(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The parse pool as one thread of this process, so a test can swap the classifier."""
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(ingestion, "_parse_executor", executor)
    monkeypatch.setattr(ingestion, "_parse_slots", None)
    monkeypatch.setattr(orientation, "_classifier", None)
    yield
    executor.shutdown(wait=True)


@pytest.fixture
def switched_off() -> Iterator[None]:
    orientation.configure_page_orientation(enabled=False)
    yield
    orientation.configure_page_orientation(enabled=True)


def _counted(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _marked(picture: Image.Image) -> Image.Image:
    """``picture`` with a small black square in its top-left margin: the fake model's
    way to tell an upright page."""
    marked = picture.copy()
    side = picture.width // 25
    ImageDraw.Draw(marked).rectangle((side, side, 2 * side, 2 * side), fill=(0, 0, 0))
    return marked


def _upright_page(seed: int = 0) -> Image.Image:
    return _marked(orientation_pages.document("zh", seed))


def _turned_pdf(*clockwise: int) -> bytes:
    """One page per angle: the same marked page, turned that many degrees clockwise."""
    return orientation_pages.scanned_pdf(
        [
            orientation_pages.turned(_upright_page(seed), angle)
            for seed, angle in enumerate(clockwise)
        ]
    )


def _is_upright(image: bytes) -> bool:
    picture = Image.open(io.BytesIO(image)).convert("L")
    width, height = picture.size
    corner = picture.crop((0, 0, width // 8, height // 12))
    dark = sum(corner.histogram()[:80]) / (corner.width * corner.height)
    return height > width and dark > 0.05


class _UprightOnlyReader:
    """Reads :data:`_SENTENCE` off an upright page only — the marker in its top-left
    corner, the page taller than wide; anything else reads as :data:`_GARBLE`."""

    def __init__(self) -> None:
        self.images: list[bytes] = []

    @property
    def calls(self) -> int:
        return len(self.images)

    async def read(self, image: bytes) -> Reading:
        self.images.append(image)
        return Reading(
            text=_SENTENCE if _is_upright(image) else _GARBLE,
            input_tokens=1000,
            output_tokens=20,
            usage_reported=True,
        )


@dataclass
class _Settings:
    figure: ModelChoice | None = None
    max_pages: int = 1000
    primary: ModelChoice | None = field(default_factory=lambda: ModelChoice("glm", "glm-ocr"))

    async def recognition_settings(self) -> RecognitionSettings:
        return RecognitionSettings(
            primary=self.primary, fallback=None, max_pages=self.max_pages, figure=self.figure
        )


async def _keys(*, tenant_id: UUID, provider: str) -> str:
    del tenant_id
    return f"key-{provider}"


def _recognizer(
    store: InMemoryKnowledgeStore, reader: Any, settings: _Settings | None = None
) -> DocumentRecognizer:
    def factory(choice: ModelChoice, api_key: str) -> RecognitionClient:
        del choice, api_key
        return reader  # type: ignore[no-any-return]

    return DocumentRecognizer(
        store=store, settings=settings or _Settings(), api_keys=_keys, client_factory=factory
    )


@dataclass
class _Doc:
    store: InMemoryKnowledgeStore
    tenant: UUID
    kb: UUID
    document: UUID
    raw: bytes


async def _document(raw: bytes) -> _Doc:
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="scan.pdf", content=raw, content_sha256="sha-1"
    )
    return _Doc(store, tenant, base.id, document.id, raw)


async def _ingest(doc: _Doc, reader: Any, *, figures: FigureDescriber | None = None) -> str:
    await ingest_document_bytes(
        store=doc.store,
        embedder=FakeEmbedder(),
        tenant_id=doc.tenant,
        document_id=doc.document,
        kb_id=doc.kb,
        filename="scan.pdf",
        raw=doc.raw,
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=DEFAULT_CHUNK_OVERLAP_TOKENS,
        recognizer=_recognizer(doc.store, reader),
        figures=figures,
    )
    chunks, _ = await doc.store.list_chunks(
        tenant_id=doc.tenant, document_id=doc.document, limit=1000
    )
    return "".join("".join(chunk.content for chunk in chunks).split())


# -- reading turned pages ----------------------------------------------------------------------


async def test_a_sideways_scanned_page_is_read_upright() -> None:
    # The must-be-red control (written before the feature): the same page, turned
    # 90 degrees clockwise by the scanner, read by a model that only reads upright text.
    before = _counted(_ROTATED, rotation="90")
    doc = await _document(_turned_pdf(90))
    reader = _UprightOnlyReader()
    bodies = await _ingest(doc, reader)
    assert reader.calls == 1
    assert _SENTENCE in bodies
    assert "乱码" not in bodies
    assert _counted(_ROTATED, rotation="90") == before + 1


async def test_pages_turned_every_way_are_each_read_upright() -> None:
    doc = await _document(_turned_pdf(0, 90, 180, 270))
    reader = _UprightOnlyReader()
    bodies = await _ingest(doc, reader)
    assert reader.calls == 4
    assert [_is_upright(image) for image in reader.images] == [True] * 4
    assert bodies.count(_SENTENCE) >= 1 and "乱码" not in bodies


@pytest.mark.usefixtures("switched_off")
async def test_with_the_switch_off_the_page_is_read_as_it_lies() -> None:
    before = {angle: _counted(_ROTATED, rotation=angle) for angle in ("0", "90")}
    doc = await _document(_turned_pdf(90))
    reader = _UprightOnlyReader()
    bodies = await _ingest(doc, reader)
    assert "乱码" in bodies and _SENTENCE not in bodies
    assert {angle: _counted(_ROTATED, rotation=angle) for angle in ("0", "90")} == before


def _render_before_b301(raw: bytes, index: int) -> bytes:
    """The scanned-page rendering exactly as it was before B-301 PR 4."""
    doc = pymupdf.open(stream=raw, filetype="pdf")
    page = doc[index]
    zoom = RENDER_DPI / 72
    longest = max(page.rect.width, page.rect.height)
    if longest * zoom > RENDER_MAX_EDGE_PX:
        zoom = RENDER_MAX_EDGE_PX / longest
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    rendered = bytes(pixmap.tobytes("jpeg", jpg_quality=RENDER_JPEG_QUALITY))
    doc.close()
    return rendered


async def test_an_upright_page_is_read_from_the_same_bytes_as_before() -> None:
    before = _counted(_ROTATED, rotation="0")
    doc = await _document(_turned_pdf(0))
    reader = _UprightOnlyReader()
    bodies = await _ingest(doc, reader)
    assert _SENTENCE in bodies
    assert reader.images == [_render_before_b301(doc.raw, 0)]
    assert _counted(_ROTATED, rotation="0") == before + 1


# -- the cache record, the parse snapshot ------------------------------------------------------


async def test_a_turned_pages_cached_reading_records_its_angle_and_confidence() -> None:
    doc = await _document(_turned_pdf(0, 270))
    await _ingest(doc, _UprightOnlyReader())
    rows = [entry.result for entry in doc.store._parse_cache.values()]
    by_page = {row["page"]: row for row in rows}
    assert set(by_page) == {0, 1}
    assert "rotation" not in by_page[0] and "rotation_confidence" not in by_page[0]
    assert by_page[1]["rotation"] == 270
    assert 0.9 <= by_page[1]["rotation_confidence"] < 1
    assert json.loads(json.dumps(by_page[1])) == by_page[1]  # a JSON value: no migration


def test_the_parse_snapshot_keeps_each_pages_orientation() -> None:
    from control_plane.knowledge.recognition.ingest_parse import LocalParse

    analysis = analyse_pdf_within_cap("a.pdf", _turned_pdf(0, 90), 10_000_000, 1000)
    assert [page.rotation for page in analysis.pages] == [0, 90]
    local = LocalParse(kind="pdf", figures_on=False, analysis=analysis, text_limit=100)
    snapshot = encode_snapshot(local)
    assert snapshot is not None
    decoded = decode_snapshot(snapshot)
    assert decoded is not None and decoded.analysis is not None
    assert decoded.analysis.pages == analysis.pages
    # A snapshot written before B-301 PR 4 has no orientation: the page was not asked.
    old = encode_snapshot(
        replace(
            local,
            analysis=replace(
                analysis, pages=tuple(replace(p, orientation=None) for p in analysis.pages)
            ),
        )
    )
    assert old is not None
    reread = decode_snapshot(old)
    assert reread is not None and reread.analysis is not None
    assert [p.orientation for p in reread.analysis.pages] == [None, None]


# -- estimate == paid --------------------------------------------------------------------------


class _Forbidden:
    async def read(self, image: bytes) -> Reading:
        raise AssertionError("the estimate called a model")


async def _estimated_paid_pages(doc: _Doc) -> int:
    """Pages the estimate says the next ingest pays to have read."""
    store = doc.store
    recognizer = _recognizer(store, _Forbidden())
    local = await parse_locally(
        store,
        tenant_id=doc.tenant,
        kb_id=doc.kb,
        document_id=doc.document,
        filename="scan.pdf",
        raw=doc.raw,
        figures=None,
        recognizer=recognizer,
    )
    claim = ClaimedIngestion(
        tenant_id=doc.tenant,
        document_id=doc.document,
        kb_id=doc.kb,
        filename="scan.pdf",
        content=doc.raw,
        content_sha256="sha-1",
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=0,
        attempts=1,
        generation=1,
        phase="estimate",
        origin="upload",
        serving_sha256="sha-1",
    )
    estimate = await IngestEstimator(
        store=store, settings=_Settings(), recognizer=recognizer
    ).estimate(claim, local, embedding_model=None, now=_now())
    (line,) = [item for item in estimate.items if item.kind is EstimateKind.RECOGNITION]
    return line.units - line.reused_units


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime(2026, 11, 12, tzinfo=UTC)


async def _paid_pages(doc: _Doc) -> int:
    reader = _UprightOnlyReader()
    await _ingest(doc, reader)
    return reader.calls


async def test_a_cached_turned_document_is_estimated_at_what_it_pays() -> None:
    doc = await _document(_turned_pdf(0, 90, 180))
    assert await _estimated_paid_pages(doc) == await _paid_pages(doc) == 3
    # Read once: every page, turned or not, comes from the cache — and is estimated so.
    assert await _estimated_paid_pages(doc) == await _paid_pages(doc) == 0


async def test_pages_read_sideways_before_are_not_counted_free_once_they_are_turned() -> None:
    doc = await _document(_turned_pdf(0, 90, 180))
    orientation.configure_page_orientation(enabled=False)
    try:
        assert await _paid_pages(doc) == 3  # read as they lay: cached sideways
    finally:
        orientation.configure_page_orientation(enabled=True)
    # Now the two turned pages render upright (other bytes): both are paid for again,
    # and the estimate says so; the upright page is still free.
    assert await _estimated_paid_pages(doc) == await _paid_pages(doc) == 2


class _AnotherModel:
    """The real classifier's answers, as if a new model file had given them."""

    def __init__(self, real: PageClassifier) -> None:
        self._real = real

    def classify(self, image: np.ndarray) -> Orientation:
        return replace(self._real.classify(image), model="f" * 64)


async def test_the_estimate_asks_and_the_confirmed_ingest_turns_the_page_it_counted() -> None:
    # The runner's estimate phase parses with the ingest's recognizer: the snapshot it
    # keeps carries the angle, and the confirmed ingest reads the page upright from it.
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    started = await store.begin_upload(
        tenant_id=tenant,
        kb_id=base.id,
        filename="scan.pdf",
        content=_turned_pdf(90),
        content_sha256="sha-sideways",
        now=_now(),
    )
    document_id = started.document.id
    reader = _UprightOnlyReader()
    recognizer = _recognizer(store, reader)
    runner = KnowledgeIngestionRunner(
        store=store,
        embedder=FakeEmbedder(),
        recognizer=recognizer,
        estimator=IngestEstimator(store=store, settings=_Settings(), recognizer=recognizer),
    )
    await runner.submit(tenant_id=tenant, document_id=document_id)
    waiting = await store.get_document(tenant_id=tenant, document_id=document_id)
    assert waiting is not None and waiting.status is DocumentStatus.AWAITING_CONFIRMATION
    assert waiting.ingest_estimate is not None and reader.calls == 0
    snapshot = await store.get_parse_snapshot(
        tenant_id=tenant, document_id=document_id, estimate_id=waiting.ingest_estimate.id
    )
    assert snapshot is not None
    local = decode_snapshot(snapshot)
    assert local is not None and local.analysis is not None
    assert [page.rotation for page in local.analysis.pages] == [90]
    await store.confirm_documents(
        tenant_id=tenant,
        kb_id=base.id,
        estimates=[(document_id, waiting.ingest_estimate.id)],
        confirmed_by="user",
        now=_now(),
    )
    await runner.submit(tenant_id=tenant, document_id=document_id)
    ready = await store.get_document(tenant_id=tenant, document_id=document_id)
    assert ready is not None
    assert ready.status is DocumentStatus.READY, ready.error
    assert reader.calls == 1 and _is_upright(reader.images[0])


class _Counting:
    """The real classifier, counting the pages it is asked about."""

    def __init__(self, real: PageClassifier) -> None:
        self._real = real
        self.calls = 0

    def classify(self, image: np.ndarray) -> Orientation:
        self.calls += 1
        return self._real.classify(image)


async def _parse_and_ingest(doc: _Doc, settings: _Settings | None) -> None:
    """The estimate's parse, then an ingest that parses for itself (with
    ``settings``' recognizer; ``None``: no recognizer at all)."""
    reader = _UprightOnlyReader()
    recognizer = _recognizer(doc.store, reader, settings) if settings is not None else None
    await parse_locally(
        doc.store,
        tenant_id=doc.tenant,
        kb_id=doc.kb,
        document_id=doc.document,
        filename="scan.pdf",
        raw=doc.raw,
        figures=None,
        recognizer=recognizer,
    )
    await ingest_document_bytes(
        store=doc.store,
        embedder=FakeEmbedder(),
        tenant_id=doc.tenant,
        document_id=doc.document,
        kb_id=doc.kb,
        filename="scan.pdf",
        raw=doc.raw,
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=DEFAULT_CHUNK_OVERLAP_TOKENS,
        recognizer=recognizer,
    )


@pytest.mark.usefixtures("in_process_parse")
async def test_only_pages_recognition_will_read_are_asked_their_orientation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counting = _Counting(orientation.process_classifier())
    monkeypatch.setattr(orientation, "_classifier", counting)
    doc = await _document(_turned_pdf(90, 90, 90, 90))
    # The page cap is 2: pages 3 and 4 are never read — not drawn small or asked
    # either, by the estimate's parse or the ingest's.
    await _parse_and_ingest(doc, _Settings(max_pages=2))
    assert counting.calls == 2 + 2
    analysis = analyse_pdf_within_cap("scan.pdf", doc.raw, 10_000_000, 2)
    assert [page.rotation for page in analysis.pages] == [90, 90, 0, 0]
    assert [page.orientation is None for page in analysis.pages] == [False, False, True, True]
    # No recognition model configured, or no recognizer at all: nothing is read now
    # (a later 补识别 parses again), nothing is asked.
    counting.calls = 0
    for settings in (_Settings(primary=None), None):
        with pytest.raises(DocumentParseError, match="no text"):  # all scanned, none read
            await _parse_and_ingest(await _document(doc.raw), settings)
    assert counting.calls == 0


@pytest.mark.usefixtures("in_process_parse")
async def test_a_new_orientation_model_is_estimated_at_what_it_pays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = await _document(_turned_pdf(0, 90))
    assert await _paid_pages(doc) == 2
    assert await _estimated_paid_pages(doc) == await _paid_pages(doc) == 0
    monkeypatch.setattr(orientation, "_classifier", _AnotherModel(orientation.process_classifier()))
    # Same angle, same image bytes — but read under another model's say-so: the turned
    # page is read (and paid for) again, and estimated so; the upright page is not.
    assert await _estimated_paid_pages(doc) == await _paid_pages(doc) == 1


# -- failing loud ------------------------------------------------------------------------------


@pytest.mark.usefixtures("in_process_parse")
async def test_a_model_that_will_not_load_leaves_the_pages_as_they_are_and_is_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orientation, "MODEL_SHA256", "0" * 64)  # the file is not the pinned one
    failures = _counted(_FAILURES)
    doc = await _document(_turned_pdf(0, 90))
    reader = _UprightOnlyReader()
    bodies = await _ingest(doc, reader)
    assert reader.calls == 2 and _SENTENCE in bodies and "乱码" in bodies  # page 2 as it lies
    assert _counted(_FAILURES) == failures + 2
    assert not orientation.session_loaded()


# -- figures on a turned page --------------------------------------------------------------------


def _page_with_picture() -> Image.Image:
    page = _upright_page(3)
    picture = Image.open(io.BytesIO(noise_png(320, 240, seed=11)))
    page.paste(picture, (page.width // 2 - 160, page.height // 2 - 120))
    return page


_BOX = (0.25, 0.35, 0.75, 0.62)  # relative to the page as read (upright)


def _upright_crop(
    raw: bytes, index: int, rotation: int, box: tuple[float, float, float, float] | None = None
) -> figure_images.NormalizedImage:
    """Turn the page upright, then crop ``box`` (pixels; default :data:`_BOX` of the
    page) — what the figure must be."""
    rendered = render_pages(raw, [index], None, {index: rotation})[0]
    width, height = Image.open(io.BytesIO(rendered)).size
    x0, y0, x1, y1 = _BOX
    cropped = figure_images.crop(
        rendered, box or (x0 * width, y0 * height, x1 * width, y1 * height)
    )
    assert cropped is not None
    return cropped


def test_a_figure_on_a_turned_page_is_cropped_from_the_page_turned_upright() -> None:
    raw = orientation_pages.scanned_pdf([orientation_pages.turned(_page_with_picture(), 90)])
    scanned = crop_scanned_figures(raw, {0: [_BOX]}, 1, {0: 90})
    (candidate,) = scanned.figures.candidates
    assert candidate.pixel_hash == _upright_crop(raw, 0, 90).pixel_hash
    sideways = crop_scanned_figures(raw, {0: [_BOX]}, 1)
    assert sideways.figures.candidates[0].pixel_hash != candidate.pixel_hash


@dataclass
class _LayoutReader:
    """Reads a page (long edge 2,000 px) as text with one image region at :data:`_BOX`
    (whole pixels, as a layout model gives them)."""

    boxes: list[tuple[float, ...]] = field(default_factory=list)

    async def read(self, image: bytes) -> Reading:
        width, height = Image.open(io.BytesIO(image)).size
        if max(width, height) != RENDER_MAX_EDGE_PX:
            return Reading(text="none", input_tokens=10, output_tokens=1)  # a figure's text
        x0, y0, x1, y1 = _BOX
        pixels = tuple(float(round(v)) for v in (x0 * width, y0 * height, x1 * width, y1 * height))
        self.boxes.append(pixels)
        box = ", ".join(f"{v:.0f}" for v in pixels)
        return Reading(
            text=f"## {_SENTENCE}\n\n![](page=0,bbox=[{box}])\n\n安装完成后接通电源。",
            input_tokens=1500,
            output_tokens=80,
            usage_reported=True,
        )


@dataclass
class _Vision:
    images: list[bytes] = field(default_factory=list)

    def factory(
        self, choice: ModelChoice, api_key: str, prompt: str, user_text: str, max_tokens: int
    ) -> _Vision:
        return self

    async def read(self, image: bytes) -> Reading:
        self.images.append(image)
        answer = {"text": "none", "chart": False, "description": "一张设备照片。"}
        return Reading(
            text=json.dumps(answer), finish_reason="stop", input_tokens=7, output_tokens=7
        )


async def test_an_ingest_crops_a_turned_pages_figure_upright() -> None:
    doc = await _document(
        orientation_pages.scanned_pdf([orientation_pages.turned(_page_with_picture(), 270)])
    )
    settings = _Settings(figure=ModelChoice("qwen", "qwen3.8-flash"))
    vision = _Vision()
    layout = _LayoutReader()
    recognizer = _recognizer(doc.store, layout, settings)
    figures = FigureDescriber(
        store=doc.store,
        settings=settings,
        api_keys=_keys,
        recognizer=recognizer,
        storage=FigureStorage(InMemoryObjectStore()),
        client_factory=vision.factory,
    )
    await ingest_document_bytes(
        store=doc.store,
        embedder=FakeEmbedder(),
        tenant_id=doc.tenant,
        document_id=doc.document,
        kb_id=doc.kb,
        filename="scan.pdf",
        raw=doc.raw,
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=DEFAULT_CHUNK_OVERLAP_TOKENS,
        recognizer=recognizer,
        figures=figures,
    )
    (box,) = layout.boxes
    assert vision.images == [_upright_crop(doc.raw, 0, 270, (box[0], box[1], box[2], box[3])).jpeg]


# -- where the classifier never runs -------------------------------------------------------------


async def test_the_figure_worker_never_creates_a_session() -> None:
    raw = orientation_pages.scanned_pdf([orientation_pages.turned(_page_with_picture(), 90)])
    FIGURE_POOL.shutdown(kill=True)  # a fresh figure worker
    try:
        budget = PoolBudget(120)
        scanned = await FIGURE_POOL.run(
            "a.pdf", crop_scanned_figures, raw, {0: [_BOX]}, 1, {0: 90}, budget=budget
        )
        assert len(scanned.figures.candidates) == 1
        assert not await FIGURE_POOL.run("a.pdf", orientation.session_loaded, budget=budget)
        # The probe can tell: the same worker, once it has analysed a page with orientation on.
        await FIGURE_POOL.run(
            "a.pdf", analyse_pdf_within_cap, "a.pdf", raw, 10_000_000, 1000, budget=budget
        )
        assert await FIGURE_POOL.run("a.pdf", orientation.session_loaded, budget=budget)
    finally:
        FIGURE_POOL.shutdown(kill=True)


def test_d4_nothing_on_the_ask_image_side_can_reach_the_classifier() -> None:
    # ``ask_image`` (uploads, workspace images) lives in the orchestrator, which does
    # not depend on the control plane: it can only reach the classifier by importing
    # the control plane, onnxruntime or the model file — none of its modules does.
    orchestrator_src = Path(__file__).resolve().parents[2] / "orchestrator" / "src"
    sources = list(orchestrator_src.rglob("*.py"))
    assert any(path.name == "vision.py" for path in sources)  # the ask_image tool is here
    users = [
        path
        for path in sources
        if any(
            needle in path.read_text()
            for needle in (
                "recognition.orientation",
                "onnxruntime",
                "pp_lcnet",
                "from control_plane",
                "import control_plane",
            )
        )
    ]
    assert users == []
