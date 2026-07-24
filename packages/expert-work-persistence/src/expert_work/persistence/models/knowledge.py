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
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
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
    #: Embedding model that produced this base's vectors (captured at create).
    #: Compared against the live platform model to derive ``needs_reindex``.
    embedding_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Set when a re-index is requested; the recovery worker re-embeds the
    #: base's retained chunk text with the current model, then clears it.
    reindex_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("knowledge_chunk_kb_idx", "tenant_id", "kb_id"),
        Index("knowledge_chunk_document_idx", "document_id"),
        Index("knowledge_chunk_tsv_idx", "content_tsv", postgresql_using="gin"),
    )
