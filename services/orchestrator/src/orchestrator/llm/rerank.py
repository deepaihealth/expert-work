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

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import httpx

from expert_work.protocol.model_catalog import RerankCaps, catalog_entry
from orchestrator.llm.providers._http import client_for
from orchestrator.tools.knowledge import RerankRanking

logger = logging.getLogger(__name__)

#: DashScope native (NOT compatible-mode) text-rerank endpoint.
DASHSCOPE_RERANK_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
)
#: DashScope models are catalogued under this provider.
_CATALOG_PROVIDER = "qwen"
_DEFAULT_TIMEOUT_S = 30.0
_ERROR_BODY_LIMIT = 500
#: How DashScope words a refusal for length (B-206 K-14). The only wording on
#: record is the embedding endpoint's (B-200: HTTP 400, code ``InvalidParameter``,
#: "Range of input length should be [1, 16000]"); the rerank endpoint's is not,
#: so this matches conservatively — a parameter error that talks about length,
#: tokens or a limit being exceeded. Anything else is raised as it was (the
#: search falls back to the fused order) rather than answered with a shorter pool.
_SIZE_REFUSAL_CODE = "InvalidParameter"
_SIZE_REFUSAL_WORDING = re.compile(r"length|too long|exceed|token", re.IGNORECASE)


@runtime_checkable
class RerankClient(Protocol):
    """The one rerank endpoint we use — sized so tests fake it without httpx.

    ``instruction`` (B-206 K-14) is passed only when there is one, so a client
    written before it existed keeps working."""

    async def rerank(
        self,
        *,
        model: str,
        query: str,
        documents: Sequence[str],
        top_n: int,
        instruction: str | None = None,
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
        self,
        *,
        model: str,
        query: str,
        documents: Sequence[str],
        top_n: int,
        instruction: str | None = None,
    ) -> Mapping[str, Any]:
        # We only need the reordered indices, not the echoed documents.
        parameters: dict[str, Any] = {"return_documents": False, "top_n": top_n}
        if instruction is not None:
            # B-206 K-14 —— 2026-10-22 真接口实测(qwen3-vl-rerank / qwen3-rerank):原生口的
            # 任务说明放 ``parameters.instruct`` 分数会变,放请求体顶层被忽略(分数逐位不变)。
            parameters["instruct"] = instruction
        body = {
            "model": model,
            "input": {
                "query": {"text": query},
                "documents": [{"text": d} for d in documents],
            },
            "parameters": parameters,
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
    """:class:`orchestrator.tools.knowledge.Reranker` (and ``ScoredReranker``)
    over DashScope's native rerank API. Returns candidate indices in best-first
    order, truncated to ``top_k``; a malformed body degrades to the input order
    without scores (rerank is an optional quality pass — never break search).

    B-206 K-14 — what the model catalog says about ``model``
    (:class:`~expert_work.protocol.model_catalog.RerankCaps`) shapes the request:
    only the leading ``max_documents`` are sent (callers hand them best first),
    ``top_n`` never asks for more than were sent, an ``instruction`` goes only to
    a model that takes one, and a request the vendor refuses for length (HTTP 400,
    see :data:`_SIZE_REFUSAL_WORDING`) when ``max_total_tokens`` is declared is
    sent again with the leading documents that surely fit (:func:`_fit_total`).
    Documents left out either way are reported, not silent: the ranking is a
    :class:`~orchestrator.tools.knowledge.RerankRanking` whose ``dropped`` counts
    them. Off-catalog models get none of this."""

    client: RerankClient
    model: str

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        ranked = await self.rerank_scored(
            query=query, documents=documents, top_k=top_k, tenant_id=tenant_id
        )
        return [index for index, _score in ranked]

    async def rerank_scored(
        self,
        *,
        query: str,
        documents: Sequence[str],
        top_k: int,
        tenant_id: UUID,
        instruction: str | None = None,
    ) -> list[tuple[int, float | None]]:
        del tenant_id  # credential is baked into ``client``
        entry = catalog_entry(_CATALOG_PROVIDER, self.model)
        caps = entry.rerank_caps if entry is not None else None
        items = list(documents)
        if caps is not None and caps.max_documents is not None:
            items = items[: caps.max_documents]
        if not items:
            return []
        extra: dict[str, Any] = {}
        if instruction is not None and caps is not None and caps.supports_instruction:
            extra["instruction"] = instruction
        try:
            body = await self.client.rerank(
                model=self.model,
                query=query,
                documents=items,
                top_n=min(top_k, len(items)),
                **extra,
            )
        except httpx.HTTPStatusError as exc:
            fitted = _fit_total(query, items, extra.get("instruction"), caps)
            if not _is_size_refusal(exc) or not fitted or len(fitted) == len(items):
                raise
            logger.warning(
                "dashscope rerank refused %d documents for length; retrying with the %d that "
                "surely fit model=%s",
                len(items),
                len(fitted),
                self.model,
            )
            items = fitted
            body = await self.client.rerank(
                model=self.model,
                query=query,
                documents=items,
                top_n=min(top_k, len(items)),
                **extra,
            )
        dropped = len(documents) - len(items)
        output = body.get("output")
        results = output.get("results") if isinstance(output, Mapping) else None
        if not isinstance(results, list):
            unscored = ((index, None) for index in range(min(len(items), top_k)))
            return RerankRanking(unscored, dropped=dropped)
        ranked = [
            (row["index"], _relevance(row.get("relevance_score")))
            for row in results
            if isinstance(row, Mapping)
            and isinstance(row.get("index"), int)
            and not isinstance(row.get("index"), bool)
            and 0 <= row["index"] < len(items)
        ]
        return RerankRanking(ranked[:top_k], dropped=dropped)


def _is_size_refusal(exc: httpx.HTTPStatusError) -> bool:
    """Whether DashScope refused the request for its length (see
    :data:`_SIZE_REFUSAL_WORDING`): HTTP 400 with a JSON body whose ``code`` is
    ``InvalidParameter`` and whose ``message`` talks about length."""
    if exc.response.status_code != 400:
        return False
    try:
        body = exc.response.json()
    except ValueError:
        return False
    if not isinstance(body, Mapping) or body.get("code") != _SIZE_REFUSAL_CODE:
        return False
    message = body.get("message")
    return isinstance(message, str) and _SIZE_REFUSAL_WORDING.search(message) is not None


def _fit_total(
    query: str, documents: Sequence[str], instruction: str | None, caps: RerankCaps | None
) -> list[str]:
    """The leading ``documents`` a request surely stays within
    ``caps.max_total_tokens`` with — all of them when no total is declared.

    The platform cannot count the vendor's tokens, so it counts what bounds them:
    UTF-8 bytes (a token is never shorter than one byte; see
    :data:`~expert_work.protocol.model_catalog.LengthUnit`), or characters for a
    ``"char"`` model. The query and instruction are charged to every document in
    case the vendor counts each query-document pair. That bound is several times
    the real count for prose, so it is used only after the vendor has refused."""
    if caps is None or caps.max_total_tokens is None:
        return list(documents)
    by_char = caps.length_unit == "char"

    def size(text: str) -> int:
        return len(text) if by_char else len(text.encode("utf-8"))

    per_document = size(query) + (size(instruction) if instruction is not None else 0)
    room = caps.max_total_tokens
    fitted: list[str] = []
    for document in documents:
        room -= per_document + size(document)
        if room < 0:
            break
        fitted.append(document)
    return fitted


def _relevance(value: object) -> float | None:
    """A result row's ``relevance_score``; ``None`` when missing or not a number."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None
