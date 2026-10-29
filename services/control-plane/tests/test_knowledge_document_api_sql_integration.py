"""The console's document responses are the same on the SQL store as on the
in-memory one (whose responses the unit tests pin) — real Postgres + Alembic.

The SQL store reads documents without their byte slots and derives
``has_pending`` in SQL; this walks one document through upload, a failed
re-upload, retry, discard, re-ingest and a good re-upload, then searches, and
compares every response with the in-memory run.

Before running locally:
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

import gc
import re
import tracemalloc
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from testcontainers.postgres import PostgresContainer

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.knowledge.ingestion import KnowledgeIngestionRunner
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import (
    DatabaseConfig,
    InMemoryKnowledgeStore,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.knowledge.base import KnowledgeStore
from expert_work.protocol import (
    DocumentPage,
    DocumentStatus,
    PageDecision,
    PageReason,
    PageStatus,
)
from orchestrator.llm import FakeEmbedder
from orchestrator.tools.knowledge import KnowledgeRetriever
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt

pytestmark = pytest.mark.integration

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages/expert-work-persistence/alembic.ini"
_DB = "knowledge_document_api"
_MIB = 1 << 20
_MD = b"# Handbook\n\nThe deductible is 500 dollars per year."
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: Differ between runs (or, for the vector / keyword scores, between Postgres's
#: float4 and Python's float8 arithmetic; ``elapsed_ms`` is wall-clock).
_VOLATILE = frozenset(
    {
        "created_at",
        "updated_at",
        "score",
        "vector_score",
        "keyword_score",
        "elapsed_ms",
        # B-222 PR-2 —— estimate / confirmation / actual-cost times.
        "confirm_by",
        "earliest_confirm_by",
        "confirmed_at",
        "finished_at",
    }
)


class _FakeEmbeddingConfig:
    async def effective_embedding_config(self) -> tuple[str, str] | None:
        return ("qwen", "text-embedding-v4")

    async def effective_rerank_config(self) -> tuple[str, str] | None:
        return None


@pytest.fixture
async def sql_store(postgres_container: PostgresContainer) -> AsyncIterator[SqlKnowledgeStore]:
    admin = str(postgres_container.get_connection_url()).replace("+psycopg2", "")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_DB}"')
    base, _, _ = admin.rpartition("/")
    dsn = f"{base}/{_DB}"
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(cfg, "head")
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    )
    try:
        yield SqlKnowledgeStore(create_async_session_factory(engine))
    finally:
        await engine.dispose()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


def _normalise(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    if isinstance(value, str):
        return _UUID.sub("<uuid>", value)
    return value


def _client(store: KnowledgeStore, runner: Any) -> AsyncClient:
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
    app.state.platform_embedding_config_service = _FakeEmbeddingConfig()
    app.state.knowledge_retriever = KnowledgeRetriever(store=store, embedder=FakeEmbedder())
    token = make_test_jwt(tenant_id=DEFAULT_DEV_TENANT_ID, subject="user-a")
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://cp.test",
        headers={"Authorization": f"Bearer {token}"},
    )


async def _walk(store: KnowledgeStore) -> list[tuple[str, int, Any]]:
    """``(step, status, normalised body)`` for each request of the walk."""
    runner = KnowledgeIngestionRunner(store=store, embedder=FakeEmbedder())
    steps: list[tuple[str, int, Any]] = []
    async with _client(store, runner) as client:

        async def call(step: str, method: str, url: str, **kwargs: Any) -> Any:
            response = await client.request(method, url, **kwargs)
            await runner.drain()
            body = response.json() if response.content else None
            steps.append((step, response.status_code, _normalise(body)))
            return body

        def upload(body: bytes) -> dict[str, Any]:
            return {"files": {"file": ("h.md", body, "text/markdown")}}

        docs = "/v1/knowledge/bases/kb/documents"

        async def confirm(step: str) -> Any:
            """B-222 PR-2: 「全部确认」 with what the list reports."""
            summary = (await call(f"{step} (summary)", "GET", docs))["estimate_summary"]
            expected = {
                "all": True,
                "expected_count": summary["awaiting"],
                "expected_total_micros": summary["total_micros"],
            }
            return await call(step, "POST", f"{docs}/confirm", json=expected)

        await call("create base", "POST", "/v1/knowledge/bases", json={"name": "kb"})
        doc = (await call("upload v1", "POST", docs, **upload(_MD)))["id"]
        await call("list awaiting", "GET", docs)
        await confirm("confirm v1")
        await call("list", "GET", docs)
        await call("upload same", "POST", docs, **upload(_MD))
        await call("upload bad v2", "POST", docs, **upload(b"   "))
        await call("list pending", "GET", f"{docs}?limit=50&offset=0")
        await call("retry pending", "POST", f"{docs}/{doc}/reingest")
        await call("list after retry", "GET", docs)
        await call("chunks", "GET", f"{docs}/{doc}/chunks")
        await call("pages", "GET", f"{docs}/{doc}/pages")
        await call("discard", "POST", f"{docs}/{doc}/discard-pending")
        await call("discard again", "POST", f"{docs}/{doc}/discard-pending")
        await call("reingest serving", "POST", f"{docs}/{doc}/reingest")
        await confirm("confirm reingest")
        await call("list after reingest", "GET", docs)
        await call("upload good v2", "POST", docs, **upload(_MD + b"\n\nMore."))
        await confirm("confirm v2")
        await call("list after v2", "GET", docs)
        copy = await call(
            "upload a copy", "POST", docs, files={"file": ("copy.md", _MD + b"\n\nMore.")}
        )
        withdraw = {"document_ids": [copy["id"]]}
        await call("withdraw the copy", "POST", f"{docs}/cancel", json=withdraw)
        search = {"query": "deductible", "rerank": False}
        await call("search", "POST", "/v1/knowledge/bases/kb/test", json=search)
    return steps


async def test_document_responses_match_the_in_memory_store(
    sql_store: SqlKnowledgeStore,
) -> None:
    on_sql = await _walk(sql_store)
    assert on_sql == await _walk(InMemoryKnowledgeStore())
    by_step = {step: (status, body) for step, status, body in on_sql}
    (awaiting,) = by_step["list awaiting"][1]["documents"]
    assert (awaiting["status"], awaiting["ingest_estimate"]["origin"]) == (
        "awaiting_confirmation",
        "upload",
    )
    assert by_step["confirm v1"][1]["confirmed"] == ["<uuid>"]
    pending = by_step["list pending"][1]["documents"][0]
    assert (pending["status"], pending["has_pending"]) == ("failed", True)
    assert by_step["discard"][1]["has_pending"] is False
    assert by_step["discard again"][0] == 409
    assert by_step["list after v2"][1]["documents"][0]["size_bytes"] == len(_MD + b"\n\nMore.")
    assert [hit["filename"] for hit in by_step["search"][1]["results"]] == ["h.md"]
    # K-21 ①: the same bytes under another name wait as a duplicate, unparsed.
    copy = by_step["upload a copy"][1]
    assert (copy["status"], copy["ingest_estimate"]["estimated"]) == (
        "awaiting_confirmation",
        False,
    )
    assert by_step["withdraw the copy"][1]["results"][0]["outcome"] == "withdrawn_deleted"


# -- a reingest / recognize click leaves the bytes in Postgres ---------------

#: The document's stored upload; reading it costs about twice this.
_UPLOAD = 8 * _MIB


class _RecordingRunner:
    """Stands in for the ingestion runner: records what a click queues and
    ingests nothing, so the click is measured on its own."""

    def __init__(self) -> None:
        self.queued: list[UUID] = []

    def submit(self, *, tenant_id: UUID, document_id: UUID) -> None:
        self.queued.append(document_id)

    def cancel_local(self, document_ids: Sequence[UUID]) -> int:
        return 0


async def _peak(operation: Callable[[], Awaitable[Any]]) -> tuple[int, Any]:
    gc.collect()
    tracemalloc.start()
    try:
        start, _ = tracemalloc.get_traced_memory()
        result = await operation()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak - start, result


async def test_reingest_and_recognize_clicks_do_not_read_the_upload(
    sql_store: SqlKnowledgeStore,
) -> None:
    tenant = DEFAULT_DEV_TENANT_ID
    kb_id = (await sql_store.create_base(tenant_id=tenant, name="kb")).id
    upload = bytes([7]) * _UPLOAD
    document = await sql_store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="scan.pdf", content=upload, content_sha256="s"
    )

    async def claimed(phase: str) -> int:
        """What the queued work does first: claim, which carries the bytes."""
        peak, claim = await _peak(
            lambda: sql_store.claim_document(
                tenant_id=tenant,
                document_id=document.id,
                now=datetime.now(UTC),
                lease_seconds=600,
                max_attempts=5,
            )
        )
        assert claim is not None and (claim.phase, claim.content) == (phase, upload)
        return peak

    async def ingest() -> int:
        peak = await claimed("ingest")
        current = await sql_store.get_document(tenant_id=tenant, document_id=document.id)
        assert current is not None
        generation = current.ingest_generation
        failed_page = DocumentPage(
            page=1,
            decision=PageDecision.OCR,
            reason=PageReason.FULL_PAGE_IMAGE,
            status=PageStatus.FAILED,
            images=1,
            error="primary: empty output",
        )
        assert await sql_store.set_document_pages(
            tenant_id=tenant,
            document_id=document.id,
            pages=[failed_page],
            generation=generation,
        )
        assert await sql_store.set_document_status(
            tenant_id=tenant,
            document_id=document.id,
            status=DocumentStatus.READY,
            chunk_count=0,
            generation=generation,
        )
        return peak

    assert await ingest() >= _UPLOAD
    runner = _RecordingRunner()
    async with _client(sql_store, runner) as client:
        url = f"/v1/knowledge/bases/kb/documents/{document.id}"
        await client.get(f"{url}/pages")  # warm the app and the pool
        for action in ("reingest", "recognize"):
            peak, response = await _peak(partial(client.post, f"{url}/{action}"))
            assert response.status_code == 202, (action, response.text)
            assert peak < _UPLOAD // 4, (action, peak)
            assert runner.queued[-1] == document.id
            # The estimate the click queued (B-222 PR-2) still gets the bytes.
            assert await claimed("estimate") >= _UPLOAD, action
            # Withdrawn, the document is back as it was: ready, its page failed.
            withdrawn = await client.post(
                "/v1/knowledge/bases/kb/documents/cancel",
                json={"document_ids": [str(document.id)]},
            )
            assert withdrawn.json()["results"] == [
                {"document_id": str(document.id), "outcome": "withdrawn"}
            ], action
