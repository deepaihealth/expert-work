"""Console retrieval test (hit test) upgrade — B-175 P2-PR4 (B-206 §6).

Each result gains the four scores (vector / keyword / fused / rerank) and its
document (id, short-id number, title, filename, upload time); the response
gains ``elapsed_ms`` (the search, not its logging) and ``log_id``, the
``console_test`` row the search wrote under the employee's id, as the
retriever reports it back. Every key the response had before is still there.
A retriever that logs nothing, or a write that failed, reports ``log_id: null``
rather than an id that names no row. A result's document comes from its
document id — no chunk re-read. The responses (and the retrieval-log reads of
the row) carry every key the admin UI's shared fixtures name.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import InMemoryKnowledgeStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.knowledge import (
    InMemoryKnowledgeRetrievalLogStore,
    RetrievalLogEntry,
)
from orchestrator.llm import FakeEmbedder
from orchestrator.tools import KnowledgeRetriever
from orchestrator.tools.knowledge_log import LoggedKnowledgeRetriever
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    grant_system_admin,
    make_test_jwt,
)
from tests.test_knowledge_api_logs import _GUIDE
from tests.knowledge_confirm import settle

_TENANT = DEFAULT_DEV_TENANT_ID
_EMPLOYEE = "employee-hit-test"
_FIXTURES = Path(__file__).resolve().parents[3] / "apps/admin-ui/src/api/__fixtures__"
#: Keys the hit-test response carried before this change (result / top level).
_RESULT_KEYS_BEFORE = {
    "content",
    "heading_path",
    "source",
    "filename",
    "chunk_index",
    "score",
    "rerank_score",
    "recall_source",
    "parent_index",
    "hit_chunk_indexes",
    "hit_content",
    "hit_heading_path",
}
_TOP_KEYS_BEFORE = {
    "query",
    "results",
    "count",
    "status",
    "candidates",
    "rerank_degraded",
    "rerank_outcome",
    "embedding_degraded",
    "rerank_dropped",
    "rerank_threshold_paused",
    "rerank_threshold_model",
    "rerank_model",
}


class _Setup:
    def __init__(
        self,
        client: AsyncClient,
        store: InMemoryKnowledgeStore,
        log: InMemoryKnowledgeRetrievalLogStore,
        app: Any,
    ) -> None:
        self.client, self.store, self.log, self.app = client, store, log, app


@pytest.fixture
async def setup() -> AsyncIterator[_Setup]:
    store = InMemoryKnowledgeStore()
    log = InMemoryKnowledgeRetrievalLogStore()
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
        knowledge_retrieval_log_repo=log,
        knowledge_ingestion_runner=runner,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    app.state.knowledge_retriever = LoggedKnowledgeRetriever(
        store=store, embedder=embedder, log_sink=log
    )
    token = make_test_jwt(tenant_id=_TENANT, subject=_EMPLOYEE)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://cp.test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        await client.post("/v1/knowledge/bases", json={"name": "kb"})
        await client.post(
            "/v1/knowledge/bases/kb/documents",
            files={"file": ("h.md", b"# H\n\nThe deductible is 500 dollars.", "text/markdown")},
        )
        await settle(client, runner)
        yield _Setup(client, store, log, app)


@pytest.mark.asyncio
async def test_results_carry_four_scores_and_their_document(setup: _Setup) -> None:
    resp = await setup.client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) >= _TOP_KEYS_BEFORE | {"elapsed_ms", "log_id"}
    assert isinstance(body["elapsed_ms"], int)
    assert body["elapsed_ms"] >= 0
    [document] = await setup.store.list_documents(tenant_id=_TENANT, kb_id=(await _base_id(setup)))
    assert body["count"] >= 1
    for result in body["results"]:
        assert set(result) >= _RESULT_KEYS_BEFORE
        assert result["source"] == f"h.md#{result['chunk_index']}"
        assert {"vector_score", "keyword_score", "fused_score", "rerank_score"} <= set(result)
        assert result["vector_score"] == result["score"]
        assert result["fused_score"] is not None
        assert result["document"] == {
            "id": str(document.id),
            "doc_seq": document.doc_seq,
            "title": document.title,
            "filename": "h.md",
            "created_at": document.created_at.isoformat() if document.created_at else None,
            "enabled": True,
        }
    assert document.doc_seq is not None


async def _base_id(setup: _Setup) -> UUID:
    base = await setup.store.get_base(tenant_id=_TENANT, name="kb")
    assert base is not None
    return base.id


@pytest.mark.asyncio
async def test_the_search_is_logged_as_a_console_test_of_the_employee(setup: _Setup) -> None:
    body = (
        await setup.client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})
    ).json()
    kb_id = await _base_id(setup)
    rows, total = await setup.log.list_for_base(tenant_id=_TENANT, kb_id=kb_id)
    assert total == 1
    [row] = rows
    assert str(row.id) == body["log_id"]
    assert (row.source, row.actor_id, row.run_id) == ("console_test", _EMPLOYEE, None)
    assert (row.kb_ids, row.query, row.status) == ((kb_id,), "deductible", body["status"])
    assert row.result_count == body["count"]


@pytest.mark.asyncio
async def test_a_platform_admins_search_of_another_tenant_is_marked(setup: _Setup) -> None:
    """User ruling (10-28): platform staff searching a tenant's base from the
    console still leave the record in that tenant, marked as theirs —
    ``flags.platform_admin`` — so the tenant sees who searched. Decided on the
    tenants (the caller's home vs the one searched), not on a role name."""
    admin = await grant_system_admin(setup.client)
    resp = await setup.client.post(
        "/v1/knowledge/bases/kb/test",
        params={"tenant_id": str(_TENANT)},
        json={"query": "deductible"},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    [row], _ = await setup.log.list_for_base(tenant_id=_TENANT, kb_id=await _base_id(setup))
    assert str(row.id) == resp.json()["log_id"]
    assert row.flags["platform_admin"] is True
    assert row.actor_id != _EMPLOYEE
    # Shown to the tenant's own employees, in the list and the detail.
    base = "/v1/knowledge/bases/kb/retrieval-logs"
    [listed] = (await setup.client.get(base)).json()["logs"]
    assert listed["flags"]["platform_admin"] is True
    detail = (await setup.client.get(f"{base}/{row.id}")).json()
    assert detail["flags"]["platform_admin"] is True


@pytest.mark.asyncio
async def test_a_search_of_the_callers_own_tenant_is_not_marked(setup: _Setup) -> None:
    for params in ({}, {"tenant_id": str(_TENANT)}):
        resp = await setup.client.post(
            "/v1/knowledge/bases/kb/test", params=params, json={"query": "deductible"}
        )
        assert resp.status_code == 200, resp.text
    rows, total = await setup.log.list_for_base(tenant_id=_TENANT, kb_id=await _base_id(setup))
    assert total == 2
    assert all("platform_admin" not in row.flags for row in rows)


@pytest.mark.asyncio
async def test_a_retriever_that_logs_nothing_reports_no_log_id(setup: _Setup) -> None:
    setup.app.state.knowledge_retriever = KnowledgeRetriever(
        store=setup.store, embedder=FakeEmbedder()
    )
    body = (
        await setup.client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})
    ).json()
    assert body["log_id"] is None
    assert body["count"] >= 1


