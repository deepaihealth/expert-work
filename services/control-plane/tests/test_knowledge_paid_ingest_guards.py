"""B-222 PR-2 —— a confirmed ingest stays on what the user confirmed (B2a review).

* it embeds with the model checked against the confirmed estimate, even when
  the platform switches model before the ingest gets to embedding;
* a recognition / figure model switched after the check sends the document
  back for a new confirmation before the next paid call;
* the parse snapshot is encoded and decoded off the event loop;
* a pod shutting down mid-ingest keeps the snapshot for the retry (a lost or
  settled claim drops it).

Models are fakes (no network).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge import parse_snapshot
from control_plane.knowledge.estimate import IngestEstimator
from control_plane.knowledge.ingestion import (
    KnowledgeIngestionRunner,
    StaleIngestionError,
    _ModelsGuard,
    shutdown_parse_pool,
)
from control_plane.knowledge.recognition.clients import Reading
from control_plane.knowledge.recognition.figure_pipeline import FigureDescriber
from control_plane.knowledge.recognition.ingest_parse import LocalParse
from control_plane.knowledge.recognition.recognizer import (
    DocumentRecognizer,
    ModelChoice,
    RecognitionSettings,
)
from control_plane.knowledge.recovery import KnowledgeIngestRecoveryWorker
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.knowledge.base import ClaimedIngestion, ParseSnapshot
from expert_work.protocol import DocumentStatus, IngestErrorCode, KnowledgeDocument
from expert_work.protocol.knowledge_estimate import EstimateModels
from orchestrator.llm import FakeEmbedder
from tests.knowledge_form_fixtures import MIXED_DOCS, PICTURE_DOCS, FormDoc

_TENANT = uuid4()
_TEXT = "\n\n".join(f"Paragraph {i}: the deductible of plan {i} is {i}00 yuan." for i in range(5))


@pytest.fixture(scope="module", autouse=True)
def _parse_pool_teardown() -> Iterator[None]:
    yield
    shutdown_parse_pool()


class _Keyed(FakeEmbedder):
    def __init__(self, model_key: str) -> None:
        super().__init__()
        self.model_key = model_key
        self.calls = 0

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.calls += 1
        return await super().embed(texts, tenant_id=tenant_id)


class _Switching(_Keyed):
    """The live platform model: ``acme/embed-1`` for the first ``switch_after``
    resolutions, ``acme/embed-2`` from then on."""

    def __init__(self, switch_after: int) -> None:
        super().__init__("acme/embed-1")
        self.switch_after = switch_after
        self.pins = 0
        self.second = _Keyed("acme/embed-2")

    async def pin(self, *, tenant_id: UUID) -> tuple[str, _Keyed]:
        self.pins += 1
        if self.pins <= self.switch_after:
            return "acme/embed-1", self
        return "acme/embed-2", self.second


class _Stuck(_Keyed):
    """Embeds nothing: hangs until cancelled (a pod shutting down)."""

    def __init__(self) -> None:
        super().__init__("acme/embed-1")
        self.entered = asyncio.Event()

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _SwitchedModels(IngestEstimator):
    """Every model check after the confirmation finds a model switched."""

    checks = 0

    async def models_changed(self, claim: ClaimedIngestion, confirmed: EstimateModels) -> bool:
        type(self).checks += 1
        return True


@dataclass
class _Paid:
    """Fake recognition and description models, counting their paid calls."""

    reads: int = 0
    descriptions: int = 0

    def reader(self, choice: ModelChoice, key: str) -> _PaidReader:
        return _PaidReader(self)

    def describer(
        self, choice: ModelChoice, key: str, prompt: str, user: str, cap: int
    ) -> _PaidDescriber:
        return _PaidDescriber(self)


@dataclass
class _PaidReader:
    paid: _Paid

    async def read(self, image: bytes) -> Reading:
        self.paid.reads += 1
        return Reading(text="Scanned page text.")


@dataclass
class _PaidDescriber:
    paid: _Paid

    async def read(self, image: bytes) -> Reading:
        self.paid.descriptions += 1
        return Reading(
            text='{"text": "none", "chart": false, "description": "A photo."}',
            finish_reason="stop",
        )


@dataclass
class _Recognition:
    async def recognition_settings(self) -> RecognitionSettings:
        return RecognitionSettings(
            primary=ModelChoice("glm", "glm-ocr"),
            fallback=None,
            max_pages=1000,
            figure=ModelChoice("qwen", "qwen3.8-flash"),
        )


async def _api_key(*, tenant_id: UUID, provider: str) -> str:
    return f"key-{provider}"


async def _waiting(
    store: InMemoryKnowledgeStore, runner: KnowledgeIngestionRunner, name: str, raw: bytes
) -> KnowledgeDocument:
    """An upload estimated (by ``runner``) and confirmed."""
    base = await store.get_base(tenant_id=_TENANT, name="kb") or await store.create_base(
        tenant_id=_TENANT, name="kb"
    )
    started = await store.begin_upload(
        tenant_id=_TENANT,
        kb_id=base.id,
        filename=name,
        content=raw,
        content_sha256=f"sha-{name}",
        now=datetime.now(UTC),
    )
    await runner.submit(tenant_id=_TENANT, document_id=started.document.id)
    waiting = await store.get_document(tenant_id=_TENANT, document_id=started.document.id)
    assert waiting is not None and waiting.ingest_estimate is not None
    outcome = await store.confirm_documents(
        tenant_id=_TENANT,
        kb_id=base.id,
        estimates=[(waiting.id, waiting.ingest_estimate.id)],
        confirmed_by="user",
        now=datetime.now(UTC),
    )
    assert outcome == {waiting.id: "confirmed"}
    return waiting


async def _snapshot_of(store: InMemoryKnowledgeStore, document: KnowledgeDocument) -> Any:
    assert document.ingest_estimate is not None
    return await store.get_parse_snapshot(
        tenant_id=_TENANT, document_id=document.id, estimate_id=document.ingest_estimate.id
    )


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_the_ingest_embeds_with_the_model_checked_against_the_confirmation() -> None:
    store = InMemoryKnowledgeStore()
    # Resolutions: the estimate (1), the check before spending (2), then the
    # platform switches — an ingest re-resolving would get embed-2.
    embedder = _Switching(switch_after=2)
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    waiting = await _waiting(store, runner, "plans.md", _TEXT.encode())

    await runner.submit(tenant_id=_TENANT, document_id=waiting.id)

    ready = await store.get_document(tenant_id=_TENANT, document_id=waiting.id)
    assert ready is not None and ready.status is DocumentStatus.READY
    assert (embedder.calls > 0, embedder.second.calls) == (True, 0)
    chunks, _ = await store.list_chunks(tenant_id=_TENANT, document_id=waiting.id, limit=50)
    assert {chunk.embedding_model for chunk in chunks} == {"acme/embed-1"}


def _recovery(
    store: InMemoryKnowledgeStore, embedder: Any, estimator: IngestEstimator | None = None
) -> KnowledgeIngestRecoveryWorker:
    return KnowledgeIngestRecoveryWorker(
        store=store,
        embedder=embedder,
        interval_s=30,
        concurrency=1,
        lease_seconds=300,
        max_attempts=5,
        estimator=estimator,
    )


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_the_recovery_worker_embeds_with_the_checked_model_too() -> None:
    store = InMemoryKnowledgeStore()
    embedder = _Switching(switch_after=2)
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    waiting = await _waiting(store, runner, "a.md", _TEXT.encode())

    await _recovery(store, embedder).run_once()

    ready = await store.get_document(tenant_id=_TENANT, document_id=waiting.id)
    assert ready is not None and ready.status is DocumentStatus.READY
    assert embedder.second.calls == 0


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_the_recovery_worker_checks_the_models_before_a_paid_call_too() -> None:
    store = InMemoryKnowledgeStore()
    embedder = _Keyed("acme/embed-1")
    mixed = MIXED_DOCS[0]
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    waiting = await _waiting(store, runner, mixed.filename, mixed.raw)

    await _recovery(store, embedder, _SwitchedModels(store=store)).run_once()

    back = await store.get_document(tenant_id=_TENANT, document_id=waiting.id)
    assert back is not None and back.status is DocumentStatus.AWAITING_CONFIRMATION
    assert embedder.calls == 0


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_a_model_switched_after_the_check_sends_it_back_before_a_paid_call() -> None:
    store = InMemoryKnowledgeStore()
    embedder = _Keyed("acme/embed-1")
    estimator = _SwitchedModels(store=store)
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder, estimator=estimator)
    mixed = MIXED_DOCS[0]  # has scanned pages: the ingest reaches recognition
    waiting = await _waiting(store, runner, mixed.filename, mixed.raw)
    assert waiting.ingest_estimate is not None

    await runner.submit(tenant_id=_TENANT, document_id=waiting.id)

    back = await store.get_document(tenant_id=_TENANT, document_id=waiting.id)
    assert back is not None and back.ingest_estimate is not None
    assert back.status is DocumentStatus.AWAITING_CONFIRMATION
    assert back.ingest_estimate.id != waiting.ingest_estimate.id
    assert back.ingest_estimate.confirmed_at is None
    assert (embedder.calls, back.chunk_count) == (0, 0)  # nothing embedded


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_snapshots_are_encoded_and_decoded_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []
    encode, decode = parse_snapshot.encode_snapshot, parse_snapshot.decode_snapshot

    def encoding(local: LocalParse) -> ParseSnapshot | None:
        calls.append(("encode", threading.get_ident()))
        return encode(local)

    def decoding(snapshot: ParseSnapshot) -> LocalParse | None:
        calls.append(("decode", threading.get_ident()))
        return decode(snapshot)

    monkeypatch.setattr(parse_snapshot, "encode_snapshot", encoding)
    monkeypatch.setattr(parse_snapshot, "decode_snapshot", decoding)
    store = InMemoryKnowledgeStore()
    runner = KnowledgeIngestionRunner(store=store, embedder=_Keyed("acme/embed-1"))
    waiting = await _waiting(store, runner, "plans.md", _TEXT.encode())
    await runner.submit(tenant_id=_TENANT, document_id=waiting.id)

    loop_thread = threading.get_ident()
    assert sorted({kind for kind, _ in calls}) == ["decode", "encode"]
    assert all(thread != loop_thread for _, thread in calls)


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_a_shutdown_mid_ingest_keeps_the_snapshot_for_the_retry() -> None:
    store = InMemoryKnowledgeStore()
    waiting = await _waiting(
        store,
        KnowledgeIngestionRunner(store=store, embedder=_Keyed("acme/embed-1")),
        "plans.md",
        _TEXT.encode(),
    )
    stuck = _Stuck()
    task = KnowledgeIngestionRunner(store=store, embedder=stuck).submit(
        tenant_id=_TENANT, document_id=waiting.id
    )
    await stuck.entered.wait()

    task.cancel()  # the runner's shutdown cancels its tasks
    await asyncio.gather(task, return_exceptions=True)

    assert await _snapshot_of(store, waiting) is not None
    still = await store.get_document(tenant_id=_TENANT, document_id=waiting.id)
    assert still is not None and still.status is DocumentStatus.PROCESSING  # retried later


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_a_shutdown_of_the_recovery_worker_keeps_the_snapshot_too() -> None:
    store = InMemoryKnowledgeStore()
    waiting = await _waiting(
        store,
        KnowledgeIngestionRunner(store=store, embedder=_Keyed("acme/embed-1")),
        "plans.md",
        _TEXT.encode(),
    )
    stuck = _Stuck()
    worker = KnowledgeIngestRecoveryWorker(
        store=store,
        embedder=stuck,
        interval_s=30,
        concurrency=1,
        lease_seconds=300,
        max_attempts=5,
    )
    cycle = asyncio.create_task(worker.run_once())
    await stuck.entered.wait()

    cycle.cancel()
    await asyncio.gather(cycle, return_exceptions=True)

    assert await _snapshot_of(store, waiting) is not None


# -- B2b review M6: no paid call after a model switch ------------------------------


@pytest.mark.parametrize(
    "doc",
    [MIXED_DOCS[0], PICTURE_DOCS[0]],
    ids=["scanned-page-recognition", "picture-description"],
)
@pytest.mark.usefixtures("no_semantic_breaks")
async def test_a_model_switch_mid_ingest_pays_for_no_recognition_or_description(
    doc: FormDoc,
) -> None:
    store = InMemoryKnowledgeStore()
    paid = _Paid()
    settings = _Recognition()
    recognizer = DocumentRecognizer(
        store=store, settings=settings, api_keys=_api_key, client_factory=paid.reader
    )
    figures = FigureDescriber(
        store=store,
        settings=settings,
        api_keys=_api_key,
        recognizer=recognizer,
        client_factory=paid.describer,
    )
    embedder = _Keyed("acme/embed-1")
    runner = KnowledgeIngestionRunner(
        store=store,
        embedder=embedder,
        recognizer=recognizer,
        figures=figures,
        estimator=_SwitchedModels(store=store, settings=settings, recognizer=recognizer),
    )
    waiting = await _waiting(store, runner, doc.filename, doc.raw)

    await runner.submit(tenant_id=_TENANT, document_id=waiting.id)

    assert (paid.reads, paid.descriptions, embedder.calls) == (0, 0, 0)
    back = await store.get_document(tenant_id=_TENANT, document_id=waiting.id)
    assert back is not None and back.status is DocumentStatus.AWAITING_CONFIRMATION


def _claim_of(document: KnowledgeDocument) -> ClaimedIngestion:
    return ClaimedIngestion(
        tenant_id=_TENANT,
        document_id=document.id,
        kb_id=document.kb_id,
        filename=document.filename,
        content=_TEXT.encode(),
        content_sha256="sha",
        chunk_max_tokens=400,
        chunk_overlap_tokens=0,
        attempts=1,
        generation=document.ingest_generation,
    )


class _BrokenStore(InMemoryKnowledgeStore):
    sent_back = 0

    async def return_to_confirmation(self, **kwargs: Any) -> bool:
        type(self).sent_back += 1
        msg = "database went away"
        raise RuntimeError(msg)


async def test_the_models_guard_fails_closed_and_checks_once() -> None:
    store = _BrokenStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    document = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="a.md", content=_TEXT.encode()
    )
    estimator = _SwitchedModels(store=store)
    _SwitchedModels.checks = 0
    confirmed = await estimator.estimate(
        _claim_of(document),
        LocalParse(kind="text", figures_on=False, markdown=_TEXT),
        embedding_model="acme/embed-1",
        now=datetime.now(UTC),
    )
    guard = _ModelsGuard(
        store,
        estimator,
        _claim_of(document),
        LocalParse(kind="text", figures_on=False, markdown=_TEXT),
        confirmed,
    )

    outcomes = await asyncio.gather(*(guard() for _ in range(5)), return_exceptions=True)

    # Sending it back failed, yet every caller is refused; checked and sent back once.
    assert all(isinstance(outcome, StaleIngestionError) for outcome in outcomes)
    assert (_SwitchedModels.checks, _BrokenStore.sent_back) == (1, 1)
    with pytest.raises(StaleIngestionError):
        await guard()
    assert _SwitchedModels.checks == 1


# -- B2b second review: a check that cannot tell refuses; keys are not models -------


class _CheckFails(IngestEstimator):
    """The model check itself fails (settings or key lookup) — after
    answering ``passes`` checks."""

    checks = 0
    passes = 0

    async def models_changed(self, claim: ClaimedIngestion, confirmed: EstimateModels) -> bool:
        type(self).checks += 1
        if type(self).checks <= type(self).passes:
            return False
        msg = "settings unavailable"
        raise RuntimeError(msg)


class _CountingStore(InMemoryKnowledgeStore):
    sent_back = 0

    async def return_to_confirmation(self, **kwargs: Any) -> bool:
        type(self).sent_back += 1
        return await super().return_to_confirmation(**kwargs)


@pytest.mark.parametrize(
    "doc",
    [MIXED_DOCS[0], PICTURE_DOCS[0]],
    ids=["scanned-page-recognition", "picture-description"],
)
@pytest.mark.usefixtures("no_semantic_breaks")
async def test_a_models_check_that_fails_pays_for_nothing(doc: FormDoc) -> None:
    """The check as recognition starts passes; the next fails. Before a
    figure call that is the call's checkpoint, which would log the error and
    let the call go; before a page batch it would fail the document. Either
    way: no paid call, and the document is left for a retry."""
    _CheckFails.checks, _CheckFails.passes = 0, 1
    store = InMemoryKnowledgeStore()
    paid = _Paid()
    settings = _Recognition()
    recognizer = DocumentRecognizer(
        store=store, settings=settings, api_keys=_api_key, client_factory=paid.reader
    )
    figures = FigureDescriber(
        store=store,
        settings=settings,
        api_keys=_api_key,
        recognizer=recognizer,
        client_factory=paid.describer,
    )
    embedder = _Keyed("acme/embed-1")
    runner = KnowledgeIngestionRunner(
        store=store,
        embedder=embedder,
        recognizer=recognizer,
        figures=figures,
        estimator=_CheckFails(store=store, settings=settings, recognizer=recognizer),
    )
    waiting = await _waiting(store, runner, doc.filename, doc.raw)

    await runner.submit(tenant_id=_TENANT, document_id=waiting.id)

    assert (paid.reads, paid.descriptions, embedder.calls) == (0, 0, 0)
    after = await store.get_document(tenant_id=_TENANT, document_id=waiting.id)
    assert after is not None and after.status is DocumentStatus.PROCESSING  # left for a retry


@pytest.mark.parametrize("path", ["runner", "recovery"])
@pytest.mark.usefixtures("no_semantic_breaks")
async def test_a_models_check_that_keeps_failing_is_retried_then_fails_as_itself(
    path: str,
) -> None:
    """B2b third review: each attempt keeps the parse snapshot; the last one
    fails ``models_check_failed``, not ``attempts_exhausted``."""
    _CheckFails.checks, _CheckFails.passes = 0, 0
    store = InMemoryKnowledgeStore()
    settings = _Recognition()
    paid = _Paid()
    recognizer = DocumentRecognizer(
        store=store, settings=settings, api_keys=_api_key, client_factory=paid.reader
    )
    figures = FigureDescriber(
        store=store,
        settings=settings,
        api_keys=_api_key,
        recognizer=recognizer,
        client_factory=paid.describer,
    )
    embedder = _Keyed("acme/embed-1")
    estimator = _CheckFails(store=store, settings=settings, recognizer=recognizer)
    runner = KnowledgeIngestionRunner(
        store=store,
        embedder=embedder,
        recognizer=recognizer,
        figures=figures,
        estimator=estimator,
        max_attempts=2,
    )
    worker = KnowledgeIngestRecoveryWorker(
        store=store,
        embedder=embedder,
        interval_s=30,
        concurrency=1,
        lease_seconds=300,
        max_attempts=2,
        recognizer=recognizer,
        figures=figures,
        estimator=estimator,
    )
    doc = PICTURE_DOCS[0]
    waiting = await _waiting(store, runner, doc.filename, doc.raw)

    async def attempt() -> KnowledgeDocument:
        if path == "runner":
            await runner.submit(tenant_id=_TENANT, document_id=waiting.id)
        else:
            await worker.run_once()
        store._lease[waiting.id] = None  # the lease runs out
        after = await store.get_document(tenant_id=_TENANT, document_id=waiting.id)
        assert after is not None
        return after

    first = await attempt()
    assert first.status is DocumentStatus.PROCESSING
    assert await _snapshot_of(store, waiting) is not None
    last = await attempt()
    assert (last.status, last.error_code) == (
        DocumentStatus.FAILED,
        IngestErrorCode.MODELS_CHECK_FAILED,
    )
    assert (paid.reads, paid.descriptions, embedder.calls) == (0, 0, 0)


async def test_the_models_guard_refuses_when_its_check_fails() -> None:
    store = _CountingStore()
    _CountingStore.sent_back = 0
    _CheckFails.checks, _CheckFails.passes = 0, 0
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    document = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="a.md", content=_TEXT.encode()
    )
    estimator = _CheckFails(store=store)
    local = LocalParse(kind="text", figures_on=False, markdown=_TEXT)
    confirmed = await estimator.estimate(
        _claim_of(document), local, embedding_model="acme/embed-1", now=datetime.now(UTC)
    )
    guard = _ModelsGuard(store, estimator, _claim_of(document), local, confirmed)

    outcomes = await asyncio.gather(*(guard() for _ in range(3)), return_exceptions=True)

    assert all(isinstance(outcome, StaleIngestionError) for outcome in outcomes)
    assert (_CheckFails.checks, _CountingStore.sent_back) == (1, 0)


@dataclass
class _Configured:
    """Recognition settings a test switches."""

    primary: ModelChoice | None = field(default_factory=lambda: ModelChoice("glm", "glm-ocr"))
    fallback: ModelChoice | None = None

    async def recognition_settings(self) -> RecognitionSettings:
        return RecognitionSettings(primary=self.primary, fallback=self.fallback, max_pages=1000)


@dataclass
class _Keys:
    """API keys; ``failing`` providers' lookups fail (a passing outage)."""

    failing: set[str]

    async def __call__(self, *, tenant_id: UUID, provider: str) -> str:
        if provider in self.failing:
            msg = f"no key for {provider}"
            raise RuntimeError(msg)
        return f"key-{provider}"


