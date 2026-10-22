"""``knowledge_*`` ORM models — Stream J.5 RAG.

A tenant's ``knowledge_base`` owns ``knowledge_document`` rows (uploaded
source files); each document owns ``knowledge_chunk`` rows (embedded
slices). All three are tenant-scoped only — RLS (migration ``0021``)
enforces ``app.tenant_id``. Originally there were no foreign keys between
the tables (a FK into a ``FORCE`` RLS table is a footgun, Mini-ADR J-1a);
the ``KnowledgeStore`` cascades deletes in the application layer. Migration
``0136`` added ``knowledge_chunk.document_id → knowledge_document.id``
(ON DELETE CASCADE) as a DB-level backstop against the delete/in-flight-
ingest race — the app-layer cascade remains the primary path.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from expert_work.persistence.base import Base
from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.protocol import DEFAULT_CHUNK_MAX_TOKENS, DEFAULT_CHUNK_OVERLAP_TOKENS


class KnowledgeBaseRow(Base):
    """One ``knowledge_base`` — a named, tenant-scoped document collection."""

    __tablename__ = "knowledge_base"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_max_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(DEFAULT_CHUNK_MAX_TOKENS))
    )
    chunk_overlap_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text(str(DEFAULT_CHUNK_OVERLAP_TOKENS))
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Subject id of the creator. Nullable: rows predating migration ``0100``
    #: have no recorded creator.
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Per-base retrieval defaults — surfaced so they are not hardcoded.
    retrieval_top_k: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    retrieval_score_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    retrieval_method: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'hybrid'")
    )
    rerank_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    #: B-175 wave 2 PR3 — minimum rerank score to keep a hit (NULL = off).
    rerank_score_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: B-206 K-7b (migration ``0171``) — the rerank model (``provider/model``) the
    #: threshold was set under; a search scored by another model leaves it out.
    rerank_threshold_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Embedding model that produced this base's vectors (captured at create).
    #: Compared against the live platform model to derive ``needs_reindex``.
    embedding_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Set when a re-index is requested; the recovery worker re-embeds the
    #: base's retained chunk text with the current model, then clears it.
    reindex_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Model v2 (migration ``0165``): base kind, declared metadata fields,
    #: open-API switch.
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'document'"))
    metadata_schema: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    api_access_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="knowledge_base_identity_uniq"),)


class KnowledgeDocumentRow(Base):
    """One ``knowledge_document`` — an ingested source file.

    ``(tenant_id, kb_id, filename)`` is unique — re-uploading the same
    filename updates the document in place.
    """

    __tablename__ = "knowledge_document"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    kb_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: Ingestion durability (migration ``0101``). A document is CAS-claimed for
    #: ingest; ``lease_until`` lets a reaper reclaim crashed/expired work and
    #: ``attempts`` bounds retries. ``content`` retains the original bytes so a
    #: failed/crashed/re-ingested document can be re-driven without re-upload.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: Re-ingest generation (migration ``0164``) — bumped by ``upsert_document``
    #: on every reset; fenced write-backs carry the generation they claimed.
    ingest_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The new version being ingested (migration ``0164``). ``content`` keeps
    #: the version whose chunks are serving; a successful ingest promotes the
    #: pending bytes into it, a failed one leaves both untouched.
    pending_content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    pending_content_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Model v2 (migration ``0165``). ``doc_seq`` comes from the per-tenant
    #: ``knowledge_doc_seq`` counter on insert and survives re-uploads.
    doc_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'file'"))
    source_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: ``metadata`` is reserved on a declarative class — the attribute is
    #: ``doc_metadata``, the column keeps the plain name.
    doc_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: B-209 (migration ``0169``) — one record per PDF page (how its text was
    #: obtained, recognition model and outcome) and their counts. The records
    #: can run to a thousand entries: deferred, read only by the page view.
    page_records: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True, deferred=True
    )
    page_stats: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mime: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "kb_id", "filename", name="knowledge_document_identity_uniq"),
        Index(
            "knowledge_document_recovery_idx",
            "lease_until",
            postgresql_where=text("status IN ('pending', 'processing')"),
        ),
        UniqueConstraint("tenant_id", "doc_seq", name="knowledge_document_doc_seq_uniq"),
        Index(
            "knowledge_document_external_id_uniq",
            "tenant_id",
            "kb_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )


class KnowledgeChunkRow(Base):
    """One ``knowledge_chunk`` — an embedded slice of a document."""

    __tablename__ = "knowledge_chunk"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    kb_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    #: Full-text vector for the keyword side of hybrid search — populated
    #: by the store from jieba-segmented ``content`` (Stream J.5).
    content_tsv: Mapped[str | None] = mapped_column(TSVECTOR(), nullable=True)
    #: Model v2 (migration ``0165``). ``content`` is the clean body;
    #: ``heading_path`` holds the section breadcrumb that the embedding /
    #: keyword text prepend (``chunk_retrieval_text``).
    chunk_type: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'text'"))
    parent_chunk_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    #: ``none_as_null`` — Python ``None`` must be SQL NULL, not a JSON ``null``
    #: scalar (array functions on a scalar raise; see migration 0165).
    heading_path: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    #: The document's title at ingest (migration ``0167``) — prefixed to the
    #: retrieval text and indexed apart (weight A) in ``content_tsv``.
    #: ``NULL`` for chunks ingested before it was stored.
    doc_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_locator: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    edited: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("knowledge_chunk_kb_idx", "tenant_id", "kb_id"),
        Index("knowledge_chunk_document_idx", "document_id"),
        Index("knowledge_chunk_tsv_idx", "content_tsv", postgresql_using="gin"),
        UniqueConstraint("document_id", "chunk_index", name="knowledge_chunk_document_index_uniq"),
    )


class KnowledgeParseCacheRow(Base):
    """``knowledge_parse_cache`` (migration ``0169``, B-209 §5.5) — a model's
    raw reading of one page image, per **document**: the same document
    uploaded again, re-chunked or recognized again is never billed twice for a
    page, and deleting the document deletes its readings (they can be health
    data — review M1). ``cache_key`` = sha256 of the image bytes sent + model +
    request parameters + render parameters."""

    __tablename__ = "knowledge_parse_cache"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "knowledge_document.id",
            ondelete="CASCADE",
            name="fk_knowledge_parse_cache_document",
        ),
        nullable=False,
    )
    #: ``page_ocr`` (PR-1); ``figure_desc`` / ``figure_ocr`` reserved for PR-2.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    cache_key: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        PrimaryKeyConstraint("document_id", "kind", "cache_key", name="knowledge_parse_cache_pkey"),
        CheckConstraint(
            "kind IN ('page_ocr', 'figure_desc', 'figure_ocr')",
            name="knowledge_parse_cache_kind_enum",
        ),
    )


class KnowledgeDocSeqRow(Base):
    """Per-tenant ``doc_seq`` counter (migration ``0165``) — bumped with
    ``INSERT … ON CONFLICT DO UPDATE … RETURNING`` in the document's own
    insert transaction, so numbers never repeat and a rolled-back insert
    gives its number back."""

    __tablename__ = "knowledge_doc_seq"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)


class KnowledgeTaskRow(Base):
    """``knowledge_task`` (migration ``0165``) — one asynchronous knowledge
    job (import batch, re-parse, delete, re-index, curation, FAQ import),
    shared by the admin UI's progress view and the open API's ``/tasks``.
    Nothing writes it yet (P3-PR2)."""

    __tablename__ = "knowledge_task"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    kb_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    progress: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="knowledge_task_status_enum",
        ),
        Index("knowledge_task_kb_idx", "tenant_id", "kb_id", "created_at"),
        Index(
            "knowledge_task_active_idx",
            "created_at",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )


class KnowledgeChunkRevisionRow(Base):
    """``knowledge_chunk_revision`` (migration ``0165``) — a chunk's edit
    history for review / rollback. Nothing writes it yet (P3-PR2)."""

    __tablename__ = "knowledge_chunk_revision"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    kb_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    chunk_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    heading_path: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    edited_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("chunk_id", "revision", name="knowledge_chunk_revision_uniq"),
        Index("knowledge_chunk_revision_document_idx", "document_id"),
    )


class KnowledgeTagRow(Base):
    """``knowledge_tag`` (migration ``0165``) — a base-scoped document tag.
    Deleting the base deletes its tags (FK cascade). Nothing writes it yet."""

    __tablename__ = "knowledge_tag"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    kb_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_base.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    color: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "kb_id", "name", name="knowledge_tag_name_uniq"),
    )


class KnowledgeDocumentTagRow(Base):
    """``knowledge_document_tag`` (migration ``0165``) — document ↔ tag.
    Deleting either side deletes the link (FK cascade)."""

    __tablename__ = "knowledge_document_tag"

    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_document.id", ondelete="CASCADE"),
        nullable=False,
    )
    tag_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("knowledge_tag.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        PrimaryKeyConstraint("document_id", "tag_id", name="knowledge_document_tag_pkey"),
        Index("knowledge_document_tag_tag_idx", "tag_id"),
    )
