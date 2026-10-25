"""Tests for ``/v1/knowledge`` — Stream J.5 knowledge-base + document API."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.knowledge.chunking import chunk_params_hash
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import ChunkType, DocumentStatus, KnowledgeChunk
from orchestrator.llm import FakeEmbedder
from orchestrator.tools import KnowledgeRetriever
from orchestrator.tools.knowledge import RerankRanking
from orchestrator.tools.knowledge_rerank import RerankUnavailableError
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    grant_system_admin,
    make_test_jwt,
)

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

    async def effective_rerank_config(self) -> tuple[str, str] | None:
        return None


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
        json={"name": "bad", "chunk_max_tokens": 200, "chunk_overlap_tokens": 200},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_base_defaults_to_400_tokens_without_overlap(setup: Setup) -> None:
    # B-206 K-3: the platform defaults; 0 overlap is stored as 0, not swapped for a default.
    client, _ = setup
    resp = await client.post("/v1/knowledge/bases", json={"name": "plain"})
    assert resp.status_code == 201
    assert (resp.json()["chunk_max_tokens"], resp.json()["chunk_overlap_tokens"]) == (400, 0)


@pytest.mark.asyncio
async def test_a_base_reports_the_chunking_fingerprint_of_its_parameters(setup: Setup) -> None:
    # B-206 K-3: a document whose chunk_params_hash differs is an old version.
    client, _ = setup
    created = (await client.post("/v1/knowledge/bases", json={"name": "kb"})).json()
    assert created["chunk_params_hash"] == chunk_params_hash(max_tokens=400, overlap_tokens=0)
    await client.patch("/v1/knowledge/bases/kb", json={"chunk_max_tokens": 256})
    fetched = (await client.get("/v1/knowledge/bases/kb")).json()
    assert fetched["chunk_params_hash"] == chunk_params_hash(max_tokens=256, overlap_tokens=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("max_tokens", [127, 1025])
async def test_chunk_size_outside_128_to_1024_is_rejected(setup: Setup, max_tokens: int) -> None:
    client, _ = setup
    created = await client.post(
        "/v1/knowledge/bases", json={"name": "kb", "chunk_max_tokens": max_tokens}
    )
    assert created.status_code == 422
    await client.post("/v1/knowledge/bases", json={"name": "ok"})
    patched = await client.patch("/v1/knowledge/bases/ok", json={"chunk_max_tokens": max_tokens})
    assert patched.status_code == 422


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
        "rerank_score_threshold": None,
        # B-206 K-7b — no threshold, nothing bound; this app configures no
        # platform rerank model.
        "rerank_threshold_model": None,
        "rerank_threshold_paused": False,
        "current_rerank_model": None,
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
async def test_list_bases_paginates(setup: Setup) -> None:
    client, runner = setup
    for i in range(4):
        await client.post("/v1/knowledge/bases", json={"name": f"kb-{i}"})
    await client.post(
        "/v1/knowledge/bases/kb-0/documents",
        files={"file": ("d.md", b"# D\n\nbody text here.", "text/markdown")},
    )
    await runner.drain()
    # No ``limit`` → the whole list (the knowledge picker still reads all).
    everything = (await client.get("/v1/knowledge/bases")).json()
    assert everything["total"] == 4
    assert everything["limit"] is None and everything["offset"] == 0
    assert len(everything["bases"]) == 4

    page = (await client.get("/v1/knowledge/bases?limit=2&offset=2")).json()
    assert page["total"] == 4
    assert [b["name"] for b in page["bases"]] == [b["name"] for b in everything["bases"][2:4]]
    assert page["limit"] == 2 and page["offset"] == 2
    assert page["truncated"] is False
    # Stats still attach on a page that holds the base with the document.
    by_name = {b["name"]: b for b in everything["bases"]}
    assert by_name["kb-0"]["stats"]["document_count"] == 1
    tail = (await client.get("/v1/knowledge/bases?limit=2&offset=0")).json()["bases"]
    tail += (await client.get("/v1/knowledge/bases?limit=2&offset=2")).json()["bases"]
    assert next(b for b in tail if b["name"] == "kb-0")["stats"]["document_count"] == 1

    clamped = (await client.get("/v1/knowledge/bases?limit=500&offset=-3")).json()
    assert clamped["limit"] == 200 and clamped["offset"] == 0


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


# ---------------------------------------------------------------------------
# B-206 K-1 — parent rows in the segment preview and the hit-test
# ---------------------------------------------------------------------------


async def _seed_parented(
    client: AsyncClient,
    runner: KnowledgeIngestionRunner,
    store: InMemoryKnowledgeStore,
    children: Sequence[tuple[int | None, str]],
) -> tuple[str, list[KnowledgeChunk]]:
    """Base ``kb`` with one ready document ``h.md`` whose chunks are ``children``
    — child ``i`` has chunk index ``i`` and heading ``("Part", "child i")``, and
    belongs to the parent row its pair names (``None`` = no parent row). Parent
    ``p`` (chunk index ``p``, heading ``("Part p",)``) holds its children's bodies
    joined by a blank line. Returns the document id and the parent rows."""
    doc_id = await _seed_kb_with_document(client, runner)
    document = await store.get_document(tenant_id=_TENANT, document_id=UUID(doc_id))
    assert document is not None

    def row(index: int, content: str, **fields: Any) -> KnowledgeChunk:
        return KnowledgeChunk(
            id=uuid4(),
            tenant_id=_TENANT,
            kb_id=document.kb_id,
            document_id=document.id,
            chunk_index=index,
            content=content,
            **fields,
        )

    parents = [
        row(
            p,
            "\n\n".join(body for ref, body in children if ref == p),
            embedding=None,
            chunk_type=ChunkType.PARENT,
            heading_path=(f"Part {p}",),
        )
        for p in range(1 + max(ref for ref, _ in children if ref is not None))
    ]
    vectors = await FakeEmbedder().embed([body for _, body in children], tenant_id=_TENANT)
    kids = [
        row(
            i,
            body,
            embedding=vector,
            heading_path=("Part", f"child {i}"),
            parent_chunk_id=None if ref is None else parents[ref].id,
        )
        for i, ((ref, body), vector) in enumerate(zip(children, vectors, strict=True))
    ]
    await store.replace_chunks(tenant_id=_TENANT, document_id=document.id, chunks=[*parents, *kids])
    return doc_id, parents


def _record_parent_reads(
    store: InMemoryKnowledgeStore, monkeypatch: pytest.MonkeyPatch
) -> list[list[UUID]]:
    """Every ``get_chunks`` call's ids, from now on."""
    reads: list[list[UUID]] = []
    read = store.get_chunks

    async def recording(*, tenant_id: UUID, chunk_ids: Sequence[UUID]) -> list[KnowledgeChunk]:
        reads.append(list(chunk_ids))
        return await read(tenant_id=tenant_id, chunk_ids=chunk_ids)

    monkeypatch.setattr(store, "get_chunks", recording)
    return reads