class _SlowLog(InMemoryKnowledgeRetrievalLogStore):
    async def add(self, entry: RetrievalLogEntry) -> None:
        await asyncio.sleep(0.3)
        await super().add(entry)


class _BrokenLog(InMemoryKnowledgeRetrievalLogStore):
    async def add(self, entry: RetrievalLogEntry) -> None:
        msg = "db down"
        raise ConnectionError(msg)


@pytest.mark.asyncio
async def test_elapsed_ms_is_the_search_without_its_logging(setup: _Setup) -> None:
    log = _SlowLog()
    setup.app.state.knowledge_retriever = LoggedKnowledgeRetriever(
        store=setup.store, embedder=FakeEmbedder(), log_sink=log
    )
    body = (
        await setup.client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})
    ).json()
    [row], _ = await log.list_for_base(tenant_id=_TENANT, kb_id=await _base_id(setup))
    assert body["elapsed_ms"] == row.latency_ms
    assert body["elapsed_ms"] < 250  # the 300 ms write is not counted


@pytest.mark.asyncio
async def test_a_failed_log_write_reports_no_log_id(setup: _Setup) -> None:
    setup.app.state.knowledge_retriever = LoggedKnowledgeRetriever(
        store=setup.store, embedder=FakeEmbedder(), log_sink=_BrokenLog()
    )
    resp = await setup.client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["log_id"] is None


