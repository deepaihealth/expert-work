"""Unit tests for the DashScope native rerank client + reranker."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

import httpx
import pytest

from expert_work.protocol.model_catalog import RerankCaps, catalog_entry
from orchestrator.llm import DashScopeReranker, HTTPDashScopeRerankClient
from orchestrator.llm.rerank_guard import RerankCallError
from orchestrator.tools.knowledge import RERANK_DOC_MAX_CHARS, RerankRanking, rerank_documents


@pytest.mark.asyncio
async def test_reranker_returns_best_first_indices_truncated() -> None:
    class _Client:
        async def rerank(
            self,
            *,
            model: str,
            query: str,
            documents: Sequence[str],
            top_n: int,
            instruction: str | None = None,
        ) -> Mapping[str, Any]:
            # Best-first order from the API: doc 2, then 0, then 1.
            return {"output": {"results": [{"index": 2}, {"index": 0}, {"index": 1}]}}

    out = await DashScopeReranker(client=_Client(), model="qwen3-vl-rerank").rerank(
        query="q", documents=["a", "b", "c"], top_k=2, tenant_id=uuid4()
    )
    assert out == [2, 0]  # truncated to top_k, best-first


@pytest.mark.asyncio
async def test_reranker_empty_documents() -> None:
    class _Client:
        async def rerank(self, **_: Any) -> Mapping[str, Any]:  # pragma: no cover
            raise AssertionError("should not be called for empty input")

    out = await DashScopeReranker(client=_Client(), model="m").rerank(
        query="q", documents=[], top_k=5, tenant_id=uuid4()
    )
    assert out == []


@pytest.mark.asyncio
async def test_reranker_degrades_on_malformed_body() -> None:
    class _Client:
        async def rerank(self, **_: Any) -> Mapping[str, Any]:
            return {"unexpected": "shape"}

    out = await DashScopeReranker(client=_Client(), model="m").rerank(
        query="q", documents=["a", "b"], top_k=5, tenant_id=uuid4()
    )
    assert out == [0, 1]  # input order


@pytest.mark.asyncio
async def test_reranker_drops_out_of_range_indices() -> None:
    class _Client:
        async def rerank(self, **_: Any) -> Mapping[str, Any]:
            return {"output": {"results": [{"index": 9}, {"index": 0}, {"index": "x"}]}}

    out = await DashScopeReranker(client=_Client(), model="m").rerank(
        query="q", documents=["a", "b"], top_k=5, tenant_id=uuid4()
    )
    assert out == [0]


@pytest.mark.asyncio
async def test_reranker_scored_pairs_each_index_with_its_relevance() -> None:
    class _Client:
        async def rerank(self, **_: Any) -> Mapping[str, Any]:
            return {
                "output": {
                    "results": [
                        {"index": 2, "relevance_score": 0.9},
                        {"index": 0, "relevance_score": 1},
                        {"index": 1},
                        {"index": 3, "relevance_score": True},
                    ]
                }
            }

    out = await DashScopeReranker(client=_Client(), model="m").rerank_scored(
        query="q", documents=["a", "b", "c", "d"], top_k=4, tenant_id=uuid4()
    )
    # A missing or non-numeric score is None, not a guess.
    assert out == [(2, 0.9), (0, 1.0), (1, None), (3, None)]


@pytest.mark.asyncio
async def test_reranker_scored_degrades_to_input_order_without_scores() -> None:
    class _Client:
        async def rerank(self, **_: Any) -> Mapping[str, Any]:
            return {"unexpected": "shape"}

    out = await DashScopeReranker(client=_Client(), model="m").rerank_scored(
        query="q", documents=["a", "b", "c"], top_k=2, tenant_id=uuid4()
    )
    assert out == [(0, None), (1, None)]
    # B-206 K-7 — an answer it cannot read is the model's fault, not a ranking.
    assert isinstance(out, RerankRanking)
    assert out.outcome == "model_error"


@pytest.mark.asyncio
async def test_http_client_posts_native_rerank_shape() -> None:
    seen: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"output": {"results": [{"index": 0}]}})

    client = HTTPDashScopeRerankClient(api_key="sk-x", transport=httpx.MockTransport(_handler))
    body = await client.rerank(
        model="qwen3-vl-rerank", query="什么是排序", documents=["d0", "d1"], top_n=2
    )

    assert body["output"]["results"][0]["index"] == 0
    assert seen["url"].endswith("/text-rerank/text-rerank")
    assert seen["auth"] == "Bearer sk-x"
    assert seen["body"]["model"] == "qwen3-vl-rerank"
    assert seen["body"]["input"]["query"] == {"text": "什么是排序"}
    assert seen["body"]["input"]["documents"] == [{"text": "d0"}, {"text": "d1"}]
    assert seen["body"]["parameters"]["top_n"] == 2


@pytest.mark.asyncio
async def test_http_client_surfaces_error_body() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Model not exist"})

    client = HTTPDashScopeRerankClient(api_key="k", transport=httpx.MockTransport(_handler))
    with pytest.raises(RerankCallError) as excinfo:
        await client.rerank(model="bad", query="q", documents=["a"], top_n=1)
    assert "Model not exist" in str(excinfo.value)
    assert (excinfo.value.failure, excinfo.value.status) == ("rejected", 400)


#: DashScope's answer to a request over the model's rate — the body recorded by
#: the 2026-10-22 probe (``qwen3-vl-rerank``); it sent no ``Retry-After``.
_THROTTLED = {
    "request_id": "r",
    "code": "Throttling.RateQuota",
    "message": "Requests rate limit exceeded, please try again later.",
}

#: DashScope's body for an over-long request, worded as B-200 recorded it from
#: the embedding endpoint (the rerank endpoint's own wording is not on record).
_SIZE_REFUSAL = {
    "code": "InvalidParameter",
    "message": "Range of input length should be [1, 120000]",
}
_NO_MODEL = {"code": "InvalidParameter", "message": "Model not exist."}
_BAD_KEY = {"code": "InvalidApiKey", "message": "Invalid API-key provided."}
_THROTTLING_400 = {"code": "Throttling", "message": "Request rate limit exceeded."}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error", "headers", "failure", "retry_after"),
    [
        (429, _THROTTLED, {}, "rate_limited", None),
        (429, _THROTTLED, {"Retry-After": "2"}, "rate_limited", 2.0),
        (500, {"code": "InternalError", "message": "x"}, {}, "transient", None),
        (503, "<html>Service Unavailable</html>", {}, "transient", None),
        # A 5xx is not about size, whatever its body says.
        (500, _SIZE_REFUSAL, {}, "transient", None),
        (400, _SIZE_REFUSAL, {}, "too_large", None),
        # A 400 that is not about size must surface, not quietly lose the pool's tail.
        (400, _NO_MODEL, {}, "rejected", None),
        (400, _BAD_KEY, {}, "rejected", None),
        (400, "<html>Bad Request</html>", {}, "rejected", None),
        # B-206 K-7a (M5) — DashScope also reports throttling and its own faults
        # as HTTP 400 (tools/eval/longmem/transient.py; B-206 K-8's
        # ``InternalError.Algo.Embedding_pipeline_Error``): the code decides.
        (400, _THROTTLING_400, {}, "rate_limited", None),
        (400, {"code": "Throttling.RateQuota", "message": "x"}, {}, "rate_limited", None),
        (
            400,
            {"code": "InternalError.Algo.Embedding_pipeline_Error", "message": "x"},
            {},
            "transient",
            None,
        ),
        # Only the code counts: the same words in a parameter error's message do not.
        (400, {"code": "InvalidParameter", "message": "Throttling"}, {}, "rejected", None),
    ],
)
async def test_http_client_classifies_each_refusal(
    status: int,
    error: Mapping[str, Any] | str,
    headers: dict[str, str],
    failure: str,
    retry_after: float | None,
) -> None:
    """B-206 K-7 — the client's whole vendor-specific part: what a failed
    request means, for the guard (:mod:`orchestrator.llm.rerank_guard`) to act on."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        if isinstance(error, str):
            return httpx.Response(status, text=error, headers=headers)
        return httpx.Response(status, json=error, headers=headers)

    client = HTTPDashScopeRerankClient(api_key="k", transport=httpx.MockTransport(_handler))
    with pytest.raises(RerankCallError) as excinfo:
        await client.rerank(model="m", query="q", documents=["a"], top_n=1)
    assert (excinfo.value.failure, excinfo.value.status) == (failure, status)
    assert excinfo.value.retry_after_s == retry_after


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("x")],
)
async def test_http_client_reports_an_unreachable_vendor_as_transient(
    error: httpx.TransportError,
) -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        raise error

    client = HTTPDashScopeRerankClient(api_key="k", transport=httpx.MockTransport(_handler))
    with pytest.raises(RerankCallError) as excinfo:
        await client.rerank(model="m", query="q", documents=["a"], top_n=1)
    assert (excinfo.value.failure, excinfo.value.status) == ("transient", None)


