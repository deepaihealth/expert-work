"""Tests for the J.5 ``KnowledgeIngestionRunner``."""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import DocumentStatus, KnowledgeBase, KnowledgeChunk, KnowledgeDocument
from orchestrator.llm import FakeEmbedder


async def _seed(
    store: InMemoryKnowledgeStore, filename: str
) -> tuple[UUID, KnowledgeBase, KnowledgeDocument]:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename=filename)
    return tenant, base, document


async def _ingest(store: InMemoryKnowledgeStore, filename: str, raw: bytes) -> KnowledgeDocument:
    """Submit one document, await it, and return the refreshed document row."""
    tenant, base, document = await _seed(store, filename)
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder())
    await runner.submit(
        tenant_id=tenant,
        document_id=document.id,
        kb_id=base.id,
        filename=filename,
        raw=raw,
        chunk_max_tokens=512,
        chunk_overlap_tokens=64,
    )
    fetched = await store.get_document(tenant_id=tenant, document_id=document.id)
    assert fetched is not None
    return fetched


@pytest.mark.asyncio
async def test_ingest_marks_document_ready_with_chunks() -> None:
    store = InMemoryKnowledgeStore()
    document = await _ingest(
        store, "notes.md", b"# Handbook\n\nThe deductible is 500 dollars per year."
    )
    assert document.status is DocumentStatus.READY
    assert document.chunk_count >= 1
    assert document.error is None


@pytest.mark.asyncio
async def test_ingest_marks_document_failed_on_unparseable_file() -> None:
    store = InMemoryKnowledgeStore()
    document = await _ingest(store, "broken.pdf", b"this is definitely not a valid pdf")
    assert document.status is DocumentStatus.FAILED
    assert document.error


@pytest.mark.asyncio
async def test_ingest_empty_document_failed() -> None:
    store = InMemoryKnowledgeStore()
    document = await _ingest(store, "empty.md", b"   ")
    assert document.status is DocumentStatus.FAILED


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
    """模拟删除竞态:写回时文档已被并发删除,FK/守卫拒绝写回."""

    async def replace_chunks(
        self, *, tenant_id: UUID, document_id: UUID, chunks: Sequence[KnowledgeChunk]
    ) -> None:
        await self.delete_document(tenant_id=tenant_id, document_id=document_id)
        raise KeyError(document_id)


class _FailingReplaceStore(_RecordingStore):
    """写回失败但文档仍在 —— 必须照旧 mark failed."""

    async def replace_chunks(
        self, *, tenant_id: UUID, document_id: UUID, chunks: Sequence[KnowledgeChunk]
    ) -> None:
        raise RuntimeError("replace_chunks exploded")


@pytest.mark.asyncio
async def test_ingest_document_deleted_mid_flight_is_not_marked_failed() -> None:
    """删除竞态守卫:文档已被并发删除 → 不 mark failed,静默结束."""
    store = _ConcurrentDeleteStore()
    tenant, base, document = await _seed(store, "notes.md")
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder())
    await runner.submit(
        tenant_id=tenant,
        document_id=document.id,
        kb_id=base.id,
        filename="notes.md",
        raw=b"# Doc\n\nbody text here.",
        chunk_max_tokens=512,
        chunk_overlap_tokens=64,
    )
    assert store.failed_terminal == []
    assert await store.get_document(tenant_id=tenant, document_id=document.id) is None


@pytest.mark.asyncio
async def test_ingest_failure_with_document_still_present_marks_failed() -> None:
    """守卫只放行"已删"分支:文档仍在的失败照旧 mark failed."""
    store = _FailingReplaceStore()
    tenant, base, document = await _seed(store, "notes.md")
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder())
    await runner.submit(
        tenant_id=tenant,
        document_id=document.id,
        kb_id=base.id,
        filename="notes.md",
        raw=b"# Doc\n\nbody text here.",
        chunk_max_tokens=512,
        chunk_overlap_tokens=64,
    )
    assert store.failed_terminal == [document.id]
    fetched = await store.get_document(tenant_id=tenant, document_id=document.id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.FAILED
    assert fetched.error == "replace_chunks exploded"


@pytest.mark.asyncio
async def test_drain_awaits_outstanding_tasks() -> None:
    store = InMemoryKnowledgeStore()
    tenant, base, document = await _seed(store, "notes.md")
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder())
    runner.submit(
        tenant_id=tenant,
        document_id=document.id,
        kb_id=base.id,
        filename="notes.md",
        raw=b"# Doc\n\nbody text here.",
        chunk_max_tokens=512,
        chunk_overlap_tokens=64,
    )
    await runner.drain()
    fetched = await store.get_document(tenant_id=tenant, document_id=document.id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.READY
