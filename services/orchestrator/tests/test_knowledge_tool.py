"""Unit tests for J.5 knowledge retrieval — retriever, reranker, tool."""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage

from expert_work.common.search.rrf import rrf_fuse as _rrf_fuse
from expert_work.common.search.rrf import rrf_fuse_scored
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import (
    KnowledgeChunk,
    RetrievalMethod,
    ScoredChunk,
    chunk_retrieval_text,
)
from orchestrator.errors import PlatformNotConfiguredError
from orchestrator.llm.rerank import (
    DASHSCOPE_RERANK_URL,
    DashScopeReranker,
    HTTPDashScopeRerankClient,
)
from orchestrator.tools import (
    KnowledgeRetriever,
    KnowledgeSearchTool,
    LLMReranker,
    Reranker,
    Tool,
    ToolBlockedError,
    ToolContext,
)
from orchestrator.tools.error_classifier import GuidedToolError, classify_tool_error
from orchestrator.tools.knowledge import (
    DEFAULT_RERANK_POOL,
    DEFAULT_RERANK_VIEW,
    KNOWLEDGE_ANSWERABILITY_RULE,
    KNOWLEDGE_SEARCH_GUIDANCE,
    RERANK_DOC_MAX_CHARS,
    RERANK_INSTRUCTION,
    RerankRanking,
    _parse_rerank_order,
)
from orchestrator.tools.rerank_passage import rerank_passage
from orchestrator.usage_metering import ScopedReranker, UsageIdentity

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


async def test_every_document_handed_to_the_reranker_is_cut_to_the_cap() -> None:
    # One over-long candidate made DashScope refuse the whole request, so the
    # search lost its rerank (B-198); every caller cuts them first (B-200).
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="guide.pdf")
    chunk = _chunk(
        tenant=tenant, kb=base.id, doc=document.id, index=0, content="x" * 5000, embedding=(1.0,)
    ).model_copy(update={"heading_path": ("Costs",)})
    await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=[chunk])
    reranker = _RecordingReranker()
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0,)), reranker=reranker)
    await retriever.search(tenant_id=tenant, base_names=["kb"], query="x", limit=5)
    (sent,) = reranker.documents
    assert sent == chunk.retrieval_text[:RERANK_DOC_MAX_CHARS]


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


def _dual(store: InMemoryKnowledgeStore, reranker: Any) -> KnowledgeRetriever:
    """The two-view rerank (B-175 wave 2 PR2b), kept as the ``dual`` view since
    B-206 K-14 made the single view the default: its scores and order are pinned
    by the tests below."""
    return KnowledgeRetriever(
        store=store, embedder=_FixedEmbedder((1.0,)), reranker=reranker, rerank_view="dual"
    )


async def test_two_view_rerank_cuts_both_views_to_the_cap() -> None:
    store, tenant, _chunks = await _titled_store(["y" * 5000, "z" * 10], title="guide")
    reranker = _ScoringReranker({})
    retriever = _dual(store, reranker)
    await retriever.search(tenant_id=tenant, base_names=["kb"], query="y", limit=5)
    assert len(reranker.calls) == 2
    assert all(len(doc) <= RERANK_DOC_MAX_CHARS for docs, _ in reranker.calls for doc in docs)


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
    retriever = _dual(store, reranker)

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
    retriever = _dual(store, reranker)

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
    retriever = _dual(store, reranker)

    results = await retriever.search(tenant_id=tenant, base_names=["kb"], query="q", limit=5)

    assert [r.content for r in results] == ["second", "first"]
    assert len(reranker.calls) == 1


async def test_two_view_rerank_skips_the_body_view_for_untitled_chunks() -> None:
    """Chunks ingested before titles were stored read the same in both views:
    one call, no second charge."""
    store, tenant, _ = await _titled_store(["first", "second"], title=None)
    reranker = _ScoringReranker({"second": 0.9, "first": 0.1})
    retriever = _dual(store, reranker)

    results = await retriever.search(tenant_id=tenant, base_names=["kb"], query="q", limit=5)

    assert [r.content for r in results] == ["second", "first"]
    assert len(reranker.calls) == 1


# ---------------------------------------------------------------------------
# B-206 K-14 — single view, rerank pool, task instruction
# ---------------------------------------------------------------------------


class _InstructedScoringReranker(_ScoringReranker):
    """A scoring reranker that takes a task instruction; records what it got."""

    def __init__(self, scores: dict[str, float]) -> None:
        super().__init__(scores)
        self.instructions: list[str | None] = []

    async def rerank_scored(
        self,
        *,
        query: str,
        documents: Sequence[str],
        top_k: int,
        tenant_id: UUID,
        instruction: str | None = None,
    ) -> list[tuple[int, float | None]]:
        self.instructions.append(instruction)
        return await super().rerank_scored(
            query=query, documents=documents, top_k=top_k, tenant_id=tenant_id
        )


