"""Tests for ``/v1/knowledge`` — Stream J.5 knowledge-base + document API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import Role
from orchestrator.llm import FakeEmbedder
from orchestrator.tools import KnowledgeRetriever
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt

_TENANT = DEFAULT_DEV_TENANT_ID


def _settings() -> Settings:
    return Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT, subject='user-a')}"}


Setup = tuple[AsyncClient, KnowledgeIngestionRunner]
FullSetup = tuple[AsyncClient, KnowledgeIngestionRunner, InMemoryKnowledgeStore]


@pytest.fixture
async def setup() -> AsyncIterator[Setup]:
    store = InMemoryKnowledgeStore()
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder())
    app = create_app(
        settings=_settings(),
        knowledge_repo=store,
        knowledge_ingestion_runner=runner,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers()
    ) as client:
        yield client, runner


class _FakeEmbeddingConfig:
    """Mutable stand-in for ``PlatformEmbeddingConfigService`` — tests flip
    ``pair`` to simulate a platform embedding-model change."""

    def __init__(self, pair: tuple[str, str] | None) -> None:
        self.pair = pair

    async def effective_embedding_config(self) -> tuple[str, str] | None:
        return self.pair


ReindexSetup = tuple[AsyncClient, KnowledgeIngestionRunner, _FakeEmbeddingConfig]


@pytest.fixture
async def reindex_setup() -> AsyncIterator[ReindexSetup]:
    """``full_setup`` plus a mutable fake embedding-config service so tests can
    drive the ``needs_reindex`` / re-index flow deterministically."""
    store = InMemoryKnowledgeStore()
    embedder = FakeEmbedder()
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    config = _FakeEmbeddingConfig(("qwen", "text-embedding-v4"))
    app = create_app(
        settings=_settings(),
        knowledge_repo=store,
        knowledge_ingestion_runner=runner,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    app.state.knowledge_retriever = KnowledgeRetriever(store=store, embedder=embedder)
    app.state.platform_embedding_config_service = config
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers()
    ) as client:
        yield client, runner, config


@pytest.fixture
async def full_setup() -> AsyncIterator[FullSetup]:
    """Like ``setup`` but also attaches a real :class:`KnowledgeRetriever`
    (the retrieval-test endpoint reads it off ``app.state``) and exposes the
    store so tests can assert/seed directly."""
    store = InMemoryKnowledgeStore()
    embedder = FakeEmbedder()
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    app = create_app(
        settings=_settings(),
        knowledge_repo=store,
        knowledge_ingestion_runner=runner,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    app.state.knowledge_retriever = KnowledgeRetriever(store=store, embedder=embedder)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers()
    ) as client:
        yield client, runner, store


# ---------------------------------------------------------------------------
# knowledge bases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_list_base(setup: Setup) -> None:
    client, _ = setup
    created = await client.post("/v1/knowledge/bases", json={"name": "hr-policies"})
    assert created.status_code == 201
    assert created.json()["name"] == "hr-policies"

    listed = await client.get("/v1/knowledge/bases")
    assert listed.status_code == 200
    assert [b["name"] for b in listed.json()["bases"]] == ["hr-policies"]


@pytest.mark.asyncio
async def test_create_base_duplicate_returns_409(setup: Setup) -> None:
    client, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    again = await client.post("/v1/knowledge/bases", json={"name": "kb"})
    assert again.status_code == 409


@pytest.mark.asyncio
async def test_create_base_with_custom_chunk_params(setup: Setup) -> None:
    client, _ = setup
    resp = await client.post(
        "/v1/knowledge/bases",
        json={"name": "tuned", "chunk_max_tokens": 256, "chunk_overlap_tokens": 16},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["chunk_max_tokens"] == 256
    assert body["chunk_overlap_tokens"] == 16


@pytest.mark.asyncio
async def test_create_base_rejects_overlap_not_below_max(setup: Setup) -> None:
    client, _ = setup
    resp = await client.post(
        "/v1/knowledge/bases",
        json={"name": "bad", "chunk_max_tokens": 100, "chunk_overlap_tokens": 100},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_base(setup: Setup) -> None:
    client, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    deleted = await client.delete("/v1/knowledge/bases/kb")
    assert deleted.status_code == 204
    listed = await client.get("/v1/knowledge/bases")
    assert listed.json()["bases"] == []


@pytest.mark.asyncio
async def test_delete_missing_base_returns_404(setup: Setup) -> None:
    client, _ = setup
    resp = await client.delete("/v1/knowledge/bases/ghost")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_document_ingests_to_ready(setup: Setup) -> None:
    client, runner = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})

    uploaded = await client.post(
        "/v1/knowledge/bases/kb/documents",
        files={"file": ("handbook.md", b"# Handbook\n\nThe deductible is 500.", "text/markdown")},
    )
    assert uploaded.status_code == 202
    assert uploaded.json()["status"] == "pending"

    # Ingestion runs in the background — wait for it, then poll the list.
    await runner.drain()
    documents = (await client.get("/v1/knowledge/bases/kb/documents")).json()["documents"]
    assert len(documents) == 1
    assert documents[0]["filename"] == "handbook.md"
    assert documents[0]["status"] == "ready"
    assert documents[0]["chunk_count"] >= 1


@pytest.mark.asyncio
async def test_upload_to_missing_base_returns_404(setup: Setup) -> None:
    client, _ = setup
    resp = await client.post(
        "/v1/knowledge/bases/ghost/documents",
        files={"file": ("x.md", b"body", "text/markdown")},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_upload_unsupported_extension_returns_400(setup: Setup) -> None:
    client, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    resp = await client.post(
        "/v1/knowledge/bases/kb/documents",
        files={"file": ("data.xyz", b"body", "application/octet-stream")},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_document(setup: Setup) -> None:
    client, runner = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    await client.post(
        "/v1/knowledge/bases/kb/documents",
        files={"file": ("doc.md", b"# Doc\n\nbody.", "text/markdown")},
    )
    await runner.drain()
    documents = (await client.get("/v1/knowledge/bases/kb/documents")).json()["documents"]
    document_id = documents[0]["id"]

    deleted = await client.delete(f"/v1/knowledge/bases/kb/documents/{document_id}")
    assert deleted.status_code == 204
    remaining = (await client.get("/v1/knowledge/bases/kb/documents")).json()["documents"]
    assert remaining == []


@pytest.mark.asyncio
async def test_delete_missing_document_returns_404(setup: Setup) -> None:
    client, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    resp = await client.delete(
        "/v1/knowledge/bases/kb/documents/00000000-0000-0000-0000-000000000000"
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# single-base view + stats + edit (commercial uplift)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_base_with_config_and_get_single(setup: Setup) -> None:
    client, runner = setup
    created = await client.post(
        "/v1/knowledge/bases",
        json={
            "name": "kb",
            "description": "HR docs",
            "retrieval_top_k": 8,
            "retrieval_score_threshold": 0.4,
            "retrieval_method": "vector",
            "rerank_enabled": False,
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["description"] == "HR docs"
    assert body["retrieval_config"] == {
        "top_k": 8,
        "score_threshold": 0.4,
        "method": "vector",
        "rerank_enabled": False,
    }
    assert body["stats"] == {"document_count": 0, "chunk_count": 0}

    # Upload a doc so stats are non-zero, then GET the single base.
    await client.post(
        "/v1/knowledge/bases/kb/documents",
        files={"file": ("h.md", b"# H\n\nThe deductible is 500.", "text/markdown")},
    )
    await runner.drain()
    single = await client.get("/v1/knowledge/bases/kb")
    assert single.status_code == 200
    sbody = single.json()
    assert sbody["stats"]["document_count"] == 1
    assert sbody["stats"]["chunk_count"] >= 1


@pytest.mark.asyncio
async def test_get_missing_base_returns_404(setup: Setup) -> None:
    client, _ = setup
    assert (await client.get("/v1/knowledge/bases/ghost")).status_code == 404


@pytest.mark.asyncio
async def test_list_bases_includes_stats(setup: Setup) -> None:
    client, runner = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    await client.post(
        "/v1/knowledge/bases/kb/documents",
        files={"file": ("d.md", b"# D\n\nbody text here.", "text/markdown")},
    )
    await runner.drain()
    listed = (await client.get("/v1/knowledge/bases")).json()["bases"]
    assert listed[0]["stats"]["document_count"] == 1


@pytest.mark.asyncio
async def test_patch_base_updates_config(setup: Setup) -> None:
    client, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb", "description": "orig"})
    patched = await client.patch(
        "/v1/knowledge/bases/kb",
        json={"retrieval_top_k": 12, "retrieval_method": "keyword"},
    )
    assert patched.status_code == 200
    cfg = patched.json()["retrieval_config"]
    assert cfg["top_k"] == 12
    assert cfg["method"] == "keyword"
    # Omitted description is preserved.
    assert patched.json()["description"] == "orig"


@pytest.mark.asyncio
async def test_patch_base_clear_vs_omit_nullable(setup: Setup) -> None:
    client, _ = setup
    await client.post(
        "/v1/knowledge/bases",
        json={"name": "kb", "description": "orig", "retrieval_score_threshold": 0.5},
    )
    # Explicit null clears the threshold; description omitted → unchanged.
    patched = await client.patch("/v1/knowledge/bases/kb", json={"retrieval_score_threshold": None})
    assert patched.status_code == 200
    assert patched.json()["retrieval_config"]["score_threshold"] is None
    assert patched.json()["description"] == "orig"


@pytest.mark.asyncio
async def test_patch_base_rejects_overlap_not_below_max(setup: Setup) -> None:
    client, _ = setup
    await client.post(
        "/v1/knowledge/bases",
        json={"name": "kb", "chunk_max_tokens": 200, "chunk_overlap_tokens": 16},
    )
    resp = await client.patch("/v1/knowledge/bases/kb", json={"chunk_overlap_tokens": 500})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_patch_base_rejects_bad_method(setup: Setup) -> None:
    client, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    resp = await client.patch("/v1/knowledge/bases/kb", json={"retrieval_method": "magic"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_missing_base_returns_404(setup: Setup) -> None:
    client, _ = setup
    assert (await client.patch("/v1/knowledge/bases/ghost", json={})).status_code == 404


# ---------------------------------------------------------------------------
# chunk preview + retrieval test (commercial uplift)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_chunks_preview(full_setup: FullSetup) -> None:
    client, runner, _ = full_setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    await client.post(
        "/v1/knowledge/bases/kb/documents",
        files={"file": ("h.md", b"# Handbook\n\nThe deductible is 500 dollars.", "text/markdown")},
    )
    await runner.drain()
    doc_id = (await client.get("/v1/knowledge/bases/kb/documents")).json()["documents"][0]["id"]
    resp = await client.get(f"/v1/knowledge/bases/kb/documents/{doc_id}/chunks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert body["chunks"][0]["chunk_index"] == 0
    assert "content" in body["chunks"][0]
    # The (large) embedding is never returned in a preview.
    assert "embedding" not in body["chunks"][0]


@pytest.mark.asyncio
async def test_list_chunks_unknown_document_404(full_setup: FullSetup) -> None:
    client, _, _ = full_setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    resp = await client.get(
        "/v1/knowledge/bases/kb/documents/00000000-0000-0000-0000-000000000000/chunks"
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_retrieval_test_returns_scored_results(full_setup: FullSetup) -> None:
    client, runner, _ = full_setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    await client.post(
        "/v1/knowledge/bases/kb/documents",
        files={"file": ("h.md", b"# H\n\nThe deductible is 500 dollars.", "text/markdown")},
    )
    await runner.drain()
    resp = await client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "deductible"
    assert body["count"] >= 1
    first = body["results"][0]
    assert set(first) >= {"content", "source", "filename", "chunk_index", "score", "recall_source"}
    assert first["source"].startswith("h.md#")


@pytest.mark.asyncio
async def test_retrieval_test_missing_base_404(full_setup: FullSetup) -> None:
    client, _, _ = full_setup
    resp = await client.post("/v1/knowledge/bases/ghost/test", json={"query": "x"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_retrieval_test_503_when_retriever_unavailable(setup: Setup) -> None:
    # The plain ``setup`` fixture does not attach a retriever (app.state value
    # is None), so the endpoint reports the embedding-unconfigured 503.
    client, _ = setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    resp = await client.post("/v1/knowledge/bases/kb/test", json={"query": "x"})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# embedding pin + needs_reindex + re-index (commercial uplift)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_pins_embedding_model(reindex_setup: ReindexSetup) -> None:
    client, _, _ = reindex_setup
    created = (await client.post("/v1/knowledge/bases", json={"name": "kb"})).json()
    assert created["embedding_provider"] == "qwen"
    assert created["embedding_model"] == "text-embedding-v4"
    assert created["needs_reindex"] is False


@pytest.mark.asyncio
async def test_needs_reindex_flips_on_model_change(reindex_setup: ReindexSetup) -> None:
    client, _, config = reindex_setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    # Platform admin swaps the embedding model.
    config.pair = ("qwen", "text-embedding-v5")
    single = (await client.get("/v1/knowledge/bases/kb")).json()
    assert single["needs_reindex"] is True


@pytest.mark.asyncio
async def test_reindex_reembeds_and_restamps(reindex_setup: ReindexSetup) -> None:
    client, runner, config = reindex_setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    await client.post(
        "/v1/knowledge/bases/kb/documents",
        files={"file": ("h.md", b"# H\n\nThe deductible is 500 dollars.", "text/markdown")},
    )
    await runner.drain()
    # Swap the model → base is now stale.
    config.pair = ("qwen", "text-embedding-v5")
    assert (await client.get("/v1/knowledge/bases/kb")).json()["needs_reindex"] is True

    accepted = await client.post("/v1/knowledge/bases/kb/reindex")
    assert accepted.status_code == 202
    await runner.drain()

    refreshed = (await client.get("/v1/knowledge/bases/kb")).json()
    assert refreshed["embedding_model"] == "text-embedding-v5"
    assert refreshed["needs_reindex"] is False
    assert refreshed["reindexing"] is False


@pytest.mark.asyncio
async def test_reindex_503_when_embedding_unconfigured(reindex_setup: ReindexSetup) -> None:
    client, _, config = reindex_setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    config.pair = None  # platform embedding unconfigured
    resp = await client.post("/v1/knowledge/bases/kb/reindex")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_reindex_missing_base_404(reindex_setup: ReindexSetup) -> None:
    client, _, _ = reindex_setup
    assert (await client.post("/v1/knowledge/bases/ghost/reindex")).status_code == 404


# ---------------------------------------------------------------------------
# document re-ingest (durability)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reingest_redrives_document(full_setup: FullSetup) -> None:
    client, runner, _ = full_setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    await client.post(
        "/v1/knowledge/bases/kb/documents",
        files={"file": ("h.md", b"# H\n\nThe deductible is 500 dollars.", "text/markdown")},
    )
    await runner.drain()
    doc_id = (await client.get("/v1/knowledge/bases/kb/documents")).json()["documents"][0]["id"]

    resp = await client.post(f"/v1/knowledge/bases/kb/documents/{doc_id}/reingest")
    assert resp.status_code == 202
    assert resp.json()["status"] == "pending"
    await runner.drain()
    refreshed = (await client.get("/v1/knowledge/bases/kb/documents")).json()["documents"][0]
    assert refreshed["status"] == "ready"


@pytest.mark.asyncio
async def test_reingest_without_bytes_returns_409(full_setup: FullSetup) -> None:
    client, _, store = full_setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    base = (await client.get("/v1/knowledge/bases/kb")).json()
    # A legacy document with no retained bytes (seeded straight on the store).
    doc = await store.upsert_document(
        tenant_id=_TENANT, kb_id=UUID(base["id"]), filename="legacy.md"
    )
    resp = await client.post(f"/v1/knowledge/bases/kb/documents/{doc.id}/reingest")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_reingest_missing_document_404(full_setup: FullSetup) -> None:
    client, _, _ = full_setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    resp = await client.post(
        "/v1/knowledge/bases/kb/documents/00000000-0000-0000-0000-000000000000/reingest"
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# W3/W4 — knowledge 读端点接跨租户 scope(系统管理员租户切换器)
#
# 三件套 per endpoint:system_admin 带目标租户 tenant_id → 200;普通租户
# 用户带他租户 tenant_id → 403 TENANT_NOT_ALLOWED;详情端点 tenant_id=* →
# 400 SCOPE_ALL_NOT_SUPPORTED。列表 tenant_id=* → W4 真聚合(全租户行,
# 每行带 tenant_id)。照 test_agents_api.py W2 先例。
# ---------------------------------------------------------------------------


async def _grant_system_admin(client: AsyncClient) -> dict[str, str]:
    """Seed a platform-scope binding; return headers for a system_admin whose
    HOME tenant differs from ``_TENANT`` (the tenant under test)."""
    sys_admin_id = uuid4()
    app = client._transport.app  # type: ignore[attr-defined,union-attr]
    await app.state.role_binding_repo.create(
        subject_type="user",
        subject_id=sys_admin_id,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    token = make_test_jwt(tenant_id=uuid4(), subject=str(sys_admin_id))
    return {"Authorization": f"Bearer {token}"}


async def _seed_kb_with_document(client: AsyncClient, runner: KnowledgeIngestionRunner) -> str:
    """Create base ``kb`` with one ready document in ``_TENANT``; return doc id."""
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    await client.post(
        "/v1/knowledge/bases/kb/documents",
        files={"file": ("h.md", b"# H\n\nThe deductible is 500.", "text/markdown")},
    )
    await runner.drain()
    documents = (await client.get("/v1/knowledge/bases/kb/documents")).json()["documents"]
    return str(documents[0]["id"])


@pytest.mark.asyncio
async def test_knowledge_scope_system_admin_target_tenant_200(full_setup: FullSetup) -> None:
    client, runner, _ = full_setup
    doc_id = await _seed_kb_with_document(client, runner)
    headers = await _grant_system_admin(client)
    params = {"tenant_id": str(_TENANT)}

    listed = await client.get("/v1/knowledge/bases", params=params, headers=headers)
    assert listed.status_code == 200, listed.text
    assert [b["name"] for b in listed.json()["bases"]] == ["kb"]

    single = await client.get("/v1/knowledge/bases/kb", params=params, headers=headers)
    assert single.status_code == 200, single.text
    assert single.json()["name"] == "kb"

    docs = await client.get("/v1/knowledge/bases/kb/documents", params=params, headers=headers)
    assert docs.status_code == 200, docs.text
    assert [d["id"] for d in docs.json()["documents"]] == [doc_id]

    chunks = await client.get(
        f"/v1/knowledge/bases/kb/documents/{doc_id}/chunks", params=params, headers=headers
    )
    assert chunks.status_code == 200, chunks.text
    assert chunks.json()["total"] >= 1

    hit = await client.post(
        "/v1/knowledge/bases/kb/test",
        params=params,
        headers=headers,
        json={"query": "deductible"},
    )
    assert hit.status_code == 200, hit.text
    assert hit.json()["count"] >= 1


@pytest.mark.asyncio
async def test_knowledge_list_bases_star_aggregates_all_tenants(full_setup: FullSetup) -> None:
    """W4:system_admin ``tenant_id=*`` 真聚合——全租户 base,每行带
    ``tenant_id``;非聚合分支的行同样带 ``tenant_id``(值=该租户)。
    Review C-9 — 同名跨租户 pair((tenant_id, name) 唯一,name 不唯一):
    断言按 (tenant_id, name) 键,两行都在且 tenant_id 不同。"""
    client, runner, store = full_setup
    await _seed_kb_with_document(client, runner)
    other_tenant = uuid4()
    # Same-name pair across tenants — the schema only dedups (tenant_id, name).
    await store.create_base(tenant_id=other_tenant, name="kb")
    await store.create_base(tenant_id=other_tenant, name="other-kb")

    # Non-aggregate branch: items carry tenant_id = the scoped tenant.
    plain = await client.get("/v1/knowledge/bases")
    assert [b["tenant_id"] for b in plain.json()["bases"]] == [str(_TENANT)]

    headers = await _grant_system_admin(client)
    resp = await client.get("/v1/knowledge/bases", params={"tenant_id": "*"}, headers=headers)
    assert resp.status_code == 200, resp.text
    by_key = {(b["tenant_id"], b["name"]): b for b in resp.json()["bases"]}
    assert len(by_key) == len(resp.json()["bases"])  # no row collapsed
    # Both same-name rows surface with distinct tenant_ids.
    assert (str(_TENANT), "kb") in by_key
    assert (str(other_tenant), "kb") in by_key
    assert (str(other_tenant), "other-kb") in by_key
    # Stats stay attributed per base across the aggregate.
    assert by_key[(str(_TENANT), "kb")]["stats"]["document_count"] == 1
    assert by_key[(str(other_tenant), "kb")]["stats"]["document_count"] == 0
    assert by_key[(str(other_tenant), "other-kb")]["stats"]["document_count"] == 0


@pytest.mark.asyncio
async def test_knowledge_list_bases_star_truncated_flag(
    full_setup: FullSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W4 二轮 #3:聚合响应带顶层 ``truncated``——聚合页装满 cap 为 true,
    未装满为 false,非聚合分支恒 false。cap 通过 monkeypatch 端点模块引用的
    store 侧常量注入(端点用同一常量取页+算 flag,单源)。"""
    client, _runner, store = full_setup
    for i in range(3):
        await store.create_base(tenant_id=uuid4(), name=f"kb-{i}")
    headers = await _grant_system_admin(client)

    under = await client.get("/v1/knowledge/bases", params={"tenant_id": "*"}, headers=headers)
    assert under.status_code == 200, under.text
    assert under.json()["truncated"] is False  # 3 rows < default cap (200)

    monkeypatch.setattr("control_plane.api.knowledge.ALL_TENANTS_BASES_LIMIT", 2)
    capped = await client.get("/v1/knowledge/bases", params={"tenant_id": "*"}, headers=headers)
    assert capped.status_code == 200, capped.text
    assert len(capped.json()["bases"]) == 2  # the cap actually bounds the page
    assert capped.json()["truncated"] is True

    # Non-aggregate branch has no cap — never truncated.
    plain = await client.get("/v1/knowledge/bases")
    assert plain.status_code == 200, plain.text
    assert plain.json()["truncated"] is False


