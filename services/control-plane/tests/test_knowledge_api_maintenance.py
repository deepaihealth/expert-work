"""B-175 P3-PR2 —— document maintenance on ``/v1/knowledge``.

* ``PATCH …/documents/{id}`` switches a document off / on: switched off it is
  out of retrieval (``/test``, an agent's search) but listed, with its chunks;
* ``POST …/documents/batch`` enables / disables / re-ingests many at once and
  says what it did and what it skipped (and why); ``POST
  …/documents/batch-delete`` deletes many — ``manifest:delete``, so an
  operator gets 403;
* ``GET …/documents?enabled=&status=`` filters the list;
* ``GET /bases/{n}/stale-documents`` counts the documents cut by other
  chunking parameters; ``POST /bases/{n}/reingest-stale`` re-ingests those
  that still have their file;
* every write is audited.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.api import knowledge_maintenance
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.knowledge.chunking import PARENT_MAX_TOKENS
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditQuery, DocumentStatus
from orchestrator.llm import FakeEmbedder
from orchestrator.tools import KnowledgeRetriever
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt

_TENANT = DEFAULT_DEV_TENANT_ID


def _headers(role: str = "admin") -> dict[str, str]:
    token = make_test_jwt(tenant_id=_TENANT, subject="user-a", roles=(role,))
    return {"Authorization": f"Bearer {token}"}


@dataclass
class Harness:
    client: AsyncClient
    runner: KnowledgeIngestionRunner
    store: InMemoryKnowledgeStore
    audit: InMemoryAuditLogStore

    async def upload(self, name: str, text: str, base: str = "kb") -> str:
        response = await self.client.post(
            f"/v1/knowledge/bases/{base}/documents",
            files={"file": (name, text.encode(), "text/markdown")},
        )
        assert response.status_code == 202, response.text
        await self.runner.drain()
        return str(response.json()["id"])

    async def documents(self, **params: Any) -> list[dict[str, Any]]:
        response = await self.client.get("/v1/knowledge/bases/kb/documents", params=params)
        assert response.status_code == 200, response.text
        documents: list[dict[str, Any]] = response.json()["documents"]
        return documents

    async def hits(self, query: str) -> set[str]:
        response = await self.client.post("/v1/knowledge/bases/kb/test", json={"query": query})
        assert response.status_code == 200, response.text
        return {r["filename"] for r in response.json()["results"]}

    async def audit_rows(self) -> list[tuple[str, str, str | None, dict[str, Any]]]:
        page = await self.audit.query(AuditQuery(tenant_id=_TENANT, limit=100))
        return [
            (str(e.action), e.resource_type, e.resource_id, dict(e.details))
            for e in reversed(page.entries)
            if str(e.action).startswith("knowledge:")
        ]


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    store = InMemoryKnowledgeStore()
    embedder = FakeEmbedder()
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
    audit = InMemoryAuditLogStore()
    app = create_app(
        settings=Settings(
            env="dev",
            auth_mode="dev",
            rate_limit_burst=10_000,
            rate_limit_per_second=10_000.0,
            oidc_issuer=TEST_ISSUER,
            oidc_audience=[TEST_AUDIENCE],
        ),
        knowledge_repo=store,
        knowledge_ingestion_runner=runner,
        audit_logger=build_default_audit_logger(audit),
        jwt_verifier=build_test_jwt_verifier(),
    )
    app.state.knowledge_retriever = KnowledgeRetriever(store=store, embedder=embedder)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://cp.test", headers=_headers()
    ) as client:
        assert (await client.post("/v1/knowledge/bases", json={"name": "kb"})).status_code == 201
        yield Harness(client, runner, store, audit)


async def test_switching_a_document_off_takes_it_out_of_retrieval(harness: Harness) -> None:
    off = await harness.upload("off.md", "# Off\n\nThe deductible is 500 dollars.")
    await harness.upload("on.md", "# On\n\nThe deductible is 300 dollars.")
    assert await harness.hits("deductible") == {"off.md", "on.md"}

    response = await harness.client.patch(
        f"/v1/knowledge/bases/kb/documents/{off}", json={"enabled": False}
    )
    assert response.status_code == 200, response.text
    assert (response.json()["id"], response.json()["enabled"]) == (off, False)
    assert await harness.hits("deductible") == {"on.md"}
    # Still listed, its chunks kept.
    assert {d["id"]: d["enabled"] for d in await harness.documents()}[off] is False
    chunks = await harness.client.get(f"/v1/knowledge/bases/kb/documents/{off}/chunks")
    assert chunks.json()["total"] == 1

    back = await harness.client.patch(
        f"/v1/knowledge/bases/kb/documents/{off}", json={"enabled": True}
    )
    assert back.status_code == 200
    assert await harness.hits("deductible") == {"off.md", "on.md"}
    rows = await harness.audit_rows()
    toggles = [r for r in rows if r[0] == "knowledge:document_toggle"]
    assert [(r[1], r[2], r[3]["enabled"]) for r in toggles] == [
        ("knowledge_document", off, False),
        ("knowledge_document", off, True),
    ]


async def test_patch_document_needs_a_document_of_this_base(harness: Harness) -> None:
    await harness.client.post("/v1/knowledge/bases", json={"name": "other"})
    elsewhere = await harness.upload("x.md", "# X\n\ntext", base="other")
    for document_id in (elsewhere, str(uuid4())):
        response = await harness.client.patch(
            f"/v1/knowledge/bases/kb/documents/{document_id}", json={"enabled": False}
        )
        assert response.status_code == 404
    assert (
        await harness.client.patch(f"/v1/knowledge/bases/kb/documents/{elsewhere}", json={})
    ).status_code == 422


async def test_list_documents_filters_by_switch_and_status(harness: Harness) -> None:
    on = await harness.upload("a.md", "# A\n\ntext a")
    off = await harness.upload("b.md", "# B\n\ntext b")
    failed = await harness.upload("c.md", "   ")  # nothing to ingest: failed
    await harness.client.patch(f"/v1/knowledge/bases/kb/documents/{off}", json={"enabled": False})

    assert {d["id"] for d in await harness.documents(enabled="false")} == {off}
    assert {d["id"] for d in await harness.documents(enabled="true")} == {on, failed}
    assert {d["id"] for d in await harness.documents(status="ready")} == {on, off}
    assert {d["id"] for d in await harness.documents(status=["failed", "ready"])} == {
        on,
        off,
        failed,
    }
    paged = await harness.client.get(
        "/v1/knowledge/bases/kb/documents",
        params={"status": "ready", "enabled": "true", "limit": 1},
    )
    assert (paged.json()["total"], [d["id"] for d in paged.json()["documents"]]) == (1, [on])


async def test_batch_enable_and_disable_say_what_they_did(harness: Harness) -> None:
    a = await harness.upload("a.md", "# A\n\nThe deductible is 500 dollars.")
    b = await harness.upload("b.md", "# B\n\nThe deductible is 300 dollars.")
    await harness.client.post("/v1/knowledge/bases", json={"name": "other"})
    elsewhere = await harness.upload("x.md", "# X\n\ntext", base="other")
    missing = str(uuid4())

    response = await harness.client.post(
        "/v1/knowledge/bases/kb/documents/batch",
        json={"action": "disable", "document_ids": [a, missing, b, elsewhere]},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "action": "disable",
        "done": [a, b],
        "skipped": [
            {"id": missing, "reason": "not_found"},
            {"id": elsewhere, "reason": "not_found"},
        ],
    }
    assert await harness.hits("deductible") == set()
    enabled = await harness.client.post(
        "/v1/knowledge/bases/kb/documents/batch", json={"action": "enable", "document_ids": [a]}
    )
    assert enabled.json()["done"] == [a]
    assert await harness.hits("deductible") == {"a.md"}
    rows = [r for r in await harness.audit_rows() if r[0] == "knowledge:document_batch"]
    assert [(r[1], r[3]["action"], r[3]["done"], r[3]["skipped"]) for r in rows] == [
        ("knowledge_base", "disable", 2, 2),
        ("knowledge_base", "enable", 1, 0),
    ]
    # One row per document switched, so the trail says which.
    toggles = [r for r in await harness.audit_rows() if r[0] == "knowledge:document_toggle"]
    assert [(r[1], r[2], r[3]["enabled"], r[3]["batch"]) for r in toggles] == [
        ("knowledge_document", a, False, True),
        ("knowledge_document", b, False, True),
        ("knowledge_document", a, True, True),
    ]


async def test_batch_reingest_skips_documents_without_their_file(harness: Harness) -> None:
    a = await harness.upload("a.md", "# A\n\ntext a")
    base = await harness.store.get_base(tenant_id=_TENANT, name="kb")
    assert base is not None
    legacy = await harness.store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="legacy.md"
    )
    response = await harness.client.post(
        "/v1/knowledge/bases/kb/documents/batch",
        json={"action": "reingest", "document_ids": [a, str(legacy.id)]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["done"] == [a]
    assert response.json()["skipped"] == [{"id": str(legacy.id), "reason": "content_missing"}]
    await harness.runner.drain()
    document = await harness.store.get_document(tenant_id=_TENANT, document_id=UUID(a))
    assert document is not None and document.ingest_generation == 1
    rows = [r for r in await harness.audit_rows() if r[0] == "knowledge:document_reingest"]
    assert [(r[2], r[3]["filename"], r[3]["batch"]) for r in rows] == [(a, "a.md", True)]


async def test_batch_delete_needs_the_delete_permission(harness: Harness) -> None:
    a = await harness.upload("a.md", "# A\n\ntext a")
    b = await harness.upload("b.md", "# B\n\ntext b")
    body = {"document_ids": [a, b, str(uuid4())]}
    denied = await harness.client.post(
        "/v1/knowledge/bases/kb/documents/batch-delete", json=body, headers=_headers("operator")
    )
    assert denied.status_code == 403
    assert len(await harness.documents()) == 2
    # An operator may still switch documents (manifest:write).
    allowed = await harness.client.post(
        "/v1/knowledge/bases/kb/documents/batch",
        json={"action": "disable", "document_ids": [a]},
        headers=_headers("operator"),
    )
    assert allowed.status_code == 200

    deleted = await harness.client.post("/v1/knowledge/bases/kb/documents/batch-delete", json=body)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["done"] == [a, b]
    assert deleted.json()["skipped"] == [{"id": body["document_ids"][2], "reason": "not_found"}]
    assert await harness.documents() == []
    rows = [r for r in await harness.audit_rows() if r[0] == "knowledge:document_batch"]
    assert rows[-1][3] == {"kb": "kb", "action": "delete", "done": 2, "skipped": 1}
    # One delete row per document, naming it.
    deletes = [r for r in await harness.audit_rows() if r[0] == "knowledge:document_delete"]
    assert [(r[1], r[2], r[3]) for r in deletes] == [
        ("knowledge_document", a, {"kb": "kb", "filename": "a.md", "batch": True}),
        ("knowledge_document", b, {"kb": "kb", "filename": "b.md", "batch": True}),
    ]


async def test_batch_size_is_bounded(harness: Harness) -> None:
    ids = [str(uuid4()) for _ in range(201)]
    for path, body in (
        ("batch", {"action": "disable", "document_ids": ids}),
        ("batch-delete", {"document_ids": ids}),
        ("batch", {"action": "disable", "document_ids": []}),
        ("batch", {"action": "purge", "document_ids": ids[:1]}),
    ):
        response = await harness.client.post(f"/v1/knowledge/bases/kb/documents/{path}", json=body)
        assert response.status_code == 422, (path, body)


async def test_stale_documents_are_counted_and_reingested(harness: Harness) -> None:
    a = await harness.upload("a.md", "# A\n\ntext a")
    b = await harness.upload("b.md", "# B\n\ntext b")
    stale = await harness.client.get("/v1/knowledge/bases/kb/stale-documents")
    assert stale.status_code == 200, stale.text
    assert stale.json()["stale"] == 0
    # A ready document from before chunking was recorded, its file not kept:
    # stale, and it cannot be re-ingested.
    base = await harness.store.get_base(tenant_id=_TENANT, name="kb")
    assert base is not None
    legacy = await harness.store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="legacy.md"
    )
    await harness.store.set_document_status(
        tenant_id=_TENANT, document_id=legacy.id, status=DocumentStatus.READY
    )
    assert (await harness.client.get("/v1/knowledge/bases/kb/stale-documents")).json()["stale"] == 1
    # The base's chunking changes: every ready document is now stale.
    patched = await harness.client.patch("/v1/knowledge/bases/kb", json={"chunk_max_tokens": 300})
    assert patched.status_code == 200
    stale = await harness.client.get("/v1/knowledge/bases/kb/stale-documents")
    assert stale.json() == {"stale": 3, "chunk_params_hash": patched.json()["chunk_params_hash"]}

    queued = await harness.client.post("/v1/knowledge/bases/kb/reingest-stale")
    assert queued.status_code == 202, queued.text
    assert queued.json() == {
        "queued": [b, a],
        "skipped": [{"id": str(legacy.id), "reason": "content_missing"}],
        "remaining": 0,
    }
    await harness.runner.drain()
    stale = await harness.client.get("/v1/knowledge/bases/kb/stale-documents")
    assert stale.json()["stale"] == 1
    rows = [r for r in await harness.audit_rows() if r[0] == "knowledge:base_reingest"]
    assert [(r[1], r[2], r[3]) for r in rows] == [
        ("knowledge_base", str(base.id), {"kb": "kb", "queued": 2, "skipped": 1, "remaining": 0})
    ]
    reingests = [r for r in await harness.audit_rows() if r[0] == "knowledge:document_reingest"]
    assert [(r[2], r[3]["stale"]) for r in reingests] == [(b, True), (a, True)]


async def test_reingest_stale_works_through_at_most_a_cap_per_call(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(knowledge_maintenance, "REINGEST_STALE_MAX", 1)
    a = await harness.upload("a.md", "# A\n\ntext a")
    b = await harness.upload("b.md", "# B\n\ntext b")
    await harness.client.patch("/v1/knowledge/bases/kb", json={"chunk_max_tokens": 300})

    first = await harness.client.post("/v1/knowledge/bases/kb/reingest-stale")
    assert first.json() == {"queued": [b], "skipped": [], "remaining": 1}
    await harness.runner.drain()
    second = await harness.client.post("/v1/knowledge/bases/kb/reingest-stale")
    assert second.json() == {"queued": [a], "skipped": [], "remaining": 0}


async def test_a_base_reports_its_passage_cap(harness: Harness) -> None:
    base = await harness.client.get("/v1/knowledge/bases/kb")
    assert base.json()["parent_max_tokens"] == PARENT_MAX_TOKENS
