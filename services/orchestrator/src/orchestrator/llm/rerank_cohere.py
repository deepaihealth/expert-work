"""Cohere-style rerank — one client for every vendor whose rerank endpoint takes
a query, a list of documents and a result count (B-206 K-7c).

Cohere (``/v2/rerank``), Jina, Voyage, Zhipu, SiliconFlow, Baidu Qianfan and the
self-hosted servers (vLLM, TEI, Xinference) all serve this shape (design §5.5
table 5-B). They differ only in names — ``top_n`` or ``top_k``, ``results`` or
``data``, ``relevance_score`` or ``score`` — which one row of
:data:`COHERE_STYLE_SHAPES` per family records. The client sends one request,
says what a failed one means (:class:`~orchestrator.llm.rerank_guard.RerankCallError`,
so :class:`~orchestrator.llm.rerank_guard.RerankGuards` paces and retries it as
it does any vendor's) and rewrites the answer to the one shape
:class:`~orchestrator.llm.rerank_batches.CohereStyleReranker` reads:
``{"results": [{"index": ..., "relevance_score": ...}, ...]}`` — empty when the
answer holds no result list.

The platform reaches a family through a provider it already holds a credential
and a host for (:data:`RERANK_SHAPE_BY_PROVIDER`): today Zhipu, through ``glm``.
The other rows wait for a provider: Cohere, Jina, Voyage, SiliconFlow and
Qianfan are not platform providers yet, and a self-hosted server's base URL is
not part of the platform's rerank configuration.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import httpx

from orchestrator.llm.providers._http import client_for
from orchestrator.llm.providers.openai_compatible import GLM_ACCOUNT_429_CODES, GLM_BASE_URL
from orchestrator.llm.rerank_guard import (
    RerankCallError,
    RerankFailure,
    retry_after_seconds,
    status_failure,
)

_DEFAULT_TIMEOUT_S = 30.0
_ERROR_BODY_LIMIT = 500
#: How vendors word a refusal for the request's length, in English or Chinese —
#: narrow on purpose: a 400 / 422 worded otherwise is ``rejected`` (the search
#: falls back to the fused order) rather than answered with a shorter pool. The
#: one on record is Zhipu's (2026-10-22: HTTP 400, code ``1214``,
#: "query+document超长", for query + documents over 128,000 characters).
_SIZE_REFUSAL_WORDING = re.compile(
    r"too long|too large|too many tokens|exceeds? (?:the )?max|"
    r"maximum (?:input |context )?length|超长|过长",
    re.IGNORECASE,
)
_SIZE_REFUSAL_STATUSES = frozenset({400, 422})
#: An HTTP 422 is a validation error, whose body may quote the request back —
#: the documents' text: it is left out of error messages (they reach logs).
_ECHOING_STATUS = 422


@dataclass(frozen=True)
class CohereStyleShape:
    """How one vendor family spells a Cohere-style rerank request and answer."""

    #: The endpoint is ``host`` + ``path``; ``host=None`` = a self-hosted server,
    #: whose base URL its deployment supplies.
    path: str
    host: str | None = None
    documents_field: str = "documents"
    #: The result-count field; ``None`` = the server scores every document (TEI).
    top_field: str | None = "top_n"
    #: The field turning the echoed documents off (sent as ``false``); ``None`` =
    #: not sent (the family never echoes them, or does not document the field).
    return_documents_field: str | None = "return_documents"
    #: ``False`` = the server serves one model and takes no ``model`` field (TEI).
    sends_model: bool = True
    #: Where a task instruction goes, for a model the catalog says takes one;
    #: ``None`` = the family has no such field and the instruction is not sent.
    instruction_field: str | None = None
    #: The result list's key; ``None`` = the answer is the list itself (TEI).
    results_field: str | None = "results"
    score_field: str = "relevance_score"
    #: Fields every request carries as they are.
    fixed: tuple[tuple[str, Any], ...] = ()
    #: Error codes that make an HTTP 429 the account's state — no balance, the
    #: day's quota used up — rather than load: ``rejected``, not retried.
    account_codes: frozenset[str] = frozenset()
    #: Error codes under which a 400 / 422 worded as a size refusal is one; empty
    #: = the wording alone decides (families that send no code).
    size_codes: frozenset[str] = frozenset()


#: One row per vendor family (B-206 design §5.5 table 5-B; vendor docs read 2026-10).
COHERE_STYLE_SHAPES: Mapping[str, CohereStyleShape] = MappingProxyType(
    {
        # v2 answers index + score only: it has no ``return_documents``.
        "cohere": CohereStyleShape(
            host="https://api.cohere.com", path="/v2/rerank", return_documents_field=None
        ),
        "jina": CohereStyleShape(host="https://api.jina.ai", path="/v1/rerank"),
        "voyage": CohereStyleShape(
            host="https://api.voyageai.com",
            path="/v1/rerank",
            top_field="top_k",
            results_field="data",
        ),
        # 2026-10-22 live: ``relevance_score`` is the sigmoid of the model's logit,
        # rounded to float32 — every strongly relevant passage reads 1.0 (logits 22
        # and 25 alike). ``return_raw_scores`` returns the logit itself, which the
        # catalog declares (``score_scale="logit"``) so the platform takes the
        # sigmoid in double precision and keeps those passages apart. Size codes:
        # 1214 「${field} 参数非法」(live: "query+document超长"), 1261「Prompt 超长」;
        # 1210 is any parameter error (live: 129 documents) — not a size refusal.
        "zhipu": CohereStyleShape(
            host=GLM_BASE_URL,
            path="/api/paas/v4/rerank",
            fixed=(("return_raw_scores", True),),
            account_codes=GLM_ACCOUNT_429_CODES,
            size_codes=frozenset({"1214", "1261"}),
        ),
        "siliconflow": CohereStyleShape(host="https://api.siliconflow.cn", path="/v1/rerank"),
        "qianfan": CohereStyleShape(
            host="https://qianfan.baidubce.com", path="/v2/rerank", return_documents_field=None
        ),
        "vllm": CohereStyleShape(path="/rerank", return_documents_field=None),
        "xinference": CohereStyleShape(path="/v1/rerank"),
        "tei": CohereStyleShape(
            path="/rerank",
            documents_field="texts",
            top_field=None,
            return_documents_field="return_text",
            sends_model=False,
            results_field=None,
            score_field="score",
        ),
    }
)

#: The platform providers (:data:`~expert_work.protocol.provider_catalog.Provider`)
#: whose rerank endpoint is Cohere-style → their family in :data:`COHERE_STYLE_SHAPES`.
RERANK_SHAPE_BY_PROVIDER: Mapping[str, str] = MappingProxyType({"glm": "zhipu"})


@dataclass(frozen=True)
class HTTPCohereStyleRerankClient:
    """httpx-backed :class:`~orchestrator.llm.rerank.RerankClient` for one
    Cohere-style endpoint (``url``) spelled as ``shape``. ``http`` is the
    process-level shared client, never closed here (see
    :func:`~orchestrator.llm.providers._http.client_for`)."""

    api_key: str = field(repr=False)
    url: str
    shape: CohereStyleShape
    timeout_s: float = _DEFAULT_TIMEOUT_S
    transport: httpx.AsyncBaseTransport | None = None
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
        body = _request_body(
            self.shape,
            model=model,
            query=query,
            documents=documents,
            top_n=top_n,
            instruction=instruction,
        )
        async with client_for(
            self.http, timeout=self.timeout_s, transport=self.transport
        ) as client:
            try:
                response = await client.post(
                    self.url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "content-type": "application/json",
                    },
                    json=body,
                    timeout=self.timeout_s,  # per-request — governs even when sharing a client
                )
            except httpx.TransportError as exc:  # timeouts, refused / reset connections
                msg = f"rerank unreachable: {type(exc).__name__}"
                raise RerankCallError(msg, failure="transient") from exc
            if response.is_error:
                echoes = response.status_code == _ECHOING_STATUS
                body_text = "(body withheld)" if echoes else response.text[:_ERROR_BODY_LIMIT]
                raise RerankCallError(
                    f"rerank failed: {response.status_code} {body_text}",
                    failure=_failure(response, self.shape),
                    status=response.status_code,
                    retry_after_s=retry_after_seconds(response.headers),
                )
            try:
                data: Any = response.json()
            except ValueError:
                data = None
        return _results(data, self.shape)


def cohere_style_client(
    provider: str,
    *,
    api_key: str,
    http: httpx.AsyncClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HTTPCohereStyleRerankClient:
    """The client for platform provider ``provider``'s Cohere-style rerank
    endpoint; :class:`ValueError` when the platform knows none for it (see
    :data:`RERANK_SHAPE_BY_PROVIDER`)."""
    family = RERANK_SHAPE_BY_PROVIDER.get(provider)
    shape = COHERE_STYLE_SHAPES[family] if family is not None else None
    if shape is None or shape.host is None:
        msg = f"no Cohere-style rerank endpoint is known for provider {provider!r}"
        raise ValueError(msg)
    return HTTPCohereStyleRerankClient(
        api_key=api_key,
        url=f"{shape.host}{shape.path}",
        shape=shape,
        http=http,
        transport=transport,
    )


def _request_body(
    shape: CohereStyleShape,
    *,
    model: str,
    query: str,
    documents: Sequence[str],
    top_n: int,
    instruction: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"query": query, shape.documents_field: list(documents)}
    if shape.sends_model:
        body["model"] = model
    if shape.top_field is not None:
        body[shape.top_field] = top_n
    if shape.return_documents_field is not None:
        body[shape.return_documents_field] = False
    if instruction is not None and shape.instruction_field is not None:
        body[shape.instruction_field] = instruction
    body.update(shape.fixed)
    return body


def _results(data: Any, shape: CohereStyleShape) -> Mapping[str, Any]:
    """The answer's rows as ``{"index", "relevance_score"}``; ``{}`` when it has
    no result list (the reranker reports that as ``model_error``)."""
    if shape.results_field is None:
        rows = data
    else:
        rows = data.get(shape.results_field) if isinstance(data, Mapping) else None
    if not isinstance(rows, list):
        return {}
    return {
        "results": [
            {"index": row.get("index"), "relevance_score": row.get(shape.score_field)}
            for row in rows
            if isinstance(row, Mapping)
        ]
    }


def _failure(response: httpx.Response, shape: CohereStyleShape) -> RerankFailure:
    """What an error response means: the HTTP status's usual meaning
    (:func:`~orchestrator.llm.rerank_guard.status_failure`), except a refusal
    for size (HTTP 413, or a 400 / 422 worded as one under one of the family's
    size codes) is ``too_large`` and a 429 carrying one of the family's account
    codes is ``rejected``."""
    status = response.status_code
    if status == 413:
        return "too_large"
    code, message = _error_detail(response)
    if status == 429 and code in shape.account_codes:
        return "rejected"
    if (
        status in _SIZE_REFUSAL_STATUSES
        and (not shape.size_codes or code in shape.size_codes)
        and _SIZE_REFUSAL_WORDING.search(message)
    ):
        return "too_large"
    return status_failure(status)


def _error_detail(response: httpx.Response) -> tuple[str | None, str]:
    """An error body's code (``None`` when it has none) and its message — the
    body's own text when it carries no ``message`` string (FastAPI's ``detail``,
    TEI's ``error``, an HTML page). Zhipu and OpenAI-style servers nest both
    under ``error``; Cohere puts ``message`` at the top."""
    try:
        body: Any = response.json()
    except ValueError:
        return None, response.text[:_ERROR_BODY_LIMIT]
    if not isinstance(body, Mapping):
        return None, response.text[:_ERROR_BODY_LIMIT]
    error = body.get("error")
    source: Mapping[str, Any] = error if isinstance(error, Mapping) else body
    code = source.get("code")
    message = source.get("message")
    return (
        str(code) if code is not None else None,
        message if isinstance(message, str) else response.text[:_ERROR_BODY_LIMIT],
    )