def _single(store: InMemoryKnowledgeStore, reranker: Any) -> KnowledgeRetriever:
    """A retriever judging the single view (an internal option; dual is the default)."""
    return KnowledgeRetriever(
        store=store, embedder=_FixedEmbedder((1.0,)), reranker=reranker, rerank_view="single"
    )


async def test_single_view_judges_every_candidate_once_on_its_passage() -> None:
    """One call, each candidate as its structured passage, the whole pool ranked
    (a threshold can still fill ``limit``), and each result carries the score it
    was ranked by."""
    store, tenant, chunks = await _titled_store(["answer", "boilerplate", "other"], title="guide")
    reranker = _ScoringReranker(
        {"title: guide\ncontent: answer": 0.9, "title: guide\ncontent: boilerplate": 0.4}
    )
    retriever = _single(store, reranker)

    results = await retriever.search(tenant_id=tenant, base_names=["kb"], query="q", limit=2)

    ((documents, top_k),) = reranker.calls
    assert sorted(documents) == sorted(rerank_passage(chunk) for chunk in chunks)
    assert top_k == 3
    assert [(r.content, r.rerank_score) for r in results] == [
        ("answer", 0.9),
        ("boilerplate", 0.4),
    ]


async def test_single_clean_view_sends_the_cleaned_body() -> None:
    store, tenant, chunks = await _titled_store(["**dose** 2 g"], title="guide")
    reranker = _ScoringReranker({})
    retriever = replace(_single(store, reranker), rerank_view="single_clean")

    await retriever.search(tenant_id=tenant, base_names=["kb"], query="q", limit=5)

    assert reranker.calls == [(["title: guide\ncontent: dose 2 g"], 1)]
    assert rerank_passage(chunks[0], clean=True) == "title: guide\ncontent: dose 2 g"


async def test_single_view_passages_are_cut_to_the_cap() -> None:
    store, tenant, _ = await _titled_store(["y" * 5000], title="guide")
    reranker = _ScoringReranker({})
    await _single(store, reranker).search(tenant_id=tenant, base_names=["kb"], query="y", limit=5)
    ((documents, _top_k),) = reranker.calls
    assert documents == [("title: guide\ncontent: " + "y" * 5000)[:RERANK_DOC_MAX_CHARS]]


async def test_a_ranking_only_reranker_gets_the_single_view_passages() -> None:
    store, tenant, chunks = await _titled_store(["first", "second"], title="guide")
    reranker = _RecordingReranker()
    await _single(store, reranker).search(tenant_id=tenant, base_names=["kb"], query="q", limit=5)
    assert sorted(reranker.documents) == sorted(rerank_passage(chunk) for chunk in chunks)


async def test_the_task_instruction_goes_only_with_a_single_view_when_enabled() -> None:
    store, tenant, _ = await _titled_store(["first", "second"], title="guide")
    # Off (the default): not even ``instruction=None`` — a scoring reranker
    # written before K-14 takes no such argument, and must not degrade.
    off = _ScoringReranker({})
    off_outcome = await _single(store, off).search_detailed(
        tenant_id=tenant, base_names=["kb"], query="q", limit=5
    )
    on = _InstructedScoringReranker({})
    await replace(_single(store, on), rerank_instruction=True).search(
        tenant_id=tenant, base_names=["kb"], query="q", limit=5
    )
    assert len(off.calls) == 1
    assert off_outcome.rerank_degraded is False
    assert on.instructions == [RERANK_INSTRUCTION]
    assert RERANK_INSTRUCTION == (
        "Given a question, judge whether the passage content itself states the answer; "
        "the title only identifies the document"
    )


def test_an_instruction_with_the_dual_view_is_refused() -> None:
    """The dual view sends no instruction; a retriever set up to send one with it
    would silently not, and a run would be recorded as instructed when it was not."""
    with pytest.raises(ValueError, match="rerank_instruction needs a single view"):
        KnowledgeRetriever(
            store=InMemoryKnowledgeStore(),
            embedder=_FixedEmbedder((1.0,)),
            rerank_view="dual",
            rerank_instruction=True,
        )
    with pytest.raises(ValueError, match="rerank_instruction needs a single view"):
        replace(
            KnowledgeRetriever(store=InMemoryKnowledgeStore(), embedder=_FixedEmbedder((1.0,))),
            rerank_instruction=True,
        )


