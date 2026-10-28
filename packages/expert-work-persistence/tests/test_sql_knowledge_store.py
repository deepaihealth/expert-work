"""Integration tests for SqlKnowledgeStore against Postgres + pgvector — J.5."""

from __future__ import annotations

import asyncio
from array import array
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import overload
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Text, cast, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge import DuplicateKnowledgeBaseError, text_search
from expert_work.persistence.knowledge import sql as sql_module
from expert_work.persistence.knowledge.sql import _kb_keyword_tsquery
from expert_work.persistence.knowledge.text_search import KbKeywordQuery
from expert_work.persistence.models import KnowledgeBaseRow, KnowledgeChunkRow
from expert_work.protocol import (
    ChunkType,
    DocumentStatus,
    IngestErrorCode,
    KnowledgeChunk,
    RetrievalMethod,
)

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

SqlStoreFixture = tuple[SqlKnowledgeStore, AsyncEngine]


def _sync_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


def _async_dsn(container: PostgresContainer) -> str:
    url = str(container.get_connection_url())
    return url.replace("+psycopg2", "+asyncpg").replace("postgresql://", "postgresql+asyncpg://", 1)


def _vec(*head: float) -> tuple[float, ...]:
    """An ``EMBEDDING_DIM``-wide vector with ``head`` as its leading values."""
    return tuple(head) + (0.0,) * (EMBEDDING_DIM - len(head))


@pytest.fixture
def sql_store(postgres_container: PostgresContainer) -> Iterator[SqlStoreFixture]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")

    engine = create_async_engine_from_config(DatabaseConfig(dsn=_async_dsn(postgres_container)))
    session_factory = create_async_session_factory(engine)
    yield SqlKnowledgeStore(session_factory), engine


