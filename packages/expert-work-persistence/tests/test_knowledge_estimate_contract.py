"""B-222 PR-2 — ingest estimates, both stores, one contract.

An upload (or a re-ingest) is estimated before anything is spent: the document
is ``estimating`` (claimed for the free estimate only), then
``awaiting_confirmation`` with its estimate and parse snapshot, and only a
confirmation (compare-and-set on the estimate id) queues it ``pending``. A
withdrawn estimate (cancelled, or expired) deletes a new upload and puts
anything else back as it was. Duplicates are judged within one base. What a
paid ingest cost is kept next to its estimate. The memory run goes into unit CI;
the SQL run is marked ``integration``.

Before running the SQL tests locally:
export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from testcontainers.postgres import PostgresContainer

from expert_work.persistence import (
    DatabaseConfig,
    InMemoryKnowledgeStore,
    SqlKnowledgeStore,
    create_async_engine_from_config,
    create_async_session_factory,
)
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge.base import KnowledgeStore, ParseSnapshot
from expert_work.persistence.knowledge.passages import ChunkVector
from expert_work.persistence.models import KnowledgeDocumentRow
from expert_work.protocol import (
    DocumentStatus,
    IngestErrorCode,
    IngestStage,
    KnowledgeChunk,
    KnowledgeDocument,
)
from expert_work.protocol.knowledge_estimate import (
    CONFIRM_WINDOW,
    SNAPSHOT_GRACE_AFTER_CONFIRM,
    ActualItem,
    EstimateModels,
    EstimateOrigin,
    IngestActual,
    IngestEstimate,
)

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"
_DB = "knowledge_estimate_contract"
_LEASE_S = 600
_MAX_ATTEMPTS = 5
_NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


@pytest.fixture(params=["memory", pytest.param("sql", marks=pytest.mark.integration)])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[KnowledgeStore]:
    if request.param == "memory":
        yield InMemoryKnowledgeStore()
        return
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
        yield SqlKnowledgeStore(create_async_session_factory(engine))
    finally:
        await engine.dispose()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{_DB}" WITH (FORCE)')


# -- helpers -----------------------------------------------------------------------


def _estimate(total: int = 120_000, *, embedding: str = "p/embed") -> dict[str, Any]:
    models = EstimateModels(embedding=embedding)
    return IngestEstimate(
        id=uuid4(),
        origin=EstimateOrigin.UPLOAD,  # the store keeps the document's own origin
        created_at=_NOW,
        total_micros=total,
        models=models,
        models_fingerprint=models.fingerprint(),
    ).model_dump(mode="json")


def _snapshot(text: str = "parsed") -> ParseSnapshot:
    return ParseSnapshot(format="text/1", payload=text.encode())


async def _lease_until(store: KnowledgeStore, document_id: UUID) -> datetime | None:
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


async def _get(store: KnowledgeStore, tenant: UUID, document_id: UUID) -> KnowledgeDocument:
    document = await store.get_document(tenant_id=tenant, document_id=document_id)
    assert document is not None
    return document


async def _upload(
    store: KnowledgeStore, tenant: UUID, kb_id: UUID, filename: str, sha: str | None = None
) -> KnowledgeDocument:
    started = await store.begin_upload(
        tenant_id=tenant,
        kb_id=kb_id,
        filename=filename,
        content=f"bytes of {filename}".encode(),
        content_sha256=sha or f"sha-{filename}",
        now=_NOW,
    )
    return started.document


async def _claim(store: KnowledgeStore, tenant: UUID, document_id: UUID) -> int:
    claim = await store.claim_document(
        tenant_id=tenant,
        document_id=document_id,
        now=datetime.now(UTC),
        lease_seconds=_LEASE_S,
        max_attempts=_MAX_ATTEMPTS,
    )
    assert claim is not None
    return claim.generation


async def _estimated(
    store: KnowledgeStore,
    tenant: UUID,
    document_id: UUID,
    *,
    total: int = 120_000,
    confirm_by: datetime | None = None,
) -> KnowledgeDocument:
    """Claim the document's estimate and finish it."""
    generation = await _claim(store, tenant, document_id)
    finished = await store.finish_estimate(
        tenant_id=tenant,
        document_id=document_id,
        generation=generation,
        estimate=_estimate(total),
        snapshot=_snapshot(),
        confirm_by=confirm_by or _NOW + CONFIRM_WINDOW,
    )
    assert finished is not None
    return finished


async def _confirm(store: KnowledgeStore, tenant: UUID, document: KnowledgeDocument) -> None:
    assert document.ingest_estimate is not None
    outcome = await store.confirm_documents(
        tenant_id=tenant,
        kb_id=document.kb_id,
        estimates=[(document.id, document.ingest_estimate.id)],
        confirmed_by="user-1",
        now=_NOW,
    )
    assert outcome == {document.id: "confirmed"}


async def _ready(
    store: KnowledgeStore, tenant: UUID, kb_id: UUID, filename: str, *, chunks: int = 1
) -> KnowledgeDocument:
    """A document ingested the old way (no estimate): READY with ``chunks`` chunks."""
    document = await store.upsert_document(
        tenant_id=tenant,
        kb_id=kb_id,
        filename=filename,
        content=b"v1 bytes",
        content_sha256=f"v1-{filename}",
    )
    generation = await _claim(store, tenant, document.id)
    rows = [
        KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant,
            kb_id=kb_id,
            document_id=document.id,
            chunk_index=index,
            content=f"v1 passage {index}",
            embedding=(1.0,) + (0.0,) * (EMBEDDING_DIM - 1),
            content_hash=f"h{index}",
            embedding_model="p/old",
        )
        for index in range(chunks)
    ]
    assert await store.replace_chunks(
        tenant_id=tenant, document_id=document.id, chunks=rows, generation=generation
    )
    assert await store.set_document_status(
        tenant_id=tenant,
        document_id=document.id,
        status=DocumentStatus.READY,
        chunk_count=chunks,
        generation=generation,
    )
    return await _get(store, tenant, document.id)


