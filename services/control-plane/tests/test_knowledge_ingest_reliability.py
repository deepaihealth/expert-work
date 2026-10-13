"""L2-PR2 入库可靠性 —— 运行器侧:代际围栏 / 嵌入退避 / 并发闸 / 续约 / 超长块 / 预解析."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest

from control_plane.knowledge import ingestion
from control_plane.knowledge.chunking import count_tokens
from control_plane.knowledge.ingestion import (
    KnowledgeIngestionRunner,
    StaleIngestionError,
    embed_with_backoff,
    ingest_document_bytes,
    shutdown_parse_pool,
)
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import DocumentStatus
from orchestrator.llm import FakeEmbedder

_TENANT = uuid4()
_DOC = b"# Handbook\n\nThe deductible is 500 dollars per year."


async def _seed(store: InMemoryKnowledgeStore, *, content: bytes = _DOC) -> tuple[UUID, UUID]:
    base = await store.create_base(tenant_id=_TENANT, name=f"kb-{uuid4().hex[:6]}")
    doc = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="h.md", content=content, content_sha256="s"
    )
    return base.id, doc.id


def _submit(runner: KnowledgeIngestionRunner, kb_id: UUID, doc_id: UUID) -> asyncio.Task[None]:
    del kb_id  # the runner reads everything but the identity from the claim
    return runner.submit(tenant_id=_TENANT, document_id=doc_id)


@pytest.fixture(scope="module", autouse=True)
def _parse_pool_teardown() -> Iterator[None]:
    yield
    shutdown_parse_pool()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Backoff sleeps are recorded, not slept."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(ingestion, "_sleep", fake_sleep)
    return slept


# ---------------------------------------------------------------------------
# 代际围栏
# ---------------------------------------------------------------------------


class _FlakyEmbedder:
    """Fails the first ``failures`` calls with ``exc``, then behaves like FakeEmbedder."""

    def __init__(self, exc: Exception, failures: int) -> None:
        self._exc = exc
        self._remaining = failures
        self.calls = 0
        self._inner = FakeEmbedder()

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise self._exc
        return await self._inner.embed(texts, tenant_id=tenant_id)


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://embed.test/v1/embeddings")
    response = httpx.Response(code, request=request, text="throttled")
    return httpx.HTTPStatusError(
        f"embeddings request failed: {code}", request=request, response=response
    )


class _GateEmbedder(FakeEmbedder):
    """Blocks inside ``embed`` until released — lets a test re-upload mid-ingest."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.entered.set()
        await self.release.wait()
        return await super().embed(texts, tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_queued_task_ingests_the_bytes_current_at_claim_time() -> None:
    """评审 Critical:任务排在并发闸外时文档被重传 → 它认领到新代际,必须入库新字节.

    concurrency=1:先用一个卡住的文档占住闸,再提交目标文档 v1,排队期间重传 v2。
    排队任务拿到闸后认领的是 gen=1,入库的必须是 v2 的内容。"""
    store = InMemoryKnowledgeStore()
    gate = _GateEmbedder()
    runner = KnowledgeIngestionRunner(store=store, embedder=gate, concurrency=1)
    blocker_kb, blocker = await _seed(store)
    blocking = _submit(runner, blocker_kb, blocker)
    await asyncio.wait_for(gate.entered.wait(), timeout=5)
    # Target document: v1 submitted while the gate is held → queued.
    kb_id, doc_id = await _seed(store, content=b"# T\n\nVERSION ONE content.")
    queued = _submit(runner, kb_id, doc_id)
    await asyncio.sleep(0.05)
    reset = await store.upsert_document(
        tenant_id=_TENANT,
        kb_id=kb_id,
        filename="h.md",
        content=b"# T\n\nVERSION TWO content.",
        content_sha256="s2",
    )
    assert reset.ingest_generation == 1
    gate.release.set()
    await asyncio.gather(blocking, queued)
    fetched = await store.get_document(tenant_id=_TENANT, document_id=doc_id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.READY
    chunks, _ = await store.list_chunks(tenant_id=_TENANT, document_id=doc_id)
    assert "VERSION TWO" in chunks[0].content
    assert "VERSION ONE" not in chunks[0].content


@pytest.mark.asyncio
async def test_claim_without_retained_bytes_fails_terminally() -> None:
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    doc = await store.upsert_document(tenant_id=_TENANT, kb_id=base.id, filename="h.md")
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder())
    await runner.submit(tenant_id=_TENANT, document_id=doc.id)
    fetched = await store.get_document(tenant_id=_TENANT, document_id=doc.id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.FAILED
    assert fetched.error == "original file not retained; re-upload required"


class _ClaimExplodesStore(InMemoryKnowledgeStore):
    async def claim_document(self, **kwargs: object) -> None:
        raise RuntimeError("db hiccup")


@pytest.mark.asyncio
async def test_failure_before_claim_writes_nothing() -> None:
    """认领之前出错(没有代际)不能写无围栏的 FAILED —— 文档留给恢复 worker."""
    store = _ClaimExplodesStore()
    kb_id, doc_id = await _seed(store)
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder())
    await _submit(runner, kb_id, doc_id)
    fetched = await store.get_document(tenant_id=_TENANT, document_id=doc_id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.PENDING
    assert fetched.error is None


@pytest.mark.asyncio
async def test_stale_worker_after_reupload_does_not_overwrite() -> None:
    """worker A 入库途中文档被重传(gen+1)→ A 的写回被围栏拒绝,文档留给新代际."""
    store = InMemoryKnowledgeStore()
    kb_id, doc_id = await _seed(store)
    gate = _GateEmbedder()
    runner = KnowledgeIngestionRunner(store=store, embedder=gate)
    task = _submit(runner, kb_id, doc_id)
    await asyncio.wait_for(gate.entered.wait(), timeout=5)
    # Re-upload while A is mid-embed: generation bumps, status back to pending.
    reset = await store.upsert_document(
        tenant_id=_TENANT, kb_id=kb_id, filename="h.md", content=b"# v2\n\nnew", content_sha256="s2"
    )
    assert reset.ingest_generation == 1
    gate.release.set()
    await task
    fetched = await store.get_document(tenant_id=_TENANT, document_id=doc_id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.PENDING  # A neither readied nor failed it
    assert fetched.chunk_count == 0
    assert fetched.error is None
    _, total = await store.list_chunks(tenant_id=_TENANT, document_id=doc_id)
    assert total == 0


@pytest.mark.asyncio
async def test_stale_worker_failure_does_not_mark_failed() -> None:
    """围栏同样挡住失败写回:旧代际 worker 的异常不能把新代际的文档置 failed."""
    store = InMemoryKnowledgeStore()
    kb_id, doc_id = await _seed(store)

    class _ExplodeAfterReupload(FakeEmbedder):
        async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
            await store.upsert_document(
                tenant_id=_TENANT, kb_id=kb_id, filename="h.md", content=b"v2", content_sha256="s2"
            )
            raise RuntimeError("boom from the old generation")

    runner = KnowledgeIngestionRunner(store=store, embedder=_ExplodeAfterReupload())
    await _submit(runner, kb_id, doc_id)
    fetched = await store.get_document(tenant_id=_TENANT, document_id=doc_id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.PENDING
    assert fetched.error is None


@pytest.mark.asyncio
async def test_ingest_document_bytes_raises_stale_when_fence_rejects() -> None:
    store = InMemoryKnowledgeStore()
    kb_id, doc_id = await _seed(store)
    # Nobody claimed it: status is pending, so a fenced write-back must fail.
    with pytest.raises(StaleIngestionError):
        await ingest_document_bytes(
            store=store,
            embedder=FakeEmbedder(),
            tenant_id=_TENANT,
            document_id=doc_id,
            kb_id=kb_id,
            filename="h.md",
            raw=_DOC,
            chunk_max_tokens=512,
            chunk_overlap_tokens=64,
            generation=0,
        )


# ---------------------------------------------------------------------------
# 嵌入退避
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_backoff_recovers_after_two_429(_no_sleep: list[float]) -> None:
    embedder = _FlakyEmbedder(_status_error(429), failures=2)
    vectors = await embed_with_backoff(embedder, ["a", "b"], tenant_id=_TENANT)
    assert len(vectors) == 2
    assert embedder.calls == 3
    assert _no_sleep == [1.0, 2.0]


@pytest.mark.asyncio
async def test_embed_backoff_gives_up_after_three_retries(_no_sleep: list[float]) -> None:
    embedder = _FlakyEmbedder(_status_error(503), failures=10)
    with pytest.raises(httpx.HTTPStatusError, match="503"):
        await embed_with_backoff(embedder, ["a"], tenant_id=_TENANT)
    assert embedder.calls == 4  # 1 + 3 retries
    assert _no_sleep == [1.0, 2.0, 4.0]


@pytest.mark.asyncio
async def test_embed_backoff_does_not_retry_client_errors(_no_sleep: list[float]) -> None:
    embedder = _FlakyEmbedder(_status_error(400), failures=10)
    with pytest.raises(httpx.HTTPStatusError, match="400"):
        await embed_with_backoff(embedder, ["a"], tenant_id=_TENANT)
    assert embedder.calls == 1
    assert _no_sleep == []


@pytest.mark.asyncio
async def test_embed_backoff_retries_timeouts(_no_sleep: list[float]) -> None:
    embedder = _FlakyEmbedder(httpx.ReadTimeout("slow"), failures=1)
    assert len(await embed_with_backoff(embedder, ["a"], tenant_id=_TENANT)) == 1
    assert _no_sleep == [1.0]


@pytest.mark.asyncio
async def test_embed_backoff_retries_transport_errors(_no_sleep: list[float]) -> None:
    embedder = _FlakyEmbedder(httpx.ConnectError("reset"), failures=1)
    assert len(await embed_with_backoff(embedder, ["a"], tenant_id=_TENANT)) == 1
    assert _no_sleep == [1.0]


class _SliceRecordingEmbedder(FakeEmbedder):
    """Fails the Nth request once; records every request's size."""

    def __init__(self, fail_request: int) -> None:
        self.requests: list[int] = []
        self._fail_request = fail_request

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.requests.append(len(texts))
        if len(self.requests) == self._fail_request:
            raise _status_error(429)
        return await super().embed(texts, tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_embed_backoff_retries_only_the_failed_slice(_no_sleep: list[float]) -> None:
    """1000 段 = 100 个请求,第 90 个 429 → 只重发第 90 个,不从头来."""
    embedder = _SliceRecordingEmbedder(fail_request=3)
    vectors = await embed_with_backoff(embedder, [f"t{i}" for i in range(25)], tenant_id=_TENANT)
    assert len(vectors) == 25
    assert embedder.requests == [10, 10, 5, 5]  # slice 3 (5 texts) sent twice, slices 1-2 once
    assert _no_sleep == [1.0]


class _FlakyFirstCallEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.calls += 1
        if self.calls == 1:
            raise _status_error(429)
        return await super().embed(texts, tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_semantic_chunking_embed_also_backs_off() -> None:
    """语义切块那一路(多块文档先嵌块)也走退避:第一次嵌入 429 仍然 READY."""
    store = InMemoryKnowledgeStore()
    two_blocks = b"# H\n\nFirst paragraph about deductibles.\n\nSecond paragraph about premiums."
    kb_id, doc_id = await _seed(store, content=two_blocks)
    embedder = _FlakyFirstCallEmbedder()
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    await _submit(runner, kb_id, doc_id)
    fetched = await store.get_document(tenant_id=_TENANT, document_id=doc_id)
    assert fetched is not None and fetched.status is DocumentStatus.READY
    assert embedder.calls >= 3  # block embed (failed + retried) + chunk embed


@pytest.mark.asyncio
async def test_runner_ready_after_transient_429_and_failed_after_persistent() -> None:
    store = InMemoryKnowledgeStore()
    kb_id, ok_doc = await _seed(store)
    runner = KnowledgeIngestionRunner(store=store, embedder=_FlakyEmbedder(_status_error(429), 2))
    await _submit(runner, kb_id, ok_doc)
    fetched = await store.get_document(tenant_id=_TENANT, document_id=ok_doc)
    assert fetched is not None and fetched.status is DocumentStatus.READY

    kb2, bad_doc = await _seed(store)
    runner = KnowledgeIngestionRunner(store=store, embedder=_FlakyEmbedder(_status_error(429), 99))
    await _submit(runner, kb2, bad_doc)
    fetched = await store.get_document(tenant_id=_TENANT, document_id=bad_doc)
    assert fetched is not None
    assert fetched.status is DocumentStatus.FAILED
    assert fetched.error is not None and "429" in fetched.error


# ---------------------------------------------------------------------------
# 并发闸 + 续约
# ---------------------------------------------------------------------------


class _ParkingEmbedder(FakeEmbedder):
    """Every ``embed`` call parks until released — the number parked at once
    is the number of documents in flight, independent of scheduling noise."""

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
async def test_runner_bounds_concurrent_ingests() -> None:
    store = InMemoryKnowledgeStore()
    embedder = _ParkingEmbedder()
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder, concurrency=2)
    pairs = [await _seed(store) for _ in range(6)]
    for kb_id, doc_id in pairs:
        _submit(runner, kb_id, doc_id)
    await _wait_until(lambda: embedder.entered >= 2)
    await asyncio.sleep(0.2)
    assert embedder.entered == 2  # the semaphore holds the other four back
    embedder.release.set()
    await runner.drain()
    assert embedder.entered == 6
    for _, doc_id in pairs:
        fetched = await store.get_document(tenant_id=_TENANT, document_id=doc_id)
        assert fetched is not None and fetched.status is DocumentStatus.READY


class _RenewRecordingStore(InMemoryKnowledgeStore):
    def __init__(self) -> None:
        super().__init__()
        self.renewals: list[int] = []

    async def renew_lease(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        generation: int,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        self.renewals.append(generation)
        return await super().renew_lease(
            tenant_id=tenant_id,
            document_id=document_id,
            generation=generation,
            now=now,
            lease_seconds=lease_seconds,
        )


class _WaitForRenewalsEmbedder(FakeEmbedder):
    """Holds the embed until the store has seen ``wanted`` renewals."""

    def __init__(self, store: _RenewRecordingStore, wanted: int) -> None:
        self._store = store
        self._wanted = wanted

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        await _wait_until(lambda: len(self._store.renewals) >= self._wanted)
        return await super().embed(texts, tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_runner_renews_lease_while_processing() -> None:
    store = _RenewRecordingStore()
    kb_id, doc_id = await _seed(store)
    # lease 1 s → renew every 1/3 s; the embed waits for two renewals.
    runner = KnowledgeIngestionRunner(
        store=store, embedder=_WaitForRenewalsEmbedder(store, 2), lease_seconds=1
    )
    await _submit(runner, kb_id, doc_id)
    assert len(store.renewals) >= 2
    assert set(store.renewals) == {0}
    fetched = await store.get_document(tenant_id=_TENANT, document_id=doc_id)
    assert fetched is not None and fetched.status is DocumentStatus.READY
    # No renewal task left running after the document settled.
    before = len(store.renewals)
    await asyncio.sleep(0.5)
    assert len(store.renewals) == before


class _RefuseRenewalStore(_RenewRecordingStore):
    async def renew_lease(self, **kwargs: object) -> bool:
        self.renewals.append(-1)
        return False


class _NeverFinishEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        self.cancelled = False

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return await super().embed(texts, tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_refused_renewal_cancels_the_ingest() -> None:
    """续约被拒 = 文档已不归自己 → 取消本次入库,不再占着并发闸."""
    store = _RefuseRenewalStore()
    kb_id, doc_id = await _seed(store)
    embedder = _NeverFinishEmbedder()
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder, lease_seconds=1)
    task = _submit(runner, kb_id, doc_id)
    await asyncio.wait_for(task, timeout=5)  # would hang for an hour without the cancel
    assert embedder.cancelled
    fetched = await store.get_document(tenant_id=_TENANT, document_id=doc_id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.PROCESSING  # nothing written by the loser


# ---------------------------------------------------------------------------
# 超长块 / 预解析
# ---------------------------------------------------------------------------


class _RecordingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        self.inputs: list[str] = []

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.inputs.extend(texts)
        return await super().embed(texts, tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_oversized_table_block_is_split_before_embedding() -> None:
    rows = "\n".join(f"| 产品{i} | 规格说明文字{i} | 价格 {i} 元 |" for i in range(1500))
    table = "| 名称 | 规格 | 价格 |\n|---|---|---|\n" + rows
    # Two blocks in one section, so the semantic chunker really embeds the
    # blocks for topic detection (a single block skips that step).
    markdown = "# 价目表\n\n以下为全部产品的规格与价格。\n\n" + table
    assert len(table) > 20_000
    store = InMemoryKnowledgeStore()
    kb_id, doc_id = await _seed(store)
    embedder = _RecordingEmbedder()
    claim = await store.claim_document(
        tenant_id=_TENANT,
        document_id=doc_id,
        now=datetime.now(UTC),
        lease_seconds=60,
        max_attempts=5,
    )
    assert claim is not None
    count = await ingest_document_bytes(
        store=store,
        embedder=embedder,
        tenant_id=_TENANT,
        document_id=doc_id,
        kb_id=kb_id,
        filename="t.md",
        raw=markdown.encode("utf-8"),
        chunk_max_tokens=512,
        chunk_overlap_tokens=64,
        generation=claim.generation,
    )
    assert count > 1
    assert embedder.inputs
    # Every embedding input (block embeddings for topic detection *and* the
    # chunk texts) is bounded: body cap + carried overlap + heading prefix —
    # never the 30k-token whole table.
    assert max(count_tokens(text) for text in embedder.inputs) <= 512 + 64 + 16


@pytest.mark.asyncio
async def test_parsed_markdown_skips_parsing() -> None:
    store = InMemoryKnowledgeStore()
    kb_id, doc_id = await _seed(store)
    count = await ingest_document_bytes(
        store=store,
        embedder=FakeEmbedder(),
        tenant_id=_TENANT,
        document_id=doc_id,
        kb_id=kb_id,
        filename="scan.pdf",  # bytes are not a PDF — parsing would fail
        raw=b"not a pdf",
        chunk_max_tokens=512,
        chunk_overlap_tokens=64,
        parsed_markdown="# OCR\n\n识别出来的正文。",
    )
    assert count == 1
    chunks, _ = await store.list_chunks(tenant_id=_TENANT, document_id=doc_id)
    assert "识别出来的正文" in chunks[0].content


class _FakeProcess:
    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


class _NeverPool:
    """Executor double whose parses never return; carries fake workers."""

    def __init__(self) -> None:
        self._processes = {1: _FakeProcess(), 2: _FakeProcess()}
        self.shutdown_calls: list[tuple[bool, bool]] = []

    def submit(self, fn: object, *args: object) -> asyncio.Future[str]:
        return asyncio.get_running_loop().create_future()  # never resolves

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        self.shutdown_calls.append((wait, cancel_futures))


@pytest.mark.asyncio
async def test_parse_timeout_fails_document_and_kills_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingestion, "_parse_timeout_s", 0.05)
    wedged = _NeverPool()
    monkeypatch.setattr(ingestion, "_parse_executor", wedged)
    store = InMemoryKnowledgeStore()
    kb_id, doc_id = await _seed(store)
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder())
    await _submit(runner, kb_id, doc_id)
    fetched = await store.get_document(tenant_id=_TENANT, document_id=doc_id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.FAILED
    assert fetched.error is not None and "timed out" in fetched.error
    # The wedged workers were killed and the pool dropped for rebuild.
    assert all(p.terminated for p in wedged._processes.values())
    assert wedged.shutdown_calls == [(False, True)]
    assert cast(object, ingestion._parse_executor) is not wedged


class _BrokenOncePool(_NeverPool):
    """First submit raises BrokenProcessPool (a worker died); the rebuilt real
    pool then handles the retry."""

    def __init__(self) -> None:
        super().__init__()
        self.submits = 0

    def submit(self, fn: object, *args: object) -> asyncio.Future[str]:
        self.submits += 1
        from concurrent.futures.process import BrokenProcessPool

        raise BrokenProcessPool("a child process terminated abruptly")


@pytest.mark.asyncio
async def test_broken_pool_is_rebuilt_and_the_parse_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = _BrokenOncePool()
    monkeypatch.setattr(ingestion, "_parse_executor", broken)
    store = InMemoryKnowledgeStore()
    kb_id, doc_id = await _seed(store)
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder())
    await _submit(runner, kb_id, doc_id)
    fetched = await store.get_document(tenant_id=_TENANT, document_id=doc_id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.READY, fetched.error
    assert broken.submits == 1
    assert cast(object, ingestion._parse_executor) is not broken  # rebuilt, not the broken one


@pytest.mark.asyncio
async def test_concurrent_pdf_ingests_all_ready() -> None:
    """四份 PDF 并发入库全部 READY(解析走进程池,不再多线程跑 PyMuPDF)."""
    import pymupdf

    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "The deductible is 500 dollars per year. " * 20)
    raw = pdf.tobytes()
    pdf.close()
    store = InMemoryKnowledgeStore()
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder(), concurrency=4)
    pairs = []
    for _ in range(4):
        base = await store.create_base(tenant_id=_TENANT, name=f"kb-{uuid4().hex[:6]}")
        doc = await store.upsert_document(
            tenant_id=_TENANT, kb_id=base.id, filename="h.pdf", content=raw, content_sha256="p"
        )
        pairs.append((base.id, doc.id))
    for _, doc_id in pairs:
        runner.submit(tenant_id=_TENANT, document_id=doc_id)
    await runner.drain()
    for _, doc_id in pairs:
        fetched = await store.get_document(tenant_id=_TENANT, document_id=doc_id)
        assert fetched is not None
        assert fetched.status is DocumentStatus.READY, fetched.error
        assert fetched.chunk_count >= 1