def test_rerank_documents_cuts_each_to_the_cap() -> None:
    # The cut every caller applies before a reranker (B-198, shared since
    # B-200): one document over the model's per-document limit makes DashScope
    # refuse the whole request (HTTP 400).
    long = "x" * (RERANK_DOC_MAX_CHARS + 500)
    assert rerank_documents(["short", long]) == ["short", long[:RERANK_DOC_MAX_CHARS]]


# ---------------------------------------------------------------------------
# B-206 K-14 — model caps from the catalog: documents per request, instruction,
# total request size
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Records each request; answers best-first in input order. ``refuse_over``
    makes it refuse with ``failure`` (as a vendor client classifies it) while a
    request carries more than that many documents."""

    def __init__(self, *, refuse_over: int | None = None, failure: str = "too_large") -> None:
        self.requests: list[dict[str, Any]] = []
        self.refuse_over = refuse_over
        self.failure = failure

    async def rerank(self, **kwargs: Any) -> Mapping[str, Any]:
        self.requests.append(kwargs)
        documents = kwargs["documents"]
        if self.refuse_over is not None and len(documents) > self.refuse_over:
            raise RerankCallError(
                "dashscope rerank failed: refused",
                failure=self.failure,  # type: ignore[arg-type]
                status=400,
            )
        results = [{"index": i, "relevance_score": 1 - i / 1000} for i in range(len(documents))]
        return {"output": {"results": results}}


def _caps(model: str) -> RerankCaps:
    entry = catalog_entry("qwen", model)
    assert entry is not None
    assert entry.rerank_caps is not None
    return entry.rerank_caps


@pytest.mark.asyncio
async def test_a_request_never_carries_more_documents_than_the_model_takes() -> None:
    cap = _caps("qwen3-vl-rerank").max_documents
    assert cap == 100
    client = _RecordingClient()
    ranked = await DashScopeReranker(client=client, model="qwen3-vl-rerank").rerank_scored(
        query="q", documents=[f"d{i}" for i in range(150)], top_k=150, tenant_id=uuid4()
    )
    (request,) = client.requests
    assert request["documents"] == [f"d{i}" for i in range(cap)]  # the best-fused first
    assert request["top_n"] == cap  # never more than it was sent
    assert [index for index, _score in ranked] == list(range(cap))
    assert isinstance(ranked, RerankRanking)
    assert ranked.dropped == 50  # the cut is reported, not silent


@pytest.mark.asyncio
async def test_the_text_models_take_a_whole_pool_in_one_request() -> None:
    """B-206 K-7b — 500 documents a request: the 100-candidate pool is never cut."""
    for model in ("qwen3-rerank", "qwen3.7-text-rerank"):
        client = _RecordingClient()
        ranked = await DashScopeReranker(client=client, model=model).rerank_scored(
            query="q", documents=[f"d{i}" for i in range(150)], top_k=150, tenant_id=uuid4()
        )
        assert len(client.requests[0]["documents"]) == 150, model
        assert getattr(ranked, "dropped", 0) == 0, model


@pytest.mark.asyncio
async def test_a_ranking_names_the_model_that_scored_it() -> None:
    """B-206 K-7b — what a base's threshold is checked against."""
    ranked = await DashScopeReranker(client=_RecordingClient(), model="qwen3-rerank").rerank_scored(
        query="q", documents=["a", "b"], top_k=2, tenant_id=uuid4()
    )
    assert isinstance(ranked, RerankRanking)
    assert ranked.model == "qwen/qwen3-rerank"


