"""Retrieval log of knowledge searches (B-175 P2-PR4).

``LoggedKnowledgeRetriever`` writes one ``knowledge_retrieval_log`` row per
search that a purge can find again: an agent's ``knowledge_search`` under the
current run and its conversation, or a console retrieval test (inside
:func:`retrieval_log_scope`) as ``console_test`` under its employee — which
learns the row id and the search's own latency through the scope's receipt. A
search outside a run that nobody scoped writes nothing. Results are ids and
scores only (the document id comes on the result; logging adds no store
reads). A search that raises is logged as ``error`` and still raises. A
failing or slow log write never fails or holds up an agent's search (bounded
wait), and nothing it logs carries the query. ``dataclasses.replace`` (the
agent factory re-wraps the retriever per build) keeps the subclass and its sink.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from expert_work.common.context import (
    reset_current_run_id,
    reset_current_thread_id,
    set_current_run_id,
    set_current_thread_id,
)
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.knowledge import (
    InMemoryKnowledgeRetrievalLogStore,
    RetrievalLogEntry,
)
from expert_work.protocol import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    RetrievalMethod,
)
from orchestrator.tools import KnowledgeRetriever, KnowledgeSearchTool, ToolContext, knowledge_log
from orchestrator.tools.knowledge_log import (
    LoggedKnowledgeRetriever,
    RetrievalLogContext,
    retrieval_log_scope,
)

_QUERY_MARKER = "deductible-q7f3"
_RESULT_KEYS = {
    "chunk_id",
    "document_id",
    "parent_chunk_id",
    "chunk_index",
    "parent_index",
    "recall_source",
    "vector_score",
    "keyword_score",
    "fused_score",
    "rerank_score",
}


class _FixedEmbedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        return [(1.0, 0.0) for _ in texts]


class _BrokenEmbedder:
    """Not a vendor failure (those degrade to keywords): the search raises."""

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        msg = "embedding misconfigured"
        raise RuntimeError(msg)


class _FailingSink:
    """A log store whose write fails the way a driver error does: echoing the
    statement's parameters — the query among them."""

    def __init__(self) -> None:
        self.calls = 0

    async def add(self, entry: RetrievalLogEntry) -> None:
        self.calls += 1
        msg = f"insert failed [parameters: ({entry.query!r},)]"
        raise RuntimeError(msg)


class _SlowSink(InMemoryKnowledgeRetrievalLogStore):
    def __init__(self, delay_s: float) -> None:
        super().__init__()
        self.delay_s = delay_s

    async def add(self, entry: RetrievalLogEntry) -> None:
        await asyncio.sleep(self.delay_s)
        await super().add(entry)


class _CountingStore(InMemoryKnowledgeStore):
    """Counts the reads logging could add."""

    def __init__(self) -> None:
        super().__init__()
        self.reads: dict[str, int] = {"list_bases": 0, "get_chunks": 0, "get_documents": 0}

    async def list_bases(self, *, tenant_id: UUID) -> list[KnowledgeBase]:
        self.reads["list_bases"] += 1
        return await super().list_bases(tenant_id=tenant_id)

    async def get_chunks(
        self, *, tenant_id: UUID, chunk_ids: Sequence[UUID]
    ) -> list[KnowledgeChunk]:
        self.reads["get_chunks"] += 1
        return await super().get_chunks(tenant_id=tenant_id, chunk_ids=chunk_ids)

    async def get_documents(
        self, *, tenant_id: UUID, document_ids: Sequence[UUID]
    ) -> dict[UUID, KnowledgeDocument]:
        self.reads["get_documents"] += 1
        return await super().get_documents(tenant_id=tenant_id, document_ids=document_ids)


async def _seed(
    store: InMemoryKnowledgeStore | None = None,
) -> tuple[InMemoryKnowledgeStore, UUID, KnowledgeBase, KnowledgeDocument]:
    store = store if store is not None else InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="doc.pdf")
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=document.id,
        chunks=[
            KnowledgeChunk(
                id=uuid4(),
                tenant_id=tenant,
                kb_id=base.id,
                document_id=document.id,
                chunk_index=index,
                content=content,
                embedding=embedding,
            )
            for index, (content, embedding) in enumerate(
                (
                    (f"the {_QUERY_MARKER} is 500 dollars", (1.0, 0.0)),
                    ("vacation policy grants 20 days", (0.0, 1.0)),
                )
            )
        ],
    )
    return store, tenant, base, document


