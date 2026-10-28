"""Knowledge retrieval logs API — B-175 P2-PR4.

``GET /v1/knowledge/bases/{name}/retrieval-logs`` (newest first, filtered by
source / status / zero hits / time, paged with a total) and ``…/{log_id}``
(the row with each result's document resolved; ``null`` once deleted).
``…/{log_id}/passages`` (B-175 P2-PR4 console) reads each result's passage and
best hit chunk as they are now — the row itself keeps no text.

Ruling (2026-10-27): logs are read under the conversation records' gate —
``session:read`` (every employee: viewer included), console only (an API key
gets 403), one concrete tenant (``tenant_id=*`` refused). Deleting the base
deletes its logs.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

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
from expert_work.protocol import AuditAction, AuditQuery, ChunkType, KnowledgeChunk
from orchestrator.llm import FakeEmbedder
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
_NOW = datetime.now(UTC).replace(microsecond=0)
_LOGS = "/v1/knowledge/bases/kb/retrieval-logs"


def _headers(*roles: str, sub_type: str = "user", scopes: tuple[str, ...] = ()) -> dict[str, str]:
    token = make_test_jwt(
        tenant_id=_TENANT,
        subject=f"emp-{'-'.join(roles) or 'none'}",
        sub_type=sub_type,
        roles=roles,
        scopes=scopes,
    )
    return {"Authorization": f"Bearer {token}"}


_ADMIN = _headers("admin")
_VIEWER = _headers("viewer")


class _Setup:
    def __init__(
        self,
        client: AsyncClient,
        store: InMemoryKnowledgeStore,
        log: InMemoryKnowledgeRetrievalLogStore,
        kb_id: UUID,
        other_kb_id: UUID,
        document_id: UUID,
        runner: KnowledgeIngestionRunner,
    ) -> None:
        self.client, self.store, self.log = client, store, log
        self.kb_id, self.other_kb_id, self.document_id = kb_id, other_kb_id, document_id
        self.runner = runner

    async def add(self, kb_id: UUID | None = None, **fields: Any) -> RetrievalLogEntry:
        entry = RetrievalLogEntry(
            tenant_id=_TENANT,
            kb_ids=(kb_id or self.kb_id,),
            source=fields.pop("source", "agent"),
            query=fields.pop("query", "what is the deductible"),
            status=fields.pop("status", "ok"),
            latency_ms=fields.pop("latency_ms", 40),
            **fields,
        )
        await self.log.add(entry)
        return entry


@pytest.fixture
async def setup() -> AsyncIterator[_Setup]:
    store = InMemoryKnowledgeStore()
    log = InMemoryKnowledgeRetrievalLogStore()
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder())
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cp.test") as client:
        for name in ("kb", "other"):
            await client.post("/v1/knowledge/bases", json={"name": name}, headers=_ADMIN)
        await client.post(
            "/v1/knowledge/bases/kb/documents",
            files={"file": ("h.md", b"# H\n\nThe deductible is 500 dollars.", "text/markdown")},
            headers=_ADMIN,
        )
        await runner.drain()
        kb = await store.get_base(tenant_id=_TENANT, name="kb")
        other = await store.get_base(tenant_id=_TENANT, name="other")
        assert kb is not None
        assert other is not None
        [document] = await store.list_documents(tenant_id=_TENANT, kb_id=kb.id)
        yield _Setup(client, store, log, kb.id, other.id, document.id, runner)


@pytest.mark.asyncio
async def test_a_viewer_lists_the_base_logs_newest_first_with_a_total(setup: _Setup) -> None:
    run_id, thread_id = uuid4(), uuid4()
    older = await setup.add(
        created_at=_NOW - timedelta(minutes=2), run_id=run_id, thread_id=thread_id
    )
    newer = await setup.add(
        created_at=_NOW - timedelta(minutes=1),
        source="console_test",
        actor_id="emp-admin",
        run_id=None,
        params={"limit": 5},
        flags={"rerank_outcome": "ok"},
        results=({"chunk_id": str(uuid4()), "rerank_score": 0.8},),
    )
    await setup.add(kb_id=setup.other_kb_id)  # another base's: not listed

    resp = await setup.client.get(_LOGS, headers=_VIEWER)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [log["id"] for log in body["logs"]] == [str(newer.id), str(older.id)]
    assert (body["total"], body["offset"], body["limit"]) == (2, 0, 50)
    first = body["logs"][0]
    assert first == {
        "id": str(newer.id),
        "source": "console_test",
        "query": "what is the deductible",
        "status": "ok",
        "result_count": 1,
        "latency_ms": 40,
        "run_id": None,
        "thread_id": None,
        "actor_id": "emp-admin",
        "params": {"limit": 5},
        "flags": {"rerank_outcome": "ok"},
        "created_at": newer.created_at.isoformat(),
    }
    # An agent's row names its run and conversation.
    assert (body["logs"][1]["run_id"], body["logs"][1]["thread_id"]) == (
        str(run_id),
        str(thread_id),
    )

    page = (await setup.client.get(_LOGS, params={"limit": 1, "offset": 1}, headers=_VIEWER)).json()
    assert [log["id"] for log in page["logs"]] == [str(older.id)]
    assert page["total"] == 2


@pytest.mark.asyncio
async def test_filters_pass_through(setup: _Setup) -> None:
    hit = await setup.add(created_at=_NOW - timedelta(hours=3), results=({"chunk_id": "c"},))
    empty = await setup.add(created_at=_NOW - timedelta(hours=2), status="no_hits")
    console = await setup.add(created_at=_NOW - timedelta(hours=1), source="console_test")

    async def ids(**params: Any) -> list[str]:
        resp = await setup.client.get(_LOGS, params=params, headers=_VIEWER)
        assert resp.status_code == 200, resp.text
        return [log["id"] for log in resp.json()["logs"]]

    assert await ids(source="console_test") == [str(console.id)]
    assert await ids(zero_hit="true", source="agent") == [str(empty.id)]
    assert await ids(status="no_hits") == [str(empty.id)]
    window = {
        "since": (hit.created_at + timedelta(minutes=1)).isoformat(),
        "until": console.created_at.isoformat(),
    }
    assert await ids(**window) == [str(empty.id)]
    bad = await setup.client.get(_LOGS, params={"source": "webhook"}, headers=_VIEWER)
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_the_detail_resolves_each_result_document(setup: _Setup) -> None:
    gone = uuid4()
    entry = await setup.add(
        results=(
            {"chunk_id": str(uuid4()), "document_id": str(setup.document_id), "rerank_score": 0.9},
            {"chunk_id": str(uuid4()), "document_id": str(gone), "rerank_score": 0.4},
        ),
    )
    resp = await setup.client.get(f"{_LOGS}/{entry.id}", headers=_VIEWER)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(entry.id)
    assert body["kb_ids"] == [str(setup.kb_id)]
    document = await setup.store.get_document(tenant_id=_TENANT, document_id=setup.document_id)
    assert document is not None
    first, second = body["results"]
    assert first["rerank_score"] == 0.9
    assert first["document"] == {
        "id": str(document.id),
        "doc_seq": document.doc_seq,
        "title": document.title,
        "filename": "h.md",
        "created_at": document.created_at.isoformat() if document.created_at else None,
    }
    assert second["document"] is None


@pytest.mark.asyncio
async def test_the_detail_is_404_outside_the_base(setup: _Setup) -> None:
    elsewhere = await setup.add(kb_id=setup.other_kb_id)
    assert (await setup.client.get(f"{_LOGS}/{elsewhere.id}", headers=_VIEWER)).status_code == 404
    assert (await setup.client.get(f"{_LOGS}/{uuid4()}", headers=_VIEWER)).status_code == 404
    ghost = "/v1/knowledge/bases/ghost/retrieval-logs"
    assert (await setup.client.get(ghost, headers=_VIEWER)).status_code == 404


@pytest.mark.asyncio
async def test_a_roleless_employee_is_refused(setup: _Setup) -> None:
    entry = await setup.add()
    for path in (_LOGS, f"{_LOGS}/{entry.id}", f"{_LOGS}/{entry.id}/passages"):
        resp = await setup.client.get(path, headers=_headers())
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["message"] == "principal lacks required role"


@pytest.mark.asyncio
async def test_an_api_key_is_refused(setup: _Setup) -> None:
    key = _headers(sub_type="service_account", scopes=("admin",))
    entry = await setup.add()
    for path in (_LOGS, f"{_LOGS}/{entry.id}", f"{_LOGS}/{entry.id}/passages"):
        resp = await setup.client.get(path, headers=key)
        assert resp.status_code == 403, resp.text
        assert "not available to API keys" in resp.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_all_tenants_is_refused(setup: _Setup) -> None:
    entry = await setup.add()
    for path in (_LOGS, f"{_LOGS}/{entry.id}/passages"):
        resp = await setup.client.get(path, params={"tenant_id": "*"}, headers=_ADMIN)
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["code"] == "SCOPE_ALL_NOT_SUPPORTED"


# -- live passage text (B-175 P2-PR4 console) ------------------------------------------

#: One chapter that fills a passage of several chunks (a parent row; one chunk
#: per paragraph at a 128-token chunk size), then a short one that stands for
#: itself (as in ``test_knowledge_api_passages``).
_GUIDE = "\n\n".join(
    [
        "# Guide",
        *(" ".join(f"p{tag}{i}" for i in range(50)) + "." for tag in "abcdefghij"),
        "# Notes",
        "The deductible is 500 dollars per year. " + " ".join(f"nt{i}" for i in range(20)),
    ]
)


class _Guide:
    """The uploaded guide: its id, the passage's parent row, one chunk under
    it, and the chunk standing for itself."""

    def __init__(
        self, document_id: UUID, parent: KnowledgeChunk, child: KnowledgeChunk, lone: KnowledgeChunk
    ) -> None:
        self.document_id, self.parent, self.child, self.lone = document_id, parent, child, lone


async def _upload(setup: _Setup, base: str, filename: str, text: str) -> UUID:
    uploaded = await setup.client.post(
        f"/v1/knowledge/bases/{base}/documents",
        files={"file": (filename, text.encode(), "text/markdown")},
        headers=_ADMIN,
    )
    assert uploaded.status_code == 202, uploaded.text
    await setup.runner.drain()
    return UUID(uploaded.json()["id"])


async def _guide(setup: _Setup) -> _Guide:
    patched = await setup.client.patch(
        "/v1/knowledge/bases/kb", json={"chunk_max_tokens": 128}, headers=_ADMIN
    )
    assert patched.status_code == 200, patched.text
    document_id = await _upload(setup, "kb", "guide.md", _GUIDE)
    chunks, _ = await setup.store.list_chunks(tenant_id=_TENANT, document_id=document_id, limit=200)
    [parent], _ = await setup.store.list_chunks(
        tenant_id=_TENANT, document_id=document_id, chunk_types=(ChunkType.PARENT,)
    )
    under = [c for c in chunks if c.parent_chunk_id == parent.id]
    [lone] = [c for c in chunks if c.parent_chunk_id is None]
    assert len(under) > 1
    return _Guide(document_id, parent, under[1], lone)


def _hit(
    chunk: KnowledgeChunk, *, parent: KnowledgeChunk | None = None, document_id: UUID | None = None
) -> dict[str, Any]:
    """A logged result for ``chunk`` (under ``parent``) — ids and a score, no text."""
    return {
        "chunk_id": str(chunk.id),
        "document_id": str(document_id or chunk.document_id),
        "parent_chunk_id": str(parent.id) if parent is not None else None,
        "chunk_index": chunk.chunk_index,
        "rerank_score": 0.5,
    }


def _live(chunk: KnowledgeChunk, **changes: Any) -> dict[str, Any]:
    return {
        "id": str(chunk.id),
        "content": chunk.content,
        "heading_path": list(chunk.heading_path),
        "enabled": chunk.enabled,
        **changes,
    }


@pytest.mark.asyncio
async def test_live_passages_are_each_result_text_as_it_is_now(
    setup: _Setup, caplog: pytest.LogCaptureFixture
) -> None:
    guide = await _guide(setup)
    entry = await setup.add(
        results=(
            _hit(guide.child, parent=guide.parent),
            _hit(guide.lone),
            # A document deleted since, and a result written without ids.
            {"chunk_id": str(uuid4()), "document_id": str(uuid4()), "parent_chunk_id": None},
            {"chunk_id": None, "document_id": None},
        )
    )
    caplog.set_level(logging.DEBUG)
    resp = await setup.client.get(f"{_LOGS}/{entry.id}/passages", headers=_VIEWER)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "passages": [
            {"passage": _live(guide.parent), "hit": _live(guide.child)},
            {"passage": _live(guide.lone), "hit": None},
            {"passage": None, "hit": None},
            {"passage": None, "hit": None},
        ]
    }
    # The text is read for the console only — never written to the app log.
    assert guide.child.content not in caplog.text
    assert guide.parent.content[:40] not in caplog.text


@pytest.mark.asyncio
async def test_live_passages_follow_a_switch_made_since(setup: _Setup) -> None:
    guide = await _guide(setup)
    entry = await setup.add(results=(_hit(guide.child, parent=guide.parent),))
    switched = await setup.client.patch(
        f"/v1/knowledge/bases/kb/documents/{guide.document_id}/passages/{guide.parent.id}",
        json={"enabled": False},
        headers=_ADMIN,
    )
    assert switched.status_code == 200, switched.text
    [live] = (await setup.client.get(f"{_LOGS}/{entry.id}/passages", headers=_VIEWER)).json()[
        "passages"
    ]
    assert live["passage"] == _live(guide.parent, enabled=False)
    assert live["hit"]["enabled"] is False


@pytest.mark.asyncio
async def test_live_passages_leave_out_chunks_the_record_did_not_search(setup: _Setup) -> None:
    guide = await _guide(setup)
    other_document = await _upload(setup, "other", "o.md", "# O\n\nAnother base's passage.")
    other_chunks, _ = await setup.store.list_chunks(tenant_id=_TENANT, document_id=other_document)
    other_chunk = other_chunks[0]
    entry = await setup.add(
        results=(
            # The chunk is real, but the result names another document.
            _hit(guide.child, parent=guide.parent, document_id=uuid4()),
            # A chunk of a base this search did not cover.
            _hit(other_chunk),
        )
    )
    body = (await setup.client.get(f"{_LOGS}/{entry.id}/passages", headers=_VIEWER)).json()
    assert body == {"passages": [{"passage": None, "hit": None}] * 2}

    # A search that covered both bases reads both.
    both = RetrievalLogEntry(
        tenant_id=_TENANT,
        kb_ids=(setup.kb_id, setup.other_kb_id),
        source="agent",
        query="q",
        status="ok",
        latency_ms=1,
        results=(_hit(other_chunk),),
    )
    await setup.log.add(both)
    body = (await setup.client.get(f"{_LOGS}/{both.id}/passages", headers=_VIEWER)).json()
    assert body == {"passages": [{"passage": _live(other_chunk), "hit": None}]}


@pytest.mark.asyncio
async def test_live_passages_are_404_where_the_detail_is(setup: _Setup) -> None:
    guide = await _guide(setup)
    hits = (_hit(guide.lone),)
    elsewhere = await setup.add(kb_id=setup.other_kb_id, results=hits)
    foreign = RetrievalLogEntry(
        tenant_id=uuid4(),
        kb_ids=(setup.kb_id,),
        source="agent",
        query="q",
        status="ok",
        latency_ms=1,
        results=hits,
    )
    await setup.log.add(foreign)
    for log_id in (elsewhere.id, foreign.id, uuid4()):
        resp = await setup.client.get(f"{_LOGS}/{log_id}/passages", headers=_VIEWER)
        assert resp.status_code == 404, resp.text
        assert guide.lone.content not in resp.text
    entry = await setup.add(results=hits)
    ghost = f"/v1/knowledge/bases/ghost/retrieval-logs/{entry.id}/passages"
    assert (await setup.client.get(ghost, headers=_VIEWER)).status_code == 404


@pytest.mark.asyncio
async def test_deleting_the_base_deletes_its_logs(setup: _Setup) -> None:
    await setup.add()
    await setup.add(kb_id=setup.other_kb_id)
    resp = await setup.client.delete("/v1/knowledge/bases/kb", headers=_ADMIN)
    assert resp.status_code == 204, resp.text
    _rows, total = await setup.log.list_for_base(tenant_id=_TENANT, kb_id=setup.kb_id)
    assert total == 0
    _rows, total = await setup.log.list_for_base(tenant_id=_TENANT, kb_id=setup.other_kb_id)
    assert total == 1


class _WatchedLog(InMemoryKnowledgeRetrievalLogStore):
    """Records whether the base still existed at each ``delete_for_base``;
    fails the calls numbered in ``fail_on``."""

    def __init__(self, store: InMemoryKnowledgeStore, fail_on: frozenset[int]) -> None:
        super().__init__()
        self.store, self.fail_on = store, fail_on
        self.base_existed: list[bool] = []

    async def delete_for_base(self, *, tenant_id: UUID, kb_id: UUID) -> int:
        self.base_existed.append(
            await self.store.get_base(tenant_id=tenant_id, name="kb") is not None
        )
        if len(self.base_existed) in self.fail_on:
            msg = "db blip"
            raise ConnectionError(msg)
        return await super().delete_for_base(tenant_id=tenant_id, kb_id=kb_id)


async def _delete_base_with(fail_on: frozenset[int]) -> tuple[list[int], _WatchedLog, Any, Any]:
    store, audit_store = InMemoryKnowledgeStore(), InMemoryAuditLogStore()
    log = _WatchedLog(store, fail_on)
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
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
    )
    statuses: list[int] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cp.test") as client:
        await client.post("/v1/knowledge/bases", json={"name": "kb"}, headers=_ADMIN)
        base = await store.get_base(tenant_id=_TENANT, name="kb")
        assert base is not None
        await log.add(
            RetrievalLogEntry(
                tenant_id=_TENANT,
                kb_ids=(base.id,),
                source="agent",
                query="q",
                status="ok",
                latency_ms=1,
            )
        )
        for _ in range(2):
            statuses.append(
                (await client.delete("/v1/knowledge/bases/kb", headers=_ADMIN)).status_code
            )
    page = await audit_store.query(
        AuditQuery(tenant_id=_TENANT, action=AuditAction.KNOWLEDGE_BASE_DELETE)
    )
    return statuses, log, store, page


@pytest.mark.asyncio
async def test_logs_go_before_the_base_and_again_after_it() -> None:
    """Before: a log-store error leaves the base. After: a search that wrote
    while the base was being deleted leaves no row behind."""
    statuses, log, _store, page = await _delete_base_with(frozenset())
    assert statuses == [204, 404]
    assert log.base_existed == [True, False]
    assert len(page.entries) == 1


@pytest.mark.asyncio
async def test_a_log_store_error_keeps_the_base_for_a_retry() -> None:
    statuses, log, store, page = await _delete_base_with(frozenset({1}))
    # The first attempt fails before anything is deleted; the retry completes.
    assert statuses == [503, 204]
    assert log.base_existed == [True, True, False]
    assert await store.get_base(tenant_id=_TENANT, name="kb") is None
    rows, _total = await log.list_for_base(tenant_id=_TENANT, kb_id=uuid4())
    assert rows == []
    assert len(page.entries) == 1  # only the delete that happened is audited


@pytest.mark.asyncio
async def test_the_second_log_delete_is_best_effort() -> None:
    statuses, log, store, page = await _delete_base_with(frozenset({2}))
    assert statuses[0] == 204
    assert log.base_existed[:2] == [True, False]
    assert await store.get_base(tenant_id=_TENANT, name="kb") is None
    assert len(page.entries) == 1
