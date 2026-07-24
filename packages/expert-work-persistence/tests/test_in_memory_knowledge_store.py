"""Unit tests for ``InMemoryKnowledgeStore`` — Stream J.5."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from expert_work.persistence.knowledge import (
    DuplicateKnowledgeBaseError,
    InMemoryKnowledgeStore,
)
from expert_work.protocol import DocumentStatus, KnowledgeChunk, RetrievalMethod


def _chunk(
    *,
    tenant_id: UUID,
    kb_id: UUID,
    document_id: UUID,
    index: int,
    content: str,
    embedding: tuple[float, ...],
) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=uuid4(),
        tenant_id=tenant_id,
        kb_id=kb_id,
        document_id=document_id,
        chunk_index=index,
        content=content,
        embedding=embedding,
    )


# ---------------------------------------------------------------------------
# knowledge bases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_base() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    created = await store.create_base(tenant_id=tenant, name="hr-policies")
    fetched = await store.get_base(tenant_id=tenant, name="hr-policies")
    assert fetched is not None
    assert fetched.id == created.id


@pytest.mark.asyncio
async def test_create_base_persists_chunk_params() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    default = await store.create_base(tenant_id=tenant, name="default")
    assert (default.chunk_max_tokens, default.chunk_overlap_tokens) == (512, 64)
    tuned = await store.create_base(
        tenant_id=tenant, name="tuned", chunk_max_tokens=256, chunk_overlap_tokens=16
    )
    assert (tuned.chunk_max_tokens, tuned.chunk_overlap_tokens) == (256, 16)
    fetched = await store.get_base(tenant_id=tenant, name="tuned")
    assert fetched is not None
    assert fetched.chunk_max_tokens == 256


@pytest.mark.asyncio
async def test_create_base_persists_metadata_and_retrieval_config() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    created = await store.create_base(
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
    assert created.updated_at is not None


@pytest.mark.asyncio
async def test_update_base_patches_supplied_fields_only() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(
        tenant_id=tenant, name="kb", description="orig", retrieval_top_k=5
    )
    updated = await store.update_base(
        tenant_id=tenant,
        kb_id=base.id,
        retrieval_top_k=12,
        retrieval_method=RetrievalMethod.KEYWORD,
    )
    assert updated is not None
    assert updated.retrieval_top_k == 12
    assert updated.retrieval_method is RetrievalMethod.KEYWORD
    # Untouched fields preserved.
    assert updated.description == "orig"


@pytest.mark.asyncio
async def test_update_base_clear_vs_omit_nullable() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(
        tenant_id=tenant, name="kb", description="orig", retrieval_score_threshold=0.5
    )
    # Omitting description leaves it; clearing the threshold with explicit None.
    updated = await store.update_base(
        tenant_id=tenant, kb_id=base.id, retrieval_score_threshold=None
    )
    assert updated is not None
    assert updated.description == "orig"  # omitted → unchanged
    assert updated.retrieval_score_threshold is None  # explicit None → cleared
    # Now clear description explicitly.
    cleared = await store.update_base(tenant_id=tenant, kb_id=base.id, description=None)
    assert cleared is not None
    assert cleared.description is None


@pytest.mark.asyncio
async def test_update_missing_base_returns_none() -> None:
    store = InMemoryKnowledgeStore()
    assert await store.update_base(tenant_id=uuid4(), kb_id=uuid4(), description="x") is None


@pytest.mark.asyncio
async def test_base_stats_counts_documents_and_chunks() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    other = await store.create_base(tenant_id=tenant, name="other")
    d1 = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="a.pdf")
    d2 = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="b.pdf")
    await store.set_document_status(
        tenant_id=tenant, document_id=d1.id, status=DocumentStatus.READY, chunk_count=3
    )
    await store.set_document_status(
        tenant_id=tenant, document_id=d2.id, status=DocumentStatus.READY, chunk_count=4
    )
    assert await store.base_stats(tenant_id=tenant, kb_id=base.id) == (2, 7)
    assert await store.base_stats(tenant_id=tenant, kb_id=other.id) == (0, 0)
    many = await store.base_stats_many(tenant_id=tenant)
    assert many[base.id] == (2, 7)
    # Bases with no documents are simply absent from the map.
    assert other.id not in many


@pytest.mark.asyncio
async def test_stamp_embedding_and_reindex_flag() -> None:
    store = InMemoryKnowledgeStore()
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
    # Missing base → request_reindex returns False.
    assert await store.request_reindex(tenant_id=tenant, kb_id=uuid4()) is False


@pytest.mark.asyncio
async def test_create_base_rejects_duplicate() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    await store.create_base(tenant_id=tenant, name="kb")
    with pytest.raises(DuplicateKnowledgeBaseError):
        await store.create_base(tenant_id=tenant, name="kb")


@pytest.mark.asyncio
async def test_get_base_is_tenant_scoped() -> None:
    store = InMemoryKnowledgeStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    await store.create_base(tenant_id=tenant_a, name="kb")
    assert await store.get_base(tenant_id=tenant_b, name="kb") is None
    # Same name under a different tenant is allowed.
    await store.create_base(tenant_id=tenant_b, name="kb")


@pytest.mark.asyncio
async def test_list_bases_scoped_to_tenant() -> None:
    store = InMemoryKnowledgeStore()
    tenant, other = uuid4(), uuid4()
    await store.create_base(tenant_id=tenant, name="a")
    await store.create_base(tenant_id=tenant, name="b")
    await store.create_base(tenant_id=other, name="c")
    assert {b.name for b in await store.list_bases(tenant_id=tenant)} == {"a", "b"}


@pytest.mark.asyncio
async def test_delete_base_cascades_documents_and_chunks() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc.id,
        chunks=[
            _chunk(
                tenant_id=tenant,
                kb_id=base.id,
                document_id=doc.id,
                index=0,
                content="c",
                embedding=(1.0,),
            )
        ],
    )

    assert await store.delete_base(tenant_id=tenant, kb_id=base.id) is True
    assert await store.get_base(tenant_id=tenant, name="kb") is None
    assert await store.list_documents(tenant_id=tenant, kb_id=base.id) == []
    assert await store.search(tenant_id=tenant, kb_ids=[base.id], query_embedding=(1.0,)) == []


@pytest.mark.asyncio
async def test_delete_missing_base_returns_false() -> None:
    store = InMemoryKnowledgeStore()
    assert await store.delete_base(tenant_id=uuid4(), kb_id=uuid4()) is False


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_document_creates_pending() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.pdf")
    assert doc.status is DocumentStatus.PENDING
    assert doc.chunk_count == 0


@pytest.mark.asyncio
async def test_upsert_existing_document_resets_and_clears_chunks() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.pdf")
    await store.set_document_status(
        tenant_id=tenant, document_id=doc.id, status=DocumentStatus.READY, chunk_count=3
    )
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc.id,
        chunks=[
            _chunk(
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=doc.id,
                index=0,
                content="c",
                embedding=(1.0,),
            )
        ],
    )

    reset = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.pdf")
    assert reset.id == doc.id  # same logical document
    assert reset.status is DocumentStatus.PENDING
    assert reset.chunk_count == 0
    assert await store.search(tenant_id=tenant, kb_ids=[kb_id], query_embedding=(1.0,)) == []


@pytest.mark.asyncio
async def test_upsert_retains_bytes_and_get_content() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"hello"
    )
    assert await store.get_document_content(tenant_id=tenant, document_id=doc.id) == b"hello"
    # Cross-tenant isolation.
    assert await store.get_document_content(tenant_id=uuid4(), document_id=doc.id) is None


@pytest.mark.asyncio
async def test_claim_document_cas_and_terminal_release() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    doc = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="d.md", content=b"x"
    )
    now = datetime.now(UTC)
    claim = await store.claim_document(
        tenant_id=tenant, document_id=doc.id, now=now, lease_seconds=300, max_attempts=5
    )
    assert claim is not None
    assert claim.content == b"x"
    assert claim.attempts == 1
    assert claim.chunk_max_tokens == base.chunk_max_tokens
    # A second claim while the lease is live returns None (already held).
    assert (
        await store.claim_document(
            tenant_id=tenant, document_id=doc.id, now=now, lease_seconds=300, max_attempts=5
        )
        is None
    )
    # Reaching a terminal state releases the lease (not re-claimed).
    await store.mark_document_failed_terminal(tenant_id=tenant, document_id=doc.id, error="boom")
    fetched = await store.get_document(tenant_id=tenant, document_id=doc.id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.FAILED


@pytest.mark.asyncio
async def test_claim_documents_for_ingest_batches_claimable() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    for i in range(3):
        await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename=f"d{i}.md", content=b"x"
        )
    claims = await store.claim_documents_for_ingest(
        now=datetime.now(UTC), lease_seconds=300, limit=10, max_attempts=5
    )
    assert len(claims) == 3
    # All now claimed (processing + live lease) → a second sweep finds none.
    assert (
        await store.claim_documents_for_ingest(
            now=datetime.now(UTC), lease_seconds=300, limit=10, max_attempts=5
        )
        == []
    )


@pytest.mark.asyncio
async def test_set_document_status_failed_records_error() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.pdf")
    await store.set_document_status(
        tenant_id=tenant,
        document_id=doc.id,
        status=DocumentStatus.FAILED,
        error="parse error",
    )
    fetched = await store.get_document(tenant_id=tenant, document_id=doc.id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.FAILED
    assert fetched.error == "parse error"


@pytest.mark.asyncio
async def test_list_documents_scoped_to_base() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_a, kb_b = uuid4(), uuid4(), uuid4()
    await store.upsert_document(tenant_id=tenant, kb_id=kb_a, filename="a.pdf")
    await store.upsert_document(tenant_id=tenant, kb_id=kb_b, filename="b.pdf")
    docs = await store.list_documents(tenant_id=tenant, kb_id=kb_a)
    assert [d.filename for d in docs] == ["a.pdf"]


@pytest.mark.asyncio
async def test_delete_document_cascades_chunks() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.pdf")
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc.id,
        chunks=[
            _chunk(
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=doc.id,
                index=0,
                content="c",
                embedding=(1.0,),
            )
        ],
    )
    assert await store.delete_document(tenant_id=tenant, document_id=doc.id) is True
    assert await store.get_document(tenant_id=tenant, document_id=doc.id) is None
    assert await store.search(tenant_id=tenant, kb_ids=[kb_id], query_embedding=(1.0,)) == []


@pytest.mark.asyncio
async def test_set_document_status_reports_document_presence() -> None:
    """已删文档的状态写回返回 False —— 与 SQL 侧 rowcount 语义平价."""
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.pdf")
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


@pytest.mark.asyncio
async def test_replace_chunks_after_document_delete_raises_and_leaves_no_orphans() -> None:
    """镜像 SQL 侧 FK(0136):已删文档的写回抛 KeyError,不得重建孤儿 chunk.

    与 ``test_knowledge_chunk_fk.py`` 的
    ``test_replace_chunks_after_delete_raises_and_leaves_no_orphans`` 同型。
    """
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.pdf")
    assert await store.delete_document(tenant_id=tenant, document_id=doc.id) is True
    with pytest.raises(KeyError):
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=doc.id,
            chunks=[
                _chunk(
                    tenant_id=tenant,
                    kb_id=kb_id,
                    document_id=doc.id,
                    index=0,
                    content="stale write-back",
                    embedding=(1.0,),
                )
            ],
        )
    assert await store.search(tenant_id=tenant, kb_ids=[kb_id], query_embedding=(1.0,)) == []


# ---------------------------------------------------------------------------
# chunk search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_orders_by_cosine_distance() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc_id = (await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.pdf")).id
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc_id,
        chunks=[
            _chunk(
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=doc_id,
                index=0,
                content="east",
                embedding=(1.0, 0.0),
            ),
            _chunk(
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=doc_id,
                index=1,
                content="north",
                embedding=(0.0, 1.0),
            ),
            _chunk(
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=doc_id,
                index=2,
                content="ne",
                embedding=(0.7, 0.7),
            ),
        ],
    )
    hits = await store.search(tenant_id=tenant, kb_ids=[kb_id], query_embedding=(1.0, 0.0), limit=3)
    assert [h.content for h in hits] == ["east", "ne", "north"]


@pytest.mark.asyncio
async def test_search_filters_by_kb_ids() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_a, kb_b = uuid4(), uuid4(), uuid4()
    doc = (await store.upsert_document(tenant_id=tenant, kb_id=kb_a, filename="d.pdf")).id
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc,
        chunks=[
            _chunk(
                tenant_id=tenant,
                kb_id=kb_a,
                document_id=doc,
                index=0,
                content="in-a",
                embedding=(1.0,),
            ),
            _chunk(
                tenant_id=tenant,
                kb_id=kb_b,
                document_id=doc,
                index=1,
                content="in-b",
                embedding=(1.0,),
            ),
        ],
    )
    hits = await store.search(tenant_id=tenant, kb_ids=[kb_a], query_embedding=(1.0,))
    assert [h.content for h in hits] == ["in-a"]


@pytest.mark.asyncio
async def test_search_is_tenant_scoped() -> None:
    store = InMemoryKnowledgeStore()
    tenant, other, kb_id = uuid4(), uuid4(), uuid4()
    doc = (await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.pdf")).id
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc,
        chunks=[
            _chunk(
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=doc,
                index=0,
                content="mine",
                embedding=(1.0,),
            )
        ],
    )
    assert await store.search(tenant_id=other, kb_ids=[kb_id], query_embedding=(1.0,)) == []


@pytest.mark.asyncio
async def test_search_empty_kb_ids_returns_empty() -> None:
    store = InMemoryKnowledgeStore()
    assert await store.search(tenant_id=uuid4(), kb_ids=[], query_embedding=(1.0,)) == []


# ---------------------------------------------------------------------------
# keyword search (hybrid-search keyword side)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keyword_search_ranks_by_term_overlap() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = (await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.pdf")).id
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc,
        chunks=[
            _chunk(
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=doc,
                index=0,
                content="the quick brown fox",
                embedding=(1.0,),
            ),
            _chunk(
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=doc,
                index=1,
                content="a quick fox jumps",
                embedding=(1.0,),
            ),
            _chunk(
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=doc,
                index=2,
                content="lazy sleeping dog",
                embedding=(1.0,),
            ),
        ],
    )
    hits = await store.keyword_search(tenant_id=tenant, kb_ids=[kb_id], query="quick fox", limit=5)
    # Both query terms hit chunk 0 and 1; chunk 2 has neither.
    assert {h.chunk_index for h in hits} == {0, 1}


@pytest.mark.asyncio
async def test_keyword_search_filters_by_kb_and_tenant() -> None:
    store = InMemoryKnowledgeStore()
    tenant, other, kb_a, kb_b = uuid4(), uuid4(), uuid4(), uuid4()
    doc = (await store.upsert_document(tenant_id=tenant, kb_id=kb_a, filename="d.pdf")).id
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc,
        chunks=[
            _chunk(
                tenant_id=tenant,
                kb_id=kb_a,
                document_id=doc,
                index=0,
                content="invoice payment terms",
                embedding=(1.0,),
            ),
            _chunk(
                tenant_id=tenant,
                kb_id=kb_b,
                document_id=doc,
                index=1,
                content="invoice payment terms",
                embedding=(1.0,),
            ),
        ],
    )
    in_a = await store.keyword_search(tenant_id=tenant, kb_ids=[kb_a], query="invoice")
    assert [h.chunk_index for h in in_a] == [0]
    assert await store.keyword_search(tenant_id=other, kb_ids=[kb_a], query="invoice") == []


@pytest.mark.asyncio
async def test_keyword_search_no_match_returns_empty() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = (await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.pdf")).id
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc,
        chunks=[
            _chunk(
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=doc,
                index=0,
                content="quarterly financial report",
                embedding=(1.0,),
            )
        ],
    )
    assert await store.keyword_search(tenant_id=tenant, kb_ids=[kb_id], query="unrelated") == []


@pytest.mark.asyncio
async def test_keyword_search_empty_kb_ids_returns_empty() -> None:
    store = InMemoryKnowledgeStore()
    assert await store.keyword_search(tenant_id=uuid4(), kb_ids=[], query="x") == []


# ---------------------------------------------------------------------------
# scored search + chunk preview (commercial uplift)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_scored_returns_descending_similarity() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = (await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.pdf")).id
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc,
        chunks=[
            _chunk(
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=doc,
                index=0,
                content="near",
                embedding=(1.0, 0.0),
            ),
            _chunk(
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=doc,
                index=1,
                content="far",
                embedding=(0.0, 1.0),
            ),
        ],
    )
    hits = await store.search_scored(tenant_id=tenant, kb_ids=[kb_id], query_embedding=(1.0, 0.0))
    assert [h.chunk.content for h in hits] == ["near", "far"]
    assert all(h.source == "vector" for h in hits)
    # Identical vector → similarity ~1.0; orthogonal → ~0.0.
    assert hits[0].score == pytest.approx(1.0)
    assert hits[1].score == pytest.approx(0.0, abs=1e-9)
    # Scores are monotonically non-increasing.
    assert hits[0].score >= hits[1].score


@pytest.mark.asyncio
async def test_keyword_search_scored_carries_rank_and_source() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = (await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.pdf")).id
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc,
        chunks=[
            _chunk(
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=doc,
                index=0,
                content="quick brown fox",
                embedding=(1.0,),
            )
        ],
    )
    hits = await store.keyword_search_scored(tenant_id=tenant, kb_ids=[kb_id], query="quick fox")
    assert len(hits) == 1
    assert hits[0].source == "keyword"
    assert hits[0].score > 0


@pytest.mark.asyncio
async def test_list_chunks_paginates_and_omits_embedding() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = (await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.pdf")).id
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc,
        chunks=[
            _chunk(
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=doc,
                index=i,
                content=f"chunk-{i}",
                embedding=(float(i),),
            )
            for i in range(5)
        ],
    )
    page, total = await store.list_chunks(tenant_id=tenant, document_id=doc, offset=1, limit=2)
    assert total == 5
    assert [c.chunk_index for c in page] == [1, 2]
    # Embedding omitted from preview rows.
    assert all(c.embedding == () for c in page)


@pytest.mark.asyncio
async def test_list_chunks_is_tenant_scoped() -> None:
    store = InMemoryKnowledgeStore()
    tenant, other, kb_id = uuid4(), uuid4(), uuid4()
    doc = (await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.pdf")).id
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc,
        chunks=[
            _chunk(
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=doc,
                index=0,
                content="mine",
                embedding=(1.0,),
            )
        ],
    )
    page, total = await store.list_chunks(tenant_id=other, document_id=doc)
    assert (page, total) == ([], 0)


@pytest.mark.asyncio
async def test_get_documents_batches_and_scopes() -> None:
    store = InMemoryKnowledgeStore()
    tenant, other = uuid4(), uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    d1 = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="a.pdf")
    d2 = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="b.pdf")
    other_base = await store.create_base(tenant_id=other, name="kb")
    d3 = await store.upsert_document(tenant_id=other, kb_id=other_base.id, filename="c.pdf")
    missing = uuid4()
    got = await store.get_documents(tenant_id=tenant, document_ids=[d1.id, d2.id, d3.id, missing])
    # d3 (other tenant) + the missing id are absent; never reveals cross-tenant.
    assert set(got) == {d1.id, d2.id}
    assert got[d1.id].filename == "a.pdf"


@pytest.mark.asyncio
async def test_get_documents_empty_input_returns_empty() -> None:
    store = InMemoryKnowledgeStore()
    assert await store.get_documents(tenant_id=uuid4(), document_ids=[]) == {}
