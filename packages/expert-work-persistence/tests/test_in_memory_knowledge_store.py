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
    assert (default.chunk_max_tokens, default.chunk_overlap_tokens) == (400, 0)
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
async def test_rerank_score_threshold_round_trip() -> None:
    """B-175 wave 2 PR3 — stored at create; an update that omits it keeps it,
    an explicit None clears it."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb", rerank_score_threshold=0.7)
    assert base.rerank_score_threshold == 0.7
    kept = await store.update_base(tenant_id=tenant, kb_id=base.id, retrieval_top_k=9)
    assert kept is not None
    assert kept.rerank_score_threshold == 0.7
    changed = await store.update_base(tenant_id=tenant, kb_id=base.id, rerank_score_threshold=0.6)
    assert changed is not None
    assert changed.rerank_score_threshold == 0.6
    cleared = await store.update_base(tenant_id=tenant, kb_id=base.id, rerank_score_threshold=None)
    assert cleared is not None
    assert cleared.rerank_score_threshold is None


@pytest.mark.asyncio
async def test_rerank_threshold_model_round_trip() -> None:
    """B-206 K-7b — the model a threshold was set under: stored at create, kept
    by an update that omits it (a threshold change alone does not touch it),
    cleared by an explicit None."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(
        tenant_id=tenant,
        name="kb",
        rerank_score_threshold=0.7,
        rerank_threshold_model="qwen/qwen3-vl-rerank",
    )
    assert base.rerank_threshold_model == "qwen/qwen3-vl-rerank"
    kept = await store.update_base(tenant_id=tenant, kb_id=base.id, rerank_score_threshold=0.6)
    assert kept is not None
    assert kept.rerank_threshold_model == "qwen/qwen3-vl-rerank"
    moved = await store.update_base(
        tenant_id=tenant, kb_id=base.id, rerank_threshold_model="qwen/qwen3-rerank"
    )
    assert moved is not None
    assert moved.rerank_threshold_model == "qwen/qwen3-rerank"
    cleared = await store.update_base(tenant_id=tenant, kb_id=base.id, rerank_threshold_model=None)
    assert cleared is not None
    assert cleared.rerank_threshold_model is None


@pytest.mark.asyncio
async def test_update_missing_base_returns_none() -> None:
    store = InMemoryKnowledgeStore()
    assert await store.update_base(tenant_id=uuid4(), kb_id=uuid4(), description="x") is None


@pytest.mark.asyncio
async def test_list_bases_all_tenants_id_tiebreak_and_cap() -> None:
    """W4 review C-3/C-5 — ``created_at`` ties order by ``id`` ASC (``.int``)
    and the aggregate is capped at ``limit``. ``create_base`` always stamps
    ``now()``, so ties are seeded white-box (precedent:
    ``test_in_memory_memory_consolidator.py``) in *descending* id order — a
    dropped tiebreak degrades to seed order and the assertion goes red."""
    store = InMemoryKnowledgeStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    base_1 = await store.create_base(tenant_id=tenant_a, name="tie-1")
    base_2 = await store.create_base(tenant_id=tenant_b, name="tie-2")
    ts = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    lo, hi = sorted(
        (b.model_copy(update={"created_at": ts}) for b in (base_1, base_2)),
        key=lambda b: b.id.int,
    )
    store._bases[:] = [hi, lo]  # descending id seed

    assert [b.id for b in await store.list_bases_all_tenants()] == [lo.id, hi.id]
    # C-5 — the aggregate is bounded.
    assert [b.id for b in await store.list_bases_all_tenants(limit=1)] == [lo.id]


