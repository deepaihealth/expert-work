"""Embedding client — Stream J.3 (long-term memory).

Turns text into vectors for long-term memory recall + write-back.
Targets the OpenAI-compatible ``/v1/embeddings`` endpoint, so the
domestic vendors expert_work already speaks (qwen DashScope compatible-mode,
…) work uniformly — same pattern as the chat providers (E.11.5).

The output dimension is whatever the embedding model returns; the
deployment must set ``EXPERT_WORK_EMBEDDING_DIM`` to match it (the
``memory_item.embedding`` column is fixed at that width).

:class:`FakeEmbedder` is a deterministic test double — same text always
maps to the same vector — so memory tests need no network.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import httpx

from orchestrator.llm.providers._http import client_for

#: OpenAI-compatible embeddings path — qwen DashScope compatible-mode,
#: DeepSeek, etc. all accept it.
DEFAULT_EMBEDDINGS_PATH = "/v1/embeddings"

#: qwen / Alibaba DashScope OpenAI-compatible base URL (mirrors the chat
#: provider's ``QWEN_BASE_URL``).
QWEN_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode"

_DEFAULT_TIMEOUT_S = 30.0
_UINT32_MAX = 0xFFFFFFFF

#: Inputs per embeddings request. qwen DashScope compatible-mode caps a
#: request at 10 strings (``text-embedding-v4``); larger sets must be split
#: or the API returns 400 "batch size is invalid, expecting: range[1, 10]".
#: Safe for every OpenAI-compatible vendor (others allow more, never fewer).
_DEFAULT_MAX_BATCH_SIZE = 10

#: Cap the vendor error body folded into the raised message — enough to read
#: the reason, bounded so a stray HTML page can't flood it.
_ERROR_BODY_LIMIT = 500


@runtime_checkable
class Embedder(Protocol):
    """Async callable that embeds text into vectors.

    Stream O (Mini-ADR O-9) — ``tenant_id`` lets a credential-resolving
    embedder pick the per-tenant API key at call time (platform vs tenant
    mode). Implementations without per-tenant keys (test doubles, the
    fixed-key :class:`OpenAICompatibleEmbedder`) accept and ignore it.
    """

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        """Return one embedding vector per input text, in input order."""


@runtime_checkable
class EmbeddingClient(Protocol):
    """The embeddings HTTP surface — sized to the one endpoint we use so
    tests can fake it without mocking httpx."""

    async def embeddings(self, *, model: str, texts: Sequence[str]) -> Mapping[str, Any]:
        """POST the embeddings endpoint; return the parsed JSON body.

        Expected shape: ``{"data": [{"embedding": [...], "index": int}, ...]}``.
        """


@dataclass(frozen=True)
class HTTPEmbeddingClient:
    """httpx-backed :class:`EmbeddingClient` for an OpenAI-compatible API."""

    api_key: str
    base_url: str = QWEN_EMBEDDING_BASE_URL
    embeddings_path: str = DEFAULT_EMBEDDINGS_PATH
    transport: httpx.AsyncBaseTransport | None = None
    #: 一期 Task 5 — process-level shared client. ``None`` falls back to the
    #: original per-call ``async with httpx.AsyncClient(...)`` behaviour
    #: (tests / eval CLI / not-yet-wired production paths). When injected it
    #: must NOT be closed here: it is owned by the control-plane lifespan and
    #: outlives every individual call.
    http: httpx.AsyncClient | None = None

    async def embeddings(self, *, model: str, texts: Sequence[str]) -> Mapping[str, Any]:
        async with client_for(
            self.http, timeout=_DEFAULT_TIMEOUT_S, transport=self.transport
        ) as client:
            response = await client.post(
                f"{self.base_url}{self.embeddings_path}",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": model, "input": list(texts)},
                timeout=_DEFAULT_TIMEOUT_S,  # per-request — governs even when sharing a client
            )
            if response.is_error:
                # httpx's default message is opaque ("Client error '400 Bad
                # Request'"); the vendor puts the real reason in the body
                # (e.g. DashScope "batch size is invalid"). Surface it.
                detail = response.text[:_ERROR_BODY_LIMIT]
                raise httpx.HTTPStatusError(
                    f"embeddings request failed: {response.status_code} {detail}",
                    request=response.request,
                    response=response,
                )
            body: Mapping[str, Any] = response.json()
            return body


@dataclass(frozen=True)
class OpenAICompatibleEmbedder:
    """:class:`Embedder` over an OpenAI-compatible ``/v1/embeddings`` API."""

    client: EmbeddingClient
    model: str
    #: Inputs per request — split larger sets so qwen's 10-input cap (and any
    #: other vendor batch limit) is never exceeded. See _DEFAULT_MAX_BATCH_SIZE.
    max_batch_size: int = _DEFAULT_MAX_BATCH_SIZE

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        # Fixed-key embedder — the credential is baked into ``client``;
        # ``tenant_id`` is accepted for protocol conformance and ignored.
        del tenant_id
        items = list(texts)
        if not items:
            return []
        vectors: list[tuple[float, ...]] = []
        for start in range(0, len(items), self.max_batch_size):
            batch = items[start : start + self.max_batch_size]
            body = await self.client.embeddings(model=self.model, texts=batch)
            # ``index`` is per-request (0-based within this batch); sort then
            # append so vectors stay aligned to inputs across batches.
            rows = sorted(body["data"], key=lambda row: row["index"])
            vectors.extend(tuple(float(value) for value in row["embedding"]) for row in rows)
        return vectors


@dataclass(frozen=True)
class FakeEmbedder:
    """Deterministic test double — hashes text to a fixed-width vector.

    Same text always maps to the same vector; different texts to
    different ones. No semantic meaning — enough for store / node tests.
    """

    dim: int = 1024

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id  # deterministic test double — no per-tenant key
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> tuple[float, ...]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values: list[float] = []
        counter = 0
        while len(values) < self.dim:
            block = hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
            for offset in range(0, len(block), 4):
                values.append(int.from_bytes(block[offset : offset + 4], "big") / _UINT32_MAX)
            counter += 1
        return tuple(values[: self.dim])
