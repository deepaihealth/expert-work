"""B-206 K-7c — the reranker over a Cohere-style client: the catalog's length
limits in the model's own unit, every scale's scores on one footing, and a pool
larger than one request split across requests only when their scores compare."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

import pytest

from expert_work.protocol.model_catalog import RerankCaps
from orchestrator.llm.rerank_batches import CohereStyleReranker
from orchestrator.llm.rerank_guard import RerankCallError
from orchestrator.tools.knowledge_rerank import RerankRanking


class _Scorer:
    """A fake Cohere-style client: each document's score is ``scores[text]``,
    answered best first, ``top_n`` of them; records every request."""

    def __init__(self, scores: Mapping[str, float] | None = None) -> None:
        self.scores = dict(scores or {})
        self.requests: list[dict[str, Any]] = []

    async def rerank(
        self,
        *,
        model: str,
        query: str,
        documents: Sequence[str],
        top_n: int,
        instruction: str | None = None,
    ) -> Mapping[str, Any]:
        self.requests.append(
            {
                "query": query,
                "documents": list(documents),
                "top_n": top_n,
                "instruction": instruction,
            }
        )
        rows = [
            {"index": i, "relevance_score": self.scores.get(text, 0.0)}
            for i, text in enumerate(documents)
        ]
        rows.sort(key=lambda row: -row["relevance_score"])
        return {"results": rows[:top_n]}


def _caps(**overrides: Any) -> RerankCaps:
    return RerankCaps(**{"rerank_api": "cohere_style", **overrides})


async def _scored(
    reranker: CohereStyleReranker,
    documents: Sequence[str],
    *,
    top_k: int = 10,
    query: str = "q",
    instruction: str | None = None,
) -> list[tuple[int, float | None]]:
    extra = {} if instruction is None else {"instruction": instruction}
    return await reranker.rerank_scored(
        query=query, documents=documents, top_k=top_k, tenant_id=uuid4(), **extra
    )


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


# ─── scores ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_logit_scores_come_back_through_a_sigmoid() -> None:
    client = _Scorer({"a": 22.265625, "b": 4.0351562, "c": -3.0})
    ranked = await _scored(
        CohereStyleReranker(client=client, model="m", caps=_caps(score_scale="logit")),
        ["a", "b", "c"],
    )
    assert [index for index, _ in ranked] == [0, 1, 2]
    assert [score for _, score in ranked] == pytest.approx(
        [_sigmoid(22.265625), _sigmoid(4.0351562), _sigmoid(-3.0)], rel=1e-12
    )
    assert isinstance(ranked, RerankRanking)
    assert (ranked.outcome, ranked.dropped) == ("ok", 0)
    # The raw logits stay on the ranking, for merging / combining in logit space.
    assert ranked.logits == {0: 22.265625, 1: 4.0351562, 2: -3.0}


@pytest.mark.asyncio
@pytest.mark.parametrize("scale", ["normalized", "sigmoid", "relative", None])
async def test_only_a_logit_model_carries_logits(scale: str | None) -> None:
    ranked = await _scored(
        CohereStyleReranker(client=_Scorer({"a": 0.5}), model="m", caps=_caps(score_scale=scale)),
        ["a"],
    )
    assert isinstance(ranked, RerankRanking)
    assert ranked.logits is None


@pytest.mark.asyncio
async def test_a_split_pool_merges_on_the_logit_not_the_saturated_sigmoid() -> None:
    """Logits 40 and 45 both read 1.0 after a sigmoid in double precision; the
    merge must still put 45 first, whichever request it came back from."""
    client = _Scorer({"d0": 40.0, "d1": -2.0, "d2": 45.0})
    caps = _caps(max_documents=2, score_scale="logit")
    ranked = await _scored(
        CohereStyleReranker(client=client, model="m", caps=caps), ["d0", "d1", "d2"], top_k=2
    )
    assert _sigmoid(40.0) == _sigmoid(45.0) == 1.0
    assert [index for index, _ in ranked] == [2, 0]
    assert isinstance(ranked, RerankRanking)
    assert ranked.logits == {2: 45.0, 0: 40.0}


@pytest.mark.asyncio
async def test_a_very_negative_logit_does_not_overflow() -> None:
    client = _Scorer({"a": -1000.0, "b": 1000.0})
    ranked = await _scored(
        CohereStyleReranker(client=client, model="m", caps=_caps(score_scale="logit")),
        ["a", "b"],
    )
    assert ranked == [(1, 1.0), (0, 0.0)]


@pytest.mark.asyncio
@pytest.mark.parametrize("scale", ["normalized", "sigmoid", "relative", None])
async def test_other_scales_keep_the_vendor_score(scale: str | None) -> None:
    client = _Scorer({"a": 0.25, "b": 3.5})
    ranked = await _scored(
        CohereStyleReranker(client=client, model="m", caps=_caps(score_scale=scale)), ["a", "b"]
    )
    assert ranked == [(1, 3.5), (0, 0.25)]


@pytest.mark.asyncio
async def test_rows_it_cannot_read_are_left_out_or_unscored() -> None:
    class _Client:
        async def rerank(self, **_: Any) -> Mapping[str, Any]:
            return {
                "results": [
                    {"index": 2, "relevance_score": "high"},
                    {"index": 9, "relevance_score": 0.9},
                    {"index": True, "relevance_score": 0.8},
                    {"index": None, "relevance_score": 0.7},
                    {"index": 0, "relevance_score": 0.5},
                ]
            }

    ranked = await _scored(CohereStyleReranker(client=_Client(), model="m"), ["a", "b", "c"])
    assert ranked == [(2, None), (0, 0.5)]


@pytest.mark.asyncio
async def test_an_answer_without_results_is_a_model_error() -> None:
    class _Client:
        async def rerank(self, **_: Any) -> Mapping[str, Any]:
            return {}

    ranked = await _scored(
        CohereStyleReranker(client=_Client(), model="m"), ["a", "b", "c"], top_k=2
    )
    assert ranked == [(0, None), (1, None)]
    assert isinstance(ranked, RerankRanking)
    assert ranked.outcome == "model_error"


@pytest.mark.asyncio
async def test_empty_documents_send_nothing() -> None:
    client = _Scorer()
    assert await _scored(CohereStyleReranker(client=client, model="m"), []) == []
    assert client.requests == []


@pytest.mark.asyncio
async def test_plain_rerank_returns_indices() -> None:
    reranker = CohereStyleReranker(client=_Scorer({"b": 1.0}), model="m")
    out = await reranker.rerank(query="q", documents=["a", "b"], top_k=1, tenant_id=uuid4())
    assert out == [1]


# ─── lengths ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_char_model_gets_query_and_documents_cut_to_its_limits() -> None:
    client = _Scorer()
    caps = _caps(length_unit="char", max_doc_tokens=4, query_max_tokens=3)
    await _scored(
        CohereStyleReranker(client=client, model="m", caps=caps),
        ["法律条文", "法律条文第五条", "ab"],
        query="问题很长",
    )
    (request,) = client.requests
    assert request["query"] == "问题很"
    assert request["documents"] == ["法律条文", "法律条文", "ab"]


@pytest.mark.asyncio
async def test_a_token_model_is_sent_the_text_as_handed() -> None:
    client = _Scorer()
    caps = _caps(max_doc_tokens=4, query_max_tokens=3)
    await _scored(
        CohereStyleReranker(client=client, model="m", caps=caps),
        ["法律条文第五条"],
        query="问题很长",
    )
    (request,) = client.requests
    assert (request["query"], request["documents"]) == ("问题很长", ["法律条文第五条"])


class _RefusesLong(_Scorer):
    """Refuses (``too_large``) any request carrying more than ``limit`` UTF-8
    bytes of query and documents."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    async def rerank(self, **kwargs: Any) -> Mapping[str, Any]:
        size = len(kwargs["query"].encode()) + sum(len(d.encode()) for d in kwargs["documents"])
        if size > self.limit:
            self.requests.append({"refused": size})
            raise RerankCallError("too long", failure="too_large", status=400)
        return await super().rerank(**kwargs)


