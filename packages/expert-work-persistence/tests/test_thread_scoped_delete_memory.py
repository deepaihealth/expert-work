"""B-280 —— in-memory ``delete_for_threads`` of the quality-score and curation stores,
and ``tombstone_for_threads`` of the upload registry.

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
from expert_work.persistence.user_upload import InMemoryUserUploadStore
from expert_work.protocol import (
    CurationCandidateRecord,
    QualityScoreRecord,
    is_session_deleted_tombstone,
)


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


@pytest.mark.asyncio
async def test_upload_tombstones_only_these_threads_in_this_tenant() -> None:
    store = InMemoryUserUploadStore()
    tenant, other_tenant, user = uuid4(), uuid4(), uuid4()
    doomed, sibling = uuid4(), uuid4()
    now = datetime.now(UTC)
    ids: dict[str, UUID] = {}
    for name, t, thread, kind in (
        ("doc", tenant, doomed, "document"),
        ("image", tenant, doomed, "image"),
        ("expired", tenant, doomed, "document"),
        ("sibling", tenant, sibling, "document"),
        ("elsewhere", other_tenant, doomed, "document"),
    ):
        ids[name] = uuid4()
        await store.insert(
            upload_id=ids[name],
            tenant_id=t,
            user_id=user,
            thread_id=thread,
            kind=kind,  # type: ignore[arg-type]
            ref="uploads/a.pdf",
            mime_type="application/pdf",
            size_bytes=9,
            filename="a.pdf",
            sha256="ab" * 32,
        )
    await store.soft_delete(upload_id=ids["expired"], tenant_id=tenant, now=now)

    assert await store.tombstone_for_threads(tenant_id=tenant, thread_ids=[doomed], now=now) == 3
    assert await store.tombstone_for_threads(tenant_id=tenant, thread_ids=[doomed], now=now) == 0

    doc = await store.get(upload_id=ids["doc"], tenant_id=tenant)
    assert doc is not None and is_session_deleted_tombstone(doc)
    assert (doc.ref, doc.filename, doc.mime_type, doc.size_bytes) == ("", "", "", 0)
    assert (doc.sha256, doc.last_used_at, doc.deleted_at) == (None, None, now)
    assert (doc.user_id, doc.thread_id, doc.kind) == (user, doomed, "document")
    assert await store.get(upload_id=ids["image"], tenant_id=tenant) is None
    assert await store.get(upload_id=ids["expired"], tenant_id=tenant) is None
    for name, t in (("sibling", tenant), ("elsewhere", other_tenant)):
        row = await store.get(upload_id=ids[name], tenant_id=t)
        assert row is not None and row.filename == "a.pdf" and row.deleted_at is None
