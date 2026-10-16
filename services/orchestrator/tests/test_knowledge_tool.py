"""Unit tests for J.5 knowledge retrieval — retriever, reranker, tool."""

from __future__ import annotations

import random
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage

from expert_work.common.search.rrf import rrf_fuse as _rrf_fuse
from expert_work.common.search.rrf import rrf_fuse_scored
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import KnowledgeChunk, RetrievalMethod, ScoredChunk
from orchestrator.tools import (
    KnowledgeRetriever,
    KnowledgeSearchTool,
    LLMReranker,
    Reranker,
    Tool,
    ToolBlockedError,
    ToolContext,
)
from orchestrator.tools.knowledge import _parse_rerank_order

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FixedEmbedder:
    """Embedder returning one fixed vector for every text."""

    def __init__(self, vector: tuple[float, ...]) -> None:
        self._vector = vector

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        return [self._vector for _ in texts]


class _ScriptedLLM:
    """LLMCaller returning one scripted reply."""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def __call__(self, *, messages: Any, tools: Any) -> AIMessage:
        del messages, tools
        return AIMessage(content=self._reply)


class _FailingLLM:
    async def __call__(self, *, messages: Any, tools: Any) -> AIMessage:
        del messages, tools
        raise RuntimeError("llm down")


class _ReversingReranker:
    """Reranker that reverses the candidate order."""

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        del tenant_id
        del query
        return list(reversed(range(len(documents))))[:top_k]


def _chunk(
    *, tenant: UUID, kb: UUID, doc: UUID, index: int, content: str, embedding: tuple[float, ...]
) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=uuid4(),
        tenant_id=tenant,
        kb_id=kb,
        document_id=doc,
        chunk_index=index,
        content=content,
        embedding=embedding,
    )


