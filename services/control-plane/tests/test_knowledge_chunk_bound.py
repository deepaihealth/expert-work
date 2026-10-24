"""B-214 —— the server's memory for an ingest follows the document's chunks.

10-23 test env: a 2.4M-character Word document of 240-character paragraphs
made 9,917 chunks and took the server +616 MiB (~64 KiB a chunk: a 1,024-float
vector as Python objects, copied into the chunk, then into an ORM row). The
character cap cannot bound that — a heading breaks a chunk however short it
is, and 2.5M characters of short headed entries are ~58,000 chunks. So:

* vectors are kept compact and chunks embedded a window at a time
  (``CHUNK_EMBED_WINDOW``), chunk objects built only as the store reads them
  (``chunk_rows.ChunkRows``), rows written in batches — ~11 KiB a chunk;
* a per-document chunk cap (``knowledge_document_max_chunks``), checked on the
  structural chunks before anything is embedded and on the semantic chunks
  before any chunk is embedded;
* the ingest budget weighs a document by its chunks when they cost more than
  its characters.
"""

from __future__ import annotations

import pickle
from array import array
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest

from control_plane.knowledge import ingestion, parsed_size
from control_plane.knowledge.chunk_rows import ChunkRows, compact
from control_plane.knowledge.chunking import TextChunk
from control_plane.knowledge.ingestion import (
    CHUNK_EMBED_WINDOW,
    KnowledgeIngestionRunner,
    content_hash,
    ingest_markdown,
)
from control_plane.knowledge.parsed_size import (
    DEFAULT_MAX_CHUNKS,
    DEFAULT_MAX_PARSED_CHARS,
    TOO_MANY_CHUNKS_PATTERN,
    DocumentTooManyChunksError,
    admission_weight,
    configure_parsed_size,
    too_many_chunks_message,
)
from control_plane.knowledge.recovery import KnowledgeIngestRecoveryWorker
from control_plane.settings import Settings
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    DocumentStatus,
    IngestErrorCode,
    chunk_retrieval_text,
)
from orchestrator.llm import FakeEmbedder

_TENANT = uuid4()


@pytest.fixture(autouse=True)
def _default_caps() -> Iterator[None]:
    yield
    configure_parsed_size(max_chars=DEFAULT_MAX_PARSED_CHARS, max_chunks=DEFAULT_MAX_CHUNKS)