async def test_the_metering_wrapper_passes_an_instruction_on_and_none_when_absent() -> None:
    identity = UsageIdentity(
        store=None, agent_name="a", agent_version="1", usage_kind="platform_overhead"
    )
    instructed = _InstructedScoringReranker({})
    await ScopedReranker(inner=instructed, identity=identity).rerank_scored(
        query="q", documents=["a"], top_k=1, tenant_id=uuid4(), instruction="judge it"
    )
    # A scoring reranker that predates instructions is not handed ``instruction=None``.
    plain = _ScoringReranker({})
    await ScopedReranker(inner=plain, identity=identity).rerank_scored(
        query="q", documents=["a"], top_k=1, tenant_id=uuid4()
    )
    assert instructed.instructions == ["judge it"]
    assert plain.calls == [(["a"], 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rerank_pool", "limit", "recall_limit", "candidates"),
    [(None, 5, None, 100), (50, 5, None, 50), (None, 5, 30, 60), (150, 5, 150, 150)],
)
async def test_the_rerank_pool_is_the_top_of_the_fused_candidates(
    rerank_pool: int | None, limit: int, recall_limit: int | None, candidates: int
) -> None:
    # Two bases x 120 chunks: fusion yields up to 240 candidates. The default
    # pool is 100 (it was the 50-deep recall before K-14); a smaller fusion is
    # judged whole. The model's own per-request cap is the reranker's to apply.
    store = InMemoryKnowledgeStore()
    tenant = await _seed_bases(store, bases=2, per_base=120)
    reranker = _CountingReranker()
    retriever = KnowledgeRetriever(
        store=store,
        embedder=_FixedEmbedder((1.0, 0.0)),
        reranker=reranker,
        recall_limit=recall_limit,
    )
    if rerank_pool is not None:
        retriever = replace(retriever, rerank_pool=rerank_pool)
    await retriever.search(tenant_id=tenant, base_names=["kb0", "kb1"], query="policy", limit=limit)
    assert reranker.sizes == [candidates]


def test_the_product_defaults_are_dual_view_pool_100_no_instruction() -> None:
    retriever = KnowledgeRetriever(store=InMemoryKnowledgeStore(), embedder=_FixedEmbedder((1.0,)))
    assert (retriever.rerank_view, retriever.rerank_pool, retriever.rerank_instruction) == (
        "dual",
        100,
        False,
    )
    assert (DEFAULT_RERANK_VIEW, DEFAULT_RERANK_POOL) == ("dual", 100)


async def _split_paths_store(
    *, bases: int, per_base: int
) -> tuple[InMemoryKnowledgeStore, UUID, list[KnowledgeChunk]]:
    """Bases ``kb0``.. of ``per_base`` titled chunks with a section. The vector
    path favours the last chunks of a base and the keyword path the first, so
    the two 50-deep paths fuse into up to 100 candidates per base (the fused
    order is fixed)."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    chunks: list[KnowledgeChunk] = []
    for b in range(bases):
        base = await store.create_base(tenant_id=tenant, name=f"kb{b}")
        document = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename=f"guide{b}.pdf"
        )
        in_base = [
            _chunk(
                tenant=tenant,
                kb=base.id,
                doc=document.id,
                index=i,
                content=f"policy clause {i} 条款",
                embedding=(1.0, (per_base - 1 - i) / per_base),
            ).model_copy(update={"doc_title": "guide", "heading_path": ("Policy",)})
            for i in range(per_base)
        ]
        await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=in_base)
        chunks += in_base
    return store, tenant, chunks


@pytest.mark.asyncio
@pytest.mark.parametrize(("bases", "per_base", "titled"), [(2, 120, 100), (1, 30, 30)])
async def test_the_default_titled_view_judges_the_top_100_fused_candidates(
    bases: int, per_base: int, titled: int
) -> None:
    """Default dual / 100: the titled view gets ``min(fused, 100)`` candidates
    (200 fused → 100; 30 → 30) as their retrieval text, top 20 asked back."""
    store, tenant, _ = await _split_paths_store(bases=bases, per_base=per_base)
    reranker = _ScoringReranker({})
    retriever = KnowledgeRetriever(
        store=store, embedder=_FixedEmbedder((1.0, 0.0)), reranker=reranker
    )
    names = [f"kb{b}" for b in range(bases)]
    await retriever.search(tenant_id=tenant, base_names=names, query="policy", limit=5)
    (titled_docs, titled_top_k), *_ = reranker.calls
    assert len(titled_docs) == titled
    assert titled_top_k == 20
    # The titled view's text, not a single-view passage.
    assert all(doc.startswith("[Document: guide]\n[Section: Policy]") for doc in titled_docs)


#: sha256 over the request bodies the pre-K-14 code (``63cabc03f``) POSTs for
#: :func:`_golden_dual_store` — limit 5, then limit 20; titled then body view
#: each. Recomputed with that commit's ``knowledge.py`` / ``llm/rerank.py``.
_PRE_K14_REQUESTS_SHA256 = "a3eeac4e177cf7afdfa5658c2d465825d57555f3f202830e4587f39fb1a512ff"


async def _golden_dual_store() -> tuple[InMemoryKnowledgeStore, UUID, list[KnowledgeChunk]]:
    """One base of 120 chunks fusing into 100 candidates: more than 50, so the
    pre-K-14 pool of 50 is full and cut."""
    return await _split_paths_store(bases=1, per_base=120)


def _pre_k14_bodies(query: str, fused: Sequence[KnowledgeChunk], limit: int) -> list[bytes]:
    """What the pre-K-14 code sent: the fused top 50 as retrieval text with
    ``top_n = max(limit, 20)``, then the titled top 20 (the fake answers in input
    order) as body text with ``top_n = 20``."""
    titled = [chunk.retrieval_text[:RERANK_DOC_MAX_CHARS] for chunk in fused[:50]]
    bodies = [
        chunk_retrieval_text(chunk.heading_path, chunk.content)[:RERANK_DOC_MAX_CHARS]
        for chunk in fused[:20]
    ]
    return [
        httpx.Request(
            "POST",
            DASHSCOPE_RERANK_URL,
            json={
                "model": "qwen3-vl-rerank",
                "input": {"query": {"text": query}, "documents": [{"text": d} for d in docs]},
                "parameters": {"return_documents": False, "top_n": top_n},
            },
        ).content
        for docs, top_n in ((titled, max(limit, 20)), (bodies, 20))
    ]


async def test_dual_pool_50_sends_the_pre_k14_requests_byte_for_byte() -> None:
    """``--rerank-view dual --rerank-pool 50`` is the pre-K-14 product: the same
    HTTP bodies, so its rerank-cache rows still answer it."""
    store, tenant, chunks = await _golden_dual_store()
    sent: list[bytes] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        top_n = json.loads(request.content)["parameters"]["top_n"]
        results = [{"index": i, "relevance_score": 1 - i / 1000} for i in range(top_n)]
        return httpx.Response(200, json={"output": {"results": results}})

    client = HTTPDashScopeRerankClient(api_key="k", transport=httpx.MockTransport(_handler))
    retriever = KnowledgeRetriever(
        store=store,
        embedder=_FixedEmbedder((1.0, 0.0)),
        reranker=DashScopeReranker(client=client, model="qwen3-vl-rerank"),
        rerank_view="dual",
        rerank_pool=50,
    )
    # Same per-path recall depth (50) as a 5- or 20-result search.
    plain = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0, 0.0)), recall_limit=50)
    by_id = {chunk.id: chunk for chunk in chunks}
    expected: list[bytes] = []
    for limit in (5, 20):
        order = await plain.search(
            tenant_id=tenant, base_names=["kb0"], query="policy", limit=50, rerank=False
        )
        fused = [by_id[r.chunk_id] for r in order if r.chunk_id is not None]
        expected += _pre_k14_bodies("policy", fused, limit)
        await retriever.search(tenant_id=tenant, base_names=["kb0"], query="policy", limit=limit)

    assert sent == expected
    assert hashlib.sha256(b"".join(sent)).hexdigest() == _PRE_K14_REQUESTS_SHA256


class _CappedReranker(_ScoringReranker):
    """Judges only the leading ``judged`` documents of a call — a model whose
    per-request cap is below the pool — and says how many it left out."""

    def __init__(self, judged: int) -> None:
        super().__init__({})
        self.judged = judged

    async def rerank_scored(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[tuple[int, float | None]]:
        ranked = await super().rerank_scored(
            query=query, documents=documents[: self.judged], top_k=top_k, tenant_id=tenant_id
        )
        return RerankRanking(ranked, dropped=max(0, len(documents) - self.judged))


async def test_candidates_the_reranker_never_judged_are_reported() -> None:
    """A rerank over part of the pool is not silent: the outcome and the tool's
    meta say how many candidates were never judged (dual: titled view 30 → 25)."""
    store, tenant, _ = await _titled_store([f"passage {i:02d}" for i in range(30)], title="guide")
    dual = await _dual(store, _CappedReranker(judged=25)).search_detailed(
        tenant_id=tenant, base_names=["kb"], query="q", limit=5
    )
    single = await _single(store, _CappedReranker(judged=25)).search_detailed(
        tenant_id=tenant, base_names=["kb"], query="q", limit=5
    )
    whole = await _dual(store, _ScoringReranker({})).search_detailed(
        tenant_id=tenant, base_names=["kb"], query="q", limit=5
    )
    assert (dual.rerank_dropped, single.rerank_dropped, whole.rerank_dropped) == (5, 5, 0)

    ctx = ToolContext(tenant_id=tenant)
    capped = KnowledgeSearchTool(
        retriever=_dual(store, _CappedReranker(judged=25)), knowledge_base_refs=("kb",)
    )
    plain = KnowledgeSearchTool(
        retriever=_dual(store, _ScoringReranker({})), knowledge_base_refs=("kb",)
    )
    assert (await capped.call({"query": "q"}, ctx=ctx)).meta["rerank_dropped"] == 5
    assert "rerank_dropped" not in (await plain.call({"query": "q"}, ctx=ctx)).meta

    await _set_threshold(store, tenant, 0.99)  # everything judged falls below it
    below = await _dual(store, _CappedReranker(judged=25)).search_detailed(
        tenant_id=tenant, base_names=["kb"], query="q", limit=5
    )
    assert (below.status, below.rerank_dropped) == ("below_threshold", 5)


# ---------------------------------------------------------------------------
# B-175 W2 PR3 — rerank scores, rerank-score threshold, rerank failure, outcomes
# ---------------------------------------------------------------------------


class _RaisingReranker:
    """A reranker whose backend is down (e.g. DashScope answering 500)."""

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        raise RuntimeError("dashscope rerank failed: 500")

    async def rerank_scored(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[tuple[int, float | None]]:
        raise RuntimeError("dashscope rerank failed: 500")


class _EmptyReranker:
    """A reranker that answers with no results for a non-empty candidate list."""

    async def rerank_scored(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[tuple[int, float | None]]:
        return []

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        return []


class _TimingOutEmbedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        raise TimeoutError("embedding endpoint timed out")


class _UnscoredBodyViewReranker(_ScoringReranker):
    """Scores the titled view but returns the body view without scores."""

    async def rerank_scored(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[tuple[int, float | None]]:
        ranked = await super().rerank_scored(
            query=query, documents=documents, top_k=top_k, tenant_id=tenant_id
        )
        if len(self.calls) == 1:
            return ranked
        return [(index, None) for index, _score in ranked]


async def _set_threshold(store: InMemoryKnowledgeStore, tenant: UUID, value: float) -> None:
    base = await store.get_base(tenant_id=tenant, name="kb")
    assert base is not None
    await store.update_base(tenant_id=tenant, kb_id=base.id, rerank_score_threshold=value)


def _scored(store: InMemoryKnowledgeStore, reranker: Any) -> KnowledgeRetriever:
    # These tests predate B-206 K-14 and key their scores on the dual view's texts.
    return _dual(store, reranker)


async def test_each_result_reports_its_rerank_score() -> None:
    store, tenant, _ = await _titled_store(["first", "second"], title=None)
    retriever = _scored(store, _ScoringReranker({"second": 0.9, "first": 0.4}))

    results = await retriever.search(tenant_id=tenant, base_names=["kb"], query="q", limit=5)

    assert [(r.content, r.rerank_score) for r in results] == [("second", 0.9), ("first", 0.4)]


async def test_two_view_results_report_the_weighted_score_they_were_ranked_by() -> None:
    store, tenant, _ = await _titled_store(["answer", "boilerplate"], title="guide")
    retriever = _scored(
        store,
        _ScoringReranker(
            {
                "[Document: guide]\n\nanswer": 0.80,
                "[Document: guide]\n\nboilerplate": 0.90,
                "answer": 0.90,
                "boilerplate": 0.10,
            }
        ),
    )

    results = await retriever.search(tenant_id=tenant, base_names=["kb"], query="q", limit=5)

    assert [r.content for r in results] == ["answer", "boilerplate"]
    assert [r.rerank_score for r in results] == pytest.approx([0.83, 0.66])


async def test_rerank_threshold_keeps_hits_at_or_above_it() -> None:
    store, tenant, _ = await _titled_store(["high", "edge", "low"], title=None)
    await _set_threshold(store, tenant, 0.5)
    retriever = _scored(store, _ScoringReranker({"high": 0.9, "edge": 0.5, "low": 0.49}))

    outcome = await retriever.search_detailed(
        tenant_id=tenant, base_names=["kb"], query="q", limit=5
    )

    assert [r.content for r in outcome.results] == ["high", "edge"]
    assert outcome.status == "ok"


async def test_every_hit_below_the_threshold_is_a_below_threshold_outcome() -> None:
    store, tenant, _ = await _titled_store(["a", "b", "c"], title=None)
    await _set_threshold(store, tenant, 0.7)
    retriever = _scored(store, _ScoringReranker({"a": 0.69, "b": 0.3, "c": 0.1}))

    outcome = await retriever.search_detailed(
        tenant_id=tenant, base_names=["kb"], query="q", limit=5
    )

    assert outcome.results == ()
    assert outcome.status == "below_threshold"
    assert outcome.candidates == 3


async def test_threshold_override_beats_the_base_setting() -> None:
    store, tenant, _ = await _titled_store(["a", "b"], title=None)
    await _set_threshold(store, tenant, 0.95)
    retriever = _scored(store, _ScoringReranker({"a": 0.9, "b": 0.2}))

    outcome = await retriever.search_detailed(
        tenant_id=tenant, base_names=["kb"], query="q", limit=5, rerank_score_threshold=0.5
    )

    assert [r.content for r in outcome.results] == ["a"]


async def test_threshold_does_not_apply_without_rerank_scores() -> None:
    """A chat-model reranker ranks without scores: nothing to compare, keep all."""
    store, tenant, _ = await _titled_store(["a", "b"], title=None)
    await _set_threshold(store, tenant, 0.99)
    retriever = _scored(store, _ScoringReranker({"b": 0.9}, unscored=True))

    outcome = await retriever.search_detailed(
        tenant_id=tenant, base_names=["kb"], query="q", limit=5
    )

    assert [r.content for r in outcome.results] == ["b", "a"]
    assert [r.rerank_score for r in outcome.results] == [None, None]


async def test_threshold_does_not_apply_when_rerank_is_off() -> None:
    store, tenant, _ = await _titled_store(["a", "b"], title=None)
    await _set_threshold(store, tenant, 0.99)
    retriever = _scored(store, _ScoringReranker({"a": 0.1, "b": 0.1}))

    outcome = await retriever.search_detailed(
        tenant_id=tenant, base_names=["kb"], query="q", limit=5, rerank=False
    )

    assert len(outcome.results) == 2
    assert outcome.status == "ok"


async def test_each_base_applies_its_own_threshold() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    strict = await store.create_base(tenant_id=tenant, name="strict", rerank_score_threshold=0.5)
    loose = await store.create_base(tenant_id=tenant, name="loose")
    for base, body in ((strict, "strict passage"), (loose, "loose passage")):
        document = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename=f"{base.name}.pdf"
        )
        await store.replace_chunks(
            tenant_id=tenant,
            document_id=document.id,
            chunks=[
                _chunk(
                    tenant=tenant,
                    kb=base.id,
                    doc=document.id,
                    index=0,
                    content=body,
                    embedding=(1.0,),
                )
            ],
        )
    retriever = _scored(store, _ScoringReranker({"strict passage": 0.3, "loose passage": 0.3}))

    outcome = await retriever.search_detailed(
        tenant_id=tenant, base_names=["strict", "loose"], query="q", limit=5
    )

    assert [r.content for r in outcome.results] == ["loose passage"]


async def test_a_strict_base_dropping_the_top_hits_leaves_room_for_another_base() -> None:
    """The threshold applies before the cut to ``limit``: the strict base's hits
    outrank the loose base's, all fall under its floor, and the loose base's hit
    below the cut still comes back instead of a ``below_threshold``."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    strict = await store.create_base(tenant_id=tenant, name="strict", rerank_score_threshold=0.8)
    loose = await store.create_base(tenant_id=tenant, name="loose")
    for base, bodies in ((strict, ("strict one", "strict two")), (loose, ("loose one",))):
        document = await store.upsert_document(
            tenant_id=tenant, kb_id=base.id, filename=f"{base.name}.pdf"
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
                    content=body,
                    embedding=(1.0,),
                )
                for i, body in enumerate(bodies)
            ],
        )
    retriever = _scored(
        store, _ScoringReranker({"strict one": 0.7, "strict two": 0.6, "loose one": 0.3})
    )

    outcome = await retriever.search_detailed(
        tenant_id=tenant, base_names=["strict", "loose"], query="q", limit=1
    )

    assert [r.content for r in outcome.results] == ["loose one"]
    assert outcome.status == "ok"
    assert outcome.candidates == 3


