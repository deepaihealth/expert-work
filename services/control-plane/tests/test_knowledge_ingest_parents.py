"""B-206 K-1 —— an ingest stores the parent chunks next to the chunks searched.

* a parent row (``chunk_type='parent'``) holds the passage a search returns:
  no vector, its own ``chunk_index`` 0..m-1; each chunk points at its parent
  row (``parent_chunk_id``), and keeps its own ``chunk_index`` 0..n-1;
* a parent that is just its one chunk gets no row — the chunk stands for
  itself (``parent_chunk_id`` empty);
* only the chunks are embedded, and ``chunk_count`` counts only them;
* a re-ingest of unchanged text reuses the chunks' vectors and writes the
  same parents again; a re-index re-embeds the chunks and carries the parent
  rows through unchanged.

Every test runs on the in-memory store and, marked ``integration``, on
Postgres (real migrations).

Before running the Postgres half locally:
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from control_plane.knowledge.chunking import (
    ParentChunk,
    chunk_markdown_parented,
    chunk_markdown_semantic_parented,
    configure_semantic_breaks,
    semantic_breaks_enabled,
)
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner, ingest_markdown
from control_plane.knowledge.recovery import KnowledgeIngestRecoveryWorker
from expert_work.persistence import (
    DatabaseConfig,
    InMemoryKnowledgeStore,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge.base import KnowledgeStore
from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    ChunkType,
    DocumentStatus,
    KnowledgeChunk,
    KnowledgeDocument,
)
from orchestrator.llm import FakeEmbedder

_FILENAME = "guide.md"
_TITLE = "guide"  # knowledge_document_title(_FILENAME)
_DB = "knowledge_ingest_parents"
_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages/expert-work-persistence/alembic.ini"


def _paragraph(tag: str, words: int) -> str:
    return " ".join(f"{tag}{i}" for i in range(words)) + "."


#: Three parents at the default chunk size: ``One`` is one chunk (no parent
#: row), ``Two`` is one block cut into three chunks, ``Three`` is three
#: paragraphs — two chunks by size, three when every paragraph is a topic
#: of its own (:class:`_TopicEmbedder`).
_DOC = "\n\n".join(
    [
        "# One",
        _paragraph("one", 120),
        "# Two",
        _paragraph("two", 450),
        "# Three",
        _paragraph("alpha", 70),
        _paragraph("beta", 70),
        _paragraph("gamma", 70),
    ]
)
_SHORT = "# Handbook\n\nThe deductible is 500 dollars per year."


class _TopicEmbedder:
    """A text with ``beta0`` in it reads as another topic than the rest
    (orthogonal unit vectors, full width so the SQL store takes them): the
    semantic chunker splits ``Three`` into one chunk per paragraph. Records
    every text it is sent; ``model_key`` makes it a fixed model."""

    def __init__(self, model_key: str | None = None) -> None:
        self.texts: list[str] = []
        if model_key is not None:
            self.model_key = model_key

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        self.texts.extend(texts)
        return [self._vector(1 if "beta0" in text else 0) for text in texts]

    @staticmethod
    def _vector(axis: int) -> tuple[float, ...]:
        return tuple(1.0 if i == axis else 0.0 for i in range(EMBEDDING_DIM))


class _Recording(FakeEmbedder):
    """:class:`FakeEmbedder` that records every text; ``model_key`` makes it
    a fixed model (vector reuse applies)."""

    def __init__(self, model_key: str | None = None) -> None:
        super().__init__()
        self.texts: list[str] = []
        if model_key is not None:
            self.model_key = model_key

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        self.texts.extend(texts)
        return await super().embed(texts, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Stores and chunking modes
# ---------------------------------------------------------------------------


@pytest.fixture(params=["memory", pytest.param("sql", marks=pytest.mark.integration)])
def _dsn(request: pytest.FixtureRequest) -> Iterator[str | None]:
    """``None`` for the in-memory store; a fresh, migrated database otherwise."""
    if request.param == "memory":
        yield None
        return
    container = request.getfixturevalue("postgres_container")
    admin = str(container.get_connection_url()).replace("+psycopg2", "")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_DB}"')
    dsn = f"{admin.rpartition('/')[0]}/{_DB}"
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")
    try:
        yield dsn
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


@pytest.fixture
async def store(_dsn: str | None) -> AsyncIterator[KnowledgeStore]:
    if _dsn is None:
        yield InMemoryKnowledgeStore()
        return
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    )
    try:
        yield SqlKnowledgeStore(create_async_session_factory(engine))
    finally:
        await engine.dispose()


@pytest.fixture(params=[False, True], ids=["structural", "semantic"])
def semantic(request: pytest.FixtureRequest) -> Iterator[bool]:
    """Ingests chunk by structure and size only, or also at topic shifts."""
    previous = semantic_breaks_enabled()
    configure_semantic_breaks(enabled=request.param)
    yield request.param
    configure_semantic_breaks(enabled=previous)


async def _parents_cut(markdown: str, *, semantic: bool) -> list[ParentChunk]:
    """The parents the ingest cuts ``markdown`` into (the upload path's
    arguments)."""
    max_tokens, overlap_tokens = DEFAULT_CHUNK_MAX_TOKENS, DEFAULT_CHUNK_OVERLAP_TOKENS
    if semantic:
        return await chunk_markdown_semantic_parented(
            markdown,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
            embedder=_TopicEmbedder(),
            tenant_id=uuid4(),
            doc_title=_TITLE,
        )
    return chunk_markdown_parented(
        markdown, max_tokens=max_tokens, overlap_tokens=overlap_tokens, doc_title=_TITLE
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _upload(
    store: KnowledgeStore, markdown: str, *, tenant: UUID | None = None
) -> KnowledgeDocument:
    tenant = tenant or uuid4()
    base = await store.get_base(tenant_id=tenant, name="kb") or await store.create_base(
        tenant_id=tenant, name="kb"
    )
    return await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename=_FILENAME, content=markdown.encode()
    )


async def _ingest(
    store: KnowledgeStore, embedder: object, markdown: str, *, tenant: UUID | None = None
) -> KnowledgeDocument:
    """Upload ``markdown`` and run the fast ingest path on it."""
    document = await _upload(store, markdown, tenant=tenant)
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)  # type: ignore[arg-type]
    await runner.submit(tenant_id=document.tenant_id, document_id=document.id)
    return await _reread(store, document)


async def _reread(store: KnowledgeStore, document: KnowledgeDocument) -> KnowledgeDocument:
    fetched = await store.get_document(tenant_id=document.tenant_id, document_id=document.id)
    assert fetched is not None
    return fetched


async def _rows(
    store: KnowledgeStore, document: KnowledgeDocument
) -> tuple[list[KnowledgeChunk], list[KnowledgeChunk]]:
    """The document's ``(parent rows, chunks)``, each in ``chunk_index`` order."""
    tenant, doc = document.tenant_id, document.id
    parents, _ = await store.list_chunks(
        tenant_id=tenant, document_id=doc, limit=1000, chunk_types={ChunkType.PARENT}
    )
    chunks, _ = await store.list_chunks(tenant_id=tenant, document_id=doc, limit=1000)
    return parents, chunks