@pytest.mark.asyncio
async def test_list_chunks_names_each_chunks_parent_in_one_read(
    full_setup: FullSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default view lists the searchable chunks (never the parent rows), each
    with its parent row's own index — read once for the page's parents."""
    client, runner, store = full_setup
    doc_id, parents = await _seed_parented(
        client, runner, store, [(0, "alpha"), (0, "beta"), (1, "gamma"), (None, "delta")]
    )
    reads = _record_parent_reads(store, monkeypatch)
    url = f"/v1/knowledge/bases/kb/documents/{doc_id}/chunks"

    body = (await client.get(url)).json()

    assert body["total"] == 4
    assert [(c["chunk_index"], c["content"], c["chunk_type"]) for c in body["chunks"]] == [
        (0, "alpha", "text"),
        (1, "beta", "text"),
        (2, "gamma", "text"),
        (3, "delta", "text"),
    ]
    assert [c["parent_index"] for c in body["chunks"]] == [0, 0, 1, None]
    assert reads == [[parents[0].id, parents[1].id]]
    # A later page names its parents by their own index, not by their place on the page.
    page = (await client.get(url, params={"offset": 2, "limit": 2})).json()
    assert [c["parent_index"] for c in page["chunks"]] == [1, None]


@pytest.mark.asyncio
async def test_list_chunks_parents_view_lists_the_parent_rows(full_setup: FullSetup) -> None:
    client, runner, store = full_setup
    doc_id, _ = await _seed_parented(
        client, runner, store, [(0, "alpha"), (0, "beta"), (1, "gamma")]
    )
    url = f"/v1/knowledge/bases/kb/documents/{doc_id}/chunks"

    body = (await client.get(url, params={"view": "parents"})).json()

    assert body["total"] == 2
    assert [
        (c["chunk_index"], c["content"], c["heading_path"], c["chunk_type"], c["parent_index"])
        for c in body["chunks"]
    ] == [
        (0, "alpha\n\nbeta", ["Part 0"], "parent", None),
        (1, "gamma", ["Part 1"], "parent", None),
    ]
    page = (await client.get(url, params={"view": "parents", "offset": 1, "limit": 1})).json()
    assert ([c["content"] for c in page["chunks"]], page["total"]) == (["gamma"], 2)
    capped = (await client.get(url, params={"view": "parents", "limit": 500})).json()
    assert capped["limit"] == 200
    children = (await client.get(url, params={"view": "children"})).json()
    assert ([c["content"] for c in children["chunks"]], children["total"]) == (
        ["alpha", "beta", "gamma"],
        3,
    )


@pytest.mark.asyncio
async def test_list_chunks_of_a_document_without_parent_rows(
    full_setup: FullSetup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A document with no parent rows (ingested before them): the preview is as
    before plus ``parent_index: null``, nothing is read for parents, and the
    parents view is empty."""
    client, runner, store = full_setup
    doc_id = await _seed_kb_with_document(client, runner)
    reads = _record_parent_reads(store, monkeypatch)
    url = f"/v1/knowledge/bases/kb/documents/{doc_id}/chunks"

    body = (await client.get(url)).json()

    assert body["total"] >= 1
    assert set(body["chunks"][0]) == {
        "id",
        "chunk_index",
        "content",
        "heading_path",
        "chunk_type",
        "enabled",
        "edited",
        "revision",
        "parent_index",
    }
    assert [c["parent_index"] for c in body["chunks"]] == [None] * len(body["chunks"])
    assert reads == []
    parents = (await client.get(url, params={"view": "parents"})).json()
    assert (parents["chunks"], parents["total"]) == ([], 0)


@pytest.mark.asyncio
async def test_list_chunks_rejects_an_unknown_view(full_setup: FullSetup) -> None:
    client, runner, _ = full_setup
    doc_id = await _seed_kb_with_document(client, runner)
    resp = await client.get(
        f"/v1/knowledge/bases/kb/documents/{doc_id}/chunks", params={"view": "all"}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_retrieval_test_returns_parents_with_their_hit_children(
    full_setup: FullSetup,
) -> None:
    """A hit inside a parent comes back as the parent's text and index, naming its
    hit children (best first) and carrying the best one's own text; a hit with no
    parent row comes back as itself."""
    client, runner, store = full_setup
    await _seed_parented(
        client,
        runner,
        store,
        [
            (0, "The deductible is 500."),
            (0, "A second deductible applies abroad."),
            (None, "The deductible resets in January."),
            (1, "Claims close in 30 days."),
        ],
    )

    body = (
        await client.post(
            "/v1/knowledge/bases/kb/test", json={"query": "deductible", "method": "keyword"}
        )
    ).json()

    by_parent = {r["parent_index"]: r for r in body["results"]}
    assert set(by_parent) == {0, None}
    parent = by_parent[0]
    assert parent["content"] == "The deductible is 500.\n\nA second deductible applies abroad."
    assert parent["heading_path"] == ["Part 0"]
    assert sorted(parent["hit_chunk_indexes"]) == [0, 1]
    assert parent["hit_chunk_indexes"][0] == parent["chunk_index"]
    assert parent["source"] == f"h.md#{parent['chunk_index']}"
    best = parent["chunk_index"]
    assert (
        parent["hit_content"]
        == [
            "The deductible is 500.",
            "A second deductible applies abroad.",
        ][best]
    )
    assert parent["hit_heading_path"] == ["Part", f"child {best}"]
    own = by_parent[None]
    assert (own["content"], own["heading_path"], own["chunk_index"]) == (
        "The deductible resets in January.",
        ["Part", "child 2"],
        2,
    )
    assert (own["hit_chunk_indexes"], own["hit_content"], own["hit_heading_path"]) == (
        [2],
        None,
        [],
    )


@pytest.mark.asyncio
async def test_rerank_score_threshold_create_patch_clear(setup: Setup) -> None:
    """B-175 wave 2 PR3 — the per-base rerank-score threshold round-trips
    through create / PATCH, and an explicit null clears it."""
    client, _ = setup
    created = await client.post(
        "/v1/knowledge/bases", json={"name": "kb", "rerank_score_threshold": 0.7}
    )
    assert created.json()["retrieval_config"]["rerank_score_threshold"] == 0.7
    kept = await client.patch("/v1/knowledge/bases/kb", json={"retrieval_top_k": 9})
    assert kept.json()["retrieval_config"]["rerank_score_threshold"] == 0.7
    cleared = await client.patch("/v1/knowledge/bases/kb", json={"rerank_score_threshold": None})
    assert cleared.json()["retrieval_config"]["rerank_score_threshold"] is None
    bad = await client.patch("/v1/knowledge/bases/kb", json={"rerank_score_threshold": 1.5})
    assert bad.status_code == 422
    bad_create = await client.post(
        "/v1/knowledge/bases", json={"name": "kb2", "rerank_score_threshold": -0.1}
    )
    assert bad_create.status_code == 422


@pytest.mark.asyncio
async def test_retrieval_test_reports_how_the_search_ended(full_setup: FullSetup) -> None:
    client, runner, _ = full_setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    await client.post(
        "/v1/knowledge/bases/kb/documents",
        files={"file": ("h.md", b"# H\n\nThe deductible is 500 dollars.", "text/markdown")},
    )
    await runner.drain()
    body = (await client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})).json()
    assert body["status"] == "ok"
    assert body["candidates"] >= 1
    assert body["rerank_degraded"] is False
    assert body["rerank_dropped"] == 0
    assert "rerank_score" in body["results"][0]
    # B-206 K-7 — the base asks for rerank, this deployment has no reranker.
    assert body["rerank_outcome"] == "no_model"


