"""B-206 K-7 — how a knowledge search's rerank ended (``rerank_outcome``), the
one time budget both rerank views share, and the guarded DashScope path end to
end (fake transport-free client, injected sleep: no network, no real sleeping)."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.protocol import KnowledgeChunk
from orchestrator.llm.rerank import DashScopeReranker
from orchestrator.llm.rerank_guard import MAX_RETRIES, RerankCallError, RerankGuards
from orchestrator.tools import KnowledgeRetriever, KnowledgeSearchTool, LLMReranker, ToolContext
from orchestrator.tools.knowledge_rerank import (
    RerankBudget,
    RerankOutcome,
    RerankRanking,
    RerankUnavailableError,
    current_rerank_budget,
)

_BODIES = ("first passage", "second passage", "third passage")


class _Embedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        return [(1.0,) for _ in texts]


async def _store() -> tuple[InMemoryKnowledgeStore, UUID]:
    """One base, one titled document, three chunks with the same vector."""
    store = InMemoryKnowledgeStore()
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="doc.md")
    chunks = [
        KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant,
            kb_id=base.id,
            document_id=document.id,
            chunk_index=i,
            content=body,
            embedding=(1.0,),
            doc_title="guide",
        )
        for i, body in enumerate(_BODIES)
    ]
    await store.replace_chunks(tenant_id=tenant, document_id=document.id, chunks=chunks)
    return store, tenant


class _Reranker:
    """A scoring reranker answering each call from ``answer``: an exception is
    raised, a callable maps the documents to a ranking. Records the budget each
    call saw."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.budgets: list[RerankBudget | None] = []

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        raise AssertionError("the knowledge search ranks with scores")

    async def rerank_scored(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[tuple[int, float | None]]:
        self.budgets.append(current_rerank_budget())
        if isinstance(self.answer, Exception):
            raise self.answer
        ranked: list[tuple[int, float | None]] = self.answer(documents)
        return ranked


def _reversed(documents: Sequence[str]) -> list[tuple[int, float | None]]:
    """A healthy model: the input reversed, scored."""
    n = len(documents)
    return [(i, 0.9 - (n - 1 - i) / 100) for i in reversed(range(n))]


def _skipped(outcome: RerankOutcome) -> Any:
    """A resolving reranker with nothing to call: the input order, unscored."""
    return lambda documents: RerankRanking(
        ((i, None) for i in range(len(documents))), outcome=outcome
    )


def _retriever(
    store: InMemoryKnowledgeStore, reranker: Any, *, view: str = "dual"
) -> KnowledgeRetriever:
    return KnowledgeRetriever(
        store=store,
        embedder=_Embedder(),
        reranker=reranker,
        rerank_view=view,  # type: ignore[arg-type]
    )


async def _search(retriever: KnowledgeRetriever, tenant: UUID, **kwargs: Any) -> Any:
    return await retriever.search_detailed(
        tenant_id=tenant, base_names=["kb"], query="q", limit=5, **kwargs
    )


async def test_a_ranked_search_is_ok() -> None:
    store, tenant = await _store()
    outcome = await _search(_retriever(store, _Reranker(_reversed)), tenant)
    assert (outcome.rerank_outcome, outcome.rerank_degraded) == ("ok", False)
    assert [r.content for r in outcome.results] == list(reversed(_BODIES))


async def test_a_search_without_rerank_has_no_outcome() -> None:
    store, tenant = await _store()
    off = await _search(_retriever(store, _Reranker(_reversed)), tenant, rerank=False)
    assert off.rerank_outcome is None


async def test_rerank_asked_for_with_no_reranker_is_no_model() -> None:
    store, tenant = await _store()
    outcome = await _search(_retriever(store, None), tenant)
    assert (outcome.rerank_outcome, outcome.rerank_degraded) == ("no_model", False)


@pytest.mark.parametrize("outcome", ["rate_limited", "model_error"])
async def test_a_rerank_that_gave_up_degrades_with_its_reason(outcome: RerankOutcome) -> None:
    store, tenant = await _store()
    plain = await _search(_retriever(store, None), tenant)
    failing = _Reranker(RerankUnavailableError("gave up", outcome=outcome))
    degraded = await _search(_retriever(store, failing), tenant)
    assert (degraded.rerank_outcome, degraded.rerank_degraded) == (outcome, True)
    assert [r.chunk_id for r in degraded.results] == [r.chunk_id for r in plain.results]


@pytest.mark.parametrize(
    "answer",
    [
        RuntimeError("a bug, or a vendor client that does not classify"),
        RerankCallError("refused", failure="rejected", status=400),
        lambda documents: [],  # an empty answer for a non-empty pool
        _skipped("model_error"),  # an answer the reranker could not read
    ],
)
async def test_every_other_failure_is_a_degraded_model_error(answer: Any) -> None:
    store, tenant = await _store()
    outcome = await _search(_retriever(store, _Reranker(answer)), tenant)
    assert (outcome.rerank_outcome, outcome.rerank_degraded) == ("model_error", True)


@pytest.mark.parametrize("view", ["dual", "single"])
@pytest.mark.parametrize("skipped", ["no_model", "model_unavailable"])
async def test_a_rerank_with_nothing_to_call_says_why_without_degrading(
    view: str, skipped: RerankOutcome
) -> None:
    """``rerank_degraded`` keeps its meaning — the configured model was called
    and failed; a search that could not call one says so in the outcome only."""
    store, tenant = await _store()
    outcome = await _search(_retriever(store, _Reranker(_skipped(skipped)), view=view), tenant)
    assert (outcome.rerank_outcome, outcome.rerank_degraded) == (skipped, False)
    assert [r.content for r in outcome.results] == list(_BODIES)  # the fused order


def _second_view(second: Any) -> Any:
    """A model whose first (titled) view answers healthily and whose second
    (body) view answers ``second``: an exception is raised, a callable maps."""
    calls = 0

    def answer(documents: Sequence[str]) -> list[tuple[int, float | None]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _reversed(documents)
        if isinstance(second, Exception):
            raise second
        ranked: list[tuple[int, float | None]] = second(documents)
        return ranked

    return answer


@pytest.mark.parametrize(
    "second",
    [
        _skipped("model_error"),  # an answer the reranker could not read
        lambda documents: [(i, None) for i in range(len(documents))],  # no scores
        RerankUnavailableError("gave up", outcome="rate_limited"),  # the guard gave up
        RerankCallError("429", failure="rate_limited", status=429),  # unguarded client
    ],
)
async def test_a_second_view_that_fails_keeps_the_first_views_order(
    second: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """B-206 K-7a (M2) — the titled view's paid-for ranking is not thrown away
    when the body view gives nothing usable: the search keeps the titled order
    (unscored — titled-only scores run above the weighted ones a threshold is
    calibrated on) and says ``partial``, not a fallback to the recall order."""
    store, tenant = await _store()
    with caplog.at_level(logging.INFO, logger="orchestrator.tools.knowledge"):
        outcome = await _search(_retriever(store, _Reranker(_second_view(second))), tenant)
    assert (outcome.rerank_outcome, outcome.rerank_degraded) == ("partial", False)
    assert [r.content for r in outcome.results] == list(reversed(_BODIES))  # titled order
    assert [r.rerank_score for r in outcome.results] == [None, None, None]
    lines = [r.getMessage() for r in caplog.records if r.name == "orchestrator.tools.knowledge"]
    assert len(lines) == 1
    assert "outcome=partial" in lines[0]


async def test_a_first_view_that_fails_still_fails_the_search() -> None:
    store, tenant = await _store()
    failing = _Reranker(RerankUnavailableError("gave up", outcome="rate_limited"))
    outcome = await _search(_retriever(store, failing), tenant)
    assert (outcome.rerank_outcome, outcome.rerank_degraded) == ("rate_limited", True)
    assert [r.content for r in outcome.results] == list(_BODIES)  # the fused order


@pytest.mark.parametrize(
    ("failure", "outcome"), [("rate_limited", "rate_limited"), ("transient", "model_error")]
)
async def test_an_unguarded_clients_classified_failure_keeps_its_meaning(
    failure: str, outcome: RerankOutcome
) -> None:
    """B-206 K-7a (L3) — a vendor client nobody guards (the eval's, a test's)
    raises :class:`RerankCallError`; a 429 is still reported as the rate."""
    store, tenant = await _store()
    error = RerankCallError("x", failure=failure, status=429)  # type: ignore[arg-type]
    result = await _search(_retriever(store, _Reranker(error), view="single"), tenant)
    assert (result.rerank_outcome, result.rerank_degraded) == (outcome, True)


class _ChatModel:
    """A chat model for :class:`LLMReranker`: replies ``reply``, or raises it."""

    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply

    async def __call__(self, **_kwargs: Any) -> Any:
        if isinstance(self.reply, Exception):
            raise self.reply
        from langchain_core.messages import AIMessage

        return AIMessage(content=self.reply)


@pytest.mark.parametrize(
    ("reply", "order", "outcome"),
    [
        ("[3, 1]", [2, 0], "ok"),
        ("sorry, I cannot help", [0, 1, 2], "model_error"),
        (RuntimeError("vendor down"), [0, 1, 2], "model_error"),
    ],
)
async def test_a_chat_model_reranker_says_when_it_fell_back(
    reply: str | Exception, order: list[int], outcome: RerankOutcome
) -> None:
    """B-206 K-7a (M7) — the chat-model reranker swallows its failures and
    returns the input order; its ranking says so, while ``rerank`` (long-term
    memory's path) still returns the plain order."""
    reranker = LLMReranker(llm_caller=_ChatModel(reply))
    ranked = await reranker.ranked(query="q", documents=["a", "b", "c"], top_k=3, tenant_id=uuid4())
    assert isinstance(ranked, RerankRanking)
    assert ([i for i, _score in ranked], ranked.outcome) == (order, outcome)
    assert all(score is None for _i, score in ranked)
    plain = await reranker.rerank(query="q", documents=["a", "b", "c"], top_k=3, tenant_id=uuid4())
    assert plain == order and type(plain) is list


async def test_both_views_of_a_search_share_one_budget() -> None:
    store, tenant = await _store()
    reranker = _Reranker(_reversed)
    await _search(_retriever(store, reranker), tenant)
    first, second = reranker.budgets
    assert first is not None and first is second
    assert current_rerank_budget() is None  # it ends with the search


async def test_the_tool_meta_carries_an_outcome_that_is_not_ok() -> None:
    store, tenant = await _store()
    ctx = ToolContext(tenant_id=tenant)

    def tool(reranker: Any) -> KnowledgeSearchTool:
        return KnowledgeSearchTool(
            retriever=_retriever(store, reranker), knowledge_base_refs=("kb",)
        )

    limited = _Reranker(RerankUnavailableError("gave up", outcome="rate_limited"))
    meta = (await tool(limited).call({"query": "q"}, ctx=ctx)).meta
    assert (meta["rerank_outcome"], meta["rerank_degraded"]) == ("rate_limited", True)
    skipped = (await tool(_Reranker(_skipped("no_model"))).call({"query": "q"}, ctx=ctx)).meta
    assert skipped["rerank_outcome"] == "no_model"
    assert "rerank_degraded" not in skipped
    ok = (await tool(_Reranker(_reversed)).call({"query": "q"}, ctx=ctx)).meta
    assert "rerank_outcome" not in ok  # like the other flags: present only when not the norm


class _Throttled:
    """A DashScope client the vendor keeps throttling (HTTP 429)."""

    def __init__(self) -> None:
        self.calls = 0

    async def rerank(self, **_kwargs: Any) -> Mapping[str, Any]:
        self.calls += 1
        raise RerankCallError("429", failure="rate_limited", status=429)


async def test_a_throttled_dashscope_rerank_retries_then_falls_back_and_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """End to end: retriever → DashScope reranker → guard → a vendor that keeps
    answering 429. The search gets the fused order, outcome ``rate_limited``,
    and one log line with the attempts — retries of the second view never
    start because the first already failed the search."""
    store, tenant = await _store()
    sleeps: list[float] = []

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    throttled = _Throttled()
    guards = RerankGuards(sleep=_sleep, rand=lambda: 0.0)
    reranker = DashScopeReranker(
        client=guards.wrap(throttled, provider="qwen", model="qwen3-vl-rerank", secret_ref="s"),
        model="qwen3-vl-rerank",
    )
    with caplog.at_level(logging.INFO, logger="orchestrator.tools.knowledge"):
        outcome = await _search(_retriever(store, reranker), tenant)
    assert (outcome.rerank_outcome, outcome.rerank_degraded) == ("rate_limited", True)
    assert throttled.calls == 1 + MAX_RETRIES
    assert sleeps == [1.0, 1.0, 2.0]  # a 429 waits at least 1 s
    lines = [r.getMessage() for r in caplog.records if r.name == "orchestrator.tools.knowledge"]
    assert len(lines) == 1
    assert "outcome=rate_limited" in lines[0]
    assert f"attempts={1 + MAX_RETRIES}" in lines[0]
