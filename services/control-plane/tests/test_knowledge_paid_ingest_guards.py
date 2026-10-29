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
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge import parse_snapshot
from control_plane.knowledge.estimate import IngestEstimator
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner, shutdown_parse_pool
from control_plane.knowledge.recognition.ingest_parse import LocalParse
from control_plane.knowledge.recovery import KnowledgeIngestRecoveryWorker
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.knowledge.base import ClaimedIngestion, ParseSnapshot
from expert_work.protocol import DocumentStatus, KnowledgeDocument
from expert_work.protocol.knowledge_estimate import EstimateModels
from orchestrator.llm import FakeEmbedder
from tests.knowledge_form_fixtures import MIXED_DOCS

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

    async def models_changed(self, claim: ClaimedIngestion, confirmed: EstimateModels) -> bool:
        return True


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
