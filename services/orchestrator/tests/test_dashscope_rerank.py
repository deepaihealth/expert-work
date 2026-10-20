"""Unit tests for the DashScope native rerank client + reranker."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

import httpx
import pytest

from orchestrator.llm import DashScopeReranker, HTTPDashScopeRerankClient
from orchestrator.llm.rerank import _MAX_DOC_CHARS


@pytest.mark.asyncio
async def test_reranker_returns_best_first_indices_truncated() -> None:
    class _Client:
        async def rerank(
            self, *, model: str, query: str, documents: Sequence[str], top_n: int
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
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await client.rerank(model="bad", query="q", documents=["a"], top_n=1)
    assert "Model not exist" in str(excinfo.value)


@pytest.mark.asyncio
async def test_reranker_cuts_each_document_under_the_api_limit() -> None:
    # One document over the model's per-document limit makes DashScope refuse
    # the whole request (HTTP 400), costing the search its rerank (B-198).
    sent: list[str] = []

    class _Client:
        async def rerank(
            self, *, model: str, query: str, documents: Sequence[str], top_n: int
        ) -> Mapping[str, Any]:
            sent.extend(documents)
            return {"output": {"results": [{"index": 1}, {"index": 0}]}}

    long = "x" * (_MAX_DOC_CHARS + 500)
    out = await DashScopeReranker(client=_Client(), model="m").rerank(
        query="q", documents=["short", long], top_k=2, tenant_id=uuid4()
    )
    assert sent == ["short", long[:_MAX_DOC_CHARS]]
    assert out == [1, 0]