# -- uploads -----------------------------------------------------------------------


async def test_a_new_upload_is_estimated_first_and_claimed_for_the_estimate_only(
    store: KnowledgeStore,
) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    started = await store.begin_upload(
        tenant_id=tenant,
        kb_id=base.id,
        filename="a.md",
        content=b"alpha",
        content_sha256="sha-a",
        now=_NOW,
    )
    assert started.outcome == "estimating"
    document = started.document
    assert document.status is DocumentStatus.ESTIMATING
    assert document.has_content and not document.has_pending
    assert document.ingest_estimate is None  # not estimated yet
    assert document.confirm_by is None
    assert document.doc_seq is not None

    claim = await store.claim_document(
        tenant_id=tenant,
        document_id=document.id,
        now=datetime.now(UTC),
        lease_seconds=_LEASE_S,
        max_attempts=_MAX_ATTEMPTS,
    )
    assert claim is not None
    assert claim.phase == "estimate"
    assert claim.origin == "upload"
    assert claim.content == b"alpha"
    claimed = await _get(store, tenant, document.id)
    assert claimed.status is DocumentStatus.ESTIMATING  # never processing
    assert claimed.attempts == 1
    # Held while the lease lives; the recovery scan takes it once it lapses.
    assert (
        await store.claim_document(
            tenant_id=tenant,
            document_id=document.id,
            now=datetime.now(UTC),
            lease_seconds=_LEASE_S,
            max_attempts=_MAX_ATTEMPTS,
        )
        is None
    )
    later = datetime.now(UTC) + timedelta(seconds=_LEASE_S * 2)
    recovered = await store.claim_documents_for_ingest(
        now=later, lease_seconds=_LEASE_S, limit=10, max_attempts=_MAX_ATTEMPTS
    )
    assert [c.document_id for c in recovered] == [document.id]
    assert recovered[0].phase == "estimate"


