"""B-222 — cancelling an ingest in progress, both stores, one contract.

``cancel_documents`` moves a ``pending`` / ``processing`` document to
``cancelled``: its generation is bumped (every write of the worker still
ingesting it is fenced out) and its lease released; a settled document is not
cancellable; another base's / tenant's document is not found. A cancelled
document is never claimed; a re-upload or re-ingest resets it like any other.
The memory run goes into unit CI; the SQL run is marked ``integration``.

Before running the SQL tests locally:
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, update
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
from expert_work.protocol import (
    ChunkType,
    DocumentStatus,
    IngestStage,
    KnowledgeChunk,
    KnowledgeDocument,
)

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "knowledge_cancel_contract"
_LEASE_S = 600
_MAX_ATTEMPTS = 5


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


async def _lease_until(store: KnowledgeStore, document_id: UUID) -> datetime | None:
    """The document's lease expiry — not on the DTO, so read where each store keeps it."""
    if isinstance(store, InMemoryKnowledgeStore):
        return store._lease.get(document_id)
    assert isinstance(store, SqlKnowledgeStore)
    async with store._sf() as session:
        return (
            await session.execute(
                select(KnowledgeDocumentRow.lease_until).where(
                    KnowledgeDocumentRow.id == document_id
                )
            )
        ).scalar_one()


async def _upload(
    store: KnowledgeStore, tenant: UUID, kb_id: UUID, filename: str
) -> KnowledgeDocument:
    return await store.upsert_document(
        tenant_id=tenant,
        kb_id=kb_id,
        filename=filename,
        content=f"bytes of {filename}".encode(),
        content_sha256=f"sha-{filename}",
    )


async def _claim(
    store: KnowledgeStore, tenant: UUID, document_id: UUID, *, now: datetime | None = None
) -> int:
    claim = await store.claim_document(
        tenant_id=tenant,
        document_id=document_id,
        now=now or datetime.now(UTC),
        lease_seconds=_LEASE_S,
        max_attempts=_MAX_ATTEMPTS,
    )
    assert claim is not None
    return claim.generation


async def _at_stage(
    store: KnowledgeStore,
    tenant: UUID,
    document_id: UUID,
    stage: IngestStage,
    *,
    lease_expired: bool = False,
) -> int:
    """Claim the document (its lease long expired when ``lease_expired``: the
    worker died) and record that its ingest reached ``stage``."""
    now = datetime.now(UTC) - timedelta(seconds=_LEASE_S * 2) if lease_expired else None
    generation = await _claim(store, tenant, document_id, now=now)
    assert await store.set_document_stage(
        tenant_id=tenant, document_id=document_id, stage=stage, generation=generation
    )
    return generation


async def _serving_with_reupload(
    store: KnowledgeStore, tenant: UUID, kb_id: UUID, filename: str
) -> KnowledgeDocument:
    """A READY document with one chunk, then re-uploaded (v2 pending)."""
    document = await _upload(store, tenant, kb_id, filename)
    generation = await _claim(store, tenant, document.id)
    chunk = KnowledgeChunk(
        id=uuid4(),
        tenant_id=tenant,
        kb_id=kb_id,
        document_id=document.id,
        chunk_index=0,
        content="v1 passage",
        embedding=(1.0,) + (0.0,) * (EMBEDDING_DIM - 1),
    )
    assert await store.replace_chunks(
        tenant_id=tenant, document_id=document.id, chunks=[chunk], generation=generation
    )
    assert await store.set_document_status(
        tenant_id=tenant,
        document_id=document.id,
        status=DocumentStatus.READY,
        chunk_count=1,
        generation=generation,
    )
    reuploaded = await store.upsert_document(
        tenant_id=tenant,
        kb_id=kb_id,
        filename=filename,
        content=b"v2 bytes",
        content_sha256="v2",
    )
    assert reuploaded.has_pending
    return reuploaded


async def _get(store: KnowledgeStore, tenant: UUID, document_id: UUID) -> KnowledgeDocument:
    document = await store.get_document(tenant_id=tenant, document_id=document_id)
    assert document is not None
    return document