def _shape(parents: list[KnowledgeChunk], chunks: list[KnowledgeChunk]) -> list[object]:
    """Rows as comparable values: each parent row's place and text, each
    chunk's text and the place of the parent row it points at."""
    place: dict[UUID | None, int] = {parent.id: parent.chunk_index for parent in parents}
    return [
        *((p.chunk_index, p.content, p.heading_path) for p in parents),
        *((c.chunk_index, c.content, c.heading_path, place.get(c.parent_chunk_id)) for c in chunks),
    ]


def _expected_shape(cut: list[ParentChunk]) -> list[object]:
    stored = [parent for parent in cut if not parent.is_trivial]
    rows: list[object] = [(i, p.body, p.heading_path) for i, p in enumerate(stored)]
    owners = [
        None if parent.is_trivial else stored.index(parent)
        for parent in cut
        for _ in parent.children
    ]
    children = [child for parent in cut for child in parent.children]
    rows.extend(
        (i, child.body, child.heading_path, owner)
        for i, (child, owner) in enumerate(zip(children, owners, strict=True))
    )
    return rows


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


async def test_parent_rows_are_stored_and_every_chunk_points_at_its_own(
    store: KnowledgeStore, semantic: bool
) -> None:
    cut = await _parents_cut(_DOC, semantic=semantic)
    # The document is what this test needs: a parent of one chunk among
    # parents of several, and the two modes cutting it differently.
    assert [len(parent.children) for parent in cut] == ([1, 3, 3] if semantic else [1, 3, 2])
    assert cut[0].is_trivial and not any(parent.is_trivial for parent in cut[1:])

    document = await _ingest(store, _TopicEmbedder(), _DOC)
    assert document.status is DocumentStatus.READY
    parents, chunks = await _rows(store, document)
    assert _shape(parents, chunks) == _expected_shape(cut)
    assert [p.chunk_index for p in parents] == [0, 1]
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    # The chunk of the one-chunk parent stands for itself.
    assert chunks[0].parent_chunk_id is None
    assert {c.parent_chunk_id for c in chunks[1:]} == {p.id for p in parents}