@pytest.mark.asyncio
async def test_a_size_refusal_is_answered_once_with_what_surely_fits() -> None:
    """A token model refused for size: the query and each document cut to their
    limits in UTF-8 bytes, then the leading documents within the total."""
    client = _RefusesLong(limit=30)
    caps = _caps(max_doc_tokens=6, query_max_tokens=3, max_total_tokens=20)
    ranked = await _scored(
        CohereStyleReranker(client=client, model="m", caps=caps),
        ["法律条文", "abcdefgh", "xyz", "tail"],
        query="问题",
    )
    refused, sent = client.requests
    assert refused == {"refused": 6 + 12 + 8 + 3 + 4}
    # "问题" is 6 bytes → "问"; documents cut to 6 bytes; each charged with the query.
    assert sent["query"] == "问"
    assert sent["documents"] == ["法律", "abcdef"]
    assert isinstance(ranked, RerankRanking)
    assert ranked.dropped == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rejected", "rate_limited", "transient"])
async def test_other_refusals_are_not_answered_with_less(failure: str) -> None:
    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        async def rerank(self, **_: Any) -> Mapping[str, Any]:
            self.calls += 1
            raise RerankCallError("no", failure=failure)  # type: ignore[arg-type]

    client = _Client()
    caps = _caps(max_doc_tokens=1)  # a size refusal would be answered with "a"
    with pytest.raises(RerankCallError):
        await _scored(CohereStyleReranker(client=client, model="m", caps=caps), ["abc"])
    assert client.calls == 1