async def _seed_store() -> tuple[InMemoryKnowledgeStore, UUID]:
    """A store with one tenant, base ``kb``, document ``doc.pdf``, two chunks:
    chunk 0 about deductibles (vector (1,0)), chunk 1 about vacation (0,1)."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="doc.pdf")
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=document.id,
        chunks=[
            _chunk(
                tenant=tenant,
                kb=base.id,
                doc=document.id,
                index=0,
                content="the deductible is 500 dollars",
                embedding=(1.0, 0.0),
            ),
            _chunk(
                tenant=tenant,
                kb=base.id,
                doc=document.id,
                index=1,
                content="vacation policy grants 20 days",
                embedding=(0.0, 1.0),
            ),
        ],
    )
    return store, tenant


# ---------------------------------------------------------------------------
# RRF fusion
# ---------------------------------------------------------------------------


def test_rrf_fuse_rewards_chunks_in_both_lists() -> None:
    tenant, kb, doc = uuid4(), uuid4(), uuid4()
    shared = _chunk(tenant=tenant, kb=kb, doc=doc, index=0, content="shared", embedding=(1.0,))
    vec_only = _chunk(tenant=tenant, kb=kb, doc=doc, index=1, content="vec", embedding=(1.0,))
    kw_only = _chunk(tenant=tenant, kb=kb, doc=doc, index=2, content="kw", embedding=(1.0,))
    fused = _rrf_fuse([[vec_only, shared], [kw_only, shared]])
    # ``shared`` appears in both lists → highest fused score.
    assert fused[0].id == shared.id
    assert {c.id for c in fused} == {shared.id, vec_only.id, kw_only.id}


# ---------------------------------------------------------------------------
# rerank-order parsing
# ---------------------------------------------------------------------------


def test_parse_rerank_order_json_array() -> None:
    assert _parse_rerank_order("[2, 1, 3]", 5) == [1, 0, 2]


def test_parse_rerank_order_bare_numbers() -> None:
    assert _parse_rerank_order("most relevant: 3 then 1", 5) == [2, 0]


def test_parse_rerank_order_drops_out_of_range_and_dups() -> None:
    assert _parse_rerank_order("[9, 1, 1, 2]", 3) == [0, 1]


def test_parse_rerank_order_garbage_yields_empty() -> None:
    assert _parse_rerank_order("no numbers here", 3) == []


# ---------------------------------------------------------------------------
# LLMReranker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_reranker_reorders() -> None:
    reranker = LLMReranker(llm_caller=_ScriptedLLM("[2, 1]"))
    order = await reranker.rerank(query="q", documents=["a", "b"], top_k=2, tenant_id=uuid4())
    assert order == [1, 0]


@pytest.mark.asyncio
async def test_llm_reranker_truncates_to_top_k() -> None:
    reranker = LLMReranker(llm_caller=_ScriptedLLM("[3, 2, 1]"))
    order = await reranker.rerank(query="q", documents=["a", "b", "c"], top_k=2, tenant_id=uuid4())
    assert order == [2, 1]


@pytest.mark.asyncio
async def test_llm_reranker_unparseable_reply_falls_back_to_input_order() -> None:
    reranker = LLMReranker(llm_caller=_ScriptedLLM("sorry, I cannot help"))
    order = await reranker.rerank(query="q", documents=["a", "b", "c"], top_k=3, tenant_id=uuid4())
    assert order == [0, 1, 2]


@pytest.mark.asyncio
async def test_llm_reranker_llm_failure_falls_back() -> None:
    reranker = LLMReranker(llm_caller=_FailingLLM())
    order = await reranker.rerank(query="q", documents=["a", "b"], top_k=2, tenant_id=uuid4())
    assert order == [0, 1]


# ---------------------------------------------------------------------------
# KnowledgeRetriever
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retriever_returns_attributed_chunks() -> None:
    store, tenant = await _seed_store()
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0, 0.0)))
    results = await retriever.search(
        tenant_id=tenant, base_names=["kb"], query="deductible", limit=5
    )
    assert results
    top = results[0]
    assert "deductible" in top.content
    assert top.filename == "doc.pdf"
    assert top.chunk_index == 0


@pytest.mark.asyncio
async def test_retriever_keyword_recall_surfaces_a_vector_miss() -> None:
    # The query vector points at chunk 0, but "vacation" only matches
    # chunk 1 by keyword — hybrid retrieval still surfaces chunk 1.
    store, tenant = await _seed_store()
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0, 0.0)))
    results = await retriever.search(tenant_id=tenant, base_names=["kb"], query="vacation", limit=5)
    assert any("vacation" in r.content for r in results)


@pytest.mark.asyncio
async def test_retriever_unknown_base_name_is_skipped() -> None:
    store, tenant = await _seed_store()
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0, 0.0)))
    assert await retriever.search(tenant_id=tenant, base_names=["ghost"], query="x", limit=5) == []


@pytest.mark.asyncio
async def test_retriever_applies_reranker() -> None:
    store, tenant = await _seed_store()
    retriever = KnowledgeRetriever(
        store=store, embedder=_FixedEmbedder((1.0, 0.0)), reranker=_ReversingReranker()
    )
    plain = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0, 0.0)))
    reranked = await retriever.search(
        tenant_id=tenant, base_names=["kb"], query="deductible", limit=5
    )
    fused = await plain.search(tenant_id=tenant, base_names=["kb"], query="deductible", limit=5)
    assert [r.content for r in reranked] == [r.content for r in reversed(fused)]


@pytest.mark.asyncio
async def test_retriever_surfaces_score_and_recall_source() -> None:
    # query vector == chunk 0 → similarity ~1.0; "deductible" also matches it
    # by keyword → recall_source "both".
    store, tenant = await _seed_store()
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0, 0.0)))
    results = await retriever.search(
        tenant_id=tenant, base_names=["kb"], query="deductible", limit=5
    )
    top = next(r for r in results if r.chunk_index == 0)
    assert top.score == pytest.approx(1.0)
    assert top.recall_source == "both"


@pytest.mark.asyncio
async def test_retriever_override_method_and_threshold_filters() -> None:
    # method=vector skips keyword; threshold 0.5 drops chunk 1 (similarity ~0).
    store, tenant = await _seed_store()
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0, 0.0)))
    results = await retriever.search(
        tenant_id=tenant,
        base_names=["kb"],
        query="deductible",
        limit=5,
        method=RetrievalMethod.VECTOR,
        score_threshold=0.5,
    )
    assert [r.chunk_index for r in results] == [0]
    assert results[0].recall_source == "vector"


@pytest.mark.asyncio
async def test_retriever_reads_per_base_config_defaults() -> None:
    # No overrides — the base's own stored config (vector-only + threshold)
    # is applied, proving per-KB defaults are honoured.
    store, tenant = await _seed_store()
    base = await store.get_base(tenant_id=tenant, name="kb")
    assert base is not None
    await store.update_base(
        tenant_id=tenant,
        kb_id=base.id,
        retrieval_method=RetrievalMethod.VECTOR,
        retrieval_score_threshold=0.5,
    )
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0, 0.0)))
    results = await retriever.search(tenant_id=tenant, base_names=["kb"], query="vacation", limit=5)
    # "vacation" matches chunk 1 by keyword, but keyword recall is off and
    # chunk 1's vector similarity (~0) is below the base threshold → only
    # chunk 0 survives.
    assert [r.chunk_index for r in results] == [0]


@pytest.mark.asyncio
async def test_retriever_keyword_only_method() -> None:
    store, tenant = await _seed_store()
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0, 0.0)))
    results = await retriever.search(
        tenant_id=tenant,
        base_names=["kb"],
        query="vacation",
        limit=5,
        method=RetrievalMethod.KEYWORD,
    )
    assert [r.chunk_index for r in results] == [1]
    assert results[0].recall_source == "keyword"
    assert results[0].score is None  # keyword-only hits carry no [0,1] similarity


@pytest.mark.asyncio
async def test_retriever_rerank_override_disables() -> None:
    store, tenant = await _seed_store()
    retriever = KnowledgeRetriever(
        store=store, embedder=_FixedEmbedder((1.0, 0.0)), reranker=_ReversingReranker()
    )
    plain = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0, 0.0)))
    no_rerank = await retriever.search(
        tenant_id=tenant, base_names=["kb"], query="deductible", limit=5, rerank=False
    )
    fused = await plain.search(tenant_id=tenant, base_names=["kb"], query="deductible", limit=5)
    # rerank=False → the reranker is skipped, so the order matches plain fusion.
    assert [r.content for r in no_rerank] == [r.content for r in fused]


# ---------------------------------------------------------------------------
# KnowledgeSearchTool
# ---------------------------------------------------------------------------


def _tool(
    store: InMemoryKnowledgeStore, *, reranker: Reranker | None = None
) -> KnowledgeSearchTool:
    retriever = KnowledgeRetriever(
        store=store, embedder=_FixedEmbedder((1.0, 0.0)), reranker=reranker
    )
    return KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",))


def test_knowledge_search_tool_satisfies_tool_protocol() -> None:
    assert isinstance(_tool(InMemoryKnowledgeStore()), Tool)


def test_knowledge_search_spec() -> None:
    spec = _tool(InMemoryKnowledgeStore()).spec
    assert spec.name == "knowledge_search"
    assert spec.parameters["required"] == ["query"]


@pytest.mark.asyncio
async def test_knowledge_search_tool_returns_attributed_block() -> None:
    store, tenant = await _seed_store()
    result = await _tool(store).call({"query": "deductible"}, ctx=ToolContext(tenant_id=tenant))
    assert "[doc.pdf#0]" in result.content
    assert "deductible" in result.content
    assert result.meta["hits"] >= 1


@pytest.mark.asyncio
async def test_knowledge_search_tool_requires_tenant() -> None:
    with pytest.raises(ToolBlockedError, match="tenant binding"):
        await _tool(InMemoryKnowledgeStore()).call({"query": "x"}, ctx=ToolContext())


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{}, {"query": ""}, {"query": "   "}])
async def test_knowledge_search_tool_rejects_empty_query(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="non-empty 'query'"):
        await _tool(InMemoryKnowledgeStore()).call(bad, ctx=ToolContext(tenant_id=uuid4()))


@pytest.mark.asyncio
async def test_knowledge_search_tool_no_hits_message() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    await store.create_base(tenant_id=tenant, name="kb")  # empty base
    result = await _tool(store).call({"query": "anything"}, ctx=ToolContext(tenant_id=tenant))
    assert "no relevant knowledge" in result.content
    assert result.meta["hits"] == 0


# ---------------------------------------------------------------------------
# B-175 W2 PR2 — recall depth, rerank candidate cap, weighted fusion, sub-scores
# ---------------------------------------------------------------------------


class _RecordingStore(InMemoryKnowledgeStore):
    """Records the ``limit`` each recall path is asked for."""

    def __init__(self) -> None:
        super().__init__()
        self.vector_limits: list[int] = []
        self.keyword_limits: list[int] = []

    async def search_scored(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        query_embedding: Sequence[float],
        limit: int = 5,
    ) -> list[ScoredChunk]:
        self.vector_limits.append(limit)
        return await super().search_scored(
            tenant_id=tenant_id, kb_ids=kb_ids, query_embedding=query_embedding, limit=limit
        )

    async def keyword_search_scored(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        query: str,
        limit: int = 5,
    ) -> list[ScoredChunk]:
        self.keyword_limits.append(limit)
        return await super().keyword_search_scored(
            tenant_id=tenant_id, kb_ids=kb_ids, query=query, limit=limit
        )


class _CountingReranker:
    """Records how many candidates each rerank call received; keeps their order."""

    def __init__(self) -> None:
        self.sizes: list[int] = []

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        del query, tenant_id
        self.sizes.append(len(documents))
        return list(range(len(documents)))[:top_k]


async def _seed_bases(store: InMemoryKnowledgeStore, *, bases: int, per_base: int) -> UUID:
    """``bases`` bases ``kb0``.. with ``per_base`` chunks each; every chunk
    mentions "policy" (keyword hit) and has a distinct vector."""
    tenant = uuid4()
    for b in range(bases):
        base = await store.create_base(tenant_id=tenant, name=f"kb{b}")
        document = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename=f"d{b}.pdf"
        )
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=document.id,
            chunks=[
                _chunk(
                    tenant=tenant,
                    kb=base.id,
                    doc=document.id,
                    index=i,
                    content=f"policy clause {i}",
                    embedding=(1.0, i / per_base),
                )
                for i in range(per_base)
            ],
        )
    return tenant


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit", "depth"),
    [(1, 50), (5, 50), (10, 50), (25, 50), (30, 60), (50, 100), (60, 100)],
)
async def test_default_recall_depth_is_twice_limit_clamped_to_50_100(
    limit: int, depth: int
) -> None:
    store = _RecordingStore()
    tenant = await _seed_bases(store, bases=1, per_base=3)
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0, 0.0)))
    await retriever.search(tenant_id=tenant, base_names=["kb0"], query="policy", limit=limit)
    assert store.vector_limits == [depth]
    assert store.keyword_limits == [depth]


@pytest.mark.asyncio
@pytest.mark.parametrize("recall_limit", [7, 150])
async def test_explicit_recall_limit_is_respected(recall_limit: int) -> None:
    store = _RecordingStore()
    tenant = await _seed_bases(store, bases=1, per_base=3)
    retriever = KnowledgeRetriever(
        store=store, embedder=_FixedEmbedder((1.0, 0.0)), recall_limit=recall_limit
    )
    await retriever.search(tenant_id=tenant, base_names=["kb0"], query="policy", limit=5)
    assert store.vector_limits == [recall_limit]
    assert store.keyword_limits == [recall_limit]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit", "recall_limit", "candidates"),
    [(5, None, 50), (60, None, 100), (5, 150, 100)],
)
async def test_rerank_candidates_follow_recall_depth_capped_at_100(
    limit: int, recall_limit: int | None, candidates: int
) -> None:
    # Two bases x 120 chunks: fusion yields up to 240 candidates; the
    # reranker (qwen3-vl-rerank: <= 100 documents per request) never gets more
    # than 100, however many bases are searched or how deep recall goes.
    store = InMemoryKnowledgeStore()
    tenant = await _seed_bases(store, bases=2, per_base=120)
    reranker = _CountingReranker()
    retriever = KnowledgeRetriever(
        store=store,
        embedder=_FixedEmbedder((1.0, 0.0)),
        reranker=reranker,
        recall_limit=recall_limit,
    )
    await retriever.search(tenant_id=tenant, base_names=["kb0", "kb1"], query="policy", limit=limit)
    assert reranker.sizes == [candidates]


@pytest.mark.asyncio
async def test_without_rerank_the_candidate_cap_does_not_cut_results() -> None:
    # The 100 cap is the reranker's request limit; a fused-order search still
    # returns its full ``limit`` beyond it.
    store = InMemoryKnowledgeStore()
    tenant = await _seed_bases(store, bases=2, per_base=120)
    retriever = KnowledgeRetriever(
        store=store, embedder=_FixedEmbedder((1.0, 0.0)), recall_limit=130
    )
    results = await retriever.search(
        tenant_id=tenant, base_names=["kb0", "kb1"], query="policy", limit=130, rerank=False
    )
    assert len(results) == 130


@pytest.mark.asyncio
async def test_default_weights_reproduce_plain_rrf_order_and_scores() -> None:
    """Regression lock: with default weights the fused order AND scores are
    exactly what ``rrf_fuse_scored`` gives over the same per-path rankings
    (the pre-PR2 fusion), across several bases."""
    rng = random.Random(20261014)  # noqa: S311 — reproducible test data, not crypto
    words = ["claim", "policy", "refund", "premium", "deductible", "network", "provider"]
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    bases = []
    for b in range(3):
        base = await store.create_base(tenant_id=tenant, name=f"kb{b}")
        document = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename=f"d{b}.pdf"
        )
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=document.id,
            chunks=[
                _chunk(
                    tenant=tenant,
                    kb=base.id,
                    doc=document.id,
                    index=i,
                    content=" ".join(rng.sample(words, 3)),
                    embedding=(rng.random(), rng.random(), rng.random()),
                )
                for i in range(30)
            ],
        )
        bases.append(base)
    query, embedding = "refund premium", (0.3, 0.9, 0.1)
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder(embedding))
    results = await retriever.search(
        tenant_id=tenant, base_names=["kb0", "kb1", "kb2"], query=query, limit=60, rerank=False
    )

    rankings: list[list[KnowledgeChunk]] = []
    for base in bases:
        vector = await store.search_scored(
            tenant_id=tenant, kb_ids=[base.id], query_embedding=embedding, limit=100
        )
        keyword = await store.keyword_search_scored(
            tenant_id=tenant, kb_ids=[base.id], query=query, limit=100
        )
        rankings += [[hit.chunk for hit in vector], [hit.chunk for hit in keyword]]
    expected = [(chunk.id, score) for chunk, score in rrf_fuse_scored(rankings)][:60]
    assert len(expected) == 60
    assert [(r.chunk_id, r.fused_score) for r in results] == expected


async def _fused_indices(
    store: InMemoryKnowledgeStore,
    tenant: UUID,
    *,
    vector_weight: float = 1.0,
    keyword_weight: float = 1.0,
) -> list[int]:
    # Threshold 0.5 keeps only chunk 0 on the vector path; "vacation" matches
    # only chunk 1 by keyword — one rank-0 hit per path, tied at equal weights.
    retriever = KnowledgeRetriever(
        store=store,
        embedder=_FixedEmbedder((1.0, 0.0)),
        vector_weight=vector_weight,
        keyword_weight=keyword_weight,
    )
    results = await retriever.search(
        tenant_id=tenant, base_names=["kb"], query="vacation", limit=5, score_threshold=0.5
    )
    return [r.chunk_index for r in results]


@pytest.mark.asyncio
async def test_path_weights_reorder_fusion() -> None:
    store, tenant = await _seed_store()
    assert await _fused_indices(store, tenant) == [0, 1]  # tie → vector list fused first
    assert await _fused_indices(store, tenant, keyword_weight=1.5) == [1, 0]
    assert await _fused_indices(store, tenant, vector_weight=0.5) == [1, 0]
    assert await _fused_indices(store, tenant, vector_weight=1.5) == [0, 1]


@pytest.mark.asyncio
async def test_sub_scores_for_vector_only_keyword_only_and_both() -> None:
    store, tenant = await _seed_store()
    base = await store.get_base(tenant_id=tenant, name="kb")
    assert base is not None
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="b.pdf")
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=document.id,
        chunks=[
            _chunk(
                tenant=tenant,
                kb=base.id,
                doc=document.id,
                index=0,
                content="vacation carryover rules",
                embedding=(0.9, 0.1),
            )
        ],
    )
    query, embedding = "vacation carryover", (1.0, 0.0)
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder(embedding))
    results = await retriever.search(
        tenant_id=tenant, base_names=["kb"], query=query, limit=5, score_threshold=0.5
    )

    vector = [
        hit
        for hit in await store.search_scored(
            tenant_id=tenant, kb_ids=[base.id], query_embedding=embedding, limit=50
        )
        if hit.score >= 0.5
    ]
    keyword = await store.keyword_search_scored(
        tenant_id=tenant, kb_ids=[base.id], query=query, limit=50
    )
    fused = {
        chunk.id: score
        for chunk, score in rrf_fuse_scored(
            [[hit.chunk for hit in vector], [hit.chunk for hit in keyword]]
        )
    }
    vector_by_id = {hit.chunk.id: hit.score for hit in vector}
    keyword_by_id = {hit.chunk.id: hit.score for hit in keyword}
    by_source = {r.recall_source: r for r in results}
    assert set(by_source) == {"vector", "keyword", "both"}

    for result in results:
        assert result.chunk_id is not None
        assert result.vector_score == vector_by_id.get(result.chunk_id)
        assert result.keyword_score == keyword_by_id.get(result.chunk_id)
        assert result.fused_score == fused[result.chunk_id]
        assert result.score == result.vector_score  # ``score`` keeps its meaning

    vector_only, keyword_only, both = by_source["vector"], by_source["keyword"], by_source["both"]
    assert "deductible" in vector_only.content
    assert vector_only.vector_score is not None
    assert vector_only.keyword_score is None
    assert "vacation policy" in keyword_only.content
    assert keyword_only.vector_score is None
    assert keyword_only.keyword_score is not None
    assert "carryover" in both.content
    assert both.vector_score is not None
    assert both.keyword_score is not None
    # Found by both paths → the highest fused score.
    assert both.fused_score is not None
    assert vector_only.fused_score is not None
    assert both.fused_score > vector_only.fused_score


class _EditedBetweenPathsStore(InMemoryKnowledgeStore):
    """The keyword path returns the chunk as edited after the vector path read
    it: same id, different content — two unequal objects."""

    async def keyword_search_scored(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        query: str,
        limit: int = 5,
    ) -> list[ScoredChunk]:
        hits = await super().keyword_search_scored(
            tenant_id=tenant_id, kb_ids=kb_ids, query=query, limit=limit
        )
        return [
            hit.model_copy(
                update={"chunk": hit.chunk.model_copy(update={"content": hit.chunk.content + "!"})}
            )
            for hit in hits
        ]


@pytest.mark.asyncio
async def test_fusion_keys_on_chunk_id_not_the_chunk_value() -> None:
    # Chunk 0 matches by vector and keyword, but the two paths return unequal
    # objects for it. Fusion counts it once (both paths) and keeps the first
    # object seen (the vector path's).
    store = _EditedBetweenPathsStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
    await store.replace_chunks(
        tenant_id=tenant,
        document_id=document.id,
        chunks=[
            _chunk(
                tenant=tenant,
                kb=base.id,
                doc=document.id,
                index=0,
                content="the deductible is 500 dollars",
                embedding=(1.0, 0.0),
            )
        ],
    )
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0, 0.0)))
    results = await retriever.search(
        tenant_id=tenant, base_names=["kb"], query="deductible", limit=5
    )
    assert len(results) == 1
    assert results[0].recall_source == "both"
    assert results[0].content == "the deductible is 500 dollars"
    assert results[0].fused_score == pytest.approx(2 / 61)


@pytest.mark.asyncio
async def test_chunk_with_unhashable_source_locator_is_retrievable() -> None:
    # ``source_locator`` is a list of dicts, so the chunk value is unhashable;
    # fusion must not need to hash it.
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="d.pdf")
    chunk = _chunk(
        tenant=tenant,
        kb=base.id,
        doc=document.id,
        index=0,
        content="the deductible is 500 dollars",
        embedding=(1.0, 0.0),
    ).model_copy(update={"source_locator": [{"type": "pdf", "page": 3}]})
    await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=[chunk])
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0, 0.0)))
    results = await retriever.search(
        tenant_id=tenant, base_names=["kb"], query="deductible", limit=5
    )
    assert [r.chunk_id for r in results] == [chunk.id]


class _RecordingReranker:
    """Records the texts it was asked to rank; keeps their order."""

    def __init__(self) -> None:
        self.documents: list[str] = []

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        del query, tenant_id
        self.documents.extend(documents)
        return list(range(len(documents)))[:top_k]


async def test_reranker_sees_the_document_title_but_the_agent_sees_it_once() -> None:
    """The reranker ranks the retrieval text, title included (B-175 wave 2
    PR2b); the agent already gets the file name in each result's header, so
    its text carries no second ``[Document: …]`` line."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="guide.pdf")
    chunk = _chunk(
        tenant=tenant, kb=base.id, doc=document.id, index=0, content="deductible", embedding=(1.0,)
    ).model_copy(update={"doc_title": "guide", "heading_path": ("Costs",)})
    await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=[chunk])
    reranker = _RecordingReranker()
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0,)), reranker=reranker)

    results = await retriever.search(
        tenant_id=tenant, base_names=["kb"], query="deductible", limit=5
    )

    assert reranker.documents == ["[Document: guide]\n[Section: Costs]\n\ndeductible"]
    assert [(r.filename, r.retrieval_text) for r in results] == [
        ("guide.pdf", "[Section: Costs]\n\ndeductible")
    ]


