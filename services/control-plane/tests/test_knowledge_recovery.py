"""Tests for the knowledge ingestion recovery worker — Stream KB durability.

In-memory logic tests: the worker claims stuck documents (``pending`` /
lease-expired ``processing``) and re-drives them from retained bytes. The
exactly-once CAS guarantee under concurrency is only meaningfully testable
against real Postgres (see ``test_sql_knowledge_store.py``)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge.recovery import KnowledgeIngestRecoveryWorker
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var
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
        concurrency=10,
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
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        error: str,
        generation: int | None = None,
    ) -> bool:
        self.failed_terminal.append(document_id)
        return await super().mark_document_failed_terminal(
            tenant_id=tenant_id, document_id=document_id, error=error, generation=generation
        )


class _ConcurrentDeleteStore(_RecordingStore):
    """模拟删除竞态:重新驱动写回时文档已被并发删除,FK/守卫拒绝写回."""

    async def replace_chunks(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        chunks: Sequence[KnowledgeChunk],
        generation: int | None = None,
        expect_status: DocumentStatus = DocumentStatus.PROCESSING,
    ) -> bool:
        await self.delete_document(tenant_id=tenant_id, document_id=document_id)
        raise KeyError(document_id)


class _FailingReplaceStore(_RecordingStore):
    """写回失败但文档仍在 —— 重试耗尽后必须照旧 mark failed."""

    async def replace_chunks(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        chunks: Sequence[KnowledgeChunk],
        generation: int | None = None,
        expect_status: DocumentStatus = DocumentStatus.PROCESSING,
    ) -> bool:
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
async def test_exhausted_attempts_swept_to_failed_not_reclaimed() -> None:
    # A document already at max attempts is past its retry budget — it is not
    # re-driven; the sweep turns it terminally failed instead of leaving it
    # stuck in ``processing`` forever (the "crash on the 5th attempt" bug).
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
    fetched = await store.get_document(tenant_id=tenant, document_id=doc.id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.FAILED
    assert fetched.error == "ingest attempts exhausted"
    assert fetched.attempts == 3


class _GateEmbedder(FakeEmbedder):
    """Every ``embed`` call parks until the test releases the gate — so the
    number of callers parked at once is the number of documents being driven
    concurrently, independent of scheduling noise."""

    def __init__(self) -> None:
        self.entered = 0
        self.release = asyncio.Event()

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.entered += 1
        await self.release.wait()
        return await super().embed(texts, tenant_id=tenant_id)


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_run_once_claims_concurrency_many_and_drives_them_in_parallel() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    for i in range(25):
        await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename=f"{i}.md", content=_DOC
        )
    embedder = _GateEmbedder()
    worker = KnowledgeIngestRecoveryWorker(
        store=store,
        embedder=embedder,
        interval_s=1,
        concurrency=4,
        lease_seconds=300,
        max_attempts=5,
    )
    cycle = asyncio.create_task(worker.run_once())
    # Four documents reach the embedder while the gate is shut: they are being
    # driven at the same time. A serial worker would park one and hang here.
    await _wait_until(lambda: embedder.entered >= 4)
    await asyncio.sleep(0.2)
    assert embedder.entered == 4  # and no more than the concurrency bound
    embedder.release.set()
    assert await cycle == 4
    docs = await store.list_documents(tenant_id=tenant, kb_id=base.id)
    assert sum(d.status is DocumentStatus.READY for d in docs) == 4
    assert sum(d.status is DocumentStatus.PENDING for d in docs) == 21


class _RenewRecordingStore(InMemoryKnowledgeStore):
    def __init__(self) -> None:
        super().__init__()
        self.renewals = 0

    async def renew_lease(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        generation: int,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        self.renewals += 1
        return await super().renew_lease(
            tenant_id=tenant_id,
            document_id=document_id,
            generation=generation,
            now=now,
            lease_seconds=lease_seconds,
        )


class _WaitForRenewalsEmbedder(FakeEmbedder):
    def __init__(self, store: _RenewRecordingStore, wanted: int) -> None:
        self._store = store
        self._wanted = wanted

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        await _wait_until(lambda: self._store.renewals >= self._wanted)
        return await super().embed(texts, tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_recovery_renews_lease_while_driving() -> None:
    store = _RenewRecordingStore()
    await _seed(store)
    worker = KnowledgeIngestRecoveryWorker(
        store=store,
        embedder=_WaitForRenewalsEmbedder(store, 2),
        interval_s=1,
        concurrency=1,
        lease_seconds=1,
        max_attempts=5,
    )
    assert await worker.run_once() == 1
    assert store.renewals >= 2


class _ContextRecordingStore(InMemoryKnowledgeStore):
    """Records the RLS context variables seen by every chunk write-back."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[UUID, UUID | None, bool]] = []

    async def replace_chunks(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        chunks: Sequence[KnowledgeChunk],
        generation: int | None = None,
        expect_status: DocumentStatus = DocumentStatus.PROCESSING,
    ) -> bool:
        self.seen.append((tenant_id, current_tenant_id_var.get(), bypass_rls_var.get()))
        return await super().replace_chunks(
            tenant_id=tenant_id,
            document_id=document_id,
            chunks=chunks,
            generation=generation,
            expect_status=expect_status,
        )


@pytest.mark.asyncio
async def test_concurrent_drives_run_in_their_own_tenant_rls_context() -> None:
    """并发驱动两个租户的文档:每个写回看到的 RLS 上下文是自己租户的,且不 bypass."""
    store = _ContextRecordingStore()
    tenants = [uuid4(), uuid4()]
    for tenant in tenants:
        base = await store.create_base(tenant_id=tenant, name="kb")
        await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="h.md", content=_DOC)
    worker = KnowledgeIngestRecoveryWorker(
        store=store,
        embedder=_GateEmbedder(),
        interval_s=1,
        concurrency=2,
        lease_seconds=300,
        max_attempts=5,
    )
    cycle = asyncio.create_task(worker.run_once())
    await _wait_until(lambda: worker._embedder.entered >= 2)  # type: ignore[attr-defined]
    worker._embedder.release.set()  # type: ignore[attr-defined]
    assert await cycle == 2
    assert len(store.seen) == 2
    for tenant_id, ctx_tenant, bypass in store.seen:
        assert ctx_tenant == tenant_id
        assert bypass is False
    assert {row[0] for row in store.seen} == set(tenants)


@pytest.mark.asyncio
async def test_recovery_stale_after_reupload_is_silent() -> None:
    """恢复 worker 驱动途中文档被重传 → 写回被围栏拒绝,不 mark failed、不计 settled."""
    store = _RecordingStore()
    tenant, document_id = await _seed(store)
    doc = await store.get_document(tenant_id=tenant, document_id=document_id)
    assert doc is not None
    kb_id = doc.kb_id

    class _ReuploadEmbedder(FakeEmbedder):
        async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
            await store.upsert_document(
                tenant_id=tenant, kb_id=kb_id, filename="h.md", content=b"v2"
            )
            return await super().embed(texts, tenant_id=tenant_id)

    worker = KnowledgeIngestRecoveryWorker(
        store=store,
        embedder=_ReuploadEmbedder(),
        interval_s=1,
        concurrency=1,
        lease_seconds=300,
        max_attempts=1,
    )
    assert await worker.run_once() == 0
    assert store.failed_terminal == []
    fetched = await store.get_document(tenant_id=tenant, document_id=document_id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.PENDING
    assert fetched.ingest_generation == 1
