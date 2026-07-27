"""DashScope native rerank — for dedicated rerank models.

expert_work's default reranker (:class:`orchestrator.tools.knowledge.LLMReranker`)
prompts a **chat** model to rank candidates. DashScope's dedicated rerank
models (``qwen3-vl-rerank``, ``gte-rerank``, ``text-rerank-*``) are NOT served
over the OpenAI-compatible chat endpoint — calling them there returns
``404 Unsupported model ... for OpenAI compatibility mode``. This module calls
DashScope's **native** rerank API instead so a real rerank model can be used.

Endpoint: ``POST /api/v1/services/rerank/text-rerank/text-rerank``
Response shape: ``{"output": {"results": [{"index": int, "relevance_score": float}, ...]}}``
(``results`` are ordered best-first).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import httpx

from orchestrator.llm.providers._http import client_for

#: DashScope native (NOT compatible-mode) text-rerank endpoint.
DASHSCOPE_RERANK_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
)
_DEFAULT_TIMEOUT_S = 30.0
_ERROR_BODY_LIMIT = 500


@runtime_checkable
class RerankClient(Protocol):
    """The one rerank endpoint we use — sized so tests fake it without httpx."""

    async def rerank(
        self, *, model: str, query: str, documents: Sequence[str], top_n: int
    ) -> Mapping[str, Any]:
        """POST the rerank endpoint; return the parsed JSON body."""


@dataclass(frozen=True)
class HTTPDashScopeRerankClient:
    """httpx-backed :class:`RerankClient` for the DashScope native rerank API."""

    api_key: str
    base_url: str = DASHSCOPE_RERANK_URL
    timeout_s: float = _DEFAULT_TIMEOUT_S
    transport: httpx.AsyncBaseTransport | None = None
    #: 一期 Task 5 — process-level shared client. ``None`` falls back to the
    #: original per-call ``async with httpx.AsyncClient(...)`` behaviour
    #: (tests / eval CLI / not-yet-wired production paths). When injected it
    #: must NOT be closed here: it is owned by the control-plane lifespan and
    #: outlives every individual call.
    http: httpx.AsyncClient | None = None

    async def rerank(
        self, *, model: str, query: str, documents: Sequence[str], top_n: int
    ) -> Mapping[str, Any]:
        body = {
            "model": model,
            "input": {
                "query": {"text": query},
                "documents": [{"text": d} for d in documents],
            },
            # We only need the reordered indices, not the echoed documents.
            "parameters": {"return_documents": False, "top_n": top_n},
        }
        async with client_for(
            self.http, timeout=self.timeout_s, transport=self.transport
        ) as client:
            response = await client.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "content-type": "application/json",
                },
                json=body,
                timeout=self.timeout_s,  # per-request — governs even when sharing a client
            )
            if response.is_error:
                detail = response.text[:_ERROR_BODY_LIMIT]
                raise httpx.HTTPStatusError(
                    f"dashscope rerank failed: {response.status_code} {detail}",
                    request=response.request,
                    response=response,
                )
            data: Any = response.json()
        if not isinstance(data, Mapping):
            msg = f"dashscope rerank returned non-object body: {type(data).__name__}"
            raise httpx.HTTPError(msg)
        return data


@dataclass(frozen=True)
class DashScopeReranker:
    """:class:`orchestrator.tools.knowledge.Reranker` over DashScope's native
    rerank API. Returns candidate indices in best-first order, truncated to
    ``top_k``; a malformed body degrades to the input order (rerank is an
    optional quality pass — never break search)."""

    client: RerankClient
    model: str

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        del tenant_id  # credential is baked into ``client``
        items = list(documents)
        if not items:
            return []
        body = await self.client.rerank(model=self.model, query=query, documents=items, top_n=top_k)
        output = body.get("output")
        results = output.get("results") if isinstance(output, Mapping) else None
        if not isinstance(results, list):
            return list(range(len(items)))[:top_k]
        order = [
            row["index"]
            for row in results
            if isinstance(row, Mapping)
            and isinstance(row.get("index"), int)
            and not isinstance(row.get("index"), bool)
            and 0 <= row["index"] < len(items)
        ]
        return order[:top_k]
