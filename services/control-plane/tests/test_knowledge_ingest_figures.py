"""B-209 PR-2 —— 入库路径上的文档内图片:PDF 文字页插图 / 扫描页上的图片区域 / Word / 幻灯片
里的图片描述成引用块、插回原位,进切块;逐页记录带图片数,文档带图片计数;重新入库不再付费。

看图模型与识别模型是假的(不联网);文件现场合成(零租户内容),走真实的解析进程池。
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID, uuid4

import pymupdf
import pytest
from PIL import Image

from control_plane.knowledge.ingestion import (
    KnowledgeIngestionRunner,
    StaleIngestionError,
    ingest_document_bytes,
    shutdown_parse_pool,
)
from control_plane.knowledge.parsing import DocumentParseError
from control_plane.knowledge.recognition.clients import Reading
from control_plane.knowledge.recognition.figure_describe import DESCRIBE_USER_TEXT
from control_plane.knowledge.recognition.figure_markup import FIGURE_ID_PATTERN, TEXT_LABEL
from control_plane.knowledge.recognition.figure_pipeline import FigureDescriber
from control_plane.knowledge.recognition.figure_storage import FigureStorage
from control_plane.knowledge.recognition.recognizer import (
    DocumentRecognizer,
    ModelChoice,
    RecognitionSettings,
)
from control_plane.knowledge.recovery import KnowledgeIngestRecoveryWorker
from control_plane.knowledge.usage import IngestUsageRecorder
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.knowledge.base import UNSET
from expert_work.persistence.token_usage_store import InMemoryTokenUsageStore
from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    DocumentStatus,
    FigureStats,
    IngestStage,
    PageDecision,
    PageStatus,
)
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.llm import FakeEmbedder
from tests.figure_test_images import noise_png
from tests.knowledge_form_fixtures import picture_docx, picture_pptx

_DESCRIPTION = "一台设备的外观照片。"
_PHOTO = json.dumps({"text": "none", "chart": False, "description": _DESCRIPTION})
_TEXT_SHOT = json.dumps({"text": "lots", "chart": False, "description": "一张操作步骤的截图。"})
_SCAN_TEXT_BEFORE = "第二章 安装"
_SCAN_TEXT_AFTER = "安装完成后接通电源。"


@pytest.fixture(scope="module", autouse=True)
def _parse_pool_teardown() -> Iterator[None]:
    yield
    shutdown_parse_pool()


@dataclass
class _Vision:
    answer: str = _PHOTO
    calls: int = 0
    prompts: list[str] = field(default_factory=list)

    def factory(
        self, choice: ModelChoice, api_key: str, prompt: str, user_text: str, max_tokens: int
    ) -> _VisionClient:
        del choice, api_key, max_tokens
        self.prompts.append(prompt)
        return _VisionClient(self, user_text)


@dataclass
class _VisionClient:
    vision: _Vision
    user_text: str

    async def read(self, image: bytes) -> Reading:
        assert image.startswith(b"\xff\xd8")  # the normalized JPEG
        assert self.user_text == DESCRIBE_USER_TEXT
        self.vision.calls += 1
        return Reading(
            text=self.vision.answer,
            finish_reason="stop",
            input_tokens=700,
            output_tokens=40,
            usage_reported=True,
        )


@dataclass
class _LayoutReader:
    """Fake layout-parsing model: reads a scanned page as two paragraphs with
    one image region between them (the picture's box, in the page's pixels)."""

    box: tuple[float, float, float, float]  # relative
    calls: int = 0
    figure_text_calls: int = 0

    async def read(self, image: bytes) -> Reading:
        with Image.open(io.BytesIO(image)) as page:
            width, height = page.size
        if height != _PAGE_HEIGHT_PX:
            self.figure_text_calls += 1  # a figure's text, not a page
            return Reading(text="第一步 接通电源", input_tokens=300, output_tokens=20)
        self.calls += 1
        x0, y0, x1, y1 = self.box
        box = ", ".join(f"{v:.0f}" for v in (x0 * width, y0 * height, x1 * width, y1 * height))
        region = f"![](page=0,bbox=[{box}])"
        text = f"## {_SCAN_TEXT_BEFORE}\n\n{region}\n\n{_SCAN_TEXT_AFTER}"
        return Reading(text=text, input_tokens=1500, output_tokens=80, usage_reported=True)


#: A4 at 200 DPI has its long edge capped at 2,000 px — the scanned-page rendering.
_PAGE_HEIGHT_PX = 2000


@dataclass
class _Settings:
    figure: ModelChoice | None = field(default_factory=lambda: ModelChoice("qwen", "qwen3.8-flash"))

    async def recognition_settings(self) -> RecognitionSettings:
        return RecognitionSettings(
            primary=ModelChoice("glm", "glm-ocr"), fallback=None, max_pages=1000, figure=self.figure
        )


async def _keys(*, tenant_id: UUID, provider: str) -> str:
    del tenant_id
    return f"key-{provider}"


@dataclass
class _Doc:
    store: InMemoryKnowledgeStore
    tenant: UUID
    kb: UUID
    document: UUID
    filename: str
    raw: bytes


async def _document(filename: str, raw: bytes) -> _Doc:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename=filename, content=raw
    )
    return _Doc(store, tenant, base.id, document.id, filename, raw)


@dataclass
class _Pipeline:
    vision: _Vision
    reader: _LayoutReader
    usage: InMemoryTokenUsageStore
    objects: InMemoryObjectStore
    recognizer: DocumentRecognizer
    figures: FigureDescriber


def _pipeline(
    store: InMemoryKnowledgeStore,
    *,
    vision: _Vision | None = None,
    box: tuple[float, float, float, float] = (0.2, 0.3, 0.8, 0.6),
    settings: _Settings | None = None,
) -> _Pipeline:
    resolved_vision = vision or _Vision()
    reader = _LayoutReader(box)
    usage = InMemoryTokenUsageStore()
    objects = InMemoryObjectStore()
    recorder = IngestUsageRecorder(usage)
    resolved_settings = settings or _Settings()
    recognizer = DocumentRecognizer(
        store=store,
        settings=resolved_settings,
        api_keys=_keys,
        usage=recorder,
        client_factory=lambda choice, key: reader,
    )
    figures = FigureDescriber(
        store=store,
        settings=resolved_settings,
        api_keys=_keys,
        recognizer=recognizer,
        storage=FigureStorage(objects),
        usage=recorder,
        client_factory=resolved_vision.factory,
    )
    return _Pipeline(resolved_vision, reader, usage, objects, recognizer, figures)


async def _ingest(doc: _Doc, pipeline: _Pipeline | None, *, figures: bool = True) -> str:
    """Ingest; the chunks' text in order (heading path + body). ``figures``
    off: no figure step at all."""
    await ingest_document_bytes(
        store=doc.store,
        embedder=FakeEmbedder(),
        tenant_id=doc.tenant,
        document_id=doc.document,
        kb_id=doc.kb,
        filename=doc.filename,
        raw=doc.raw,
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=DEFAULT_CHUNK_OVERLAP_TOKENS,
        recognizer=pipeline.recognizer if pipeline else None,
        usage=IngestUsageRecorder(pipeline.usage) if pipeline else None,
        figures=pipeline.figures if pipeline and figures else None,
    )
    chunks, _ = await doc.store.list_chunks(
        tenant_id=doc.tenant, document_id=doc.document, limit=10_000
    )
    return "\n".join(" > ".join(c.heading_path) + "\n" + c.content for c in chunks)


def _scan_with_picture() -> bytes:
    """A scanned page image: a picture in the middle of white paper."""
    paper = Image.new("RGB", (1240, 1754), (255, 255, 255))
    paper.paste(Image.open(io.BytesIO(noise_png(744, 526, seed=7))), (248, 526))
    out = io.BytesIO()
    paper.save(out, format="PNG")
    return out.getvalue()


def _mixed_pdf() -> bytes:
    """Page 1: text, a picture, more text. Page 2: a scan with a picture on it."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 80), "Opening paragraph about the device and its parts.", fontsize=11)
    page.insert_image(pymupdf.Rect(72, 120, 372, 345), stream=noise_png(600, 450))
    page.insert_text((72, 400), "Closing paragraph after the picture of the device.", fontsize=11)
    scan = doc.new_page()
    scan.insert_image(scan.rect, stream=_scan_with_picture())
    return bytes(doc.tobytes())


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