@pytest.mark.asyncio
async def test_an_off_catalog_model_gets_every_document() -> None:
    client = _RecordingClient()
    ranked = await DashScopeReranker(client=client, model="gte-rerank-v2").rerank_scored(
        query="q", documents=[f"d{i}" for i in range(150)], top_k=150, tenant_id=uuid4()
    )
    assert len(client.requests[0]["documents"]) == 150
    assert getattr(ranked, "dropped", 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("documents", "top_k", "top_n"), [(3, 10, 3), (30, 20, 20)])
async def test_top_n_is_never_more_than_the_documents_sent(
    documents: int, top_k: int, top_n: int
) -> None:
    client = _RecordingClient()
    await DashScopeReranker(client=client, model="qwen3-vl-rerank").rerank_scored(
        query="q", documents=["d"] * documents, top_k=top_k, tenant_id=uuid4()
    )
    assert client.requests[0]["top_n"] == top_n


@pytest.mark.asyncio
async def test_the_instruction_goes_only_to_a_model_that_takes_one() -> None:
    assert _caps("qwen3-vl-rerank").supports_instruction is True
    takes = _RecordingClient()
    await DashScopeReranker(client=takes, model="qwen3-vl-rerank").rerank_scored(
        query="q", documents=["a"], top_k=1, tenant_id=uuid4(), instruction="judge it"
    )
    off_catalog = _RecordingClient()
    await DashScopeReranker(client=off_catalog, model="gte-rerank-v2").rerank_scored(
        query="q", documents=["a"], top_k=1, tenant_id=uuid4(), instruction="judge it"
    )
    none_given = _RecordingClient()
    await DashScopeReranker(client=none_given, model="qwen3-vl-rerank").rerank_scored(
        query="q", documents=["a"], top_k=1, tenant_id=uuid4()
    )
    assert takes.requests[0]["instruction"] == "judge it"
    # Not even ``instruction=None``: a client written before K-14 keeps working.
    assert "instruction" not in off_catalog.requests[0]
    assert "instruction" not in none_given.requests[0]