@pytest.mark.asyncio
async def test_base_crud(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        created = await store.create_base(tenant_id=tenant, name="hr-policies")
        fetched = await store.get_base(tenant_id=tenant, name="hr-policies")
        assert fetched is not None
        assert fetched.id == created.id
        assert {b.name for b in await store.list_bases(tenant_id=tenant)} == {"hr-policies"}
        with pytest.raises(DuplicateKnowledgeBaseError):
            await store.create_base(tenant_id=tenant, name="hr-policies")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_document_upsert_resets_existing(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
        assert doc.status is DocumentStatus.PENDING

        await store.set_document_status(
            tenant_id=tenant,
            document_id=doc.id,
            status=DocumentStatus.READY,
            chunk_count=5,
        )
        reset = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
        assert reset.id == doc.id
        assert reset.status is DocumentStatus.PENDING
        assert reset.chunk_count == 5  # the serving count stays until replaced
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_replace_chunks_and_search_orders_by_cosine(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=doc.id,
            chunks=[
                _make_chunk(tenant, base.id, doc.id, 0, "east", _vec(1.0, 0.0)),
                _make_chunk(tenant, base.id, doc.id, 1, "north", _vec(0.0, 1.0)),
                _make_chunk(tenant, base.id, doc.id, 2, "ne", _vec(0.7, 0.7)),
            ],
        )
        hits = await store.search(
            tenant_id=tenant, kb_ids=[base.id], query_embedding=_vec(1.0, 0.0), limit=3
        )
        assert [h.content for h in hits] == ["east", "ne", "north"]
        assert hits[0].embedding is not None
        assert len(hits[0].embedding) == EMBEDDING_DIM
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_replace_chunks_writes_every_batch_in_order(
    sql_store: SqlStoreFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B-214: rows are built and flushed a batch at a time (so a document's
    # rows are never all in memory at once) — still all of them, in one swap.
    monkeypatch.setattr(sql_module, "_CHUNK_INSERT_BATCH", 3)
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
        chunks = [
            _make_chunk(tenant, base.id, doc.id, i, f"part {i}", _vec(0.1 * i, 1.0 / (i + 1)))
            for i in range(7)
        ]
        await store.replace_chunks(tenant_id=tenant, document_id=doc.id, chunks=chunks[:2])
        await store.replace_chunks(tenant_id=tenant, document_id=doc.id, chunks=chunks)
        stored, total = await store.list_chunks(tenant_id=tenant, document_id=doc.id, limit=50)
        assert total == 7
        assert [c.content for c in stored] == [f"part {i}" for i in range(7)]
        hits = await store.search(
            tenant_id=tenant, kb_ids=[base.id], query_embedding=_vec(0.6, 1.0 / 7), limit=1
        )
        assert hits[0].content == "part 6"
        assert hits[0].embedding == pytest.approx(chunks[6].embedding)
    finally:
        await engine.dispose()


class _TrackedChunks(Sequence[KnowledgeChunk]):
    """A chunk sequence that records the furthest index anything has read."""

    def __init__(self, chunks: list[KnowledgeChunk]) -> None:
        self._chunks = chunks
        self.furthest = -1

    def __len__(self) -> int:
        return len(self._chunks)

    @overload
    def __getitem__(self, index: int) -> KnowledgeChunk:
        """One chunk."""

    @overload
    def __getitem__(self, index: slice) -> Sequence[KnowledgeChunk]:
        """A run of chunks."""

    def __getitem__(self, index: int | slice) -> KnowledgeChunk | Sequence[KnowledgeChunk]:
        if isinstance(index, slice):
            picked = range(len(self._chunks))[index]
            if picked:
                self.furthest = max(self.furthest, max(picked))
            return self._chunks[index]
        self.furthest = max(self.furthest, index % len(self._chunks))
        return self._chunks[index]


@pytest.mark.asyncio
async def test_replace_chunks_reads_its_input_a_batch_at_a_time(
    sql_store: SqlStoreFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B-214: the ingest path hands a lazy sequence whose rows are built on
    # read; reading it whole first (to reorder, say) would hold every vector at
    # once. When a batch's rows are built, nothing past that batch was read.
    monkeypatch.setattr(sql_module, "_CHUNK_INSERT_BATCH", 2)
    seen_at_build: list[int] = []
    real_chunk_row = sql_module._chunk_row
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.md")
        parent = _parent_chunk(tenant, base.id, doc.id, 0, "parent 0")
        children = [
            _make_chunk(tenant, base.id, doc.id, i, f"child {i}", _vec(1.0, 0.1 * i)).model_copy(
                update={"parent_chunk_id": parent.id}
            )
            for i in range(5)
        ]
        tracked = _TrackedChunks([parent, *children])

        def _recording(chunk: KnowledgeChunk) -> KnowledgeChunkRow:
            seen_at_build.append(tracked.furthest)
            return real_chunk_row(chunk)

        monkeypatch.setattr(sql_module, "_chunk_row", _recording)
        await store.replace_chunks(tenant_id=tenant, document_id=doc.id, chunks=tracked)
        assert seen_at_build == [1, 1, 3, 3, 5, 5]
        stored, total = await store.list_chunks(tenant_id=tenant, document_id=doc.id, limit=50)
        assert total == 5
        assert {c.parent_chunk_id for c in stored} == {parent.id}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_replace_chunks_records_the_chunking_fingerprint(sql_store: SqlStoreFixture) -> None:
    # B-206 K-3: the chunks' fingerprint lands on the document with them; a
    # write without one (a re-index swapping vectors) leaves it as it is.
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.md")
        assert doc.chunk_params_hash is None
        chunks = [_make_chunk(tenant, base.id, doc.id, 0, "east", _vec(1.0, 0.0))]
        await store.replace_chunks(
            tenant_id=tenant, document_id=doc.id, chunks=chunks, chunk_params_hash="aaaa"
        )
        fetched = await store.get_document(tenant_id=tenant, document_id=doc.id)
        assert fetched is not None and fetched.chunk_params_hash == "aaaa"
        await store.replace_chunks(tenant_id=tenant, document_id=doc.id, chunks=chunks)
        fetched = await store.get_document(tenant_id=tenant, document_id=doc.id)
        assert fetched is not None and fetched.chunk_params_hash == "aaaa"
        await store.replace_chunks(
            tenant_id=tenant, document_id=doc.id, chunks=chunks, chunk_params_hash="bbbb"
        )
        listed = await store.list_documents(tenant_id=tenant, kb_id=base.id)
        assert [d.chunk_params_hash for d in listed] == ["bbbb"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_chunk_embeddings_by_hash_reads_every_batch_as_compact_vectors(
    sql_store: SqlStoreFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B-216: a re-ingest reads the stored vectors a batch of hashes at a time,
    # each made 8-byte floats as it arrives — never all of them as Python
    # floats at once — and still gets every one of them back.
    monkeypatch.setattr(sql_module, "_EMBEDDING_READ_BATCH", 3)
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
        chunks = [
            _make_chunk(
                tenant, base.id, doc.id, i, f"part {i}", _vec(0.1 * i, 1.0 / (i + 1))
            ).model_copy(update={"content_hash": f"{i:064x}", "embedding_model": "m/v1"})
            for i in range(7)
        ]
        await store.replace_chunks(tenant_id=tenant, document_id=doc.id, chunks=chunks)
        found = await store.chunk_embeddings_by_hash(
            tenant_id=tenant,
            document_id=doc.id,
            content_hashes=[f"{i:064x}" for i in range(7)],
            embedding_model="m/v1",
        )
        assert set(found) == {f"{i:064x}" for i in range(7)}
        for chunk in chunks:
            vector = found[chunk.content_hash or ""]
            assert isinstance(vector, array) and vector.typecode == "d"
            assert list(vector) == pytest.approx(chunk.embedding)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_base_persists_chunk_params(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        await store.create_base(
            tenant_id=tenant, name="default", chunk_max_tokens=512, chunk_overlap_tokens=64
        )
        tuned = await store.create_base(
            tenant_id=tenant, name="tuned", chunk_max_tokens=256, chunk_overlap_tokens=16
        )
        assert (tuned.chunk_max_tokens, tuned.chunk_overlap_tokens) == (256, 16)
        fetched = await store.get_base(tenant_id=tenant, name="tuned")
        assert fetched is not None
        assert fetched.chunk_max_tokens == 256
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_keyword_search_ranks_by_full_text(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=doc.id,
            chunks=[
                _make_chunk(tenant, base.id, doc.id, 0, "the quarterly invoice payment", _vec(1.0)),
                _make_chunk(tenant, base.id, doc.id, 1, "lazy sleeping dog at noon", _vec(1.0)),
            ],
        )
        hits = await store.keyword_search(
            tenant_id=tenant, kb_ids=[base.id], query="invoice", limit=5
        )
        assert [h.chunk_index for h in hits] == [0]
        # A query whose terms are not indexed yields nothing.
        assert (
            await store.keyword_search(tenant_id=tenant, kb_ids=[base.id], query="spaceship") == []
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_delete_base_cascades(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=doc.id,
            chunks=[_make_chunk(tenant, base.id, doc.id, 0, "c", _vec(1.0))],
        )
        assert await store.delete_base(tenant_id=tenant, kb_id=base.id) is True
        assert await store.get_base(tenant_id=tenant, name="kb") is None
        assert await store.list_documents(tenant_id=tenant, kb_id=base.id) == []
        assert (
            await store.search(tenant_id=tenant, kb_ids=[base.id], query_embedding=_vec(1.0)) == []
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_base_persists_metadata_and_retrieval_config(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        await store.create_base(
            tenant_id=tenant,
            name="kb",
            description="HR docs",
            created_by="alice@acme.com",
            retrieval_top_k=8,
            retrieval_score_threshold=0.4,
            retrieval_method=RetrievalMethod.VECTOR,
            rerank_enabled=False,
            embedding_provider="qwen",
            embedding_model="text-embedding-v4",
        )
        fetched = await store.get_base(tenant_id=tenant, name="kb")
        assert fetched is not None
        assert fetched.description == "HR docs"
        assert fetched.created_by == "alice@acme.com"
        assert fetched.retrieval_top_k == 8
        assert fetched.retrieval_score_threshold == 0.4
        assert fetched.retrieval_method is RetrievalMethod.VECTOR
        assert fetched.rerank_enabled is False
        assert fetched.embedding_model == "text-embedding-v4"
        assert fetched.updated_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_update_base_clear_vs_omit(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(
            tenant_id=tenant, name="kb", description="orig", retrieval_score_threshold=0.5
        )
        # Omit description (unchanged); clear threshold with explicit None; bump top_k.
        updated = await store.update_base(
            tenant_id=tenant,
            kb_id=base.id,
            retrieval_score_threshold=None,
            retrieval_top_k=12,
            retrieval_method=RetrievalMethod.KEYWORD,
        )
        assert updated is not None
        assert updated.description == "orig"
        assert updated.retrieval_score_threshold is None
        assert updated.retrieval_top_k == 12
        assert updated.retrieval_method is RetrievalMethod.KEYWORD
        # Clearing description explicitly.
        cleared = await store.update_base(tenant_id=tenant, kb_id=base.id, description=None)
        assert cleared is not None
        assert cleared.description is None
        # Missing base → None.
        assert await store.update_base(tenant_id=tenant, kb_id=uuid4(), description="x") is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rerank_score_threshold_round_trip(sql_store: SqlStoreFixture) -> None:
    """B-175 wave 2 PR3 — migration 0168's column: stored at create, kept by an
    update that omits it, cleared by an explicit None."""
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb", rerank_score_threshold=0.7)
        fetched = await store.get_base(tenant_id=tenant, name="kb")
        assert fetched is not None
        assert fetched.rerank_score_threshold == 0.7
        kept = await store.update_base(tenant_id=tenant, kb_id=base.id, retrieval_top_k=9)
        assert kept is not None
        assert kept.rerank_score_threshold == 0.7
        cleared = await store.update_base(
            tenant_id=tenant, kb_id=base.id, rerank_score_threshold=None
        )
        assert cleared is not None
        assert cleared.rerank_score_threshold is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rerank_threshold_model_round_trip(sql_store: SqlStoreFixture) -> None:
    """B-206 K-7b — migration 0171's column: stored at create, kept by an update
    that omits it, replaced or cleared by an explicit value."""
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(
            tenant_id=tenant,
            name="kb",
            rerank_score_threshold=0.7,
            rerank_threshold_model="qwen/qwen3-vl-rerank",
        )
        fetched = await store.get_base(tenant_id=tenant, name="kb")
        assert fetched is not None
        assert fetched.rerank_threshold_model == "qwen/qwen3-vl-rerank"
        kept = await store.update_base(tenant_id=tenant, kb_id=base.id, rerank_score_threshold=0.6)
        assert kept is not None
        assert kept.rerank_threshold_model == "qwen/qwen3-vl-rerank"
        moved = await store.update_base(
            tenant_id=tenant, kb_id=base.id, rerank_threshold_model="qwen/qwen3-rerank"
        )
        assert moved is not None
        assert moved.rerank_threshold_model == "qwen/qwen3-rerank"
        cleared = await store.update_base(
            tenant_id=tenant, kb_id=base.id, rerank_threshold_model=None
        )
        assert cleared is not None
        assert cleared.rerank_threshold_model is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_base_stats_aggregate(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        empty = await store.create_base(tenant_id=tenant, name="empty")
        d1 = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="a.pdf")
        d2 = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="b.pdf")
        await store.set_document_status(
            tenant_id=tenant, document_id=d1.id, status=DocumentStatus.READY, chunk_count=3
        )
        await store.set_document_status(
            tenant_id=tenant, document_id=d2.id, status=DocumentStatus.READY, chunk_count=4
        )
        assert await store.base_stats(tenant_id=tenant, kb_id=base.id) == (2, 7)
        assert await store.base_stats(tenant_id=tenant, kb_id=empty.id) == (0, 0)
        many = await store.base_stats_many(tenant_id=tenant)
        assert many[base.id] == (2, 7)
        assert empty.id not in many
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_count_documents_capped(sql_store: SqlStoreFixture) -> None:
    """B-175 P4 — same contract as the in-memory store's test of this name."""
    store, engine = sql_store
    try:
        tenant, other_tenant = uuid4(), uuid4()
        a = await store.create_base(tenant_id=tenant, name="a")
        b = await store.create_base(tenant_id=tenant, name="b")
        empty = await store.create_base(tenant_id=tenant, name="empty")
        foreign = await store.create_base(tenant_id=other_tenant, name="a")
        for kb, owner, n in ((a, tenant, 3), (b, tenant, 1), (foreign, other_tenant, 2)):
            for _ in range(n):
                await store.upsert_document(tenant_id=owner, kb_id=kb.id, filename=f"{uuid4()}.md")
        got = await store.count_documents_capped(
            tenant_id=tenant, kb_ids=[a.id, b.id, empty.id, foreign.id, a.id], cap=2
        )
        assert got == {a.id: 2, b.id: 1, empty.id: 0, foreign.id: 0}
        assert await store.count_documents_capped(tenant_id=tenant, kb_ids=[a.id], cap=10) == {
            a.id: 3
        }
        assert await store.count_documents_capped(tenant_id=tenant, kb_ids=[], cap=10) == {}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_bases_all_tenants_aggregates(sql_store: SqlStoreFixture) -> None:
    """W4 — the all-tenants readers span tenants (newest first, per-base
    stats keyed by globally-unique kb_id). Session container is shared, so
    assertions filter to this test's tenants."""
    store, engine = sql_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        base_a = await store.create_base(tenant_id=tenant_a, name="kb-a")
        base_b = await store.create_base(tenant_id=tenant_b, name="kb-b")
        doc = await store.upsert_document(tenant_id=tenant_a, kb_id=base_a.id, filename="a.pdf")
        await store.set_document_status(
            tenant_id=tenant_a, document_id=doc.id, status=DocumentStatus.READY, chunk_count=3
        )

        rows = await store.list_bases_all_tenants(limit=500)
        mine = [b for b in rows if b.tenant_id in {tenant_a, tenant_b}]
        # Both tenants' bases surface, newest first across tenants.
        assert [(b.tenant_id, b.name) for b in mine] == [(tenant_b, "kb-b"), (tenant_a, "kb-a")]

        # C-5 — stats are bounded to the kb_ids of the (capped) base page.
        stats = await store.base_stats_many_all_tenants(kb_ids=[base_a.id, base_b.id])
        assert stats[base_a.id] == (1, 3)
        assert base_b.id not in stats  # no documents yet
        assert await store.base_stats_many_all_tenants(kb_ids=[base_b.id]) == {}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_bases_all_tenants_id_tiebreak_on_equal_created_at(
    sql_store: SqlStoreFixture,
) -> None:
    """W4 review C-3 (2nd round) — ``created_at`` ties order by ``id`` ASC.

    The query has no WHERE, so under the session-scoped container EVERY row in
    the table joins the sort — with a 2-row tie a dropped tiebreak could still
    come out ascending by accident (file-scope false green). Eight tied rows
    are seeded instead, their ``created_at`` equalized via raw UPDATEs issued
    highest-id first so the live heap tuples land in *descending* id order: a
    dropped tiebreak cannot happen to emit the full 8-element ascending id
    sequence, whatever else is in the table."""
    store, engine = sql_store
    try:
        tenants = [uuid4() for _ in range(8)]
        bases = [
            await store.create_base(tenant_id=tenant, name=f"tie-{i}")
            for i, tenant in enumerate(tenants)
        ]
        ordered = sorted(bases, key=lambda b: b.id.int)
        ts = datetime(2035, 1, 1, 12, 0, tzinfo=UTC)
        async with engine.begin() as conn:
            for kb in reversed(ordered):  # highest id first → heap id-descending
                await conn.execute(
                    update(KnowledgeBaseRow)
                    .where(KnowledgeBaseRow.id == kb.id)
                    .values(created_at=ts)
                )

        rows = await store.list_bases_all_tenants(limit=500)
        my_tenants = set(tenants)
        # Postgres uuid byte order == UUID.int order.
        assert [b.id for b in rows if b.tenant_id in my_tenants] == [b.id for b in ordered]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_bases_all_tenants_respects_limit(sql_store: SqlStoreFixture) -> None:
    """W4 review #2 — the ``limit`` leg is load-bearing. Two rows are pushed
    to the global top of the ``created_at DESC`` order (far-future stamps beat
    every other row in the shared session container), then ``limit=1`` must
    return exactly the newer one."""
    store, engine = sql_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        older = await store.create_base(tenant_id=tenant_a, name="limit-older")
        newer = await store.create_base(tenant_id=tenant_b, name="limit-newer")
        async with engine.begin() as conn:
            for kb, ts in (
                (older, datetime(2036, 1, 1, tzinfo=UTC)),
                (newer, datetime(2036, 1, 2, tzinfo=UTC)),
            ):
                await conn.execute(
                    update(KnowledgeBaseRow)
                    .where(KnowledgeBaseRow.id == kb.id)
                    .values(created_at=ts)
                )

        rows = await store.list_bases_all_tenants(limit=1)
        assert [b.id for b in rows] == [newer.id]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_search_scored_surfaces_similarity(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=doc.id,
            chunks=[
                _make_chunk(tenant, base.id, doc.id, 0, "near", _vec(1.0, 0.0)),
                _make_chunk(tenant, base.id, doc.id, 1, "far", _vec(0.0, 1.0)),
            ],
        )
        hits = await store.search_scored(
            tenant_id=tenant, kb_ids=[base.id], query_embedding=_vec(1.0, 0.0)
        )
        assert [h.chunk.content for h in hits] == ["near", "far"]
        assert all(h.source == "vector" for h in hits)
        # 1 - cosine_distance lands in [0, 1] for normalised vectors, descending.
        assert hits[0].score == pytest.approx(1.0, abs=1e-6)
        assert hits[1].score == pytest.approx(0.0, abs=1e-6)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_keyword_search_scored_surfaces_rank(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=doc.id,
            chunks=[
                _make_chunk(tenant, base.id, doc.id, 0, "quarterly invoice payment", _vec(1.0)),
            ],
        )
        hits = await store.keyword_search_scored(
            tenant_id=tenant, kb_ids=[base.id], query="invoice"
        )
        assert len(hits) == 1
        assert hits[0].source == "keyword"
        assert hits[0].score > 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_kb_keyword_tsquery_binds_every_term_as_a_value(sql_store: SqlStoreFixture) -> None:
    """Terms and phrases reach Postgres as bound values parsed by
    ``plainto_tsquery`` / ``phraseto_tsquery`` — tsquery operators inside them
    are plain text, never syntax — and the parser drops the ``-`` of
    ``qxa - n7`` without spending a position on it, so the identifier becomes
    the adjacent pair ``'qxa' <-> 'n7'``."""
    _store, engine = sql_store
    try:
        query = KbKeywordQuery(
            terms=("a & b", "x:*", "<->", "'q'"), phrases=("qxa - n7", "(n7 | z)")
        )
        async with engine.connect() as conn:
            rendered = (
                await conn.execute(select(cast(_kb_keyword_tsquery(query), Text)))
            ).scalar_one()
            adjacent = (
                await conn.execute(
                    select(
                        text(
                            "to_tsvector('simple', 'x qxa - n7 y') "
                            "@@ phraseto_tsquery('simple', 'qxa - n7')"
                        )
                    )
                )
            ).scalar_one()
        assert rendered == "'a' & 'b' | 'x' | 'q' | 'qxa' <-> 'n7' | 'n7' <-> 'z'"
        assert adjacent is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_keyword_statement_survives_hundreds_of_query_units(
    sql_store: SqlStoreFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the term cap lifted, a 201-unit query still compiles and runs: the
    ``||`` chain and the coverage sum are balanced trees, not left-deep chains
    that blow SQLAlchemy's recursion limit."""
    store, engine = sql_store
    try:
        monkeypatch.setattr(text_search, "KB_QUERY_MAX_TERMS", 10_000)
        words = ["w" + chr(97 + i // 26) + chr(97 + i % 26) for i in range(200)]
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=doc.id,
            chunks=[_make_chunk(tenant, base.id, doc.id, 0, f"{words[150]} 内容", _vec(1.0))],
        )
        hits = await store.keyword_search_scored(
            tenant_id=tenant, kb_ids=[base.id], query=" ".join(words) + " QXA-N7"
        )
        assert [hit.chunk.chunk_index for hit in hits] == [0]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_chunks_paginates_and_omits_embedding(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=doc.id,
            chunks=[
                _make_chunk(tenant, base.id, doc.id, i, f"chunk-{i}", _vec(float(i)))
                for i in range(5)
            ],
        )
        page, total = await store.list_chunks(
            tenant_id=tenant, document_id=doc.id, offset=1, limit=2
        )
        assert total == 5
        assert [c.chunk_index for c in page] == [1, 2]
        assert all(c.embedding == () for c in page)
        # Cross-tenant isolation.
        other_page, other_total = await store.list_chunks(tenant_id=uuid4(), document_id=doc.id)
        assert (other_page, other_total) == ([], 0)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stamp_embedding_and_reindex_flag(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        await store.stamp_embedding_model(
            tenant_id=tenant, kb_id=base.id, embedding_provider="qwen", embedding_model="v4"
        )
        assert await store.request_reindex(tenant_id=tenant, kb_id=base.id) is True
        fetched = await store.get_base(tenant_id=tenant, name="kb")
        assert fetched is not None
        assert (fetched.embedding_provider, fetched.embedding_model) == ("qwen", "v4")
        assert fetched.reindex_requested_at is not None
        await store.clear_reindex(tenant_id=tenant, kb_id=base.id)
        cleared = await store.get_base(tenant_id=tenant, name="kb")
        assert cleared is not None
        assert cleared.reindex_requested_at is None
        assert await store.request_reindex(tenant_id=tenant, kb_id=uuid4()) is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upsert_retains_bytes(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"hello bytes"
        )
        assert (
            await store.get_document_content(tenant_id=tenant, document_id=doc.id) == b"hello bytes"
        )
        assert await store.get_document_content(tenant_id=uuid4(), document_id=doc.id) is None
    finally:
        await engine.dispose()


# NOTE: ``postgres_container`` is session-scoped (shared DB) and
# ``claim_documents_for_ingest`` is intentionally cross-tenant, so assertions
# below scope to this test's own seeded document ids and never assume the
# claimable set is empty — other tests leave rows behind.


@pytest.mark.asyncio
async def test_claim_documents_concurrent_exactly_once(sql_store: SqlStoreFixture) -> None:
    # The CAS + SKIP LOCKED claim must hand each document to exactly one
    # concurrent claimer — the guarantee an in-memory store cannot prove.
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        seeded = set()
        for i in range(10):
            doc = await store.upsert_document(
                tenant_id=tenant, kb_id=base.id, filename=f"d{i}.md", content=b"x"
            )
            seeded.add(doc.id)
        now = datetime.now(UTC)
        # Ample per-claimer capacity so every claimable doc (incl. unrelated
        # leftovers) is taken — what matters is each is taken exactly once.
        batches = await asyncio.gather(
            *[
                store.claim_documents_for_ingest(
                    now=now, lease_seconds=300, limit=50, max_attempts=5
                )
                for _ in range(5)
            ]
        )
        claimed = [c.document_id for batch in batches for c in batch]
        assert len(claimed) == len(set(claimed))  # no document claimed twice, anywhere
        claimed_seeded = [d for d in claimed if d in seeded]
        assert sorted(claimed_seeded) == sorted(seeded)  # each seeded doc claimed exactly once
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claim_reclaims_expired_processing(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"x"
        )
        past = datetime.now(UTC) - timedelta(seconds=10)
        first = await store.claim_document(
            tenant_id=tenant, document_id=doc.id, now=past, lease_seconds=0, max_attempts=5
        )
        assert first is not None and first.attempts == 1
        # A sweep at the present reclaims it (the lease expired = crashed worker).
        reclaimed = await store.claim_documents_for_ingest(
            now=datetime.now(UTC), lease_seconds=300, limit=50, max_attempts=5
        )
        mine = [c for c in reclaimed if c.document_id == doc.id]
        assert len(mine) == 1
        assert mine[0].attempts == 2  # reclaimed = a second attempt
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claim_respects_max_attempts(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"x"
        )
        base_time = datetime.now(UTC) - timedelta(seconds=30)
        # Each claim uses a ``now`` past the previous (0s) lease so the row is
        # re-claimable; two claims (max=2) exhaust the budget, the third fails.
        assert (
            await store.claim_document(
                tenant_id=tenant,
                document_id=doc.id,
                now=base_time,
                lease_seconds=0,
                max_attempts=2,
            )
            is not None
        )
        assert (
            await store.claim_document(
                tenant_id=tenant,
                document_id=doc.id,
                now=base_time + timedelta(seconds=10),
                lease_seconds=0,
                max_attempts=2,
            )
            is not None
        )
        assert (
            await store.claim_document(
                tenant_id=tenant,
                document_id=doc.id,
                now=base_time + timedelta(seconds=20),
                lease_seconds=0,
                max_attempts=2,
            )
            is None
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_set_status_ready_clears_lease(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"x"
        )
        await store.claim_document(
            tenant_id=tenant,
            document_id=doc.id,
            now=datetime.now(UTC),
            lease_seconds=300,
            max_attempts=5,
        )
        await store.set_document_status(
            tenant_id=tenant, document_id=doc.id, status=DocumentStatus.READY, chunk_count=1
        )
        # Terminal → lease released → even a far-future sweep never re-claims it.
        reclaimed = await store.claim_documents_for_ingest(
            now=datetime.now(UTC) + timedelta(hours=1),
            lease_seconds=300,
            limit=50,
            max_attempts=5,
        )
        assert doc.id not in {c.document_id for c in reclaimed}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_set_document_status_reports_document_presence(sql_store: SqlStoreFixture) -> None:
    """已删文档的状态写回返回 False —— 删除竞态下调用方可见"文档已不在"."""
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
        assert (
            await store.set_document_status(
                tenant_id=tenant, document_id=doc.id, status=DocumentStatus.READY, chunk_count=1
            )
            is True
        )
        assert await store.delete_document(tenant_id=tenant, document_id=doc.id) is True
        assert (
            await store.set_document_status(
                tenant_id=tenant, document_id=doc.id, status=DocumentStatus.READY
            )
            is False
        )
    finally:
        await engine.dispose()


def _make_chunk(
    tenant_id: object,
    kb_id: object,
    document_id: object,
    index: int,
    content: str,
    embedding: tuple[float, ...],
) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=uuid4(),
        tenant_id=tenant_id,  # type: ignore[arg-type]
        kb_id=kb_id,  # type: ignore[arg-type]
        document_id=document_id,  # type: ignore[arg-type]
        chunk_index=index,
        content=content,
        embedding=embedding,
    )


# ---------------------------------------------------------------------------
# L2-PR2 入库可靠性:同哈希不动 / 代际围栏(真并发)/ 终态巡检 / 续约 / 分页
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_same_hash_ready_is_unchanged_and_keeps_chunks(
    sql_store: SqlStoreFixture,
) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"v1", content_sha256="s1"
        )
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=doc.id,
            chunks=[_make_chunk(tenant, base.id, doc.id, 0, "old", _vec(1.0))],
        )
        await store.set_document_status(
            tenant_id=tenant, document_id=doc.id, status=DocumentStatus.READY, chunk_count=1
        )
        same = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"v1", content_sha256="s1"
        )
        assert same.status is DocumentStatus.READY
        assert same.chunk_count == 1
        assert same.ingest_generation == 0
        # Different bytes → reset, generation bumped, old chunks still there.
        changed = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"v2", content_sha256="s2"
        )
        assert changed.status is DocumentStatus.PENDING
        assert changed.ingest_generation == 1
        hits = await store.search(tenant_id=tenant, kb_ids=[base.id], query_embedding=_vec(1.0))
        assert [h.content for h in hits] == ["old"]
        forced = await store.upsert_document(
            tenant_id=tenant,
            kb_id=base.id,
            filename="d.md",
            content=b"v2",
            content_sha256="s2",
            force=True,
        )
        assert forced.ingest_generation == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_generation_fence_under_concurrent_writeback(sql_store: SqlStoreFixture) -> None:
    """A 认领 gen=0 → 重传 gen=1 → B 认领 gen=1;A/B 并发写回,只有 B 的落库."""
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"v1", content_sha256="s1"
        )
        now = datetime.now(UTC)
        a = await store.claim_document(
            tenant_id=tenant, document_id=doc.id, now=now, lease_seconds=300, max_attempts=5
        )
        assert a is not None and a.generation == 0
        await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"v2", content_sha256="s2"
        )
        b = await store.claim_document(
            tenant_id=tenant, document_id=doc.id, now=now, lease_seconds=300, max_attempts=5
        )
        assert b is not None and b.generation == 1

        async def _writeback(claim_generation: int, label: str) -> tuple[bool, bool]:
            wrote = await store.replace_chunks(
                tenant_id=tenant,
                document_id=doc.id,
                chunks=[_make_chunk(tenant, base.id, doc.id, 0, label, _vec(1.0))],
                generation=claim_generation,
            )
            settled = await store.set_document_status(
                tenant_id=tenant,
                document_id=doc.id,
                status=DocumentStatus.READY,
                chunk_count=1,
                generation=claim_generation,
            )
            return wrote, settled

        results = await asyncio.gather(
            _writeback(a.generation, "from-a"), _writeback(b.generation, "from-b")
        )
        assert results[0] == (False, False)
        assert results[1] == (True, True)
        fetched = await store.get_document(tenant_id=tenant, document_id=doc.id)
        assert fetched is not None
        assert fetched.status is DocumentStatus.READY
        assert fetched.attempts == 1  # _to_document now carries attempts
        hits = await store.search(tenant_id=tenant, kb_ids=[base.id], query_embedding=_vec(1.0))
        assert [h.content for h in hits] == ["from-b"]
        assert not await store.mark_document_failed_terminal(
            tenant_id=tenant, document_id=doc.id, error="stale", generation=a.generation
        )
        fetched = await store.get_document(tenant_id=tenant, document_id=doc.id)
        assert fetched is not None and fetched.error is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sweep_terminal_and_renew_lease(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        exhausted = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="x.md", content=b"x"
        )
        past = datetime.now(UTC) - timedelta(seconds=10)
        # Each claim is made strictly after the previous (0 s) lease expired.
        for step in range(3):
            assert (
                await store.claim_document(
                    tenant_id=tenant,
                    document_id=exhausted.id,
                    now=past + timedelta(seconds=step),
                    lease_seconds=0,
                    max_attempts=3,
                )
                is not None
            )
        live = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="y.md", content=b"y"
        )
        claim = await store.claim_document(
            tenant_id=tenant, document_id=live.id, now=past, lease_seconds=0, max_attempts=3
        )
        assert claim is not None
        now = datetime.now(UTC)
        # The container is shared across tests and the sweep is cross-tenant:
        # assert on this test's own document, not on an exact count.
        assert await store.sweep_terminal(now=now, max_attempts=3) >= 1
        swept = await store.get_document(tenant_id=tenant, document_id=exhausted.id)
        assert swept is not None
        assert swept.status is DocumentStatus.FAILED
        assert swept.error == "ingest attempts exhausted"
        assert await store.sweep_terminal(now=now, max_attempts=3) == 0

        # Renewing the live claim's lease keeps it away from the recovery scan.
        assert await store.renew_lease(
            tenant_id=tenant,
            document_id=live.id,
            generation=claim.generation,
            now=now,
            lease_seconds=600,
        )
        claims = await store.claim_documents_for_ingest(
            now=now, lease_seconds=60, limit=10, max_attempts=3
        )
        assert live.id not in {c.document_id for c in claims}
        assert not await store.renew_lease(
            tenant_id=tenant,
            document_id=live.id,
            generation=claim.generation + 1,
            now=now,
            lease_seconds=600,
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_documents_page_sql(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        for i in range(5):
            await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename=f"{i}.md")
        full = await store.list_documents(tenant_id=tenant, kb_id=base.id)
        page, total = await store.list_documents_page(
            tenant_id=tenant, kb_id=base.id, limit=2, offset=2
        )
        assert total == 5
        assert [d.id for d in page] == [d.id for d in full[2:4]]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_bases_page_sql(sql_store: SqlStoreFixture) -> None:
    store, engine = sql_store
    try:
        tenant = uuid4()
        for i in range(5):
            await store.create_base(tenant_id=tenant, name=f"kb-{i}")
        await store.create_base(tenant_id=uuid4(), name="foreign")
        full = await store.list_bases(tenant_id=tenant)
        page, total = await store.list_bases_page(tenant_id=tenant, limit=2, offset=2)
        assert total == 5
        assert [b.id for b in page] == [b.id for b in full[2:4]]
        everyone, all_total = await store.list_bases_all_tenants_page(limit=100, offset=0)
        assert all_total >= 6
        assert {b.id for b in full} <= {b.id for b in everyone}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_bases_pages_follow_unpaged_order_on_created_at_tie_sql(
    sql_store: SqlStoreFixture,
) -> None:
    """同一 ``created_at``(批量建库)时,分页与不分页同序 —— SQL 与内存两版的
    ``id`` 决胜方向钉在一起(单租户 id DESC、全租户 id ASC)。"""
    store, engine = sql_store
    try:
        tenant = uuid4()
        for i in range(4):
            await store.create_base(tenant_id=tenant, name=f"tie-{i}")
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE knowledge_base SET created_at = :ts"),
                {"ts": datetime(2026, 1, 1, tzinfo=UTC)},
            )
        full = await store.list_bases(tenant_id=tenant)
        assert [b.id for b in full] == sorted(
            (b.id for b in full), key=lambda u: u.int, reverse=True
        )
        first, _ = await store.list_bases_page(tenant_id=tenant, limit=2, offset=0)
        second, _ = await store.list_bases_page(tenant_id=tenant, limit=2, offset=2)
        assert [b.id for b in first + second] == [b.id for b in full]

        everyone = await store.list_bases_all_tenants()
        assert [b.id for b in everyone] == sorted((b.id for b in everyone), key=lambda u: u.int)
        page, _ = await store.list_bases_all_tenants_page(limit=2, offset=1)
        assert [b.id for b in page] == [b.id for b in everyone[1:3]]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unknown_enum_values_read_without_failing_sql(sql_store: SqlStoreFixture) -> None:
    """A newer release may write values this one does not know; after a
    rollback every document list must still load (unknown → default)."""
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"d"
        )
        stored = KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant,
            kb_id=base.id,
            document_id=doc.id,
            chunk_index=0,
            content="body",
            embedding=_vec(1.0),
        )
        await store.replace_chunks(tenant_id=tenant, document_id=doc.id, chunks=[stored])
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE knowledge_document SET stage = 'ocr', error_code = 'ocr_failed', "
                    "source_type = 'webhook' WHERE id = :id"
                ),
                {"id": doc.id},
            )
            await conn.execute(
                text("UPDATE knowledge_chunk SET chunk_type = 'image' WHERE document_id = :id"),
                {"id": doc.id},
            )
            await conn.execute(
                text("UPDATE knowledge_base SET kind = 'graph' WHERE id = :id"), {"id": base.id}
            )
        read = await store.get_document(tenant_id=tenant, document_id=doc.id)
        assert read is not None
        assert read.stage is None
        assert read.error_code is IngestErrorCode.INTERNAL_ERROR
        (chunk,) = await store.get_chunks(tenant_id=tenant, chunk_ids=[stored.id])
        assert chunk.chunk_type is ChunkType.TEXT
        # B-206 K-1: listing and recall take an allow-list of chunk types, so a
        # type only a newer release knows stays out of them.
        assert await store.list_chunks(tenant_id=tenant, document_id=doc.id) == ([], 0)
        assert await store.get_base(tenant_id=tenant, name="kb") is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_shaped_chunks_keep_working_sql(sql_store: SqlStoreFixture) -> None:
    """Chunks from before 0165 carry the breadcrumb inside ``content`` and no
    heading path / hash: their retrieval text is the content as is, keyword
    search still finds the heading words, and they are never offered for
    vector reuse."""
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="old.md", content=b"o"
        )
        legacy = KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant,
            kb_id=base.id,
            document_id=doc.id,
            chunk_index=0,
            content="[Section: 免赔额]\n\n金额是 500 元。",
            embedding=_vec(1.0),
        )
        await store.replace_chunks(tenant_id=tenant, document_id=doc.id, chunks=[legacy])
        (read,), _ = await store.list_chunks(tenant_id=tenant, document_id=doc.id)
        assert read.heading_path == ()
        assert read.retrieval_text == legacy.content
        # An empty path is SQL NULL, not a JSON ``null`` scalar (array
        # functions on a scalar raise — migration 0165's downgrade uses them).
        async with engine.begin() as conn:
            stored = (
                await conn.execute(
                    text(
                        "SELECT heading_path IS NULL, source_locator IS NULL "
                        "FROM knowledge_chunk WHERE id = :id"
                    ),
                    {"id": legacy.id},
                )
            ).one()
        assert tuple(stored) == (True, True)
        hits = await store.keyword_search_scored(tenant_id=tenant, kb_ids=[base.id], query="免赔额")
        assert [hit.chunk.id for hit in hits] == [legacy.id]
        assert (
            await store.chunk_embeddings_by_hash(
                tenant_id=tenant,
                document_id=doc.id,
                content_hashes=["anything"],
                embedding_model="qwen/text-embedding-v4",
            )
            == {}
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fence_requires_processing_status_sql(sql_store: SqlStoreFixture) -> None:
    """围栏的状态谓词(不只是代际)在 SQL 侧也钉住:已收尾的文档拒绝同代际的写回."""
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"v1"
        )
        claim = await store.claim_document(
            tenant_id=tenant,
            document_id=doc.id,
            now=datetime.now(UTC),
            lease_seconds=300,
            max_attempts=5,
        )
        assert claim is not None
        assert await store.mark_document_failed_terminal(
            tenant_id=tenant, document_id=doc.id, error="first", generation=claim.generation
        )
        chunk = _make_chunk(tenant, base.id, doc.id, 0, "late", _vec(1.0))
        assert not await store.replace_chunks(
            tenant_id=tenant, document_id=doc.id, chunks=[chunk], generation=claim.generation
        )
        assert not await store.set_document_status(
            tenant_id=tenant,
            document_id=doc.id,
            status=DocumentStatus.READY,
            chunk_count=1,
            generation=claim.generation,
        )
        assert not await store.renew_lease(
            tenant_id=tenant,
            document_id=doc.id,
            generation=claim.generation,
            now=datetime.now(UTC),
            lease_seconds=60,
        )
        fetched = await store.get_document(tenant_id=tenant, document_id=doc.id)
        assert fetched is not None and fetched.status is DocumentStatus.FAILED
        # Re-index fence: READY + current generation only.
        await store.set_document_status(
            tenant_id=tenant, document_id=doc.id, status=DocumentStatus.READY, chunk_count=0
        )
        assert await store.replace_chunks(
            tenant_id=tenant,
            document_id=doc.id,
            chunks=[chunk],
            generation=0,
            expect_status=DocumentStatus.READY,
        )
        assert not await store.replace_chunks(
            tenant_id=tenant,
            document_id=doc.id,
            chunks=[chunk],
            generation=1,
            expect_status=DocumentStatus.READY,
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pending_slot_failure_keeps_serving_and_ready_promotes_sql(
    sql_store: SqlStoreFixture,
) -> None:
    """重传进 pending;失败 → v1 原件 + 分段仍在服务;成功 → pending 提升为 content;可丢弃."""
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"v1", content_sha256="s1"
        )
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=doc.id,
            chunks=[_make_chunk(tenant, base.id, doc.id, 0, "old", _vec(1.0))],
        )
        await store.set_document_status(
            tenant_id=tenant, document_id=doc.id, status=DocumentStatus.READY, chunk_count=1
        )
        # v2 → pending slot; v1 stays serving.
        reset = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"v2", content_sha256="s2"
        )
        assert reset.has_pending and reset.chunk_count == 1
        assert await store.get_document_content(tenant_id=tenant, document_id=doc.id) == b"v1"
        claim = await store.claim_document(
            tenant_id=tenant,
            document_id=doc.id,
            now=datetime.now(UTC),
            lease_seconds=300,
            max_attempts=5,
        )
        assert claim is not None
        assert claim.content == b"v2" and claim.content_sha256 == "s2"
        # v2 fails: nothing moves, v1 still answers.
        assert await store.mark_document_failed_terminal(
            tenant_id=tenant, document_id=doc.id, error="parse failed", generation=claim.generation
        )
        fetched = await store.get_document(tenant_id=tenant, document_id=doc.id)
        assert fetched is not None
        assert fetched.status is DocumentStatus.FAILED and fetched.has_pending
        assert fetched.chunk_count == 1
        hits = await store.search(tenant_id=tenant, kb_ids=[base.id], query_embedding=_vec(1.0))
        assert [h.content for h in hits] == ["old"]
        assert await store.get_document_content(tenant_id=tenant, document_id=doc.id) == b"v1"
        # Discard v2 → back to READY on v1, generation bumped.
        restored = await store.discard_pending(tenant_id=tenant, document_id=doc.id)
        assert restored is not None
        assert restored.status is DocumentStatus.READY and not restored.has_pending
        assert restored.ingest_generation == claim.generation + 1
        # Upload v2 again, ingest it successfully: READY promotes pending → content.
        await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"v2", content_sha256="s2"
        )
        claim2 = await store.claim_document(
            tenant_id=tenant,
            document_id=doc.id,
            now=datetime.now(UTC),
            lease_seconds=300,
            max_attempts=5,
        )
        assert claim2 is not None and claim2.content == b"v2"
        assert await store.replace_chunks(
            tenant_id=tenant,
            document_id=doc.id,
            chunks=[_make_chunk(tenant, base.id, doc.id, 0, "new", _vec(1.0))],
            generation=claim2.generation,
        )
        assert await store.set_document_status(
            tenant_id=tenant,
            document_id=doc.id,
            status=DocumentStatus.READY,
            chunk_count=1,
            generation=claim2.generation,
        )
        promoted = await store.get_document(tenant_id=tenant, document_id=doc.id)
        assert promoted is not None
        assert promoted.status is DocumentStatus.READY and not promoted.has_pending
        assert await store.get_document_content(tenant_id=tenant, document_id=doc.id) == b"v2"
        same = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"v2", content_sha256="s2"
        )
        assert same.status is DocumentStatus.READY  # v2 is the serving hash now
        hits = await store.search(tenant_id=tenant, kb_ids=[base.id], query_embedding=_vec(1.0))
        assert [h.content for h in hits] == ["new"]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# B-206 K-1 父段行(两个后端共有的契约见 test_knowledge_parent_chunks_contract.py)