async def test_an_unscored_body_view_keeps_the_titled_order_without_scores() -> None:
    """Titled-only scores run higher than the weighted scores a threshold is
    calibrated on, so when the body view comes back unscored the hits carry no
    score and the threshold lets them through."""
    store, tenant, _ = await _titled_store(["answer", "boilerplate"], title="guide")
    await _set_threshold(store, tenant, 0.85)
    reranker = _UnscoredBodyViewReranker(
        {"[Document: guide]\n\nanswer": 0.90, "[Document: guide]\n\nboilerplate": 0.80}
    )

    outcome = await _scored(store, reranker).search_detailed(
        tenant_id=tenant, base_names=["kb"], query="q", limit=5
    )

    assert len(reranker.calls) == 2
    assert [(r.content, r.rerank_score) for r in outcome.results] == [
        ("answer", None),
        ("boilerplate", None),
    ]


async def test_a_failing_reranker_degrades_to_the_fused_order() -> None:
    """Rerank is an optional quality pass: a vendor 500 must not fail the search
    (it used to surface as a tool error / console 500). No scores, so the
    threshold does not apply either."""
    store, tenant = await _seed_store()
    await _set_threshold(store, tenant, 0.99)
    plain = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0, 0.0)))
    failing = KnowledgeRetriever(
        store=store, embedder=_FixedEmbedder((1.0, 0.0)), reranker=_RaisingReranker()
    )

    expected = await plain.search(tenant_id=tenant, base_names=["kb"], query="policy", limit=5)
    outcome = await failing.search_detailed(
        tenant_id=tenant, base_names=["kb"], query="policy", limit=5
    )

    assert [r.chunk_id for r in outcome.results] == [r.chunk_id for r in expected]
    assert outcome.rerank_degraded is True
    assert outcome.status == "ok"


