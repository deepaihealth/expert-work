"""Unit tests for the embedding client — Stream J.3."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx
import pytest

from orchestrator.llm import (
    DEFAULT_MAX_INPUT_BYTES,
    FakeEmbedder,
    HTTPEmbeddingClient,
    OpenAICompatibleEmbedder,
)

# ---------------------------------------------------------------------------
# FakeEmbedder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_embedder_is_deterministic_and_correct_width() -> None:
    embedder = FakeEmbedder(dim=64)
    first = await embedder.embed(["hello", "world"], tenant_id=uuid4())
    again = await embedder.embed(["hello", "world"], tenant_id=uuid4())

    assert [len(v) for v in first] == [64, 64]
    # Same text → same vector; different text → different vector.
    assert first == again
    assert first[0] != first[1]


@pytest.mark.asyncio
async def test_fake_embedder_empty_input() -> None:
    assert await FakeEmbedder(dim=8).embed([], tenant_id=uuid4()) == []


# ---------------------------------------------------------------------------
# OpenAICompatibleEmbedder
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedEmbeddingClient:
    """EmbeddingClient stub returning a fixed body, recording the call."""

    body: Mapping[str, Any]
    calls: list[tuple[str, list[str]]] = field(default_factory=list)

    async def embeddings(self, *, model: str, texts: Sequence[str]) -> Mapping[str, Any]:
        self.calls.append((model, list(texts)))
        return self.body


@pytest.mark.asyncio
async def test_openai_compatible_embedder_extracts_vectors() -> None:
    client = _ScriptedEmbeddingClient(
        body={
            "data": [
                {"index": 0, "embedding": [0.1, 0.2]},
                {"index": 1, "embedding": [0.3, 0.4]},
            ]
        }
    )
    embedder = OpenAICompatibleEmbedder(client=client, model="text-embedding-v4")
    vectors = await embedder.embed(["a", "b"], tenant_id=uuid4())

    assert vectors == [(0.1, 0.2), (0.3, 0.4)]
    assert client.calls == [("text-embedding-v4", ["a", "b"])]


@pytest.mark.asyncio
async def test_openai_compatible_embedder_reorders_by_index() -> None:
    """The API may return rows out of order — ``index`` puts them back."""
    client = _ScriptedEmbeddingClient(
        body={
            "data": [
                {"index": 1, "embedding": [0.3, 0.4]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ]
        }
    )
    embedder = OpenAICompatibleEmbedder(client=client, model="m")
    assert await embedder.embed(["a", "b"], tenant_id=uuid4()) == [(0.1, 0.2), (0.3, 0.4)]


@pytest.mark.asyncio
async def test_openai_compatible_embedder_empty_input_skips_call() -> None:
    client = _ScriptedEmbeddingClient(body={"data": []})
    embedder = OpenAICompatibleEmbedder(client=client, model="m")
    assert await embedder.embed([], tenant_id=uuid4()) == []
    assert client.calls == []


@dataclass
class _EchoEmbeddingClient:
    """EmbeddingClient stub: one vector per input, index-ordered per call.

    Each vector encodes the input text length so order can be verified
    across batch boundaries.
    """

    calls: list[list[str]] = field(default_factory=list)

    async def embeddings(self, *, model: str, texts: Sequence[str]) -> Mapping[str, Any]:
        del model
        self.calls.append(list(texts))
        return {
            "data": [{"index": i, "embedding": [float(len(text))]} for i, text in enumerate(texts)]
        }


@pytest.mark.asyncio
async def test_openai_compatible_embedder_splits_oversized_batch() -> None:
    """DashScope text-embedding-v4 caps a request at 10 inputs — a larger
    set must be split into sub-batches, vectors concatenated in input order."""
    client = _EchoEmbeddingClient()
    embedder = OpenAICompatibleEmbedder(client=client, model="text-embedding-v4")
    texts = [str(n) * n for n in range(1, 26)]  # 25 distinct-length texts

    vectors = await embedder.embed(texts, tenant_id=uuid4())

    # 25 inputs at batch size 10 → 10 + 10 + 5.
    assert [len(batch) for batch in client.calls] == [10, 10, 5]
    # Vectors stay aligned to inputs across batch boundaries.
    assert vectors == [(float(len(text)),) for text in texts]


@pytest.mark.asyncio
async def test_openai_compatible_embedder_custom_batch_size() -> None:
    client = _EchoEmbeddingClient()
    embedder = OpenAICompatibleEmbedder(client=client, model="m", max_batch_size=2)
    await embedder.embed(["a", "b", "c"], tenant_id=uuid4())
    assert [len(batch) for batch in client.calls] == [2, 1]


# ---------------------------------------------------------------------------
# HTTPEmbeddingClient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_embedding_client_posts_and_parses() -> None:
    seen: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    client = HTTPEmbeddingClient(api_key="sk-test", transport=httpx.MockTransport(_handler))
    body = await client.embeddings(model="text-embedding-v4", texts=["hi"])

    assert body["data"][0]["embedding"] == [1.0, 2.0]
    assert seen["url"].endswith("/v1/embeddings")
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"] == {"model": "text-embedding-v4", "input": ["hi"]}


@pytest.mark.asyncio
async def test_http_embedding_client_raises_on_http_error() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    client = HTTPEmbeddingClient(api_key="bad", transport=httpx.MockTransport(_handler))
    with pytest.raises(httpx.HTTPStatusError):
        await client.embeddings(model="m", texts=["x"])


@pytest.mark.asyncio
async def test_http_embedding_client_error_surfaces_response_body() -> None:
    """A vendor 400 carries the real reason in its body — surface it instead
    of httpx's opaque "Client error '400 Bad Request'" message."""

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": "batch size is invalid, expecting: range[1, 10]"}}
        )

    client = HTTPEmbeddingClient(api_key="k", transport=httpx.MockTransport(_handler))
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await client.embeddings(model="text-embedding-v4", texts=["x"])

    assert "batch size is invalid" in str(excinfo.value)


# An input over the model's limit makes the embeddings API refuse the whole
# request; the embedder cuts each input to ``max_input_bytes`` first (B-200).
@pytest.mark.asyncio
async def test_openai_compatible_embedder_cuts_inputs_over_the_byte_cap() -> None:
    client = _EchoEmbeddingClient()
    embedder = OpenAICompatibleEmbedder(client=client, model="m", max_input_bytes=50)
    long = "退款期限十四天" * 20  # 3 bytes a character
    await embedder.embed(["short text", long], tenant_id=uuid4())
    (sent,) = client.calls
    assert sent[0] == "short text"
    assert sent[1] == long[:16]  # 48 bytes: the 17th character would end at 51


@pytest.mark.asyncio
async def test_openai_compatible_embedder_caps_inputs_by_default() -> None:
    # 8,000 o200k tokens of Hindi are over 20,000 text-embedding-v4 tokens;
    # bytes bound every tokenizer, so the default cap holds for any script.
    client = _EchoEmbeddingClient()
    long = "धनवापसी की अवधि चौदह दिन है। " * 400
    await OpenAICompatibleEmbedder(client=client, model="m").embed([long], tenant_id=uuid4())
    ((sent,),) = client.calls
    assert long.startswith(sent)
    assert DEFAULT_MAX_INPUT_BYTES - 4 < len(sent.encode()) <= DEFAULT_MAX_INPUT_BYTES