@pytest.mark.asyncio
async def test_base_stats_many_all_tenants_bounded_to_kb_ids() -> None:
    """W4 review C-5 — the aggregate stats reader only aggregates the
    ``kb_ids`` the (capped) base page returned."""
    store = InMemoryKnowledgeStore()
    tenant_a, tenant_b = uuid4(), uuid4()
    base_a = await store.create_base(tenant_id=tenant_a, name="kb-a")
    base_b = await store.create_base(tenant_id=tenant_b, name="kb-b")
    for base, tenant in ((base_a, tenant_a), (base_b, tenant_b)):
        doc = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
        await store.set_document_status(
            tenant_id=tenant, document_id=doc.id, status=DocumentStatus.READY, chunk_count=2
        )

    stats = await store.base_stats_many_all_tenants(kb_ids=[base_a.id])
    assert stats == {base_a.id: (1, 2)}  # base_b excluded — outside the page
    assert await store.base_stats_many_all_tenants(kb_ids=[]) == {}


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
async def test_count_documents_capped() -> None:
    """B-175 P4 — the knowledge catalog's per-base document count: only the
    asked bases, each stopped at ``cap``; another tenant's documents never count,
    even when its base id is passed. Every asked id is in the answer."""
    store = InMemoryKnowledgeStore()
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
    assert await store.count_documents_capped(tenant_id=tenant, kb_ids=[a.id], cap=10) == {a.id: 3}
    assert await store.count_documents_capped(tenant_id=tenant, kb_ids=[], cap=10) == {}


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
async def test_upsert_existing_document_resets_and_keeps_chunks_until_replaced() -> None:
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
    assert reset.chunk_count == 3  # the serving version's count stays until replaced
    # L2-PR2:重传不再立刻删分段 —— 旧分段一直可检索,直到新分段整体替换。
    hits = await store.search(tenant_id=tenant, kb_ids=[kb_id], query_embedding=(1.0,))
    assert [h.content for h in hits] == ["c"]
    assert reset.ingest_generation == doc.ingest_generation + 1


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


# ---------------------------------------------------------------------------
# L2-PR2 入库可靠性:同哈希不动 / 代际围栏 / 终态巡检 / 续约 / 分页
# ---------------------------------------------------------------------------


async def _ready_document(
    store: InMemoryKnowledgeStore, *, content: bytes = b"v1", sha: str = "sha-v1"
) -> tuple[UUID, UUID, UUID]:
    """One READY document with one chunk; returns (tenant, kb, document)."""
    tenant, kb_id = uuid4(), uuid4()
    doc = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="d.md", content=content, content_sha256=sha
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
                content="old",
                embedding=(1.0,),
            )
        ],
    )
    await store.set_document_status(
        tenant_id=tenant, document_id=doc.id, status=DocumentStatus.READY, chunk_count=1
    )
    return tenant, kb_id, doc.id


@pytest.mark.asyncio
async def test_upsert_same_hash_on_ready_document_is_unchanged() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id, doc_id = await _ready_document(store)
    before = await store.get_document(tenant_id=tenant, document_id=doc_id)
    assert before is not None

    same = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"v1", content_sha256="sha-v1"
    )
    assert same.status is DocumentStatus.READY  # the "unchanged" signal
    assert same.chunk_count == 1
    assert same.ingest_generation == before.ingest_generation
    hits = await store.search(tenant_id=tenant, kb_ids=[kb_id], query_embedding=(1.0,))
    assert [h.content for h in hits] == ["old"]


@pytest.mark.asyncio
async def test_upsert_same_hash_with_force_resets() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id, doc_id = await _ready_document(store)
    forced = await store.upsert_document(
        tenant_id=tenant,
        kb_id=kb_id,
        filename="d.md",
        content=b"v1",
        content_sha256="sha-v1",
        force=True,
    )
    assert forced.id == doc_id
    assert forced.status is DocumentStatus.PENDING
    assert forced.ingest_generation == 1


@pytest.mark.asyncio
async def test_upsert_same_hash_on_failed_document_resets() -> None:
    # Only READY + same hash is "unchanged"; a failed document with the same
    # bytes must be re-driven.
    store = InMemoryKnowledgeStore()
    tenant, kb_id, doc_id = await _ready_document(store)
    await store.set_document_status(
        tenant_id=tenant, document_id=doc_id, status=DocumentStatus.FAILED, error="boom"
    )
    again = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"v1", content_sha256="sha-v1"
    )
    assert again.status is DocumentStatus.PENDING


@pytest.mark.asyncio
async def test_claim_carries_generation() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"x")
    claim = await store.claim_document(
        tenant_id=tenant, document_id=doc.id, now=_now(), lease_seconds=60, max_attempts=5
    )
    assert claim is not None
    assert claim.generation == 0
    await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"y")
    second = await store.claim_document(
        tenant_id=tenant, document_id=doc.id, now=_now(), lease_seconds=60, max_attempts=5
    )
    assert second is not None
    assert second.generation == 1