def _retriever(
    store: InMemoryKnowledgeStore, sink: object | None = None, *, embedder: object | None = None
) -> LoggedKnowledgeRetriever:
    log = InMemoryKnowledgeRetrievalLogStore() if sink is None else sink
    return LoggedKnowledgeRetriever(
        store=store,
        embedder=embedder or _FixedEmbedder(),  # type: ignore[arg-type]
        log_sink=log,  # type: ignore[arg-type]
    )


@contextmanager
def _in_run(run_id: UUID, thread_id: UUID) -> Iterator[None]:
    """What the run worker binds for a run (``sse.run_agent``)."""
    run_token = set_current_run_id(run_id)
    thread_token = set_current_thread_id(thread_id)
    try:
        yield
    finally:
        reset_current_thread_id(thread_token)
        reset_current_run_id(run_token)


async def _rows(
    retriever: LoggedKnowledgeRetriever, tenant: UUID, kb: UUID
) -> list[RetrievalLogEntry]:
    assert isinstance(retriever.log_sink, InMemoryKnowledgeRetrievalLogStore)
    rows, _total = await retriever.log_sink.list_for_base(tenant_id=tenant, kb_id=kb)
    return rows


@pytest.mark.asyncio
async def test_results_carry_their_document_id() -> None:
    store, tenant, _base, document = await _seed()
    outcome = await KnowledgeRetriever(store=store, embedder=_FixedEmbedder()).search_detailed(
        tenant_id=tenant, base_names=["kb"], query=_QUERY_MARKER, limit=3
    )
    assert outcome.results
    assert {r.document_id for r in outcome.results} == {document.id}


@pytest.mark.asyncio
async def test_an_agent_search_writes_one_row_under_its_run_and_conversation() -> None:
    store, tenant, base, document = await _seed()
    retriever = _retriever(store)
    run_id, thread_id = uuid4(), uuid4()
    with _in_run(run_id, thread_id):
        outcome = await retriever.search_detailed(
            tenant_id=tenant, base_names=["kb"], query=_QUERY_MARKER, limit=3
        )

    [row] = await _rows(retriever, tenant, base.id)
    assert (row.source, row.run_id, row.thread_id, row.actor_id) == (
        "agent",
        run_id,
        thread_id,
        None,
    )
    assert (row.kb_ids, row.query, row.status) == ((base.id,), _QUERY_MARKER, "ok")
    assert row.result_count == len(outcome.results) >= 1
    assert row.params["limit"] == 3
    assert row.flags["embedding_degraded"] is False
    assert "rerank_outcome" in row.flags
    assert row.latency_ms >= 0
    best, logged = outcome.results[0], row.results[0]
    # Ids and scores only — never the passage text.
    assert all(set(result) == _RESULT_KEYS for result in row.results)
    assert logged["chunk_id"] == str(best.chunk_id)
    assert logged["document_id"] == str(document.id)
    assert (logged["vector_score"], logged["keyword_score"]) == (
        best.vector_score,
        best.keyword_score,
    )
    assert (logged["fused_score"], logged["rerank_score"]) == (best.fused_score, best.rerank_score)


@pytest.mark.asyncio
async def test_logging_adds_no_store_reads() -> None:
    """The bases are the ones the search resolved, the document id comes on
    the result: a logged search reads the store exactly as a plain one."""
    store = _CountingStore()
    _store, tenant, _base, _document = await _seed(store)
    plain = KnowledgeRetriever(store=store, embedder=_FixedEmbedder())
    await plain.search_detailed(tenant_id=tenant, base_names=["kb"], query=_QUERY_MARKER, limit=3)
    plain_reads = dict(store.reads)
    store.reads = dict.fromkeys(store.reads, 0)

    retriever = _retriever(store)
    with _in_run(uuid4(), uuid4()):
        await retriever.search_detailed(
            tenant_id=tenant, base_names=["kb"], query=_QUERY_MARKER, limit=3
        )

    assert store.reads == plain_reads
    assert len(await _rows(retriever, tenant, _base.id)) == 1


@pytest.mark.asyncio
async def test_the_knowledge_search_tool_logs_through_its_retriever() -> None:
    store, tenant, base, _document = await _seed()
    retriever = _retriever(store)
    tool = KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",))
    with _in_run(uuid4(), uuid4()):
        await tool.call({"query": _QUERY_MARKER}, ctx=ToolContext(tenant_id=tenant))
    [row] = await _rows(retriever, tenant, base.id)
    assert row.source == "agent"


