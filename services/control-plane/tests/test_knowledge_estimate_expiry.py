"""B-222 PR-2 (§6.5) —— the recovery worker withdraws estimates nobody confirmed
within 7 days, audits each as ``system`` and drops expired parse snapshots,
every cycle; a failure there leaves the cycle's claims alone."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from control_plane.audit import build_default_audit_logger
from control_plane.knowledge import recovery
from control_plane.knowledge.recovery import KnowledgeIngestRecoveryWorker
from control_plane.settings import DEFAULT_DEV_TENANT_ID
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.knowledge.estimates import ExpiredEstimate, ParseSnapshot
from expert_work.protocol import AuditQuery, DocumentStatus
from orchestrator.llm import FakeEmbedder

_TENANT = DEFAULT_DEV_TENANT_ID
_ESTIMATE = {
    "id": "6f1c1f5e-1f43-4c55-9a52-0d1c5f8c0d11",
    "version": 1,
    "origin": "upload",
    "created_at": "2026-10-01T00:00:00+00:00",
    "total_micros": 1200,
}


class _Store(InMemoryKnowledgeStore):
    """Counts snapshot purges; ``broken`` makes the expiry sweep raise."""

    def __init__(self) -> None:
        super().__init__()
        self.purges = 0
        self.broken = False

    async def expire_awaiting(self, *, now: datetime, limit: int) -> list[ExpiredEstimate]:
        if self.broken:
            msg = "database went away"
            raise RuntimeError(msg)
        return await super().expire_awaiting(now=now, limit=limit)

    async def purge_expired_snapshots(self, *, now: datetime) -> int:
        self.purges += 1
        return await super().purge_expired_snapshots(now=now)


def _worker(store: _Store, audit: InMemoryAuditLogStore) -> KnowledgeIngestRecoveryWorker:
    return KnowledgeIngestRecoveryWorker(
        store=store,
        embedder=FakeEmbedder(),
        interval_s=30,
        concurrency=1,
        lease_seconds=300,
        max_attempts=5,
        audit=build_default_audit_logger(audit),
    )


async def _waiting(store: _Store, name: str, *, confirm_by: datetime) -> UUID:
    """A new upload whose estimate waits until ``confirm_by``."""
    base = await store.get_base(tenant_id=_TENANT, name="kb") or await store.create_base(
        tenant_id=_TENANT, name="kb"
    )
    body = f"# {name}\n\ntext".encode()
    start = await store.begin_upload(
        tenant_id=_TENANT,
        kb_id=base.id,
        filename=name,
        content=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        now=datetime.now(UTC),
    )
    claim = await store.claim_document(
        tenant_id=_TENANT,
        document_id=start.document.id,
        now=datetime.now(UTC),
        lease_seconds=300,
        max_attempts=5,
    )
    assert claim is not None and claim.phase == "estimate"
    assert await store.finish_estimate(
        tenant_id=_TENANT,
        document_id=claim.document_id,
        generation=claim.generation,
        estimate={**_ESTIMATE, "id": str(UUID(int=claim.generation + len(name)))},
        snapshot=ParseSnapshot(format="local-parse/1", payload=b"x"),
        confirm_by=confirm_by,
    )
    return claim.document_id


async def test_an_estimate_past_its_deadline_is_withdrawn_and_audited_as_system() -> None:
    store, audit = _Store(), InMemoryAuditLogStore()
    now = datetime.now(UTC)
    expired = await _waiting(store, "old.md", confirm_by=now - timedelta(minutes=1))
    current = await _waiting(store, "new.md", confirm_by=now + timedelta(days=7))

    await _worker(store, audit).run_once()

    assert await store.get_document(tenant_id=_TENANT, document_id=expired) is None
    still = await store.get_document(tenant_id=_TENANT, document_id=current)
    assert still is not None and still.status is DocumentStatus.AWAITING_CONFIRMATION
    page = await audit.query(AuditQuery(tenant_id=_TENANT, limit=10))
    rows = [
        (str(e.action), e.actor_id, e.resource_id, dict(e.details))
        for e in page.entries
        if str(e.action).startswith("knowledge:")
    ]
    kb = await store.get_base(tenant_id=_TENANT, name="kb")
    assert kb is not None
    assert rows == [
        (
            "knowledge:document_estimate_expired",
            "system",
            str(expired),
            {"kb_id": str(kb.id), "filename": "old.md", "outcome": "withdrawn_deleted"},
        )
    ]
    assert store.purges == 1


async def test_an_expired_new_upload_loses_its_figures_and_frees_its_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review L7 + M5: as a user's withdrawal does."""
    deleted: list[UUID | None] = []

    async def record(store: object, *, tenant_id: UUID, kb_id: UUID, document_id: UUID) -> None:
        deleted.append(document_id)

    monkeypatch.setattr(recovery, "delete_figures_quietly", record)
    store, audit = _Store(), InMemoryAuditLogStore()
    now = datetime.now(UTC)
    original = await _waiting(store, "old.md", confirm_by=now - timedelta(minutes=1))
    kb = await store.get_base(tenant_id=_TENANT, name="kb")
    assert kb is not None
    body = b"# old.md\n\ntext"
    copy = await store.begin_upload(
        tenant_id=_TENANT,
        kb_id=kb.id,
        filename="copy.md",
        content=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        now=now,
    )
    assert copy.outcome == "duplicate"

    await _worker(store, audit).run_once()

    assert deleted == [original]
    freed = await store.get_document(tenant_id=_TENANT, document_id=copy.document.id)
    # Released, then estimated by the same cycle's claims.
    assert freed is not None and freed.ingest_estimate is not None
    assert freed.status is DocumentStatus.AWAITING_CONFIRMATION
    assert freed.ingest_estimate.duplicate_of is None


async def test_a_failing_expiry_sweep_leaves_the_claims_of_the_cycle_alone() -> None:
    store, audit = _Store(), InMemoryAuditLogStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    queued = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="q.md", content=b"# Q\n\ntext q"
    )
    store.broken = True

    settled = await _worker(store, audit).run_once()

    assert settled == 1  # the queued document was still claimed and ingested
    document = await store.get_document(tenant_id=_TENANT, document_id=queued.id)
    assert document is not None and document.status is DocumentStatus.READY
