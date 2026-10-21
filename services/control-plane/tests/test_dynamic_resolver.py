"""Stream T (PR B) — DynamicResolvingEmbedder/Reranker read the live platform
embedding/rerank config at CALL time, so an admin's config change takes effect
without a process restart (Mini-ADR T-3)."""

import json
from uuid import uuid4

import httpx
import pytest

from control_plane.runtime import (
    DynamicResolvingEmbedder,
    DynamicResolvingReranker,
    _is_dashscope_rerank_model,
)
from expert_work.common.credentials import CredentialsResolverError
from expert_work.protocol import ModelEntry
from expert_work.protocol.model_catalog import EmbeddingCaps
from orchestrator.errors import AgentFactoryError


class _Cfg:
    """Fake config service whose embedding/rerank config can change between calls."""

    def __init__(self) -> None:
        self.embedding: tuple[str, str] | None = ("qwen", "text-embedding-v4")
        self.rerank: tuple[str, str] | None = None

    async def effective_embedding_config(self) -> tuple[str, str] | None:
        return self.embedding

    async def effective_rerank_config(self) -> tuple[str, str] | None:
        return self.rerank


class _Resolver:
    async def resolve_provider(self, *, tenant_id, provider):
        return f"secret://{provider}"


class _SecretStore:
    async def get(self, name):
        return "fake-key"


@pytest.mark.asyncio
async def test_embedder_reads_current_config_each_call(monkeypatch):
    captured: list[str] = []

    class _FakeDelegate:
        def __init__(self, *, client, model, **_):
            captured.append(model)

        async def embed(self, texts, *, tenant_id):
            return [(0.0,) for _ in texts]

    # seam: patch the delegate the dynamic embedder constructs, plus the HTTP
    # client so NO network happens.
    monkeypatch.setattr("control_plane.runtime.OpenAICompatibleEmbedder", _FakeDelegate)
    monkeypatch.setattr("control_plane.runtime.HTTPEmbeddingClient", lambda **kw: object())

    cfg = _Cfg()
    emb = DynamicResolvingEmbedder(
        config_service=cfg, resolver=_Resolver(), secret_store=_SecretStore()
    )
    await emb.embed(["a"], tenant_id=uuid4())
    cfg.embedding = ("glm", "embedding-3")  # admin changes config
    await emb.embed(["b"], tenant_id=uuid4())
    assert captured == ["text-embedding-v4", "embedding-3"]  # second call used NEW model


@pytest.mark.asyncio
async def test_pin_fixes_one_model_for_the_whole_unit_of_work(monkeypatch):
    """B-175 L2-PR3 — a knowledge ingest pins the model once: an admin switch
    in the middle does not change the embedder it already holds, and the
    returned key names provider and model (chunks are tagged with it)."""
    captured: list[str] = []

    class _FakeDelegate:
        def __init__(self, *, client, model, **_):
            self.model = model

        async def embed(self, texts, *, tenant_id):
            captured.append(self.model)
            return [(0.0,) for _ in texts]

    monkeypatch.setattr("control_plane.runtime.OpenAICompatibleEmbedder", _FakeDelegate)
    monkeypatch.setattr("control_plane.runtime.HTTPEmbeddingClient", lambda **kw: object())

    cfg = _Cfg()
    emb = DynamicResolvingEmbedder(
        config_service=cfg, resolver=_Resolver(), secret_store=_SecretStore()
    )
    model_key, pinned = await emb.pin(tenant_id=uuid4())
    assert model_key == "qwen/text-embedding-v4"
    cfg.embedding = ("glm", "embedding-3")  # admin changes config mid-ingest
    await pinned.embed(["a"], tenant_id=uuid4())
    assert captured == ["text-embedding-v4"]


@pytest.mark.asyncio
async def test_pinned_embedder_carries_the_input_byte_cap(monkeypatch):
    # B-200 — the deployment's cap reaches the embedder that sends the request.
    monkeypatch.setattr("control_plane.runtime.HTTPEmbeddingClient", lambda **kw: object())
    emb = DynamicResolvingEmbedder(
        config_service=_Cfg(),
        resolver=_Resolver(),
        secret_store=_SecretStore(),
        max_input_bytes=512,
    )
    _, pinned = await emb.pin(tenant_id=uuid4())
    assert pinned.max_input_bytes == 512


