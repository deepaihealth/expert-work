"""Stream T (PR B) — DynamicResolvingEmbedder/Reranker read the live platform
embedding/rerank config at CALL time, so an admin's config change takes effect
without a process restart (Mini-ADR T-3)."""

import json
from uuid import uuid4

import httpx
import pytest

from control_plane.rerank_vendors import is_dashscope_rerank_model
from control_plane.runtime import (
    DynamicResolvingEmbedder,
    DynamicResolvingReranker,
    ResolvingReranker,
    resolve_reranker,
)
from expert_work.common.credentials import CredentialsResolverError
from expert_work.protocol import ModelEntry
from expert_work.protocol.model_catalog import EmbeddingCaps
from orchestrator.errors import AgentFactoryError
from orchestrator.llm import DEFAULT_MAX_INPUT_BYTES, RerankCallError, RerankGuards
from orchestrator.tools.knowledge import RerankRanking


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


def _declare_caps(monkeypatch: pytest.MonkeyPatch, caps: EmbeddingCaps) -> None:
    """Make the live model's catalog entry declare ``caps`` (no catalog model
    has a marker enabled today)."""
    monkeypatch.setattr(
        "control_plane.runtime.catalog_entry",
        lambda provider, name: ModelEntry(name=name, embeddings=True, embedding_caps=caps),
    )


_QUERY_FIELD = EmbeddingCaps(asymmetry="query_field", query_marker="instruct", instruction=_TASK)


@pytest.mark.asyncio
async def test_memory_path_embed_sends_no_marker(monkeypatch):
    # Long-term memory recall / write-back call ``embed``: byte-for-byte the
    # request it sent before K-5, even on a model with a query marker.
    _declare_caps(monkeypatch, _QUERY_FIELD)
    emb, bodies = _recording_embedder(_Cfg())
    await emb.embed(["what did I order"], tenant_id=uuid4())
    assert bodies == [{"model": "text-embedding-v4", "input": ["what did I order"]}]


@pytest.mark.asyncio
async def test_knowledge_query_carries_the_catalog_query_marker(monkeypatch):
    _declare_caps(monkeypatch, _QUERY_FIELD)
    emb, bodies = _recording_embedder(_Cfg())
    vector = await emb.embed_query("what did I order", tenant_id=uuid4())
    assert vector == (0.5,)
    assert bodies == [
        {"model": "text-embedding-v4", "input": ["what did I order"], "instruct": _TASK}
    ]


@pytest.mark.asyncio
async def test_qwen_v4_queries_and_documents_go_out_unmarked():
    # The real catalog entry: no marker enabled (dev-set A/B showed no gain), so
    # knowledge queries and documents send what they sent before K-5, and the
    # key stored on chunks is the one every existing chunk carries.
    emb, bodies = _recording_embedder(_Cfg())
    await emb.embed_query("what did I order", tenant_id=uuid4())
    model_key, pinned = await emb.pin(tenant_id=uuid4())
    await pinned.embed(["a passage"], tenant_id=uuid4())
    assert model_key == "qwen/text-embedding-v4"
    assert bodies == [
        {"model": "text-embedding-v4", "input": ["what did I order"]},
        {"model": "text-embedding-v4", "input": ["a passage"]},
    ]


@pytest.mark.asyncio
async def test_a_document_marker_is_part_of_the_stored_model_key(monkeypatch):
    caps = EmbeddingCaps(
        asymmetry="param",
        query_marker="input_type=search_query",
        document_marker="input_type=search_document",
    )
    _declare_caps(monkeypatch, caps)
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
    cfg.embedding = ("glm", "embedding-3")  # catalog: 64 per request, doc says 3,072 tokens
    emb, bodies = _recording_embedder(cfg)
    texts = [f"t{i}" for i in range(11)]
    _, pinned = await emb.pin(tenant_id=uuid4())
    await pinned.embed(texts, tenant_id=uuid4())
    assert [len(body["input"]) for body in bodies] == [11]
    # The per-input cap stays the deployment's: a vendor's documented token
    # figure is not a hard limit (B-200) and is not applied as bytes.
    assert pinned.max_input_bytes == DEFAULT_MAX_INPUT_BYTES
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
    # B-206 K-7 — and the search learns why.
    scored = await rr.rerank_scored(query="q", documents=["a", "b"], top_k=2, tenant_id=uuid4())
    assert isinstance(scored, RerankRanking)
    assert scored.outcome == "no_model"