@pytest.mark.asyncio
async def test_an_oversized_request_is_retried_with_what_surely_fits() -> None:
    """DashScope refuses a request over the model's total (HTTP 400, no
    truncation). The retry keeps the leading documents whose UTF-8 bytes — never
    fewer than tokens — fit, charging the query and instruction to every one, and
    reports how many it left out."""
    total = _caps("qwen3-vl-rerank").max_total_tokens
    assert total == 120_000
    query, instruction, doc = "查询", "judge " * 40, "文" * 1000
    per_document = len((query + instruction + doc).encode())
    fits = total // per_document
    assert fits == 36  # 39 if the instruction were not charged, 37 without the query
    client = _RecordingClient(refuse_over=fits)
    ranked = await DashScopeReranker(client=client, model="qwen3-vl-rerank").rerank_scored(
        query=query, documents=[doc] * 100, top_k=100, tenant_id=uuid4(), instruction=instruction
    )
    assert [len(r["documents"]) for r in client.requests] == [100, fits]
    assert [r["top_n"] for r in client.requests] == [100, fits]
    assert client.requests[1]["instruction"] == instruction
    assert [index for index, _score in ranked] == list(range(fits))
    assert isinstance(ranked, RerankRanking)
    assert ranked.dropped == 100 - fits


_BIG = ["文" * 1000] * 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "documents"),
    [
        # Not about size, on a request big enough to be cut: it must surface
        # (the guard has already retried what is worth retrying), not quietly
        # lose the tail of the pool.
        ("transient", _BIG),
        ("rate_limited", _BIG),
        ("rejected", _BIG),
        # Refused as too long although it already fits: it would be refused again.
        ("too_large", ["d"] * 5),
    ],
)
async def test_other_refusals_are_not_retried(failure: str, documents: list[str]) -> None:
    client = _RecordingClient(refuse_over=0, failure=failure)
    with pytest.raises(RerankCallError):
        await DashScopeReranker(client=client, model="qwen3-vl-rerank").rerank_scored(
            query="q", documents=documents, top_k=5, tenant_id=uuid4()
        )
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_http_client_sends_the_instruction_as_parameters_instruct() -> None:
    bodies: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        import json

        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"output": {"results": []}})

    client = HTTPDashScopeRerankClient(api_key="k", transport=httpx.MockTransport(_handler))
    await client.rerank(model="m", query="q", documents=["a"], top_n=1, instruction="judge it")
    await client.rerank(model="m", query="q", documents=["a"], top_n=1)
    assert bodies[0]["parameters"] == {
        "return_documents": False,
        "top_n": 1,
        "instruct": "judge it",
    }
    assert bodies[1]["parameters"] == {"return_documents": False, "top_n": 1}
