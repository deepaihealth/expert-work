"""B-222 —— an ingest cancelled while it runs stops paying at its next checkpoint.

Cancelling (``KnowledgeStore.cancel_documents``) bumps the document's
generation, so every fenced write of the worker still busy with it is refused.
Each step that spends money checks before its next batch: chunk and topic-break
embeddings before each window, scanned-page recognition before each batch of
pages, figure description before each model call. On the pod that runs the
ingest, ``KnowledgeIngestionRunner.cancel_local`` also stops it at once.

Embedding / recognition / vision models are fakes (no network); files are
synthesised here (no tenant content) and parsed in the real parse pool.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pymupdf
import pytest

from control_plane.knowledge.estimate import IngestEstimator
from control_plane.knowledge.ingestion import (
    KnowledgeIngestionRunner,
    StaleIngestionError,
    parse_locally,
    shutdown_parse_pool,
)
from control_plane.knowledge.recognition.clients import (
    Reading,
    RecognitionCallError,
    RecognitionClient,
)
from control_plane.knowledge.recognition.figure_extract import FigureExtraction
from control_plane.knowledge.recognition.figure_filters import FigureCandidate
from control_plane.knowledge.recognition.figure_pipeline import FigureDescriber, FigureOutcome
from control_plane.knowledge.recognition.figure_storage import FigureStorage
from control_plane.knowledge.recognition.recognizer import (
    DocumentRecognizer,
    ModelChoice,
    RecognitionSettings,
    model_concurrency,
)
from control_plane.knowledge.recovery import KnowledgeIngestRecoveryWorker
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.knowledge.base import (
    PAGE_OCR_PAGE,
    PAGE_OCR_READER,
    PAGE_OCR_SOURCE,
    ClaimedIngestion,
)
from expert_work.protocol import DEFAULT_CHUNK_MAX_TOKENS, DocumentStatus
from expert_work.protocol.knowledge_estimate import EstimateKind
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.llm import FakeEmbedder
from tests.figure_test_images import noise_png

_TENANT = uuid4()
#: Chunks / blocks sent per embedding window (``CHUNK_EMBED_WINDOW`` /
#: ``SEMANTIC_EMBED_WINDOW`` = 200), 10 texts a request: one window's requests.
_ONE_WINDOW_OF_REQUESTS = 20
_PHOTO = json.dumps({"text": "none", "chart": False, "description": "A photo of a device."})
_TEXT_SHOT = json.dumps({"text": "lots", "chart": False, "description": "A screenshot of steps."})


@pytest.fixture(scope="module", autouse=True)
def _parse_pool_teardown() -> Iterator[None]:
    yield
    shutdown_parse_pool()


def _entries(count: int) -> str:
    """Headed entries, each long enough to stand as a chunk of its own."""
    return "\n\n".join(
        f"## Entry {i}\n\n" + f"Entry {i} explains one rule of the handbook. " * 8
        for i in range(count)
    )


def _one_section(paragraphs: int) -> str:
    """Paragraphs under one heading: every adjacent pair is compared for a topic shift."""
    return "# Handbook\n\n" + "\n\n".join(
        f"Paragraph {i} explains one rule of the handbook." for i in range(paragraphs)
    )


async def _document(store: InMemoryKnowledgeStore, filename: str, raw: bytes) -> tuple[UUID, UUID]:
    base = await store.create_base(tenant_id=_TENANT, name=f"kb-{uuid4().hex[:6]}")
    document = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename=filename, content=raw, content_sha256="sha"
    )
    return base.id, document.id


class _Canceller:
    """Cancels one document through the store, as the cancel endpoint does."""

    def __init__(self, store: InMemoryKnowledgeStore, kb_id: UUID, document_id: UUID) -> None:
        self._store = store
        self._kb_id = kb_id
        self._document_id = document_id

    async def __call__(self) -> None:
        outcome = await self._store.cancel_documents(
            tenant_id=_TENANT, kb_id=self._kb_id, document_ids=[self._document_id]
        )
        assert outcome == {self._document_id: "cancelled"}


class _CancellingEmbedder:
    """:class:`FakeEmbedder` vectors; records every request's texts; the
    document is cancelled during the first request."""

    def __init__(self, cancel: _Canceller) -> None:
        self._cancel = cancel
        self._inner = FakeEmbedder()
        self.calls: list[list[str]] = []

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.calls.append(list(texts))
        if len(self.calls) == 1:
            await self._cancel()
        return await self._inner.embed(texts, tenant_id=tenant_id)


async def _assert_cancelled_and_empty(store: InMemoryKnowledgeStore, document_id: UUID) -> None:
    document = await store.get_document(tenant_id=_TENANT, document_id=document_id)
    assert document is not None
    assert document.status is DocumentStatus.CANCELLED
    assert document.error == "cancelled by user"
    assert document.chunk_count == 0
    _, total = await store.list_chunks(tenant_id=_TENANT, document_id=document_id)
    assert total == 0


# ---------------------------------------------------------------------------
# embeddings
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_cancel_stops_before_next_embedding_window() -> None:
    store = InMemoryKnowledgeStore()
    kb_id, document_id = await _document(store, "entries.md", _entries(450).encode())
    embedder = _CancellingEmbedder(_Canceller(store, kb_id, document_id))
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)

    await runner.submit(tenant_id=_TENANT, document_id=document_id)

    # 450 chunks are three windows (200 / 200 / 50 = 45 requests): cancelled
    # during the first, the guard before the second stops the ingest.
    assert 0 < len(embedder.calls) <= _ONE_WINDOW_OF_REQUESTS
    await _assert_cancelled_and_empty(store, document_id)


@pytest.mark.usefixtures("semantic_breaks")
async def test_cancel_stops_semantic_break_embedding() -> None:
    store = InMemoryKnowledgeStore()
    kb_id, document_id = await _document(store, "handbook.md", _one_section(450).encode())
    embedder = _CancellingEmbedder(_Canceller(store, kb_id, document_id))
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)

    await runner.submit(tenant_id=_TENANT, document_id=document_id)

    # 450 blocks to compare are three windows too; no chunk is embedded after.
    assert 0 < len(embedder.calls) <= _ONE_WINDOW_OF_REQUESTS
    assert not [call for call in embedder.calls if call[0].startswith("[Document: ")]
    await _assert_cancelled_and_empty(store, document_id)


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_recovery_drive_stops_on_cancel() -> None:
    store = InMemoryKnowledgeStore()
    kb_id, document_id = await _document(store, "entries.md", _entries(450).encode())
    embedder = _CancellingEmbedder(_Canceller(store, kb_id, document_id))
    worker = KnowledgeIngestRecoveryWorker(
        store=store,
        embedder=embedder,
        interval_s=30,
        concurrency=1,
        lease_seconds=300,
        max_attempts=5,
    )

    await worker.run_once()

    assert 0 < len(embedder.calls) <= _ONE_WINDOW_OF_REQUESTS
    await _assert_cancelled_and_empty(store, document_id)


# ---------------------------------------------------------------------------
# figure description / scanned-page recognition
# ---------------------------------------------------------------------------


class _CancellingModel:
    """A vision / recognition model that cancels the document on its first call."""

    def __init__(self, cancel: Callable[[], Awaitable[None]], text: str) -> None:
        self._cancel = cancel
        self._text = text
        self.calls = 0

    def vision(
        self, choice: ModelChoice, api_key: str, prompt: str, user_text: str, max_tokens: int
    ) -> RecognitionClient:
        del choice, api_key, prompt, user_text, max_tokens
        return self

    def reader(self, choice: ModelChoice, api_key: str) -> RecognitionClient:
        del choice, api_key
        return self

    async def read(self, image: bytes) -> Reading:
        del image
        self.calls += 1
        if self.calls == 1:
            await self._cancel()
        return Reading(
            text=self._text,
            finish_reason="stop",
            input_tokens=700,
            output_tokens=40,
            usage_reported=True,
        )


class _Settings:
    async def recognition_settings(self) -> RecognitionSettings:
        return RecognitionSettings(
            primary=ModelChoice("glm", "glm-ocr"),
            fallback=None,
            max_pages=1000,
            figure=ModelChoice("qwen", "qwen3.8-flash"),
        )


async def _keys(*, tenant_id: UUID, provider: str) -> str:
    del tenant_id
    return f"key-{provider}"


def _pictures_pdf(count: int) -> bytes:
    """``count`` text pages, each with a different picture."""
    doc = pymupdf.open()
    for number in range(count):
        page = doc.new_page()
        page.insert_text(
            (72, 80), f"Page {number} describes a part of the device. " * 3, fontsize=8
        )
        page.insert_image(
            pymupdf.Rect(72, 120, 372, 345), stream=noise_png(300, 225, seed=number + 1)
        )
    return bytes(doc.tobytes())


def _scan_image(number: int) -> bytes:
    source = pymupdf.open()
    page = source.new_page()
    page.insert_text((72, 72), f"Scanned page {number} on paper " * 6, fontsize=14)
    return bytes(page.get_pixmap(dpi=72).tobytes("png"))


def _scanned_pdf(pages: int) -> bytes:
    """``pages`` scanned pages, each different (identical pages would be read
    once and served from the recognition cache)."""
    doc = pymupdf.open()
    for number in range(pages):
        page = doc.new_page()
        page.insert_image(page.rect, stream=_scan_image(number))
    return bytes(doc.tobytes())


async def test_cancel_stops_figure_description() -> None:
    store = InMemoryKnowledgeStore()
    kb_id, document_id = await _document(store, "device.pdf", _pictures_pdf(10))
    model = _CancellingModel(_Canceller(store, kb_id, document_id), _PHOTO)
    objects = InMemoryObjectStore()
    figures = FigureDescriber(
        store=store,
        settings=_Settings(),
        api_keys=_keys,
        storage=FigureStorage(objects),
        client_factory=model.vision,
    )
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder(), figures=figures)

    await runner.submit(tenant_id=_TENANT, document_id=document_id)

    # Ten figures, four calls at a time (the model's slots): the calls already
    # under way finish, no further one starts.
    assert 0 < model.calls <= 4
    await _assert_cancelled_and_empty(store, document_id)


class _RefuseAfter:
    """A ``before_call`` that lets ``allowed`` checks pass, then refuses (the
    document is no longer this worker's)."""

    def __init__(self, allowed: int) -> None:
        self._allowed = allowed
        self.checks = 0

    async def __call__(self) -> None:
        self.checks += 1
        if self.checks > self._allowed:
            raise StaleIngestionError("refused")


class _FailOnce:
    """A ``before_call`` whose first check fails for another reason (a
    database blip) — not a sign the document was lost."""

    def __init__(self) -> None:
        self.checks = 0

    async def __call__(self) -> None:
        self.checks += 1
        if self.checks == 1:
            raise ConnectionError("server closed the connection unexpectedly")


class _Never:
    async def __call__(self) -> None:
        return None


class _BusyOnce:
    """A vision model whose first call is throttled (retried), then answers."""

    def __init__(self) -> None:
        self.calls = 0

    def vision(
        self, choice: ModelChoice, api_key: str, prompt: str, user_text: str, max_tokens: int
    ) -> RecognitionClient:
        del choice, api_key, prompt, user_text, max_tokens
        return self

    async def read(self, image: bytes) -> Reading:
        del image
        self.calls += 1
        if self.calls == 1:
            raise RecognitionCallError("429 busy", kind="busy")
        return Reading(text=_PHOTO, finish_reason="stop", usage_reported=True)


async def _no_sleep(seconds: float) -> None:
    """Stands in for asyncio.sleep; never waits."""


def _candidate(number: int) -> FigureCandidate:
    return FigureCandidate(
        number=number,
        image=f"jpeg-{number}".encode(),
        width=600,
        height=600,
        pixel_hash=f"pixels-{number}",
        pages=(number,),
    )


async def _describe(
    store: InMemoryKnowledgeStore,
    describer: FigureDescriber,
    count: int,
    before_call: Callable[[], Awaitable[None]],
) -> FigureOutcome:
    kb_id, document_id = await _document(store, "device.pdf", b"pdf")
    figures = tuple(_candidate(number) for number in range(1, count + 1))
    return await describer.describe(
        tenant_id=_TENANT,
        kb_id=kb_id,
        document_id=document_id,
        extraction=FigureExtraction(candidates=figures, extracted=count),
        language="en",
        before_call=before_call,
    )


async def test_describe_raises_the_refusal_once_the_calls_under_way_are_done() -> None:
    store = InMemoryKnowledgeStore()
    model = _CancellingModel(_Never(), _PHOTO)
    describer = FigureDescriber(
        store=store, settings=_Settings(), api_keys=_keys, client_factory=model.vision
    )
    before_call = _RefuseAfter(1)

    with pytest.raises(StaleIngestionError):
        await _describe(store, describer, 10, before_call)

    assert 1 <= model.calls <= 4
    # The first refusal stands for every later call: it is not asked again.
    assert before_call.checks <= model.calls + 1


async def test_a_check_that_errors_is_not_a_refusal() -> None:
    store = InMemoryKnowledgeStore()
    model = _CancellingModel(_Never(), _PHOTO)
    describer = FigureDescriber(
        store=store, settings=_Settings(), api_keys=_keys, client_factory=model.vision
    )
    before_call = _FailOnce()

    outcome = await _describe(store, describer, 2, before_call)

    # The failed check did not stop the call, nor poison the next check.
    assert outcome.stats.described == 2
    assert model.calls == 2
    assert before_call.checks == 2


async def test_each_retry_is_checked_again() -> None:
    """A retry is a paid call too: a document lost between attempts pays for no retry."""
    store = InMemoryKnowledgeStore()
    model = _BusyOnce()
    describer = FigureDescriber(
        store=store,
        settings=_Settings(),
        api_keys=_keys,
        client_factory=model.vision,
        sleep=_no_sleep,
    )
    before_call = _RefuseAfter(1)

    with pytest.raises(StaleIngestionError):
        await _describe(store, describer, 1, before_call)

    assert model.calls == 1
    assert before_call.checks == 2


async def test_describe_checks_before_reading_a_figures_text() -> None:
    store = InMemoryKnowledgeStore()
    describer_model = _CancellingModel(_Never(), _TEXT_SHOT)
    reader = _CancellingModel(_Never(), "Step one: switch it on.")
    recognizer = DocumentRecognizer(
        store=store, settings=_Settings(), api_keys=_keys, client_factory=reader.reader
    )
    describer = FigureDescriber(
        store=store,
        settings=_Settings(),
        api_keys=_keys,
        recognizer=recognizer,
        client_factory=describer_model.vision,
    )

    with pytest.raises(StaleIngestionError):
        await _describe(store, describer, 1, _RefuseAfter(1))

    assert describer_model.calls == 1
    assert reader.calls == 0


async def test_figure_text_checks_before_each_model_call() -> None:
    """Text-heavy figures go to the recognition model all at once, within its
    concurrency slots: each call is checked when its slot comes free."""
    store = InMemoryKnowledgeStore()
    describer_model = _CancellingModel(_Never(), _TEXT_SHOT)
    reader = _CancellingModel(_Never(), "Step one: switch it on.")
    recognizer = DocumentRecognizer(
        store=store, settings=_Settings(), api_keys=_keys, client_factory=reader.reader
    )
    describer = FigureDescriber(
        store=store,
        settings=_Settings(),
        api_keys=_keys,
        recognizer=recognizer,
        client_factory=describer_model.vision,
    )
    figures = 6
    # Every description passes its check, and the first text read.
    before_call = _RefuseAfter(figures + 1)

    with pytest.raises(StaleIngestionError):
        await _describe(store, describer, figures, before_call)

    assert describer_model.calls == figures
    # At most the calls already under way when the refusal came.
    assert 1 <= reader.calls <= model_concurrency(ModelChoice("glm", "glm-ocr")) < figures


async def test_cancel_stops_recognition_between_batches() -> None:
    store = InMemoryKnowledgeStore()
    kb_id, document_id = await _document(store, "scan.pdf", _scanned_pdf(40))
    model = _CancellingModel(_Canceller(store, kb_id, document_id), "Recognised words.")
    recognizer = DocumentRecognizer(
        store=store, settings=_Settings(), api_keys=_keys, client_factory=model.reader
    )
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder(), recognizer=recognizer)

    await runner.submit(tenant_id=_TENANT, document_id=document_id)

    # Pages are read 16 at a time (``RENDER_BATCH``): the batch under way is
    # finished, the next one never starts.
    assert 0 < model.calls <= 16
    await _assert_cancelled_and_empty(store, document_id)


