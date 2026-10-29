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
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Final, Literal
from uuid import UUID

# B-222 PR-2 — estimate types both stores share, re-exported.
from expert_work.persistence.knowledge.estimates import (
    ConfirmAllResult as ConfirmAllResult,
)
from expert_work.persistence.knowledge.estimates import (
    ConfirmOutcome as ConfirmOutcome,
)
from expert_work.persistence.knowledge.estimates import (
    EstimateSummary as EstimateSummary,
)
from expert_work.persistence.knowledge.estimates import (
    ExpiredEstimate as ExpiredEstimate,
)
from expert_work.persistence.knowledge.estimates import (
    ParseSnapshot as ParseSnapshot,
)
from expert_work.persistence.knowledge.estimates import (
    UploadStart as UploadStart,
)

# B-175 P3-PR2 — passage types and the rules both stores share, re-exported.
from expert_work.persistence.knowledge.passages import (
    ChunkVector as ChunkVector,
)
from expert_work.persistence.knowledge.passages import (
    DisabledReason as DisabledReason,
)
from expert_work.persistence.knowledge.passages import (
    ManualPassage as ManualPassage,
)
from expert_work.persistence.knowledge.passages import (
    Passage as Passage,
)
from expert_work.persistence.knowledge.passages import (
    PassageChild as PassageChild,
)
from expert_work.persistence.knowledge.passages import (
    PassageRevision as PassageRevision,
)
from expert_work.persistence.knowledge.passages import (
    ReviewCounts as ReviewCounts,
)
from expert_work.persistence.knowledge.passages import (
    SwitchOffRecord as SwitchOffRecord,
)
from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    DEFAULT_RETRIEVAL_TOP_K,
    ChunkType,
    DocumentPage,
    DocumentStatus,
    FigureStats,
    IngestErrorCode,
    IngestStage,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    RetrievalMethod,
    ScoredChunk,
)
from expert_work.protocol.knowledge_estimate import EstimateOrigin, IngestEstimate


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
    #: B-222 PR-2 —— ``estimate``: the document is being estimated (local parse
    #: and counts, nothing billed; it stays ``estimating``); ``ingest``: the
    #: paid ingest (``processing``).
    phase: Literal["estimate", "ingest"] = "ingest"
    #: The estimate the ingest was confirmed under (``None``: none — a document
    #: queued without one); unused by the estimate phase.
    estimate: IngestEstimate | None = None
    #: What started the estimate (:class:`~expert_work.protocol.knowledge_estimate.EstimateOrigin`
    #: value) and whether a duplicate is to be ingested anyway.
    origin: str | None = None
    allow_duplicate: bool = False
    #: The serving version's hash (``content_sha256``): the bytes claimed are
    #: unchanged when it equals :attr:`content_sha256`.
    serving_sha256: str | None = None


#: B-222 —— what :meth:`KnowledgeStore.cancel_documents` did with one document
#: (PR-2: ``withdrawn`` / ``withdrawn_deleted`` — its estimate was withdrawn).
CancelOutcome = Literal[
    "cancelled", "not_cancellable", "not_found", "withdrawn", "withdrawn_deleted"
]


#: B-209 §5.5 —— what a ``knowledge_parse_cache`` row holds: a scanned page's
#: raw reading (PR-1); a figure's description / text (PR-2); the semantic
#: chunker's topic-shift decisions for one embedding model (B-215).
ParseCacheKind = Literal["page_ocr", "figure_desc", "figure_ocr", "semantic_break"]


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


class RowSecurityActiveError(RuntimeError):
    """A cross-tenant lookup ran on a connection that row security still
    filters — its "not found" would be a lie (the figure reconciler gives up)."""


