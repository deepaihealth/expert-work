"""B-222 —— ``POST /v1/knowledge/bases/{name}/documents/cancel``: stop ingests in progress.

Per document: ``cancelled`` (was pending / processing), ``not_cancellable``
(already settled), ``not_found`` (no such document in this base). Each
cancelled document is audited and stopped at once if this pod is ingesting it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditQuery, DocumentStatus, IngestStage
from orchestrator.llm import FakeEmbedder
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt

_TENANT = DEFAULT_DEV_TENANT_ID


def _headers(*roles: str) -> dict[str, str]:
    token = make_test_jwt(tenant_id=_TENANT, subject="user-a", roles=roles or ("admin",))
    return {"Authorization": f"Bearer {token}"}


class _SpyRunner(KnowledgeIngestionRunner):
    """Ingests nothing (documents stay where the test put them); records ``cancel_local``."""

    def __init__(self, store: InMemoryKnowledgeStore) -> None:
        super().__init__(store=store, embedder=FakeEmbedder())
        self.cancelled_local: list[list[UUID]] = []

    def cancel_local(self, document_ids: Collection[UUID]) -> int:
        self.cancelled_local.append(list(document_ids))
        return 0


class _Harness:
    def __init__(
        self,
        client: AsyncClient,
        store: InMemoryKnowledgeStore,
        runner: _SpyRunner,
        audit: InMemoryAuditLogStore,
    ) -> None:
        self.client = client
        self.store = store
        self.runner = runner
        self.audit = audit

    async def audit_rows(self) -> list[tuple[str, str | None]]:
        page = await self.audit.query(AuditQuery(tenant_id=_TENANT, limit=100))
        return [
            (str(e.action), e.resource_id)
            for e in reversed(page.entries)
            if str(e.action) == "knowledge:document_cancel"
        ]


@pytest.fixture
async def harness() -> AsyncIterator[_Harness]:
    async for built in _harness(with_runner=True):
        yield built


@pytest.fixture
async def no_runner() -> AsyncIterator[_Harness]:
    """No ingestion runner configured (no embedding model): nothing ingests
    on this pod, but documents can still be cancelled."""
    async for built in _harness(with_runner=False):
        yield built


async def _harness(*, with_runner: bool) -> AsyncIterator[_Harness]:
    store = InMemoryKnowledgeStore()
    runner = _SpyRunner(store)
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
        knowledge_ingestion_runner=runner if with_runner else None,
        audit_logger=build_default_audit_logger(audit),
        jwt_verifier=build_test_jwt_verifier(),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://cp.test", headers=_headers()
    ) as client:
        yield _Harness(client, store, runner, audit)


async def _upload(store: InMemoryKnowledgeStore, kb_id: UUID, filename: str) -> UUID:
    document = await store.upsert_document(
        tenant_id=_TENANT, kb_id=kb_id, filename=filename, content=b"# T\n\nbody"
    )
    return document.id


async def _claim(store: InMemoryKnowledgeStore, document_id: UUID) -> int:
    claim = await store.claim_document(
        tenant_id=_TENANT,
        document_id=document_id,
        now=datetime.now(UTC),
        lease_seconds=300,
        max_attempts=5,
    )
    assert claim is not None
    return claim.generation


async def test_cancel_reports_each_document_and_audits_the_cancelled(harness: _Harness) -> None:
    store = harness.store
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    other = await store.create_base(tenant_id=_TENANT, name="other")
    pending = await _upload(store, base.id, "pending.md")
    ready = await _upload(store, base.id, "ready.md")
    ready_claim = await store.claim_document(
        tenant_id=_TENANT,
        document_id=ready,
        now=datetime.now(UTC),
        lease_seconds=300,
        max_attempts=5,
    )
    assert ready_claim is not None
    await store.set_document_status(
        tenant_id=_TENANT,
        document_id=ready,
        status=DocumentStatus.READY,
        chunk_count=0,
        generation=ready_claim.generation,
    )
    elsewhere = await _upload(store, other.id, "elsewhere.md")
    missing = uuid4()

    response = await harness.client.post(
        "/v1/knowledge/bases/kb/documents/cancel",
        json={"document_ids": [str(pending), str(ready), str(elsewhere), str(missing)]},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "results": [
            {"document_id": str(pending), "outcome": "cancelled"},
            {"document_id": str(ready), "outcome": "not_cancellable"},
            {"document_id": str(elsewhere), "outcome": "not_found"},
            {"document_id": str(missing), "outcome": "not_found"},
        ]
    }
    cancelled = await store.get_document(tenant_id=_TENANT, document_id=pending)
    assert cancelled is not None and cancelled.status is DocumentStatus.CANCELLED
    untouched = await store.get_document(tenant_id=_TENANT, document_id=elsewhere)
    assert untouched is not None and untouched.status is DocumentStatus.PENDING
    assert await harness.audit_rows() == [("knowledge:document_cancel", str(pending))]
    assert harness.runner.cancelled_local == [[pending]]


async def test_the_document_list_shows_cancelled(harness: _Harness) -> None:
    base = await harness.store.create_base(tenant_id=_TENANT, name="kb")
    document = await _upload(harness.store, base.id, "a.md")
    await harness.client.post(
        "/v1/knowledge/bases/kb/documents/cancel", json={"document_ids": [str(document)]}
    )

    listed = await harness.client.get("/v1/knowledge/bases/kb/documents")

    [row] = listed.json()["documents"]
    assert (row["status"], row["error"]) == ("cancelled", "cancelled by user")


async def test_cancel_in_a_missing_base_is_404(harness: _Harness) -> None:
    response = await harness.client.post(
        "/v1/knowledge/bases/nope/documents/cancel", json={"document_ids": [str(uuid4())]}
    )
    assert response.status_code == 404


@pytest.mark.parametrize("ids", [[], ["not-a-uuid"], [str(uuid4()) for _ in range(201)]])
async def test_cancel_rejects_a_bad_body(harness: _Harness, ids: list[str]) -> None:
    await harness.store.create_base(tenant_id=_TENANT, name="kb")
    response = await harness.client.post(
        "/v1/knowledge/bases/kb/documents/cancel", json={"document_ids": ids}
    )
    assert response.status_code == 422


async def test_a_viewer_cannot_cancel(harness: _Harness) -> None:
    base = await harness.store.create_base(tenant_id=_TENANT, name="kb")
    document = await _upload(harness.store, base.id, "a.md")

    response = await harness.client.post(
        "/v1/knowledge/bases/kb/documents/cancel",
        json={"document_ids": [str(document)]},
        headers=_headers("viewer"),
    )

    assert response.status_code == 403
    kept = await harness.store.get_document(tenant_id=_TENANT, document_id=document)
    assert kept is not None and kept.status is DocumentStatus.PENDING
    assert harness.runner.cancelled_local == []


async def test_cancel_works_without_an_ingestion_runner(no_runner: _Harness) -> None:
    base = await no_runner.store.create_base(tenant_id=_TENANT, name="kb")
    document = await _upload(no_runner.store, base.id, "a.md")

    response = await no_runner.client.post(
        "/v1/knowledge/bases/kb/documents/cancel", json={"document_ids": [str(document)]}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"results": [{"document_id": str(document), "outcome": "cancelled"}]}
    assert await no_runner.audit_rows() == [("knowledge:document_cancel", str(document))]


async def test_discard_pending_while_its_chunks_are_being_written_is_409(
    harness: _Harness,
) -> None:
    """Past the point of no return (processing at indexing) the new version is
    about to serve: dropping it is refused like a cancel is."""
    store = harness.store
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    document = await _upload(store, base.id, "a.md")
    first = await _claim(store, document)
    assert await store.set_document_status(
        tenant_id=_TENANT,
        document_id=document,
        status=DocumentStatus.READY,
        chunk_count=1,
        generation=first,
    )
    await store.upsert_document(
        tenant_id=_TENANT, kb_id=base.id, filename="a.md", content=b"# T\n\nversion two"
    )
    second = await _claim(store, document)
    assert await store.set_document_stage(
        tenant_id=_TENANT, document_id=document, stage=IngestStage.INDEXING, generation=second
    )

    response = await harness.client.post(
        f"/v1/knowledge/bases/kb/documents/{document}/discard-pending"
    )

    assert response.status_code == 409, response.text
    kept = await store.get_document(tenant_id=_TENANT, document_id=document)
    assert kept is not None
    assert (kept.status, kept.has_pending) == (DocumentStatus.PROCESSING, True)