async def test_a_mixed_pdf_gets_its_figures_described_in_place() -> None:
    doc = await _document("device.pdf", _mixed_pdf())
    pipeline = _pipeline(doc.store)
    text = await _ingest(doc, pipeline)
    # Page 1: the text-page figure sits between its paragraphs.
    first = text.index(_DESCRIPTION)
    assert text.index("Opening paragraph") < first < text.index("Closing paragraph")
    # Page 2: the scanned page's image region, described where the model found it.
    second = text.index(_DESCRIPTION, first + 1)
    assert text.index(_SCAN_TEXT_BEFORE) < second < text.index(_SCAN_TEXT_AFTER)
    assert "" not in text
    assert pipeline.vision.calls == 2 and pipeline.reader.calls == 1
    pages = await doc.store.list_document_pages(tenant_id=doc.tenant, document_id=doc.document)
    assert pages is not None
    assert [p.decision for p in pages] == [PageDecision.TEXT, PageDecision.OCR]
    assert [(p.figures_kept, p.figures_described) for p in pages] == [(1, 1), (1, 1)]
    assert pages[1].status is PageStatus.RECOGNIZED
    fetched = await doc.store.get_document(tenant_id=doc.tenant, document_id=doc.document)
    assert fetched is not None
    assert fetched.figure_stats == FigureStats(extracted=2, described=2)