async def test_the_estimate_after_a_cancel_leaves_out_the_pages_already_read() -> None:
    # B-238: what was read before the cancel stays in the cache and is not
    # billed again — the next estimate counts it as read (10-29: 40 pages,
    # cancelled after 9, estimated 40 to read instead of 31).
    raw = _scanned_pdf(40)
    store = InMemoryKnowledgeStore()
    kb_id, document_id = await _document(store, "scan.pdf", raw)
    model = _CancellingModel(_Canceller(store, kb_id, document_id), "Recognised words.")
    recognizer = DocumentRecognizer(
        store=store, settings=_Settings(), api_keys=_keys, client_factory=model.reader
    )
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder(), recognizer=recognizer)
    await runner.submit(tenant_id=_TENANT, document_id=document_id)
    assert 0 < model.calls < 40

    claim = ClaimedIngestion(
        tenant_id=_TENANT,
        document_id=document_id,
        kb_id=kb_id,
        filename="scan.pdf",
        content=raw,
        content_sha256="sha",
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=0,
        attempts=1,
        generation=1,
        phase="estimate",
        origin="reingest",
        serving_sha256="sha",
    )
    local = await parse_locally(
        store,
        tenant_id=_TENANT,
        kb_id=kb_id,
        document_id=document_id,
        filename="scan.pdf",
        raw=raw,
        figures=None,
    )
    estimator = IngestEstimator(store=store, recognizer=recognizer)
    estimate = await estimator.estimate(claim, local, embedding_model=None, now=datetime.now(UTC))
    (line,) = [item for item in estimate.items if item.kind is EstimateKind.RECOGNITION]
    assert (line.units, line.reused_units) == (40, model.calls)

    # The promise: ingesting again reads (and bills) exactly the pages estimated.
    read_before = model.calls
    await store.upsert_document(tenant_id=_TENANT, kb_id=kb_id, filename="scan.pdf")
    await runner.submit(tenant_id=_TENANT, document_id=document_id)
    document = await store.get_document(tenant_id=_TENANT, document_id=document_id)
    assert document is not None and document.status is DocumentStatus.READY
    assert model.calls - read_before == line.units - line.reused_units


