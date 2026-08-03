"""Integration tests for SqlKnowledgeStore against Postgres + pgvector — J.5."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge import DuplicateKnowledgeBaseError
from expert_work.persistence.models import KnowledgeBaseRow
from expert_work.protocol import DocumentStatus, KnowledgeChunk, RetrievalMethod

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
        assert reset.chunk_count == 0
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
        assert len(hits[0].embedding) == EMBEDDING_DIM
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
    """W4 review C-3 — ``created_at`` ties order by ``id`` ASC. The two rows'
    ``created_at`` are equalized via raw UPDATEs issued higher-id first, so
    the live heap tuples end up in *descending* id order — a dropped tiebreak
    degrades to heap-scan order and the exact-order assertion goes red."""
    store, engine = sql_store
    try:
        tenant_a, tenant_b = uuid4(), uuid4()
        base_1 = await store.create_base(tenant_id=tenant_a, name="tie-1")
        base_2 = await store.create_base(tenant_id=tenant_b, name="tie-2")
        lo, hi = sorted((base_1, base_2), key=lambda b: b.id.int)
        ts = datetime(2035, 1, 1, 12, 0, tzinfo=UTC)
        async with engine.begin() as conn:
            for kb in (hi, lo):  # hi first → live tuples land id-descending
                await conn.execute(
                    update(KnowledgeBaseRow)
                    .where(KnowledgeBaseRow.id == kb.id)
                    .values(created_at=ts)
                )

        rows = await store.list_bases_all_tenants(limit=500)
        assert [b.id for b in rows if b.tenant_id in {tenant_a, tenant_b}] == [lo.id, hi.id]
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