class _RaisingReranker:
    async def rerank(self, **_: Any) -> list[int]:
        raise RuntimeError("dashscope rerank failed: 500")

    async def rerank_scored(self, **_: Any) -> list[tuple[int, float | None]]:
        raise RuntimeError("dashscope rerank failed: 500")


class _LowScoreReranker:
    async def rerank(self, **kwargs: Any) -> list[int]:
        return [i for i, _ in await self.rerank_scored(**kwargs)]

    async def rerank_scored(
        self, *, documents: Sequence[str], top_k: int, **_: Any
    ) -> list[tuple[int, float | None]]:
        return [(i, 0.2) for i in range(min(len(documents), top_k))]


class _CappedReranker:
    """Scores what it judges but leaves 3 documents of every call unjudged, like a
    model whose per-request cap is below the pool (B-206 K-14)."""

    async def rerank(self, **kwargs: Any) -> list[int]:
        return [i for i, _ in await self.rerank_scored(**kwargs)]

    async def rerank_scored(
        self, *, documents: Sequence[str], top_k: int, **_: Any
    ) -> list[tuple[int, float | None]]:
        return RerankRanking([(i, 0.5) for i in range(min(len(documents), top_k))], dropped=3)


@asynccontextmanager
async def _client_with_reranker(reranker: Any) -> AsyncIterator[AsyncClient]:
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
    app.state.knowledge_retriever = KnowledgeRetriever(
        store=store, embedder=embedder, reranker=reranker
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers()
    ) as client:
        await client.post("/v1/knowledge/bases", json={"name": "kb"})
        await client.post(
            "/v1/knowledge/bases/kb/documents",
            files={"file": ("h.md", b"# H\n\nThe deductible is 500 dollars.", "text/markdown")},
        )
        await runner.drain()
        yield client