class _SwitchableSettings:
    """The platform's recognition settings; the primary model can be switched."""

    def __init__(self) -> None:
        self.primary = ModelChoice("glm", "glm-ocr")

    async def recognition_settings(self) -> RecognitionSettings:
        return RecognitionSettings(primary=self.primary, fallback=None, max_pages=1000, figure=None)


async def _never_cancel() -> None:
    return None


async def test_the_estimate_after_a_model_switch_bills_every_page_read_again() -> None:
    # B-256: the last ingest finished — every page on record as recognized,
    # the bytes unchanged — then the recognition model was switched. The ingest
    # finds no reading of the new model in the cache and reads every page again:
    # the estimate must say so (it used to count them all as read).
    raw = _scanned_pdf(6)
    store = InMemoryKnowledgeStore()
    kb_id, document_id = await _document(store, "scan.pdf", raw)
    model = _CancellingModel(_never_cancel, "Recognised words.")
    settings = _SwitchableSettings()
    recognizer = DocumentRecognizer(
        store=store, settings=settings, api_keys=_keys, client_factory=model.reader
    )
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder(), recognizer=recognizer)
    await runner.submit(tenant_id=_TENANT, document_id=document_id)
    first = await store.get_document(tenant_id=_TENANT, document_id=document_id)
    assert first is not None and first.status is DocumentStatus.READY
    assert first.page_stats is not None and first.page_stats.pages_ocr == 6
    assert model.calls == 6

    claim = ClaimedIngestion(
        tenant_id=_TENANT,
        document_id=document_id,
        kb_id=kb_id,
        filename="scan.pdf",
        content=raw,
        content_sha256="sha",
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=0,
        attempts=1,
        generation=1,
        phase="estimate",
        origin="reingest",
        serving_sha256="sha",
    )
    local = await parse_locally(
        store,
        tenant_id=_TENANT,
        kb_id=kb_id,
        document_id=document_id,
        filename="scan.pdf",
        raw=raw,
        figures=None,
    )
    estimator = IngestEstimator(store=store, recognizer=recognizer)

    async def recognition_line() -> tuple[int, int]:
        estimate = await estimator.estimate(
            claim, local, embedding_model=None, now=datetime.now(UTC)
        )
        (line,) = [item for item in estimate.items if item.kind is EstimateKind.RECOGNITION]
        return line.units, line.reused_units

    async def ingest_again() -> int:
        before = model.calls
        await store.upsert_document(tenant_id=_TENANT, kb_id=kb_id, filename="scan.pdf")
        await runner.submit(tenant_id=_TENANT, document_id=document_id)
        document = await store.get_document(tenant_id=_TENANT, document_id=document_id)
        assert document is not None and document.status is DocumentStatus.READY
        return model.calls - before

    # The same model: every page comes from the cache, none is billed.
    units, reused = await recognition_line()
    assert (units, reused) == (6, 6)
    assert await ingest_again() == units - reused == 0

    # Another model: every page is read — and billed — again, as estimated.
    settings.primary = ModelChoice("qwen", "qwen3.8-flash")
    units, reused = await recognition_line()
    assert (units, reused) == (6, 0)
    assert await ingest_again() == units - reused == 6


