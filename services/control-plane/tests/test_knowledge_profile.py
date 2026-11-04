"""B-267 step 2 —— the knowledge-base profile and the per-base probe calibration.

* The estimate the user confirms lists the profile call and the calibration
  queries while the platform switch is on — and nothing about them while it is
  off (byte-identical estimate).
* A confirmed ingest whose estimate counted the profile asks for the base's
  refresh when its document is ready; one confirmed without it does not.
* A refresh makes ONE call of the auxiliary model over the documents' digest,
  validated and cut to the limits, billed to the tenant as ``knowledge_ingest``;
  a failure keeps the old profile; an empty base calls nothing.
* The calibration is the 90th percentile of the top scores generic queries
  reach in the base, only under the live embedding model.

Models are fakes (no network).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge.estimate import IngestEstimator
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner, shutdown_parse_pool
from control_plane.knowledge.pricing import Rate
from control_plane.knowledge.profile import (
    CALIBRATION_QUERIES,
    PROFILE_INSTRUCTIONS,
    AuxModelChoice,
    ProfileRefresher,
    calibration_tokens,
    percentile,
    profile_prompt,
    profile_token_bounds,
    render_digest,
    schedule_after_ingest,
)
from control_plane.knowledge.recovery import KnowledgeIngestRecoveryWorker
from control_plane.knowledge.usage import IngestUsageRecorder
from control_plane.memory_consolidator import ConsolidatorLLMReply
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge.base import ClaimedIngestion
from expert_work.persistence.knowledge.profiles import (
    PROFILE_DEBOUNCE,
    DocumentDigest,
    ProfileDigest,
)
from expert_work.persistence.token_usage_store import (
    KNOWLEDGE_INGEST_USAGE_KIND,
    InMemoryTokenUsageStore,
)
from expert_work.protocol import DocumentStatus, KnowledgeBase, KnowledgeChunk
from expert_work.protocol.knowledge_estimate import (
    EstimateBasis,
    EstimateKind,
    EstimateNote,
    EstimateUnit,
    IngestEstimate,
)
from expert_work.protocol.knowledge_profile import (
    PROFILE_SUMMARY_MAX_CHARS,
    PROFILE_TOPICS_MAX,
    KnowledgeProfile,
)
from orchestrator.llm import FakeEmbedder
from orchestrator.llm.embedder import _report_usage

_TENANT = uuid4()
_TEXT = "\n\n".join(f"## Part {i}\n\nThe deductible of plan {i} is {i}00 yuan." for i in range(4))
_AUX = AuxModelChoice(provider="acme", model="chat-1")
_NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
_ANSWER = {
    "summary": "保险条款与免赔额说明",
    "topics": ["免赔额", "保险条款"],
    "typical_questions": ["计划 2 的免赔额是多少?"],
    "doc_types": ["条款"],
}


@pytest.fixture(scope="module", autouse=True)
def _parse_pool_teardown() -> Iterator[None]:
    yield
    shutdown_parse_pool()


@dataclass
class _Aux:
    """The auxiliary model: records prompts, answers ``answer``."""

    answer: dict[str, Any] | None = field(default_factory=lambda: dict(_ANSWER))
    text: str = ""
    prompts: list[str] = field(default_factory=list)

    async def __call__(
        self, *, prompt: str, model: str | None, tenant_id: UUID, output_schema: Any = None
    ) -> ConsolidatorLLMReply:
        self.prompts.append(prompt)
        return ConsolidatorLLMReply(
            text=self.text,
            model=model or "",
            input_tokens=1200,
            output_tokens=90,
            parsed=self.answer,
        )


class _Billing(FakeEmbedder):
    """Reports usage like a vendor; ``scores`` maps a query to the first
    coordinate of its vector (so its cosine with an axis-aligned chunk)."""

    def __init__(self) -> None:
        super().__init__()
        self.queries: list[str] = []

    async def embed_query(self, query: str, *, tenant_id: UUID) -> tuple[float, ...]:
        self.queries.append(query)
        _report_usage("embed-1", {"usage": {"prompt_tokens": len(query)}})
        return _unit(len(self.queries))


def _unit(index: int) -> tuple[float, ...]:
    """A vector whose cosine with the chunk axis (index 0) is ``index``/20."""
    cos = min(index, 20) / 20
    return (cos, (1 - cos * cos) ** 0.5) + (0.0,) * (EMBEDDING_DIM - 2)


async def _embedding_config() -> tuple[str, str] | None:
    return ("acme", "embed-1")


async def _base_with_document(store: InMemoryKnowledgeStore) -> KnowledgeBase:
    base = await store.create_base(
        tenant_id=_TENANT, name="kb", embedding_provider="acme", embedding_model="embed-1"
    )
    document = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="条款.md", content=b"x"
    )
    chunk = KnowledgeChunk(
        id=uuid4(),
        tenant_id=_TENANT,
        kb_id=base.id,
        document_id=document.id,
        chunk_index=0,
        content="免赔额说明",
        heading_path=("第一章",),
        embedding=(1.0,) + (0.0,) * (EMBEDDING_DIM - 1),
    )
    await store.replace_chunks(tenant_id=_TENANT, document_id=document.id, chunks=[chunk])
    await store.set_document_status(
        tenant_id=_TENANT, document_id=document.id, status=DocumentStatus.READY, chunk_count=1
    )
    return base


def _refresher(
    store: InMemoryKnowledgeStore,
    aux: _Aux,
    usage: InMemoryTokenUsageStore | None = None,
    embedder: FakeEmbedder | None = None,
) -> ProfileRefresher:
    return ProfileRefresher(
        store=store,
        aux=aux,
        aux_model=_AUX,
        embedder=embedder or _Billing(),
        embedding_config=_embedding_config,
        usage=IngestUsageRecorder(usage) if usage is not None else None,
        now=lambda: _NOW,
    )


# -- the estimate -------------------------------------------------------------------


@dataclass
class _Prices:
    async def rate(self, *, provider: str, model: str) -> Rate | None:
        return {
            ("acme", "embed-1"): Rate(500_000, 0),
            ("acme", "chat-1"): Rate(2_000_000, 8_000_000),
        }.get((provider, model))


async def _estimate(profile_model: str | None) -> IngestEstimate:
    from control_plane.knowledge.recognition.ingest_parse import LocalParse

    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    document = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="a.md", content=_TEXT.encode()
    )
    claim = ClaimedIngestion(
        tenant_id=_TENANT,
        document_id=document.id,
        kb_id=base.id,
        filename="a.md",
        content=_TEXT.encode(),
        content_sha256="sha",
        chunk_max_tokens=512,
        chunk_overlap_tokens=0,
        attempts=1,
        generation=1,
        phase="estimate",
        origin="upload",
    )
    estimator = IngestEstimator(store=store, prices=_Prices(), profile_model=profile_model)
    return await estimator.estimate(
        claim,
        LocalParse(kind="text", figures_on=False, markdown=_TEXT),
        embedding_model="acme/embed-1",
        now=_NOW,
    )


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_switch_off_estimates_no_profile() -> None:
    estimate = await _estimate(None)
    assert {item.kind for item in estimate.items} == {EstimateKind.EMBEDDING}
    assert EstimateNote.PROFILE_ONCE_PER_BASE.value not in estimate.notes


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_switch_on_estimates_the_profile_call_and_the_calibration() -> None:
    off = await _estimate(None)
    on = await _estimate("acme/chat-1")
    profile = next(item for item in on.items if item.kind is EstimateKind.PROFILE)
    calibration = next(item for item in on.items if item.kind is EstimateKind.PROFILE_CALIBRATION)
    input_bound, output_bound = profile_token_bounds()
    assert (profile.units, profile.unit, profile.model) == (1, EstimateUnit.BASES, "acme/chat-1")
    assert (profile.input_tokens, profile.output_tokens) == (input_bound, output_bound)
    assert profile.micros == (input_bound * 2_000_000 + output_bound * 8_000_000) // 1_000_000
    assert profile.basis is EstimateBasis.UPPER_BOUND
    assert calibration.units == len(CALIBRATION_QUERIES)
    assert calibration.unit is EstimateUnit.QUERIES
    assert calibration.model == "acme/embed-1"
    assert calibration.input_tokens == calibration_tokens()
    assert on.total_micros == off.total_micros + profile.micros + calibration.micros
    assert EstimateNote.PROFILE_ONCE_PER_BASE.value in on.notes


# -- scheduling ---------------------------------------------------------------------


def _claim_with(estimate: IngestEstimate | None, kb_id: UUID, phase: str = "ingest") -> Any:
    return ClaimedIngestion(
        tenant_id=_TENANT,
        document_id=uuid4(),
        kb_id=kb_id,
        filename="a.md",
        content=b"x",
        content_sha256="sha",
        chunk_max_tokens=512,
        chunk_overlap_tokens=0,
        attempts=1,
        generation=1,
        phase=phase,  # type: ignore[arg-type]
        estimate=estimate,
    )


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_only_an_ingest_confirmed_with_the_profile_line_asks_for_a_refresh() -> None:
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    with_line, without = await _estimate("acme/chat-1"), await _estimate(None)
    for claim in (
        _claim_with(without, base.id),
        _claim_with(None, base.id),
        _claim_with(with_line, base.id, phase="estimate"),
    ):
        await schedule_after_ingest(store, claim)
        assert (await store.get_base(tenant_id=_TENANT, name="kb")).profile is None  # type: ignore[union-attr]
    await schedule_after_ingest(store, _claim_with(with_line, base.id))
    record = (await store.get_base(tenant_id=_TENANT, name="kb")).profile  # type: ignore[union-attr]
    assert record is not None and record.refresh is not None


async def _flow(profile_model: str | None, *, recovery: bool) -> KnowledgeBase:
    """Upload → estimate → confirm → ingest, by the runner or the recovery worker."""
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    started = await store.begin_upload(
        tenant_id=_TENANT,
        kb_id=base.id,
        filename="plans.md",
        content=_TEXT.encode(),
        content_sha256="sha-plans",
        now=datetime.now(UTC),
    )
    document_id = started.document.id
    estimator = IngestEstimator(store=store, prices=_Prices(), profile_model=profile_model)
    runner = KnowledgeIngestionRunner(store=store, embedder=_Billing(), estimator=estimator)
    await runner.submit(tenant_id=_TENANT, document_id=document_id)
    waiting = await store.get_document(tenant_id=_TENANT, document_id=document_id)
    assert waiting is not None and waiting.ingest_estimate is not None
    await store.confirm_documents(
        tenant_id=_TENANT,
        kb_id=base.id,
        estimates=[(document_id, waiting.ingest_estimate.id)],
        confirmed_by="user",
        now=datetime.now(UTC),
    )
    if recovery:
        worker = KnowledgeIngestRecoveryWorker(
            store=store,
            embedder=_Billing(),
            interval_s=30,
            concurrency=1,
            lease_seconds=300,
            max_attempts=5,
            estimator=estimator,
        )
        await worker.run_once()
    else:
        await runner.submit(tenant_id=_TENANT, document_id=document_id)
    ready = await store.get_document(tenant_id=_TENANT, document_id=document_id)
    assert ready is not None and ready.status is DocumentStatus.READY
    found = await store.get_base(tenant_id=_TENANT, name="kb")
    assert found is not None
    return found


@pytest.mark.usefixtures("no_semantic_breaks")
@pytest.mark.parametrize("recovery", [False, True], ids=["runner", "recovery"])
async def test_a_confirmed_ingest_asks_for_the_refresh_when_ready(recovery: bool) -> None:
    base = await _flow("acme/chat-1", recovery=recovery)
    assert base.profile is not None and base.profile.refresh is not None


@pytest.mark.usefixtures("no_semantic_breaks")
@pytest.mark.parametrize("recovery", [False, True], ids=["runner", "recovery"])
async def test_switch_off_ingest_asks_for_nothing(recovery: bool) -> None:
    base = await _flow(None, recovery=recovery)
    assert base.profile is None


# -- generation ---------------------------------------------------------------------


async def test_one_call_over_the_digest_validated_and_billed() -> None:
    store = InMemoryKnowledgeStore()
    usage = InMemoryTokenUsageStore()
    base = await _base_with_document(store)
    aux = _Aux()
    profile = await _refresher(store, aux, usage).generate(base)
    assert profile == KnowledgeProfile(
        **_ANSWER,
        generated_at=_NOW,
        source_doc_count=1,
        model="acme/chat-1",
    )
    [prompt] = aux.prompts
    assert prompt.startswith(PROFILE_INSTRUCTIONS)
    assert "- Title: 条款\n  Headings: 第一章\n  Opening: 免赔额说明" in prompt
    [row] = await usage.list_for_tenant(tenant_id=_TENANT, limit=10)
    assert (row.provider, row.model, row.input_tokens, row.output_tokens) == (
        "acme",
        "chat-1",
        1200,
        90,
    )
    assert row.usage_kind == KNOWLEDGE_INGEST_USAGE_KIND


async def test_a_long_answer_is_cut_to_the_limits() -> None:
    store = InMemoryKnowledgeStore()
    base = await _base_with_document(store)
    answer = {
        "summary": "长" * 500,
        "topics": [f"主题{i}" for i in range(30)],
        "typical_questions": ["q"] * 3,
        "doc_types": [],
    }
    profile = await _refresher(store, _Aux(answer=answer)).generate(base)
    assert profile is not None
    assert len(profile.summary) == PROFILE_SUMMARY_MAX_CHARS
    assert len(profile.topics) == PROFILE_TOPICS_MAX
    assert profile.typical_questions == ("q",)


async def test_the_answer_is_read_from_text_when_not_parsed() -> None:
    store = InMemoryKnowledgeStore()
    base = await _base_with_document(store)
    aux = _Aux(answer=None, text=json.dumps(_ANSWER, ensure_ascii=False))
    profile = await _refresher(store, aux).generate(base)
    assert profile is not None and profile.summary == _ANSWER["summary"]


async def test_an_empty_base_calls_nothing() -> None:
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    aux = _Aux()
    assert await _refresher(store, aux).generate(base) is None
    assert aux.prompts == []


def test_the_digest_is_cut_to_its_token_budget() -> None:
    documents = tuple(
        DocumentDigest(title=f"doc{i}", headings=("h",), opening="x" * 50) for i in range(50)
    )
    digest = ProfileDigest(documents=documents, ready_documents=50)
    whole = render_digest(digest)
    cut = render_digest(digest, budget=100)
    assert whole.count("- Title:") == 50
    assert 0 < cut.count("- Title:") < 50
    assert profile_prompt(digest).endswith(f"<documents>\n{whole}\n</documents>")


# -- refresh / failure ----------------------------------------------------------------


async def _due(store: InMemoryKnowledgeStore, kb_id: UUID) -> None:
    await store.mark_profile_refresh(tenant_id=_TENANT, kb_id=kb_id, now=_NOW - PROFILE_DEBOUNCE)


async def test_a_due_refresh_stores_profile_and_calibration() -> None:
    store = InMemoryKnowledgeStore()
    base = await _base_with_document(store)
    await _due(store, base.id)
    assert await _refresher(store, _Aux()).run_due() == 1
    record = (await store.get_base(tenant_id=_TENANT, name="kb")).profile  # type: ignore[union-attr]
    assert record is not None
    assert record.profile is not None and record.profile.summary == _ANSWER["summary"]
    assert record.calibration is not None
    assert record.refresh is None


async def test_an_unusable_answer_keeps_the_old_profile() -> None:
    store = InMemoryKnowledgeStore()
    base = await _base_with_document(store)
    await _due(store, base.id)
    await _refresher(store, _Aux()).run_due()
    await _due(store, base.id)
    await _refresher(store, _Aux(answer={"summary": ""})).run_due()
    record = (await store.get_base(tenant_id=_TENANT, name="kb")).profile  # type: ignore[union-attr]
    assert record is not None and record.profile is not None
    assert record.profile.summary == _ANSWER["summary"]
    assert record.refresh is not None and record.refresh.attempts == 1


async def test_a_calibration_only_refresh_calls_no_model() -> None:
    store = InMemoryKnowledgeStore()
    base = await _base_with_document(store)
    await store.mark_profile_refresh(
        tenant_id=_TENANT, kb_id=base.id, now=_NOW - PROFILE_DEBOUNCE, calibrate_only=True
    )
    aux = _Aux()
    await _refresher(store, aux).run_due()
    record = (await store.get_base(tenant_id=_TENANT, name="kb")).profile  # type: ignore[union-attr]
    assert aux.prompts == []
    assert record is not None and record.profile is None and record.calibration is not None


async def test_the_recovery_worker_runs_due_refreshes() -> None:
    store = InMemoryKnowledgeStore()
    base = await _base_with_document(store)
    await _due(store, base.id)
    aux = _Aux()
    worker = KnowledgeIngestRecoveryWorker(
        store=store,
        embedder=FakeEmbedder(),
        interval_s=30,
        concurrency=1,
        lease_seconds=300,
        max_attempts=5,
        profiles=_refresher(store, aux),
    )
    await worker.run_once()
    assert len(aux.prompts) == 1


# -- calibration ----------------------------------------------------------------------


def test_percentile_is_nearest_rank() -> None:
    values = [float(i) for i in range(1, 17)]
    assert percentile(values) == 15.0
    assert percentile([0.3]) == 0.3


async def test_the_calibration_is_the_90th_percentile_of_generic_queries() -> None:
    store = InMemoryKnowledgeStore()
    usage = InMemoryTokenUsageStore()
    base = await _base_with_document(store)
    embedder = _Billing()
    calibration = await _refresher(store, _Aux(), usage, embedder).calibrate(base)
    assert embedder.queries == list(CALIBRATION_QUERIES)
    assert calibration is not None
    scores = [min(i, 20) / 20 for i in range(1, len(CALIBRATION_QUERIES) + 1)]
    assert calibration.vector_threshold == pytest.approx(percentile(scores))
    assert (calibration.embedding_model, calibration.queries) == ("acme/embed-1", 16)
    [row] = await usage.list_for_tenant(tenant_id=_TENANT, limit=10)
    assert row.input_tokens == sum(len(q) for q in CALIBRATION_QUERIES)
    assert row.usage_kind == KNOWLEDGE_INGEST_USAGE_KIND


async def test_no_calibration_when_the_vectors_are_not_the_live_model() -> None:
    store = InMemoryKnowledgeStore()
    base = await _base_with_document(store)
    embedder = _Billing()
    refresher = _refresher(store, _Aux(), embedder=embedder)

    async def other() -> tuple[str, str] | None:
        return ("acme", "embed-2")

    refresher.embedding_config = other
    assert await refresher.calibrate(base) is None
    assert embedder.queries == []


def test_calibration_queries_are_platform_text() -> None:
    """Fixed, generic and many enough for a 90th percentile to mean something."""
    assert len(CALIBRATION_QUERIES) >= 10
    assert len(set(CALIBRATION_QUERIES)) == len(CALIBRATION_QUERIES)


@pytest.mark.usefixtures("no_semantic_breaks")
@pytest.mark.parametrize("switch", [True, False], ids=["on", "off"])
async def test_a_finished_reindex_asks_for_the_calibration_again(switch: bool) -> None:
    store = InMemoryKnowledgeStore()
    base = await _base_with_document(store)
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder(), profile_refresh=switch)
    await runner.submit_reindex(
        tenant_id=_TENANT, kb_id=base.id, embedding_provider="acme", embedding_model="embed-2"
    )
    record = (await store.get_base(tenant_id=_TENANT, name="kb")).profile  # type: ignore[union-attr]
    if switch:
        assert record is not None and record.refresh is not None
        assert record.refresh.calibrate_only is True
    else:
        assert record is None