@pytest.mark.asyncio
async def test_a_size_refusal_nothing_can_shrink_is_raised() -> None:
    client = _RefusesLong(limit=0)
    with pytest.raises(RerankCallError):
        await _scored(CohereStyleReranker(client=client, model="m", caps=_caps()), ["abc"])
    assert len(client.requests) == 1


# ─── pool size ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("scale", ["normalized", "sigmoid", "logit"])
async def test_a_pool_over_the_cap_is_split_and_merged_by_score(scale: str) -> None:
    scores = {"d0": 0.1, "d1": 0.7, "d2": 0.3, "d3": 0.9, "d4": 0.5}
    client = _Scorer(scores)
    caps = _caps(max_documents=3, score_scale=scale)
    ranked = await _scored(
        CohereStyleReranker(client=client, model="m", caps=caps), list(scores), top_k=2
    )
    assert [request["documents"] for request in client.requests] == [
        ["d0", "d1", "d2"],
        ["d3", "d4"],
    ]
    # Each request asks for at most top_k — the best top_k overall are among them.
    assert [request["top_n"] for request in client.requests] == [2, 2]
    assert [index for index, _ in ranked] == [3, 1]
    assert isinstance(ranked, RerankRanking)
    assert ranked.dropped == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scale", ["relative", None])
async def test_scores_that_do_not_compare_keep_the_leading_documents(scale: str | None) -> None:
    """Relative scores (or an undeclared scale) only order one request's
    documents: the leading ``max_documents`` are judged, the rest reported."""
    client = _Scorer({"d0": 0.1, "d1": 0.7, "d2": 0.9})
    caps = _caps(max_documents=2, score_scale=scale)
    ranked = await _scored(
        CohereStyleReranker(client=client, model="m", caps=caps), ["d0", "d1", "d2"]
    )
    assert [request["documents"] for request in client.requests] == [["d0", "d1"]]
    assert [index for index, _ in ranked] == [1, 0]
    assert isinstance(ranked, RerankRanking)
    assert ranked.dropped == 1


@pytest.mark.asyncio
async def test_an_off_catalog_model_sends_the_whole_pool_at_once() -> None:
    client = _Scorer()
    await _scored(CohereStyleReranker(client=client, model="m"), [f"d{i}" for i in range(300)])
    assert [len(request["documents"]) for request in client.requests] == [300]


@pytest.mark.asyncio
async def test_the_split_requests_are_in_flight_together() -> None:
    """The batches are sent at once; how many run together is the guard's
    concurrency cap (:class:`~orchestrator.llm.rerank_guard.RerankGuards`)."""
    both_sent = asyncio.Event()
    in_flight = 0

    class _Client(_Scorer):
        async def rerank(self, **kwargs: Any) -> Mapping[str, Any]:
            nonlocal in_flight
            in_flight += 1
            if in_flight == 2:
                both_sent.set()
            await asyncio.wait_for(both_sent.wait(), timeout=1.0)
            return await super().rerank(**kwargs)

    caps = _caps(max_documents=1, score_scale="normalized")
    ranked = await _scored(CohereStyleReranker(client=_Client(), model="m", caps=caps), ["a", "b"])
    assert len(ranked) == 2


@pytest.mark.asyncio
async def test_one_failed_request_fails_the_split_rerank() -> None:
    class _Client(_Scorer):
        async def rerank(self, **kwargs: Any) -> Mapping[str, Any]:
            if kwargs["documents"] == ["b"]:
                raise RerankCallError("429", failure="rate_limited", status=429)
            return await super().rerank(**kwargs)

    caps = _caps(max_documents=1, score_scale="normalized")
    with pytest.raises(RerankCallError):
        await _scored(CohereStyleReranker(client=_Client(), model="m", caps=caps), ["a", "b"])


@pytest.mark.asyncio
async def test_the_first_failure_cancels_the_other_requests() -> None:
    cancelled = asyncio.Event()

    class _Client(_Scorer):
        async def rerank(self, **kwargs: Any) -> Mapping[str, Any]:
            if kwargs["documents"] == ["b"]:
                raise RerankCallError("429", failure="rate_limited", status=429)
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return await super().rerank(**kwargs)

    caps = _caps(max_documents=1, score_scale="normalized")
    with pytest.raises(RerankCallError) as excinfo:
        await asyncio.wait_for(
            _scored(CohereStyleReranker(client=_Client(), model="m", caps=caps), ["a", "b"]),
            timeout=2.0,
        )
    assert excinfo.value.failure == "rate_limited"
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_a_classified_failure_is_raised_over_an_unclassified_one() -> None:
    """Two requests failing in the same loop turn (the unclassified one first):
    the one that says how to report it wins."""
    release = asyncio.Event()

    class _Client(_Scorer):
        async def rerank(self, **kwargs: Any) -> Mapping[str, Any]:
            await release.wait()
            if kwargs["documents"] == ["a"]:
                raise ValueError("unclassified")
            raise RerankCallError("429", failure="rate_limited", status=429)

    asyncio.get_running_loop().call_later(0.05, release.set)
    caps = _caps(max_documents=1, score_scale="normalized")
    with pytest.raises(RerankCallError):
        await _scored(CohereStyleReranker(client=_Client(), model="m", caps=caps), ["a", "b"])