@pytest.mark.asyncio
async def test_a_search_outside_a_run_and_any_scope_writes_nothing() -> None:
    """E.g. the skill-evolution replay: no run id, nobody scoped it — a row no
    purge could ever find."""
    store, tenant, base, _document = await _seed()
    retriever = _retriever(store)
    outcome = await retriever.search_detailed(
        tenant_id=tenant, base_names=["kb"], query=_QUERY_MARKER, limit=3
    )
    assert outcome.status == "ok"
    assert await _rows(retriever, tenant, base.id) == []


@pytest.mark.asyncio
async def test_a_console_test_is_logged_with_its_actor_and_reports_back() -> None:
    store, tenant, base, _document = await _seed()
    retriever = _retriever(store)
    context = RetrievalLogContext(source="console_test", actor_id="employee-7")
    with retrieval_log_scope(context) as receipt:
        await retriever.search_detailed(
            tenant_id=tenant, base_names=["kb"], query=_QUERY_MARKER, limit=3
        )

    [row] = await _rows(retriever, tenant, base.id)
    assert (row.source, row.actor_id, row.run_id, row.thread_id) == (
        "console_test",
        "employee-7",
        None,
        None,
    )
    assert receipt.log_id == row.id
    assert receipt.latency_ms == row.latency_ms


@pytest.mark.asyncio
async def test_a_search_that_finds_nothing_is_logged_with_zero_results() -> None:
    store, tenant, base, _document = await _seed()
    retriever = _retriever(store)
    with _in_run(uuid4(), uuid4()):
        outcome = await retriever.search_detailed(
            tenant_id=tenant,
            base_names=["kb"],
            query="zzz",
            limit=3,
            method=RetrievalMethod.KEYWORD,
        )
    assert outcome.status == "no_hits"
    [row] = await _rows(retriever, tenant, base.id)
    assert (row.status, row.result_count, row.results) == ("no_hits", 0, ())
    assert row.params["method"] == "keyword"


@pytest.mark.asyncio
async def test_a_search_that_raises_is_logged_as_an_error_and_still_raises() -> None:
    store, tenant, base, _document = await _seed()
    retriever = _retriever(store, embedder=_BrokenEmbedder())
    with _in_run(uuid4(), uuid4()), pytest.raises(RuntimeError, match="misconfigured"):
        await retriever.search_detailed(
            tenant_id=tenant, base_names=["kb"], query=_QUERY_MARKER, limit=3
        )
    [row] = await _rows(retriever, tenant, base.id)
    assert (row.status, row.results, row.flags) == ("error", (), {"error": "RuntimeError"})

    # A failing log write does not replace the search's own exception.
    failing = _retriever(store, _FailingSink(), embedder=_BrokenEmbedder())
    with _in_run(uuid4(), uuid4()), pytest.raises(RuntimeError, match="misconfigured"):
        await failing.search_detailed(
            tenant_id=tenant, base_names=["kb"], query=_QUERY_MARKER, limit=3
        )


@pytest.mark.asyncio
async def test_no_row_when_no_named_base_exists() -> None:
    store, tenant, _base, _document = await _seed()
    sink = _FailingSink()
    retriever = _retriever(store, sink)
    with _in_run(uuid4(), uuid4()):
        outcome = await retriever.search_detailed(
            tenant_id=tenant, base_names=["ghost"], query=_QUERY_MARKER, limit=3
        )
    assert outcome.status == "no_bases"
    assert sink.calls == 0