class _RecordingEmbedder:
    """:class:`FakeEmbedder` vectors; records every call's texts."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._inner = FakeEmbedder()

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.calls.append(list(texts))
        return await self._inner.embed(texts, tenant_id=tenant_id)

    @property
    def texts(self) -> list[str]:
        return [text for call in self.calls for text in call]


def _entries(count: int) -> str:
    """Short headed entries — every heading opens a chunk of its own."""
    return "\n\n".join(f"## 条目 {i}\n\n第 {i} 条的说明文字。" for i in range(count))


async def _document(store: InMemoryKnowledgeStore, markdown: str) -> tuple[UUID, UUID]:
    base = await store.create_base(tenant_id=_TENANT, name=f"kb-{uuid4().hex[:6]}")
    doc = await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="entries.md", content=markdown.encode()
    )
    return base.id, doc.id


async def _ingest(
    store: InMemoryKnowledgeStore, embedder: object, markdown: str, **kw: object
) -> tuple[UUID, int]:
    kb_id, doc_id = await _document(store, markdown)
    count = await ingest_markdown(
        store=store,
        embedder=embedder,  # type: ignore[arg-type]
        tenant_id=_TENANT,
        document_id=doc_id,
        kb_id=kb_id,
        markdown=markdown,
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=DEFAULT_CHUNK_OVERLAP_TOKENS,
        **kw,  # type: ignore[arg-type]
    )
    return doc_id, count


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------


def test_the_default_is_the_settings_default() -> None:
    assert Settings().knowledge_document_max_chunks == DEFAULT_MAX_CHUNKS


def test_the_refusal_says_how_many_chunks_the_limit_and_to_split() -> None:
    message = too_many_chunks_message(58_140, 40_000)
    # The console renders it from these two numbers (documentError.ts pins this text).
    assert message == (
        "too many chunks: 58,140 chunks, over the per-document limit of 40,000; "
        "split the document into smaller files and upload them separately"
    )
    match = TOO_MANY_CHUNKS_PATTERN.search(message)
    assert match is not None and match.groups() == ("58,140", "40,000")
    # Raised where the semantic chunks are counted, in the server — but it is a
    # DocumentParseError like the worker-side refusal, so it must pickle the same.
    refusal = pickle.loads(pickle.dumps(DocumentTooManyChunksError(58_140, 40_000)))  # noqa: S301
    assert (refusal.chunks, refusal.limit, refusal.code) == (
        58_140,
        40_000,
        IngestErrorCode.TOO_MANY_CHUNKS,
    )


async def test_too_many_structural_chunks_fail_before_anything_is_embedded() -> None:
    configure_parsed_size(max_chars=DEFAULT_MAX_PARSED_CHARS, max_chunks=50)
    store = InMemoryKnowledgeStore()
    _, doc_id = await _document(store, _entries(60))
    embedder = _RecordingEmbedder()
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder, concurrency=2)  # type: ignore[arg-type]
    runner.submit(tenant_id=_TENANT, document_id=doc_id)
    await runner.drain()

    doc = await store.get_document(tenant_id=_TENANT, document_id=doc_id)
    assert doc is not None
    assert (doc.status, doc.error_code, doc.attempts) == (
        DocumentStatus.FAILED,
        IngestErrorCode.TOO_MANY_CHUNKS,
        1,
    )
    assert doc.error == too_many_chunks_message(60, 50)
    assert embedder.calls == []  # not one block, not one chunk: nothing billed
    worker = KnowledgeIngestRecoveryWorker(
        store=store,
        embedder=FakeEmbedder(),
        interval_s=1,
        concurrency=2,
        lease_seconds=300,
        max_attempts=5,
    )
    assert await worker.run_once() == 0  # deterministic: never retried


class _TopicEmbedder(_RecordingEmbedder):
    """``[1, 0]`` for an alpha text, ``[0, 1]`` otherwise — alternating
    paragraphs are a topic shift each."""

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        self.calls.append(list(texts))
        return [(1.0, 0.0) if "alpha" in text else (0.0, 1.0) for text in texts]


def _alternating(count: int) -> str:
    sentence = "a paragraph long enough to stand as a chunk of its own, past the floor"
    return "\n\n".join(
        f"{'alpha' if i % 2 == 0 else 'beta'} {i}: {sentence} {'word ' * 30}" for i in range(count)
    )


async def test_without_semantic_breaks_only_the_chunks_are_embedded() -> None:
    # B-206 K-3 default: topic shifts do not split, so no block is embedded to
    # find them — every text sent is a chunk's (they open with the title).
    embedder = _TopicEmbedder()
    _, chunks = await _ingest(
        InMemoryKnowledgeStore(), embedder, _alternating(12), doc_title="report"
    )
    assert chunks < 12  # paragraphs pack by size, not one per topic
    assert len(embedder.texts) == chunks
    assert all(text.startswith("[Document: report]") for text in embedder.texts)


@pytest.mark.usefixtures("semantic_breaks")
async def test_topic_shifts_past_the_cap_are_refused_before_any_chunk_is_embedded() -> None:
    markdown = _alternating(12)
    store = InMemoryKnowledgeStore()
    probe = _TopicEmbedder()
    _, chunks = await _ingest(store, probe, markdown)
    assert chunks == 12  # a shift at every paragraph
    configure_parsed_size(max_chars=DEFAULT_MAX_PARSED_CHARS, max_chunks=11)
    embedder = _TopicEmbedder()
    with pytest.raises(DocumentTooManyChunksError) as caught:
        await _ingest(InMemoryKnowledgeStore(), embedder, markdown, doc_title="report")
    assert (caught.value.chunks, caught.value.limit) == (12, 11)
    # Only the blocks were embedded (topic detection); no chunk text — which
    # would start with the document title — was sent.
    assert embedder.texts and not any(text.startswith("[Document: ") for text in embedder.texts)


class _Pinned(FakeEmbedder):
    """A fixed embedding model, so the store keeps (and can return) its vectors by model."""

    model_key = "fake/v1"


async def test_a_document_under_the_cap_keeps_exactly_the_vectors_it_was_given() -> None:
    store = InMemoryKnowledgeStore()
    doc_id, count = await _ingest(store, _Pinned(), _entries(30), doc_title="manual")
    stored, total = await store.list_chunks(tenant_id=_TENANT, document_id=doc_id, limit=100)
    assert count == total == 30
    texts = [chunk_retrieval_text(c.heading_path, c.content, c.doc_title) for c in stored]
    vectors = await store.chunk_embeddings_by_hash(
        tenant_id=_TENANT,
        document_id=doc_id,
        content_hashes=[content_hash(text) for text in texts],
        embedding_model="fake/v1",
    )
    expected = await FakeEmbedder().embed(texts, tenant_id=_TENANT)
    assert [vectors[content_hash(text)] for text in texts] == expected  # bit for bit


async def test_chunks_are_embedded_a_window_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingestion, "CHUNK_EMBED_WINDOW", 7)
    embedder = _RecordingEmbedder()
    _, count = await _ingest(InMemoryKnowledgeStore(), embedder, _entries(30), doc_title="manual")
    chunk_calls = [call for call in embedder.calls if call[0].startswith("[Document: manual]")]
    assert [len(call) for call in chunk_calls] == [7, 7, 7, 7, 2]
    assert sum(len(call) for call in chunk_calls) == count == 30


def test_the_window_is_bounded_and_batch_aligned() -> None:
    assert CHUNK_EMBED_WINDOW <= 1000
    assert all(CHUNK_EMBED_WINDOW % batch == 0 for batch in (10, 25, 50, 100))


# ---------------------------------------------------------------------------
# The budget weighs chunks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chars", "chunks", "weight"),
    [(500, 0, 500), (500, 4, 500), (500, 8, 800), (100, 10, 1000), (2000, 1, 2000), (1, 1, 100)],
)
def test_a_document_weighs_its_chunks_at_the_caps_ratio(
    chars: int, chunks: int, weight: int
) -> None:
    configure_parsed_size(max_chars=1000, max_chunks=10)  # a chunk weighs 100 characters
    assert admission_weight(chars, chunks) == weight


async def test_an_ingest_is_admitted_with_its_chunk_count(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, int]] = []
    real = parsed_size.admitted

    @asynccontextmanager
    async def spy(chars: int, chunks: int = 0) -> AsyncIterator[None]:
        seen.append((chars, chunks))
        async with real(chars, chunks):
            yield

    monkeypatch.setattr(ingestion, "admitted", spy)
    markdown = _entries(25)
    _, count = await _ingest(InMemoryKnowledgeStore(), FakeEmbedder(), markdown)
    assert seen == [(len(markdown), count)] and count == 25


# ---------------------------------------------------------------------------
# Chunks built as they are read
# ---------------------------------------------------------------------------


def _rows(count: int) -> ChunkRows:
    pieces = [TextChunk(body=f"body {i}", heading_path=("h",)) for i in range(count)]
    hashes = [f"h{i}" for i in range(count)]
    vectors = {f"h{i}": compact([float(i), 0.5, -1.25]) for i in range(count)}
    return ChunkRows(
        pieces=pieces,
        hashes=hashes,
        vectors=vectors,
        tenant_id=_TENANT,
        kb_id=uuid4(),
        document_id=uuid4(),
        doc_title="t",
        model_key="m",
    )


def test_chunk_rows_read_like_the_list_they_replace() -> None:
    rows = _rows(5)
    assert len(rows) == 5
    assert rows[0].embedding == (0.0, 0.5, -1.25)
    assert rows[-1].chunk_index == 4 and rows[-1].content == "body 4"
    assert [chunk.chunk_index for chunk in rows[1:4]] == [1, 2, 3]
    assert [chunk.chunk_index for chunk in rows] == [0, 1, 2, 3, 4]
    assert rows[2].id == rows[2].id  # built anew on each read, same id
    assert len({chunk.id for chunk in rows}) == 5
    with pytest.raises(IndexError):
        rows[5]
    with pytest.raises(IndexError):
        rows[-6]


def test_a_compact_vector_holds_the_same_values() -> None:
    values = [0.1, -0.2, 1e-30, 3.141592653589793]
    assert tuple(compact(values)) == tuple(values)


def test_a_vector_that_is_already_compact_is_not_copied() -> None:
    # B-216: the store hands back compact vectors; copying each again would
    # hold two of every stored vector while a re-ingest collects them.
    vector = array("d", [0.25, -0.5])
    assert compact(vector) is vector
    single = array("f", [0.25, -0.5])
    assert compact(single) is not single and compact(single).typecode == "d"
