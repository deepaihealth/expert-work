"""Document reads never carry the original upload — both stores, one contract.

A document row holds two byte slots (the serving version and a pending
re-upload, up to 50 MiB each). Listing, detail, attribution and re-index read
documents all the time; only the ingest claim and ``get_document_content``
need the bytes. The memory run goes into unit CI; the SQL run is marked
``integration`` and gets its own database in the container. The SQL-only
tests at the end measure what a read allocates: a read that pulled a slot
would allocate the slot.

Before running the SQL tests locally:
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

import gc
import hashlib
import tracemalloc
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    InMemoryKnowledgeStore,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge.base import KnowledgeStore
from expert_work.persistence.models import KnowledgeDocumentRow
from expert_work.protocol import DocumentStatus, KnowledgeChunk, KnowledgeDocument

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "knowledge_document_reads_contract"
_MIB = 1 << 20


async def _session_factory(
    request: pytest.FixtureRequest,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    container: PostgresContainer = request.getfixturevalue("postgres_container")
    admin = str(container.get_connection_url()).replace("+psycopg2", "")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{_DB}"')
    base, _, _ = admin.rpartition("/")
    dsn = f"{base}/{_DB}"
    cfg = Config(str(ALEMBIC_INI))
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


@pytest.fixture(params=["memory", pytest.param("sql", marks=pytest.mark.integration)])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[KnowledgeStore]:
    if request.param == "memory":
        yield InMemoryKnowledgeStore()
        return
    async for factory in _session_factory(request):
        yield SqlKnowledgeStore(factory)


@pytest.fixture
async def sql_factory(
    request: pytest.FixtureRequest,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async for factory in _session_factory(request):
        yield factory


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def _upload(
    store: KnowledgeStore, tenant: UUID, kb_id: UUID, filename: str, content: bytes | None
) -> KnowledgeDocument:
    return await store.upsert_document(
        tenant_id=tenant,
        kb_id=kb_id,
        filename=filename,
        content=content,
        content_sha256=_sha(content) if content is not None else None,
    )


async def _make_ready(store: KnowledgeStore, tenant: UUID, document: KnowledgeDocument) -> None:
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
        kb_id=document.kb_id,
        document_id=document.id,
        chunk_index=0,
        content="serving passage",
        embedding=(1.0,) + (0.0,) * (EMBEDDING_DIM - 1),
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


@dataclass(frozen=True)
class _Library:
    """``reuploaded``: v1 serving, v2 pending. ``settled``: serving, nothing pending."""

    tenant: UUID
    kb_id: UUID
    reuploaded: UUID
    settled: UUID


async def _library(store: KnowledgeStore, v1: bytes, v2: bytes, other: bytes) -> _Library:
    tenant = uuid4()
    kb_id = (await store.create_base(tenant_id=tenant, name="kb")).id
    reuploaded = await _upload(store, tenant, kb_id, "a.pdf", v1)
    await _make_ready(store, tenant, reuploaded)
    assert (await _upload(store, tenant, kb_id, "a.pdf", v2)).has_pending
    settled = await _upload(store, tenant, kb_id, "b.pdf", other)
    await _make_ready(store, tenant, settled)
    return _Library(tenant, kb_id, reuploaded.id, settled.id)


def _light_reads(
    store: KnowledgeStore, lib: _Library
) -> dict[str, Callable[[], Awaitable[dict[UUID, KnowledgeDocument]]]]:
    """Every read that lists or looks up documents, as ``{id: document}``."""

    async def get_document() -> dict[UUID, KnowledgeDocument]:
        found = [
            await store.get_document(tenant_id=lib.tenant, document_id=document_id)
            for document_id in (lib.reuploaded, lib.settled)
        ]
        return {document.id: document for document in found if document is not None}

    async def get_documents() -> dict[UUID, KnowledgeDocument]:
        return await store.get_documents(
            tenant_id=lib.tenant, document_ids=[lib.reuploaded, lib.settled]
        )

    async def list_documents() -> dict[UUID, KnowledgeDocument]:
        listed = await store.list_documents(tenant_id=lib.tenant, kb_id=lib.kb_id)
        return {document.id: document for document in listed}

    async def list_documents_page() -> dict[UUID, KnowledgeDocument]:
        page, _total = await store.list_documents_page(
            tenant_id=lib.tenant, kb_id=lib.kb_id, limit=50, offset=0
        )
        return {document.id: document for document in page}

    return {
        "get_document": get_document,
        "get_documents": get_documents,
        "list_documents": list_documents,
        "list_documents_page": list_documents_page,
    }


async def test_light_reads_report_both_slots_and_the_explicit_read_has_the_bytes(
    store: KnowledgeStore,
) -> None:
    lib = await _library(store, b"serving v1", b"pending v2 bytes", b"other file")
    for name, read in _light_reads(store, lib).items():
        documents = await read()
        assert documents[lib.reuploaded].has_pending, name
        assert documents[lib.reuploaded].size_bytes == len(b"serving v1"), name
        assert not documents[lib.settled].has_pending, name
        assert documents[lib.settled].size_bytes == len(b"other file"), name
    assert (
        await store.get_document_content(tenant_id=lib.tenant, document_id=lib.reuploaded)
        == b"serving v1"
    )


async def test_reupload_fills_the_empty_serving_slot_else_the_pending_one(
    store: KnowledgeStore,
) -> None:
    tenant = uuid4()
    kb_id = (await store.create_base(tenant_id=tenant, name="kb")).id
    # No bytes kept and nothing serving: the upload becomes the serving version.
    bare = await _upload(store, tenant, kb_id, "bare.pdf", None)
    refilled = await _upload(store, tenant, kb_id, "bare.pdf", b"first bytes")
    assert not refilled.has_pending
    assert refilled.size_bytes == len(b"first bytes")
    assert await store.get_document_content(tenant_id=tenant, document_id=bare.id) == b"first bytes"
    # Bytes kept (first ingest failed, nothing serving): the upload waits in pending.
    failed = await _upload(store, tenant, kb_id, "failed.pdf", b"kept bytes")
    claim = await store.claim_document(
        tenant_id=tenant,
        document_id=failed.id,
        now=datetime.now(UTC),
        lease_seconds=600,
        max_attempts=5,
    )
    assert claim is not None
    assert await store.mark_document_failed_terminal(
        tenant_id=tenant, document_id=failed.id, error="parse failed", generation=claim.generation
    )
    again = await _upload(store, tenant, kb_id, "failed.pdf", b"second try")
    assert again.has_pending
    assert await store.get_document_content(tenant_id=tenant, document_id=failed.id) == (
        b"kept bytes"
    )


async def _flags(
    store: KnowledgeStore, tenant: UUID, kb_id: UUID, document_id: UUID
) -> tuple[bool, bool]:
    """``(has_content, has_pending)`` — every light read must say the same."""
    lib = _Library(tenant, kb_id, document_id, document_id)
    seen = {
        name: (documents[document_id].has_content, documents[document_id].has_pending)
        for name, read in _light_reads(store, lib).items()
        for documents in [await read()]
    }
    assert len(set(seen.values())) == 1, seen
    return next(iter(seen.values()))


async def _fail(store: KnowledgeStore, tenant: UUID, document_id: UUID) -> None:
    claim = await store.claim_document(
        tenant_id=tenant,
        document_id=document_id,
        now=datetime.now(UTC),
        lease_seconds=600,
        max_attempts=5,
    )
    assert claim is not None
    assert await store.mark_document_failed_terminal(
        tenant_id=tenant, document_id=document_id, error="parse failed", generation=claim.generation
    )


async def test_byte_slot_flags_through_every_state(store: KnowledgeStore) -> None:
    tenant = uuid4()
    kb_id = (await store.create_base(tenant_id=tenant, name="kb")).id

    async def flags(document: KnowledgeDocument) -> tuple[bool, bool]:
        stored = await _flags(store, tenant, kb_id, document.id)
        assert (document.has_content, document.has_pending) == stored  # what the write returned
        return stored

    # Upload with bytes; first ingest fails; re-upload waits; discard; re-upload + ready.
    kept = await _upload(store, tenant, kb_id, "kept.pdf", b"v1")
    assert await flags(kept) == (True, False)
    await _fail(store, tenant, kept.id)
    assert await _flags(store, tenant, kb_id, kept.id) == (True, False)
    assert await flags(await _upload(store, tenant, kb_id, "kept.pdf", b"v2")) == (True, True)
    restored = await store.discard_pending(tenant_id=tenant, document_id=kept.id)
    assert restored is not None and restored.status is DocumentStatus.FAILED
    assert await flags(restored) == (True, False)
    assert await flags(await _upload(store, tenant, kb_id, "kept.pdf", b"v3")) == (True, True)
    await _make_ready(store, tenant, kept)
    assert await _flags(store, tenant, kb_id, kept.id) == (True, False)
    assert await store.get_document_content(tenant_id=tenant, document_id=kept.id) == b"v3"

    # No bytes and nothing serving: the next upload fills the serving slot.
    bare = await _upload(store, tenant, kb_id, "bare.pdf", None)
    assert await flags(bare) == (False, False)
    assert await flags(await _upload(store, tenant, kb_id, "bare.pdf", b"b1")) == (True, False)

    # A document whose bytes were never kept, serving chunks (a legacy row).
    legacy = await _upload(store, tenant, kb_id, "legacy.pdf", None)
    assert await flags(legacy) == (False, False)
    await _make_ready(store, tenant, legacy)
    assert await _flags(store, tenant, kb_id, legacy.id) == (False, False)
    # Its re-upload waits behind the serving chunks; discard; re-upload + ready.
    assert await flags(await _upload(store, tenant, kb_id, "legacy.pdf", b"n1")) == (False, True)
    dropped = await store.discard_pending(tenant_id=tenant, document_id=legacy.id)
    assert dropped is not None and dropped.status is DocumentStatus.READY
    assert await flags(dropped) == (False, False)
    assert await flags(await _upload(store, tenant, kb_id, "legacy.pdf", b"n2")) == (False, True)
    await _make_ready(store, tenant, legacy)
    assert await _flags(store, tenant, kb_id, legacy.id) == (True, False)
    assert await store.get_document_content(tenant_id=tenant, document_id=legacy.id) == b"n2"


async def test_claim_carries_the_slot_it_ingests_with_that_slots_hash(
    store: KnowledgeStore,
) -> None:
    lib = await _library(store, b"serving v1", b"pending v2 bytes", b"other file")
    now = datetime.now(UTC)
    claim = await store.claim_document(
        tenant_id=lib.tenant,
        document_id=lib.reuploaded,
        now=now,
        lease_seconds=600,
        max_attempts=5,
    )
    assert claim is not None
    assert (claim.content, claim.content_sha256) == (b"pending v2 bytes", _sha(b"pending v2 bytes"))
    # Drop the pending upload, re-ingest explicitly: the serving bytes come back.
    assert await store.discard_pending(tenant_id=lib.tenant, document_id=lib.reuploaded)
    await store.upsert_document(tenant_id=lib.tenant, kb_id=lib.kb_id, filename="a.pdf", force=True)
    claims = await store.claim_documents_for_ingest(
        now=now, lease_seconds=600, limit=10, max_attempts=5
    )
    assert [(c.document_id, c.content, c.content_sha256) for c in claims] == [
        (lib.reuploaded, b"serving v1", _sha(b"serving v1"))
    ]


# -- SQL only: what a read allocates ------------------------------------------

#: Each byte slot in the measured library; a read that pulled one would
#: allocate at least this much (the driver's buffer plus the ``bytes``).
_SLOT = 8 * _MIB
#: What a light read of the two documents may allocate (observed: < 0.5 MiB).
_LIGHT_READ_BUDGET = 1 * _MIB


async def _peak(operation: Callable[[], Awaitable[Any]]) -> int:
    """Bytes allocated at the peak of ``operation``, above where it started."""
    gc.collect()
    tracemalloc.start()
    try:
        start, _ = tracemalloc.get_traced_memory()
        result = await operation()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    del result
    return peak - start


def _distinct(size: int, fill: int) -> bytes:
    return bytes([fill]) * size


@pytest.mark.integration
async def test_sql_light_reads_allocate_none_of_the_bytes(
    sql_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlKnowledgeStore(sql_factory)
    lib = await _library(store, _distinct(_SLOT, 1), _distinct(_SLOT, 2), _distinct(_SLOT, 3))
    reads: dict[str, Callable[[], Awaitable[Any]]] = {
        **_light_reads(store, lib),
        # An explicit re-ingest resets the document without new bytes.
        "upsert_document(force)": lambda: store.upsert_document(
            tenant_id=lib.tenant, kb_id=lib.kb_id, filename="b.pdf", force=True
        ),
        "discard_pending": lambda: store.discard_pending(
            tenant_id=lib.tenant, document_id=lib.reuploaded
        ),
    }
    await store.get_document(tenant_id=lib.tenant, document_id=lib.settled)  # warm the pool
    for name, read in reads.items():
        peak = await _peak(read)
        assert peak < _LIGHT_READ_BUDGET, (name, peak)
    # The measure sees a slot when one is read.
    explicit = await _peak(
        lambda: store.get_document_content(tenant_id=lib.tenant, document_id=lib.reuploaded)
    )
    assert explicit >= _SLOT


@pytest.mark.integration
async def test_sql_reupload_claim_reads_one_slot(
    sql_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlKnowledgeStore(sql_factory)
    lib = await _library(store, _distinct(_SLOT, 1), _distinct(_SLOT, 2), _distinct(_SLOT, 3))
    peak = await _peak(
        lambda: store.claim_document(
            tenant_id=lib.tenant,
            document_id=lib.reuploaded,
            now=datetime.now(UTC),
            lease_seconds=600,
            max_attempts=5,
        )
    )
    # One slot costs about two of its size (driver buffer + bytes); both, four.
    assert _SLOT <= peak < 3 * _SLOT


@pytest.mark.integration
async def test_sql_loaded_row_refuses_to_read_the_bytes(
    sql_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlKnowledgeStore(sql_factory)
    lib = await _library(store, b"serving v1", b"pending v2", b"other")
    async with sql_factory() as session:
        row = (
            await session.execute(
                select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.id == lib.reuploaded)
            )
        ).scalar_one()
        assert row.has_content and row.has_pending
        with pytest.raises(InvalidRequestError, match="raiseload"):
            _ = row.content
        with pytest.raises(InvalidRequestError, match="raiseload"):
            _ = row.pending_content
