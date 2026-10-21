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
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

import httpx

from expert_work.protocol.model_catalog import EmbeddingCaps
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

#: Longest input sent to the embeddings API, in UTF-8 bytes (B-200). An input
#: over the model's token limit makes the vendor refuse the whole request
#: (text-embedding-v4 answers 400 past 16,000 tokens). Every token covers at
#: least one byte, so a byte cap bounds the vendor's count whatever its
#: tokenizer and the script; a count from another tokenizer does not (8,000
#: o200k tokens of Hindi are over 20,000 text-embedding-v4 tokens). 8,000
#: bytes fits every model that takes 8k tokens (OpenAI 8,191;
#: text-embedding-v4 documents 8,192). Chunks sit well inside it; this is the
#: last guard — for a long query, or a chunk size set past the model's limit.
DEFAULT_MAX_INPUT_BYTES = 8000


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

    async def embeddings(
        self, *, model: str, texts: Sequence[str], fields: Mapping[str, str] | None = None
    ) -> Mapping[str, Any]:
        """POST the embeddings endpoint; return the parsed JSON body.

        ``fields`` are extra top-level request-body fields (B-206 K-5 markers).
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

    async def embeddings(
        self, *, model: str, texts: Sequence[str], fields: Mapping[str, str] | None = None
    ) -> Mapping[str, Any]:
        async with client_for(
            self.http, timeout=_DEFAULT_TIMEOUT_S, transport=self.transport
        ) as client:
            response = await client.post(
                f"{self.base_url}{self.embeddings_path}",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={**(fields or {}), "model": model, "input": list(texts)},
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


def fit_embedding_input(text: str, max_bytes: int) -> str:
    """``text`` cut to its longest prefix within ``max_bytes`` UTF-8 bytes,
    never inside a character; unchanged when it fits."""
    raw = text.encode()
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode(errors="ignore")


#: B-206 K-5 —— which side of knowledge retrieval a text is embedded for.
EmbeddingSide = Literal["query", "document"]


@dataclass(frozen=True)
class EmbeddingMarker:
    """B-206 K-5 —— what one side adds to an embeddings request so the vendor
    can tell a search query from a document being indexed (see
    :func:`embedding_marker`). The default adds nothing."""

    #: Extra top-level request-body fields ("param" enum, "query_field" instruction).
    fields: tuple[tuple[str, str], ...] = ()
    #: Text put around every input ("prefix" templates).
    prefix: str = ""
    suffix: str = ""

    def wrap(self, text: str) -> str:
        return f"{self.prefix}{text}{self.suffix}"

    @property
    def fingerprint(self) -> str:
        """Everything this marker changes about a request, as one string — ``""``
        when it changes nothing. Changing it changes the vectors."""
        parts = [f"{name}={value}" for name, value in self.fields]
        if self.prefix or self.suffix:
            parts.append(f"{self.prefix}{{text}}{self.suffix}")
        return "&".join(parts)


NO_MARKER = EmbeddingMarker()


def embedding_marker(caps: EmbeddingCaps | None, side: EmbeddingSide) -> EmbeddingMarker:
    """B-206 K-5 —— the marker the model catalog declares for ``side``. A model
    with no declared caps (off-catalog, custom gateway) or ``asymmetry="none"``
    gets none: its requests stay exactly what they were."""
    if caps is None or caps.asymmetry == "none":
        return NO_MARKER
    marker = caps.query_marker if side == "query" else caps.document_marker
    if marker is None:
        return NO_MARKER  # only a "prefix" document side may be undeclared
    if caps.asymmetry == "param":
        name, _, value = marker.partition("=")
        return EmbeddingMarker(fields=((name, value),))
    if caps.asymmetry == "query_field":
        # The validator guarantees an instruction; ``or ""`` only narrows the type.
        return EmbeddingMarker(fields=((marker, caps.instruction or ""),))
    if caps.instruction is not None:
        marker = marker.replace("{task}", caps.instruction)
    # Split on the placeholder rather than ``str.format``: the text itself may hold braces.
    prefix, _, suffix = marker.partition("{query}" if side == "query" else "{document}")
    return EmbeddingMarker(prefix=prefix, suffix=suffix)


def marked_model_key(model_key: str, marker: EmbeddingMarker) -> str:
    """The key a vector made with ``marker`` is stored / cached under:
    ``model_key`` itself when the marker changes nothing (so vectors made
    before markers existed stay reusable), else ``model_key#<marker>`` — a
    vector made under another marker is never taken for this one."""
    return f"{model_key}#{marker.fingerprint}" if marker.fingerprint else model_key


def catalog_request_limits(caps: EmbeddingCaps | None, max_input_bytes: int) -> tuple[int, int]:
    """B-206 K-8 —— ``(inputs per request, UTF-8 bytes per input)`` for a model
    with catalog ``caps``: the vendor's documented limits where the catalog
    declares them, the platform defaults where not. The per-input limit is
    applied as the same number of bytes (a token or a character is at least one
    byte, see ``LengthUnit``) and never above ``max_input_bytes``, the
    deployment's cap."""
    if caps is None:
        return _DEFAULT_MAX_BATCH_SIZE, max_input_bytes
    batch = caps.max_batch or _DEFAULT_MAX_BATCH_SIZE
    if caps.max_input_tokens is None:
        return batch, max_input_bytes
    return batch, min(max_input_bytes, caps.max_input_tokens)


@dataclass(frozen=True)
class OpenAICompatibleEmbedder:
    """:class:`Embedder` over an OpenAI-compatible ``/v1/embeddings`` API."""

    client: EmbeddingClient
    model: str
    #: Inputs per request — split larger sets so qwen's 10-input cap (and any
    #: other vendor batch limit) is never exceeded. See _DEFAULT_MAX_BATCH_SIZE.
    max_batch_size: int = _DEFAULT_MAX_BATCH_SIZE
    #: Each input is cut to this many UTF-8 bytes before it is sent; ``None``
    #: sends inputs whole. See DEFAULT_MAX_INPUT_BYTES.
    max_input_bytes: int | None = DEFAULT_MAX_INPUT_BYTES
    #: B-206 K-5 —— the query or document marker every request carries. Only the
    #: knowledge-base paths set one; the default (long-term memory, …) adds nothing.
    marker: EmbeddingMarker = NO_MARKER

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        # Fixed-key embedder — the credential is baked into ``client``;
        # ``tenant_id`` is accepted for protocol conformance and ignored.
        del tenant_id
        cap = self.max_input_bytes
        wrapped = [self.marker.wrap(text) for text in texts]
        items = [fit_embedding_input(text, cap) for text in wrapped] if cap else wrapped
        if not items:
            return []
        fields = dict(self.marker.fields)
        vectors: list[tuple[float, ...]] = []
        for start in range(0, len(items), self.max_batch_size):
            batch = items[start : start + self.max_batch_size]
            body = await self.client.embeddings(model=self.model, texts=batch, fields=fields)
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
