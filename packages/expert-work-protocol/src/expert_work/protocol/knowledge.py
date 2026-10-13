"""Knowledge base / RAG — Stream J.5.

A tenant uploads documents into named knowledge bases; each document is
parsed, chunked, and embedded into ``knowledge_chunk`` rows for vector
retrieval. An agent's ``knowledge:`` manifest block binds it to a subset
of the tenant's bases, which its ``knowledge_search`` tool queries.

All three records are scoped to ``tenant_id`` only — knowledge bases are
tenant-shared, not per-user (unlike J.3 memory). See
``docs/streams/STREAM-J-DESIGN.md`` § 12.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Default per-base chunking parameters (token-counted). A base may
#: override them at creation to tune chunk granularity to its content.
DEFAULT_CHUNK_MAX_TOKENS: Final = 512
DEFAULT_CHUNK_OVERLAP_TOKENS: Final = 64

#: Default per-base retrieval parameters. Surfaced (not hardcoded) so a
#: base can tune how its ``knowledge_search`` recall behaves.
DEFAULT_RETRIEVAL_TOP_K: Final = 5


class DocumentStatus(StrEnum):
    """``knowledge_document.status`` — one document's ingestion lifecycle."""

    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    #: Reserved for B-179 content safety (L2-PR2 only declares them; nothing
    #: writes these yet): ``blocked`` = rejected by a safety check, ``disabled``
    #: = kept but excluded from retrieval by an operator.
    BLOCKED = "blocked"
    DISABLED = "disabled"


class RetrievalMethod(StrEnum):
    """How a base recalls candidate chunks for ``knowledge_search``.

    ``HYBRID`` runs both the dense (vector) and keyword (FTS) recall paths
    and fuses them with RRF; ``VECTOR`` / ``KEYWORD`` restrict to one path.
    """

    VECTOR = "vector"
    KEYWORD = "keyword"
    HYBRID = "hybrid"


class KnowledgeBase(BaseModel):
    """One row of ``knowledge_base`` — a named, tenant-scoped document collection.

    ``chunk_max_tokens`` / ``chunk_overlap_tokens`` are this base's
    chunking parameters; the ingestion pipeline slices each document to
    them. Per-base so different bases can tune granularity (Stream J.5).
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    name: str = Field(description="logical name, unique per tenant")
    description: str | None = Field(default=None, description="free-text purpose, shown in the UI")
    created_by: str | None = Field(default=None, description="subject id of the creator")
    chunk_max_tokens: int = Field(
        default=DEFAULT_CHUNK_MAX_TOKENS, gt=0, description="max tokens per chunk"
    )
    chunk_overlap_tokens: int = Field(
        default=DEFAULT_CHUNK_OVERLAP_TOKENS,
        ge=0,
        description="tokens of overlap between adjacent chunks",
    )
    retrieval_top_k: int = Field(
        default=DEFAULT_RETRIEVAL_TOP_K,
        ge=1,
        le=50,
        description="default number of chunks returned by knowledge_search",
    )
    retrieval_score_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="minimum vector similarity to keep a hit; None disables the cutoff",
    )
    retrieval_method: RetrievalMethod = Field(
        default=RetrievalMethod.HYBRID, description="recall strategy: vector, keyword, or hybrid"
    )
    rerank_enabled: bool = Field(
        default=True, description="apply the LLM reranker after fusion when configured"
    )
    #: Embedding model that produced this base's vectors. Captured at create
    #: time; compared against the live platform model to derive ``needs_reindex``.
    embedding_provider: str | None = None
    embedding_model: str | None = None
    #: Set while a re-index is in flight (the recovery/runner re-embeds the
    #: base's retained chunk text), cleared on completion. Surfaced so the UI
    #: can show a "re-indexing" state.
    reindex_requested_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def _check_chunking(self) -> KnowledgeBase:
        if self.chunk_overlap_tokens >= self.chunk_max_tokens:
            msg = "chunk_overlap_tokens must be less than chunk_max_tokens"
            raise ValueError(msg)
        return self


class KnowledgeDocument(BaseModel):
    """One row of ``knowledge_document`` — an ingested source file.

    Re-uploading the same ``filename`` into the same base replaces the
    document's chunks — ``(tenant_id, kb_id, filename)`` is unique.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    kb_id: UUID
    filename: str
    status: DocumentStatus
    error: str | None = Field(default=None, description="failure detail when status is FAILED")
    chunk_count: int = Field(default=0, ge=0, description="chunks produced by the latest ingest")
    attempts: int = Field(default=0, ge=0, description="ingestion attempts so far (durability)")
    #: Bumped on every re-upload / re-ingest. A worker carries the generation
    #: it claimed and its write-backs are fenced on it, so a stale worker from
    #: a previous upload can never overwrite the chunks of a newer one.
    ingest_generation: int = Field(default=0, ge=0, description="re-ingest generation (fence)")
    #: A newer upload is waiting in the pending slot (being ingested, or its
    #: ingest failed). ``status == failed`` with ``chunk_count > 0`` and
    #: ``has_pending`` means: the previous version is still serving.
    has_pending: bool = Field(default=False, description="a newer upload is pending / failed")
    created_at: datetime | None = None
    updated_at: datetime | None = None


class KnowledgeChunk(BaseModel):
    """One row of ``knowledge_chunk`` — an embedded slice of a document."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    tenant_id: UUID
    kb_id: UUID
    document_id: UUID
    chunk_index: int = Field(ge=0, description="0-based position within the source document")
    content: str
    embedding: tuple[float, ...] = Field(
        repr=False, description="semantic embedding vector of ``content``"
    )
    created_at: datetime | None = None


class ScoredChunk(BaseModel):
    """A chunk plus its recall score and which recall path surfaced it.

    Returned by the store's ``*_scored`` methods so the retriever can apply
    a similarity threshold and the retrieval-test endpoint can show scores.
    ``score`` is a vector similarity in [0, 1] (``1 - cosine_distance``) for
    ``source == "vector"``, or an unbounded ``ts_rank`` for ``"keyword"``.
    """

    model_config = ConfigDict(frozen=True)

    chunk: KnowledgeChunk
    score: float
    source: Literal["vector", "keyword"]