async def test_figure_ids_in_chunks_point_at_stored_originals() -> None:
    doc = await _document("device.pdf", _mixed_pdf())
    pipeline = _pipeline(doc.store)
    text = await _ingest(doc, pipeline)
    ids = FIGURE_ID_PATTERN.findall(text)
    assert len(ids) == 2
    storage = FigureStorage(pipeline.objects)
    for identifier in ids:
        image = await storage.get(
            tenant_id=doc.tenant, kb_id=doc.kb, document_id=doc.document, figure=identifier
        )
        assert image is not None and image.startswith(b"\xff\xd8")


async def test_re_ingesting_the_pdf_pays_for_nothing_again() -> None:
    doc = await _document("device.pdf", _mixed_pdf())
    pipeline = _pipeline(doc.store)
    first = await _ingest(doc, pipeline)
    rows = len(await pipeline.usage.list_for_tenant(tenant_id=doc.tenant, limit=1000))
    calls = (pipeline.vision.calls, pipeline.reader.calls)
    second = await _ingest(doc, pipeline)
    assert second == first
    assert (pipeline.vision.calls, pipeline.reader.calls) == calls
    # The second ingest bills only embeddings of changed chunks — none changed.
    assert len(await pipeline.usage.list_for_tenant(tenant_id=doc.tenant, limit=1000)) == rows


def _text_pdf_with_picture() -> bytes:
    """Text pages only, one picture (the reviewer's case: no scanned page)."""
    doc = pymupdf.open()
    page = doc.new_page()
    for row in range(3):
        page.insert_text((72, 72 + 40 * row), f"Paragraph {row} above the picture.", fontsize=11)
    page.insert_image(pymupdf.Rect(72, 200, 372, 400), stream=noise_png(600, 400))
    page.insert_text((72, 460), "Paragraph after the picture.", fontsize=11)
    return bytes(doc.tobytes())


@pytest.mark.parametrize("make", [_mixed_pdf, _text_pdf_with_picture], ids=["mixed", "text-only"])
async def test_without_a_figure_model_a_pdf_parses_exactly_as_before(make: Any) -> None:
    # Review M3: the path production runs (a describer whose figure model is
    # not configured) reads the PDF exactly as a pipeline with no figure step.
    raw = make()
    wired = await _document("device.pdf", raw)
    pipeline = _pipeline(wired.store, settings=_Settings(figure=None))
    text = await _ingest(wired, pipeline)
    plain = await _document("device.pdf", raw)
    assert text == await _ingest(plain, _pipeline(plain.store), figures=False)
    assert pipeline.vision.calls == 0
    fetched = await wired.store.get_document(tenant_id=wired.tenant, document_id=wired.document)
    assert fetched is not None and fetched.figure_stats is None


