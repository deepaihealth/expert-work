"""B-206 K-7b — a knowledge base's rerank-score threshold: what clears it is kept
and a search it turns everything down for finds nothing, as before; and the
threshold is bound to the rerank model it was set under (left out, and reported,
when another model scored)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import KnowledgeChunk
from orchestrator.tools import KnowledgeRetriever, KnowledgeSearchTool, ToolContext
from orchestrator.tools.knowledge_rerank import RerankRanking

_SET_UNDER = "qwen/qwen3-vl-rerank"
_OTHER = "qwen/qwen3-rerank"


class _Embedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        return [(1.0,) for _ in texts]


class _ModelReranker:
    """Scores a document by the body it ends with (both views of a chunk score
    alike) and says which model scored, as a vendor reranker does."""

    def __init__(self, scores: dict[str, float], *, model: str | None = _SET_UNDER) -> None:
        self.scores = scores
        self.model = model

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        raise AssertionError("the knowledge search ranks with scores")

    async def rerank_scored(
        self,
        *,
        query: str,
        documents: Sequence[str],
        top_k: int,
        tenant_id: UUID,
        instruction: str | None = None,
    ) -> list[tuple[int, float | None]]:
        def score(document: str) -> float:
            return next((s for body, s in self.scores.items() if document.endswith(body)), 0.0)

        order = sorted(range(len(documents)), key=lambda i: (-score(documents[i]), i))[:top_k]
        return RerankRanking(((i, score(documents[i])) for i in order), model=self.model)


async def _store(
    bodies: Sequence[str],
    *,
    threshold: float | None,
    set_under: str | None = _SET_UNDER,
    title: str | None = "guide",
    store: InMemoryKnowledgeStore | None = None,
    tenant: UUID | None = None,
    name: str = "kb",
) -> tuple[InMemoryKnowledgeStore, UUID]:
    store = store if store is not None else InMemoryKnowledgeStore()
    tenant = tenant if tenant is not None else uuid4()
    base = await store.create_base(
        tenant_id=tenant,
        name=name,
        rerank_score_threshold=threshold,
        rerank_threshold_model=set_under,
    )
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename=f"{name}.md")
    chunks = [
        KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant,
            kb_id=base.id,
            document_id=document.id,
            chunk_index=i,
            content=body,
            embedding=(1.0,),
            doc_title=title,
        )
        for i, body in enumerate(bodies)
    ]
    await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=chunks)
    return store, tenant


async def _search(
    store: InMemoryKnowledgeStore,
    tenant: UUID,
    reranker: Any,
    *,
    query: str = "q",
    view: str = "dual",
    **kwargs: Any,
) -> Any:
    retriever = KnowledgeRetriever(
        store=store,
        embedder=_Embedder(),
        reranker=reranker,
        rerank_view=view,  # type: ignore[arg-type]
    )
    return await retriever.search_detailed(
        tenant_id=tenant, base_names=["kb"], query=query, limit=5, **kwargs
    )


# --- the threshold itself, as before K-7b -----------------------------------------


async def test_a_threshold_keeps_what_clears_it() -> None:
    store, tenant = await _store(["high", "low"], threshold=0.5)
    outcome = await _search(store, tenant, _ModelReranker({"high": 0.9, "low": 0.2}))
    assert [r.content for r in outcome.results] == ["high"]
    assert (outcome.status, outcome.rerank_outcome) == ("ok", "ok")


async def test_a_threshold_that_turns_every_candidate_down_finds_nothing() -> None:
    store, tenant = await _store(["near", "far"], threshold=0.7)
    outcome = await _search(store, tenant, _ModelReranker({"near": 0.69, "far": 0.2}))
    assert outcome.results == ()
    assert (outcome.status, outcome.candidates) == ("below_threshold", 2)
    assert (outcome.rerank_outcome, outcome.rerank_degraded) == ("ok", False)


# --- the threshold is bound to the model it was set under -------------------------


async def test_a_threshold_set_under_another_model_is_left_out_and_reported() -> None:
    store, tenant = await _store(["a", "b"], threshold=0.9, set_under=_SET_UNDER)
    outcome = await _search(store, tenant, _ModelReranker({"a": 0.5, "b": 0.05}, model=_OTHER))
    assert [r.content for r in outcome.results] == ["a", "b"]
    assert (outcome.rerank_outcome, outcome.rerank_threshold_paused) == ("ok", True)


async def test_the_same_threshold_applies_under_its_own_model() -> None:
    store, tenant = await _store(["a", "b"], threshold=0.9, set_under=_SET_UNDER)
    outcome = await _search(store, tenant, _ModelReranker({"a": 0.95, "b": 0.05}, model=_SET_UNDER))
    assert [r.content for r in outcome.results] == ["a"]
    assert (outcome.rerank_outcome, outcome.rerank_threshold_paused) == ("ok", False)


async def test_the_model_is_known_through_both_views_and_the_single_view() -> None:
    for title, view in ((None, "dual"), ("guide", "dual"), ("guide", "single")):
        store, tenant = await _store(["a", "b"], threshold=0.9, title=title)
        reranker = _ModelReranker({"a": 0.5, "b": 0.05}, model=_OTHER)
        outcome = await _search(store, tenant, reranker, view=view)
        assert outcome.rerank_threshold_paused is True, (title, view)
        assert len(outcome.results) == 2, (title, view)


async def test_the_search_names_the_model_that_scored_it() -> None:
    store, tenant = await _store(["a"], threshold=None)
    scored = await _search(store, tenant, _ModelReranker({"a": 0.5}, model=_OTHER))
    assert scored.rerank_model == _OTHER
    unscored = await _search(store, tenant, _UnscoredModelReranker({}, model=_OTHER))
    assert unscored.rerank_model is None


async def test_an_empty_search_still_reports_a_threshold_left_out() -> None:
    """One base's threshold set under another model is left out; another base's
    own threshold still turns every candidate down."""
    store, tenant = await _store(["a", "b"], threshold=0.7, set_under=_OTHER)
    await store.create_base(
        tenant_id=tenant, name="stale", rerank_score_threshold=0.7, rerank_threshold_model=None
    )
    retriever = KnowledgeRetriever(
        store=store,
        embedder=_Embedder(),
        reranker=_ModelReranker({"a": 0.1, "b": 0.1}, model=_OTHER),
    )
    outcome = await retriever.search_detailed(
        tenant_id=tenant, base_names=["kb", "stale"], query="q", limit=5
    )
    assert (outcome.status, outcome.rerank_outcome) == ("below_threshold", "ok")
    assert outcome.rerank_threshold_paused is True


async def test_a_threshold_set_before_models_were_recorded_is_left_out() -> None:
    store, tenant = await _store(["a", "b"], threshold=0.9, set_under=None)
    outcome = await _search(store, tenant, _ModelReranker({"a": 0.5, "b": 0.05}))
    assert len(outcome.results) == 2
    assert outcome.rerank_threshold_paused is True


async def test_a_reranker_that_does_not_name_its_model_keeps_the_threshold() -> None:
    store, tenant = await _store(["a", "b"], threshold=0.9)
    outcome = await _search(store, tenant, _ModelReranker({"a": 0.95, "b": 0.05}, model=None))
    assert [r.content for r in outcome.results] == ["a"]
    assert outcome.rerank_threshold_paused is False


async def test_an_explicit_threshold_applies_whichever_model_scored() -> None:
    """The retrieval test / eval passing a threshold is calibrating it now."""
    store, tenant = await _store(["a", "b"], threshold=0.9, set_under=_SET_UNDER)
    outcome = await _search(
        store,
        tenant,
        _ModelReranker({"a": 0.5, "b": 0.05}, model=_OTHER),
        rerank_score_threshold=0.4,
    )
    assert [r.content for r in outcome.results] == ["a"]
    assert (outcome.rerank_outcome, outcome.rerank_threshold_paused) == ("ok", False)


class _UnscoredModelReranker(_ModelReranker):
    """Names its model but answers without scores (a body it could not read)."""

    async def rerank_scored(
        self,
        *,
        query: str,
        documents: Sequence[str],
        top_k: int,
        tenant_id: UUID,
        instruction: str | None = None,
    ) -> list[tuple[int, float | None]]:
        return RerankRanking(((i, None) for i in range(len(documents))), model=self.model)


async def test_no_scores_means_nothing_to_pause() -> None:
    store, tenant = await _store(["a", "b"], threshold=0.9)
    off = await _search(store, tenant, _ModelReranker({"a": 0.5}, model=_OTHER), rerank=False)
    assert off.rerank_threshold_paused is False
    unscored = await _search(store, tenant, _UnscoredModelReranker({}, model=_OTHER))
    assert len(unscored.results) == 2
    assert unscored.rerank_threshold_paused is False


# --- what the tool reports ----------------------------------------------------------


async def _tool_meta(
    store: InMemoryKnowledgeStore, tenant: UUID, reranker: Any, query: str = "q"
) -> Any:
    retriever = KnowledgeRetriever(store=store, embedder=_Embedder(), reranker=reranker)
    tool = KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",))
    return (await tool.call({"query": query}, ctx=ToolContext(tenant_id=tenant))).meta


async def test_the_tool_reports_a_paused_threshold() -> None:
    store, tenant = await _store(["a", "b"], threshold=0.9)
    meta = await _tool_meta(store, tenant, _ModelReranker({"a": 0.5}, model=_OTHER))
    assert meta == {"hits": 2, "status": "ok", "rerank_threshold_paused": True}


async def test_the_tool_reports_a_threshold_that_turned_everything_down() -> None:
    store, tenant = await _store(["a", "b"], threshold=0.7)
    meta = await _tool_meta(store, tenant, _ModelReranker({"a": 0.2, "b": 0.1}))
    assert meta == {"hits": 0, "status": "below_threshold"}
