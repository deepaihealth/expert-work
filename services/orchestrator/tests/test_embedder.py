"""Unit tests for the embedding client — Stream J.3."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx
import pytest

from expert_work.protocol import catalog_entry
from expert_work.protocol.model_catalog import EmbeddingCaps
from orchestrator.llm import (
    DEFAULT_MAX_INPUT_BYTES,
    FakeEmbedder,
    HTTPEmbeddingClient,
    OpenAICompatibleEmbedder,
)
from orchestrator.llm.embedder import (
    NO_MARKER,
    EmbeddingMarker,
    EmbeddingSide,
    catalog_batch_size,
    embedding_marker,
    marked_model_key,
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

    async def embeddings(
        self, *, model: str, texts: Sequence[str], fields: Mapping[str, str] | None = None
    ) -> Mapping[str, Any]:
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

    async def embeddings(
        self, *, model: str, texts: Sequence[str], fields: Mapping[str, str] | None = None
    ) -> Mapping[str, Any]:
        del model, fields
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


# ---------------------------------------------------------------------------
# B-206 K-5 —— query / document markers from the model catalog
# ---------------------------------------------------------------------------

_TASK = "Given a search query, retrieve relevant passages that answer the query"


async def _bodies(texts: list[str], marker: EmbeddingMarker = NO_MARKER) -> list[dict[str, Any]]:
    """The JSON bodies an embedder with ``marker`` posts for ``texts``."""
    seen: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        rows = [{"index": i, "embedding": [1.0]} for i in range(len(body["input"]))]
        return httpx.Response(200, json={"data": rows})

    client = HTTPEmbeddingClient(api_key="k", transport=httpx.MockTransport(_handler))
    embedder = OpenAICompatibleEmbedder(client=client, model="m", marker=marker)
    await embedder.embed(texts, tenant_id=uuid4())
    return seen


def _caps(provider: str, name: str) -> EmbeddingCaps | None:
    entry = catalog_entry(provider, name)
    assert entry is not None
    return entry.embedding_caps


@pytest.mark.asyncio
async def test_an_embedder_without_a_marker_posts_only_model_and_input() -> None:
    # Long-term memory (and every other unsided caller) builds embedders this way.
    assert await _bodies(["q"]) == [{"model": "m", "input": ["q"]}]


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["query", "document"])
@pytest.mark.parametrize(
    ("provider", "name"),
    [("openai", "text-embedding-3-large"), ("glm", "embedding-3"), ("qwen", "text-embedding-v4")],
)
async def test_asymmetry_none_adds_nothing_on_either_side(
    provider: str, name: str, side: EmbeddingSide
) -> None:
    # Every catalog embedding model today — qwen v4 included (its instruct
    # showed no gain in the dev-set A/B, so it is not enabled).
    marker = embedding_marker(_caps(provider, name), side)
    assert await _bodies(["q"], marker) == [{"model": "m", "input": ["q"]}]


def test_an_undeclared_model_gets_no_marker() -> None:
    # Off-catalog / caps not declared: degrade to today's request, no error.
    assert embedding_marker(None, "query") == NO_MARKER
    assert embedding_marker(None, "document") == NO_MARKER


@pytest.mark.asyncio
async def test_param_asymmetry_sends_its_enum_on_both_sides() -> None:
    caps = EmbeddingCaps(
        asymmetry="param",
        query_marker="input_type=search_query",
        document_marker="input_type=search_document",
    )
    assert await _bodies(["q"], embedding_marker(caps, "query")) == [
        {"model": "m", "input": ["q"], "input_type": "search_query"}
    ]
    assert await _bodies(["d"], embedding_marker(caps, "document")) == [
        {"model": "m", "input": ["d"], "input_type": "search_document"}
    ]


@pytest.mark.asyncio
async def test_prefix_asymmetry_wraps_queries_with_the_task_and_leaves_documents() -> None:
    caps = EmbeddingCaps(
        asymmetry="prefix", query_marker="Instruct: {task}\nQuery: {query}", instruction=_TASK
    )
    # Braces in the query itself are sent verbatim, not read as a placeholder.
    assert await _bodies(["what is {query}?"], embedding_marker(caps, "query")) == [
        {"model": "m", "input": [f"Instruct: {_TASK}\nQuery: what is {{query}}?"]}
    ]
    assert await _bodies(["d"], embedding_marker(caps, "document")) == [
        {"model": "m", "input": ["d"]}
    ]


@pytest.mark.asyncio
async def test_prefix_asymmetry_wraps_documents_when_it_declares_a_template() -> None:
    caps = EmbeddingCaps(
        asymmetry="prefix",
        query_marker="search_query: {query}",
        document_marker="search_document: {document}",
    )
    assert await _bodies(["d1", "d2"], embedding_marker(caps, "document")) == [
        {"model": "m", "input": ["search_document: d1", "search_document: d2"]}
    ]
    assert await _bodies(["q"], embedding_marker(caps, "query")) == [
        {"model": "m", "input": ["search_query: q"]}
    ]


@pytest.mark.asyncio
async def test_query_field_sends_the_instruction_with_queries_and_nothing_with_documents() -> None:
    # The shape DashScope's ``instruct`` has (measured on text-embedding-v4,
    # which the catalog nonetheless leaves at "none" — see its entry).
    caps = EmbeddingCaps(asymmetry="query_field", query_marker="instruct", instruction=_TASK)
    assert await _bodies(["q"], embedding_marker(caps, "query")) == [
        {"model": "m", "input": ["q"], "instruct": _TASK}
    ]
    # Documents unchanged → vectors already stored stay valid, no re-ingest.
    assert embedding_marker(caps, "document") == NO_MARKER
    assert await _bodies(["d"], embedding_marker(caps, "document")) == [
        {"model": "m", "input": ["d"]}
    ]


@pytest.mark.asyncio
async def test_the_byte_cap_holds_for_the_wrapped_text() -> None:
    marker = EmbeddingMarker(prefix="search_query: ")
    client = _EchoEmbeddingClient()
    embedder = OpenAICompatibleEmbedder(client=client, model="m", max_input_bytes=20, marker=marker)
    await embedder.embed(["x" * 50], tenant_id=uuid4())
    assert client.calls == [["search_query: xxxxxx"]]


def test_the_model_key_names_a_marker_only_when_there_is_one() -> None:
    plain = marked_model_key("qwen/text-embedding-v4", NO_MARKER)
    assert plain == "qwen/text-embedding-v4"  # chunks stored before markers stay reusable
    by_field = marked_model_key("v/m", EmbeddingMarker(fields=(("input_type", "search_document"),)))
    by_prefix = marked_model_key("v/m", EmbeddingMarker(prefix="search_document: "))
    assert len({plain, by_field, by_prefix, "v/m"}) == 4
    assert "input_type=search_document" in by_field


@pytest.mark.parametrize(
    ("provider", "name", "batch"),
    [
        ("qwen", "text-embedding-v4", 10),
        ("openai", "text-embedding-3-large", 2_048),
        ("glm", "embedding-3", 64),
    ],
)
def test_batch_size_comes_from_the_catalog(provider: str, name: str, batch: int) -> None:
    # K-8 — only the batch size: the catalog's per-input token figure is a doc
    # value, not a hard limit, so the input cap stays the deployment's.
    assert catalog_batch_size(_caps(provider, name)) == batch


def test_batch_size_defaults_without_a_declared_one() -> None:
    assert catalog_batch_size(None) == 10
    assert catalog_batch_size(EmbeddingCaps(asymmetry="none")) == 10
