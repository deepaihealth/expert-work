"""B-206 K-7b — a knowledge base's rerank-score threshold: what clears it is kept
and a search it turns everything down for finds nothing, as before; a passage
holding a code the query names is kept under a stored threshold (at most three a
search); and the threshold is bound to the rerank model it was set under (left
out, and reported, when another model scored). Codes here are synthetic."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import KnowledgeChunk
from orchestrator.tools import KnowledgeRetriever, KnowledgeSearchTool, ToolContext
from orchestrator.tools.knowledge_rerank import RerankRanking
from orchestrator.tools.knowledge_threshold import (
    CODE_KEEP_MAX,
    Floors,
    apply_threshold,
    code_kept_notice,
)

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
    assert outcome.rerank_threshold_code_kept == 0


async def test_a_threshold_that_turns_every_candidate_down_finds_nothing() -> None:
    store, tenant = await _store(["near", "far"], threshold=0.7)
    outcome = await _search(store, tenant, _ModelReranker({"near": 0.69, "far": 0.2}))
    assert outcome.results == ()
    assert (outcome.status, outcome.candidates) == ("below_threshold", 2)
    assert (outcome.rerank_outcome, outcome.rerank_degraded) == ("ok", False)


# --- a code the query names keeps its passage ---------------------------------------


async def test_a_passage_holding_the_querys_code_is_kept_under_the_threshold() -> None:
    store, tenant = await _store(["QK7 dose: two a day", "boilerplate"], threshold=0.7)
    reranker = _ModelReranker({"QK7 dose: two a day": 0.1, "boilerplate": 0.05})
    outcome = await _search(store, tenant, reranker, query="QK7 每天吃几次")
    assert [r.content for r in outcome.results] == ["QK7 dose: two a day"]
    assert (outcome.status, outcome.rerank_threshold_code_kept) == ("ok", 1)


async def test_without_the_code_in_its_text_a_passage_is_filtered() -> None:
    store, tenant = await _store(["QK8 dose: two a day", "boilerplate"], threshold=0.7)
    reranker = _ModelReranker({"QK8 dose: two a day": 0.1, "boilerplate": 0.05})
    outcome = await _search(store, tenant, reranker, query="QK7 每天吃几次")
    assert outcome.status == "below_threshold"
    assert outcome.rerank_threshold_code_kept == 0


async def test_a_query_without_a_code_keeps_nothing_under_the_threshold() -> None:
    store, tenant = await _store(["take 2 a day", "b"], threshold=0.7)
    reranker = _ModelReranker({"take 2 a day": 0.1})
    outcome = await _search(store, tenant, reranker, query="每天吃 2 次可以吗")
    assert outcome.status == "below_threshold"


async def test_the_code_in_the_document_title_alone_does_not_keep_a_passage() -> None:
    """Every passage of a file named after a code would match."""
    store, tenant = await _store(["boilerplate"], threshold=0.7, title="ZX-9041 manual")
    outcome = await _search(
        store, tenant, _ModelReranker({"boilerplate": 0.1}), query="ZX-9041 保修多久"
    )
    assert outcome.status == "below_threshold"


def _chunk(content: str, *, kb: UUID, heading: tuple[str, ...] = ()) -> KnowledgeChunk:
    return KnowledgeChunk(
        id=uuid4(),
        tenant_id=uuid4(),
        kb_id=kb,
        document_id=uuid4(),
        chunk_index=0,
        content=content,
        heading_path=heading,
        embedding=(1.0,),
    )


def _cut(contents: Sequence[str], query: str, *, heading: tuple[str, ...] = ()) -> list[str]:
    """What a stored threshold of 0.7 keeps of ``contents``, all scored 0.1."""
    kb = uuid4()
    chunks = [_chunk(content, kb=kb, heading=heading) for content in contents]
    floors = Floors({kb: 0.7}, stored=True)
    cut = apply_threshold(chunks, {c.id: 0.1 for c in chunks}, floors, query=query)
    return [c.content for c in cut.chunks]


@pytest.mark.parametrize(
    "text",
    [
        "model ZX-9041 has an 18-month warranty",
        "MODEL zx-9041",
        "型号ZX-9041的保修",  # CJK on both sides
        "\uff3a\uff38\uff0d\uff19\uff10\uff14\uff11 fullwidth",
        "ZX\u20139041 en dash",
        "ZX 9041 spaced",
        "ZX_9041 underscored",
    ],
)
def test_a_code_matches_across_case_width_and_joiners(text: str) -> None:
    assert _cut([text], "zx-9041 保修多久") == [text]


@pytest.mark.parametrize(
    "text",
    [
        "ZX-90412 is another model",  # a longer number
        "AZX-9041 is another model",  # a longer letter run
        "ZX9041 written as one word",  # one token to the keyword search too
        "ZX-904",
    ],
)
def test_a_code_does_not_match_inside_another(text: str) -> None:
    assert _cut([text], "ZX-9041 保修多久") == []


def test_the_section_path_counts() -> None:
    assert _cut(["see the table"], "AB-N2 用量", heading=("AB-N2", "dosage")) == ["see the table"]


def test_at_most_three_passages_are_kept_for_a_code_best_scored_first() -> None:
    kb = uuid4()
    chunks = [_chunk(f"QK7 note {i}", kb=kb) for i in range(5)]
    scores = {c.id: 0.1 + i / 100 for i, c in enumerate(chunks)}  # the last scores best
    cut = apply_threshold(chunks, scores, Floors({kb: 0.7}, stored=True), query="QK7 怎么用")
    assert CODE_KEEP_MAX == 3
    assert [c.content for c in cut.chunks] == ["QK7 note 2", "QK7 note 3", "QK7 note 4"]
    assert cut.code_kept == {chunks[2].id, chunks[3].id, chunks[4].id}


async def test_the_cap_spans_every_base() -> None:
    store, tenant = await _store(["QK7 one", "QK7 two"], threshold=0.7, name="first")
    await _store(
        ["QK7 three", "QK7 four"], threshold=0.7, store=store, tenant=tenant, name="second"
    )
    reranker = _ModelReranker({"QK7 one": 0.2, "QK7 two": 0.1, "QK7 three": 0.12, "QK7 four": 0.05})
    retriever = KnowledgeRetriever(store=store, embedder=_Embedder(), reranker=reranker)
    outcome = await retriever.search_detailed(
        tenant_id=tenant, base_names=["first", "second"], query="QK7 怎么用", limit=5
    )
    assert [r.content for r in outcome.results] == ["QK7 one", "QK7 three", "QK7 two"]
    assert outcome.rerank_threshold_code_kept == 3


async def test_a_threshold_passed_with_the_search_keeps_no_passage_for_a_code() -> None:
    """The retrieval test / eval passing a threshold is calibrating it: it applies as is."""
    store, tenant = await _store(["QK7 dose: two a day"], threshold=None)
    outcome = await _search(
        store,
        tenant,
        _ModelReranker({"QK7 dose: two a day": 0.1}),
        query="QK7 每天吃几次",
        rerank_score_threshold=0.7,
    )
    assert outcome.status == "below_threshold"
    assert outcome.rerank_threshold_code_kept == 0


async def test_only_returned_results_count_as_kept_for_a_code() -> None:
    store, tenant = await _store(["a", "b", "QK7 note"], threshold=0.5)
    reranker = _ModelReranker({"a": 0.9, "b": 0.8, "QK7 note": 0.1})
    retriever = KnowledgeRetriever(store=store, embedder=_Embedder(), reranker=reranker)
    outcome = await retriever.search_detailed(
        tenant_id=tenant, base_names=["kb"], query="QK7 怎么用", limit=2
    )
    assert [r.content for r in outcome.results] == ["a", "b"]
    assert outcome.rerank_threshold_code_kept == 0


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


async def test_a_threshold_left_out_for_its_model_keeps_nothing_for_a_code() -> None:
    """Left out, it filters nothing — so nothing is kept for a code either."""
    store, tenant = await _store(["ZX-9041 warranty", "b"], threshold=0.9)
    reranker = _ModelReranker({"ZX-9041 warranty": 0.1}, model=_OTHER)
    outcome = await _search(store, tenant, reranker, query="ZX-9041 保修多久")
    assert len(outcome.results) == 2
    assert outcome.rerank_threshold_paused is True
    assert outcome.rerank_threshold_code_kept == 0


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


async def _tool_result(
    store: InMemoryKnowledgeStore, tenant: UUID, reranker: Any, query: str
) -> Any:
    retriever = KnowledgeRetriever(store=store, embedder=_Embedder(), reranker=reranker)
    tool = KnowledgeSearchTool(retriever=retriever, knowledge_base_refs=("kb",))
    return await tool.call({"query": query}, ctx=ToolContext(tenant_id=tenant))


async def test_the_tool_says_how_many_results_were_kept_for_a_code() -> None:
    store, tenant = await _store(["ZX-9041 warranty", "a"], threshold=0.7)
    reranker = _ModelReranker({"a": 0.9, "ZX-9041 warranty": 0.1})
    result = await _tool_result(store, tenant, reranker, "ZX-9041 保修多久")
    assert result.meta == {"hits": 2, "status": "ok", "rerank_threshold_code_kept": 1}
    assert result.notice.endswith(code_kept_notice(1))
    assert "1 of these passages scored below" in code_kept_notice(1)


async def test_the_tool_says_nothing_of_codes_when_none_kept_a_result() -> None:
    store, tenant = await _store(["a"], threshold=0.7)
    result = await _tool_result(store, tenant, _ModelReranker({"a": 0.9}), "ZX-9041 保修多久")
    assert "rerank_threshold_code_kept" not in result.meta
    assert "below the knowledge base's relevance threshold" not in result.notice


async def test_the_tool_reports_a_paused_threshold() -> None:
    store, tenant = await _store(["a", "b"], threshold=0.9)
    meta = await _tool_meta(store, tenant, _ModelReranker({"a": 0.5}, model=_OTHER))
    assert meta == {"hits": 2, "status": "ok", "rerank_threshold_paused": True}


async def test_the_tool_reports_a_threshold_that_turned_everything_down() -> None:
    store, tenant = await _store(["a", "b"], threshold=0.7)
    meta = await _tool_meta(store, tenant, _ModelReranker({"a": 0.2, "b": 0.1}))
    assert meta == {"hits": 0, "status": "below_threshold"}