_KNOWLEDGE_SCOPE_GETS: list[tuple[str, str]] = [
    ("list_bases", "/v1/knowledge/bases"),
    ("get_base", "/v1/knowledge/bases/kb"),
    ("list_documents", "/v1/knowledge/bases/kb/documents"),
    (
        "list_chunks",
        "/v1/knowledge/bases/kb/documents/00000000-0000-0000-0000-000000000001/chunks",
    ),
]


@pytest.mark.parametrize("name,path", _KNOWLEDGE_SCOPE_GETS)
@pytest.mark.asyncio
async def test_knowledge_scope_foreign_tenant_user_403(setup: Setup, name: str, path: str) -> None:
    client, _ = setup
    foreign = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}
    resp = await client.get(path, params={"tenant_id": str(_TENANT)}, headers=foreign)
    assert resp.status_code == 403, f"{name}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"]["code"] == "TENANT_NOT_ALLOWED", name


@pytest.mark.asyncio
async def test_knowledge_test_retrieval_foreign_tenant_user_403(setup: Setup) -> None:
    client, _ = setup
    foreign = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}
    resp = await client.post(
        "/v1/knowledge/bases/kb/test",
        params={"tenant_id": str(_TENANT)},
        headers=foreign,
        json={"query": "x"},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "TENANT_NOT_ALLOWED"


_KNOWLEDGE_DETAIL_GETS = [p for p in _KNOWLEDGE_SCOPE_GETS if p[0] != "list_bases"]


@pytest.mark.parametrize("name,path", _KNOWLEDGE_DETAIL_GETS)
@pytest.mark.asyncio
async def test_knowledge_detail_tenant_id_star_400(setup: Setup, name: str, path: str) -> None:
    resp = await setup[0].get(path, params={"tenant_id": "*"})
    assert resp.status_code == 400, f"{name}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"]["code"] == "SCOPE_ALL_NOT_SUPPORTED", name


@pytest.mark.asyncio
async def test_knowledge_test_retrieval_tenant_id_star_400(setup: Setup) -> None:
    resp = await setup[0].post(
        "/v1/knowledge/bases/kb/test", params={"tenant_id": "*"}, json={"query": "x"}
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"]["code"] == "SCOPE_ALL_NOT_SUPPORTED"


@pytest.mark.asyncio
async def test_retrieval_test_missing_base_404_wins_over_503(setup: Setup) -> None:
    """M-2 锁定:库不存在 → 404,即使 retriever 未配置(原错误优先级)。"""
    client, _ = setup
    resp = await client.post("/v1/knowledge/bases/ghost/test", json={"query": "x"})
    assert resp.status_code == 404, resp.text