@pytest.mark.asyncio
async def test_generation_fence_rejects_stale_writer() -> None:
    """worker A 认领 gen=0 → 重传 gen=1 → worker B 认领并写回 → A 再写回不生效."""
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"v1")
    a = await store.claim_document(
        tenant_id=tenant, document_id=doc.id, now=_now(), lease_seconds=60, max_attempts=5
    )
    assert a is not None and a.generation == 0
    await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"v2")
    b = await store.claim_document(
        tenant_id=tenant, document_id=doc.id, now=_now(), lease_seconds=60, max_attempts=5
    )
    assert b is not None and b.generation == 1

    def _c(content: str) -> KnowledgeChunk:
        return _chunk(
            tenant_id=tenant,
            kb_id=kb_id,
            document_id=doc.id,
            index=0,
            content=content,
            embedding=(1.0,),
        )

    assert await store.replace_chunks(
        tenant_id=tenant, document_id=doc.id, chunks=[_c("from-b")], generation=b.generation
    )
    assert await store.set_document_status(
        tenant_id=tenant,
        document_id=doc.id,
        status=DocumentStatus.READY,
        chunk_count=1,
        generation=b.generation,
    )
    # Stale worker A: both write-backs are rejected, nothing changes.
    assert not await store.replace_chunks(
        tenant_id=tenant, document_id=doc.id, chunks=[_c("from-a")], generation=a.generation
    )
    assert not await store.set_document_status(
        tenant_id=tenant,
        document_id=doc.id,
        status=DocumentStatus.READY,
        chunk_count=7,
        generation=a.generation,
    )
    assert not await store.mark_document_failed_terminal(
        tenant_id=tenant, document_id=doc.id, error="stale", generation=a.generation
    )
    fetched = await store.get_document(tenant_id=tenant, document_id=doc.id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.READY
    assert fetched.chunk_count == 1
    assert fetched.error is None
    hits = await store.search(tenant_id=tenant, kb_ids=[kb_id], query_embedding=(1.0,))
    assert [h.content for h in hits] == ["from-b"]


@pytest.mark.asyncio
async def test_fenced_write_requires_processing_status() -> None:
    # Right generation but the document is no longer processing (already
    # settled) → the fence rejects the write.
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"v1")
    claim = await store.claim_document(
        tenant_id=tenant, document_id=doc.id, now=_now(), lease_seconds=60, max_attempts=5
    )
    assert claim is not None
    assert await store.mark_document_failed_terminal(
        tenant_id=tenant, document_id=doc.id, error="first", generation=claim.generation
    )
    assert not await store.set_document_status(
        tenant_id=tenant,
        document_id=doc.id,
        status=DocumentStatus.READY,
        chunk_count=1,
        generation=claim.generation,
    )
    fetched = await store.get_document(tenant_id=tenant, document_id=doc.id)
    assert fetched is not None and fetched.status is DocumentStatus.FAILED
    # Unfenced writes (legacy callers / re-index) still go through.
    assert await store.set_document_status(
        tenant_id=tenant, document_id=doc.id, status=DocumentStatus.READY, chunk_count=1
    )


@pytest.mark.asyncio
async def test_sweep_terminal_fails_exhausted_processing_documents() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    exhausted = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="x.md", content=b"x"
    )
    for _ in range(3):
        assert (
            await store.claim_document(
                tenant_id=tenant,
                document_id=exhausted.id,
                now=_now(),
                lease_seconds=0,
                max_attempts=3,
            )
            is not None
        )
    # Under budget: must survive the sweep.
    live = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="y.md", content=b"y")
    assert (
        await store.claim_document(
            tenant_id=tenant, document_id=live.id, now=_now(), lease_seconds=0, max_attempts=3
        )
        is not None
    )
    # Exhausted but lease still held: the worker may still finish — not swept.
    held = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="z.md", content=b"z")
    for lease in (0, 0, 600):
        assert (
            await store.claim_document(
                tenant_id=tenant,
                document_id=held.id,
                now=_now(),
                lease_seconds=lease,
                max_attempts=3,
            )
            is not None
        )
    assert await store.sweep_terminal(now=_now(), max_attempts=3) == 1
    swept = await store.get_document(tenant_id=tenant, document_id=exhausted.id)
    assert swept is not None
    assert swept.status is DocumentStatus.FAILED
    assert swept.error == "ingest attempts exhausted"
    for untouched in (live.id, held.id):
        row = await store.get_document(tenant_id=tenant, document_id=untouched)
        assert row is not None and row.status is DocumentStatus.PROCESSING
    # Only the under-budget, lease-expired one is claimable afterwards.
    claims = await store.claim_documents_for_ingest(
        now=_now(), lease_seconds=60, limit=10, max_attempts=3
    )
    assert [c.document_id for c in claims] == [live.id]
    # Idempotent: a second sweep finds nothing.
    assert await store.sweep_terminal(now=_now(), max_attempts=3) == 0


