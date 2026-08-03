"""Unit tests for the in-memory quality stores — Stream RT-5 (RT-ADR-22/24)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import (
    InMemoryQualityCandidateSource,
    InMemoryQualityScoreStore,
    QualityCandidate,
)
from expert_work.protocol import QualityScoreRecord


def _record(*, tenant: UUID, run: UUID, agent: str = "a", overall: int = 4) -> QualityScoreRecord:
    return QualityScoreRecord(
        tenant_id=tenant,
        agent_name=agent,
        agent_version="1",
        run_id=run,
        thread_id=uuid4(),
        overall=overall,
        dimensions={"addressed_request": overall, "coherence": overall, "safety": 5},
        rationale="ok",
        judge_model="m",
    )


@pytest.mark.asyncio
async def test_insert_is_idempotent_per_run() -> None:
    store = InMemoryQualityScoreStore()
    tenant, run = uuid4(), uuid4()
    first = await store.insert(_record(tenant=tenant, run=run, overall=4))
    assert first.id is not None
    assert first.observed_at is not None
    # Re-insert of the same run returns the stored row, does not duplicate.
    again = await store.insert(_record(tenant=tenant, run=run, overall=1))
    assert again.id == first.id
    assert again.overall == 4  # original verdict wins
    rows = await store.list_scores(tenant_id=tenant)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_exists_tracks_scored_runs() -> None:
    store = InMemoryQualityScoreStore()
    tenant, run = uuid4(), uuid4()
    assert await store.exists(tenant_id=tenant, run_id=run) is False
    await store.insert(_record(tenant=tenant, run=run))
    assert await store.exists(tenant_id=tenant, run_id=run) is True
    # Different run / tenant is unaffected.
    assert await store.exists(tenant_id=tenant, run_id=uuid4()) is False
    assert await store.exists(tenant_id=uuid4(), run_id=run) is False


@pytest.mark.asyncio
async def test_count_since_and_list_filters() -> None:
    store = InMemoryQualityScoreStore()
    tenant, other = uuid4(), uuid4()
    await store.insert(_record(tenant=tenant, run=uuid4(), agent="a"))
    await store.insert(_record(tenant=tenant, run=uuid4(), agent="b"))
    await store.insert(_record(tenant=other, run=uuid4(), agent="a"))

    day_start = datetime.now(tz=UTC) - timedelta(hours=1)
    assert await store.count_since(tenant_id=tenant, since=day_start) == 2
    assert await store.count_since(tenant_id=other, since=day_start) == 1
    # Future watermark → nothing counted.
    future = datetime.now(tz=UTC) + timedelta(hours=1)
    assert await store.count_since(tenant_id=tenant, since=future) == 0

    only_a = await store.list_scores(tenant_id=tenant, agent_name="a")
    assert len(only_a) == 1
    assert only_a[0].agent_name == "a"


@pytest.mark.asyncio
async def test_list_scores_all_tenants_id_tiebreak_on_equal_observed_at() -> None:
    """W4 review C-3 — ``observed_at`` ties order by ``id`` ASC. ``insert``
    assigns ids in insertion order (dict order == id order), so the tie is
    seeded white-box (precedent: ``test_in_memory_memory_consolidator.py``)
    in *descending* id order — a dropped id sort degrades to dict insertion
    order and the exact-order assertion goes red. Review #5 — a same-tenant
    pair rides along so the per-tenant sibling (``list_scores``) proves the
    identical tiebreak contract."""
    store = InMemoryQualityScoreStore()
    ts = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    tenant_a, tenant_b = uuid4(), uuid4()
    rec_1 = _record(tenant=tenant_a, run=uuid4()).model_copy(update={"id": 1, "observed_at": ts})
    rec_2 = _record(tenant=tenant_a, run=uuid4()).model_copy(update={"id": 2, "observed_at": ts})
    rec_3 = _record(tenant=tenant_b, run=uuid4()).model_copy(update={"id": 3, "observed_at": ts})
    for rec in (rec_3, rec_2, rec_1):  # descending id seed
        store._by_run[(rec.tenant_id, rec.run_id)] = rec

    rows = await store.list_scores_all_tenants()
    assert [r.id for r in rows] == [1, 2, 3]
    # Review #5 — the per-tenant sibling shares the id tiebreak contract.
    sibling = await store.list_scores(tenant_id=tenant_a)
    assert [r.id for r in sibling] == [1, 2]


@pytest.mark.asyncio
async def test_window_stats_and_agent_enumeration() -> None:
    store = InMemoryQualityScoreStore()
    t1, t2 = uuid4(), uuid4()
    base = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)

    async def _add(tenant: UUID, agent: str, overall: int, at: datetime) -> None:
        rec = _record(tenant=tenant, run=uuid4(), agent=agent, overall=overall)
        await store.insert(rec.model_copy(update={"observed_at": at}))

    await _add(t1, "a", 5, base)
    await _add(t1, "a", 3, base + timedelta(minutes=10))
    await _add(t1, "b", 4, base + timedelta(minutes=20))
    await _add(t2, "a", 2, base + timedelta(minutes=30))

    # window_stats: [since, until) with mean.
    count, mean = await store.window_stats(
        tenant_id=t1, agent_name="a", since=base, until=base + timedelta(hours=1)
    )
    assert count == 2
    assert mean == 4.0
    # Empty window → (0, None).
    empty_count, empty_mean = await store.window_stats(
        tenant_id=t1,
        agent_name="a",
        since=base + timedelta(hours=2),
        until=base + timedelta(hours=3),
    )
    assert empty_count == 0
    assert empty_mean is None

    # Cross-tenant distinct (tenant, agent) enumeration.
    agents = await store.list_agents_with_scores_since(since=base)
    assert set(agents) == {(t1, "a"), (t1, "b"), (t2, "a")}
    # Watermark excludes older rows.
    recent = await store.list_agents_with_scores_since(since=base + timedelta(minutes=25))
    assert set(recent) == {(t2, "a")}


@pytest.mark.asyncio
async def test_candidate_source_filters_by_watermark_and_orders() -> None:
    base = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    tenant = uuid4()
    cands = [
        QualityCandidate(
            run_id=uuid4(),
            tenant_id=tenant,
            thread_id=uuid4(),
            agent_name="a",
            agent_version="1",
            updated_at=base + timedelta(minutes=m),
        )
        for m in (5, 1, 3)
    ]
    source = InMemoryQualityCandidateSource(cands)
    got = await source.list_candidates(since=base + timedelta(minutes=1), limit=10)
    # Strictly after the watermark, oldest first.
    assert [c.updated_at for c in got] == [base + timedelta(minutes=3), base + timedelta(minutes=5)]
    assert await source.list_candidates(since=base + timedelta(minutes=10), limit=10) == []