async def test_an_empty_rerank_answer_degrades_instead_of_finding_nothing() -> None:
    store, tenant = await _seed_store()
    retriever = KnowledgeRetriever(
        store=store, embedder=_FixedEmbedder((1.0, 0.0)), reranker=_EmptyReranker()
    )

    outcome = await retriever.search_detailed(
        tenant_id=tenant, base_names=["kb"], query="policy", limit=5
    )

    assert len(outcome.results) == 2
    assert outcome.rerank_degraded is True


async def test_unknown_bases_and_empty_bases_are_different_outcomes() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    await store.create_base(tenant_id=tenant, name="kb")  # exists, holds nothing
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0, 0.0)))

    missing = await retriever.search_detailed(
        tenant_id=tenant, base_names=["gone"], query="q", limit=5
    )
    empty = await retriever.search_detailed(tenant_id=tenant, base_names=["kb"], query="q", limit=5)

    assert (missing.status, missing.results) == ("no_bases", ())
    assert (empty.status, empty.results) == ("no_hits", ())


# --- the knowledge_search tool: what the model reads for each outcome ---


async def test_results_carry_the_answerability_notice_outside_the_content() -> None:
    """The rule reaches the model as a trusted platform notice; the fenced
    content stays the passages alone."""
    store, tenant = await _seed_store()
    result = await _tool(store).call({"query": "deductible"}, ctx=ToolContext(tenant_id=tenant))

    assert result.notice is not None
    assert "explicitly states is not supported by the knowledge base" in result.notice
    assert result.notice not in result.content
    assert result.meta["status"] == "ok"


