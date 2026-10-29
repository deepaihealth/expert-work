"""The retrieval-test endpoint reports B-206 K-22 / K-21② on every search:
``keyword_degraded`` (the keyword path failed, vector results only) and
``duplicates_dropped`` (passages left out as a copy of a better-ranked one)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import InMemoryKnowledgeStore, KnowledgeRecallError
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import ScoredChunk
from orchestrator.llm import FakeEmbedder
from orchestrator.tools import KnowledgeRetriever
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)
from tests.knowledge_confirm import settle

_BODY = b"# H\n\nThe deductible is 500 dollars."


class _KeywordTimingOutStore(InMemoryKnowledgeStore):
    async def keyword_search_scored(
        self, *, tenant_id: UUID, kb_ids: Sequence[UUID], query: str, limit: int = 5
    ) -> list[ScoredChunk]:
        raise KnowledgeRecallError("canceling statement due to statement timeout")


@asynccontextmanager
async def _client(
    store: InMemoryKnowledgeStore, files: Sequence[str]
) -> AsyncIterator[AsyncClient]:
    """A control plane over ``store`` with base ``kb`` holding ``files``, each
    with the same body."""
    embedder = FakeEmbedder()
    runner = KnowledgeIngestionRunner(store=store, embedder=embedder)
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
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    app.state.knowledge_retriever = KnowledgeRetriever(store=store, embedder=embedder)
    token = make_test_jwt(tenant_id=DEFAULT_DEV_TENANT_ID, subject="user-a")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://cp.test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        await client.post("/v1/knowledge/bases", json={"name": "kb"})
        for name in files:
            await client.post(
                "/v1/knowledge/bases/kb/documents",
                files={"file": (name, _BODY, "text/markdown")},
            )
        await runner.drain()
        # B-222 PR-2 (K-21 ①): a copy is held back on upload — 「仍入库」 it.
        listed = (await client.get("/v1/knowledge/bases/kb/documents")).json()["documents"]
        for document in listed:
            if (document["ingest_estimate"] or {}).get("duplicate_of"):
                resp = await client.post(
                    f"/v1/knowledge/bases/kb/documents/{document['id']}/estimate"
                )
                assert resp.status_code == 202, resp.text
        await settle(client, runner)
        yield client


@pytest.mark.asyncio
async def test_a_healthy_search_reports_both_flags_unset() -> None:
    async with _client(InMemoryKnowledgeStore(), ["h.md"]) as client:
        body = (
            await client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})
        ).json()
    assert body["keyword_degraded"] is False
    assert body["duplicates_dropped"] == 0


@pytest.mark.asyncio
async def test_a_failed_keyword_path_is_reported_with_the_vector_results() -> None:
    async with _client(_KeywordTimingOutStore(), ["h.md"]) as client:
        resp = await client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["keyword_degraded"] is True
    assert body["count"] >= 1
    assert {r["recall_source"] for r in body["results"]} == {"vector"}


@pytest.mark.asyncio
async def test_identical_documents_under_two_names_show_once() -> None:
    async with _client(InMemoryKnowledgeStore(), ["a.md", "b.md"]) as client:
        body = (
            await client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})
        ).json()
    assert [r["content"] for r in body["results"]].count("The deductible is 500 dollars.") == 1
    assert body["duplicates_dropped"] >= 1
