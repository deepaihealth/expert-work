"""B-280 —— in-memory ``delete_for_threads`` of the quality-score and curation stores.

Same contract as the SQL versions (pinned on Postgres by
``services/control-plane/tests/test_session_purge_children_integration.py``):
only the listed conversations' rows, only in this tenant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from expert_work.persistence.curation.memory import InMemoryCurationCandidateStore
from expert_work.persistence.quality_score.memory import InMemoryQualityScoreStore
from expert_work.protocol import CurationCandidateRecord, QualityScoreRecord


def _score(tenant: UUID, thread: UUID) -> QualityScoreRecord:
    return QualityScoreRecord(
        tenant_id=tenant,
        agent_name="a",
        agent_version="1",
        run_id=uuid4(),
        thread_id=thread,
        overall=3,
        dimensions={},
        rationale="r",
        judge_model="m",
    )


def _candidate(tenant: UUID, thread: UUID) -> CurationCandidateRecord:
    return CurationCandidateRecord(
        id=uuid4(),
        tenant_id=tenant,
        agent_name="a",
        thread_id=thread,
        trajectory_key=f"k-{uuid4().hex}",
        outcome="failed",
        signal="failed_outcome",
        detected_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_quality_scores_only_these_threads_in_this_tenant() -> None:
    store = InMemoryQualityScoreStore()
    tenant, other_tenant, doomed, sibling = uuid4(), uuid4(), uuid4(), uuid4()
    kept = [_score(tenant, sibling), _score(other_tenant, doomed)]
    for record in [_score(tenant, doomed), _score(tenant, doomed), *kept]:
        await store.insert(record)

    assert await store.delete_for_threads(tenant_id=tenant, thread_ids=[doomed]) == 2
    assert await store.delete_for_threads(tenant_id=tenant, thread_ids=[doomed]) == 0
    for record in kept:
        assert await store.exists(tenant_id=record.tenant_id, run_id=record.run_id)


@pytest.mark.asyncio
async def test_curation_candidates_only_these_threads_in_this_tenant() -> None:
    store = InMemoryCurationCandidateStore()
    tenant, other_tenant, doomed, sibling = uuid4(), uuid4(), uuid4(), uuid4()
    kept = [_candidate(tenant, sibling), _candidate(other_tenant, doomed)]
    for record in [_candidate(tenant, doomed), _candidate(tenant, doomed), *kept]:
        await store.upsert(record)

    assert await store.delete_for_threads(tenant_id=tenant, thread_ids=[doomed]) == 2
    assert await store.delete_for_threads(tenant_id=tenant, thread_ids=[doomed]) == 0
    for record in kept:
        assert await store.get(candidate_id=record.id, tenant_id=record.tenant_id) is not None
