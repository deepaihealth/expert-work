"""``knowledge_search`` → ``knowledge_read`` on the SQL store (real Postgres +
Alembic schema; B-206 K-2).

A search result's ``[D<n>#<k>]`` reference reads its passage, its document's
pages, a word find and a name lookup — passages as the SQL store lists them
(parent rows + chunks), a switched-off passage left out, a document of a base
the agent does not bind unreadable.

Before running locally:
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.protocol import ChunkType, DocumentStatus, KnowledgeChunk, RetrievalMethod
from orchestrator.tools import KnowledgeReadTool, KnowledgeRetriever, KnowledgeSearchTool
from orchestrator.tools.error_classifier import GuidedToolError
from orchestrator.tools.registry import ToolContext

pytestmark = pytest.mark.integration

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages/expert-work-persistence/alembic.ini"
_DB = "knowledge_read_integration"


@pytest.fixture
async def store(postgres_container: PostgresContainer) -> AsyncIterator[SqlKnowledgeStore]:
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


class _Embedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        return [(1.0,) + (0.0,) * (EMBEDDING_DIM - 1) for _ in texts]


async def _ready(
    store: SqlKnowledgeStore,
    tenant: UUID,
    kb_id: UUID,
    filename: str,
    passages: Sequence[Sequence[str]],
) -> UUID:
    """A ``ready`` document: each passage of several chunks gets a parent row."""
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename=filename, content=b"x", content_sha256=filename
    )
    claim = await store.claim_document(
        tenant_id=tenant,
        document_id=document.id,
        now=datetime.now(UTC),
        lease_seconds=600,
        max_attempts=5,
    )
    assert claim is not None
    rows: list[KnowledgeChunk] = []
    index = 0
    for number, bodies in enumerate(passages):
        parent = uuid4() if len(bodies) > 1 else None
        if parent is not None:
            rows.append(
                KnowledgeChunk(
                    id=parent,
                    tenant_id=tenant,
                    kb_id=kb_id,
                    document_id=document.id,
                    chunk_index=number,
                    content="\n\n".join(bodies),
                    embedding=None,
                    chunk_type=ChunkType.PARENT,
                    heading_path=(f"Part {number}",),
                )
            )
        for body in bodies:
            rows.append(
                KnowledgeChunk(
                    id=uuid4(),
                    tenant_id=tenant,
                    kb_id=kb_id,
                    document_id=document.id,
                    chunk_index=index,
                    content=body,
                    embedding=(1.0,) + (0.0,) * (EMBEDDING_DIM - 1),
                    heading_path=(f"Part {number}",),
                    parent_chunk_id=parent,
                )
            )
            index += 1
    assert await store.replace_chunks(
        tenant_id=tenant, document_id=document.id, chunks=rows, generation=claim.generation
    )
    assert await store.set_document_status(
        tenant_id=tenant,
        document_id=document.id,
        status=DocumentStatus.READY,
        chunk_count=index,
        generation=claim.generation,
    )
    return document.id


async def test_a_search_reference_reads_its_passage_and_document(store: SqlKnowledgeStore) -> None:
    tenant = uuid4()
    kb = await store.create_base(
        tenant_id=tenant, name="kb", retrieval_method=RetrievalMethod.KEYWORD
    )
    other = await store.create_base(tenant_id=tenant, name="other")
    document = await _ready(
        store,
        tenant,
        kb.id,
        "manual.md",
        [["alpha intro"], ["beta one", "beta two quartz", "beta three"], ["gamma end"]],
    )
    await _ready(store, tenant, other.id, "secret.md", [["hidden text"]])
    ctx = ToolContext(tenant_id=tenant)
    search = KnowledgeSearchTool(
        retriever=KnowledgeRetriever(store=store, embedder=_Embedder()), knowledge_base_refs=("kb",)
    )
    read = KnowledgeReadTool(store=store, knowledge_base_refs=("kb",))

    found = await search.call({"query": "quartz"}, ctx=ctx)
    reference = re.search(r"\[(D1#\d+)\] manual\.md", found.content)
    assert reference is not None and reference.group(1) == "D1#2"

    around = await read.call({"document": reference.group(1)}, ctx=ctx)
    assert "[D1#1-3] · match\n[Section: Part 1]\n\nbeta one\n\nbeta two quartz" in around.content
    assert [p["ref"] for p in around.meta["passages"]] == ["D1#0", "D1#1-3", "D1#4"]

    page = await read.call({"document": "manual.md"}, ctx=ctx)
    assert page.meta["pages"] == 1 and len(page.meta["passages"]) == 3

    find = await read.call({"document": "D1", "find": "QUARTZ beta"}, ctx=ctx)
    assert [p["role"] for p in find.meta["passages"]] == [
        "context_before",
        "match",
        "context_after",
    ]

    passages = await store.list_passages(tenant_id=tenant, document_id=document)
    await store.set_passage_enabled(
        tenant_id=tenant, document_id=document, passage_id=passages[1].id, enabled=False
    )
    after = await read.call({"document": "D1"}, ctx=ctx)
    assert "beta" not in after.content and "passages: 2" in after.content

    for unreadable in ("D2", "secret.md"):
        with pytest.raises(GuidedToolError, match=r"^no document"):
            await read.call({"document": unreadable}, ctx=ctx)