def test_is_dashscope_rerank_model() -> None:
    assert is_dashscope_rerank_model("qwen", "qwen3-vl-rerank")
    assert is_dashscope_rerank_model("qwen", "gte-rerank-v2")
    assert not is_dashscope_rerank_model("qwen", "qwen-plus")  # chat model
    assert not is_dashscope_rerank_model("doubao", "doubao-rerank")  # only qwen/DashScope


@pytest.mark.asyncio
async def test_reranker_routes_dashscope_rerank_model_to_native_api(monkeypatch):
    """A DashScope dedicated rerank model goes to the native rerank API, NOT the
    chat-prompt LLMReranker (which 404s on it)."""

    class _FakeRerankClient:
        def __init__(self, **_kw):
            pass

        async def rerank(self, *, model, query, documents, top_n):
            return {"output": {"results": [{"index": 1}, {"index": 0}]}}

    monkeypatch.setattr("control_plane.rerank_vendors.HTTPDashScopeRerankClient", _FakeRerankClient)

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

    monkeypatch.setattr("control_plane.rerank_vendors.HTTPDashScopeRerankClient", _FakeRerankClient)
    cfg = _Cfg()
    cfg.rerank = ("qwen", "qwen3-vl-rerank")
    rr = DynamicResolvingReranker(
        config_service=cfg, resolver=_Resolver(), secret_store=_SecretStore()
    )
    out = await rr.rerank_scored(query="q", documents=["a", "b"], top_k=2, tenant_id=uuid4())
    assert out == [(1, 0.8), (0, 0.3)]


@pytest.mark.asyncio
async def test_resolving_rerankers_pass_the_task_instruction_to_the_native_api(monkeypatch):
    """B-206 K-14 — the single view's task instruction reaches DashScope through
    both resolving rerankers (the model, qwen3-vl-rerank, takes one)."""
    instructions = []

    class _FakeRerankClient:
        def __init__(self, **_kw):
            pass

        async def rerank(self, *, model, query, documents, top_n, instruction=None):
            instructions.append(instruction)
            return {"output": {"results": [{"index": 0, "relevance_score": 0.5}]}}

    monkeypatch.setattr("control_plane.rerank_vendors.HTTPDashScopeRerankClient", _FakeRerankClient)
    cfg = _Cfg()
    cfg.rerank = ("qwen", "qwen3-vl-rerank")
    dynamic = DynamicResolvingReranker(
        config_service=cfg, resolver=_Resolver(), secret_store=_SecretStore()
    )
    resolving = ResolvingReranker(
        resolver=_Resolver(), secret_store=_SecretStore(), provider="qwen", model="qwen3-vl-rerank"
    )
    for reranker in (dynamic, resolving):
        await reranker.rerank_scored(
            query="q", documents=["a"], top_k=1, tenant_id=uuid4(), instruction="judge it"
        )
    assert instructions == ["judge it", "judge it"]


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
    # B-206 K-7 — a configured model whose credential is missing, for both rerankers.
    static = ResolvingReranker(
        resolver=_RaisingResolver(),
        secret_store=_SecretStore(),
        provider="qwen",
        model="qwen3-vl-rerank",
    )
    for reranker in (rr, static):
        ranked = await reranker.rerank_scored(
            query="q", documents=["a"], top_k=1, tenant_id=uuid4()
        )
        assert isinstance(ranked, RerankRanking)
        assert ranked.outcome == "model_unavailable"


