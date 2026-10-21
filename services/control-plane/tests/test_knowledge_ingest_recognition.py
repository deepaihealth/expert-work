"""B-209 PR-1 —— 入库路径上的扫描页识别:逐页判定 → ``RECOGNIZING`` → 按页拼回 → 切块入库;
每页的结果记在文档上(不允许静默空页),识别与入库嵌入记租户的 ``knowledge_ingest`` 用量。

识别模型是假的(不联网);PDF 现场生成,「扫描页」= 一张写着字的图片铺满页面、没有文字层。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pymupdf
import pytest

from control_plane.knowledge.ingestion import (
    KnowledgeIngestionRunner,
    StaleIngestionError,
    failure_code,
    ingest_document_bytes,
    shutdown_parse_pool,
)
from control_plane.knowledge.parsing import DocumentParseError
from control_plane.knowledge.recognition import recognizer as recognizer_module
from control_plane.knowledge.recognition.clients import Reading, RecognitionClient
from control_plane.knowledge.recognition.pages import render_pages
from control_plane.knowledge.recognition.recognizer import (
    DocumentRecognizer,
    ModelChoice,
    RecognitionSettings,
)
from control_plane.knowledge.usage import IngestUsageRecorder
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.token_usage_store import (
    KNOWLEDGE_INGEST_USAGE_KIND,
    InMemoryTokenUsageStore,
)
from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    DocumentPage,
    DocumentStatus,
    IngestErrorCode,
    IngestStage,
    PageDecision,
    PageReason,
    PageStats,
    PageStatus,
)
from orchestrator.llm import FakeEmbedder, OpenAICompatibleEmbedder

_TEXT_PAGE = "Leave requests are submitted three working days ahead and approved by the manager. "
_RECOGNIZED = "第二章 报销制度\n\n差旅费用凭发票在出差结束后十个工作日内报销。"


@pytest.fixture(scope="module", autouse=True)
def _parse_pool_teardown() -> Iterator[None]:
    # PDFs are analysed and rendered in the real (spawned) parse pool.
    yield
    shutdown_parse_pool()


def _scan_image() -> bytes:
    source = pymupdf.open()
    page = source.new_page()
    page.insert_text((72, 72), "Scanned words on paper " * 6, fontsize=14)
    return bytes(page.get_pixmap(dpi=72).tobytes("png"))


def _pdf(*kinds: str) -> bytes:
    """``"text"`` / ``"scan"`` pages in order."""
    doc = pymupdf.open()
    for kind in kinds:
        page = doc.new_page()
        if kind == "text":
            page.insert_text((72, 72), _TEXT_PAGE * 3, fontsize=8)
        else:
            page.insert_image(page.rect, stream=_scan_image())
    return bytes(doc.tobytes())


@dataclass
class _Settings:
    max_pages: int = 1000
    primary: ModelChoice | None = field(default_factory=lambda: ModelChoice("glm", "glm-ocr"))

    async def recognition_settings(self) -> RecognitionSettings:
        return RecognitionSettings(primary=self.primary, fallback=None, max_pages=self.max_pages)


class _Reader:
    """Fake recognition model: every page reads as :data:`_RECOGNIZED`."""

    def __init__(self) -> None:
        self.calls = 0

    async def read(self, image: bytes) -> Reading:
        assert image.startswith(b"\xff\xd8")  # a JPEG rendering of the page
        self.calls += 1
        return Reading(text=_RECOGNIZED, input_tokens=1500, output_tokens=60, usage_reported=True)


async def _keys(*, tenant_id: UUID, provider: str) -> str:
    del tenant_id
    return f"key-{provider}"


async def _no_keys(*, tenant_id: UUID, provider: str) -> str:
    del tenant_id
    msg = f"no platform credential for provider {provider!r}"
    raise LookupError(msg)


def _recognizer(
    store: InMemoryKnowledgeStore,
    reader: _Reader,
    *,
    settings: _Settings | None = None,
    usage: IngestUsageRecorder | None = None,
    keys: Any = _keys,
) -> DocumentRecognizer:
    def factory(choice: ModelChoice, api_key: str) -> RecognitionClient:
        del choice, api_key
        return reader

    return DocumentRecognizer(
        store=store,
        settings=settings or _Settings(),
        api_keys=keys,
        usage=usage,
        client_factory=factory,
    )


class _StageStore(InMemoryKnowledgeStore):
    def __init__(self) -> None:
        super().__init__()
        self.stages: list[IngestStage] = []

    async def set_document_stage(self, **kwargs: Any) -> bool:
        self.stages.append(kwargs["stage"])
        return await super().set_document_stage(**kwargs)


@dataclass
class _Doc:
    store: _StageStore
    tenant: UUID
    kb_id: UUID
    document_id: UUID


async def _document(filename: str, raw: bytes) -> _Doc:
    store, tenant = _StageStore(), uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename=filename, content=raw
    )
    return _Doc(store, tenant, base.id, document.id)


async def _ingest(
    doc: _Doc,
    filename: str,
    raw: bytes,
    *,
    recognizer: DocumentRecognizer | None,
    usage: IngestUsageRecorder | None = None,
    embedder: Any = None,
) -> int:
    return await ingest_document_bytes(
        store=doc.store,
        embedder=embedder or FakeEmbedder(),
        tenant_id=doc.tenant,
        document_id=doc.document_id,
        kb_id=doc.kb_id,
        filename=filename,
        raw=raw,
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=DEFAULT_CHUNK_OVERLAP_TOKENS,
        recognizer=recognizer,
        usage=usage,
    )


async def _bodies(doc: _Doc) -> str:
    chunks, _ = await doc.store.list_chunks(
        tenant_id=doc.tenant, document_id=doc.document_id, limit=1000
    )
    return "\n".join(chunk.content for chunk in chunks)


async def _pages(doc: _Doc) -> list[DocumentPage]:
    pages = await doc.store.list_document_pages(tenant_id=doc.tenant, document_id=doc.document_id)
    assert pages is not None
    return pages


async def test_mixed_pdf_reads_its_scanned_page_in_place() -> None:
    raw = _pdf("text", "scan", "text")
    doc = await _document("handbook.pdf", raw)
    reader = _Reader()
    await _ingest(doc, "handbook.pdf", raw, recognizer=_recognizer(doc.store, reader))

    assert reader.calls == 1
    assert IngestStage.RECOGNIZING in doc.store.stages
    assert doc.store.stages.index(IngestStage.RECOGNIZING) < doc.store.stages.index(
        IngestStage.CHUNKING
    )
    bodies = "".join((await _bodies(doc)).split())
    assert "".join(_RECOGNIZED.split()) in bodies
    assert "Leaverequestsaresubmitted" in bodies
    pages = await _pages(doc)
    assert [(p.page, p.decision, p.status) for p in pages] == [
        (1, PageDecision.TEXT, None),
        (2, PageDecision.OCR, PageStatus.RECOGNIZED),
        (3, PageDecision.TEXT, None),
    ]
    assert (pages[1].reason, pages[1].provider, pages[1].model) == (
        PageReason.SPARSE_TEXT,
        "glm",
        "glm-ocr",
    )
    assert pages[1].chars == len("".join(_RECOGNIZED.split()))
    document = await doc.store.get_document(tenant_id=doc.tenant, document_id=doc.document_id)
    assert document is not None
    assert document.page_count == 3
    assert document.page_stats == PageStats(pages_total=3, pages_ocr=1)


async def test_recognized_text_keeps_page_order() -> None:
    raw = _pdf("scan", "text")
    doc = await _document("order.pdf", raw)
    await _ingest(doc, "order.pdf", raw, recognizer=_recognizer(doc.store, _Reader()))
    bodies = "".join((await _bodies(doc)).split())
    assert bodies.index("第二章报销制度") < bodies.index("Leaverequestsaresubmitted")


async def test_without_a_recognizer_the_scanned_page_is_recorded_not_silently_empty() -> None:
    # Before B-209 a mixed PDF ingested 「successfully」 with its scanned pages
    # silently empty (``_is_sparse`` averaged over the whole document).
    raw = _pdf("text", "scan", "text")
    doc = await _document("handbook.pdf", raw)
    await _ingest(doc, "handbook.pdf", raw, recognizer=None)
    pages = await _pages(doc)
    assert pages[1].status is PageStatus.PENDING
    assert pages[1].error == "document recognition is not set up"
    document = await doc.store.get_document(tenant_id=doc.tenant, document_id=doc.document_id)
    assert document is not None and document.page_stats is not None
    assert document.page_stats.pages_pending == 1
    assert document.page_stats.needs_recognition


async def test_scanned_pdf_with_no_usable_model_fails_with_the_reason_and_records_pages() -> None:
    raw = _pdf("scan", "scan")
    doc = await _document("contract.pdf", raw)
    reader = _Reader()
    with pytest.raises(DocumentParseError) as caught:
        await _ingest(
            doc, "contract.pdf", raw, recognizer=_recognizer(doc.store, reader, keys=_no_keys)
        )
    assert failure_code(caught.value) is IngestErrorCode.PARSE_FAILED
    message = str(caught.value)
    assert "2 of 2 pages scanned" in message
    assert "2 waiting for recognition" in message
    assert "no platform credential for provider 'glm'" in message
    assert reader.calls == 0
    # The pages are on record even though the document failed.
    assert [p.status for p in await _pages(doc)] == [PageStatus.PENDING, PageStatus.PENDING]


async def test_pages_past_the_cap_are_marked_and_not_read() -> None:
    raw = _pdf("scan", "scan", "scan")
    doc = await _document("book.pdf", raw)
    reader = _Reader()
    recognizer = _recognizer(doc.store, reader, settings=_Settings(max_pages=2))
    await _ingest(doc, "book.pdf", raw, recognizer=recognizer)
    assert reader.calls == 2
    statuses = [p.status for p in await _pages(doc)]
    assert statuses == [PageStatus.RECOGNIZED, PageStatus.RECOGNIZED, PageStatus.OVER_LIMIT]


async def test_text_only_pdf_skips_recognition() -> None:
    raw = _pdf("text", "text")
    doc = await _document("plain.pdf", raw)
    reader = _Reader()
    await _ingest(doc, "plain.pdf", raw, recognizer=_recognizer(doc.store, reader))
    assert reader.calls == 0
    assert IngestStage.RECOGNIZING not in doc.store.stages
    assert [p.decision for p in await _pages(doc)] == [PageDecision.TEXT, PageDecision.TEXT]


class _ImageEcho:
    """Fake model whose reading names the image it was given."""

    async def read(self, image: bytes) -> Reading:
        return Reading(text=f"识别页 {hashlib.sha256(image).hexdigest()[:12]}")


async def test_pages_rendered_in_several_batches_keep_their_own_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recognizer_module, "RENDER_BATCH", 2)
    doc_pdf = pymupdf.open()
    for number in range(5):
        page = doc_pdf.new_page()
        picture = pymupdf.open()
        picture.new_page().insert_text((72, 72), f"Scanned page number {number} " * 4)
        page.insert_image(page.rect, stream=picture[0].get_pixmap(dpi=72).tobytes("png"))
    raw = bytes(doc_pdf.tobytes())
    expected = [
        f"识别页{hashlib.sha256(image).hexdigest()[:12]}"
        for image in render_pages(raw, list(range(5)))
    ]
    assert len(set(expected)) == 5
    doc = await _document("five.pdf", raw)
    echo = _ImageEcho()
    recognizer = DocumentRecognizer(
        store=doc.store,
        settings=_Settings(),
        api_keys=_keys,
        client_factory=lambda choice, key: echo,
    )
    await _ingest(doc, "five.pdf", raw, recognizer=recognizer)
    bodies = "".join((await _bodies(doc)).split())
    positions = [bodies.index(text) for text in expected]
    assert positions == sorted(positions)


class _RefusingPagesStore(_StageStore):
    async def set_document_pages(self, **kwargs: Any) -> bool:
        del kwargs
        return False


async def test_a_refused_page_write_stops_the_stale_worker() -> None:
    raw = _pdf("text", "scan")
    store, tenant = _RefusingPagesStore(), uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="a.pdf", content=raw
    )
    doc = _Doc(store, tenant, base.id, document.id)
    with pytest.raises(StaleIngestionError):
        await _ingest(doc, "a.pdf", raw, recognizer=_recognizer(store, _Reader()))


async def test_recognition_is_billed_to_the_uploading_tenant() -> None:
    raw = _pdf("scan")
    doc = await _document("scan.pdf", raw)
    usage_store = InMemoryTokenUsageStore()
    usage = IngestUsageRecorder(usage_store)
    await _ingest(
        doc,
        "scan.pdf",
        raw,
        recognizer=_recognizer(doc.store, _Reader(), usage=usage),
        usage=usage,
    )
    rows = await usage_store.list_for_tenant(tenant_id=doc.tenant)
    recognition = [row for row in rows if row.model == "glm-ocr"]
    assert len(recognition) == 1
    row = recognition[0]
    assert (row.usage_kind, row.provider, row.agent_name) == (
        KNOWLEDGE_INGEST_USAGE_KIND,
        "glm",
        "",
    )
    assert (row.input_tokens, row.output_tokens) == (1500, 60)


@dataclass
class _EmbeddingsApi:
    """An OpenAI-compatible embeddings endpoint reporting ``usage``."""

    calls: list[int] = field(default_factory=list)

    async def embeddings(
        self, *, model: str, texts: Sequence[str], fields: Mapping[str, str] | None = None
    ) -> Mapping[str, Any]:
        del model, fields
        self.calls.append(len(texts))
        return {
            "data": [{"index": i, "embedding": [0.1, 0.2, 0.3]} for i in range(len(texts))],
            "usage": {"prompt_tokens": 7 * len(texts), "total_tokens": 7 * len(texts)},
        }


async def test_ingest_embeddings_are_billed_to_the_uploading_tenant() -> None:
    raw = ("# 制度\n\n" + "员工手册的内容。" * 40).encode()
    doc = await _document("manual.md", raw)
    usage_store = InMemoryTokenUsageStore()
    api = _EmbeddingsApi()
    await _ingest(
        doc,
        "manual.md",
        raw,
        recognizer=None,
        usage=IngestUsageRecorder(usage_store),
        embedder=OpenAICompatibleEmbedder(client=api, model="text-embedding-v4"),
    )
    rows = await usage_store.list_for_tenant(tenant_id=doc.tenant)
    assert len(rows) == 1
    row = rows[0]
    assert (row.model, row.provider, row.usage_kind, row.agent_name) == (
        "text-embedding-v4",
        "qwen",
        KNOWLEDGE_INGEST_USAGE_KIND,
        "",
    )
    assert row.input_tokens == 7 * sum(api.calls) > 0


async def test_without_a_recorder_nothing_is_billed() -> None:
    raw = ("# 制度\n\n" + "员工手册的内容。" * 40).encode()
    doc = await _document("manual.md", raw)
    api = _EmbeddingsApi()
    await _ingest(
        doc,
        "manual.md",
        raw,
        recognizer=None,
        embedder=OpenAICompatibleEmbedder(client=api, model="text-embedding-v4"),
    )
    assert api.calls  # embedded, nowhere to bill: fine


async def test_runner_recognizes_through_the_parse_process_pool() -> None:
    # Page analysis and rendering run in the spawned parse pool (PyMuPDF is
    # not thread-safe): what crosses the process boundary must pickle.
    raw = _pdf("text", "scan")
    store, tenant = InMemoryKnowledgeStore(), uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="mixed.pdf", content=raw, content_sha256="x"
    )
    reader = _Reader()
    runner = KnowledgeIngestionRunner(
        store=store, embedder=FakeEmbedder(), recognizer=_recognizer(store, reader)
    )
    runner.submit(tenant_id=tenant, document_id=document.id)
    await runner.drain()
    fetched = await store.get_document(tenant_id=tenant, document_id=document.id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.READY, fetched.error
    assert fetched.page_stats == PageStats(pages_total=2, pages_ocr=1)
    assert reader.calls == 1