@pytest.mark.asyncio
async def test_renew_lease_extends_and_is_fenced() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"x")
    now = _now()
    claim = await store.claim_document(
        tenant_id=tenant, document_id=doc.id, now=now, lease_seconds=0, max_attempts=5
    )
    assert claim is not None
    # Expired lease → claimable again; renewing pushes it out of reach.
    assert await store.renew_lease(
        tenant_id=tenant,
        document_id=doc.id,
        generation=claim.generation,
        now=now,
        lease_seconds=600,
    )
    assert (
        await store.claim_documents_for_ingest(now=now, lease_seconds=60, limit=10, max_attempts=5)
        == []
    )
    # Wrong generation / settled document → not renewed.
    assert not await store.renew_lease(
        tenant_id=tenant,
        document_id=doc.id,
        generation=claim.generation + 1,
        now=now,
        lease_seconds=600,
    )
    await store.set_document_status(
        tenant_id=tenant, document_id=doc.id, status=DocumentStatus.READY, chunk_count=0
    )
    assert not await store.renew_lease(
        tenant_id=tenant,
        document_id=doc.id,
        generation=claim.generation,
        now=now,
        lease_seconds=600,
    )


@pytest.mark.asyncio
async def test_list_documents_page() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    for i in range(5):
        await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename=f"{i}.md")
    full = await store.list_documents(tenant_id=tenant, kb_id=kb_id)
    page, total = await store.list_documents_page(tenant_id=tenant, kb_id=kb_id, limit=2, offset=2)
    assert total == 5
    assert [d.id for d in page] == [d.id for d in full[2:4]]
    tail, _ = await store.list_documents_page(tenant_id=tenant, kb_id=kb_id, limit=10, offset=4)
    assert len(tail) == 1
    assert await store.list_documents_page(tenant_id=uuid4(), kb_id=kb_id, limit=2, offset=0) == (
        [],
        0,
    )


@pytest.mark.asyncio
async def test_reupload_goes_to_pending_slot_and_failure_keeps_serving_version() -> None:
    """重传 v2 进 pending 格子;v2 失败 → 文档 failed,但 v1 原件 + 分段都还在、仍可检索."""
    store = InMemoryKnowledgeStore()
    tenant, kb_id, doc_id = await _ready_document(store)
    reset = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"v2", content_sha256="sha-v2"
    )
    assert reset.has_pending
    assert reset.chunk_count == 1  # serving count untouched
    assert await store.get_document_content(tenant_id=tenant, document_id=doc_id) == b"v1"
    claim = await store.claim_document(
        tenant_id=tenant, document_id=doc_id, now=_now(), lease_seconds=60, max_attempts=5
    )
    assert claim is not None
    assert claim.content == b"v2" and claim.content_sha256 == "sha-v2"  # ingests the pending one
    assert await store.mark_document_failed_terminal(
        tenant_id=tenant, document_id=doc_id, error="parse failed", generation=claim.generation
    )
    fetched = await store.get_document(tenant_id=tenant, document_id=doc_id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.FAILED
    assert fetched.has_pending
    assert fetched.chunk_count == 1
    assert await store.get_document_content(tenant_id=tenant, document_id=doc_id) == b"v1"
    hits = await store.search(tenant_id=tenant, kb_ids=[kb_id], query_embedding=(1.0,))
    assert [h.content for h in hits] == ["old"]


@pytest.mark.asyncio
async def test_fenced_ready_promotes_pending_to_serving() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id, doc_id = await _ready_document(store)
    await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"v2", content_sha256="sha-v2"
    )
    claim = await store.claim_document(
        tenant_id=tenant, document_id=doc_id, now=_now(), lease_seconds=60, max_attempts=5
    )
    assert claim is not None
    assert await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc_id,
        chunks=[
            _chunk(
                tenant_id=tenant,
                kb_id=kb_id,
                document_id=doc_id,
                index=0,
                content="new",
                embedding=(1.0,),
            )
        ],
        generation=claim.generation,
    )
    assert await store.set_document_status(
        tenant_id=tenant,
        document_id=doc_id,
        status=DocumentStatus.READY,
        chunk_count=1,
        generation=claim.generation,
    )
    fetched = await store.get_document(tenant_id=tenant, document_id=doc_id)
    assert fetched is not None
    assert fetched.status is DocumentStatus.READY and not fetched.has_pending
    assert await store.get_document_content(tenant_id=tenant, document_id=doc_id) == b"v2"
    # Now v2 is the serving hash: re-uploading v2 is "unchanged".
    same = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"v2", content_sha256="sha-v2"
    )
    assert same.status is DocumentStatus.READY


