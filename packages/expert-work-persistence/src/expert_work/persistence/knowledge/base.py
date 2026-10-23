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
import mimetypes
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Final, Literal
from uuid import UUID

from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    DEFAULT_RETRIEVAL_TOP_K,
    DocumentPage,
    DocumentStatus,
    IngestErrorCode,
    IngestStage,
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

#: Default cap for :meth:`KnowledgeStore.list_bases_all_tenants` — the single
#: source shared by both store implementations and the
#: ``GET /v1/knowledge/bases`` endpoint (which also derives its ``truncated``
#: response flag from it, so the two must never drift).
ALL_TENANTS_BASES_LIMIT: Final = 200


#: Office Open XML types — absent from Python's built-in map (3.12 / 3.13),
#: so without them every ``.docx`` / ``.pptx`` / ``.xlsx`` upload got ``None``.
_OOXML_TYPES: Final = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _fixed_mime_types() -> mimetypes.MimeTypes:
    """Python's built-in extension map plus :data:`_OOXML_TYPES` — never the
    host's ``mime.types`` (``mimetypes.guess_type`` reads it), so the same
    upload gets the same mime on a laptop, in CI and in the production image."""
    table = mimetypes.MimeTypes(filenames=())
    for ext, mime in _OOXML_TYPES.items():
        table.add_type(mime, ext)
    return table


_MIME_TYPES: Final = _fixed_mime_types()


@dataclass(frozen=True)
class DocumentFileFacts:
    """What a store records about an uploaded file on insert — one helper so
    the SQL and in-memory stores derive exactly the same values."""

    title: str
    mime: str | None
    size_bytes: int | None

    @classmethod
    def of(cls, filename: str, content: bytes | None) -> DocumentFileFacts:
        """Title = filename without its last extension; mime from the
        extension (never the client's ``Content-Type``); size of ``content``
        (``None`` when the caller passed no bytes, e.g. a pre-parsed corpus)."""
        suffix = PurePosixPath(filename).suffix
        title = filename[: -len(suffix)] if suffix and len(suffix) < len(filename) else filename
        mime, _ = _MIME_TYPES.guess_type(filename, strict=False)
        return cls(title=title, mime=mime, size_bytes=len(content) if content is not None else None)


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
    #: Bytes to ingest: the pending upload when there is one, else the
    #: serving version (explicit re-ingest / crash recovery).
    content: bytes | None
    content_sha256: str | None
    chunk_max_tokens: int
    chunk_overlap_tokens: int
    attempts: int
    #: The document's ``ingest_generation`` at claim time. Every write-back the
    #: worker makes (``replace_chunks`` / ``set_document_status`` /
    #: ``mark_document_failed_terminal``) passes it as the fence.
    generation: int


#: B-209 §5.5 —— what a ``knowledge_parse_cache`` row holds: a scanned page's
#: raw reading (PR-1); a figure's description / text (reserved for PR-2).
ParseCacheKind = Literal["page_ocr", "figure_desc", "figure_ocr"]