class KnowledgeRecallError(RuntimeError):
    """A recall query the database could not finish — a statement timeout, a
    lock or serialization failure, a lost connection, exhausted resources
    (B-206 K-22); never a bug in the query or a missing permission. The message
    is the driver's, so a timeout still reads as one. A hybrid search keeps its
    other path's results on it."""


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
    async def count_documents_capped(
        self, *, tenant_id: UUID, kb_ids: Sequence[UUID], cap: int
    ) -> dict[UUID, int]:
        """``{kb_id: min(document_count, cap)}`` for every id in ``kb_ids``
        (``0`` for a base without documents, or not the tenant's). Each count
        stops after ``cap`` rows — B-175 P4: the knowledge catalog reads this on
        every run of an agent binding several bases and only needs a bucket."""

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
        """Delete a base and cascade its documents + chunks (and what
        :meth:`delete_document` cascades). ``False`` if no base matched."""

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
        version, so a re-upload's bytes only count once they are promoted.

        B-222 PR-2: this queues the document **without** an estimate (callers
        that own the cost: eval runs, tests); a reset clears the estimate and
        its confirmation deadline. The console's uploads and re-ingests go
        through :meth:`begin_upload` / :meth:`begin_estimate`."""

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
    async def get_document_by_seq(
        self, *, tenant_id: UUID, doc_seq: int
    ) -> KnowledgeDocument | None:
        """The tenant's document ``D{doc_seq}`` (B-206 K-2), or ``None``. The
        number is tenant-wide: the caller checks the base."""

    @abc.abstractmethod
    async def find_documents_by_name(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        name: str,
        limit: int,
        exact: bool = False,
    ) -> list[KnowledgeDocument]:
        """The switched-on documents of ``kb_ids`` whose file name or title is
        ``name`` (``exact``) or contains it, case-insensitively and literally (no
        wildcards), by ``doc_seq``; at most ``limit`` (B-206 K-2:
        ``knowledge_read`` finding a document by name). A switched-off document
        is never listed: it is out of the agent's knowledge, as for recall. Both
        sides are case-folded by the same ``lower()`` (the database's, or
        Python's in memory)."""

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
        the claim or ``None`` if another worker already holds it.

        B-222 PR-2: an ``estimating`` document with no live lease is claimable
        too — for its estimate only: it stays ``estimating`` (fresh lease,
        ``attempts`` bumped) and the claim's ``phase`` is ``estimate``.
        ``awaiting_confirmation`` is never claimable."""

    @abc.abstractmethod
    async def claim_documents_for_ingest(
        self, *, now: datetime, lease_seconds: int, limit: int, max_attempts: int
    ) -> list[ClaimedIngestion]:
        """Cross-tenant batch CAS claim for the recovery worker (caller wraps
        it in a bypass-RLS scope). Claims up to ``limit`` claimable documents
        (``pending`` or lease-expired ``processing``, under ``max_attempts``),
        marking each ``processing`` with a fresh lease + bumped ``attempts``.
        This single scan drains the queue AND recovers crashed work. B-222
        PR-2: ``estimating`` documents too, as :meth:`claim_document`."""

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
        ``generation`` the write is fenced (see :meth:`set_document_status`;
        B-222 PR-2: an ``estimating`` document at that generation too — its
        estimate found the document cannot be ingested, free of charge).
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
        when nothing was pending, or when the pending version's ingest is past
        the point of no return (B-222: ``processing`` at ``indexing``, see
        :meth:`cancel_documents`) — it still ``has_pending``."""

    @abc.abstractmethod
    async def cancel_documents(
        self, *, tenant_id: UUID, kb_id: UUID, document_ids: Sequence[UUID]
    ) -> dict[UUID, CancelOutcome]:
        """B-222 —— stop the ingest of each of the base's ``pending`` /
        ``processing`` documents: status ``CANCELLED``, ``error='cancelled by
        user'``, ``ingest_generation`` bumped (the worker still busy with it
        is fenced out of every write, its lease renewal included) and the
        lease released; ``stage``, the byte slots and serving chunks are kept.
        ``processing`` at ``stage=indexing`` is past the point of no return
        (its new chunks are being swapped in, READY follows): the fenced stage
        write to it and the cancel serialise on the row, so a cancel never
        lands between the swap and READY. That holds whatever the lease: a
        worker whose lease ran out there may have died after its swap, with
        the new chunks serving — the recovery worker re-claims such a
        document. One outcome per distinct id:
        ``cancelled``; ``not_cancellable`` (any other status, ``cancelled``
        included, or at indexing — nothing is written); ``not_found`` (no such
        document in this base of this tenant).

        B-222 PR-2: an ``estimating`` / ``awaiting_confirmation`` document's
        estimate is withdrawn instead (generation bumped, its snapshot
        deleted): ``withdrawn_deleted`` — a new upload, deleted with its file;
        ``withdrawn`` — put back (see
        :func:`~expert_work.persistence.knowledge.estimates.withdraw_plan`).
        A ``pending`` / ``processing`` document cancelled loses its snapshot
        too."""

    @abc.abstractmethod
    async def sweep_terminal(self, *, now: datetime, max_attempts: int) -> int:
        """Cross-tenant: every ``processing`` document that has used up
        ``max_attempts`` and whose lease has expired goes ``FAILED``
        (``error='ingest attempts exhausted'``,
        ``error_code=attempts_exhausted``). Such a document is no longer
        claimable, so without this sweep it would sit in ``processing``
        forever (a crash on the last attempt). Returns the number swept.
        Caller wraps the call in a bypass-RLS scope. B-222 PR-2: an
        ``estimating`` document likewise (its estimate kept failing)."""

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
        ours (settled, re-uploaded, deleted). B-222 PR-2: or estimating
        (``status='estimating'``, same generation)."""

    @abc.abstractmethod
    async def list_documents(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        enabled: bool | None = None,
        statuses: Collection[DocumentStatus] | None = None,
    ) -> list[KnowledgeDocument]:
        """The base's documents, newest first. ``enabled`` / ``statuses``
        (B-175 P3-PR2) keep only the documents switched that way / in one of
        those statuses; ``None`` does not filter (an empty ``statuses``
        matches nothing)."""

    @abc.abstractmethod
    async def list_documents_page(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        limit: int,
        offset: int,
        enabled: bool | None = None,
        statuses: Collection[DocumentStatus] | None = None,
    ) -> tuple[list[KnowledgeDocument], int]:
        """A page of :meth:`list_documents` (same order and filters) plus the
        total count of the filtered list."""

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
        """Delete a document and cascade its chunks, page records,
        recognition cache and passage revisions. ``False`` if no document
        matched."""

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

    # -- figures (B-209 PR-2 §5.4) -------------------------------------------

    @abc.abstractmethod
    async def set_document_figures(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        stats: FigureStats | _Unset | None,
        pixel_hashes: Sequence[str],
        generation: int | None = None,
    ) -> bool:
        """Record the pixel hashes of the ingest's figures that passed the
        per-document filters (read by :meth:`count_figure_documents`) and,
        unless ``stats`` is :data:`UNSET`, what became of them
        (``figure_stats``; ``None`` clears it). Fenced like
        :meth:`set_document_pages` (``False`` = no longer this worker's)."""

    @abc.abstractmethod
    async def count_figure_documents(
        self, *, tenant_id: UUID, pixel_hashes: Sequence[str], exclude_document_id: UUID
    ) -> dict[str, int]:
        """For each pixel hash, how many of the tenant's **other** documents
        hold a figure with it (absent: none) — the tenant-wide repeated-image
        filter (a logo / letterhead in every document of a tenant)."""

    @abc.abstractmethod
    async def existing_document_ids(self, *, document_ids: Sequence[UUID]) -> set[UUID]:
        """Which of ``document_ids`` still have a document row, whatever the
        tenant — the figure reconciler's check (run with RLS bypassed)."""

    async def sees_every_tenant(self) -> bool:
        """Whether a lookup made here (with RLS bypassed) is proven to see
        every tenant's documents — only then may "no such document" be
        trusted to delete anything. A store that cannot prove it says no."""
        return False

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
        same) is replaced: a reading cached under the key is only read again
        when the current rules reject it, and the fresh one must then stand,
        or every later ingest would pay for it again."""

    # -- maintenance (B-175 P3-PR2) -------------------------------------------

    @abc.abstractmethod
    async def set_documents_enabled(
        self, *, tenant_id: UUID, kb_id: UUID, document_ids: Sequence[UUID], enabled: bool
    ) -> list[UUID]:
        """Switch the base's documents on or off. A disabled document keeps
        its chunks (and their own switches) and its place in every listing;
        recall leaves it out (:meth:`search`). Returns the ids that are this
        base's documents, in the order asked, each once — another base's,
        another tenant's and unknown ids are skipped."""

    @abc.abstractmethod
    async def count_stale_documents(self, *, tenant_id: UUID, kb_id: UUID, params_hash: str) -> int:
        """How many of the base's ``ready`` documents serve chunks cut by
        other chunking parameters: their ``chunk_params_hash`` differs from
        ``params_hash`` (the base's current one) or was never recorded."""

    @abc.abstractmethod
    async def list_stale_documents(
        self, *, tenant_id: UUID, kb_id: UUID, params_hash: str
    ) -> list[KnowledgeDocument]:
        """The documents :meth:`count_stale_documents` counts, in
        :meth:`list_documents` order."""

    @abc.abstractmethod
    async def list_passages(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        enabled_only: bool = False,
        offset: int = 0,
        limit: int | None = None,
        disabled_reason: DisabledReason | None = None,
    ) -> list[Passage]:
        """The document's passages in document order: a passage sits where
        its first chunk does (the lowest ``chunk_index`` of its children — a
        parent row's own number is in another numbering), a chunk standing
        for itself where it is. Each passage's children in ``chunk_index``
        order, grouped by ``parent_chunk_id``; their numbers need not be
        consecutive (an edit numbers a passage's extra chunks after the
        document's last). ``enabled_only`` leaves out what recall leaves out:
        switched-off passages, and everything when the document is switched
        off. ``disabled_reason`` keeps only the passages switched off for
        that reason (``inherited``: the ones to review). ``offset`` /
        ``limit`` page through the passages; another tenant's document has
        none. Vectors are not loaded."""

    @abc.abstractmethod
    async def count_passages(self, *, tenant_id: UUID, document_id: UUID) -> int:
        """How many passages :meth:`list_passages` lists (unfiltered)."""

    @abc.abstractmethod
    async def get_passage(
        self, *, tenant_id: UUID, document_id: UUID, passage_id: UUID
    ) -> Passage | None:
        """One passage of the document by its id (its parent row's, or its
        lone chunk's); ``None`` when the document has no such passage."""

    @abc.abstractmethod
    async def replace_passage(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        passage_id: UUID,
        expected_revision: int,
        content: str,
        heading_path: Sequence[str],
        children: Sequence[PassageChild],
        edited_by: str | None,
    ) -> Passage | None:
        """Write a passage's new text and its chunks in one transaction; the
        passage keeps its id. The chunks are numbered like the ones they
        replace (in order) and any extra one after the document's last
        number; the document's ``chunk_count`` follows. A lone chunk whose new
        text needs several becomes a parent row with the same id.

        History: the first change of a passage ingested from the file first
        records its text as revision 1 (``edited_by`` ``None``); every change
        adds the next revision (restoring an old text is a change too). The
        passage is ``edited`` while its text differs from that first,
        machine-made revision — always for one added by hand.

        Refused with :class:`PassageConflictError`: ``revision_conflict`` when
        the passage is no longer at ``expected_revision``;
        ``document_busy`` unless the document is ``ready`` with no new
        version pending. ``None`` when there is no such passage."""

    @abc.abstractmethod
    async def add_passage(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        content: str,
        heading_path: Sequence[str],
        children: Sequence[PassageChild],
        edited_by: str | None,
    ) -> Passage | None:
        """Add a passage at the end of the document: its chunks numbered
        after the last, a parent row unless it is just its one chunk;
        revision 1 records the text and its author, and it is ``edited``.
        ``document_busy`` as :meth:`replace_passage`; ``None`` when there is
        no such document."""

    @abc.abstractmethod
    async def set_passage_enabled(
        self, *, tenant_id: UUID, document_id: UUID, passage_id: UUID, enabled: bool
    ) -> Passage | None:
        """Switch a passage on or off — its parent row and every chunk under
        it; switched off by hand, ``disabled_reason`` is ``manual`` (cleared
        when switched on). An edit keeps the switch and its reason.
        ``document_busy`` as :meth:`replace_passage`; ``None`` when there is
        no such passage.

        Switching a passage that was off by inheritance (either way) settles
        it: its paragraphs leave the document's switch-off records
        (:func:`~expert_work.persistence.knowledge.passages.settle_switch_off`;
        a record left empty is deleted), so a later ingest does not hide them
        again for those records — switched on it stays on, switched off it is
        the person's own (``manual``) from then on."""

    @abc.abstractmethod
    async def list_passage_revisions(
        self, *, tenant_id: UUID, document_id: UUID, passage_id: UUID
    ) -> list[PassageRevision]:
        """The passage's recorded text revisions, oldest first (switch-off
        records are not text revisions: :meth:`list_switch_offs`)."""

    @abc.abstractmethod
    async def list_switch_offs(
        self, *, tenant_id: UUID, document_id: UUID
    ) -> list[PassageRevision]:
        """The document's switch-off records (``switch_off_lost``, written by
        :meth:`replace_chunks`): what every ingest derives inherited
        switch-offs from."""

    @abc.abstractmethod
    async def list_unapplied_revisions(
        self, *, tenant_id: UUID, document_id: UUID
    ) -> list[PassageRevision]:
        """Manual changes the document's chunks no longer carry: per passage
        with a history, its latest revision when that passage is gone (a
        re-ingest could not find it again) and the revision holds more than
        the machine text, or when the passage is still there at an older
        revision; and each switch-off record no passage holds the text of
        (``orphaned_at`` set). Oldest first. A history whose passage a
        re-ingest left out carries ``orphaned_at`` (stamped by
        :meth:`replace_chunks`); it is kept until
        :meth:`discard_unapplied_revisions`, the document's deletion, or the
        retention job (30 days after ``orphaned_at``)."""

    @abc.abstractmethod
    async def discard_unapplied_revisions(self, *, tenant_id: UUID, document_id: UUID) -> int:
        """Delete what :meth:`list_unapplied_revisions` lists: the whole
        history of every passage that is gone, the revisions above the
        current one of a passage still there, and the switch-off records no
        passage holds the text of. Returns the rows deleted."""

    @abc.abstractmethod
    async def count_review_items(
        self, *, tenant_id: UUID, document_ids: Sequence[UUID]
    ) -> dict[UUID, ReviewCounts]:
        """B-175 P3-PR3 — per document of ``document_ids``, what waits for a
        person: the passages switched off by inheritance (a passage once, not
        each of its chunks) and what :meth:`list_unapplied_revisions` lists.
        Only documents with something to review are in the result (another
        tenant's and unknown ids never are). One read for all of them — a
        document list page asks it once."""

    @abc.abstractmethod
    async def list_manual_passages(
        self, *, tenant_id: UUID, document_id: UUID
    ) -> list[ManualPassage]:
        """The document's passages a re-ingest must carry over (edited,
        switched off, added by hand, or with a history), in document order,
        each with its first revision and its ``occurrence`` among the
        document's passages of the same machine text and breadcrumb
        (:func:`~expert_work.persistence.knowledge.passages.manual_passages`)."""

    @abc.abstractmethod
    async def update_chunk_embeddings(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        vectors: Sequence[ChunkVector],
        embedding_model: str | None,
        generation: int,
        expect_status: DocumentStatus = DocumentStatus.READY,
    ) -> int | None:
        """A re-index's write-back: set each chunk's vector and
        ``embedding_model`` in place, only where the chunk still has that id
        and ``content_hash`` — a chunk a passage write replaced meanwhile
        (that write embedded it with the live model) is left as it is, and
        nothing else of any row (text, switches, parent rows) is touched, so
        a re-index can never undo a passage write. Fenced: ``None`` (nothing
        written) unless the document is in ``expect_status`` at
        ``generation`` (as :meth:`replace_chunks`: a document serving its
        chunks in another status, e.g. a failed re-upload's, is re-embedded
        with that status); otherwise the number of chunks updated.
        B-222 PR-2: ``estimating`` and ``awaiting_confirmation`` stand for each
        other (an estimate finishing does not bump the generation, and the
        chunks serving meanwhile are the previous version's either way)."""

    # -- estimates (B-222 PR-2) ---------------------------------------------

    @abc.abstractmethod
    async def begin_upload(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        filename: str,
        content: bytes,
        content_sha256: str,
        now: datetime,
    ) -> UploadStart:
        """An upload, to be estimated before anything is spent — the bytes go
        where :meth:`upsert_document` puts them (a new document's into
        ``content``, an existing one's into the pending slot) and the serving
        version keeps serving. Outcomes:

        * ``unchanged`` — the same bytes the document is ready with, or is
          already being estimated / waiting with: nothing is touched;
        * ``duplicate`` — another document of the **same base** holds these
          bytes (serving or pending): ``awaiting_confirmation`` at once, with an
          unparsed estimate naming it (``duplicate_of``, nothing to pay,
          ``confirm_by`` = ``now`` + 7 days);
        * ``estimating`` — otherwise;
        * ``busy`` — the document is past the point of no return (its new
          chunks are being swapped in, ``estimates.writing_chunks``): nothing
          is touched, upload again once it is ready.

        The same bytes already queued or being ingested (``pending`` /
        ``processing``) are ``unchanged`` too: a confirmed ingest is not
        restarted.

        Either way but ``unchanged`` / ``busy``: ``ingest_generation`` bumped, ``attempts``
        0, lease, ``error`` / ``error_code`` cleared, ``stage=queued``; the
        estimate meta records the origin (``upload`` for a document this call
        created, else ``reupload``) and how to put the document back. A
        document already waiting keeps what it would be put back to."""

    @abc.abstractmethod
    async def begin_estimate(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        origin: EstimateOrigin,
        allow_duplicate: bool = False,
        expect_status: DocumentStatus | None = None,
        expect_estimate_id: UUID | None = None,
    ) -> KnowledgeDocument | None:
        """A re-ingest, 「补识别」, re-chunk or batch re-ingest of the bytes the
        document has (``origin``), or — ``allow_duplicate`` — a duplicate the
        user wants ingested anyway: ``estimating``, with the same resets as
        :meth:`begin_upload` (byte slots untouched); its snapshot is deleted.
        ``None`` when there is no such document.

        :raises ~expert_work.persistence.knowledge.estimates.EstimateRefused:
            nothing changed — ``busy`` (past the point of no return), or the
            document is no longer in ``expect_status`` / under
            ``expect_estimate_id`` (checked with the row locked)."""

    @abc.abstractmethod
    async def finish_estimate(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        generation: int,
        estimate: Mapping[str, Any],
        snapshot: ParseSnapshot | None,
        confirm_by: datetime,
    ) -> KnowledgeDocument | None:
        """Fenced (``estimating`` at ``generation``): the estimate (a dumped
        :class:`~expert_work.protocol.knowledge_estimate.IngestEstimate`) is
        recorded with the document's estimate meta, the document goes
        ``awaiting_confirmation`` until ``confirm_by`` (lease released), and
        ``snapshot`` replaces the document's (``None`` deletes it) — one
        transaction. New bytes are checked once more against the base: when a
        document created before this one holds them, the estimate names it
        (``duplicate_of``) — two identical files uploaded at once. ``None``
        (nothing written) when the document is no longer this estimate's."""

    @abc.abstractmethod
    async def confirm_documents(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        estimates: Sequence[tuple[UUID, UUID]],
        confirmed_by: str | None,
        now: datetime,
    ) -> dict[UUID, ConfirmOutcome]:
        """Confirm ``(document_id, estimate_id)`` pairs — a compare-and-set on
        the estimate id: ``awaiting_confirmation`` → ``pending`` with
        ``ingest_generation`` bumped, ``attempts`` 0, ``confirm_by`` cleared,
        ``confirmed_at`` / ``confirmed_by`` recorded and the snapshot kept one
        more day. One outcome per distinct document: ``confirmed``,
        ``estimate_changed`` (estimated again since), ``not_awaiting``,
        ``duplicate`` (caught on upload, never estimated — estimate it first),
        ``expired`` (its ``confirm_by`` has passed: it is withdrawn, never
        confirmed), ``not_found`` (not in this base)."""

    @abc.abstractmethod
    async def confirm_all(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        expected_count: int,
        expected_total_micros: int,
        expected_fingerprint: str,
        confirmed_by: str | None,
        now: datetime,
        withdraw_duplicates: Sequence[UUID] = (),
    ) -> ConfirmAllResult:
        """Confirm every confirmable document waiting in the base — only when
        they are still exactly the estimates the user was shown
        (``expected_fingerprint``, :attr:`EstimateSummary.fingerprint`) and
        number ``expected_count`` and add up to ``expected_total_micros``
        (checked with the rows locked), else nothing changes. One past its
        ``confirm_by`` is not confirmable (``skipped``: left for the expiry
        sweep). Duplicates are never confirmed by it; those
        of ``withdraw_duplicates`` (the ones the user was shown) still waiting
        as duplicates are withdrawn in the same transaction — never one that
        arrived since."""

    @abc.abstractmethod
    async def return_to_confirmation(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        generation: int,
        estimate: Mapping[str, Any],
        snapshot: ParseSnapshot | None,
        confirm_by: datetime,
    ) -> bool:
        """§6.3 —— fenced (``processing`` at ``generation``), before anything is
        spent: the confirmed estimate no longer holds (a model changed, or the
        total rose past the threshold), so the document waits for the new one
        (``awaiting_confirmation`` until ``confirm_by``, lease released, the
        estimate meta kept, ``snapshot`` replacing the old one)."""

    @abc.abstractmethod
    async def release_stale_duplicates(
        self, *, tenant_id: UUID | None = None, kb_id: UUID | None = None
    ) -> list[KnowledgeDocument]:
        """K-21 ①, §7 —— duplicates whose original no longer holds their
        bytes (withdrawn, deleted, replaced) lose the mark
        (:func:`~expert_work.persistence.knowledge.estimates.released_duplicate`):
        a real estimate waits as a plain one; one never parsed goes
        ``estimating`` (generation bumped) for the caller or the recovery
        worker to estimate. Within ``tenant_id`` / ``kb_id`` when given, else
        across tenants (caller bypasses RLS). Returns the documents changed."""

    @abc.abstractmethod
    async def expire_awaiting(self, *, now: datetime, limit: int) -> list[ExpiredEstimate]:
        """Cross-tenant (caller bypasses RLS): withdraw up to ``limit``
        estimates whose ``confirm_by`` has passed, as :meth:`cancel_documents`
        withdraws one. Rows another sweep holds are skipped; each estimate is
        withdrawn once however many sweep at the same time."""

    @abc.abstractmethod
    async def get_parse_snapshot(
        self, *, tenant_id: UUID, document_id: UUID, estimate_id: UUID
    ) -> ParseSnapshot | None:
        """The document's snapshot — only the one made with ``estimate_id``."""

    @abc.abstractmethod
    async def purge_expired_snapshots(self, *, now: datetime) -> int:
        """Cross-tenant (caller bypasses RLS): delete snapshots past their
        ``expires_at``; how many."""

    @abc.abstractmethod
    async def record_ingest_actual(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        generation: int,
        estimate_id: UUID | None,
        actual: Mapping[str, Any],
        settled: bool,
    ) -> bool:
        """Keep what a paid ingest was billed (a dumped
        :class:`~expert_work.protocol.knowledge_estimate.IngestActual`) — when
        the document is still that claim's, or was cancelled out of it, and
        still carries the estimate it was confirmed under
        (:func:`~expert_work.persistence.knowledge.estimates.actual_lands`); a
        retry of the same ingest adds to the earlier attempts'. ``settled``:
        the ingest is over — its snapshot is deleted. ``False`` when nothing
        was written."""

    @abc.abstractmethod
    async def estimate_summary(
        self, *, tenant_id: UUID, kb_id: UUID, now: datetime | None = None
    ) -> EstimateSummary:
        """The base's documents being estimated or waiting for a confirmation,
        added up (:func:`~expert_work.persistence.knowledge.estimates.summarize`)."""

    @abc.abstractmethod
    async def chunk_hashes_on_model(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        content_hashes: Sequence[str],
        embedding_model: str,
    ) -> set[str]:
        """Which of ``content_hashes`` the document already has a vector for,
        made by ``embedding_model`` (:meth:`chunk_embeddings_by_hash` without
        reading the vectors) — what an estimate does not count."""

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
        chunk_params_hash: str | None = None,
        switch_offs: Collection[SwitchOffRecord] = (),
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
        ``False`` is returned. ``True`` when the chunks landed.

        ``chunk_params_hash`` (B-206 K-3) is recorded on the document with the
        chunks: the chunking they were cut with. ``None`` leaves it as it is
        (a re-index swaps vectors, not the chunking).

        B-175 P3-PR2: the revision histories of passages the new chunks no
        longer have get ``orphaned_at`` (their manual change was not put
        back; see :meth:`list_unapplied_revisions`). ``switch_offs``: the
        switch-off records the ingest derived its inherited switch-offs from
        (new ones for switched-off passages it left out, and the document's
        earlier ones), with ``orphaned_at`` cleared when ``placed``, else set
        (kept if it already was) — in the same transaction. Only a ``new``
        record is inserted; an earlier one is only updated where it still
        exists, so one discarded (or deleted by the retention job) while the
        ingest ran stays gone. Records it does not name are left as they are;
        text histories never get records' flags or vice versa."""

    @abc.abstractmethod
    async def list_chunks(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        offset: int = 0,
        limit: int = 50,
        chunk_types: Collection[ChunkType] | None = None,
        after: tuple[int, ChunkType] | None = None,
    ) -> tuple[list[KnowledgeChunk], int]:
        """A page of a document's chunks of ``chunk_types`` ordered by
        ``chunk_index`` (then type) plus their total count — for segment
        preview. ``chunk_types`` ``None`` = the searchable types
        (:data:`~expert_work.protocol.SEARCHABLE_CHUNK_TYPES`): parent rows
        (B-206 K-1) are only listed when asked for. ``after``: only the
        chunks ordered after that ``(chunk_index, chunk_type)`` — a keyset
        page, which rows going or coming before it do not shift (B-175
        P3-PR2: a re-index reads a document while its passages are edited).
        The (large) embedding vector is omitted from the returned chunks
        (``embedding == ()``; ``None`` for a parent, which has none)."""

    @abc.abstractmethod
    async def get_chunks(
        self, *, tenant_id: UUID, chunk_ids: Sequence[UUID]
    ) -> list[KnowledgeChunk]:
        """The tenant's chunks with these ids, of any type — each once, in
        the order first asked for; ids with no such chunk (or another
        tenant's) are skipped. What the retriever reads a hit's parent row
        with (B-206 K-1). Vectors are omitted as in :meth:`list_chunks`."""

    async def chunks_contain(self, *, tenant_id: UUID, document_id: UUID, text: str) -> bool:
        """Whether any chunk of the document holds ``text`` (the figure
        endpoint's check that a document's chunks still name a figure)."""
        offset = 0
        while True:
            page, total = await self.list_chunks(
                tenant_id=tenant_id, document_id=document_id, offset=offset, limit=200
            )
            if any(text in chunk.content for chunk in page):
                return True
            offset += len(page)
            if not page or offset >= total:
                return False

    @abc.abstractmethod
    async def chunk_embeddings_by_hash(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        content_hashes: Sequence[str],
        embedding_model: str,
    ) -> dict[str, Sequence[float]]:
        """The stored vectors of this document's chunks whose ``content_hash``
        is in ``content_hashes`` **and** that ``embedding_model`` produced — a
        re-ingest reuses them instead of embedding unchanged chunks again,
        never across a model change. Only the given document's chunks
        (another document's identical text is not borrowed); hashes with no
        matching chunk are absent; a parent row has no vector to give. A
        document can have tens of thousands of chunks, so an implementation
        should hand back compact vectors (``array("d")``), not tuples of
        Python floats (B-216)."""

    @abc.abstractmethod
    async def has_chunks_off_model(
        self, *, tenant_id: UUID, kb_id: UUID, embedding_model: str | None
    ) -> bool:
        """Whether any searchable chunk of the base was embedded by another
        model than ``embedding_model`` (a ``NULL`` model counts as another) —
        what a re-index checks before it stamps the base with the new model
        (B-222: a re-upload that landed meanwhile and was then cancelled or
        failed leaves its previous chunks serving on the old model)."""

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
        ``kb_ids`` yields an empty list. Recall — this and every search
        method — only returns chunks of
        :data:`~expert_work.protocol.SEARCHABLE_CHUNK_TYPES` (never a parent
        row) that are enabled, of a document that is enabled (B-175
        P3-PR2); the others are filtered out before ``limit`` is applied."""

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
        phrases, idf = ln(1 + (N - df + 0.5) / (df + 0.5)) with N the searchable
        chunks in scope and df those holding the unit; ≤ 1) plus ``ts_rank`` (< 0.1, a
        tie-break), plus
        :data:`~expert_work.persistence.knowledge.text_search.KB_KEYWORD_PHRASE_BOOST`
        when an identifier phrase matched and
        :data:`~expert_work.persistence.knowledge.text_search.KB_KEYWORD_QUERY_PHRASE_BOOST`
        when the whole question appears verbatim. SQL and in-memory stores share the
        match predicate and the ordering intent; their exact scores (and so
        the order of near-ties) can differ.

        Raises :class:`KnowledgeRecallError` when the database could not finish
        the query — the SQL store bounds it with a statement timeout (B-206
        K-22); the in-memory store never raises it. Any other error, a database
        error included, is not a recall failure and propagates as itself."""