# ---------------------------------------------------------------------------
# B-206 K-5 / K-8 —— knowledge sides follow the model catalog; memory does not
# ---------------------------------------------------------------------------

_TASK = "Given a search query, retrieve relevant passages that answer the query"


def _recording_embedder(cfg: _Cfg) -> tuple[DynamicResolvingEmbedder, list[dict]]:
    """An embedder whose requests land in the returned list (no network)."""
    bodies: list[dict] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        rows = [{"index": i, "embedding": [0.5]} for i in range(len(body["input"]))]
        return httpx.Response(200, json={"data": rows})

    http = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    emb = DynamicResolvingEmbedder(
        config_service=cfg, resolver=_Resolver(), secret_store=_SecretStore(), http=http
    )
    return emb, bodies


@pytest.mark.asyncio
async def test_memory_path_embed_sends_no_marker():
    # Long-term memory recall / write-back call ``embed``: byte-for-byte the
    # request it sent before K-5, even on a model with a query marker.
    emb, bodies = _recording_embedder(_Cfg())
    await emb.embed(["what did I order"], tenant_id=uuid4())
    assert bodies == [{"model": "text-embedding-v4", "input": ["what did I order"]}]


@pytest.mark.asyncio
async def test_knowledge_query_carries_the_catalog_query_marker():
    emb, bodies = _recording_embedder(_Cfg())
    vector = await emb.embed_query("what did I order", tenant_id=uuid4())
    assert vector == (0.5,)
    assert bodies == [
        {"model": "text-embedding-v4", "input": ["what did I order"], "instruct": _TASK}
    ]


@pytest.mark.asyncio
async def test_qwen_v4_documents_keep_their_key_and_request():
    # Document side unchanged for qwen v4 → the key stored on chunks is the one
    # every existing chunk already carries, so they are still reused.
    emb, bodies = _recording_embedder(_Cfg())
    model_key, pinned = await emb.pin(tenant_id=uuid4())
    await pinned.embed(["a passage"], tenant_id=uuid4())
    assert model_key == "qwen/text-embedding-v4"
    assert bodies == [{"model": "text-embedding-v4", "input": ["a passage"]}]


@pytest.mark.asyncio
async def test_a_document_marker_is_part_of_the_stored_model_key(monkeypatch):
    caps = EmbeddingCaps(
        asymmetry="param",
        query_marker="input_type=search_query",
        document_marker="input_type=search_document",
    )
    monkeypatch.setattr(
        "control_plane.runtime.catalog_entry",
        lambda provider, name: ModelEntry(name=name, embeddings=True, embedding_caps=caps),
    )
    emb, bodies = _recording_embedder(_Cfg())
    model_key, pinned = await emb.pin(tenant_id=uuid4())
    await pinned.embed(["a passage"], tenant_id=uuid4())
    # Chunks embedded without this marker (or under another one) are not reused.
    assert model_key == "qwen/text-embedding-v4#input_type=search_document"
    assert bodies == [
        {"model": "text-embedding-v4", "input": ["a passage"], "input_type": "search_document"}
    ]


@pytest.mark.asyncio
async def test_knowledge_sides_batch_by_the_catalog_and_memory_keeps_ten():
    cfg = _Cfg()
    cfg.embedding = ("glm", "embedding-3")  # catalog: 64 per request, 3,072 per input
    emb, bodies = _recording_embedder(cfg)
    texts = [f"t{i}" for i in range(11)]
    _, pinned = await emb.pin(tenant_id=uuid4())
    await pinned.embed(texts, tenant_id=uuid4())
    assert [len(body["input"]) for body in bodies] == [11]
    assert pinned.max_input_bytes == 3_072
    bodies.clear()
    await emb.embed(texts, tenant_id=uuid4())
    assert [len(body["input"]) for body in bodies] == [10, 1]


@pytest.mark.asyncio
async def test_embedder_raises_when_unconfigured():
    cfg = _Cfg()
    cfg.embedding = None
    emb = DynamicResolvingEmbedder(
        config_service=cfg, resolver=_Resolver(), secret_store=_SecretStore()
    )
    with pytest.raises(AgentFactoryError):
        await emb.embed(["a"], tenant_id=uuid4())


