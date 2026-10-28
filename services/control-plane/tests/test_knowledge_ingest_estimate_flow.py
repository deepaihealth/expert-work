"""B-222 PR-2 —— a claimed document is estimated for free, then paid for once confirmed.

* The estimate phase (``estimating``) parses and counts locally, calls no
  model, bills nothing, keeps the parse as a snapshot and leaves the document
  awaiting its confirmation; what the ingest would refuse fails it here, free.
* The paid phase of a confirmed estimate starts from the snapshot (no second
  parse) and estimates again first: a changed model, or a total risen more than
  20 % and by at least ¥0.01, sends the document back for a new confirmation —
  nothing spent; otherwise it ingests and keeps what it was billed next to the
  estimate (``ingest_actual``), and the snapshot goes.
* A document queued without an estimate ingests as it always did, its spend
  kept too. The recovery worker drives both phases the same way.

Models are fakes (no network): the embedder reports usage as a vendor does.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge import ingestion
from control_plane.knowledge.estimate import IngestEstimator
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner, shutdown_parse_pool
from control_plane.knowledge.parsed_size import configure_parsed_size
from control_plane.knowledge.pricing import Rate
from control_plane.knowledge.recovery import KnowledgeIngestRecoveryWorker
from control_plane.knowledge.usage import IngestUsageRecorder
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.token_usage_store import InMemoryTokenUsageStore
from expert_work.protocol import DocumentStatus, IngestErrorCode, KnowledgeDocument
from expert_work.protocol.knowledge_estimate import CONFIRM_WINDOW
from orchestrator.llm import FakeEmbedder
from orchestrator.llm.embedder import _report_usage

_TENANT = uuid4()
_TEXT = "\n\n".join(
    f"Paragraph {i}: the deductible of plan {i} is {i}00 yuan. " * 4 for i in range(5)
)


@pytest.fixture(scope="module", autouse=True)
def _parse_pool_teardown() -> Iterator[None]:
    yield
    shutdown_parse_pool()


class _Billing(FakeEmbedder):
    """A pinned model that reports ``prompt_tokens`` = characters, as a vendor
    reports usage; counts its calls."""

    def __init__(self, model_key: str = "acme/embed-1") -> None:
        super().__init__()
        self.model_key = model_key
        self.calls = 0

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.calls += 1
        _report_usage(
            self.model_key.split("/")[1], {"usage": {"prompt_tokens": sum(map(len, texts))}}
        )
        return await super().embed(texts, tenant_id=tenant_id)


class _NoModel(_Billing):
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        raise AssertionError("an estimate called a model")


@dataclass
class _Prices:
    per_mtok: int = 50_000_000  # ¥50 / million tokens: a few cents a document

    async def rate(self, *, provider: str, model: str) -> Rate | None:
        return Rate(self.per_mtok, 0) if (provider, model) == ("acme", "embed-1") else None


@dataclass
class _World:
    store: InMemoryKnowledgeStore
    usage: InMemoryTokenUsageStore
    prices: _Prices
    kb: UUID

    def runner(self, embedder: _Billing) -> KnowledgeIngestionRunner:
        return KnowledgeIngestionRunner(
            store=self.store,
            embedder=embedder,
            usage=IngestUsageRecorder(self.usage),
            estimator=IngestEstimator(store=self.store, prices=self.prices),
        )

    def recovery(self, embedder: _Billing) -> KnowledgeIngestRecoveryWorker:
        return KnowledgeIngestRecoveryWorker(
            store=self.store,
            embedder=embedder,
            interval_s=30,
            concurrency=2,
            lease_seconds=300,
            max_attempts=5,
            usage=IngestUsageRecorder(self.usage),
            estimator=IngestEstimator(store=self.store, prices=self.prices),
        )

    async def get(self, document_id: UUID) -> KnowledgeDocument:
        document = await self.store.get_document(tenant_id=_TENANT, document_id=document_id)
        assert document is not None
        return document

    async def usage_rows(self) -> int:
        return len(await self.usage.list_for_tenant(tenant_id=_TENANT, limit=1000))


async def _world() -> _World:
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    return _World(store, InMemoryTokenUsageStore(), _Prices(), base.id)


async def _upload(world: _World, name: str = "plans.md", text: str = _TEXT) -> UUID:
    started = await world.store.begin_upload(
        tenant_id=_TENANT,
        kb_id=world.kb,
        filename=name,
        content=text.encode(),
        content_sha256=f"sha-{name}-{len(text)}",
        now=datetime.now(UTC),
    )
    assert started.outcome == "estimating"
    return started.document.id


async def _confirm(world: _World, document_id: UUID) -> None:
    waiting = await world.get(document_id)
    assert waiting.ingest_estimate is not None
    outcome = await world.store.confirm_documents(
        tenant_id=_TENANT,
        kb_id=world.kb,
        estimates=[(document_id, waiting.ingest_estimate.id)],
        confirmed_by="user",
        now=datetime.now(UTC),
    )
    assert outcome == {document_id: "confirmed"}


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_the_estimate_phase_bills_nothing_and_awaits_confirmation() -> None:
    world = await _world()
    document_id = await _upload(world)
    await world.runner(_NoModel()).submit(tenant_id=_TENANT, document_id=document_id)

    waiting = await world.get(document_id)
    assert waiting.status is DocumentStatus.AWAITING_CONFIRMATION
    assert waiting.confirm_by is not None
    assert waiting.confirm_by - datetime.now(UTC) <= CONFIRM_WINDOW
    estimate = waiting.ingest_estimate
    assert estimate is not None and estimate.total_micros > 0
    assert await world.usage_rows() == 0
    snapshot = await world.store.get_parse_snapshot(
        tenant_id=_TENANT, document_id=document_id, estimate_id=estimate.id
    )
    assert snapshot is not None
    assert waiting.chunk_count == 0  # nothing ingested yet


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_what_the_ingest_would_refuse_fails_the_estimate_for_free() -> None:
    world = await _world()
    document_id = await _upload(world)
    configure_parsed_size(max_chars=50, max_chunks=60_000)
    try:
        await world.runner(_NoModel()).submit(tenant_id=_TENANT, document_id=document_id)
    finally:
        configure_parsed_size(max_chars=2_500_000, max_chunks=60_000)
    failed = await world.get(document_id)
    assert failed.status is DocumentStatus.FAILED
    assert failed.error_code is IngestErrorCode.TEXT_TOO_LARGE
    assert await world.usage_rows() == 0


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_a_confirmed_ingest_starts_from_the_snapshot_and_keeps_what_it_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = await _world()
    document_id = await _upload(world)
    await world.runner(_NoModel()).submit(tenant_id=_TENANT, document_id=document_id)
    await _confirm(world, document_id)
    estimate_id = (await world.get(document_id)).ingest_estimate.id  # type: ignore[union-attr]

    async def no_parse(*args: object, **kwargs: object) -> object:
        raise AssertionError("parsed again")

    monkeypatch.setattr(ingestion, "parse_locally", no_parse)
    embedder = _Billing()
    await world.runner(embedder).submit(tenant_id=_TENANT, document_id=document_id)

    ready = await world.get(document_id)
    assert ready.status is DocumentStatus.READY
    assert embedder.calls > 0
    actual = ready.ingest_actual
    assert actual is not None
    assert actual.estimate_id == estimate_id
    assert actual.outcome == "ready"
    rows = await world.usage.list_for_tenant(tenant_id=_TENANT, limit=1000)
    billed = sum(row.input_tokens for row in rows)
    assert [(i.model, i.input_tokens) for i in actual.items] == [("embed-1", billed)]
    assert actual.total_micros == billed * world.prices.per_mtok // 1_000_000
    # Settled: its snapshot is gone.
    assert (
        await world.store.get_parse_snapshot(
            tenant_id=_TENANT, document_id=document_id, estimate_id=estimate_id
        )
        is None
    )


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_another_model_since_the_estimate_asks_again_and_spends_nothing() -> None:
    world = await _world()
    document_id = await _upload(world)
    await world.runner(_NoModel()).submit(tenant_id=_TENANT, document_id=document_id)
    first = (await world.get(document_id)).ingest_estimate
    await _confirm(world, document_id)

    switched = _Billing("acme/embed-2")
    await world.runner(switched).submit(tenant_id=_TENANT, document_id=document_id)

    back = await world.get(document_id)
    assert back.status is DocumentStatus.AWAITING_CONFIRMATION
    assert back.ingest_estimate is not None and first is not None
    assert back.ingest_estimate.id != first.id
    assert back.ingest_estimate.models.embedding == "acme/embed-2"
    assert back.ingest_estimate.confirmed_at is None
    assert switched.calls == 0
    assert await world.usage_rows() == 0
    assert back.ingest_actual is None


@pytest.mark.parametrize(
    ("rise_percent", "asks_again"),
    [(25, True), (15, False)],
)
@pytest.mark.usefixtures("no_semantic_breaks")
async def test_a_price_rise_asks_again_only_past_the_threshold(
    rise_percent: int, asks_again: bool
) -> None:
    world = await _world()
    long_text = _TEXT * 20  # several cents at ¥50 / million tokens
    document_id = await _upload(world, text=long_text)
    await world.runner(_NoModel()).submit(tenant_id=_TENANT, document_id=document_id)
    first = (await world.get(document_id)).ingest_estimate
    assert first is not None and first.total_micros >= 50_000  # ¥0.05: 20 % is over ¥0.01
    await _confirm(world, document_id)

    world.prices.per_mtok = world.prices.per_mtok * (100 + rise_percent) // 100
    await world.runner(_Billing()).submit(tenant_id=_TENANT, document_id=document_id)

    after = await world.get(document_id)
    expected = DocumentStatus.AWAITING_CONFIRMATION if asks_again else DocumentStatus.READY
    assert after.status is expected


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_a_small_rise_under_a_cent_does_not_ask_again() -> None:
    world = await _world()
    document_id = await _upload(world, text="One short line about the deductible.")
    await world.runner(_NoModel()).submit(tenant_id=_TENANT, document_id=document_id)
    first = (await world.get(document_id)).ingest_estimate
    assert first is not None and first.total_micros < 10_000
    await _confirm(world, document_id)
    world.prices.per_mtok = world.prices.per_mtok * 3  # +200 %, still under ¥0.01
    await world.runner(_Billing()).submit(tenant_id=_TENANT, document_id=document_id)
    assert (await world.get(document_id)).status is DocumentStatus.READY


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_a_document_queued_without_an_estimate_ingests_as_ever_and_keeps_its_cost() -> None:
    world = await _world()
    document = await world.store.upsert_document(
        tenant_id=_TENANT, kb_id=world.kb, filename="plain.md", content=_TEXT.encode()
    )
    embedder = _Billing()
    await world.runner(embedder).submit(tenant_id=_TENANT, document_id=document.id)
    ready = await world.get(document.id)
    assert ready.status is DocumentStatus.READY
    assert ready.ingest_estimate is None
    assert ready.ingest_actual is not None
    assert ready.ingest_actual.estimate_id is None
    assert ready.ingest_actual.total_micros > 0


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_an_estimate_withdrawn_meanwhile_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = await _world()
    document_id = await _upload(world)
    runner = world.runner(_NoModel())
    original = runner.estimator.estimate

    async def withdrawn_meanwhile(*args: Any, **kwargs: Any) -> Any:
        estimate = await original(*args, **kwargs)
        await world.store.cancel_documents(
            tenant_id=_TENANT, kb_id=world.kb, document_ids=[document_id]
        )
        return estimate

    monkeypatch.setattr(runner.estimator, "estimate", withdrawn_meanwhile)
    await runner.submit(tenant_id=_TENANT, document_id=document_id)
    # A new upload withdrawn: gone, and the finished estimate found nothing to land on.
    assert await world.store.get_document(tenant_id=_TENANT, document_id=document_id) is None


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_the_recovery_worker_drives_both_phases() -> None:
    world = await _world()
    document_id = await _upload(world)
    worker = world.recovery(_NoModel())
    assert await worker.run_once() == 1
    waiting = await world.get(document_id)
    assert waiting.status is DocumentStatus.AWAITING_CONFIRMATION
    assert await worker.run_once() == 0  # never claims a document awaiting confirmation

    await _confirm(world, document_id)
    paid = world.recovery(_Billing())
    assert await paid.run_once() == 1
    ready = await world.get(document_id)
    assert ready.status is DocumentStatus.READY
    assert ready.ingest_actual is not None
    assert ready.ingest_actual.estimate_id == waiting.ingest_estimate.id  # type: ignore[union-attr]


async def test_the_lifespan_prices_both_workers_with_the_rate_card() -> None:
    from control_plane import app as app_module
    from control_plane.knowledge.pricing import RateCardPrices
    from control_plane.settings import Settings
    from tests.auth_fixtures import build_test_jwt_verifier

    app = app_module.create_app(
        settings=Settings(checkpointer_backend="memory", enable_knowledge_recovery_worker=True),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
        enable_scheduler=False,
    )
    async with app.router.lifespan_context(app):
        runner = app.state.knowledge_ingestion_runner
        worker = app.state.knowledge_recovery_worker
        assert worker.estimator is runner.estimator
        assert isinstance(runner.estimator.prices, RateCardPrices)


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_an_estimate_lost_with_its_worker_is_picked_up_again() -> None:
    world = await _world()
    document_id = await _upload(world)
    claim = await world.store.claim_document(
        tenant_id=_TENANT,
        document_id=document_id,
        now=datetime.now(UTC) - timedelta(seconds=600),
        lease_seconds=300,
        max_attempts=5,
    )
    assert claim is not None and claim.phase == "estimate"  # its worker died
    assert await world.recovery(_NoModel()).run_once() == 1
    assert (await world.get(document_id)).status is DocumentStatus.AWAITING_CONFIRMATION