@pytest.mark.asyncio
async def test_a_failing_log_never_fails_the_search_nor_logs_the_query(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, tenant, _base, _document = await _seed()
    sink = _FailingSink()
    plain = KnowledgeRetriever(store=store, embedder=_FixedEmbedder())
    expected = await plain.search_detailed(
        tenant_id=tenant, base_names=["kb"], query=_QUERY_MARKER, limit=3
    )

    with caplog.at_level(logging.DEBUG), _in_run(uuid4(), uuid4()):
        outcome = await _retriever(store, sink).search_detailed(
            tenant_id=tenant, base_names=["kb"], query=_QUERY_MARKER, limit=3
        )

    assert sink.calls == 1
    assert outcome == expected
    assert any("knowledge.retrieval_log_failed" in r.getMessage() for r in caplog.records)
    for record in caplog.records:
        assert _QUERY_MARKER not in record.getMessage()
        assert _QUERY_MARKER not in (record.exc_text or "")
        assert record.exc_info is None
    assert _QUERY_MARKER not in caplog.text


class _StuckSink:
    """A write stuck on the database — and, like an ``AsyncSession`` closing
    under a shield, one that does not stop when cancelled: it finishes only
    once ``release`` is set."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.written: list[RetrievalLogEntry] = []

    async def add(self, entry: RetrievalLogEntry) -> None:
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            await self.release.wait()
            raise
        self.written.append(entry)


class _OrphanSink:
    """A write waiting on a future nothing else holds: only a strong reference
    to its task keeps it from being garbage-collected mid-write."""

    async def add(self, entry: RetrievalLogEntry) -> None:
        await asyncio.get_running_loop().create_future()


@pytest.mark.asyncio
async def test_a_stuck_log_write_does_not_hold_up_an_agent_search(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The search returns within the cap while the write is still blocked —
    the write is not cancelled (cancelling would wait for its cleanup) but
    finishes in the background."""
    monkeypatch.setattr(knowledge_log, "AGENT_LOG_TIMEOUT_S", 0.05)
    store, tenant, _base, _document = await _seed()
    # Warm up (the keyword tokenizer loads its dictionary on first use), so the
    # timing below is the search plus the wait for its log write.
    await KnowledgeRetriever(store=store, embedder=_FixedEmbedder()).search_detailed(
        tenant_id=tenant, base_names=["kb"], query=_QUERY_MARKER, limit=3
    )
    sink = _StuckSink()
    retriever = _retriever(store, sink)
    asyncio.get_running_loop().call_later(1.0, sink.release.set)
    started = time.monotonic()
    with caplog.at_level(logging.DEBUG), _in_run(uuid4(), uuid4()):
        outcome = await retriever.search_detailed(
            tenant_id=tenant, base_names=["kb"], query=_QUERY_MARKER, limit=3
        )
    assert time.monotonic() - started < 0.5
    assert outcome.status == "ok"
    assert sink.written == []  # still blocked

    await asyncio.wait_for(sink.release.wait(), timeout=2.0)
    for _ in range(50):
        if sink.written:
            break
        await asyncio.sleep(0.01)
    assert [entry.query for entry in sink.written] == [_QUERY_MARKER]
    await asyncio.sleep(0)  # the done-callback runs on the next turn
    assert not knowledge_log._PENDING_WRITES  # a finished write is let go
    assert _QUERY_MARKER not in caplog.text


@pytest.mark.asyncio
async def test_a_pending_log_write_is_not_garbage_collected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(knowledge_log, "AGENT_LOG_TIMEOUT_S", 0.05)
    store, tenant, _base, _document = await _seed()
    retriever = _retriever(store, _OrphanSink())
    with _in_run(uuid4(), uuid4()):
        await retriever.search_detailed(
            tenant_id=tenant, base_names=["kb"], query=_QUERY_MARKER, limit=3
        )
    gc.collect()
    pending = [t for t in asyncio.all_tasks() if "_OrphanSink.add" in repr(t.get_coro())]
    assert len(pending) == 1
    pending[0].cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await pending[0]


@pytest.mark.asyncio
async def test_a_console_test_waits_for_its_log_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The employee asked for the row (``log_id``): no agent-path bound here."""
    monkeypatch.setattr(knowledge_log, "AGENT_LOG_TIMEOUT_S", 0.05)
    store, tenant, base, _document = await _seed()
    retriever = _retriever(store, _SlowSink(delay_s=0.2))
    with retrieval_log_scope(RetrievalLogContext(source="console_test")) as receipt:
        await retriever.search_detailed(
            tenant_id=tenant, base_names=["kb"], query=_QUERY_MARKER, limit=3
        )
    [row] = await _rows(retriever, tenant, base.id)
    assert receipt.log_id == row.id


def test_replace_keeps_the_subclass_and_its_sink() -> None:
    sink = InMemoryKnowledgeRetrievalLogStore()
    retriever = _retriever(InMemoryKnowledgeStore(), sink)
    rewrapped = replace(retriever, embedder=_FixedEmbedder(), reranker=None)
    assert isinstance(rewrapped, LoggedKnowledgeRetriever)
    assert rewrapped.log_sink is sink