async def test_a_key_that_cannot_be_read_now_is_not_a_model_switch() -> None:
    store = InMemoryKnowledgeStore()
    settings, keys = _Configured(), _Keys(failing=set())
    recognizer = DocumentRecognizer(
        store=store, settings=settings, api_keys=keys, client_factory=_Paid().reader
    )
    estimator = IngestEstimator(store=store, settings=settings, recognizer=recognizer)
    claim = ClaimedIngestion(
        tenant_id=_TENANT,
        document_id=uuid4(),
        kb_id=uuid4(),
        filename="a.pdf",
        content=b"x",
        content_sha256="sha",
        chunk_max_tokens=400,
        chunk_overlap_tokens=0,
        attempts=1,
        generation=1,
    )
    confirmed = EstimateModels(embedding="acme/embed-1", recognition="glm/glm-ocr")

    assert not await estimator.models_changed(claim, confirmed)
    keys.failing = {"glm"}  # the confirmed model's key cannot be read just now
    assert not await estimator.models_changed(claim, confirmed)
    settings.fallback = ModelChoice("acme", "ocr-2")  # not counted, unusable: never called
    keys.failing = {"glm", "acme"}
    assert not await estimator.models_changed(claim, confirmed)
    keys.failing = {"glm"}  # ... now usable: it would be called
    assert await estimator.models_changed(claim, confirmed)
    settings.fallback, keys.failing = None, set()
    settings.primary = ModelChoice("acme", "ocr-2")  # switched
    assert await estimator.models_changed(claim, confirmed)