async def test_no_hits_keeps_its_message_and_suggests_one_rephrased_search() -> None:
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    await store.create_base(tenant_id=tenant, name="kb")
    result = await _tool(store).call({"query": "anything"}, ctx=ToolContext(tenant_id=tenant))

    assert result.content.startswith("[no relevant knowledge found for the query")
    assert result.notice is not None
    assert "a search in their terms" in result.notice
    assert result.meta == {"hits": 0, "status": "no_hits"}


async def test_below_threshold_says_how_many_passages_were_turned_down() -> None:
    store, tenant, _ = await _titled_store(["a", "b", "c"], title=None)
    await _set_threshold(store, tenant, 0.7)
    retriever = _scored(store, _ScoringReranker({"a": 0.2, "b": 0.2, "c": 0.2}))
    tool = KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",))

    result = await tool.call({"query": "q"}, ctx=ToolContext(tenant_id=tenant))

    assert result.content.startswith("[no relevant knowledge found for the query")
    assert "3 passages" in result.content
    assert result.meta == {"hits": 0, "status": "below_threshold"}


async def test_missing_bases_are_a_guided_error_not_an_empty_result() -> None:
    tool = _tool(InMemoryKnowledgeStore())
    with pytest.raises(GuidedToolError) as raised:
        await tool.call({"query": "q"}, ctx=ToolContext(tenant_id=uuid4()))
    assert "says nothing about whether the information exists" in raised.value.advice