async def test_the_scan_under_a_hidden_text_layer_is_not_described() -> None:
    doc_pdf = pymupdf.open()
    page = doc_pdf.new_page()
    page.insert_image(page.rect, stream=noise_png(620, 877))
    page.insert_text((72, 100), "Invisible scanner text layer " * 4, fontsize=10, render_mode=3)
    doc = await _document("scan.pdf", bytes(doc_pdf.tobytes()))
    pipeline = _pipeline(doc.store)
    text = await _ingest(doc, pipeline)
    assert "Invisible scanner text layer" in text
    assert pipeline.vision.calls == 0


async def test_figure_collection_out_of_time_costs_only_the_figures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review H3 / M7: the figure step's share of the parse budget runs out —
    # the document still ingests, its pictures counted as unreadable.
    from control_plane.knowledge import ingestion

    monkeypatch.setattr(ingestion, "FIGURE_POOL_SHARE", 0.0)
    doc = await _document("device.pdf", _text_pdf_with_picture())
    pipeline = _pipeline(doc.store)
    text = await _ingest(doc, pipeline)
    assert "Paragraph after the picture." in text
    assert pipeline.vision.calls == 0
    fetched = await doc.store.get_document(tenant_id=doc.tenant, document_id=doc.document)
    assert fetched is not None
    assert fetched.figure_stats == FigureStats(extracted=1, unsupported=1)


class _OrderStore(InMemoryKnowledgeStore):
    """Records the figure writes and the tenant-wide count, in order."""

    def __init__(self) -> None:
        super().__init__()
        self.steps: list[str] = []

    async def set_document_figures(self, **kwargs: Any) -> bool:
        self.steps.append("stats" if kwargs["stats"] is not UNSET else "hashes")
        return await super().set_document_figures(**kwargs)

    async def count_figure_documents(self, **kwargs: Any) -> dict[str, int]:
        self.steps.append("count")
        return await super().count_figure_documents(**kwargs)


async def test_hashes_are_recorded_before_the_tenant_wide_count() -> None:
    # Review M5: a document's hashes are visible to others before it counts.
    store = _OrderStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    raw = picture_docx(noise_png(640, 480))
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="a.docx", content=raw
    )
    await _ingest(_Doc(store, tenant, base.id, document.id, "a.docx", raw), _pipeline(store))
    assert store.steps == ["hashes", "count", "stats"]


async def test_a_text_heavy_figure_carries_its_text() -> None:
    doc = await _document("device.pdf", _mixed_pdf())
    pipeline = _pipeline(doc.store, vision=_Vision(answer=_TEXT_SHOT))
    text = await _ingest(doc, pipeline)
    assert TEXT_LABEL in text and "第一步 接通电源" in text
    assert pipeline.reader.figure_text_calls == 2


# ---------------------------------------------------------------------------
# Word / slides
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "raw"),
    [
        ("产品图.docx", picture_docx(noise_png(640, 480))),
        ("产品图.pptx", picture_pptx(noise_png(640, 480))),
    ],
    ids=["docx", "pptx"],
)
async def test_a_picture_only_office_file_becomes_its_description(
    filename: str, raw: bytes
) -> None:
    doc = await _document(filename, raw)
    pipeline = _pipeline(doc.store)
    text = await _ingest(doc, pipeline)
    assert _DESCRIPTION in text and "data:image" not in text and "![" not in text
    fetched = await doc.store.get_document(tenant_id=doc.tenant, document_id=doc.document)
    assert fetched is not None
    assert fetched.figure_stats == FigureStats(extracted=1, described=1)


async def test_a_word_picture_sits_between_its_paragraphs() -> None:
    raw = picture_docx(noise_png(640, 480), before="安装前请阅读本说明。", after="以上为整机外观。")
    doc = await _document("说明.docx", raw)
    text = await _ingest(doc, _pipeline(doc.store))
    assert (
        text.index("安装前请阅读本说明") < text.index(_DESCRIPTION) < text.index("以上为整机外观")
    )


async def test_a_picture_only_word_file_whose_picture_is_filtered_fails_with_a_reason() -> None:
    doc = await _document("图标.docx", picture_docx(noise_png(64, 64)))
    with pytest.raises(DocumentParseError, match="1 pictures, 1 filtered"):
        await _ingest(doc, _pipeline(doc.store))