@pytest.mark.asyncio
async def test_discard_pending_restores_serving_version() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id, doc_id = await _ready_document(store)
    await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"v2", content_sha256="sha-v2"
    )
    claim = await store.claim_document(
        tenant_id=tenant, document_id=doc_id, now=_now(), lease_seconds=60, max_attempts=5
    )
    assert claim is not None
    await store.mark_document_failed_terminal(
        tenant_id=tenant, document_id=doc_id, error="bad", generation=claim.generation
    )
    restored = await store.discard_pending(tenant_id=tenant, document_id=doc_id)
    assert restored is not None
    assert restored.status is DocumentStatus.READY
    assert restored.error is None and not restored.has_pending
    assert restored.ingest_generation == claim.generation + 1  # in-flight v2 work is fenced out
    assert await store.get_document_content(tenant_id=tenant, document_id=doc_id) == b"v1"
    # Idempotent: nothing pending → document returned unchanged.
    again = await store.discard_pending(tenant_id=tenant, document_id=doc_id)
    assert again == restored
    assert await store.discard_pending(tenant_id=uuid4(), document_id=doc_id) is None


@pytest.mark.asyncio
async def test_discard_pending_with_nothing_serving_stays_failed() -> None:
    # First upload failed (no chunks), then a second upload also failed:
    # discarding the second leaves a failed document, not a phantom READY.
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"v1", content_sha256="s1"
    )
    await store.set_document_status(
        tenant_id=tenant, document_id=doc.id, status=DocumentStatus.FAILED, error="bad v1"
    )
    reset = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"v2", content_sha256="s2"
    )
    assert reset.has_pending
    await store.set_document_status(
        tenant_id=tenant, document_id=doc.id, status=DocumentStatus.FAILED, error="bad v2"
    )
    restored = await store.discard_pending(tenant_id=tenant, document_id=doc.id)
    assert restored is not None
    assert restored.status is DocumentStatus.FAILED
    assert restored.error == "pending upload discarded; nothing is serving"


@pytest.mark.asyncio
async def test_force_reingest_without_bytes_keeps_both_slots() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id, doc_id = await _ready_document(store)
    await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"v2", content_sha256="sha-v2"
    )
    reset = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.md", force=True)
    assert reset.has_pending  # retry the pending v2, serving v1 untouched
    claim = await store.claim_document(
        tenant_id=tenant, document_id=doc_id, now=_now(), lease_seconds=60, max_attempts=5
    )
    assert claim is not None and claim.content == b"v2"
    assert await store.get_document_content(tenant_id=tenant, document_id=doc_id) == b"v1"


@pytest.mark.asyncio
async def test_sweep_terminal_keeps_serving_chunks() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id, doc_id = await _ready_document(store)
    await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"v2")
    for _ in range(2):
        await store.claim_document(
            tenant_id=tenant, document_id=doc_id, now=_now(), lease_seconds=0, max_attempts=2
        )
    assert await store.sweep_terminal(now=_now(), max_attempts=2) == 1
    hits = await store.search(tenant_id=tenant, kb_ids=[kb_id], query_embedding=(1.0,))
    assert [h.content for h in hits] == ["old"]