async def test_cancel_pending_and_processing(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    pending = await _upload(store, tenant, base.id, "pending.md")
    processing = await _upload(store, tenant, base.id, "processing.md")
    generation = await _claim(store, tenant, processing.id)
    assert await _lease_until(store, processing.id) is not None
    ready = await _upload(store, tenant, base.id, "ready.md")
    ready_generation = await _claim(store, tenant, ready.id)
    assert await store.set_document_status(
        tenant_id=tenant,
        document_id=ready.id,
        status=DocumentStatus.READY,
        chunk_count=0,
        generation=ready_generation,
    )
    missing = uuid4()

    outcomes = await store.cancel_documents(
        tenant_id=tenant,
        kb_id=base.id,
        document_ids=[pending.id, processing.id, ready.id, missing],
    )

    assert outcomes == {
        pending.id: "cancelled",
        processing.id: "cancelled",
        ready.id: "not_cancellable",
        missing: "not_found",
    }
    for before in (pending, processing):
        after = await _get(store, tenant, before.id)
        assert after.status is DocumentStatus.CANCELLED
        assert after.ingest_generation == before.ingest_generation + 1
        assert after.error == "cancelled by user"
        assert await _lease_until(store, before.id) is None
    # The worker that claimed ``processing`` is fenced out of every write.
    assert not await store.renew_lease(
        tenant_id=tenant,
        document_id=processing.id,
        generation=generation,
        now=datetime.now(UTC),
        lease_seconds=_LEASE_S,
    )
    untouched = await _get(store, tenant, ready.id)
    assert untouched.status is DocumentStatus.READY
    assert untouched.ingest_generation == ready.ingest_generation


async def test_cancel_again_and_settled_documents_are_not_cancellable(
    store: KnowledgeStore,
) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await _upload(store, tenant, base.id, "a.md")
    failed = await _upload(store, tenant, base.id, "b.md")
    failed_generation = await _claim(store, tenant, failed.id)
    assert await store.mark_document_failed_terminal(
        tenant_id=tenant, document_id=failed.id, error="boom", generation=failed_generation
    )
    assert await store.cancel_documents(
        tenant_id=tenant, kb_id=base.id, document_ids=[document.id]
    ) == {document.id: "cancelled"}
    generation = (await _get(store, tenant, document.id)).ingest_generation

    again = await store.cancel_documents(
        tenant_id=tenant, kb_id=base.id, document_ids=[document.id, failed.id, document.id]
    )

    assert again == {document.id: "not_cancellable", failed.id: "not_cancellable"}
    assert (await _get(store, tenant, document.id)).ingest_generation == generation
    assert (await _get(store, tenant, failed.id)).status is DocumentStatus.FAILED


async def test_cancel_other_base_or_tenant_is_not_found(store: KnowledgeStore) -> None:
    tenant, other_tenant = uuid4(), uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    other_base = await store.create_base(tenant_id=tenant, name="other")
    foreign_base = await store.create_base(tenant_id=other_tenant, name="kb")
    in_other_base = await _upload(store, tenant, other_base.id, "a.md")
    foreign = await _upload(store, other_tenant, foreign_base.id, "a.md")

    outcomes = await store.cancel_documents(
        tenant_id=tenant, kb_id=base.id, document_ids=[in_other_base.id, foreign.id]
    )
    foreign_by_kb = await store.cancel_documents(
        tenant_id=tenant, kb_id=foreign_base.id, document_ids=[foreign.id]
    )

    assert outcomes == {in_other_base.id: "not_found", foreign.id: "not_found"}
    assert foreign_by_kb == {foreign.id: "not_found"}
    assert (await _get(store, tenant, in_other_base.id)).status is DocumentStatus.PENDING
    assert (await _get(store, other_tenant, foreign.id)).status is DocumentStatus.PENDING


async def test_processing_at_indexing_is_not_cancellable(store: KnowledgeStore) -> None:
    """Indexing is the point of no return: the new chunks are being swapped in,
    and a cancel there would leave them serving under a cancelled status."""
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    embedding = await _upload(store, tenant, base.id, "embedding.md")
    indexing = await _upload(store, tenant, base.id, "indexing.md")
    generations: dict[UUID, int] = {}
    for document, stage in ((embedding, IngestStage.EMBEDDING), (indexing, IngestStage.INDEXING)):
        generations[document.id] = await _claim(store, tenant, document.id)
        assert await store.set_document_stage(
            tenant_id=tenant,
            document_id=document.id,
            stage=stage,
            generation=generations[document.id],
        )

    outcomes = await store.cancel_documents(
        tenant_id=tenant, kb_id=base.id, document_ids=[embedding.id, indexing.id]
    )

    assert outcomes == {embedding.id: "cancelled", indexing.id: "not_cancellable"}
    kept = await _get(store, tenant, indexing.id)
    assert kept.status is DocumentStatus.PROCESSING
    assert kept.ingest_generation == indexing.ingest_generation
    # Its worker still owns it: the READY write lands.
    assert await store.set_document_status(
        tenant_id=tenant,
        document_id=indexing.id,
        status=DocumentStatus.READY,
        chunk_count=0,
        generation=generations[indexing.id],
    )


async def test_a_dead_worker_at_indexing_is_still_not_cancellable(store: KnowledgeStore) -> None:
    """A worker whose lease ran out at indexing may have died after its swap,
    with the new chunks already serving: cancelling then would put a cancelled
    status on top of them. The recovery worker re-claims it instead."""
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await _upload(store, tenant, base.id, "a.md")
    await _at_stage(store, tenant, document.id, IngestStage.INDEXING, lease_expired=True)

    outcomes = await store.cancel_documents(
        tenant_id=tenant, kb_id=base.id, document_ids=[document.id]
    )

    assert outcomes == {document.id: "not_cancellable"}
    assert (await _get(store, tenant, document.id)).status is DocumentStatus.PROCESSING


async def test_discard_pending_at_indexing_is_refused(store: KnowledgeStore) -> None:
    """Same point of no return for dropping a re-upload: once its chunks are
    being swapped in, the document is returned unchanged and READY follows."""
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    indexing = await _serving_with_reupload(store, tenant, base.id, "indexing.md")
    embedding = await _serving_with_reupload(store, tenant, base.id, "embedding.md")
    indexing_generation = await _at_stage(store, tenant, indexing.id, IngestStage.INDEXING)
    await _at_stage(store, tenant, embedding.id, IngestStage.EMBEDDING)

    refused = await store.discard_pending(tenant_id=tenant, document_id=indexing.id)
    dropped = await store.discard_pending(tenant_id=tenant, document_id=embedding.id)

    assert refused is not None
    assert (refused.status, refused.has_pending) == (DocumentStatus.PROCESSING, True)
    assert refused.ingest_generation == indexing.ingest_generation
    assert await store.set_document_status(
        tenant_id=tenant,
        document_id=indexing.id,
        status=DocumentStatus.READY,
        chunk_count=1,
        generation=indexing_generation,
    )
    assert await store.get_document_content(tenant_id=tenant, document_id=indexing.id) == (
        b"v2 bytes"
    )
    assert dropped is not None
    assert (dropped.status, dropped.has_pending) == (DocumentStatus.READY, False)


async def test_discard_pending_from_a_dead_worker_at_indexing_is_still_refused(
    store: KnowledgeStore,
) -> None:
    """Same reason: dropping the new version could leave its chunks serving
    under the old bytes (``ready``)."""
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await _serving_with_reupload(store, tenant, base.id, "a.md")
    await _at_stage(store, tenant, document.id, IngestStage.INDEXING, lease_expired=True)

    kept = await store.discard_pending(tenant_id=tenant, document_id=document.id)

    assert kept is not None
    assert (kept.status, kept.has_pending) == (DocumentStatus.PROCESSING, True)


def _row(
    tenant: UUID,
    kb_id: UUID,
    document_id: UUID,
    index: int,
    model: str | None,
    chunk_type: ChunkType = ChunkType.TEXT,
) -> KnowledgeChunk:
    parent = chunk_type is ChunkType.PARENT
    return KnowledgeChunk(
        id=uuid4(),
        tenant_id=tenant,
        kb_id=kb_id,
        document_id=document_id,
        chunk_index=index,
        content=f"passage {index}",
        chunk_type=chunk_type,
        embedding=None if parent else (1.0,) + (0.0,) * (EMBEDDING_DIM - 1),
        embedding_model=None if parent else model,
    )


async def test_has_chunks_off_model(store: KnowledgeStore) -> None:
    """Whether a base still serves a searchable chunk another model embedded —
    what a re-index checks before it stamps the base with the new model."""
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    other = await store.create_base(tenant_id=tenant, name="other")
    document = await _upload(store, tenant, base.id, "a.md")
    elsewhere = await _upload(store, tenant, other.id, "b.md")
    assert await store.replace_chunks(
        tenant_id=tenant,
        document_id=document.id,
        chunks=[
            _row(tenant, base.id, document.id, 0, None, ChunkType.PARENT),  # no vector
            _row(tenant, base.id, document.id, 1, "prov/new"),
        ],
    )
    assert await store.replace_chunks(
        tenant_id=tenant,
        document_id=elsewhere.id,
        chunks=[_row(tenant, other.id, elsewhere.id, 0, "prov/old")],
    )
    scope = {"tenant_id": tenant, "kb_id": base.id}

    assert not await store.has_chunks_off_model(**scope, embedding_model="prov/new")
    assert await store.has_chunks_off_model(**scope, embedding_model="prov/newer")
    assert await store.has_chunks_off_model(**scope, embedding_model=None)
    assert not await store.has_chunks_off_model(
        tenant_id=uuid4(), kb_id=base.id, embedding_model="prov/other"
    )
    assert await store.replace_chunks(
        tenant_id=tenant,
        document_id=document.id,
        chunks=[
            _row(tenant, base.id, document.id, 1, "prov/new"),
            _row(tenant, base.id, document.id, 2, "prov/old"),
        ],
    )
    assert await store.has_chunks_off_model(**scope, embedding_model="prov/new")
    # A searchable chunk with no model recorded counts as another model.
    assert await store.replace_chunks(
        tenant_id=tenant,
        document_id=document.id,
        chunks=[
            _row(tenant, base.id, document.id, 1, "prov/new"),
            _row(tenant, base.id, document.id, 2, None),
        ],
    )
    assert await store.has_chunks_off_model(**scope, embedding_model="prov/new")


async def test_cancelled_never_claimed(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    pending = await _upload(store, tenant, base.id, "pending.md")
    processing = await _upload(store, tenant, base.id, "processing.md")
    await _claim(store, tenant, processing.id)
    await store.cancel_documents(
        tenant_id=tenant, kb_id=base.id, document_ids=[pending.id, processing.id]
    )
    # Long after every lease would have expired.
    later = datetime.now(UTC) + timedelta(seconds=_LEASE_S * 10)

    for document in (pending, processing):
        assert (
            await store.claim_document(
                tenant_id=tenant,
                document_id=document.id,
                now=later,
                lease_seconds=_LEASE_S,
                max_attempts=_MAX_ATTEMPTS,
            )
            is None
        )
    claims = await store.claim_documents_for_ingest(
        now=later, lease_seconds=_LEASE_S, limit=10, max_attempts=_MAX_ATTEMPTS
    )
    assert not {claim.document_id for claim in claims} & {pending.id, processing.id}
    assert await store.sweep_terminal(now=later, max_attempts=0) == 0
    for document in (pending, processing):
        assert (await _get(store, tenant, document.id)).status is DocumentStatus.CANCELLED


async def test_upsert_resets_cancelled(store: KnowledgeStore) -> None:
    """Re-upload of the same bytes and an explicit re-ingest both queue a
    cancelled document again (the unchanged-bytes shortcut is for ``ready``)."""
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    first = await _upload(store, tenant, base.id, "a.md")
    second = await _upload(store, tenant, base.id, "b.md")
    await store.cancel_documents(
        tenant_id=tenant, kb_id=base.id, document_ids=[first.id, second.id]
    )
    cancelled_generation = (await _get(store, tenant, first.id)).ingest_generation

    reuploaded = await _upload(store, tenant, base.id, "a.md")
    reingested = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="b.md", force=True
    )

    for reset in (reuploaded, reingested):
        assert reset.status is DocumentStatus.PENDING
        assert reset.error is None
        assert reset.attempts == 0
    assert reuploaded.ingest_generation == cancelled_generation + 1
    claim = await store.claim_document(
        tenant_id=tenant,
        document_id=first.id,
        now=datetime.now(UTC),
        lease_seconds=_LEASE_S,
        max_attempts=_MAX_ATTEMPTS,
    )
    assert claim is not None
    assert claim.content == b"bytes of a.md"


@pytest.mark.integration
async def test_unknown_status_reads_as_failed(
    sql_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A status this version does not know (written by a newer release before
    a rollback) reads as ``failed`` with an explanation — never a 500."""
    store = SqlKnowledgeStore(sql_factory)
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await _upload(store, tenant, base.id, "a.md")
    async with sql_factory() as session:
        await session.execute(
            update(KnowledgeDocumentRow)
            .where(KnowledgeDocumentRow.id == document.id)
            # B-222 PR-2 made ``awaiting_confirmation`` a known status.
            .values(status="quarantined_by_a_newer_release")
        )
        await session.commit()

    read = await _get(store, tenant, document.id)
    listed = await store.list_documents(tenant_id=tenant, kb_id=base.id)

    assert read.status is DocumentStatus.FAILED
    assert read.error is not None
    assert "quarantined_by_a_newer_release" in read.error
    assert [d.status for d in listed] == [DocumentStatus.FAILED]


@pytest.mark.integration
async def test_processing_without_a_stage_is_cancellable(
    sql_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A row from before stages were recorded (``stage`` NULL) is not at the
    point of no return."""
    store = SqlKnowledgeStore(sql_factory)
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await _upload(store, tenant, base.id, "a.md")
    await _claim(store, tenant, document.id)
    async with sql_factory() as session:
        await session.execute(
            update(KnowledgeDocumentRow)
            .where(KnowledgeDocumentRow.id == document.id)
            .values(stage=None)
        )
        await session.commit()

    outcomes = await store.cancel_documents(
        tenant_id=tenant, kb_id=base.id, document_ids=[document.id]
    )

    assert outcomes == {document.id: "cancelled"}


@pytest.mark.integration
async def test_cancel_waits_for_the_indexing_write_and_then_refuses(
    sql_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The fenced stage write to ``indexing`` and the cancel serialise on the
    document row: a cancel that arrives while the write is in flight waits for
    it and then finds the document past the point of no return."""
    store = SqlKnowledgeStore(sql_factory)
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await _upload(store, tenant, base.id, "a.md")
    generation = await _claim(store, tenant, document.id)
    async with sql_factory() as writer:
        moved = await writer.execute(
            update(KnowledgeDocumentRow)
            .where(
                KnowledgeDocumentRow.id == document.id,
                KnowledgeDocumentRow.status == DocumentStatus.PROCESSING.value,
                KnowledgeDocumentRow.ingest_generation == generation,
            )
            .values(stage=IngestStage.INDEXING.value)
        )
        assert getattr(moved, "rowcount", 0) == 1
        cancel = asyncio.create_task(
            store.cancel_documents(tenant_id=tenant, kb_id=base.id, document_ids=[document.id])
        )
        await asyncio.sleep(0.5)
        assert not cancel.done()  # blocked on the row the stage write holds
        await writer.commit()

    assert await cancel == {document.id: "not_cancellable"}
    assert (await _get(store, tenant, document.id)).status is DocumentStatus.PROCESSING