@dataclass(frozen=True)
class ParseCacheEntry:
    """One cached model reading: the model that produced it and its result
    (``{"raw": …, "finish_reason": …, "layout_chars": …}`` for ``page_ocr``)."""

    model: str
    result: dict[str, Any]


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
        rerank_score_threshold: float | None = None,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        rerank_threshold_model: str | None = None,
    ) -> KnowledgeBase:
        """Create a new knowledge base with its chunking + retrieval
        parameters and the embedding model that will produce its vectors;
        raise :class:`DuplicateKnowledgeBaseError` if ``(tenant_id, name)``
        already exists. ``rerank_threshold_model`` (B-206 K-7b) is the rerank
        model ``rerank_score_threshold`` was set under."""

    @abc.abstractmethod
    async def update_base(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        description: str | _Unset | None = UNSET,
        chunk_max_tokens: int | None = None,
        chunk_overlap_tokens: int | None = None,
        retrieval_top_k: int | None = None,
        retrieval_score_threshold: float | _Unset | None = UNSET,
        retrieval_method: RetrievalMethod | None = None,
        rerank_enabled: bool | None = None,
        rerank_score_threshold: float | _Unset | None = UNSET,
        rerank_threshold_model: str | _Unset | None = UNSET,
    ) -> KnowledgeBase | None:
        """Patch an existing base. Each argument left at its default is not
        touched; ``None`` for a nullable field (``description``,
        ``retrieval_score_threshold``, ``rerank_score_threshold``,
        ``rerank_threshold_model``) clears it —
        the sentinel :data:`UNSET` is how "not supplied" is told apart from an
        explicit ``None``.
        Renaming is deliberately not offered (manifests reference bases by
        name). ``updated_at`` is bumped. Returns the updated base, or
        ``None`` if no base matched."""

    @abc.abstractmethod
    async def get_base(self, *, tenant_id: UUID, name: str) -> KnowledgeBase | None:
        """Fetch a base by name, or ``None`` — never reveals another tenant's."""

    @abc.abstractmethod
    async def list_bases(self, *, tenant_id: UUID) -> list[KnowledgeBase]:
        """The tenant's knowledge bases, newest first (``id`` DESC tiebreak,
        the same order :meth:`list_bases_page` pages through)."""

    @abc.abstractmethod
    async def list_bases_page(
        self, *, tenant_id: UUID, limit: int, offset: int
    ) -> tuple[list[KnowledgeBase], int]:
        """One page of the tenant's bases, newest first (``id`` tiebreak so
        pages stay stable), plus the tenant's total base count."""

    @abc.abstractmethod
    async def list_bases_all_tenants(
        self, *, limit: int = ALL_TENANTS_BASES_LIMIT
    ) -> list[KnowledgeBase]:
        """Every tenant's knowledge bases, newest first (``id`` tiebreak),
        capped at ``limit`` rows — the W4 ``tenant_id=*`` aggregate
        (``list_bases`` has no cap; the cross-tenant read is bounded so it
        can never become an unbounded full-table page). Caller MUST wrap the
        call in ``bypass_rls_session()`` / ``applied_scope(CrossTenant)``.

        No covering index; full scan acceptable at current scale — see
        ``docs/superpowers/plans/2026-08-03-cross-tenant-drilldown-w4.md``.

        FORCE-RLS note: this table runs ``ENABLE + FORCE ROW LEVEL SECURITY``
        with a ``tenant_id = current_setting('app.tenant_id')`` policy that
        denies when the GUC is unset. Skipping the GUC therefore only works
        because the app connects with a BYPASSRLS role today. When RLS is
        un-parked, this read needs the ``SET LOCAL ROLE audit_reader`` +
        GRANT treatment that ``token_usage`` already has (migration 0140)."""

    @abc.abstractmethod
    async def list_bases_all_tenants_page(
        self, *, limit: int, offset: int
    ) -> tuple[list[KnowledgeBase], int]:
        """One page of the ``tenant_id=*`` aggregate (same order as
        :meth:`list_bases_all_tenants`) plus the all-tenants total. Same
        bypass posture as :meth:`list_bases_all_tenants`."""

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
    async def base_stats_many_all_tenants(
        self, *, kb_ids: Sequence[UUID]
    ) -> dict[UUID, tuple[int, int]]:
        """:meth:`base_stats_many` across every tenant, bounded to ``kb_ids``
        (the page :meth:`list_bases_all_tenants` returned) — ``kb_id`` is
        globally unique, so one dict serves the W4 aggregate list. Same
        bypass posture as :meth:`list_bases_all_tenants`.

        No covering index; full scan acceptable at current scale — see
        ``docs/superpowers/plans/2026-08-03-cross-tenant-drilldown-w4.md``."""

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
        force: bool = False,
    ) -> KnowledgeDocument:
        """Create a document at ``PENDING``, or — if ``(kb_id, filename)``
        already exists — reset it to ``PENDING`` (clearing ``error`` and the
        durability lease + ``attempts``, bumping ``ingest_generation``) for
        re-ingestion.

        Two byte slots: ``content`` holds the version whose chunks are
        serving; a re-upload's bytes go to the **pending** slot. The serving
        chunks and ``content`` are kept until the new ingest succeeds (its
        fenced ``READY`` write promotes pending → content), so the document
        stays searchable across a re-upload and a *failed* re-upload leaves
        the previous version intact (retry with re-ingest, or drop it with
        :meth:`discard_pending`). A brand-new document's bytes go straight to
        ``content``. ``content=None`` (explicit re-ingest) keeps the slots as
        they are and re-drives whichever is pending, else the serving one.

        Unchanged re-upload: when the existing document is ``READY`` and
        ``content_sha256`` equals the serving hash, nothing is touched and the
        document is returned as is (status still ``READY`` — the caller's
        signal to skip ingestion). ``force=True`` (explicit re-ingest)
        resets regardless.

        Model v2: a new document gets the tenant's next ``doc_seq`` (kept for
        good — re-uploads keep it, deleted numbers are never reused; a row
        from before ``doc_seq`` existed gets one on its next reset) and its
        :class:`DocumentFileFacts`; any create or reset sets ``stage=queued``
        and clears ``error_code``. ``size_bytes`` describes the serving
        version, so a re-upload's bytes only count once they are promoted."""

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
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        error: str,
        error_code: IngestErrorCode | None = None,
        generation: int | None = None,
    ) -> bool:
        """Mark a document ``FAILED`` with ``error`` / ``error_code`` and clear
        its lease — a terminal ingest failure, exhausted retries, or missing
        original bytes. ``stage`` is left on the step that failed. With
        ``generation`` the write is fenced (see :meth:`set_document_status`).
        Returns whether a row was written."""

    @abc.abstractmethod
    async def set_document_stage(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        stage: IngestStage,
        generation: int | None = None,
    ) -> bool:
        """Record the pipeline step an ingest has reached. Fenced like
        :meth:`set_document_status` when ``generation`` is given (``False`` =
        the document is no longer this worker's — stop); unfenced otherwise
        (a caller that owns the document outright, e.g. the eval runner)."""

    @abc.abstractmethod
    async def discard_pending(
        self, *, tenant_id: UUID, document_id: UUID
    ) -> KnowledgeDocument | None:
        """Drop the pending upload and go back to the serving version: clears
        the pending slot and ``error``, bumps ``ingest_generation`` (an
        in-flight ingest of the dropped version is fenced out) and sets the
        status to ``READY`` (and ``stage=ready``) when serving chunks exist,
        else ``FAILED`` with a note; ``error_code`` is cleared either way.
        ``None`` when the document does not exist; the document unchanged
        when nothing was pending."""

    @abc.abstractmethod
    async def sweep_terminal(self, *, now: datetime, max_attempts: int) -> int:
        """Cross-tenant: every ``processing`` document that has used up
        ``max_attempts`` and whose lease has expired goes ``FAILED``
        (``error='ingest attempts exhausted'``,
        ``error_code=attempts_exhausted``). Such a document is no longer
        claimable, so without this sweep it would sit in ``processing``
        forever (a crash on the last attempt). Returns the number swept.
        Caller wraps the call in a bypass-RLS scope."""

    @abc.abstractmethod
    async def renew_lease(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        generation: int,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        """Extend the lease of a document this worker is processing
        (``status='processing' AND ingest_generation=generation``) to
        ``now + lease_seconds``. ``False`` when the document is no longer
        ours (settled, re-uploaded, deleted)."""

    @abc.abstractmethod
    async def list_documents(self, *, tenant_id: UUID, kb_id: UUID) -> list[KnowledgeDocument]:
        """The base's documents, newest first."""

    @abc.abstractmethod
    async def list_documents_page(
        self, *, tenant_id: UUID, kb_id: UUID, limit: int, offset: int
    ) -> tuple[list[KnowledgeDocument], int]:
        """A page of :meth:`list_documents` (same order) plus the total count."""

    @abc.abstractmethod
    async def set_document_status(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        status: DocumentStatus,
        error: str | None = None,
        chunk_count: int | None = None,
        generation: int | None = None,
    ) -> bool:
        """Update a document's ingestion status. ``chunk_count`` is set
        only when supplied (on a successful ingest). Returns ``True`` if a
        document matched, ``False`` if it no longer exists (e.g. deleted
        concurrently while its ingest was in flight).

        ``generation`` fences the write: it only lands when the document is
        still ``processing`` **and** its ``ingest_generation`` equals
        ``generation`` — a worker from an earlier upload (or one whose claim
        was already settled) gets ``False`` and changes nothing.

        A fenced write that lands ``READY`` promotes the pending bytes into
        the serving slot (``content``, and ``size_bytes``) in the same
        statement. A write that lands ``FAILED`` touches neither slot nor the
        chunks: whatever was serving keeps serving (see :meth:`upsert_document`).
        ``READY`` also sets ``stage=ready`` and clears ``error_code``."""

    @abc.abstractmethod
    async def delete_document(self, *, tenant_id: UUID, document_id: UUID) -> bool:
        """Delete a document and cascade its chunks, page records and
        recognition cache. ``False`` if no document matched."""

    @abc.abstractmethod
    async def set_document_pages(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        pages: Sequence[DocumentPage],
        generation: int | None = None,
    ) -> bool:
        """B-209 §5.6 —— record a PDF ingest's per-page outcome: the page
        records, their :class:`~expert_work.protocol.PageStats` and
        ``page_count``. Written once recognition is done, before chunking — so
        a document that then fails still shows which pages failed and why.
        Fenced like :meth:`set_document_stage` when ``generation`` is given
        (``False`` = no longer this worker's document)."""

    @abc.abstractmethod
    async def list_document_pages(
        self, *, tenant_id: UUID, document_id: UUID
    ) -> list[DocumentPage] | None:
        """The page records of the document's latest PDF ingest, in page
        order; ``[]`` when none were recorded (not a PDF, or ingested before
        page records existed); ``None`` when there is no such document."""

    # -- recognition cache (B-209 §5.5) -------------------------------------

    @abc.abstractmethod
    async def get_parse_cache(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        kind: ParseCacheKind,
        keys: Sequence[str],
    ) -> dict[str, ParseCacheEntry]:
        """The document's cached readings for ``keys`` (absent keys: not
        cached). Per document on purpose: a deleted document's readings go
        with it (:meth:`delete_document` / :meth:`delete_base` remove them),
        and no tenant learns what another uploaded."""

    @abc.abstractmethod
    async def put_parse_cache(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        kind: ParseCacheKind,
        key: str,
        model: str,
        result: dict[str, Any],
    ) -> None:
        """Cache one reading. An existing entry for the key is kept (the same
        image bytes, model, prompt version and render parameters read the
        same; the first reading stands)."""

    # -- chunks -------------------------------------------------------------

    @abc.abstractmethod
    async def replace_chunks(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        chunks: Sequence[KnowledgeChunk],
        generation: int | None = None,
        expect_status: DocumentStatus = DocumentStatus.PROCESSING,
    ) -> bool:
        """Replace all of a document's chunks with ``chunks`` — deletes
        the document's existing chunks, then inserts the new set, in one
        transaction. Every chunk column round-trips (heading path, content
        hash, type, flags); the keyword index is built from each chunk's
        ``retrieval_text`` (breadcrumb + body), so heading words stay
        searchable while ``content`` is stored clean.

        Unfenced (``generation is None``): if the document no longer exists
        (deleted while an ingest was in flight) the write-back is rejected
        instead of recreating orphan chunks — the SQL store raises
        ``IntegrityError`` (FK, migration ``0136``), the in-memory store
        raises :class:`KeyError`.

        Fenced (``generation`` given): the swap only lands when the document
        is in ``expect_status`` **and** its ``ingest_generation`` equals
        ``generation`` (``processing`` for an ingest write-back; ``ready``
        for a re-index, which swaps vectors under a settled document);
        otherwise — including a deleted document — nothing is written and
        ``False`` is returned. ``True`` when the chunks landed."""

    @abc.abstractmethod
    async def list_chunks(
        self, *, tenant_id: UUID, document_id: UUID, offset: int = 0, limit: int = 50
    ) -> tuple[list[KnowledgeChunk], int]:
        """A page of a document's chunks ordered by ``chunk_index`` plus the
        total count — for segment preview. The (large) embedding vector is
        omitted from the returned chunks (``embedding == ()``)."""

    @abc.abstractmethod
    async def chunk_embeddings_by_hash(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        content_hashes: Sequence[str],
        embedding_model: str,
    ) -> dict[str, tuple[float, ...]]:
        """The stored vectors of this document's chunks whose ``content_hash``
        is in ``content_hashes`` **and** that ``embedding_model`` produced — a
        re-ingest reuses them instead of embedding unchanged chunks again,
        never across a model change. Only the given document's chunks
        (another document's identical text is not borrowed); hashes with no
        matching chunk are absent."""

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

        ``query`` becomes a
        :class:`~expert_work.persistence.knowledge.text_search.KbKeywordQuery`
        (:func:`~expert_work.persistence.knowledge.text_search.build_kb_keyword_query`):
        segmented the same way the chunks were indexed, function words and
        punctuation dropped, identifiers such as ``QXA-N7`` kept as phrases.
        Chunks are indexed as written (no NFKC); each unit also tries its raw
        form where NFKC changed the query.
        A chunk matches when it contains **any** term, or any identifier
        phrase as adjacent tokens. Chunks holding an identifier rank first;
        then chunks holding the whole question verbatim; then chunks covering
        more of the query, rarer terms weighing more (BM25 idf); ``ts_rank``
        breaks the remaining ties. A query with nothing searchable left, or an
        empty ``kb_ids``, yields an empty list."""

    @abc.abstractmethod
    async def keyword_search_scored(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        query: str,
        limit: int = 5,
    ) -> list[ScoredChunk]:
        """Like :meth:`keyword_search` but each hit carries its keyword
        score and ``source == "keyword"`` — NOT a [0, 1] similarity:
        coverage (Σ idf·hit / Σ idf over the query's terms and identifier
        phrases, idf = ln(1 + (N - df + 0.5) / (df + 0.5)) with N the chunks in
        scope and df those holding the unit; ≤ 1) plus ``ts_rank`` (< 0.1, a
        tie-break), plus
        :data:`~expert_work.persistence.knowledge.text_search.KB_KEYWORD_PHRASE_BOOST`
        when an identifier phrase matched and
        :data:`~expert_work.persistence.knowledge.text_search.KB_KEYWORD_QUERY_PHRASE_BOOST`
        when the whole question appears verbatim. SQL and in-memory stores share the
        match predicate and the ordering intent; their exact scores (and so
        the order of near-ties) can differ."""