@pytest.mark.usefixtures("semantic")
async def test_a_parent_row_has_no_vector_and_nothing_to_reuse(store: KnowledgeStore) -> None:
    document = await _ingest(store, _TopicEmbedder("fake/a"), _DOC)
    parents, chunks = await _rows(store, document)
    assert parents
    assert {
        (p.chunk_type, p.embedding, p.embedding_model, p.content_hash, p.doc_title) for p in parents
    } == {(ChunkType.PARENT, None, None, None, _TITLE)}
    assert {(c.chunk_type, c.embedding, c.embedding_model) for c in chunks} == {
        (ChunkType.TEXT, (), "fake/a")
    }
    assert all(c.content_hash for c in chunks)


@pytest.mark.usefixtures("semantic")
async def test_chunk_count_counts_only_the_chunks(store: KnowledgeStore) -> None:
    document = await _ingest(store, _TopicEmbedder(), _DOC)
    parents, chunks = await _rows(store, document)
    assert parents
    assert document.chunk_count == len(chunks)
    base = await store.get_base(tenant_id=document.tenant_id, name="kb")
    assert base is not None
    assert await store.base_stats(tenant_id=document.tenant_id, kb_id=base.id) == (1, len(chunks))


async def test_ingest_markdown_returns_the_chunk_count(store: KnowledgeStore) -> None:
    # The eval runner's entry point: what it records as the document's count.
    document = await _upload(store, _DOC)
    count = await ingest_markdown(
        store=store,
        embedder=_TopicEmbedder(),
        tenant_id=document.tenant_id,
        document_id=document.id,
        kb_id=document.kb_id,
        markdown=_DOC,
        chunk_max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=DEFAULT_CHUNK_OVERLAP_TOKENS,
        doc_title=_TITLE,
    )
    parents, chunks = await _rows(store, document)
    assert parents
    assert count == len(chunks)


@pytest.mark.usefixtures("semantic")
async def test_a_one_chunk_document_gets_no_parent_row(store: KnowledgeStore) -> None:
    [parent] = await _parents_cut(_SHORT, semantic=False)
    assert parent.is_trivial
    document = await _ingest(store, _TopicEmbedder(), _SHORT)
    parents, chunks = await _rows(store, document)
    assert parents == []
    assert [(c.content, c.parent_chunk_id) for c in chunks] == [(parent.body, None)]
    assert document.chunk_count == 1


async def test_only_the_chunks_are_embedded(store: KnowledgeStore, semantic: bool) -> None:
    embedder = _TopicEmbedder()
    document = await _ingest(store, embedder, _DOC)
    parents, chunks = await _rows(store, document)
    assert parents
    # Chunk texts open with the document title; the semantic chunker's block
    # texts (topic detection) do not.
    sent = [text for text in embedder.texts if text.startswith(f"[Document: {_TITLE}]")]
    assert sent == [c.retrieval_text for c in chunks]
    if not semantic:
        assert embedder.texts == sent
    parent_texts = {p.content for p in parents} | {p.retrieval_text for p in parents}
    assert parent_texts.isdisjoint(embedder.texts)