async def test_the_runner_wires_the_figure_describer() -> None:
    doc = await _document("产品图.docx", picture_docx(noise_png(640, 480)))
    pipeline = _pipeline(doc.store)
    runner = KnowledgeIngestionRunner(
        store=doc.store,
        embedder=FakeEmbedder(),
        recognizer=pipeline.recognizer,
        figures=pipeline.figures,
    )
    runner.submit(tenant_id=doc.tenant, document_id=doc.document)
    await runner.drain()
    fetched = await doc.store.get_document(tenant_id=doc.tenant, document_id=doc.document)
    assert fetched is not None and fetched.status is DocumentStatus.READY, fetched
    assert fetched.figure_stats is not None and fetched.figure_stats.described == 1


async def test_a_new_version_without_pictures_drops_the_old_counts() -> None:
    # Review M3: the counts are the served version's; none to drop → no write.
    doc = await _document("a.docx", picture_docx(noise_png(640, 480), before="Intro text."))
    pipeline = _pipeline(doc.store)
    await _ingest(doc, pipeline)
    first = await doc.store.get_document(tenant_id=doc.tenant, document_id=doc.document)
    assert first is not None and first.figure_stats is not None
    plain = picture_docx(noise_png(640, 480), before="Only text now.", repeat=0)
    await doc.store.upsert_document(
        tenant_id=doc.tenant, kb_id=doc.kb, filename=doc.filename, content=plain, force=True
    )
    text = await _ingest(replace(doc, raw=plain), pipeline)
    assert "Only text now." in text
    fetched = await doc.store.get_document(tenant_id=doc.tenant, document_id=doc.document)
    assert fetched is not None and fetched.figure_stats is None


async def test_a_document_without_pictures_writes_no_figure_record() -> None:
    store = _OrderStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    raw = picture_docx(noise_png(640, 480), before="Only text.", repeat=0)
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="a.docx", content=raw
    )
    await _ingest(_Doc(store, tenant, base.id, document.id, "a.docx", raw), _pipeline(store))
    assert store.steps == []


class _FailingEmbedder(FakeEmbedder):
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        raise ValueError("embedding refused")


async def _reuploaded_with_an_old_original() -> tuple[_Doc, _Pipeline]:
    """A Word file whose earlier version (generation 0) left an original;
    re-uploaded (generation 1), not yet ingested."""
    doc = await _document("a.docx", picture_docx(noise_png(640, 480)))
    pipeline = _pipeline(doc.store)
    await FigureStorage(pipeline.objects).put(
        tenant_id=doc.tenant,
        kb_id=doc.kb,
        document_id=doc.document,
        generation=0,
        figure="0123456789abcdef",
        image=b"old",
    )
    document = await doc.store.upsert_document(
        tenant_id=doc.tenant, kb_id=doc.kb, filename=doc.filename, content=doc.raw, force=True
    )
    assert document.ingest_generation == 1
    return doc, pipeline


async def _generations(pipeline: _Pipeline) -> set[str]:
    return {key.split("/")[-2] for key in await pipeline.objects.list_prefix("")}


async def _drive(doc: _Doc, pipeline: _Pipeline, path: str, embedder: FakeEmbedder) -> None:
    if path == "runner":
        runner = KnowledgeIngestionRunner(
            store=doc.store, embedder=embedder, figures=pipeline.figures
        )
        runner.submit(tenant_id=doc.tenant, document_id=doc.document)
        await runner.drain()
        return
    worker = KnowledgeIngestRecoveryWorker(
        store=doc.store,
        embedder=embedder,
        interval_s=1,
        concurrency=1,
        lease_seconds=300,
        max_attempts=1,
        figures=pipeline.figures,
    )
    await worker.run_once()


@pytest.mark.parametrize("path", ["runner", "recovery"])
async def test_a_settled_reingest_leaves_only_the_new_originals(path: str) -> None:
    # Review H5 (c), on both ingest paths.
    doc, pipeline = await _reuploaded_with_an_old_original()
    await _drive(doc, pipeline, path, FakeEmbedder())
    fetched = await doc.store.get_document(tenant_id=doc.tenant, document_id=doc.document)
    assert fetched is not None and fetched.status is DocumentStatus.READY
    assert await _generations(pipeline) == {"g1"}