async def test_a_document_awaiting_confirmation_is_never_claimed(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await _upload(store, tenant, base.id, "a.md")
    waiting = await _estimated(store, tenant, document.id)
    assert waiting.status is DocumentStatus.AWAITING_CONFIRMATION
    assert (
        await store.claim_document(
            tenant_id=tenant,
            document_id=document.id,
            now=datetime.now(UTC) + timedelta(days=30),
            lease_seconds=_LEASE_S,
            max_attempts=_MAX_ATTEMPTS,
        )
        is None
    )
    assert (
        await store.claim_documents_for_ingest(
            now=datetime.now(UTC) + timedelta(days=30),
            lease_seconds=_LEASE_S,
            limit=10,
            max_attempts=_MAX_ATTEMPTS,
        )
        == []
    )


async def test_the_same_bytes_again_change_nothing(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    ready = await _ready(store, tenant, base.id, "ready.md")
    again = await store.begin_upload(
        tenant_id=tenant,
        kb_id=base.id,
        filename="ready.md",
        content=b"v1 bytes",
        content_sha256="v1-ready.md",
        now=_NOW,
    )
    assert again.outcome == "unchanged"
    assert (await _get(store, tenant, ready.id)).ingest_generation == ready.ingest_generation

    waiting = await _upload(store, tenant, base.id, "w.md")
    waiting = await _estimated(store, tenant, waiting.id)
    twice = await store.begin_upload(
        tenant_id=tenant,
        kb_id=base.id,
        filename="w.md",
        content=b"bytes of w.md",
        content_sha256="sha-w.md",
        now=_NOW,
    )
    assert twice.outcome == "unchanged"
    after = await _get(store, tenant, waiting.id)
    assert after.status is DocumentStatus.AWAITING_CONFIRMATION
    assert after.ingest_estimate == waiting.ingest_estimate


async def test_a_reupload_waits_in_the_pending_slot_and_the_old_version_serves(
    store: KnowledgeStore,
) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    ready = await _ready(store, tenant, base.id, "doc.md")
    started = await store.begin_upload(
        tenant_id=tenant,
        kb_id=base.id,
        filename="doc.md",
        content=b"v2 bytes",
        content_sha256="v2",
        now=_NOW,
    )
    assert started.outcome == "estimating"
    document = started.document
    assert document.id == ready.id
    assert document.status is DocumentStatus.ESTIMATING
    assert document.has_pending
    assert document.ingest_generation == ready.ingest_generation + 1
    assert document.chunk_count == 1  # still serving
    claim = await store.claim_document(
        tenant_id=tenant,
        document_id=document.id,
        now=datetime.now(UTC),
        lease_seconds=_LEASE_S,
        max_attempts=_MAX_ATTEMPTS,
    )
    assert claim is not None
    assert claim.content == b"v2 bytes"
    assert claim.content_sha256 == "v2"
    assert claim.serving_sha256 == "v1-doc.md"
    assert claim.origin == "reupload"


async def test_duplicates_are_judged_within_one_base(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    other_base = await store.create_base(tenant_id=tenant, name="other")
    original = await _upload(store, tenant, base.id, "original.md", sha="same")

    copy = await store.begin_upload(
        tenant_id=tenant,
        kb_id=base.id,
        filename="copy.md",
        content=b"bytes",
        content_sha256="same",
        now=_NOW,
    )
    assert copy.outcome == "duplicate"
    assert copy.document.status is DocumentStatus.AWAITING_CONFIRMATION
    assert copy.document.confirm_by == _NOW + CONFIRM_WINDOW
    estimate = copy.document.ingest_estimate
    assert estimate is not None
    assert estimate.estimated is False
    assert estimate.total_micros == 0
    assert estimate.duplicate_of is not None
    assert estimate.duplicate_of.id == original.id
    assert estimate.duplicate_of.filename == "original.md"
    assert estimate.duplicate_of.doc_seq == original.doc_seq

    elsewhere = await store.begin_upload(
        tenant_id=tenant,
        kb_id=other_base.id,
        filename="copy.md",
        content=b"bytes",
        content_sha256="same",
        now=_NOW,
    )
    assert elsewhere.outcome == "estimating"

    # A new version waiting in a document's pending slot counts too.
    ready = await _ready(store, tenant, base.id, "ready.md")
    await store.begin_upload(
        tenant_id=tenant,
        kb_id=base.id,
        filename="ready.md",
        content=b"v2",
        content_sha256="pending-sha",
        now=_NOW,
    )
    third = await store.begin_upload(
        tenant_id=tenant,
        kb_id=base.id,
        filename="third.md",
        content=b"v2",
        content_sha256="pending-sha",
        now=_NOW,
    )
    assert third.outcome == "duplicate"
    third_estimate = third.document.ingest_estimate
    assert third_estimate is not None and third_estimate.duplicate_of is not None
    assert third_estimate.duplicate_of.id == ready.id


async def test_an_estimate_is_fenced_and_lands_with_its_snapshot(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await _upload(store, tenant, base.id, "a.md")
    generation = await _claim(store, tenant, document.id)
    estimate = _estimate(345)
    stale = await store.finish_estimate(
        tenant_id=tenant,
        document_id=document.id,
        generation=generation + 1,
        estimate=estimate,
        snapshot=_snapshot(),
        confirm_by=_NOW + CONFIRM_WINDOW,
    )
    assert stale is None
    assert (await _get(store, tenant, document.id)).status is DocumentStatus.ESTIMATING

    finished = await store.finish_estimate(
        tenant_id=tenant,
        document_id=document.id,
        generation=generation,
        estimate=estimate,
        snapshot=_snapshot("the parse"),
        confirm_by=_NOW + CONFIRM_WINDOW,
    )
    assert finished is not None
    assert finished.status is DocumentStatus.AWAITING_CONFIRMATION
    assert finished.confirm_by == _NOW + CONFIRM_WINDOW
    shown = finished.ingest_estimate
    assert shown is not None
    assert str(shown.id) == estimate["id"]
    assert shown.total_micros == 345
    assert shown.origin is EstimateOrigin.UPLOAD
    assert shown.duplicate_of is None
    assert await _lease_until(store, document.id) is None
    held = await store.get_parse_snapshot(
        tenant_id=tenant, document_id=document.id, estimate_id=shown.id
    )
    assert held == _snapshot("the parse")
    assert (
        await store.get_parse_snapshot(
            tenant_id=tenant, document_id=document.id, estimate_id=uuid4()
        )
        is None
    )
    assert (
        await store.get_parse_snapshot(
            tenant_id=uuid4(), document_id=document.id, estimate_id=shown.id
        )
        is None
    )
    # Another finish of the same estimate is refused (no longer estimating).
    assert (
        await store.finish_estimate(
            tenant_id=tenant,
            document_id=document.id,
            generation=generation,
            estimate=_estimate(1),
            snapshot=None,
            confirm_by=_NOW,
        )
        is None
    )


async def test_finishing_an_estimate_catches_a_duplicate_uploaded_at_the_same_time(
    store: KnowledgeStore,
) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    first = await _upload(store, tenant, base.id, "first.md", sha="same")
    # Queued the old way (no check on upload), created after ``first``.
    queued = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="late.md", content=b"x", content_sha256="same"
    )
    late = await store.begin_estimate(
        tenant_id=tenant, document_id=queued.id, origin=EstimateOrigin.REUPLOAD
    )
    assert late is not None

    first_done = await _estimated(store, tenant, first.id)
    assert first_done.ingest_estimate is not None
    assert first_done.ingest_estimate.duplicate_of is None  # the earlier one is not

    late_done = await _estimated(store, tenant, late.id)
    assert late_done.ingest_estimate is not None
    assert late_done.ingest_estimate.duplicate_of is not None
    assert late_done.ingest_estimate.duplicate_of.id == first.id
    assert late_done.ingest_estimate.estimated  # it has a real estimate

    # The user wants it anyway: estimated again, not flagged again.
    anyway = await store.begin_estimate(
        tenant_id=tenant,
        document_id=late.id,
        origin=EstimateOrigin.REUPLOAD,
        allow_duplicate=True,
    )
    assert anyway is not None
    again = await _estimated(store, tenant, late.id)
    assert again.ingest_estimate is not None
    assert again.ingest_estimate.duplicate_of is None
    assert again.ingest_estimate.allow_duplicate


# -- confirming --------------------------------------------------------------------


async def test_confirming_is_a_compare_and_set_on_the_estimate(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    other_base = await store.create_base(tenant_id=tenant, name="other")
    waiting = await _estimated(store, tenant, (await _upload(store, tenant, base.id, "a.md")).id)
    estimating = await _upload(store, tenant, base.id, "b.md")
    await _upload(store, tenant, base.id, "orig.md", sha="dup")
    duplicate = await _upload(store, tenant, base.id, "dup.md", sha="dup")
    assert waiting.ingest_estimate is not None

    outcomes = await store.confirm_documents(
        tenant_id=tenant,
        kb_id=base.id,
        estimates=[
            (waiting.id, uuid4()),
            (estimating.id, uuid4()),
            (duplicate.id, duplicate.ingest_estimate.id if duplicate.ingest_estimate else uuid4()),
            (uuid4(), uuid4()),
        ],
        confirmed_by="user-1",
        now=_NOW,
    )
    assert list(outcomes.values()) == ["estimate_changed", "not_awaiting", "duplicate", "not_found"]
    assert (await _get(store, tenant, waiting.id)).status is DocumentStatus.AWAITING_CONFIRMATION
    assert await store.confirm_documents(
        tenant_id=tenant,
        kb_id=other_base.id,
        estimates=[(waiting.id, waiting.ingest_estimate.id)],
        confirmed_by="user-1",
        now=_NOW,
    ) == {waiting.id: "not_found"}

    assert await store.confirm_documents(
        tenant_id=tenant,
        kb_id=base.id,
        estimates=[(waiting.id, waiting.ingest_estimate.id)],
        confirmed_by="user-1",
        now=_NOW,
    ) == {waiting.id: "confirmed"}
    queued = await _get(store, tenant, waiting.id)
    assert queued.status is DocumentStatus.PENDING
    assert queued.ingest_generation == waiting.ingest_generation + 1
    assert queued.attempts == 0
    assert queued.confirm_by is None
    assert queued.ingest_estimate is not None
    assert queued.ingest_estimate.id == waiting.ingest_estimate.id
    assert queued.ingest_estimate.confirmed_at == _NOW
    assert queued.ingest_estimate.confirmed_by == "user-1"
    claim = await store.claim_document(
        tenant_id=tenant,
        document_id=waiting.id,
        now=datetime.now(UTC),
        lease_seconds=_LEASE_S,
        max_attempts=_MAX_ATTEMPTS,
    )
    assert claim is not None
    assert claim.phase == "ingest"
    assert claim.estimate is not None and claim.estimate.id == waiting.ingest_estimate.id
    # The snapshot is kept one more day for a crashed ingest to retry from.
    estimate_id = waiting.ingest_estimate.id
    kept = _NOW + SNAPSHOT_GRACE_AFTER_CONFIRM
    assert await store.purge_expired_snapshots(now=kept - timedelta(seconds=1)) == 0
    assert await store.get_parse_snapshot(
        tenant_id=tenant, document_id=waiting.id, estimate_id=estimate_id
    )
    assert await store.purge_expired_snapshots(now=kept + timedelta(seconds=1)) == 1
    assert (
        await store.get_parse_snapshot(
            tenant_id=tenant, document_id=waiting.id, estimate_id=estimate_id
        )
        is None
    )


async def test_confirm_all_only_when_the_base_still_adds_up(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    a = await _estimated(
        store, tenant, (await _upload(store, tenant, base.id, "a.md")).id, total=100
    )
    b = await _estimated(
        store, tenant, (await _upload(store, tenant, base.id, "b.md")).id, total=200
    )
    await _upload(store, tenant, base.id, "orig.md", sha="dup")  # still estimating
    duplicate = await _upload(store, tenant, base.id, "dup.md", sha="dup")

    summary = await store.estimate_summary(tenant_id=tenant, kb_id=base.id)
    assert (summary.estimating, summary.awaiting, summary.duplicates) == (1, 2, 1)
    assert summary.total_micros == 300
    assert summary.earliest_confirm_by == _NOW + CONFIRM_WINDOW

    stale = await store.confirm_all(
        tenant_id=tenant,
        kb_id=base.id,
        expected_count=2,
        expected_total_micros=299,
        confirmed_by="user-1",
        now=_NOW,
        withdraw_duplicates=[duplicate.id],
    )
    assert not stale.matched
    assert stale.summary == summary
    assert (await _get(store, tenant, a.id)).status is DocumentStatus.AWAITING_CONFIRMATION
    assert (await _get(store, tenant, duplicate.id)).status is DocumentStatus.AWAITING_CONFIRMATION
    # A duplicate that arrives after the user was shown the others.
    late = await _upload(store, tenant, base.id, "late.md", sha="dup")

    done = await store.confirm_all(
        tenant_id=tenant,
        kb_id=base.id,
        expected_count=2,
        expected_total_micros=300,
        confirmed_by="user-1",
        now=_NOW,
        withdraw_duplicates=[duplicate.id, a.id],
    )
    assert done.matched
    assert set(done.confirmed) == {a.id, b.id}
    # Only the duplicate the user saw is withdrawn (``a`` is not a duplicate).
    assert dict(done.withdrawn) == {duplicate.id: "withdrawn_deleted"}
    assert (await _get(store, tenant, a.id)).status is DocumentStatus.PENDING
    assert (await _get(store, tenant, b.id)).status is DocumentStatus.PENDING
    assert await store.get_document(tenant_id=tenant, document_id=duplicate.id) is None
    assert (await _get(store, tenant, late.id)).status is DocumentStatus.AWAITING_CONFIRMATION


async def test_an_estimate_past_its_deadline_is_never_confirmed(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    due = _NOW - timedelta(seconds=1)
    late = await _estimated(
        store, tenant, (await _upload(store, tenant, base.id, "a.md")).id, total=100, confirm_by=due
    )
    assert late.ingest_estimate is not None
    by_list = await store.confirm_documents(
        tenant_id=tenant,
        kb_id=base.id,
        estimates=[(late.id, late.ingest_estimate.id)],
        confirmed_by="user-1",
        now=_NOW,
    )
    assert by_list == {late.id: "expired"}
    at_once = await store.confirm_all(
        tenant_id=tenant,
        kb_id=base.id,
        expected_count=1,
        expected_total_micros=100,
        confirmed_by="user-1",
        now=_NOW,
    )
    assert at_once.matched and at_once.confirmed == ()
    assert (await _get(store, tenant, late.id)).status is DocumentStatus.AWAITING_CONFIRMATION


# -- withdrawing -------------------------------------------------------------------


async def test_withdrawing_a_new_upload_deletes_it(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    estimating = await _upload(store, tenant, base.id, "a.md")
    waiting = await _estimated(store, tenant, (await _upload(store, tenant, base.id, "b.md")).id)
    assert waiting.ingest_estimate is not None
    outcomes = await store.cancel_documents(
        tenant_id=tenant, kb_id=base.id, document_ids=[estimating.id, waiting.id]
    )
    assert outcomes == {estimating.id: "withdrawn_deleted", waiting.id: "withdrawn_deleted"}
    for document_id in (estimating.id, waiting.id):
        assert await store.get_document(tenant_id=tenant, document_id=document_id) is None
    assert (
        await store.get_parse_snapshot(
            tenant_id=tenant, document_id=waiting.id, estimate_id=waiting.ingest_estimate.id
        )
        is None
    )
    # The name is free again: a new upload is a new document.
    again = await _upload(store, tenant, base.id, "a.md")
    assert again.id != estimating.id


async def test_withdrawing_a_reupload_keeps_the_serving_version(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    ready = await _ready(store, tenant, base.id, "doc.md")
    started = await store.begin_upload(
        tenant_id=tenant,
        kb_id=base.id,
        filename="doc.md",
        content=b"v2",
        content_sha256="v2",
        now=_NOW,
    )
    waiting = await _estimated(store, tenant, started.document.id)
    outcome = await store.cancel_documents(tenant_id=tenant, kb_id=base.id, document_ids=[ready.id])
    assert outcome == {ready.id: "withdrawn"}
    after = await _get(store, tenant, ready.id)
    assert after.status is DocumentStatus.READY
    assert after.stage is IngestStage.READY
    assert not after.has_pending
    assert after.chunk_count == 1
    assert after.ingest_generation == waiting.ingest_generation + 1
    assert after.ingest_estimate is None  # it had none before
    assert after.confirm_by is None
    content = await store.get_document_content(tenant_id=tenant, document_id=ready.id)
    assert content == b"v1 bytes"

    # Nothing serving (a first upload that failed): failed again, saying so.
    failed = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="f.md", content=b"f1", content_sha256="f1"
    )
    failed_generation = await _claim(store, tenant, failed.id)
    assert await store.mark_document_failed_terminal(
        tenant_id=tenant, document_id=failed.id, error="boom", generation=failed_generation
    )
    await store.begin_upload(
        tenant_id=tenant,
        kb_id=base.id,
        filename="f.md",
        content=b"f2",
        content_sha256="f2",
        now=_NOW,
    )
    assert await store.cancel_documents(
        tenant_id=tenant, kb_id=base.id, document_ids=[failed.id]
    ) == {failed.id: "withdrawn"}
    back = await _get(store, tenant, failed.id)
    assert back.status is DocumentStatus.FAILED
    assert back.error == "pending upload discarded; nothing is serving"
    assert not back.has_pending


async def test_a_withdrawn_reupload_leaves_no_bytes_behind(store: KnowledgeStore) -> None:
    """Nothing serving and no bytes kept (a legacy row): the re-upload's bytes
    still wait in the pending slot, so withdrawing it leaves none of them."""
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    legacy = await store.upsert_document(tenant_id=tenant, kb_id=base.id, filename="l.md")
    generation = await _claim(store, tenant, legacy.id)
    assert await store.mark_document_failed_terminal(
        tenant_id=tenant, document_id=legacy.id, error="no bytes", generation=generation
    )
    started = await store.begin_upload(
        tenant_id=tenant,
        kb_id=base.id,
        filename="l.md",
        content=b"new",
        content_sha256="new",
        now=_NOW,
    )
    assert (started.document.has_pending, started.document.has_content) == (True, False)
    assert await store.cancel_documents(
        tenant_id=tenant, kb_id=base.id, document_ids=[legacy.id]
    ) == {legacy.id: "withdrawn"}
    back = await _get(store, tenant, legacy.id)
    assert (back.has_pending, back.has_content) == (False, False)
    assert await store.get_document_content(tenant_id=tenant, document_id=legacy.id) is None


@pytest.mark.parametrize("held", [DocumentStatus.BLOCKED, DocumentStatus.DISABLED])
async def test_a_withdrawn_reupload_of_a_held_back_document_stays_held_back(
    store: KnowledgeStore, held: DocumentStatus
) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    ready = await _ready(store, tenant, base.id, "doc.md")
    assert await store.set_document_status(tenant_id=tenant, document_id=ready.id, status=held)
    await store.begin_upload(
        tenant_id=tenant,
        kb_id=base.id,
        filename="doc.md",
        content=b"v2",
        content_sha256="v2",
        now=_NOW,
    )
    assert await store.cancel_documents(
        tenant_id=tenant, kb_id=base.id, document_ids=[ready.id]
    ) == {ready.id: "withdrawn"}
    back = await _get(store, tenant, ready.id)
    assert (back.status, back.has_pending, back.chunk_count) == (held, False, 1)


async def test_discarding_a_waiting_reupload_drops_its_estimate(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    ready = await _ready(store, tenant, base.id, "doc.md")
    await store.begin_upload(
        tenant_id=tenant,
        kb_id=base.id,
        filename="doc.md",
        content=b"v2",
        content_sha256="v2",
        now=_NOW,
    )
    waiting = await _estimated(store, tenant, ready.id)
    assert waiting.ingest_estimate is not None and waiting.confirm_by is not None

    discarded = await store.discard_pending(tenant_id=tenant, document_id=ready.id)

    assert discarded is not None
    assert (discarded.status, discarded.has_pending) == (DocumentStatus.READY, False)
    after = await _get(store, tenant, ready.id)
    assert (after.ingest_estimate, after.confirm_by) == (None, None)
    assert (
        await store.get_parse_snapshot(
            tenant_id=tenant, document_id=ready.id, estimate_id=waiting.ingest_estimate.id
        )
        is None
    )


async def test_withdrawing_a_reingest_puts_the_document_back_exactly(
    store: KnowledgeStore,
) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="a.md", content=b"a", content_sha256="a"
    )
    generation = await _claim(store, tenant, document.id)
    assert await store.set_document_stage(
        tenant_id=tenant, document_id=document.id, stage=IngestStage.PARSING, generation=generation
    )
    assert await store.mark_document_failed_terminal(
        tenant_id=tenant,
        document_id=document.id,
        error="could not parse",
        error_code=IngestErrorCode.PARSE_FAILED,
        generation=generation,
    )
    failed = await _get(store, tenant, document.id)
    started = await store.begin_estimate(
        tenant_id=tenant, document_id=document.id, origin=EstimateOrigin.REINGEST
    )
    assert started is not None
    assert started.status is DocumentStatus.ESTIMATING
    assert started.error is None and started.error_code is None
    await _estimated(store, tenant, document.id)
    # Re-ingested again while waiting: still put back to what it was first.
    await store.begin_estimate(
        tenant_id=tenant, document_id=document.id, origin=EstimateOrigin.RECOGNIZE
    )

    assert await store.cancel_documents(
        tenant_id=tenant, kb_id=base.id, document_ids=[document.id]
    ) == {document.id: "withdrawn"}
    back = await _get(store, tenant, document.id)
    assert back.status is DocumentStatus.FAILED
    assert back.error == "could not parse"
    assert back.error_code is IngestErrorCode.PARSE_FAILED
    assert back.stage is IngestStage.PARSING
    assert back.ingest_generation > failed.ingest_generation
    assert back.ingest_estimate is None


async def test_a_reingest_of_an_ingest_under_way_is_withdrawn_as_cancelled(
    store: KnowledgeStore,
) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="a.md", content=b"a", content_sha256="a"
    )
    await _claim(store, tenant, document.id)  # processing
    await store.begin_estimate(
        tenant_id=tenant, document_id=document.id, origin=EstimateOrigin.REINGEST
    )
    assert await store.cancel_documents(
        tenant_id=tenant, kb_id=base.id, document_ids=[document.id]
    ) == {document.id: "withdrawn"}
    back = await _get(store, tenant, document.id)
    assert back.status is DocumentStatus.CANCELLED
    assert back.error == "cancelled by user"


async def test_a_withdrawal_brings_the_previous_estimate_back(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    first = await _estimated(store, tenant, (await _upload(store, tenant, base.id, "a.md")).id)
    await _confirm(store, tenant, first)
    generation = await _claim(store, tenant, first.id)
    assert await store.set_document_status(
        tenant_id=tenant,
        document_id=first.id,
        status=DocumentStatus.READY,
        chunk_count=0,
        generation=generation,
    )
    ingested = await _get(store, tenant, first.id)
    assert ingested.ingest_estimate is not None
    await store.begin_estimate(
        tenant_id=tenant, document_id=first.id, origin=EstimateOrigin.REINGEST
    )
    await _estimated(store, tenant, first.id, total=999)
    await store.cancel_documents(tenant_id=tenant, kb_id=base.id, document_ids=[first.id])
    back = await _get(store, tenant, first.id)
    assert back.status is DocumentStatus.READY
    assert back.ingest_estimate == ingested.ingest_estimate


# -- going back to the user, expiring ------------------------------------------------


async def test_an_ingest_returns_to_confirmation_only_from_its_own_claim(
    store: KnowledgeStore,
) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    waiting = await _estimated(store, tenant, (await _upload(store, tenant, base.id, "a.md")).id)
    await _confirm(store, tenant, waiting)
    generation = await _claim(store, tenant, waiting.id)
    fresh = _estimate(777)
    assert not await store.return_to_confirmation(
        tenant_id=tenant,
        document_id=waiting.id,
        generation=generation + 1,
        estimate=fresh,
        snapshot=_snapshot("again"),
        confirm_by=_NOW + CONFIRM_WINDOW,
    )
    assert await store.return_to_confirmation(
        tenant_id=tenant,
        document_id=waiting.id,
        generation=generation,
        estimate=fresh,
        snapshot=_snapshot("again"),
        confirm_by=_NOW + CONFIRM_WINDOW,
    )
    back = await _get(store, tenant, waiting.id)
    assert back.status is DocumentStatus.AWAITING_CONFIRMATION
    assert back.ingest_estimate is not None
    assert str(back.ingest_estimate.id) == fresh["id"]
    assert back.ingest_estimate.origin is EstimateOrigin.UPLOAD
    assert back.confirm_by == _NOW + CONFIRM_WINDOW
    assert await _lease_until(store, waiting.id) is None
    assert await store.get_parse_snapshot(
        tenant_id=tenant, document_id=waiting.id, estimate_id=back.ingest_estimate.id
    ) == _snapshot("again")
    # The estimate meta survived: withdrawing still deletes the new upload.
    assert await store.cancel_documents(
        tenant_id=tenant, kb_id=base.id, document_ids=[waiting.id]
    ) == {waiting.id: "withdrawn_deleted"}


async def test_estimates_past_their_deadline_are_withdrawn(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    ready = await _ready(store, tenant, base.id, "r.md")
    await store.begin_upload(
        tenant_id=tenant,
        kb_id=base.id,
        filename="r.md",
        content=b"v2",
        content_sha256="v2",
        now=_NOW,
    )
    overdue_reupload = await _estimated(store, tenant, ready.id, confirm_by=_NOW)
    overdue_new = await _estimated(
        store, tenant, (await _upload(store, tenant, base.id, "n.md")).id, confirm_by=_NOW
    )
    on_time = await _estimated(
        store,
        tenant,
        (await _upload(store, tenant, base.id, "t.md")).id,
        confirm_by=_NOW + timedelta(days=1),
    )

    expired = await store.expire_awaiting(now=_NOW + timedelta(seconds=1), limit=10)

    assert {(e.document_id, e.outcome) for e in expired} == {
        (overdue_reupload.id, "withdrawn"),
        (overdue_new.id, "withdrawn_deleted"),
    }
    first = next(e for e in expired if e.document_id == overdue_new.id)
    assert (first.tenant_id, first.kb_id, first.filename) == (tenant, base.id, "n.md")
    assert (await _get(store, tenant, ready.id)).status is DocumentStatus.READY
    assert (await _get(store, tenant, on_time.id)).status is DocumentStatus.AWAITING_CONFIRMATION
    assert await store.expire_awaiting(now=_NOW + timedelta(seconds=1), limit=10) == []


async def test_concurrent_expiry_sweeps_withdraw_each_estimate_once(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    due = [
        await _estimated(
            store, tenant, (await _upload(store, tenant, base.id, f"{i}.md")).id, confirm_by=_NOW
        )
        for i in range(6)
    ]
    sweeps = await asyncio.gather(
        *(store.expire_awaiting(now=_NOW + timedelta(seconds=1), limit=3) for _ in range(4))
    )
    withdrawn = [e.document_id for sweep in sweeps for e in sweep]
    assert sorted(withdrawn) == sorted(d.id for d in due)


# -- workers ------------------------------------------------------------------------


async def test_an_estimate_renews_its_lease_and_fails_but_writes_no_stage(
    store: KnowledgeStore,
) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await _upload(store, tenant, base.id, "a.md")
    generation = await _claim(store, tenant, document.id)
    assert await store.renew_lease(
        tenant_id=tenant,
        document_id=document.id,
        generation=generation,
        now=datetime.now(UTC),
        lease_seconds=_LEASE_S,
    )
    # Nothing of the paid pipeline runs in an estimate.
    assert not await store.set_document_stage(
        tenant_id=tenant, document_id=document.id, stage=IngestStage.PARSING, generation=generation
    )
    assert not await store.mark_document_failed_terminal(
        tenant_id=tenant, document_id=document.id, error="x", generation=generation + 1
    )
    assert await store.mark_document_failed_terminal(
        tenant_id=tenant,
        document_id=document.id,
        error="text too large",
        error_code=IngestErrorCode.TEXT_TOO_LARGE,
        generation=generation,
    )
    failed = await _get(store, tenant, document.id)
    assert failed.status is DocumentStatus.FAILED
    assert failed.error_code is IngestErrorCode.TEXT_TOO_LARGE
    assert await _lease_until(store, document.id) is None


async def test_an_estimate_that_keeps_failing_is_swept(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    document = await _upload(store, tenant, base.id, "a.md")
    start = datetime.now(UTC)
    for attempt in range(_MAX_ATTEMPTS):
        # Each estimate's worker dies: its one-second lease lapses before the next scan.
        later = start + timedelta(seconds=10 * (attempt + 1))
        assert await store.claim_documents_for_ingest(
            now=later, lease_seconds=1, limit=1, max_attempts=_MAX_ATTEMPTS
        )
    swept = await store.sweep_terminal(
        now=start + timedelta(seconds=10 * (_MAX_ATTEMPTS + 1)), max_attempts=_MAX_ATTEMPTS
    )
    assert swept == 1
    failed = await _get(store, tenant, document.id)
    assert failed.status is DocumentStatus.FAILED
    assert failed.error_code is IngestErrorCode.ATTEMPTS_EXHAUSTED


async def test_queueing_the_old_way_drops_the_estimate(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    waiting = await _estimated(store, tenant, (await _upload(store, tenant, base.id, "a.md")).id)
    assert waiting.ingest_estimate is not None
    queued = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="a.md", force=True
    )
    assert queued.status is DocumentStatus.PENDING
    assert queued.ingest_estimate is None
    assert queued.confirm_by is None
    assert (
        await store.get_parse_snapshot(
            tenant_id=tenant, document_id=waiting.id, estimate_id=waiting.ingest_estimate.id
        )
        is None
    )


# -- what it cost --------------------------------------------------------------------


def _actual(generation: int, estimate_id: UUID | None, micros: int, tokens: int) -> dict[str, Any]:
    return IngestActual(
        estimate_id=estimate_id,
        generation=generation,
        total_micros=micros,
        items=(ActualItem(provider="p", model="embed", input_tokens=tokens, micros=micros),),
        outcome="ready",
        finished_at=_NOW,
    ).model_dump(mode="json")


async def test_what_an_ingest_cost_is_kept_with_its_estimate(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    waiting = await _estimated(store, tenant, (await _upload(store, tenant, base.id, "a.md")).id)
    assert waiting.ingest_estimate is not None
    estimate_id = waiting.ingest_estimate.id
    await _confirm(store, tenant, waiting)
    generation = await _claim(store, tenant, waiting.id)

    assert not await store.record_ingest_actual(
        tenant_id=tenant,
        document_id=waiting.id,
        generation=generation,
        estimate_id=uuid4(),
        actual=_actual(generation, uuid4(), 5, 50),
        settled=False,
    )
    assert await store.record_ingest_actual(
        tenant_id=tenant,
        document_id=waiting.id,
        generation=generation,
        estimate_id=estimate_id,
        actual=_actual(generation, estimate_id, 10, 100),
        settled=False,
    )
    # A retry of the same ingest adds up.
    assert await store.record_ingest_actual(
        tenant_id=tenant,
        document_id=waiting.id,
        generation=generation,
        estimate_id=estimate_id,
        actual=_actual(generation, estimate_id, 7, 70),
        settled=True,
    )
    actual = (await _get(store, tenant, waiting.id)).ingest_actual
    assert actual is not None
    assert actual.total_micros == 17
    assert [(i.model, i.input_tokens, i.micros) for i in actual.items] == [("embed", 170, 17)]
    assert actual.estimate_id == estimate_id
    # Settled: its snapshot is gone.
    assert (
        await store.get_parse_snapshot(
            tenant_id=tenant, document_id=waiting.id, estimate_id=estimate_id
        )
        is None
    )


async def test_a_cancelled_ingest_keeps_what_it_cost_a_superseded_one_does_not(
    store: KnowledgeStore,
) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    cancelled = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="c.md", content=b"c", content_sha256="c"
    )
    generation = await _claim(store, tenant, cancelled.id)
    await store.cancel_documents(tenant_id=tenant, kb_id=base.id, document_ids=[cancelled.id])
    assert await store.record_ingest_actual(
        tenant_id=tenant,
        document_id=cancelled.id,
        generation=generation,
        estimate_id=None,
        actual=_actual(generation, None, 3, 30),
        settled=True,
    )
    assert (await _get(store, tenant, cancelled.id)).ingest_actual is not None

    superseded = await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="s.md", content=b"s", content_sha256="s"
    )
    old = await _claim(store, tenant, superseded.id)
    await store.upsert_document(
        tenant_id=tenant, kb_id=base.id, filename="s.md", content=b"s2", content_sha256="s2"
    )
    assert not await store.record_ingest_actual(
        tenant_id=tenant,
        document_id=superseded.id,
        generation=old,
        estimate_id=None,
        actual=_actual(old, None, 3, 30),
        settled=True,
    )
    assert (await _get(store, tenant, superseded.id)).ingest_actual is None


# -- reads ------------------------------------------------------------------------------


async def test_hashes_on_a_model_are_read_without_vectors(store: KnowledgeStore) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    ready = await _ready(store, tenant, base.id, "a.md", chunks=3)
    other = await _ready(store, tenant, base.id, "b.md", chunks=3)
    found = await store.chunk_hashes_on_model(
        tenant_id=tenant,
        document_id=ready.id,
        content_hashes=["h0", "h2", "h9"],
        embedding_model="p/old",
    )
    assert found == {"h0", "h2"}
    assert (
        await store.chunk_hashes_on_model(
            tenant_id=tenant,
            document_id=ready.id,
            content_hashes=["h0"],
            embedding_model="p/new",
        )
        == set()
    )
    assert (
        await store.chunk_hashes_on_model(
            tenant_id=uuid4(), document_id=other.id, content_hashes=["h0"], embedding_model="p/old"
        )
        == set()
    )


async def test_a_reindex_write_back_lands_while_an_estimate_finishes(
    store: KnowledgeStore,
) -> None:
    tenant = uuid4()
    base = await store.create_base(tenant_id=tenant, name="kb")
    ready = await _ready(store, tenant, base.id, "a.md")
    estimating = await store.begin_estimate(
        tenant_id=tenant, document_id=ready.id, origin=EstimateOrigin.REINGEST
    )
    assert estimating is not None
    chunks, _ = await store.list_chunks(tenant_id=tenant, document_id=ready.id)
    vector = (0.0, 1.0) + (0.0,) * (EMBEDDING_DIM - 2)

    async def write(expect_status: DocumentStatus) -> int | None:
        return await store.update_chunk_embeddings(
            tenant_id=tenant,
            document_id=ready.id,
            vectors=[ChunkVector(chunks[0].id, chunks[0].content_hash, vector)],
            embedding_model="p/new",
            generation=estimating.ingest_generation,
            expect_status=expect_status,
        )

    await _estimated(store, tenant, ready.id)  # estimating → awaiting, same generation
    written = await write(DocumentStatus.ESTIMATING)
    assert written == 1
    waiting = await _get(store, tenant, ready.id)
    assert waiting.ingest_estimate is not None
    await _confirm(store, tenant, waiting)  # generation bumped: the write-back is refused
    stale_estimating = await write(DocumentStatus.ESTIMATING)
    stale_pending = await write(DocumentStatus.PENDING)
    assert stale_estimating is None
    assert stale_pending is None