@pytest.mark.asyncio
async def test_reranker_degrades_to_identity_when_unconfigured():
    cfg = _Cfg()
    cfg.rerank = None
    rr = DynamicResolvingReranker(
        config_service=cfg, resolver=_Resolver(), secret_store=_SecretStore()
    )
    out = await rr.rerank(query="q", documents=["a", "b", "c"], top_k=2, tenant_id=uuid4())
    assert out == [0, 1]  # identity order, no error


def test_is_dashscope_rerank_model() -> None:
    assert _is_dashscope_rerank_model("qwen", "qwen3-vl-rerank")
    assert _is_dashscope_rerank_model("qwen", "gte-rerank-v2")
    assert not _is_dashscope_rerank_model("qwen", "qwen-plus")  # chat model
    assert not _is_dashscope_rerank_model("doubao", "doubao-rerank")  # only qwen/DashScope


@pytest.mark.asyncio
async def test_reranker_routes_dashscope_rerank_model_to_native_api(monkeypatch):
    """A DashScope dedicated rerank model goes to the native rerank API, NOT the
    chat-prompt LLMReranker (which 404s on it)."""

    class _FakeRerankClient:
        def __init__(self, **_kw):
            pass

        async def rerank(self, *, model, query, documents, top_n):
            return {"output": {"results": [{"index": 1}, {"index": 0}]}}

    monkeypatch.setattr("control_plane.runtime.HTTPDashScopeRerankClient", _FakeRerankClient)

    def _boom(*_a, **_k):
        raise AssertionError("LLM router (chat) path used for a native rerank model")

    monkeypatch.setattr("control_plane.runtime.build_llm_router", _boom)

    cfg = _Cfg()
    cfg.rerank = ("qwen", "qwen3-vl-rerank")
    rr = DynamicResolvingReranker(
        config_service=cfg, resolver=_Resolver(), secret_store=_SecretStore()
    )
    out = await rr.rerank(query="q", documents=["a", "b"], top_k=2, tenant_id=uuid4())
    assert out == [1, 0]  # native rerank order


@pytest.mark.asyncio
async def test_reranker_scored_keeps_the_native_api_scores(monkeypatch):
    """The two-view rerank (B-175 wave 2 PR2b) needs the native API's scores."""

    class _FakeRerankClient:
        def __init__(self, **_kw):
            pass

        async def rerank(self, *, model, query, documents, top_n):
            return {
                "output": {
                    "results": [
                        {"index": 1, "relevance_score": 0.8},
                        {"index": 0, "relevance_score": 0.3},
                    ]
                }
            }

    monkeypatch.setattr("control_plane.runtime.HTTPDashScopeRerankClient", _FakeRerankClient)
    cfg = _Cfg()
    cfg.rerank = ("qwen", "qwen3-vl-rerank")
    rr = DynamicResolvingReranker(
        config_service=cfg, resolver=_Resolver(), secret_store=_SecretStore()
    )
    out = await rr.rerank_scored(query="q", documents=["a", "b"], top_k=2, tenant_id=uuid4())
    assert out == [(1, 0.8), (0, 0.3)]


class _RaisingResolver:
    async def resolve_provider(self, *, tenant_id, provider):
        raise CredentialsResolverError(
            "no credential", mode="platform", kind="provider", key=provider
        )


@pytest.mark.asyncio
async def test_reranker_degrades_to_identity_when_credential_missing():
    cfg = _Cfg()
    cfg.rerank = ("qwen", "qwen3-vl-rerank")  # configured, but credential resolve fails
    rr = DynamicResolvingReranker(
        config_service=cfg, resolver=_RaisingResolver(), secret_store=_SecretStore()
    )
    out = await rr.rerank(query="q", documents=["a", "b", "c"], top_k=2, tenant_id=uuid4())
    assert out == [0, 1]  # identity order, no error
    scored = await rr.rerank_scored(
        query="q", documents=["a", "b", "c"], top_k=2, tenant_id=uuid4()
    )
    assert scored == [(0, None), (1, None)]  # no scores: the retriever keeps this order