@pytest.mark.usefixtures("semantic")
async def test_a_reingest_of_unchanged_text_reuses_the_vectors_and_the_parents(
    store: KnowledgeStore,
) -> None:
    first = await _ingest(store, _TopicEmbedder("fake/a"), _DOC)
    before = await _rows(store, first)
    assert before[0]
    embedder = _TopicEmbedder("fake/a")
    again = await _ingest(store, embedder, _DOC, tenant=first.tenant_id)
    assert again.id == first.id and again.status is DocumentStatus.READY
    # Nothing sent at all: the chunks keep their vectors, topic shifts their
    # decisions (B-215) — and a parent never has anything to send.
    assert embedder.texts == []
    after = await _rows(store, again)
    assert _shape(*after) == _shape(*before)
    assert [c.content_hash for c in after[1]] == [c.content_hash for c in before[1]]
    assert {c.embedding_model for c in after[1]} == {"fake/a"}


async def test_a_reingest_after_an_edit_rewrites_the_parents(
    store: KnowledgeStore, semantic: bool
) -> None:
    first = await _ingest(store, _TopicEmbedder("fake/a"), _DOC)
    edited = _DOC.replace("gamma0 ", "delta ")
    embedder = _TopicEmbedder("fake/a")
    again = await _ingest(store, embedder, edited, tenant=first.tenant_id)
    parents, chunks = await _rows(store, again)
    assert _shape(parents, chunks) == _expected_shape(await _parents_cut(edited, semantic=semantic))
    assert any("delta" in p.content for p in parents)
    # Only the edited chunk was embedded again.
    assert [text for text in embedder.texts if text.startswith("[Document:")] == [
        c.retrieval_text for c in chunks if "delta" in c.content
    ]


# ---------------------------------------------------------------------------
# Re-index and recovery
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("semantic")
async def test_a_reindex_keeps_the_parent_rows_and_the_links(store: KnowledgeStore) -> None:
    document = await _ingest(store, _TopicEmbedder(), _DOC)
    parents, chunks = await _rows(store, document)
    assert parents
    embedder = _Recording("fake/c")
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    await runner.submit_reindex(
        tenant_id=document.tenant_id,
        kb_id=document.kb_id,
        embedding_provider="fake",
        embedding_model="fake-2",
    )
    after_parents, after_chunks = await _rows(store, document)

    def kept(rows: list[KnowledgeChunk]) -> list[object]:
        return [
            (r.id, r.chunk_index, r.content, r.heading_path, r.doc_title, r.parent_chunk_id)
            for r in rows
        ]

    assert kept(after_parents) == kept(parents)
    assert kept(after_chunks) == kept(chunks)
    assert {(p.embedding, p.embedding_model) for p in after_parents} == {(None, None)}
    assert {c.embedding_model for c in after_chunks} == {"fake/c"}
    # Re-embedded: the chunks, nothing of a parent.
    assert embedder.texts == [c.retrieval_text for c in chunks]


async def test_the_recovery_worker_stores_the_parents_too(
    store: KnowledgeStore, semantic: bool
) -> None:
    document = await _upload(store, _DOC)  # pending: the fast path never ran
    worker = KnowledgeIngestRecoveryWorker(
        store=store,
        embedder=_TopicEmbedder(),
        interval_s=1,
        concurrency=2,
        lease_seconds=300,
        max_attempts=5,
    )
    assert await worker.run_once() == 1
    recovered = await _reread(store, document)
    assert recovered.status is DocumentStatus.READY
    parents, chunks = await _rows(store, recovered)
    assert _shape(parents, chunks) == _expected_shape(await _parents_cut(_DOC, semantic=semantic))
    assert recovered.chunk_count == len(chunks)
