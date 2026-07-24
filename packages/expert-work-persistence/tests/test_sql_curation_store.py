"""Integration tests for the SQL curation stores against Postgres — J.12."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlCurationCandidateStore,
    SqlEvalDatasetStore,
    create_async_engine_from_config,
    create_async_session_factory,
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

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

_BASE = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def curation_stores(
    postgres_container: PostgresContainer,
) -> Iterator[tuple[SqlEvalDatasetStore, SqlCurationCandidateStore]]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    sf = create_async_session_factory(engine)
    yield SqlEvalDatasetStore(sf), SqlCurationCandidateStore(sf)


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
        input={"prompt": "hello"},
        expected=expected,
        source=source,
        source_trajectory_key="trajectories/t/abc.jsonl" if source != "golden" else None,
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
        detected_at=_BASE,
    )


# --- SqlEvalDatasetStore ---------------------------------------------------


@pytest.mark.asyncio
async def test_dataset_create_then_get_round_trips(
    curation_stores: tuple[SqlEvalDatasetStore, SqlCurationCandidateStore],
) -> None:
    datasets, _ = curation_stores
    did, tenant = uuid4(), uuid4()
    await datasets.create(
        _dataset(dataset_id=did, tenant_id=tenant, source="regression", expected={"answer": "x"})
    )

    fetched = await datasets.get(dataset_id=did, tenant_id=tenant)
    assert fetched is not None
    assert fetched.input == {"prompt": "hello"}
    assert fetched.expected == {"answer": "x"}
    assert fetched.source == "regression"


@pytest.mark.asyncio
async def test_dataset_get_cross_tenant_returns_none(
    curation_stores: tuple[SqlEvalDatasetStore, SqlCurationCandidateStore],
) -> None:
    datasets, _ = curation_stores
    did, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await datasets.create(_dataset(dataset_id=did, tenant_id=tenant_a))

    assert await datasets.get(dataset_id=did, tenant_id=tenant_b) is None


@pytest.mark.asyncio
async def test_dataset_trajectory_source_allows_null_expected(
    curation_stores: tuple[SqlEvalDatasetStore, SqlCurationCandidateStore],
) -> None:
    datasets, _ = curation_stores
    did, tenant = uuid4(), uuid4()
    await datasets.create(
        _dataset(dataset_id=did, tenant_id=tenant, source="trajectory", expected=None)
    )

    fetched = await datasets.get(dataset_id=did, tenant_id=tenant)
    assert fetched is not None
    assert fetched.expected is None


@pytest.mark.asyncio
async def test_dataset_list_by_agent_and_update(
    curation_stores: tuple[SqlEvalDatasetStore, SqlCurationCandidateStore],
) -> None:
    datasets, _ = curation_stores
    did, tenant = uuid4(), uuid4()
    await datasets.create(_dataset(dataset_id=did, tenant_id=tenant, agent_name="reporter"))
    await datasets.create(_dataset(tenant_id=tenant, agent_name="auditor"))

    listed = await datasets.list_by_agent(tenant_id=tenant, agent_name="reporter")
    assert {r.id for r in listed} == {did}

    rec = listed[0]
    updated = await datasets.update(rec.model_copy(update={"expected": {"answer": "v2"}}))
    assert updated is True
    again = await datasets.get(dataset_id=did, tenant_id=tenant)
    assert again is not None
    assert again.expected == {"answer": "v2"}


@pytest.mark.asyncio
async def test_dataset_delete_and_count(
    curation_stores: tuple[SqlEvalDatasetStore, SqlCurationCandidateStore],
) -> None:
    datasets, _ = curation_stores
    did, tenant = uuid4(), uuid4()
    await datasets.create(_dataset(dataset_id=did, tenant_id=tenant))
    await datasets.create(_dataset(tenant_id=tenant))

    assert await datasets.count_by_tenant(tenant_id=tenant) == 2
    deleted = await datasets.delete(dataset_id=did, tenant_id=tenant)
    assert deleted is True
    assert await datasets.count_by_tenant(tenant_id=tenant) == 1


# --- SqlCurationCandidateStore ---------------------------------------------


@pytest.mark.asyncio
async def test_candidate_upsert_skips_duplicate_trajectory(
    curation_stores: tuple[SqlEvalDatasetStore, SqlCurationCandidateStore],
) -> None:
    _, candidates = curation_stores
    tenant, key = uuid4(), "trajectories/dup.jsonl"
    inserted = await candidates.upsert(_candidate(tenant_id=tenant, trajectory_key=key))
    assert inserted is True
    again = await candidates.upsert(_candidate(tenant_id=tenant, trajectory_key=key))
    assert again is False


@pytest.mark.asyncio
async def test_candidate_get_round_trips(
    curation_stores: tuple[SqlEvalDatasetStore, SqlCurationCandidateStore],
) -> None:
    _, candidates = curation_stores
    cid, tenant = uuid4(), uuid4()
    await candidates.upsert(
        _candidate(
            candidate_id=cid,
            tenant_id=tenant,
            signal="negative_feedback",
            feedback_rating="down",
        )
    )

    fetched = await candidates.get(candidate_id=cid, tenant_id=tenant)
    assert fetched is not None
    assert fetched.signal == "negative_feedback"
    assert fetched.feedback_rating == "down"
    assert fetched.status is CandidateStatus.PENDING
    assert await candidates.get(candidate_id=cid, tenant_id=uuid4()) is None


@pytest.mark.asyncio
async def test_candidate_get_by_trajectory_key(
    curation_stores: tuple[SqlEvalDatasetStore, SqlCurationCandidateStore],
) -> None:
    _, candidates = curation_stores
    tenant, key = uuid4(), "trajectories/lookup.jsonl"
    await candidates.upsert(_candidate(tenant_id=tenant, trajectory_key=key))

    found = await candidates.get_by_trajectory_key(tenant_id=tenant, trajectory_key=key)
    assert found is not None
    assert found.trajectory_key == key


@pytest.mark.asyncio
async def test_candidate_list_for_review_filters(
    curation_stores: tuple[SqlEvalDatasetStore, SqlCurationCandidateStore],
) -> None:
    _, candidates = curation_stores
    tenant = uuid4()
    await candidates.upsert(_candidate(tenant_id=tenant, agent_name="reporter"))
    await candidates.upsert(
        _candidate(
            tenant_id=tenant,
            agent_name="reporter",
            signal="positive_feedback",
            outcome="success",
            feedback_rating="up",
        )
    )
    await candidates.upsert(_candidate(tenant_id=tenant, agent_name="auditor"))

    by_agent = await candidates.list_for_review(tenant_id=tenant, agent_name="reporter")
    assert len(by_agent) == 2
    by_signal = await candidates.list_for_review(tenant_id=tenant, signal="positive_feedback")
    assert len(by_signal) == 1


@pytest.mark.asyncio
async def test_candidate_update_records_promotion(
    curation_stores: tuple[SqlEvalDatasetStore, SqlCurationCandidateStore],
) -> None:
    _, candidates = curation_stores
    cid, tenant = uuid4(), uuid4()
    await candidates.upsert(_candidate(candidate_id=cid, tenant_id=tenant))

    rec = await candidates.get(candidate_id=cid, tenant_id=tenant)
    assert rec is not None
    promoted = rec.model_copy(
        update={
            "status": CandidateStatus.PROMOTED,
            "eval_dataset_id": uuid4(),
            "reviewed_at": _BASE,
        }
    )
    updated = await candidates.update(promoted)
    assert updated is True

    again = await candidates.get(candidate_id=cid, tenant_id=tenant)
    assert again is not None
    assert again.status is CandidateStatus.PROMOTED
    assert again.eval_dataset_id is not None


@pytest.mark.asyncio
async def test_revert_promoted_for_dataset_reverts_only_matching_rows(
    curation_stores: tuple[SqlEvalDatasetStore, SqlCurationCandidateStore],
) -> None:
    _, candidates = curation_stores
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
    await candidates.upsert(_promote(_candidate(candidate_id=hit_a, tenant_id=tenant), d1))
    await candidates.upsert(_promote(_candidate(candidate_id=hit_b, tenant_id=tenant), d1))
    # PROMOTED but pointing at another dataset — untouched.
    other_ds = uuid4()
    await candidates.upsert(_promote(_candidate(candidate_id=other_ds, tenant_id=tenant), d2))
    # DISMISSED row pointing at D1 — mutation sentinel: must stay DISMISSED.
    dismissed = uuid4()
    await candidates.upsert(
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
    await candidates.upsert(
        _promote(_candidate(candidate_id=foreign, tenant_id=foreign_tenant), d1)
    )

    reverted = await candidates.revert_promoted_for_dataset(dataset_id=d1, tenant_id=tenant)
    assert reverted == 2

    for cid in (hit_a, hit_b):
        rec = await candidates.get(candidate_id=cid, tenant_id=tenant)
        assert rec is not None
        assert rec.status is CandidateStatus.PENDING
        assert rec.eval_dataset_id is None
        # Other columns untouched — the review history stays.
        assert rec.reviewed_at == _BASE

    untouched_other = await candidates.get(candidate_id=other_ds, tenant_id=tenant)
    assert untouched_other is not None
    assert untouched_other.status is CandidateStatus.PROMOTED
    assert untouched_other.eval_dataset_id == d2

    untouched_dismissed = await candidates.get(candidate_id=dismissed, tenant_id=tenant)
    assert untouched_dismissed is not None
    assert untouched_dismissed.status is CandidateStatus.DISMISSED
    assert untouched_dismissed.eval_dataset_id == d1

    untouched_foreign = await candidates.get(candidate_id=foreign, tenant_id=foreign_tenant)
    assert untouched_foreign is not None
    assert untouched_foreign.status is CandidateStatus.PROMOTED
    assert untouched_foreign.eval_dataset_id == d1


@pytest.mark.asyncio
async def test_record_retry_atomic_increment_sql(
    curation_stores: tuple[SqlEvalDatasetStore, SqlCurationCandidateStore],
) -> None:
    """SE-16 (SE-A40) — retry bump is an atomic UPDATE..RETURNING; the
    candidate stays unevolved so the next sweep re-picks it."""
    _, candidates = curation_stores
    cid, tenant = uuid4(), uuid4()
    await candidates.upsert(_candidate(candidate_id=cid, tenant_id=tenant))

    assert await candidates.record_retry(candidate_id=cid, tenant_id=tenant) == 1
    assert await candidates.record_retry(candidate_id=cid, tenant_id=tenant) == 2
    rec = await candidates.get(candidate_id=cid, tenant_id=tenant)
    assert rec is not None and rec.retry_count == 2 and rec.evolved_at is None
    # Cross-tenant probe: no bump, returns 0.
    assert await candidates.record_retry(candidate_id=cid, tenant_id=uuid4()) == 0


@pytest.mark.asyncio
async def test_implicit_success_signal_round_trips_sql(
    curation_stores: tuple[SqlEvalDatasetStore, SqlCurationCandidateStore],
) -> None:
    """SE-16 (SE-A38) — migration 0108 widens the signal CHECK."""
    _, candidates = curation_stores
    cid, tenant = uuid4(), uuid4()
    await candidates.upsert(
        _candidate(candidate_id=cid, tenant_id=tenant).model_copy(
            update={"signal": "implicit_success"}
        )
    )
    rec = await candidates.get(candidate_id=cid, tenant_id=tenant)
    assert rec is not None and rec.signal == "implicit_success"