@pytest.mark.asyncio
async def test_fenced_replace_on_deleted_document_returns_false() -> None:
    store = InMemoryKnowledgeStore()
    tenant, kb_id = uuid4(), uuid4()
    doc = await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"x")
    claim = await store.claim_document(
        tenant_id=tenant, document_id=doc.id, now=_now(), lease_seconds=60, max_attempts=5
    )
    assert claim is not None
    await store.delete_document(tenant_id=tenant, document_id=doc.id)
    chunk = _chunk(
        tenant_id=tenant, kb_id=kb_id, document_id=doc.id, index=0, content="c", embedding=(1.0,)
    )
    assert not await store.replace_chunks(
        tenant_id=tenant, document_id=doc.id, chunks=[chunk], generation=claim.generation
    )
    with pytest.raises(KeyError):  # unfenced keeps the loud failure
        await store.replace_chunks(tenant_id=tenant, document_id=doc.id, chunks=[chunk])


@pytest.mark.asyncio
async def test_reindex_fence_expects_ready_and_current_generation() -> None:
    """重建索引的写回:文档必须仍是 READY 且代际没变,否则不写(重传期间不许把旧分段放回去)."""
    store = InMemoryKnowledgeStore()
    tenant, kb_id, doc_id = await _ready_document(store)
    chunk = _chunk(
        tenant_id=tenant, kb_id=kb_id, document_id=doc_id, index=0, content="re", embedding=(1.0,)
    )
    assert await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc_id,
        chunks=[chunk],
        generation=0,
        expect_status=DocumentStatus.READY,
    )
    # Same generation but no longer READY → refused: the status half of the
    # fence, not just the generation (contract pin; set directly).
    assert await store.set_document_status(
        tenant_id=tenant, document_id=doc_id, status=DocumentStatus.PROCESSING
    )
    assert not await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc_id,
        chunks=[chunk],
        generation=0,
        expect_status=DocumentStatus.READY,
    )
    # Re-uploaded meanwhile: generation 1, pending → refused on both halves.
    await store.upsert_document(tenant_id=tenant, kb_id=kb_id, filename="d.md", content=b"v2")
    assert not await store.replace_chunks(
        tenant_id=tenant,
        document_id=doc_id,
        chunks=[chunk],
        generation=0,
        expect_status=DocumentStatus.READY,
    )


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.mark.asyncio
async def test_list_bases_page() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    for i in range(5):
        await store.create_base(tenant_id=tenant, name=f"kb-{i}")
    await store.create_base(tenant_id=uuid4(), name="foreign")
    full = await store.list_bases(tenant_id=tenant)
    page, total = await store.list_bases_page(tenant_id=tenant, limit=2, offset=2)
    assert total == 5
    assert [b.id for b in page] == [b.id for b in full[2:4]]
    tail, _ = await store.list_bases_page(tenant_id=tenant, limit=10, offset=4)
    assert len(tail) == 1
    assert await store.list_bases_page(tenant_id=uuid4(), limit=2, offset=0) == ([], 0)


@pytest.mark.asyncio
async def test_list_bases_all_tenants_page() -> None:
    store = InMemoryKnowledgeStore()
    for i in range(3):
        await store.create_base(tenant_id=uuid4(), name=f"kb-{i}")
    full = await store.list_bases_all_tenants()
    page, total = await store.list_bases_all_tenants_page(limit=2, offset=1)
    assert total == 3
    assert [b.id for b in page] == [b.id for b in full[1:3]]


@pytest.mark.asyncio
async def test_list_bases_created_at_tie_orders_like_sql() -> None:
    """Same ``created_at``: single-tenant id DESC, all-tenants id ASC — the
    SQL twin pins the same order (``test_sql_knowledge_store``)."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    for i in range(4):
        await store.create_base(tenant_id=tenant, name=f"tie-{i}")
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    store._bases = [b.model_copy(update={"created_at": stamp}) for b in store._bases]
    ids = [b.id for b in store._bases]
    full = await store.list_bases(tenant_id=tenant)
    assert [b.id for b in full] == sorted(ids, key=lambda u: u.int, reverse=True)
    page, _ = await store.list_bases_page(tenant_id=tenant, limit=2, offset=2)
    assert [b.id for b in page] == [b.id for b in full[2:4]]
    everyone = await store.list_bases_all_tenants()
    assert [b.id for b in everyone] == sorted(ids, key=lambda u: u.int)
