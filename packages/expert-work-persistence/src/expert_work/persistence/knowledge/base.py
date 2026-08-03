"""Abstract ``KnowledgeStore`` repository — Stream J.5 RAG.

Implementations:
- :class:`expert_work.persistence.knowledge.memory.InMemoryKnowledgeStore`
- :class:`expert_work.persistence.knowledge.sql.SqlKnowledgeStore`

The store deals in *vectors* — embedding text into a vector is the
caller's job (the J.5 ingestion runner / retriever wire the embedder).
This keeps the persistence layer free of any embedding-model dependency.
All methods are tenant-scoped; knowledge bases are tenant-shared.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    DEFAULT_RETRIEVAL_TOP_K,
    DocumentStatus,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    RetrievalMethod,
    ScoredChunk,
)


class _Unset:
    """Sentinel type — ``update_base`` distinguishes "field not supplied"
    (leave unchanged) from an explicit ``None`` (clear a nullable column)."""


#: Singleton sentinel for unspecified ``update_base`` arguments. The API layer
#: passes it (via ``model_fields_set``) for fields the caller omitted.
UNSET: Final = _Unset()


@dataclass(frozen=True)
class ClaimedIngestion:
    """A document CAS-claimed for (re-)ingestion plus the bytes + chunking
    parameters needed to re-drive it without another round trip. ``content``
    is ``None`` for a legacy document whose original bytes were not retained —
    the worker marks such a document failed (re-upload required)."""

    tenant_id: UUID
    document_id: UUID
    kb_id: UUID
    filename: str
    content: bytes | None
    content_sha256: str | None
    chunk_max_tokens: int
    chunk_overlap_tokens: int
    attempts: int


class DuplicateKnowledgeBaseError(Exception):
    """Raised when :meth:`KnowledgeStore.create_base` hits the unique
    ``(tenant_id, name)`` index. The API layer maps it to ``HTTP 409``."""

    def __init__(self, *, tenant_id: UUID, name: str) -> None:
        super().__init__(f"knowledge base already exists: tenant_id={tenant_id} name={name!r}")
        self.tenant_id = tenant_id
        self.name = name


class KnowledgeStore(abc.ABC):
    """Tenant-scoped knowledge base / document / chunk repository."""

    # -- knowledge bases ----------------------------------------------------

    @abc.abstractmethod
    async def create_base(
        self,
        *,
        tenant_id: UUID,
        name: str,
        description: str | None = None,
        created_by: str | None = None,
        chunk_max_tokens: int = DEFAULT_CHUNK_MAX_TOKENS,
        chunk_overlap_tokens: int = DEFAULT_CHUNK_OVERLAP_TOKENS,
        retrieval_top_k: int = DEFAULT_RETRIEVAL_TOP_K,
        retrieval_score_threshold: float | None = None,
        retrieval_method: RetrievalMethod = RetrievalMethod.HYBRID,
        rerank_enabled: bool = True,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
    ) -> KnowledgeBase:
        """Create a new knowledge base with its chunking + retrieval
        parameters and the embedding model that will produce its vectors;
        raise :class:`DuplicateKnowledgeBaseError` if ``(tenant_id, name)``
        already exists."""

    @abc.abstractmethod
    async def update_base(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        description: str | None | _Unset = UNSET,
        chunk_max_tokens: int | None = None,
        chunk_overlap_tokens: int | None = None,
        retrieval_top_k: int | None = None,
        retrieval_score_threshold: float | None | _Unset = UNSET,
        retrieval_method: RetrievalMethod | None = None,
        rerank_enabled: bool | None = None,
    ) -> KnowledgeBase | None:
        """Patch an existing base. Each argument left at its default is not
        touched; ``None`` for a nullable field (``description``,
        ``retrieval_score_threshold``) clears it — the sentinel :data:`UNSET`
        is how "not supplied" is told apart from an explicit ``None``.
        Renaming is deliberately not offered (manifests reference bases by
        name). ``updated_at`` is bumped. Returns the updated base, or
        ``None`` if no base matched."""

    @abc.abstractmethod
    async def get_base(self, *, tenant_id: UUID, name: str) -> KnowledgeBase | None:
        """Fetch a base by name, or ``None`` — never reveals another tenant's."""

    @abc.abstractmethod
    async def list_bases(self, *, tenant_id: UUID) -> list[KnowledgeBase]:
        """The tenant's knowledge bases, newest first."""

    @abc.abstractmethod
    async def list_bases_all_tenants(self) -> list[KnowledgeBase]:
        """Every tenant's knowledge bases, newest first (``id`` tiebreak) —
        the W4 ``tenant_id=*`` aggregate. Caller MUST wrap the call in
        ``bypass_rls_session()`` / ``applied_scope(CrossTenant)``."""

    @abc.abstractmethod
    async def base_stats(self, *, tenant_id: UUID, kb_id: UUID) -> tuple[int, int]:
        """``(document_count, total_chunk_count)`` for one base. Computed
        from ``knowledge_document`` (chunk counts are maintained per
        document by ingestion) — no stored counters to drift."""

    @abc.abstractmethod
    async def base_stats_many(self, *, tenant_id: UUID) -> dict[UUID, tuple[int, int]]:
        """``{kb_id: (document_count, total_chunk_count)}`` for all the
        tenant's bases in one query — used by ``list_bases`` to avoid N+1."""

    @abc.abstractmethod
    async def base_stats_many_all_tenants(self) -> dict[UUID, tuple[int, int]]:
        """:meth:`base_stats_many` across every tenant — ``kb_id`` is globally
        unique, so one dict serves the W4 aggregate list. Same bypass posture
        as :meth:`list_bases_all_tenants`."""

    @abc.abstractmethod
    async def stamp_embedding_model(
        self, *, tenant_id: UUID, kb_id: UUID, embedding_provider: str, embedding_model: str
    ) -> None:
        """Record the embedding model that produced (or re-produced) this
        base's vectors. Set at create time and after a re-index so
        ``needs_reindex`` (model != live platform model) is authoritative."""

    @abc.abstractmethod
    async def request_reindex(self, *, tenant_id: UUID, kb_id: UUID) -> bool:
        """Mark a base as re-index-requested (sets ``reindex_requested_at``);
        ``False`` if no base matched."""

    @abc.abstractmethod
    async def clear_reindex(self, *, tenant_id: UUID, kb_id: UUID) -> None:
        """Clear ``reindex_requested_at`` once a re-index completes."""

    @abc.abstractmethod
    async def delete_base(self, *, tenant_id: UUID, kb_id: UUID) -> bool:
        """Delete a base and cascade its documents + chunks. ``False`` if
        no base matched."""

    # -- documents ----------------------------------------------------------

    @abc.abstractmethod
    async def upsert_document(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        filename: str,
        content: bytes | None = None,
        content_sha256: str | None = None,
    ) -> KnowledgeDocument:
        """Create a document at ``PENDING``, or — if ``(kb_id, filename)``
        already exists — reset it to ``PENDING`` (clearing ``error`` /
        ``chunk_count`` and the durability lease + ``attempts``) for
        re-ingestion. ``content`` retains the original bytes so the document
        can be re-driven after a crash / for re-ingest. Returns the document."""

    @abc.abstractmethod
    async def get_document(self, *, tenant_id: UUID, document_id: UUID) -> KnowledgeDocument | None:
        """Fetch a document by id, or ``None``."""

    @abc.abstractmethod
    async def get_documents(
        self, *, tenant_id: UUID, document_ids: Sequence[UUID]
    ) -> dict[UUID, KnowledgeDocument]:
        """Batch :meth:`get_document` — one round trip for many ids, keyed by
        id (missing / cross-tenant ids absent). Replaces a per-id ``get_document``
        loop (e.g. retrieval attribution — an N+1 over the result's documents)."""

    @abc.abstractmethod
    async def get_document_content(self, *, tenant_id: UUID, document_id: UUID) -> bytes | None:
        """The document's retained original bytes, or ``None`` (legacy rows /
        unknown id)."""

    @abc.abstractmethod
    async def claim_document(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        now: datetime,
        lease_seconds: int,
        max_attempts: int,
    ) -> ClaimedIngestion | None:
        """Tenant-scoped CAS claim of one document for the fast (in-process)
        ingest path. Succeeds only if it is claimable (``pending``, or
        ``processing`` with an expired lease) and under ``max_attempts``;
        sets ``processing`` + a fresh lease and bumps ``attempts``. Returns
        the claim or ``None`` if another worker already holds it."""

    @abc.abstractmethod
    async def claim_documents_for_ingest(
        self, *, now: datetime, lease_seconds: int, limit: int, max_attempts: int
    ) -> list[ClaimedIngestion]:
        """Cross-tenant batch CAS claim for the recovery worker (caller wraps
        it in a bypass-RLS scope). Claims up to ``limit`` claimable documents
        (``pending`` or lease-expired ``processing``, under ``max_attempts``),
        marking each ``processing`` with a fresh lease + bumped ``attempts``.
        This single scan drains the queue AND recovers crashed work."""

    @abc.abstractmethod
    async def mark_document_failed_terminal(
        self, *, tenant_id: UUID, document_id: UUID, error: str
    ) -> None:
        """Mark a document ``FAILED`` and clear its lease — used when retries
        are exhausted or the original bytes are unavailable."""

    @abc.abstractmethod
    async def list_documents(self, *, tenant_id: UUID, kb_id: UUID) -> list[KnowledgeDocument]:
        """The base's documents, newest first."""

    @abc.abstractmethod
    async def set_document_status(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        status: DocumentStatus,
        error: str | None = None,
        chunk_count: int | None = None,
    ) -> bool:
        """Update a document's ingestion status. ``chunk_count`` is set
        only when supplied (on a successful ingest). Returns ``True`` if a
        document matched, ``False`` if it no longer exists (e.g. deleted
        concurrently while its ingest was in flight)."""

    @abc.abstractmethod
    async def delete_document(self, *, tenant_id: UUID, document_id: UUID) -> bool:
        """Delete a document and cascade its chunks. ``False`` if no
        document matched."""

    # -- chunks -------------------------------------------------------------

    @abc.abstractmethod
    async def replace_chunks(
        self, *, tenant_id: UUID, document_id: UUID, chunks: Sequence[KnowledgeChunk]
    ) -> None:
        """Replace all of a document's chunks with ``chunks`` — deletes
        the document's existing chunks, then inserts the new set. If the
        document no longer exists (deleted while an ingest was in flight)
        the write-back is rejected instead of recreating orphan chunks:
        the SQL store raises ``IntegrityError`` (FK, migration ``0136``),
        the in-memory store raises :class:`KeyError`."""

    @abc.abstractmethod
    async def list_chunks(
        self, *, tenant_id: UUID, document_id: UUID, offset: int = 0, limit: int = 50
    ) -> tuple[list[KnowledgeChunk], int]:
        """A page of a document's chunks ordered by ``chunk_index`` plus the
        total count — for segment preview. The (large) embedding vector is
        omitted from the returned chunks (``embedding == ()``)."""

    @abc.abstractmethod
    async def search(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        query_embedding: Sequence[float],
        limit: int = 5,
    ) -> list[KnowledgeChunk]:
        """Return the ``limit`` chunks across ``kb_ids`` nearest
        ``query_embedding`` by cosine distance, closest first. An empty
        ``kb_ids`` yields an empty list."""

    @abc.abstractmethod
    async def search_scored(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        query_embedding: Sequence[float],
        limit: int = 5,
    ) -> list[ScoredChunk]:
        """Like :meth:`search` but each hit carries its vector similarity
        (``1 - cosine_distance``, in [0, 1] for normalised embeddings) and
        ``source == "vector"`` — lets the retriever threshold and the
        retrieval-test endpoint surface scores."""

    @abc.abstractmethod
    async def keyword_search(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        query: str,
        limit: int = 5,
    ) -> list[KnowledgeChunk]:
        """Return the ``limit`` chunks across ``kb_ids`` most relevant to
        ``query`` by full-text rank — the keyword side of hybrid search.
        ``query`` is segmented the same way the chunks were indexed
        (:func:`~expert_work.persistence.knowledge.text_search.tokenize_for_search`).
        An empty ``kb_ids`` yields an empty list."""

    @abc.abstractmethod
    async def keyword_search_scored(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        query: str,
        limit: int = 5,
    ) -> list[ScoredChunk]:
        """Like :meth:`keyword_search` but each hit carries its ``ts_rank``
        relevance (unbounded — NOT a [0, 1] similarity) and
        ``source == "keyword"``."""