@pytest.mark.asyncio
async def test_one_unreadable_answer_makes_the_split_rerank_a_model_error() -> None:
    class _Client(_Scorer):
        async def rerank(self, **kwargs: Any) -> Mapping[str, Any]:
            if kwargs["documents"] == ["b"]:
                return {}
            return await super().rerank(**kwargs)

    caps = _caps(max_documents=1, score_scale="normalized")
    ranked = await _scored(CohereStyleReranker(client=_Client(), model="m", caps=caps), ["a", "b"])
    assert ranked == [(0, None), (1, None)]
    assert isinstance(ranked, RerankRanking)
    assert ranked.outcome == "model_error"


@pytest.mark.asyncio
async def test_a_char_model_packs_requests_within_its_total() -> None:
    """Query (counted once) + documents stay within ``max_total_tokens``; a
    comparable scale splits the pool rather than dropping its tail."""
    client = _Scorer({"cccc": 0.9})
    caps = _caps(length_unit="char", max_total_tokens=10, score_scale="logit")
    ranked = await _scored(
        CohereStyleReranker(client=client, model="m", caps=caps),
        ["aaa", "bbb", "cccc", "dd"],
        query="qq",
    )
    assert [request["documents"] for request in client.requests] == [
        ["aaa", "bbb"],
        ["cccc", "dd"],
    ]
    assert ranked[0][0] == 2  # the best one, from the second request
    assert isinstance(ranked, RerankRanking)
    assert ranked.dropped == 0


@pytest.mark.asyncio
async def test_a_char_model_with_relative_scores_keeps_what_fits_one_request() -> None:
    client = _Scorer()
    caps = _caps(length_unit="char", max_total_tokens=10, score_scale="relative")
    ranked = await _scored(
        CohereStyleReranker(client=client, model="m", caps=caps),
        ["aaa", "bbb", "cccc", "dd"],
        query="qq",
    )
    assert [request["documents"] for request in client.requests] == [["aaa", "bbb"]]
    assert isinstance(ranked, RerankRanking)
    assert ranked.dropped == 2


@pytest.mark.asyncio
async def test_a_char_document_longer_than_the_total_allows_is_cut_to_fit() -> None:
    client = _Scorer()
    caps = _caps(length_unit="char", max_total_tokens=6, score_scale="logit")
    await _scored(
        CohereStyleReranker(client=client, model="m", caps=caps), ["abcdefgh"], query="qq"
    )
    assert client.requests[0]["documents"] == ["abcd"]


@pytest.mark.asyncio
async def test_a_split_pool_judges_every_slice_against_the_same_query() -> None:
    """A size refusal of one slice is answered with a shorter query only if the
    others were sent it too: the query is cut to its limit before any request."""

    class _RefusesSecond(_Scorer):
        async def rerank(self, **kwargs: Any) -> Mapping[str, Any]:
            if kwargs["documents"][0].startswith("b") and len(kwargs["documents"][0]) > 3:
                self.requests.append({"refused": kwargs["documents"]})
                raise RerankCallError("too long", failure="too_large", status=400)
            return await super().rerank(**kwargs)

    client = _RefusesSecond()
    caps = _caps(max_documents=1, max_doc_tokens=3, query_max_tokens=3, score_scale="logit")
    await _scored(
        CohereStyleReranker(client=client, model="m", caps=caps), ["aaaaaa", "bbbbbb"], query="问题"
    )
    sent = [request for request in client.requests if "refused" not in request]
    assert sorted(request["documents"][0] for request in sent) == ["aaaaaa", "bbb"]
    assert {request["query"] for request in sent} == {"问"}  # 3 UTF-8 bytes


# ─── instruction ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_instruction_goes_only_to_a_model_that_takes_one() -> None:
    client = _Scorer()
    await _scored(
        CohereStyleReranker(client=client, model="m", caps=_caps(supports_instruction=True)),
        ["a"],
        instruction="judge it",
    )
    await _scored(
        CohereStyleReranker(client=client, model="m", caps=_caps()), ["a"], instruction="judge it"
    )
    await _scored(CohereStyleReranker(client=client, model="m"), ["a"], instruction="judge it")
    assert [request["instruction"] for request in client.requests] == ["judge it", None, None]
