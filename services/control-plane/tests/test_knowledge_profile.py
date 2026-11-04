"""B-267 step 2 —— the profile's estimate line and when a refresh is asked for.

* The estimate the user confirms lists the profile call and the calibration
  queries while the platform switch is on — and nothing about them while it is
  off (same estimate, same fingerprint).
* A confirmed ingest whose estimate counted the profile asks for the base's
  refresh when its document is ready, only while the switch is on.
* A finished re-index asks for the calibration only when the stored one is
  for another model; a failure there never fails the re-index.

Running a refresh: ``test_knowledge_profile_refresh.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge.estimate import IngestEstimator
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner, shutdown_parse_pool
from control_plane.knowledge.pricing import Rate
from control_plane.knowledge.profile import (
    CALIBRATION_QUERIES,
    calibration_tokens,
    profile_token_bounds,
    schedule_after_ingest,
    schedule_recalibration,
)
from control_plane.knowledge.recovery import KnowledgeIngestRecoveryWorker
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge.base import ClaimedIngestion
from expert_work.persistence.knowledge.profiles import (
    PROFILE_DEBOUNCE,
)
from expert_work.protocol import DocumentStatus, KnowledgeBase, KnowledgeChunk
from expert_work.protocol.knowledge_estimate import (
    EstimateBasis,
    EstimateKind,
    EstimateModels,
    EstimateNote,
    EstimateUnit,
    IngestEstimate,
)
from expert_work.protocol.knowledge_profile import ProbeCalibration
from orchestrator.llm import FakeEmbedder
from orchestrator.llm.embedder import _report_usage

_TENANT = uuid4()
_TEXT = "\n\n".join(f"## Part {i}\n\nThe deductible of plan {i} is {i}00 yuan." for i in range(4))
_NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module", autouse=True)
def _parse_pool_teardown() -> Iterator[None]:
    yield
    shutdown_parse_pool()


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
        await schedule_after_ingest(store, claim, enabled=True)
        assert (await store.get_base(tenant_id=_TENANT, name="kb")).profile is None  # type: ignore[union-attr]
    # The switch turned off after the confirmation: nothing is asked for.
    await schedule_after_ingest(store, _claim_with(with_line, base.id), enabled=False)
    assert (await store.get_base(tenant_id=_TENANT, name="kb")).profile is None  # type: ignore[union-attr]
    await schedule_after_ingest(store, _claim_with(with_line, base.id), enabled=True)
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
    runner = KnowledgeIngestionRunner(
        store=store,
        embedder=_Billing(),
        estimator=estimator,
        profile_refresh=profile_model is not None,
    )
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
            profile_refresh=profile_model is not None,
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


async def _calibrated(store: InMemoryKnowledgeStore, kb_id: UUID, model_key: str) -> None:
    await store.mark_profile_refresh(tenant_id=_TENANT, kb_id=kb_id, now=_NOW, calibrate_only=True)
    [claim] = await store.claim_profile_refreshes(now=_NOW + PROFILE_DEBOUNCE, limit=1)
    calibration = ProbeCalibration(
        embedding_model=model_key, vector_threshold=0.5, queries=16, computed_at=_NOW
    )
    await store.finish_profile_refresh(
        tenant_id=_TENANT,
        kb_id=kb_id,
        holder=claim.holder,
        marks=claim.marks,
        profile=None,
        calibration=calibration,
        now=_NOW,
    )


async def test_recalibration_is_asked_only_when_the_model_changed() -> None:
    store = InMemoryKnowledgeStore()
    base = await _base_with_document(store)
    await _calibrated(store, base.id, "acme/embed-2")
    await schedule_recalibration(store, tenant_id=_TENANT, kb_id=base.id, model_key="acme/embed-2")
    record = (await store.get_base(tenant_id=_TENANT, name="kb")).profile  # type: ignore[union-attr]
    assert record is not None and record.refresh is None
    await schedule_recalibration(store, tenant_id=_TENANT, kb_id=base.id, model_key="acme/embed-3")
    record = (await store.get_base(tenant_id=_TENANT, name="kb")).profile  # type: ignore[union-attr]
    assert record is not None and record.refresh is not None and record.refresh.calibrate_only


async def test_a_failing_recalibration_request_does_not_fail_the_reindex() -> None:
    class _Broken(InMemoryKnowledgeStore):
        async def mark_profile_refresh(self, **kwargs: Any) -> bool:
            raise RuntimeError("down")

    store = _Broken()
    base = await _base_with_document(store)
    await schedule_recalibration(
        store, tenant_id=_TENANT, kb_id=base.id, model_key="acme/embed-9"
    )  # no exception


@pytest.mark.usefixtures("no_semantic_breaks")
async def test_the_profile_model_joins_the_estimate_fingerprint_only_when_counted() -> None:
    off = await _estimate(None)
    on = await _estimate("acme/chat-1")
    assert on.models.profile == "acme/chat-1"
    assert off.models.profile is None
    assert on.models_fingerprint != off.models_fingerprint
    legacy = EstimateModels(embedding="acme/embed-1")
    assert off.models_fingerprint == legacy.fingerprint()