# ---------------------------------------------------------------------------


def _parent_chunk(
    tenant_id: object, kb_id: object, document_id: object, index: int, content: str
) -> KnowledgeChunk:
    return _make_chunk(tenant_id, kb_id, document_id, index, content, _vec(1.0)).model_copy(
        update={"chunk_type": ChunkType.PARENT, "embedding": None}
    )


@pytest.mark.asyncio
async def test_parent_rows_are_written_first_without_any_vector(
    sql_store: SqlStoreFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parent rows go in before the chunks pointing at them, and carry neither
    a vector nor a keyword vector; the children keep both."""
    built: list[ChunkType] = []
    real_chunk_row = sql_module._chunk_row

    def _recording(chunk: KnowledgeChunk) -> KnowledgeChunkRow:
        built.append(chunk.chunk_type)
        return real_chunk_row(chunk)

    monkeypatch.setattr(sql_module, "_chunk_row", _recording)
    monkeypatch.setattr(sql_module, "_CHUNK_INSERT_BATCH", 2)
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.md")
        parents = [_parent_chunk(tenant, base.id, doc.id, i, f"parent {i}") for i in range(2)]
        children = [
            _make_chunk(tenant, base.id, doc.id, i, f"child {i}", _vec(1.0, 0.1 * i)).model_copy(
                update={"parent_chunk_id": parents[i // 2].id}
            )
            for i in range(3)
        ]
        await store.replace_chunks(
            tenant_id=tenant, document_id=doc.id, chunks=[*parents, *children]
        )
        assert built == [ChunkType.PARENT] * 2 + [ChunkType.TEXT] * 3
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT chunk_type, chunk_index, embedding IS NULL, content_tsv IS NULL "
                        "FROM knowledge_chunk WHERE document_id = :id "
                        "ORDER BY chunk_type, chunk_index"
                    ),
                    {"id": doc.id},
                )
            ).all()
        assert [tuple(row) for row in rows] == [
            ("parent", 0, True, True),
            ("parent", 1, True, True),
            ("text", 0, False, False),
            ("text", 1, False, False),
            ("text", 2, False, False),
        ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_parent_row_with_a_keyword_vector_is_still_never_recalled(
    sql_store: SqlStoreFixture,
) -> None:
    """Recall filters by chunk type, not by the absence of a keyword vector:
    a parent row given one by hand (an older writer, a manual fix) neither
    comes back nor counts toward BM25's N."""
    store, engine = sql_store
    try:
        tenant = uuid4()
        base = await store.create_base(tenant_id=tenant, name="kb")
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.md")
        texts = ["苹果 沙拉", "苹果 派", "香蕉 甜点", "面包 牛奶"]
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=doc.id,
            chunks=[
                *(
                    _make_chunk(tenant, base.id, doc.id, i, t, _vec(1.0))
                    for i, t in enumerate(texts)
                ),
                _parent_chunk(tenant, base.id, doc.id, 0, "苹果 香蕉"),
                _parent_chunk(tenant, base.id, doc.id, 1, "苹果 香蕉 苹果 香蕉"),
            ],
        )
        before = await store.keyword_search_scored(
            tenant_id=tenant, kb_ids=[base.id], query="苹果和香蕉", limit=10
        )
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE knowledge_chunk SET content_tsv = to_tsvector('simple', content) "
                    "WHERE document_id = :id AND chunk_type = 'parent'"
                ),
                {"id": doc.id},
            )
        after = await store.keyword_search_scored(
            tenant_id=tenant, kb_ids=[base.id], query="苹果和香蕉", limit=10
        )
        assert {hit.chunk.chunk_type for hit in after} == {ChunkType.TEXT}
        assert {hit.chunk.content: hit.score for hit in after} == pytest.approx(
            {hit.chunk.content: hit.score for hit in before}, abs=1e-9
        )
        assert len(after) == 3
    finally:
        await engine.dispose()


def test_a_full_row_without_a_vector_reads_as_a_parent_with_none() -> None:
    """Every full-row read builds its DTO the same way; a parent row's NULL
    vector reads as ``None`` instead of failing."""
    tenant, kb_id, document_id = uuid4(), uuid4(), uuid4()
    row = KnowledgeChunkRow(
        id=uuid4(),
        tenant_id=tenant,
        kb_id=kb_id,
        document_id=document_id,
        chunk_index=0,
        content="parent",
        embedding=None,
        chunk_type="parent",
        enabled=True,
        edited=False,
        revision=1,
    )
    chunk = sql_module._to_chunk_with_embedding(row)
    assert (chunk.chunk_type, chunk.embedding) == (ChunkType.PARENT, None)