class _QueryEmbedFailingEmbedder(FakeEmbedder):
    """Ingests fine; every search query fails to embed (B-206 K-8)."""

    async def embed_query(self, query: str, *, tenant_id: UUID) -> tuple[float, ...]:
        raise httpx.ConnectError("embedding endpoint unreachable")


@pytest.mark.asyncio
async def test_retrieval_test_reports_a_failed_query_embedding() -> None:
    """The hit-test shows keyword results and says the vector path was skipped,
    instead of passing them off as a hybrid search."""
    store = InMemoryKnowledgeStore()
    embedder = _QueryEmbedFailingEmbedder()
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    app = create_app(
        settings=_settings(),
        knowledge_repo=store,
        knowledge_ingestion_runner=runner,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    app.state.knowledge_retriever = KnowledgeRetriever(store=store, embedder=embedder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://cp.test", headers=_headers()
    ) as client:
        await client.post("/v1/knowledge/bases", json={"name": "kb"})
        await client.post(
            "/v1/knowledge/bases/kb/documents",
            files={"file": ("h.md", b"# H\n\nThe deductible is 500 dollars.", "text/markdown")},
        )
        await runner.drain()
        resp = await client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["embedding_degraded"] is True
    assert body["count"] >= 1
    assert {r["recall_source"] for r in body["results"]} == {"keyword"}


@pytest.mark.asyncio
async def test_retrieval_test_survives_a_failing_reranker() -> None:
    """A rerank vendor error used to be an unhandled 500 on the hit-test."""
    async with _client_with_reranker(_RaisingReranker()) as client:
        resp = await client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["rerank_degraded"] is True
    assert body["rerank_outcome"] == "model_error"
    assert body["count"] >= 1


class _RateLimitedReranker:
    async def rerank(self, **_: Any) -> list[int]:
        raise RerankUnavailableError("gave up", outcome="rate_limited")

    async def rerank_scored(self, **_: Any) -> list[tuple[int, float | None]]:
        raise RerankUnavailableError("gave up", outcome="rate_limited")


@pytest.mark.asyncio
async def test_retrieval_test_says_the_rerank_was_rate_limited() -> None:
    async with _client_with_reranker(_RateLimitedReranker()) as client:
        resp = await client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})
    body = resp.json()
    assert (body["rerank_outcome"], body["rerank_degraded"]) == ("rate_limited", True)
    assert body["count"] >= 1  # the fused order