async def test_pages_read_before_b238_are_free_again_after_one_reingest() -> None:
    # B-256: readings cached before B-238 record no page / bytes / reader, so the
    # estimate bills them (high, the safe side) although the ingest reuses them;
    # that ingest tags them, and from then on they are estimated free.
    raw = _scanned_pdf(4)
    store = InMemoryKnowledgeStore()
    kb_id, document_id = await _document(store, "scan.pdf", raw)
    model = _CancellingModel(_never_cancel, "Recognised words.")
    recognizer = DocumentRecognizer(
        store=store, settings=_SwitchableSettings(), api_keys=_keys, client_factory=model.reader
    )
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder(), recognizer=recognizer)
    await runner.submit(tenant_id=_TENANT, document_id=document_id)
    assert model.calls == 4
    # As a reading cached before B-238 looks.
    for entry in store._parse_cache.values():
        for name in (PAGE_OCR_PAGE, PAGE_OCR_SOURCE, PAGE_OCR_READER):
            entry.result.pop(name, None)

    claim = ClaimedIngestion(
        tenant_id=_TENANT,
        document_id=document_id,
        kb_id=kb_id,
        filename="scan.pdf",
        content=raw,
        content_sha256="sha",
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=0,
        attempts=1,
        generation=1,
        phase="estimate",
        origin="reingest",
        serving_sha256="sha",
    )
    local = await parse_locally(
        store,
        tenant_id=_TENANT,
        kb_id=kb_id,
        document_id=document_id,
        filename="scan.pdf",
        raw=raw,
        figures=None,
    )
    estimator = IngestEstimator(store=store, recognizer=recognizer)

    async def recognition_line() -> tuple[int, int]:
        estimate = await estimator.estimate(
            claim, local, embedding_model=None, now=datetime.now(UTC)
        )
        (line,) = [item for item in estimate.items if item.kind is EstimateKind.RECOGNITION]
        return line.units, line.reused_units

    assert await recognition_line() == (4, 0)
    await store.upsert_document(tenant_id=_TENANT, kb_id=kb_id, filename="scan.pdf")
    await runner.submit(tenant_id=_TENANT, document_id=document_id)
    assert model.calls == 4  # reused from the cache, not read again
    assert await recognition_line() == (4, 4)