@pytest.mark.asyncio
async def test_documents_come_from_the_results_document_ids(
    setup: _Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No chunk re-read to find a result's document: one document read."""
    calls: list[str] = []
    get_chunks, get_documents = setup.store.get_chunks, setup.store.get_documents

    async def counted_chunks(**kwargs: Any) -> Any:
        calls.append("get_chunks")
        return await get_chunks(**kwargs)

    async def counted_documents(**kwargs: Any) -> Any:
        calls.append("get_documents")
        return await get_documents(**kwargs)

    monkeypatch.setattr(setup.store, "get_chunks", counted_chunks)
    monkeypatch.setattr(setup.store, "get_documents", counted_documents)
    body = (
        await setup.client.post("/v1/knowledge/bases/kb/test", json={"query": "deductible"})
    ).json()
    assert body["results"][0]["document"] is not None
    # The retriever's own attribution read + the response's document read.
    assert calls == ["get_documents", "get_documents"]


def _fixture(name: str) -> Any:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _has_keys(actual: Any, expected: Any, where: str) -> None:
    """``actual`` carries every key ``expected`` (a fixture object) has, down
    through nested objects and the first item of each list."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), where
        missing = set(expected) - set(actual)
        assert not missing, f"{where}: missing {sorted(missing)}"
        for key, value in expected.items():
            if actual[key] is not None:
                _has_keys(actual[key], value, f"{where}.{key}")
    elif isinstance(expected, list) and expected and actual:
        _has_keys(actual[0], expected[0], f"{where}[0]")


@pytest.mark.asyncio
async def test_responses_carry_every_key_the_console_reads(setup: _Setup) -> None:
    """The admin UI's fixtures (``apps/admin-ui/src/api/__fixtures__``) name the
    keys its retrieval-test tab and record views read: a renamed or dropped
    key fails here, not silently in the browser. Read from a real search whose
    first hit is a chunk under a parent row, so every object is filled in
    (the live ``hit`` too)."""
    patched = await setup.client.patch("/v1/knowledge/bases/kb", json={"chunk_max_tokens": 128})
    assert patched.status_code == 200, patched.text
    uploaded = await setup.client.post(
        "/v1/knowledge/bases/kb/documents",
        files={"file": ("guide.md", _GUIDE.encode(), "text/markdown")},
    )
    assert uploaded.status_code == 202, uploaded.text
    await setup.app.state.knowledge_ingestion_runner.drain()
    tested = (
        await setup.client.post(
            "/v1/knowledge/bases/kb/test", json={"query": "pd7 pd8", "method": "keyword"}
        )
    ).json()
    _has_keys(tested, _fixture("knowledge_retrieval_test.json"), "test")
    assert tested["results"] and tested["results"][0]["document"] is not None
    assert tested["results"][0]["parent_index"] is not None

    logs = _fixture("knowledge_retrieval_logs.json")
    base = "/v1/knowledge/bases/kb/retrieval-logs"
    page = (await setup.client.get(base)).json()
    _has_keys(page, logs["page"], "page")
    assert page["logs"]
    detail = (await setup.client.get(f"{base}/{tested['log_id']}")).json()
    _has_keys(detail, logs["detail"], "detail")
    assert detail["results"] and detail["results"][0]["document"] is not None
    passages = (await setup.client.get(f"{base}/{tested['log_id']}/passages")).json()
    _has_keys(passages, logs["passages"], "passages")
    assert passages["passages"][0]["passage"] is not None
    assert passages["passages"][0]["hit"] is not None
