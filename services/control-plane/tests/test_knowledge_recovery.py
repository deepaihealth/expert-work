"""Tests for the knowledge ingestion recovery worker — Stream KB durability.

In-memory logic tests: the worker claims stuck documents (``pending`` /
lease-expired ``processing``) and re-drives them from retained bytes. The
exactly-once CAS guarantee under concurrency is only meaningfully testable
against real Postgres (see ``test_sql_knowledge_store.py``)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge.recovery import KnowledgeIngestRecoveryWorker
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import DocumentStatus, KnowledgeChunk
from orchestrator.llm import FakeEmbedder

_DOC = b"# Handbook\n\nThe deductible is 500 dollars per year."


def _worker(
    store: InMemoryKnowledgeStore, *, max_attempts: int = 5
) -> KnowledgeIngestRecoveryWorker:
    return KnowledgeIngestRecoveryWorker(
        store=store,
        embedder=FakeEmbedder(),
        interval_s=1,
        batch_size=10,
        lease_seconds=300,
        max_attempts=max_attempts,
    )


@pytest.mark.asyncio
async def test_recovers_pending_document_from_bytes() -> None:
    # A document uploaded (bytes persisted, status pending) but whose fast-path
    # task never ran (e.g. a crash) is drained by the worker.
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    doc = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="h.md", content=_DOC
    )

    settled = await _worker(store).run_once()
    assert settled == 1
    fetched = await store.get_document(tenant_id=tenant, document_id=doc.id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.READY
    assert fetched.chunk_count >= 1


@pytest.mark.asyncio
async def test_recovers_processing_document_with_expired_lease() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    doc = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="h.md", content=_DOC
    )
    # Simulate a crashed claim: processing with an already-expired (0s) lease.
    claimed = await store.claim_document(
        tenant_id=tenant,
        document_id=doc.id,
        now=datetime.now(UTC),
        lease_seconds=0,
        max_attempts=5,
    )
    assert claimed is not None

    settled = await _worker(store).run_once()
    assert settled == 1
    fetched = await store.get_document(tenant_id=tenant, document_id=doc.id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.READY


@pytest.mark.asyncio
async def test_legacy_document_without_bytes_fails_terminally() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    # No content retained (legacy row).
    doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="h.md")

    settled = await _worker(store).run_once()
    assert settled == 1
    fetched = await store.get_document(tenant_id=tenant, document_id=doc.id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.FAILED
    assert fetched.error is not None


@pytest.mark.asyncio
async def test_ready_document_is_not_reclaimed() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    doc = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="h.md", content=_DOC
    )
    await store.set_document_status(
        tenant_id=tenant, document_id=doc.id, status=DocumentStatus.READY, chunk_count=2
    )
    assert await _worker(store).run_once() == 0


class _RecordingStore(InMemoryKnowledgeStore):
    """Records ``mark_document_failed_terminal`` calls for assertion."""

    def __init__(self) -> None:
        super().__init__()
        self.failed_terminal: list[UUID] = []

    async def mark_document_failed_terminal(
        self, *, tenant_id: UUID, document_id: UUID, error: str
    ) -> None:
        self.failed_terminal.append(document_id)
        await super().mark_document_failed_terminal(
            tenant_id=tenant_id, document_id=document_id, error=error
        )


class _ConcurrentDeleteStore(_RecordingStore):
    """模拟删除竞态:重新驱动写回时文档已被并发删除,FK/守卫拒绝写回."""

    async def replace_chunks(
        self, *, tenant_id: UUID, document_id: UUID, chunks: Sequence[KnowledgeChunk]
    ) -> None:
        await self.delete_document(tenant_id=tenant_id, document_id=document_id)
        raise KeyError(document_id)


class _FailingReplaceStore(_RecordingStore):
    """写回失败但文档仍在 —— 重试耗尽后必须照旧 mark failed."""

    async def replace_chunks(
        self, *, tenant_id: UUID, document_id: UUID, chunks: Sequence[KnowledgeChunk]
    ) -> None:
        raise RuntimeError("replace_chunks exploded")


async def _seed(store: InMemoryKnowledgeStore) -> tuple[UUID, UUID]:
    """One claimable document with retained bytes; returns (tenant, document)."""
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    doc = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="h.md", content=_DOC
    )
    return tenant, doc.id


@pytest.mark.asyncio
async def test_recovery_document_deleted_mid_flight_is_not_marked_failed() -> None:
    """删除竞态守卫:重试耗尽的这一轮里文档已被并发删除 → 不 mark failed,静默结束."""
    store = _ConcurrentDeleteStore()
    tenant, document_id = await _seed(store)
    # max_attempts=1 → 本轮 claim 即为最后一次尝试,走 mark failed 那条路径。
    settled = await _worker(store, max_attempts=1).run_once()
    assert store.failed_terminal == []
    assert settled == 0
    assert await store.get_document(tenant_id=tenant, document_id=document_id) is None


@pytest.mark.asyncio
async def test_recovery_failure_with_document_still_present_marks_failed() -> None:
    """守卫只放行「已删」分支:文档仍在的失败照旧 mark failed."""
    store = _FailingReplaceStore()
    tenant, document_id = await _seed(store)
    settled = await _worker(store, max_attempts=1).run_once()
    assert settled == 1
    assert store.failed_terminal == [document_id]
    fetched = await store.get_document(tenant_id=tenant, document_id=document_id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.FAILED
    assert fetched.error == "replace_chunks exploded"


@pytest.mark.asyncio
async def test_recovery_document_deleted_mid_flight_logs_no_retry_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """还有重试余额时同样静默:已删文档不该留一轮 retry WARNING."""
    store = _ConcurrentDeleteStore()
    await _seed(store)
    with caplog.at_level(logging.WARNING):
        settled = await _worker(store, max_attempts=5).run_once()
    assert settled == 0
    assert store.failed_terminal == []
    # 只看 recovery 自己的 logger:CI 里其他测试遗留的 otel exporter 后台线程
    # 会往 root 吐 "Transient error ... retrying" WARNING,污染全局断言(flake)。
    assert [
        r.message
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and r.name.startswith("expert_work.control_plane.knowledge.recovery")
    ] == []


@pytest.mark.asyncio
async def test_exhausted_attempts_not_reclaimed() -> None:
    # A document already at max attempts is past its retry budget — the worker
    # leaves it alone (a separate manual re-ingest is the path forward).
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    doc = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="h.md", content=_DOC
    )
    # Burn the attempt budget with 0s leases so it stays claimable until max.
    for _ in range(3):
        await store.claim_document(
            tenant_id=tenant,
            document_id=doc.id,
            now=datetime.now(UTC),
            lease_seconds=0,
            max_attempts=3,
        )
    assert await _worker(store, max_attempts=3).run_once() == 0
