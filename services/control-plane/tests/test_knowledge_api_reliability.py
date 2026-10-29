"""L2-PR2 入库可靠性 —— API 侧:上传上限 413 / 同内容重传 200 unchanged / 分页 / 审计."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditQuery
from orchestrator.llm import FakeEmbedder
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt
from tests.knowledge_confirm import settle

_TENANT = DEFAULT_DEV_TENANT_ID
_MD = b"# Handbook\n\nThe deductible is 500 dollars per year."


def _settings(**overrides: object) -> Settings:
    return Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
        **overrides,  # type: ignore[arg-type]
    )


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT, subject='user-a')}"}


class _FakeEmbeddingConfig:
    async def effective_embedding_config(self) -> tuple[str, str] | None:
        return ("qwen", "text-embedding-v4")

    async def effective_rerank_config(self) -> tuple[str, str] | None:
        return None


class Harness:
    def __init__(
        self,
        client: AsyncClient,
        runner: KnowledgeIngestionRunner,
        audit: InMemoryAuditLogStore,
        store: InMemoryKnowledgeStore,
    ) -> None:
        self.client = client
        self.runner = runner
        self.audit = audit
        self.store = store

    async def upload(self, base: str, name: str, body: bytes) -> dict[str, object]:
        response = await self.client.post(
            f"/v1/knowledge/bases/{base}/documents", files={"file": (name, body, "text/markdown")}
        )
        assert response.status_code in (200, 202), response.text
        await settle(self.client, self.runner, base)
        data: dict[str, object] = response.json()
        data["_status"] = response.status_code
        return data

    async def audit_rows(self) -> list[tuple[str, str, str | None, str]]:
        page = await self.audit.query(AuditQuery(tenant_id=_TENANT, limit=100))
        # The audit page is newest-first; tests read it in event order.
        return [
            (str(e.action), e.resource_type, e.resource_id, e.actor_id)
            for e in reversed(page.entries)
            if str(e.action).startswith("knowledge:")
        ]


async def _harness(**settings_overrides: object) -> AsyncIterator[Harness]:
    store = InMemoryKnowledgeStore()
    embedder = FakeEmbedder()
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    audit_store = InMemoryAuditLogStore()
    app = create_app(
        settings=_settings(**settings_overrides),
        knowledge_repo=store,
        knowledge_ingestion_runner=runner,
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
    )
    app.state.platform_embedding_config_service = _FakeEmbeddingConfig()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://cp.test", headers=_headers()
    ) as client:
        yield Harness(client, runner, audit_store, store)


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    async for h in _harness():
        yield h


@pytest.fixture
async def small_cap() -> AsyncIterator[Harness]:
    async for h in _harness(knowledge_document_max_bytes=1024):
        yield h


# ---------------------------------------------------------------------------
# 上传上限
# ---------------------------------------------------------------------------


def test_default_upload_cap_is_50_mib() -> None:
    assert Settings().knowledge_document_max_bytes == 50 * 1024 * 1024


@pytest.mark.asyncio
async def test_upload_over_cap_returns_413_with_limit(small_cap: Harness) -> None:
    await small_cap.client.post("/v1/knowledge/bases", json={"name": "kb"})
    too_big = await small_cap.client.post(
        "/v1/knowledge/bases/kb/documents", files={"file": ("big.md", b"x" * 1025, "text/markdown")}
    )
    assert too_big.status_code == 413
    assert too_big.json()["detail"] == {"error": "file_too_large", "limit_bytes": 1024}
    docs = await small_cap.client.get("/v1/knowledge/bases/kb/documents")
    assert docs.json()["documents"] == []  # nothing recorded for the rejected upload

    at_cap = await small_cap.client.post(
        "/v1/knowledge/bases/kb/documents", files={"file": ("ok.md", b"x" * 1024, "text/markdown")}
    )
    assert at_cap.status_code == 202


# ---------------------------------------------------------------------------
# 同内容重传
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reupload_same_content_is_200_unchanged(harness: Harness) -> None:
    await harness.client.post("/v1/knowledge/bases", json={"name": "kb"})
    first = await harness.upload("kb", "h.md", _MD)
    assert first["_status"] == 202
    assert first["unchanged"] is False
    ready = await harness.client.get("/v1/knowledge/bases/kb/documents")
    before = ready.json()["documents"][0]
    assert before["status"] == "ready"
    first_doc = await harness.store.get_document(tenant_id=_TENANT, document_id=UUID(before["id"]))
    assert first_doc is not None

    again = await harness.upload("kb", "h.md", _MD)
    assert again["_status"] == 200
    assert again["unchanged"] is True
    assert again["status"] == "ready"
    after = (await harness.client.get("/v1/knowledge/bases/kb/documents")).json()["documents"][0]
    assert after["chunk_count"] == before["chunk_count"]
    assert after["attempts"] == before["attempts"]  # not re-ingested
    assert after["updated_at"] == before["updated_at"]

    changed = await harness.upload("kb", "h.md", _MD + b"\n\nMore.")
    assert changed["_status"] == 202
    assert changed["unchanged"] is False
    final = (await harness.client.get("/v1/knowledge/bases/kb/documents")).json()["documents"][0]
    assert final["status"] == "ready"
    doc = await harness.store.get_document(tenant_id=_TENANT, document_id=UUID(final["id"]))
    # A real re-ingest happened: estimated (one generation), confirmed (another).
    assert doc is not None and doc.ingest_generation == first_doc.ingest_generation + 2


@pytest.mark.asyncio
async def test_reingest_forces_a_new_generation(harness: Harness) -> None:
    await harness.client.post("/v1/knowledge/bases", json={"name": "kb"})
    uploaded = await harness.upload("kb", "h.md", _MD)
    document_id = UUID(str(uploaded["id"]))
    settled = await harness.store.get_document(tenant_id=_TENANT, document_id=document_id)
    assert settled is not None
    response = await harness.client.post(
        f"/v1/knowledge/bases/kb/documents/{uploaded['id']}/reingest"
    )
    assert response.status_code == 202
    await settle(harness.client, harness.runner)
    doc = await harness.store.get_document(tenant_id=_TENANT, document_id=document_id)
    assert doc is not None
    # The same bytes again: estimated (a generation) and confirmed (another).
    assert doc.ingest_generation == settled.ingest_generation + 2
    assert doc.status.value == "ready"


# ---------------------------------------------------------------------------
# 分页
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_documents_paginates(harness: Harness) -> None:
    await harness.client.post("/v1/knowledge/bases", json={"name": "kb"})
    for i in range(4):
        await harness.upload("kb", f"{i}.md", _MD)
    # No ``limit`` → the whole list (the admin UI still pages client-side).
    everything = (await harness.client.get("/v1/knowledge/bases/kb/documents")).json()
    assert everything["total"] == 4
    assert everything["limit"] is None and everything["offset"] == 0
    assert len(everything["documents"]) == 4

    page = (await harness.client.get("/v1/knowledge/bases/kb/documents?limit=2&offset=2")).json()
    assert page["total"] == 4
    assert [d["id"] for d in page["documents"]] == [d["id"] for d in everything["documents"][2:4]]
    assert page["limit"] == 2 and page["offset"] == 2

    clamped = (
        await harness.client.get("/v1/knowledge/bases/kb/documents?limit=500&offset=-3")
    ).json()
    assert clamped["limit"] == 200 and clamped["offset"] == 0


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_write_endpoint_emits_an_audit_row(harness: Harness) -> None:
    created = await harness.client.post("/v1/knowledge/bases", json={"name": "kb"})
    base_id = created.json()["id"]
    patched = await harness.client.patch("/v1/knowledge/bases/kb", json={"description": "d"})
    assert patched.status_code == 200
    uploaded = await harness.upload("kb", "h.md", _MD)
    doc_id = str(uploaded["id"])
    reingested = await harness.client.post(f"/v1/knowledge/bases/kb/documents/{doc_id}/reingest")
    assert reingested.status_code == 202
    await settle(harness.client, harness.runner)
    reindexed = await harness.client.post("/v1/knowledge/bases/kb/reindex")
    assert reindexed.status_code == 202
    await harness.runner.drain()
    doc_deleted = await harness.client.delete(f"/v1/knowledge/bases/kb/documents/{doc_id}")
    assert doc_deleted.status_code == 204
    base_deleted = await harness.client.delete("/v1/knowledge/bases/kb")
    assert base_deleted.status_code == 204

    rows = await harness.audit_rows()
    assert rows == [
        ("knowledge:base_create", "knowledge_base", base_id, "user-a"),
        ("knowledge:base_update", "knowledge_base", base_id, "user-a"),
        ("knowledge:document_upload", "knowledge_document", doc_id, "user-a"),
        # B-222 PR-2: every paid ingest is confirmed first, and that is audited.
        ("knowledge:document_confirm", "knowledge_document", doc_id, "user-a"),
        ("knowledge:document_reingest", "knowledge_document", doc_id, "user-a"),
        ("knowledge:document_confirm", "knowledge_document", doc_id, "user-a"),
        ("knowledge:base_reindex", "knowledge_base", base_id, "user-a"),
        ("knowledge:document_delete", "knowledge_document", doc_id, "user-a"),
        ("knowledge:base_delete", "knowledge_base", base_id, "user-a"),
    ]


@pytest.mark.asyncio
async def test_rejected_upload_emits_no_audit_row(small_cap: Harness) -> None:
    await small_cap.client.post("/v1/knowledge/bases", json={"name": "kb"})
    await small_cap.client.post(
        "/v1/knowledge/bases/kb/documents", files={"file": ("big.md", b"x" * 2048, "text/markdown")}
    )
    assert [r[0] for r in await small_cap.audit_rows()] == ["knowledge:base_create"]


# ---------------------------------------------------------------------------
# 重传失败:上一版继续服务 / 丢弃新版 / 重试新版
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_reupload_keeps_previous_version_serving(harness: Harness) -> None:
    await harness.client.post("/v1/knowledge/bases", json={"name": "kb"})
    first = await harness.upload("kb", "h.md", _MD)
    doc_id = UUID(str(first["id"]))
    # A bad v2 (parses to nothing) → failed, but v1's chunks still serve.
    bad = await harness.upload("kb", "h.md", b"   ")
    assert bad["_status"] == 202
    row = (await harness.client.get("/v1/knowledge/bases/kb/documents")).json()["documents"][0]
    assert row["status"] == "failed"
    assert row["has_pending"] is True
    assert row["chunk_count"] >= 1
    _, serving = await harness.store.list_chunks(tenant_id=_TENANT, document_id=doc_id)
    assert serving >= 1
    assert await harness.store.get_document_content(tenant_id=_TENANT, document_id=doc_id) == _MD

    # Retry the pending v2 → still bad → still failed, still serving v1.
    retried = await harness.client.post(f"/v1/knowledge/bases/kb/documents/{doc_id}/reingest")
    assert retried.status_code == 202
    await settle(harness.client, harness.runner)
    row = (await harness.client.get("/v1/knowledge/bases/kb/documents")).json()["documents"][0]
    assert row["status"] == "failed" and row["has_pending"] is True

    # Discard v2 → ready on v1.
    discarded = await harness.client.post(
        f"/v1/knowledge/bases/kb/documents/{doc_id}/discard-pending"
    )
    assert discarded.status_code == 200
    assert discarded.json()["status"] == "ready"
    assert discarded.json()["has_pending"] is False
    again = await harness.client.post(f"/v1/knowledge/bases/kb/documents/{doc_id}/discard-pending")
    assert again.status_code == 409
    rows = await harness.audit_rows()
    assert rows[-1][0] == "knowledge:document_discard_pending"
    assert rows[-1][2] == str(doc_id)

    # A good v2 later promotes: serving bytes become v2.
    good = await harness.upload("kb", "h.md", _MD + b"\n\nMore.")
    assert good["_status"] == 202
    row = (await harness.client.get("/v1/knowledge/bases/kb/documents")).json()["documents"][0]
    assert row["status"] == "ready" and row["has_pending"] is False
    assert (
        await harness.store.get_document_content(tenant_id=_TENANT, document_id=doc_id)
        == _MD + b"\n\nMore."
    )


@pytest.mark.asyncio
async def test_discard_pending_unknown_document_404(harness: Harness) -> None:
    await harness.client.post("/v1/knowledge/bases", json={"name": "kb"})
    response = await harness.client.post(
        f"/v1/knowledge/bases/kb/documents/{UUID(int=1)}/discard-pending"
    )
    assert response.status_code == 404