# ---------------------------------------------------------------------------
# same pod: stop at once
# ---------------------------------------------------------------------------


class _BlockingEmbedder:
    """Blocks inside ``embed`` until cancelled (an embedding request in flight)."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.aborted = False

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del texts, tenant_id
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.aborted = True
            raise
        raise AssertionError("unreachable")


class _FailureSpyStore(InMemoryKnowledgeStore):
    def __init__(self) -> None:
        super().__init__()
        self.failures: list[dict[str, Any]] = []

    async def mark_document_failed_terminal(self, **kwargs: Any) -> bool:
        self.failures.append(kwargs)
        return await super().mark_document_failed_terminal(**kwargs)


async def test_cancel_local_task_stops_in_flight_call() -> None:
    store = _FailureSpyStore()
    kb_id, document_id = await _document(store, "h.md", b"# Handbook\n\nThe deductible is 500.")
    embedder = _BlockingEmbedder()
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    task = runner.submit(tenant_id=_TENANT, document_id=document_id)
    await asyncio.wait_for(embedder.entered.wait(), timeout=30)

    await _Canceller(store, kb_id, document_id)()
    stopped = runner.cancel_local([document_id, uuid4()])

    assert stopped == 1  # only the document this pod is ingesting
    await asyncio.wait_for(task, timeout=5)  # ends quietly, not as a failure
    assert embedder.aborted
    assert store.failures == []
    await _assert_cancelled_and_empty(store, document_id)
    assert runner.cancel_local([document_id]) == 0  # nothing left running


async def test_cancel_local_leaves_other_documents_running() -> None:
    store = InMemoryKnowledgeStore()
    _, document_id = await _document(store, "h.md", b"# Handbook\n\nThe deductible is 500.")
    embedder = _BlockingEmbedder()
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    task = runner.submit(tenant_id=_TENANT, document_id=document_id)
    await asyncio.wait_for(embedder.entered.wait(), timeout=30)

    assert runner.cancel_local([uuid4()]) == 0

    await asyncio.sleep(0.05)
    assert not task.done()
    assert not embedder.aborted
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