class _Bucket:
    def __init__(self) -> None:
        self.taken = 0

    async def __aenter__(self) -> None:
        self.taken += 1

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.mark.asyncio
async def test_both_resolving_rerankers_send_native_requests_through_the_guard(monkeypatch):
    """B-206 K-7 — the DashScope branch is paced from the model's bucket (keyed
    by the model and the credential reference, never the key) and retried on a
    429 by the vendor-neutral guard, for the dynamic and the static reranker."""
    built: list[tuple[str, int]] = []
    shared = _Bucket()

    def _buckets(*, bucket: str, rpm: int) -> _Bucket:
        built.append((bucket, rpm))
        return shared

    calls: list[int] = []

    class _FlakyClient:
        def __init__(self, **_kw):
            pass

        async def rerank(self, *, model, query, documents, top_n):
            calls.append(1)
            if len(calls) % 2 == 1:
                raise RerankCallError("429", failure="rate_limited", status=429)
            return {"output": {"results": [{"index": 0, "relevance_score": 0.5}]}}

    async def _no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("control_plane.rerank_vendors.HTTPDashScopeRerankClient", _FlakyClient)
    guards = RerankGuards(buckets=_buckets, sleep=_no_sleep)
    cfg = _Cfg()
    cfg.rerank = ("qwen", "qwen3-vl-rerank")
    dynamic = DynamicResolvingReranker(
        config_service=cfg, resolver=_Resolver(), secret_store=_SecretStore(), rerank_guards=guards
    )
    static = ResolvingReranker(
        resolver=_Resolver(),
        secret_store=_SecretStore(),
        provider="qwen",
        model="qwen3-vl-rerank",
        rerank_guards=guards,
    )
    for reranker in (dynamic, static):
        out = await reranker.rerank_scored(query="q", documents=["a"], top_k=1, tenant_id=uuid4())
        assert out == [(0, 0.5)]
    assert len(calls) == 4  # each reranker: a 429, then its retry
    assert shared.taken == 4  # every request, retries included, took a token
    # One bucket for the model + credential, shared by both rerankers, at the catalog's 600 RPM.
    assert built == [("rl:llm:rerank:qwen:qwen3-vl-rerank:secret://qwen", 600)]


class _DownChatModel:
    async def __call__(self, **_kwargs):
        raise RuntimeError("chat model down")


@pytest.mark.asyncio
async def test_the_chat_model_rerank_branch_says_when_it_fell_back(monkeypatch):
    """B-206 K-7a (M7) — the chat-model reranker swallows a failure and returns
    the input order; the search must not report that as the model's ranking.
    Long-term memory's plain ``rerank`` keeps its list of indices."""

    async def _router(*_args, **_kwargs):
        return _DownChatModel()

    monkeypatch.setattr("control_plane.runtime.build_llm_router", _router)
    cfg = _Cfg()
    cfg.rerank = ("anthropic", "claude-haiku-4-5")  # a chat model → the LLM branch
    dynamic = DynamicResolvingReranker(
        config_service=cfg, resolver=_Resolver(), secret_store=_SecretStore()
    )
    static = ResolvingReranker(
        resolver=_Resolver(),
        secret_store=_SecretStore(),
        provider="anthropic",
        model="claude-haiku-4-5",
    )
    for reranker in (dynamic, static):
        ranked = await reranker.rerank_scored(
            query="q", documents=["a", "b"], top_k=2, tenant_id=uuid4()
        )
        assert isinstance(ranked, RerankRanking)
        assert (list(ranked), ranked.outcome) == ([(0, None), (1, None)], "model_error")
        plain = await reranker.rerank(query="q", documents=["a", "b"], top_k=2, tenant_id=uuid4())
        assert plain == [0, 1]


@pytest.mark.asyncio
async def test_resolve_reranker_passes_the_process_guards_through():
    """B-206 K-7a (L7)."""
    guards = RerankGuards()
    reranker = await resolve_reranker(
        resolver=_Resolver(),
        secret_store=_SecretStore(),
        provider="qwen",
        model="qwen3-vl-rerank",
        supported_providers=["qwen"],
        rerank_guards=guards,
    )
    assert isinstance(reranker, ResolvingReranker)
    assert reranker.rerank_guards is guards


def test_rerankers_holding_the_guards_stay_hashable():
    """B-206 K-7a (L5) — frozen dataclasses hash their fields."""
    cfg = _Cfg()
    dynamic = DynamicResolvingReranker(
        config_service=cfg, resolver=_Resolver(), secret_store=_SecretStore()
    )
    assert isinstance(hash(dynamic), int)