class _TimingOutKeywordStore(InMemoryKnowledgeStore):
    async def keyword_search_scored(
        self, *, tenant_id: UUID, kb_ids: Sequence[UUID], query: str, limit: int = 5
    ) -> list[ScoredChunk]:
        raise TimeoutError("database statement timed out")


async def test_a_timed_out_search_stays_a_transient_failure() -> None:
    """A failed search propagates as-is, so an outage keeps the ``transient``
    class (no effort escalation over a blip) and its generic advice. (A failed
    query *embedding* no longer fails the search — it degrades to keywords.)"""
    store = _TimingOutKeywordStore()
    tenant = uuid4()
    await store.create_base(tenant_id=tenant, name="kb")
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder((1.0, 0.0)))
    tool = KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",))

    with pytest.raises(TimeoutError) as raised:
        await tool.call({"query": "q"}, ctx=ToolContext(tenant_id=tenant))

    classified = classify_tool_error(
        tool_name="knowledge_search", error=raised.value, spec=tool.spec
    )
    assert classified.error_class == "transient"


async def test_a_degraded_rerank_is_flagged_in_meta() -> None:
    store, tenant = await _seed_store()
    result = await _tool(store, reranker=_RaisingReranker()).call(
        {"query": "policy"}, ctx=ToolContext(tenant_id=tenant)
    )
    assert result.meta["rerank_degraded"] is True
    assert result.meta["hits"] == 2


# ---------------------------------------------------------------------------
# B-206 K-8 —— a failed query embedding degrades to keyword search, visibly
# ---------------------------------------------------------------------------


