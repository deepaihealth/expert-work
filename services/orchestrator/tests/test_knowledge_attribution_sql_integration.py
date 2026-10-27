"""``knowledge_search`` names each hit's source file from the SQL store without
reading any document's original upload (real Postgres + Alembic schema).

Attribution runs on every search, once per distinct source document; it used
to pull every one of them whole (each up to 50 MiB) only to read a file name.

Before running locally:
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

import gc
import tracemalloc
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.protocol import ChunkType, DocumentStatus, KnowledgeChunk
from orchestrator.tools.knowledge import KnowledgeRetriever

pytestmark = pytest.mark.integration

_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "packages/expert-work-persistence/alembic.ini"
_DB = "knowledge_attribution_reads"
_MIB = 1 << 20
#: Each document's stored upload.
_UPLOAD = 8 * _MIB
_DOCUMENTS = 4


@pytest.fixture
async def session_factory(
    postgres_container: PostgresContainer,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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
        yield create_async_session_factory(engine)
    finally:
        await engine.dispose()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


def _vec(lean: float) -> tuple[float, ...]:
    return (1.0, lean) + (0.0,) * (EMBEDDING_DIM - 2)


class _FixedEmbedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        return [_vec(0.0) for _ in texts]


async def _ready_document(store: SqlKnowledgeStore, tenant: UUID, kb_id: UUID, index: int) -> None:
    document = await store.upsert_document(
        tenant_id=tenant,
        kb_id=kb_id,
        filename=f"doc-{index}.pdf",
        content=bytes([index + 1]) * _UPLOAD,
        content_sha256=f"sha-{index}",
    )
    claim = await store.claim_document(
        tenant_id=tenant,
        document_id=document.id,
        now=datetime.now(UTC),
        lease_seconds=600,
        max_attempts=5,
    )
    assert claim is not None
    chunk = KnowledgeChunk(
        id=uuid4(),
        tenant_id=tenant,
        kb_id=kb_id,
        document_id=document.id,
        chunk_index=0,
        content=f"alpha passage {index}",
        embedding=_vec(0.01 * (index + 1)),
    )
    assert await store.replace_chunks(
        tenant_id=tenant, document_id=document.id, chunks=[chunk], generation=claim.generation
    )
    assert await store.set_document_status(
        tenant_id=tenant,
        document_id=document.id,
        status=DocumentStatus.READY,
        chunk_count=1,
        generation=claim.generation,
    )


async def test_search_attributes_file_names_without_reading_the_uploads(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlKnowledgeStore(session_factory)
    tenant = uuid4()
    kb_id = (await store.create_base(tenant_id=tenant, name="kb")).id
    for index in range(_DOCUMENTS):
        await _ready_document(store, tenant, kb_id, index)
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder())

    async def search() -> set[str]:
        hits = await retriever.search(
            tenant_id=tenant, base_names=["kb"], query="alpha", limit=_DOCUMENTS, rerank=False
        )
        return {hit.filename for hit in hits}

    await search()  # warm the pool, the statement cache and the segmenter
    gc.collect()
    tracemalloc.start()
    try:
        start, _ = tracemalloc.get_traced_memory()
        filenames = await search()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert filenames == {f"doc-{index}.pdf" for index in range(_DOCUMENTS)}
    # Reading the uploads would allocate at least ``_DOCUMENTS * _UPLOAD``.
    assert peak - start < _UPLOAD // 4


async def test_search_returns_the_parent_row_of_its_hit_children(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """B-206 K-1 on the SQL store: two hit children of one parent come back as
    that parent row (stored without a vector), once, labelled by the better child."""
    store = SqlKnowledgeStore(session_factory)
    tenant = uuid4()
    kb_id = (await store.create_base(tenant_id=tenant, name="kb")).id
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=kb_id, filename="manual.md", content=b"x", content_sha256="sha"
    )
    claim = await store.claim_document(
        tenant_id=tenant,
        document_id=document.id,
        now=datetime.now(UTC),
        lease_seconds=600,
        max_attempts=5,
    )
    assert claim is not None

    def row(index: int, content: str, **extra: object) -> KnowledgeChunk:
        return KnowledgeChunk.model_validate(
            {
                "id": uuid4(),
                "tenant_id": tenant,
                "kb_id": kb_id,
                "document_id": document.id,
                "chunk_index": index,
                "content": content,
                **extra,
            }
        )

    parent = row(0, "alpha parent body", embedding=None, chunk_type=ChunkType.PARENT)
    # Only child 0 matches "alpha" by keyword, and its vector is the closer one.
    children = [
        row(0, "alpha passage", embedding=_vec(0.01), parent_chunk_id=parent.id),
        row(1, "beta passage", embedding=_vec(0.02), parent_chunk_id=parent.id),
    ]
    assert await store.replace_chunks(
        tenant_id=tenant,
        document_id=document.id,
        chunks=[parent, *children],
        generation=claim.generation,
    )
    assert await store.set_document_status(
        tenant_id=tenant,
        document_id=document.id,
        status=DocumentStatus.READY,
        chunk_count=len(children),
        generation=claim.generation,
    )
    retriever = KnowledgeRetriever(store=store, embedder=_FixedEmbedder())

    (hit,) = await retriever.search(
        tenant_id=tenant, base_names=["kb"], query="alpha", limit=5, rerank=False
    )

    assert (hit.content, hit.parent_chunk_id, hit.parent_index) == (
        "alpha parent body",
        parent.id,
        0,
    )
    assert (hit.chunk_index, hit.chunk_id, hit.hit_chunk_indexes) == (0, children[0].id, (0, 1))
    assert (hit.filename, hit.hit_content) == ("manual.md", "alpha passage")
