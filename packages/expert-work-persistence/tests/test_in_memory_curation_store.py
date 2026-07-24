"""Unit tests for the in-memory curation stores — Stream J.12 (Mini-ADR J-43)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import (
    InMemoryCurationCandidateStore,
    InMemoryEvalDatasetStore,
)
from expert_work.protocol import (
    CandidateStatus,
    CurationCandidateRecord,
    CurationSignal,
    EvalDatasetRecord,
    EvalDatasetSource,
    FeedbackRating,
    TrajectoryOutcome,
)

_BASE = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


def _dataset(
    *,
    dataset_id: UUID | None = None,
    tenant_id: UUID | None = None,
    agent_name: str = "reporter",
    name: str = "nightly-set",
    source: EvalDatasetSource = "golden",
    expected: dict[str, object] | None = None,
) -> EvalDatasetRecord:
    if expected is None and source in ("golden", "regression"):
        expected = {"answer": "ok"}
    return EvalDatasetRecord(
        id=dataset_id or uuid4(),
        tenant_id=tenant_id or uuid4(),
        agent_name=agent_name,
        name=name,
        input={"prompt": "hi"},
        expected=expected,
        source=source,
        created_at=_BASE,
        updated_at=_BASE,
    )


def _candidate(
    *,
    candidate_id: UUID | None = None,
    tenant_id: UUID | None = None,
    agent_name: str = "reporter",
    trajectory_key: str | None = None,
    outcome: TrajectoryOutcome = "failed",
    signal: CurationSignal = "failed_outcome",
    feedback_rating: FeedbackRating | None = None,
    detected_at: datetime = _BASE,
) -> CurationCandidateRecord:
    return CurationCandidateRecord(
        id=candidate_id or uuid4(),
        tenant_id=tenant_id or uuid4(),
        agent_name=agent_name,
        agent_version="1.0.0",
        thread_id=uuid4(),
        user_id=uuid4(),
        trajectory_key=trajectory_key or f"trajectories/{uuid4()}.jsonl",
        outcome=outcome,
        signal=signal,
        feedback_rating=feedback_rating,
        detected_at=detected_at,
    )


# --- InMemoryEvalDatasetStore ---------------------------------------------


@pytest.mark.asyncio
async def test_dataset_create_then_get_round_trips() -> None:
    store = InMemoryEvalDatasetStore()
    did, tenant = uuid4(), uuid4()
    await store.create(_dataset(dataset_id=did, tenant_id=tenant))

    fetched = await store.get(dataset_id=did, tenant_id=tenant)
    assert fetched is not None
    assert fetched.id == did
    assert fetched.source == "golden"


@pytest.mark.asyncio
async def test_dataset_get_unknown_and_cross_tenant_return_none() -> None:
    store = InMemoryEvalDatasetStore()
    did, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.create(_dataset(dataset_id=did, tenant_id=tenant_a))

    assert await store.get(dataset_id=uuid4(), tenant_id=tenant_a) is None
    assert await store.get(dataset_id=did, tenant_id=tenant_b) is None


@pytest.mark.asyncio
async def test_dataset_create_duplicate_id_raises() -> None:
    store = InMemoryEvalDatasetStore()
    did = uuid4()
    await store.create(_dataset(dataset_id=did))
    with pytest.raises(ValueError, match="already exists"):
        await store.create(_dataset(dataset_id=did))


@pytest.mark.asyncio
async def test_dataset_list_by_agent_filters() -> None:
    store = InMemoryEvalDatasetStore()
    tenant = uuid4()
    await store.create(_dataset(tenant_id=tenant, agent_name="reporter", name="a"))
    await store.create(_dataset(tenant_id=tenant, agent_name="reporter", name="b"))
    await store.create(_dataset(tenant_id=tenant, agent_name="auditor", name="a"))

    listed = await store.list_by_agent(tenant_id=tenant, agent_name="reporter")
    assert {r.name for r in listed} == {"a", "b"}


@pytest.mark.asyncio
async def test_dataset_list_by_tenant_is_tenant_scoped() -> None:
    store = InMemoryEvalDatasetStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    await store.create(_dataset(tenant_id=tenant_a, name="a"))
    await store.create(_dataset(tenant_id=tenant_a, name="b"))
    await store.create(_dataset(tenant_id=tenant_b, name="c"))

    listed = await store.list_by_tenant(tenant_id=tenant_a)
    assert {r.name for r in listed} == {"a", "b"}


@pytest.mark.asyncio
async def test_dataset_update_replaces_row() -> None:
    store = InMemoryEvalDatasetStore()
    did, tenant = uuid4(), uuid4()
    await store.create(_dataset(dataset_id=did, tenant_id=tenant))

    rec = await store.get(dataset_id=did, tenant_id=tenant)
    assert rec is not None
    updated = await store.update(rec.model_copy(update={"expected": {"answer": "fixed"}}))
    assert updated is True

    again = await store.get(dataset_id=did, tenant_id=tenant)
    assert again is not None
    assert again.expected == {"answer": "fixed"}


@pytest.mark.asyncio
async def test_dataset_update_cross_tenant_returns_false() -> None:
    store = InMemoryEvalDatasetStore()
    did, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.create(_dataset(dataset_id=did, tenant_id=tenant_a))

    rec = await store.get(dataset_id=did, tenant_id=tenant_a)
    assert rec is not None
    updated = await store.update(rec.model_copy(update={"tenant_id": tenant_b}))
    assert updated is False


@pytest.mark.asyncio
async def test_dataset_delete() -> None:
    store = InMemoryEvalDatasetStore()
    did, tenant = uuid4(), uuid4()
    await store.create(_dataset(dataset_id=did, tenant_id=tenant))

    deleted = await store.delete(dataset_id=did, tenant_id=tenant)
    assert deleted is True
    assert await store.get(dataset_id=did, tenant_id=tenant) is None
    deleted_again = await store.delete(dataset_id=did, tenant_id=tenant)
    assert deleted_again is False


@pytest.mark.asyncio
async def test_dataset_count_by_tenant() -> None:
    store = InMemoryEvalDatasetStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    await store.create(_dataset(tenant_id=tenant_a, name="a"))
    await store.create(_dataset(tenant_id=tenant_a, name="b"))
    await store.create(_dataset(tenant_id=tenant_b, name="c"))

    assert await store.count_by_tenant(tenant_id=tenant_a) == 2
    assert await store.count_by_tenant(tenant_id=tenant_b) == 1
    assert await store.count_by_tenant(tenant_id=uuid4()) == 0


# --- InMemoryCurationCandidateStore ---------------------------------------


@pytest.mark.asyncio
async def test_candidate_upsert_inserts_once() -> None:
    store = InMemoryCurationCandidateStore()
    tenant, key = uuid4(), "trajectories/abc.jsonl"
    inserted = await store.upsert(_candidate(tenant_id=tenant, trajectory_key=key))
    assert inserted is True

    # A second upsert for the same trajectory is a no-op skip.
    again = await store.upsert(_candidate(tenant_id=tenant, trajectory_key=key))
    assert again is False


@pytest.mark.asyncio
async def test_candidate_upsert_distinct_trajectory_keys_each_insert() -> None:
    store = InMemoryCurationCandidateStore()
    tenant = uuid4()
    first = await store.upsert(_candidate(tenant_id=tenant, trajectory_key="t/1.jsonl"))
    second = await store.upsert(_candidate(tenant_id=tenant, trajectory_key="t/2.jsonl"))
    assert first is True
    assert second is True


@pytest.mark.asyncio
async def test_candidate_same_key_different_tenant_both_insert() -> None:
    """Uniqueness is per (tenant, trajectory_key)."""
    store = InMemoryCurationCandidateStore()
    key = "trajectories/shared.jsonl"
    first = await store.upsert(_candidate(tenant_id=uuid4(), trajectory_key=key))
    second = await store.upsert(_candidate(tenant_id=uuid4(), trajectory_key=key))
    assert first is True
    assert second is True


@pytest.mark.asyncio
async def test_candidate_get_unknown_and_cross_tenant_return_none() -> None:
    store = InMemoryCurationCandidateStore()
    cid, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.upsert(_candidate(candidate_id=cid, tenant_id=tenant_a))

    assert await store.get(candidate_id=uuid4(), tenant_id=tenant_a) is None
    assert await store.get(candidate_id=cid, tenant_id=tenant_b) is None


@pytest.mark.asyncio
async def test_candidate_get_by_trajectory_key() -> None:
    store = InMemoryCurationCandidateStore()
    tenant, key = uuid4(), "trajectories/xyz.jsonl"
    await store.upsert(_candidate(tenant_id=tenant, trajectory_key=key))

    found = await store.get_by_trajectory_key(tenant_id=tenant, trajectory_key=key)
    assert found is not None
    assert found.trajectory_key == key
    assert await store.get_by_trajectory_key(tenant_id=tenant, trajectory_key="t/none") is None


@pytest.mark.asyncio
async def test_candidate_list_for_review_filters() -> None:
    store = InMemoryCurationCandidateStore()
    tenant = uuid4()
    await store.upsert(_candidate(tenant_id=tenant, agent_name="reporter", signal="failed_outcome"))
    await store.upsert(
        _candidate(
            tenant_id=tenant,
            agent_name="reporter",
            signal="negative_feedback",
            feedback_rating="down",
        )
    )
    await store.upsert(_candidate(tenant_id=tenant, agent_name="auditor"))

    by_agent = await store.list_for_review(tenant_id=tenant, agent_name="reporter")
    assert len(by_agent) == 2
    by_signal = await store.list_for_review(tenant_id=tenant, signal="negative_feedback")
    assert len(by_signal) == 1
    assert by_signal[0].feedback_rating == "down"


@pytest.mark.asyncio
async def test_candidate_list_for_review_status_filter() -> None:
    store = InMemoryCurationCandidateStore()
    tenant = uuid4()
    pending = _candidate(tenant_id=tenant)
    await store.upsert(pending)

    only_pending = await store.list_for_review(tenant_id=tenant, status=CandidateStatus.PENDING)
    assert len(only_pending) == 1
    promoted = await store.list_for_review(tenant_id=tenant, status=CandidateStatus.PROMOTED)
    assert promoted == []


@pytest.mark.asyncio
async def test_candidate_update_records_promotion() -> None:
    store = InMemoryCurationCandidateStore()
    cid, tenant = uuid4(), uuid4()
    await store.upsert(_candidate(candidate_id=cid, tenant_id=tenant))

    rec = await store.get(candidate_id=cid, tenant_id=tenant)
    assert rec is not None
    promoted = rec.model_copy(
        update={
            "status": CandidateStatus.PROMOTED,
            "eval_dataset_id": uuid4(),
            "reviewed_at": _BASE,
        }
    )
    updated = await store.update(promoted)
    assert updated is True

    again = await store.get(candidate_id=cid, tenant_id=tenant)
    assert again is not None
    assert again.status is CandidateStatus.PROMOTED
    assert again.eval_dataset_id is not None


@pytest.mark.asyncio
async def test_candidate_update_cross_tenant_returns_false() -> None:
    store = InMemoryCurationCandidateStore()
    cid, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await store.upsert(_candidate(candidate_id=cid, tenant_id=tenant_a))

    rec = await store.get(candidate_id=cid, tenant_id=tenant_a)
    assert rec is not None
    impostor = rec.model_copy(update={"tenant_id": tenant_b})
    updated = await store.update(impostor)
    assert updated is False


@pytest.mark.asyncio
async def test_revert_promoted_for_dataset_reverts_only_matching_rows() -> None:
    store = InMemoryCurationCandidateStore()
    tenant, d1, d2 = uuid4(), uuid4(), uuid4()

    def _promote(rec: CurationCandidateRecord, dataset_id: UUID) -> CurationCandidateRecord:
        return rec.model_copy(
            update={
                "status": CandidateStatus.PROMOTED,
                "eval_dataset_id": dataset_id,
                "reviewed_at": _BASE,
            }
        )

    hit_a, hit_b = uuid4(), uuid4()
    await store.upsert(_promote(_candidate(candidate_id=hit_a, tenant_id=tenant), d1))
    await store.upsert(_promote(_candidate(candidate_id=hit_b, tenant_id=tenant), d1))
    # PROMOTED but pointing at another dataset — untouched.
    other_ds = uuid4()
    await store.upsert(_promote(_candidate(candidate_id=other_ds, tenant_id=tenant), d2))
    # DISMISSED row pointing at D1 — mutation sentinel: must stay DISMISSED.
    dismissed = uuid4()
    await store.upsert(
        _candidate(candidate_id=dismissed, tenant_id=tenant).model_copy(
            update={
                "status": CandidateStatus.DISMISSED,
                "eval_dataset_id": d1,
                "reviewed_at": _BASE,
            }
        )
    )
    # Same dataset, another tenant — untouched.
    foreign_tenant, foreign = uuid4(), uuid4()
    await store.upsert(_promote(_candidate(candidate_id=foreign, tenant_id=foreign_tenant), d1))

    reverted = await store.revert_promoted_for_dataset(dataset_id=d1, tenant_id=tenant)
    assert reverted == 2

    for cid in (hit_a, hit_b):
        rec = await store.get(candidate_id=cid, tenant_id=tenant)
        assert rec is not None
        assert rec.status is CandidateStatus.PENDING
        assert rec.eval_dataset_id is None
        # Other columns untouched — the review history stays.
        assert rec.reviewed_at == _BASE

    untouched_other = await store.get(candidate_id=other_ds, tenant_id=tenant)
    assert untouched_other is not None
    assert untouched_other.status is CandidateStatus.PROMOTED
    assert untouched_other.eval_dataset_id == d2

    untouched_dismissed = await store.get(candidate_id=dismissed, tenant_id=tenant)
    assert untouched_dismissed is not None
    assert untouched_dismissed.status is CandidateStatus.DISMISSED
    assert untouched_dismissed.eval_dataset_id == d1

    untouched_foreign = await store.get(candidate_id=foreign, tenant_id=foreign_tenant)
    assert untouched_foreign is not None
    assert untouched_foreign.status is CandidateStatus.PROMOTED
    assert untouched_foreign.eval_dataset_id == d1


@pytest.mark.asyncio
async def test_record_retry_increments_and_scopes_by_tenant() -> None:
    store = InMemoryCurationCandidateStore()
    cid, tenant = uuid4(), uuid4()
    await store.upsert(_candidate(candidate_id=cid, tenant_id=tenant))

    first = await store.record_retry(candidate_id=cid, tenant_id=tenant)
    second = await store.record_retry(candidate_id=cid, tenant_id=tenant)
    assert (first, second) == (1, 2)
    rec = await store.get(candidate_id=cid, tenant_id=tenant)
    assert rec is not None and rec.retry_count == 2
    # A retried candidate stays unevolved — the sweep re-picks it.
    assert rec.evolved_at is None

    # Wrong tenant → no bump, returns 0.
    assert await store.record_retry(candidate_id=cid, tenant_id=uuid4()) == 0