async def test_a_failed_query_embedding_falls_back_to_keyword_search(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, tenant = await _seed_store()
    retriever = KnowledgeRetriever(store=store, embedder=_TimingOutEmbedder())
    with caplog.at_level("WARNING", logger="orchestrator.tools.knowledge"):
        outcome = await retriever.search_detailed(
            tenant_id=tenant, base_names=["kb"], query="vacation", limit=5
        )
    assert outcome.status == "ok"
    assert outcome.embedding_degraded is True
    assert [(r.chunk_index, r.recall_source) for r in outcome.results] == [(1, "keyword")]
    assert any(
        "knowledge.embedding_degraded" in record.getMessage() and record.exc_info
        for record in caplog.records
    )


async def test_a_vector_only_base_still_searches_keywords_when_embedding_fails() -> None:
    store, tenant = await _seed_store()
    retriever = KnowledgeRetriever(store=store, embedder=_TimingOutEmbedder())
    outcome = await retriever.search_detailed(
        tenant_id=tenant,
        base_names=["kb"],
        query="deductible",
        limit=5,
        method=RetrievalMethod.VECTOR,
    )
    assert outcome.embedding_degraded is True
    assert [r.chunk_index for r in outcome.results] == [0]


async def test_a_degraded_embedding_is_flagged_in_meta() -> None:
    store, tenant = await _seed_store()
    retriever = KnowledgeRetriever(store=store, embedder=_TimingOutEmbedder())
    tool = KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",))
    result = await tool.call({"query": "vacation"}, ctx=ToolContext(tenant_id=tenant))
    assert result.meta["embedding_degraded"] is True
    assert result.meta["hits"] == 1
    # Nothing found by keywords either: still flagged, not a plain "no hits".
    missed = await tool.call({"query": "zzz"}, ctx=ToolContext(tenant_id=tenant))
    assert missed.meta == {"hits": 0, "status": "no_hits", "embedding_degraded": True}


def _embed_status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://embed.test/v1/embeddings")
    response = httpx.Response(code, request=request, text="upstream failure")
    return httpx.HTTPStatusError(
        f"embeddings request failed: {code}", request=request, response=response
    )


class _RaisingEmbedder:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        raise self._exc


@pytest.mark.parametrize(
    "exc",
    [
        _embed_status_error(503),
        _embed_status_error(400),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("refused"),
        TimeoutError("deadline"),
        ConnectionResetError("reset"),
    ],
    ids=["5xx", "4xx", "read-timeout", "connect", "timeout", "os-error"],
)
async def test_every_vendor_or_transport_failure_degrades_to_keywords(exc: Exception) -> None:
    store, tenant = await _seed_store()
    retriever = KnowledgeRetriever(store=store, embedder=_RaisingEmbedder(exc))
    outcome = await retriever.search_detailed(
        tenant_id=tenant, base_names=["kb"], query="vacation", limit=5
    )
    assert outcome.embedding_degraded is True
    assert [r.chunk_index for r in outcome.results] == [1]


@pytest.mark.parametrize(
    "exc",
    [
        # Embedding not configured for this deployment — a setup error the
        # search reported before K-8 and still must, not a degraded search.
        PlatformNotConfiguredError("platform embedding is not configured"),
        # A bug, or a body that is not an embeddings response: keyword results
        # flagged "degraded" on every search would hide it.
        TypeError("unexpected keyword argument"),
        KeyError("data"),
    ],
    ids=["not-configured", "type-error", "malformed-body"],
)
async def test_errors_that_are_not_vendor_failures_propagate(exc: Exception) -> None:
    store, tenant = await _seed_store()
    retriever = KnowledgeRetriever(store=store, embedder=_RaisingEmbedder(exc))
    with pytest.raises(type(exc)):
        await retriever.search_detailed(
            tenant_id=tenant, base_names=["kb"], query="vacation", limit=5
        )


async def test_a_healthy_search_is_not_flagged() -> None:
    store, tenant = await _seed_store()
    result = await _tool(store).call({"query": "vacation"}, ctx=ToolContext(tenant_id=tenant))
    assert "embedding_degraded" not in result.meta


class _QuerySidedEmbedder:
    """The platform embedder's shape: ``embed`` (memory, documents) and
    ``embed_query`` (knowledge search queries, B-206 K-5)."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        raise AssertionError("a knowledge search query must go through embed_query")

    async def embed_query(self, query: str, *, tenant_id: UUID) -> tuple[float, ...]:
        self.queries.append(query)
        return (1.0, 0.0)


async def test_a_search_query_goes_through_embed_query_when_the_embedder_has_one() -> None:
    store, tenant = await _seed_store()
    embedder = _QuerySidedEmbedder()
    retriever = KnowledgeRetriever(store=store, embedder=embedder)
    outcome = await retriever.search_detailed(
        tenant_id=tenant, base_names=["kb"], query="deductible", limit=5
    )
    assert embedder.queries == ["deductible"]
    assert outcome.embedding_degraded is False
    assert outcome.results[0].recall_source == "both"


def test_the_tool_description_states_the_answerability_rule() -> None:
    assert KNOWLEDGE_ANSWERABILITY_RULE in _tool(InMemoryKnowledgeStore()).spec.description


def test_the_tool_description_states_how_to_search_and_conclude() -> None:
    """B-206 K-6 — search / verify guidance, phrase by phrase."""
    description = _tool(InMemoryKnowledgeStore()).spec.description
    for phrase in (
        KNOWLEDGE_SEARCH_GUIDANCE,
        "several small, targeted searches rather than one broad one",
        "search again in the documents' own terms",
        "do not repeat the same or a very similar query",
        "all / every / how many",
        "until a search returns no document you have not seen",
        "against every condition in the question before counting it",
        "verify each condition in the question against text you have actually read",
        "conflicting documents are reported as conflicting, not resolved by guessing",
        "Say you searched and found nothing only when a search actually ran",
    ):
        assert phrase in description, phrase


def test_the_tool_description_names_no_tool_that_does_not_exist() -> None:
    # Reading a candidate in full / listing documents come with K-2 / K-13.
    description = _tool(InMemoryKnowledgeStore()).spec.description
    for absent in ("knowledge_read", "knowledge_list_documents", "list_documents"):
        assert absent not in description, absent