@pytest.mark.asyncio
async def test_retrieval_test_reports_candidates_the_reranker_never_judged() -> None:
    async with _client_with_reranker(_CappedReranker()) as client:
        resp = await client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})
    body = resp.json()
    assert body["status"] == "ok"
    assert body["rerank_degraded"] is False
    assert body["rerank_outcome"] == "ok"
    assert body["rerank_dropped"] >= 3  # titled view, plus the body view when it runs


@pytest.mark.asyncio
async def test_retrieval_test_threshold_override_reports_below_threshold() -> None:
    async with _client_with_reranker(_LowScoreReranker()) as client:
        plain = await client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})
        resp = await client.post(
            "/v1/knowledge/bases/kb/test",
            json={"query": "deductible", "rerank_score_threshold": 0.5},
        )
    unfiltered = plain.json()
    assert unfiltered["status"] == "ok"
    assert [r["rerank_score"] for r in unfiltered["results"]] == pytest.approx(
        [0.2] * unfiltered["count"]
    )
    body = resp.json()
    assert body["status"] == "below_threshold"
    assert body["count"] == 0
    assert body["candidates"] >= 1


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
async def test_reingest_without_serving_bytes_redrives_the_pending_upload(
    full_setup: FullSetup,
) -> None:
    client, runner, store = full_setup
    await client.post("/v1/knowledge/bases", json={"name": "kb"})
    kb_id = UUID((await client.get("/v1/knowledge/bases/kb")).json()["id"])
    # A legacy document: chunks serving, original bytes never kept.
    doc = await store.upsert_document(tenant_id=_TENANT, kb_id=kb_id, filename="legacy.md")
    claim = await store.claim_document(
        tenant_id=_TENANT,
        document_id=doc.id,
        now=datetime.now(UTC),
        lease_seconds=60,
        max_attempts=5,
    )
    assert claim is not None and claim.content is None
    chunk = KnowledgeChunk(
        id=uuid4(),
        tenant_id=_TENANT,
        kb_id=kb_id,
        document_id=doc.id,
        chunk_index=0,
        content="old text",
        embedding=(1.0,),
    )
    assert await store.replace_chunks(
        tenant_id=_TENANT, document_id=doc.id, chunks=[chunk], generation=claim.generation
    )
    assert await store.set_document_status(
        tenant_id=_TENANT,
        document_id=doc.id,
        status=DocumentStatus.READY,
        chunk_count=1,
        generation=claim.generation,
    )
    # Its re-upload waits in the pending slot; re-ingest drives that one.
    await store.upsert_document(
        tenant_id=_TENANT,
        kb_id=kb_id,
        filename="legacy.md",
        content=b"# L\n\nNew text.",
        content_sha256="s",
    )
    resp = await client.post(f"/v1/knowledge/bases/kb/documents/{doc.id}/reingest")
    assert resp.status_code == 202
    await runner.drain()
    row = (await client.get("/v1/knowledge/bases/kb/documents")).json()["documents"][0]
    assert (row["status"], row["has_pending"]) == ("ready", False)


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
    headers = await grant_system_admin(client)
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

    headers = await grant_system_admin(client)
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
    headers = await grant_system_admin(client)

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


@pytest.mark.asyncio
async def test_knowledge_list_bases_star_paginates(full_setup: FullSetup) -> None:
    """``tenant_id=*`` + ``limit``:真分页,``total`` 是全租户总数,不打 truncated。"""
    client, _runner, store = full_setup
    for i in range(3):
        await store.create_base(tenant_id=uuid4(), name=f"kb-{i}")
    headers = await grant_system_admin(client)
    everything = await client.get("/v1/knowledge/bases", params={"tenant_id": "*"}, headers=headers)
    full = everything.json()["bases"]
    page = await client.get(
        "/v1/knowledge/bases",
        params={"tenant_id": "*", "limit": 2, "offset": 1},
        headers=headers,
    )
    assert page.status_code == 200, page.text
    body = page.json()
    assert body["total"] == len(full)
    assert [(b["tenant_id"], b["name"]) for b in body["bases"]] == [
        (b["tenant_id"], b["name"]) for b in full[1:3]
    ]
    assert body["cross_tenant"] is True and body["truncated"] is False


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