@pytest.mark.parametrize("path", ["runner", "recovery"])
async def test_a_failed_ingest_drops_only_what_it_stored(path: str) -> None:
    # Review H5 (a): the earlier version's originals stay.
    doc, pipeline = await _reuploaded_with_an_old_original()
    await _drive(doc, pipeline, path, _FailingEmbedder())
    fetched = await doc.store.get_document(tenant_id=doc.tenant, document_id=doc.document)
    assert fetched is not None and fetched.status is DocumentStatus.FAILED
    assert pipeline.vision.calls == 1  # described (and stored) before the failure
    assert await _generations(pipeline) == {"g0"}


class _ReadyWriteFails(InMemoryKnowledgeStore):
    """A separate READY write would fail (a database blip)."""

    async def set_document_status(self, **kwargs: Any) -> bool:
        if kwargs.get("status") is DocumentStatus.READY:
            raise ConnectionError("database blip")
        return await super().set_document_status(**kwargs)


@pytest.mark.parametrize("path", ["runner", "recovery"])
async def test_the_chunk_swap_lands_ready_with_its_originals(path: str) -> None:
    # B2b review: the swap and READY are one write — there is no READY write
    # left to fail after the chunks are in place (re-review L1's case cannot
    # happen). The served chunks name this generation's originals.
    store = _ReadyWriteFails()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    raw = picture_docx(noise_png(640, 480))
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="a.docx", content=raw
    )
    doc = _Doc(store, tenant, base.id, document.id, "a.docx", raw)
    pipeline = _pipeline(store)
    await _drive(doc, pipeline, path, FakeEmbedder())
    fetched = await store.get_document(tenant_id=tenant, document_id=document.id)
    assert fetched is not None and fetched.status is DocumentStatus.READY
    assert fetched.chunk_count > 0
    assert await _generations(pipeline) == {"g0"}


async def test_figures_are_described_in_the_documents_language() -> None:
    raw = picture_docx(
        noise_png(640, 480),
        before="Read this guide before installing the device in the cabinet.",
        after="The picture above shows the device from the front and the side.",
    )
    doc = await _document("guide.docx", raw)
    pipeline = _pipeline(doc.store)
    await _ingest(doc, pipeline)
    assert pipeline.vision.prompts
    assert all("Write the description in English." in p for p in pipeline.vision.prompts)


class _LostStore(InMemoryKnowledgeStore):
    """The document is re-uploaded before its figures are described."""

    async def set_document_stage(self, **kwargs: object) -> bool:
        if kwargs["stage"] is IngestStage.RECOGNIZING:
            return False
        return await super().set_document_stage(**kwargs)  # type: ignore[arg-type]


async def test_a_worker_that_lost_the_document_describes_no_figure() -> None:
    store = _LostStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    raw = picture_docx(noise_png(640, 480))
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="a.docx", content=raw
    )
    doc = _Doc(store, tenant, base.id, document.id, "a.docx", raw)
    pipeline = _pipeline(store)
    with pytest.raises(StaleIngestionError):
        await _ingest(doc, pipeline)
    assert pipeline.vision.calls == 0


class _LostLateStore(InMemoryKnowledgeStore):
    """The document is re-uploaded while its figures are being described:
    the stage writes before the description and before its model call (B-222)
    pass, the next one is refused."""

    def __init__(self) -> None:
        super().__init__()
        self.recognizing = 0

    async def set_document_stage(self, **kwargs: object) -> bool:
        if kwargs["stage"] is IngestStage.RECOGNIZING:
            self.recognizing += 1
            if self.recognizing > 2:
                return False
        return await super().set_document_stage(**kwargs)  # type: ignore[arg-type]


async def test_a_worker_that_lost_the_document_mid_description_stores_no_original() -> None:
    # Security review S-M2: the lease is checked again before anything goes
    # to object storage.
    store = _LostLateStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    raw = picture_docx(noise_png(640, 480))
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="a.docx", content=raw
    )
    pipeline = _pipeline(store)
    with pytest.raises(StaleIngestionError):
        await _ingest(_Doc(store, tenant, base.id, document.id, "a.docx", raw), pipeline)
    assert pipeline.vision.calls == 1
    assert await pipeline.objects.list_prefix("") == []