class _ScoringReranker:
    """A scoring reranker: each text's score comes from ``scores`` (unknown
    texts score 0, or ``None`` throughout with ``unscored``); records each call."""

    def __init__(self, scores: dict[str, float], *, unscored: bool = False) -> None:
        self.scores = scores
        self.unscored = unscored
        self.calls: list[tuple[list[str], int]] = []

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        ranked = await self.rerank_scored(
            query=query, documents=documents, top_k=top_k, tenant_id=tenant_id
        )
        return [index for index, _score in ranked]

    async def rerank_scored(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[tuple[int, float | None]]:
        del query, tenant_id
        self.calls.append((list(documents), top_k))
        order = sorted(
            range(len(documents)), key=lambda i: (-self.scores.get(documents[i], 0.0), i)
        )[:top_k]
        if self.unscored:
            return [(i, None) for i in order]
        return [(i, self.scores.get(documents[i], 0.0)) for i in order]


async def _titled_store(
    bodies: Sequence[str], *, title: str | None
) -> tuple[InMemoryKnowledgeStore, UUID, list[KnowledgeChunk]]:
    """One base, one document; one chunk per body, all with the same vector."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="doc.pdf")
    chunks = [
        _chunk(
            tenant=tenant, kb=base.id, doc=document.id, index=i, content=body, embedding=(1.0,)
        ).model_copy(update={"doc_title": title})
        for i, body in enumerate(bodies)
    ]
    await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=chunks)
    return store, tenant, chunks


async def test_two_view_rerank_ranks_by_the_weighted_mean_of_both_views() -> None:
    """A question about a code: with the title every passage of the file named
    after it looks on topic, so the boilerplate outranks the passage carrying
    the code. The body view still tells them apart, and 0.7 x titled + 0.3 x
    body puts the answer first (B-175 wave 2 PR2b eval)."""
    store, tenant, _ = await _titled_store(
        ["QXA-N7 strain", "claims boilerplate"], title="patent QXA-N7"
    )
    answer, boilerplate = "QXA-N7 strain", "claims boilerplate"
    reranker = _ScoringReranker(
        {
            f"[Document: patent QXA-N7]\n\n{answer}": 0.80,
            f"[Document: patent QXA-N7]\n\n{boilerplate}": 0.90,
            answer: 0.90,
            boilerplate: 0.10,
        }
    )
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0,)), reranker=reranker)

    results = await retriever.search(
        tenant_id=tenant, base_names=["kb"], query="QXA-N7 专利", limit=5
    )

    # 0.7 x 0.80 + 0.3 x 0.90 = 0.83 beats 0.7 x 0.90 + 0.3 x 0.10 = 0.66.
    assert [r.content for r in results] == [answer, boilerplate]
    assert len(reranker.calls) == 2
    assert sorted(reranker.calls[1][0]) == sorted([answer, boilerplate])


async def test_two_view_rerank_judges_the_titled_top_20_on_the_body() -> None:
    """The titled view returns its best 20 even for a 5-result search, the body
    view re-judges exactly those, and the search still returns 5."""
    bodies = [f"passage {i:02d}" for i in range(30)]
    store, tenant, _ = await _titled_store(bodies, title="guide")
    # Titled: passage 00 best, 29 worst. Body: reversed within the titled top 20.
    scores = {f"[Document: guide]\n\n{b}": 1.0 - i / 100 for i, b in enumerate(bodies)}
    scores |= {b: i / 100 for i, b in enumerate(bodies)}
    reranker = _ScoringReranker(scores)
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0,)), reranker=reranker)

    results = await retriever.search(tenant_id=tenant, base_names=["kb"], query="q", limit=5)

    (_titled_docs, titled_top_k), (body_docs, body_top_k) = reranker.calls
    assert titled_top_k == 20
    assert sorted(body_docs) == bodies[:20]
    assert body_top_k == 20
    assert len(results) == 5
    # Titled gaps are 0.01 x 0.7, body gaps 0.01 x 0.3: the titled order holds.
    assert [r.content for r in results] == bodies[:5]


async def test_two_view_rerank_falls_back_to_the_titled_order_without_scores() -> None:
    """A reranker that ranks without scoring (a chat model behind the resolving
    reranker) is called once, on the titled text, and its order is kept."""
    store, tenant, _ = await _titled_store(["first", "second"], title="guide")
    reranker = _ScoringReranker({"[Document: guide]\n\nsecond": 0.9}, unscored=True)
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0,)), reranker=reranker)

    results = await retriever.search(tenant_id=tenant, base_names=["kb"], query="q", limit=5)

    assert [r.content for r in results] == ["second", "first"]
    assert len(reranker.calls) == 1


async def test_two_view_rerank_skips_the_body_view_for_untitled_chunks() -> None:
    """Chunks ingested before titles were stored read the same in both views:
    one call, no second charge."""
    store, tenant, _ = await _titled_store(["first", "second"], title=None)
    reranker = _ScoringReranker({"second": 0.9, "first": 0.1})
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0,)), reranker=reranker)

    results = await retriever.search(tenant_id=tenant, base_names=["kb"], query="q", limit=5)

    assert [r.content for r in results] == ["second", "first"]
    assert len(reranker.calls) == 1
