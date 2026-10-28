"""SQLAlchemy + pgvector ``KnowledgeStore`` (Postgres / asyncpg)."""

from __future__ import annotations

import logging
import operator
import re
from array import array
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Float,
    Row,
    Select,
    Text,
    and_,
    any_,
    case,
    cast,
    column,
    delete,
    func,
    literal,
    literal_column,
    or_,
    select,
    text,
    true,
    tuple_,
    update,
)
from sqlalchemy import values as sa_values
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import CTE

from expert_work.persistence.embedding import EMBEDDING_DIM
from expert_work.persistence.knowledge.base import (
    ALL_TENANTS_BASES_LIMIT,
    UNSET,
    CancelOutcome,
    ClaimedIngestion,
    DocumentFileFacts,
    DuplicateKnowledgeBaseError,
    KnowledgeRecallError,
    KnowledgeStore,
    ParseCacheEntry,
    ParseCacheKind,
    RowSecurityActiveError,
    _Unset,
)
from expert_work.persistence.knowledge.passages import (
    SWITCH_OFF_REVISION,
    ChunkVector,
    DisabledReason,
    ManualPassage,
    Passage,
    PassageChild,
    PassageConflictError,
    PassageLayout,
    PassageRevision,
    SwitchOffRecord,
    edited_since,
    manual_passages,
    passage_rows,
    settle_switch_off,
    unapplied_revisions,
)
from expert_work.persistence.knowledge.text_search import (
    KB_DOC_TITLE_HIT_WEIGHT,
    KB_KEYWORD_PHRASE_BOOST,
    KB_KEYWORD_QUERY_PHRASE_BOOST,
    KbKeywordQuery,
    build_kb_keyword_query,
    kb_index_text,
    tokenize_for_search,
)
from expert_work.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeChunkRevisionRow,
    KnowledgeChunkRow,
    KnowledgeDocSeqRow,
    KnowledgeDocumentRow,
    KnowledgeParseCacheRow,
)
from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    DEFAULT_RETRIEVAL_TOP_K,
    SEARCHABLE_CHUNK_TYPES,
    ChunkType,
    DocumentPage,
    DocumentSourceType,
    DocumentStatus,
    FigureStats,
    IngestErrorCode,
    IngestStage,
    KnowledgeBase,
    KnowledgeBaseKind,
    KnowledgeChunk,
    KnowledgeDocument,
    PageStats,
    RetrievalMethod,
    ScoredChunk,
    chunk_retrieval_text,
)

logger = logging.getLogger(__name__)

#: Postgres text-search config — chunks are pre-segmented app-side, so
#: ``simple`` (no stemming / stopwords) is the correct universal config.
_TS_CONFIG = "simple"

#: Chunk rows inserted per flush in :meth:`SqlKnowledgeStore.replace_chunks`.
_CHUNK_INSERT_BATCH = 500

#: Content hashes looked up per query in
#: :meth:`SqlKnowledgeStore.chunk_embeddings_by_hash`: each batch's vectors are
#: made 8-byte floats as they arrive, so a re-ingest never holds every stored
#: vector of a document as Python floats at once (B-216).
_EMBEDDING_READ_BATCH = 500

#: ``hnsw.ef_search`` for vector recall (raised to the request's ``limit`` when
#: deeper). An HNSW scan returns at most ef_search rows *before* the tenant /
#: base filter, and the server default (40) is below the retriever's default
#: recall depth (50). 200 = twice the deepest default recall (100), so even
#: without iterative scan a filtered scan has headroom; it is still far below
#: pgvector's cap (1000) and costs a few ms per query.
_HNSW_EF_SEARCH = 200
#: pgvector rejects ``hnsw.ef_search`` above this.
_HNSW_EF_SEARCH_MAX = 1000
#: pgvector >= 0.8: keep scanning the index until ``limit`` rows survive the
#: filter (a small tenant in a big table), in exact distance order.
_HNSW_ITERATIVE_SCAN_GUC = "hnsw.iterative_scan"
_HNSW_ITERATIVE_SCAN = "strict_order"
_ITERATIVE_SCAN_MIN_VERSION = (0, 8)
#: ``statement_timeout`` of the keyword recall statement, for its transaction
#: only (B-206 K-22): a plan gone wrong has taken 10 to 44 s, and a hybrid search
#: keeps its vector results when this path fails rather than waiting it out.
_KEYWORD_STATEMENT_TIMEOUT_MS = 5_000
#: SQLSTATEs that mean the keyword query could not finish, not that it is wrong —
#: the only database errors it degrades on (:class:`KnowledgeRecallError`):
#: 57014 statement timeout / cancelled, 55P03 lock not available, 40001
#: serialization failure, 40P01 deadlock, 57P01-57P03 the server shutting down or
#: starting; and the classes 08 (connection) and 53 (insufficient resources). A
#: bug (42883 undefined function, 42703 undefined column) or a missing permission
#: (42501) propagates: degrading it would hide it behind vector-only results.
_RECALL_FAILED_SQLSTATES = frozenset(
    {"57014", "55P03", "40001", "40P01", "57P01", "57P02", "57P03"}
)
_RECALL_FAILED_SQLSTATE_CLASSES = frozenset({"08", "53"})
#: SQLSTATEs that mean the server refused the setting itself — the only
#: failures that turn iterative scan off: 22023 invalid value, 42602 a name
#: under an extension's reserved prefix that this build does not define (what
#: pgvector < 0.8 answers for ``hnsw.iterative_scan``), 42704 unrecognized
#: parameter. Anything else (dropped connection, timeout, ...) propagates.
_SETTING_REJECTED_SQLSTATES = frozenset({"22023", "42602", "42704"})

#: Terminal ingestion states — reaching one clears the durability lease so the
#: document is never re-claimed.
_TERMINAL_STATUSES = (DocumentStatus.READY.value, DocumentStatus.FAILED.value)

#: ``error`` written by :meth:`SqlKnowledgeStore.sweep_terminal` — same text as
#: the in-memory store so operators see one message.
_EXHAUSTED_ERROR = "ingest attempts exhausted"
#: ``error`` after discarding a pending upload when nothing was serving.
_DISCARDED_ERROR = "pending upload discarded; nothing is serving"
#: B-222 —— the ``error`` :meth:`SqlKnowledgeStore.cancel_documents` writes
#: (same text as the in-memory store).
_CANCELLED_ERROR = "cancelled by user"


def _fence(
    generation: int | None, status: DocumentStatus = DocumentStatus.PROCESSING
) -> list[ColumnElement[bool]]:
    """Extra predicates for a fenced write-back: the document must still be
    in ``status`` with the exact generation the worker claimed. Empty (no
    fence) when ``generation`` is ``None`` — legacy callers."""
    if generation is None:
        return []
    return [
        KnowledgeDocumentRow.status == status.value,
        KnowledgeDocumentRow.ingest_generation == generation,
    ]


def _documents_where(
    tenant_id: UUID,
    kb_id: UUID,
    enabled: bool | None = None,
    statuses: Collection[DocumentStatus] | None = None,
) -> list[ColumnElement[bool]]:
    """The base's documents, optionally only those switched ``enabled`` and
    in one of ``statuses`` (an empty collection matches none)."""
    where = [KnowledgeDocumentRow.tenant_id == tenant_id, KnowledgeDocumentRow.kb_id == kb_id]
    if enabled is not None:
        where.append(KnowledgeDocumentRow.enabled.is_(enabled))
    if statuses is not None:
        where.append(KnowledgeDocumentRow.status.in_(sorted(s.value for s in statuses)))
    return where


def _stale_where(tenant_id: UUID, kb_id: UUID, params_hash: str) -> list[ColumnElement[bool]]:
    """Ready documents whose chunks were cut by other parameters (or by
    parameters never recorded)."""
    return [
        *_documents_where(tenant_id, kb_id, statuses=(DocumentStatus.READY,)),
        or_(
            KnowledgeDocumentRow.chunk_params_hash.is_(None),
            KnowledgeDocumentRow.chunk_params_hash != params_hash,
        ),
    ]


def _claimable(now: datetime, max_attempts: int) -> ColumnElement[bool]:
    """A document is claimable when it is ``pending``, or ``processing`` with
    an expired (or absent) lease, and still under its retry budget."""
    return and_(
        KnowledgeDocumentRow.attempts < max_attempts,
        or_(
            KnowledgeDocumentRow.status == DocumentStatus.PENDING.value,
            and_(
                KnowledgeDocumentRow.status == DocumentStatus.PROCESSING.value,
                or_(
                    KnowledgeDocumentRow.lease_until.is_(None),
                    KnowledgeDocumentRow.lease_until < now,
                ),
            ),
        ),
    )


def _cancellable() -> ColumnElement[bool]:
    """B-222 —— a document :meth:`SqlKnowledgeStore.cancel_documents` may still
    stop: ``pending``, or ``processing`` short of the point of no return
    (:func:`_writing_chunks`)."""
    return or_(
        KnowledgeDocumentRow.status == DocumentStatus.PENDING.value,
        and_(
            KnowledgeDocumentRow.status == DocumentStatus.PROCESSING.value,
            # NOT at indexing, NULL-safe (a NULL stage must not make the whole
            # condition NULL).
            or_(
                KnowledgeDocumentRow.stage.is_(None),
                KnowledgeDocumentRow.stage != IngestStage.INDEXING.value,
            ),
        ),
    )


def _writing_chunks(row: KnowledgeDocumentRow) -> bool:
    """B-222 —— past the point of no return: ``processing`` at ``indexing``
    (the new chunks are being swapped in, READY follows), whatever the lease —
    a worker whose lease ran out may have died after its swap, with the new
    chunks serving; the recovery worker re-claims it. The fenced stage write
    to indexing and a cancel / discard serialise on the row."""
    return row.status == DocumentStatus.PROCESSING.value and row.stage == IngestStage.INDEXING.value


#: What a claim's ``RETURNING`` reads: the document's identity and counters,
#: and the bytes to ingest — the pending upload when there is one, else the
#: serving version, with its hash. The other byte slot is never read.
_CLAIM_COLUMNS = (
    KnowledgeDocumentRow.id,
    KnowledgeDocumentRow.tenant_id,
    KnowledgeDocumentRow.kb_id,
    KnowledgeDocumentRow.filename,
    KnowledgeDocumentRow.attempts,
    KnowledgeDocumentRow.ingest_generation,
    func.coalesce(KnowledgeDocumentRow.pending_content, KnowledgeDocumentRow.content).label(
        "content"
    ),
    case(
        (
            KnowledgeDocumentRow.pending_content.is_not(None),
            KnowledgeDocumentRow.pending_content_sha256,
        ),
        else_=KnowledgeDocumentRow.content_sha256,
    ).label("content_sha256"),
)


def _enum_or[E: StrEnum](enum: type[E], value: str | None, default: E | None) -> E | None:
    """Read a free-text enum column without failing on a value this version
    does not know (written by a newer release before a rollback): unknown →
    ``default`` instead of a 500 on every list call. ``None`` stays ``None``."""
    if value is None:
        return None
    try:
        return enum(value)
    except ValueError:
        return default


def _to_base(row: KnowledgeBaseRow) -> KnowledgeBase:
    return KnowledgeBase(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        description=row.description,
        created_by=row.created_by,
        chunk_max_tokens=row.chunk_max_tokens,
        chunk_overlap_tokens=row.chunk_overlap_tokens,
        retrieval_top_k=row.retrieval_top_k,
        retrieval_score_threshold=row.retrieval_score_threshold,
        retrieval_method=RetrievalMethod(row.retrieval_method),
        rerank_enabled=row.rerank_enabled,
        rerank_score_threshold=row.rerank_score_threshold,
        rerank_threshold_model=row.rerank_threshold_model,
        embedding_provider=row.embedding_provider,
        embedding_model=row.embedding_model,
        reindex_requested_at=row.reindex_requested_at,
        kind=_enum_or(KnowledgeBaseKind, row.kind, KnowledgeBaseKind.DOCUMENT)
        or KnowledgeBaseKind.DOCUMENT,
        metadata_schema=row.metadata_schema,
        api_access_enabled=row.api_access_enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_document(row: KnowledgeDocumentRow) -> KnowledgeDocument:
    # A status this version does not know (written by a newer release before a
    # rollback) reads as failed, saying so — never a 500 on every list call.
    status = _enum_or(DocumentStatus, row.status, None)
    return KnowledgeDocument(
        id=row.id,
        tenant_id=row.tenant_id,
        kb_id=row.kb_id,
        filename=row.filename,
        status=status or DocumentStatus.FAILED,
        error=row.error if status is not None else f"status {row.status!r} unknown to this version",
        chunk_count=row.chunk_count,
        attempts=row.attempts,
        ingest_generation=row.ingest_generation,
        has_pending=row.has_pending,
        has_content=row.has_content,
        doc_seq=row.doc_seq,
        title=row.title,
        source_type=_enum_or(DocumentSourceType, row.source_type, DocumentSourceType.FILE)
        or DocumentSourceType.FILE,
        source_uri=row.source_uri,
        external_id=row.external_id,
        metadata=row.doc_metadata,
        enabled=row.enabled,
        stage=_enum_or(IngestStage, row.stage, None),
        progress=row.progress,
        error_code=_enum_or(IngestErrorCode, row.error_code, IngestErrorCode.INTERNAL_ERROR),
        page_count=row.page_count,
        page_stats=PageStats.model_validate(row.page_stats) if row.page_stats else None,
        figure_stats=FigureStats.model_validate(row.figure_stats) if row.figure_stats else None,
        chunk_params_hash=row.chunk_params_hash,
        size_bytes=row.size_bytes,
        mime=row.mime,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


#: ``knowledge_chunk`` columns a chunk DTO is built from, minus the embedding
#: (preview reads must never SELECT the large vector).
_CHUNK_COLUMNS = (
    KnowledgeChunkRow.id,
    KnowledgeChunkRow.tenant_id,
    KnowledgeChunkRow.kb_id,
    KnowledgeChunkRow.document_id,
    KnowledgeChunkRow.chunk_index,
    KnowledgeChunkRow.content,
    KnowledgeChunkRow.chunk_type,
    KnowledgeChunkRow.heading_path,
    KnowledgeChunkRow.content_hash,
    KnowledgeChunkRow.embedding_model,
    KnowledgeChunkRow.enabled,
    KnowledgeChunkRow.disabled_reason,
    KnowledgeChunkRow.edited,
    KnowledgeChunkRow.revision,
    KnowledgeChunkRow.parent_chunk_id,
    KnowledgeChunkRow.source_locator,
    KnowledgeChunkRow.created_at,
    KnowledgeChunkRow.doc_title,
)


def _to_chunk(row: Any, embedding: tuple[float, ...] | None = ()) -> KnowledgeChunk:
    """Build a chunk DTO from a ``KnowledgeChunkRow`` or a ``_CHUNK_COLUMNS``
    projection row (same attribute names). A parent row has no vector, so its
    ``embedding`` is ``None`` whatever was passed (``()`` = not loaded)."""
    chunk_type = _enum_or(ChunkType, row.chunk_type, ChunkType.TEXT) or ChunkType.TEXT
    return KnowledgeChunk(
        id=row.id,
        tenant_id=row.tenant_id,
        kb_id=row.kb_id,
        document_id=row.document_id,
        chunk_index=row.chunk_index,
        content=row.content,
        embedding=None if chunk_type is ChunkType.PARENT else embedding,
        chunk_type=chunk_type,
        heading_path=tuple(row.heading_path or ()),
        doc_title=row.doc_title,
        content_hash=row.content_hash,
        embedding_model=row.embedding_model,
        enabled=row.enabled,
        disabled_reason=_disabled_reason(row.disabled_reason),
        edited=row.edited,
        revision=row.revision,
        parent_chunk_id=row.parent_chunk_id,
        source_locator=row.source_locator,
        created_at=row.created_at,
    )


def _disabled_reason(value: str | None) -> DisabledReason | None:
    """``knowledge_chunk.disabled_reason`` as the DTO takes it (the column's
    CHECK allows only these two)."""
    if value == "inherited":
        return "inherited"
    return "manual" if value is not None else None


def _to_chunk_with_embedding(row: KnowledgeChunkRow) -> KnowledgeChunk:
    vector = row.embedding
    return _to_chunk(row, None if vector is None else tuple(float(value) for value in vector))


#: ``chunk_type`` values recall may return, sorted so the statement is the same
#: from one process to the next.
_SEARCHABLE_TYPE_VALUES = tuple(sorted(chunk_type.value for chunk_type in SEARCHABLE_CHUNK_TYPES))


def _chunk_type_in(chunk_types: Collection[ChunkType] | None) -> ColumnElement[bool]:
    """Rows of ``chunk_types``; ``None`` = the searchable types — an allow-list,
    so a type added later (or one only a newer release knows) stays out."""
    values = (
        _SEARCHABLE_TYPE_VALUES
        if chunk_types is None
        else sorted(chunk_type.value for chunk_type in chunk_types)
    )
    return KnowledgeChunkRow.chunk_type.in_(values)


def _recall_scope(tenant_id: UUID, kb_ids: Sequence[UUID]) -> tuple[ColumnElement[bool], ...]:
    """What recall may return — and so BM25's N: the tenant's chunks in
    ``kb_ids`` of the searchable types, enabled, of a document that is not
    disabled (B-175 P3-PR2). The disabled documents are one uncorrelated
    ``NOT IN`` (a hashed filter on the scan), so a vector search still walks
    its index in distance order and filters before ``LIMIT``."""
    kbs = list(kb_ids)
    disabled_documents = select(KnowledgeDocumentRow.id).where(
        KnowledgeDocumentRow.tenant_id == tenant_id,
        KnowledgeDocumentRow.kb_id.in_(kbs),
        KnowledgeDocumentRow.enabled.is_(False),
    )
    return (
        KnowledgeChunkRow.tenant_id == tenant_id,
        KnowledgeChunkRow.kb_id.in_(kbs),
        _chunk_type_in(None),
        KnowledgeChunkRow.enabled.is_(True),
        KnowledgeChunkRow.document_id.not_in(disabled_documents),
    )


def _balanced(
    parts: Sequence[ColumnElement[Any]],
    combine: Callable[[ColumnElement[Any], ColumnElement[Any]], ColumnElement[Any]],
) -> ColumnElement[Any]:
    """Combine ``parts`` pairwise as a balanced tree. A left-deep chain as long
    as the query overflows the recursion of SQLAlchemy's compiler and
    cache-key walk (~160 terms); a balanced tree is log2(n) deep."""
    if len(parts) == 1:
        return parts[0]
    middle = len(parts) // 2
    return combine(_balanced(parts[:middle], combine), _balanced(parts[middle:], combine))


def _any_tsquery(parts: Sequence[ColumnElement[Any]]) -> ColumnElement[Any]:
    """``parts[0] || parts[1] || ...`` — the tsquery OR operator."""
    return _balanced(parts, lambda left, right: left.op("||")(right))


def _kb_unit_tsquery(query: KbKeywordQuery, unit: str, *, phrase: bool) -> ColumnElement[Any]:
    """A unit's NFKC form — ``plainto_tsquery`` for a term, ``phraseto_tsquery``
    for an identifier phrase — OR its raw form (``phraseto_tsquery``, tokens
    adjacent) where the raw query text differs: chunks are indexed raw."""
    parse = func.phraseto_tsquery if phrase else func.plainto_tsquery
    nfkc = parse(_TS_CONFIG, unit)
    raw = query.unit_raw_form(unit)
    return nfkc if raw is None else nfkc.op("||")(func.phraseto_tsquery(_TS_CONFIG, raw))


def _kb_phrase_tsqueries(query: KbKeywordQuery) -> list[ColumnElement[Any]]:
    return [_kb_unit_tsquery(query, phrase, phrase=True) for phrase in query.phrases]


def _kb_unit_tsqueries(query: KbKeywordQuery) -> list[ColumnElement[Any]]:
    """One tsquery per term (``plainto_tsquery``) and per identifier phrase
    (``phraseto_tsquery``). Every term and phrase is a bound value parsed by
    Postgres — no tsquery syntax is assembled from query text, so operators a
    user types stay plain text."""
    terms = [_kb_unit_tsquery(query, term, phrase=False) for term in query.terms]
    return [*terms, *_kb_phrase_tsqueries(query)]


def _recall_failed(exc: DBAPIError) -> bool:
    """Whether ``exc`` says a recall query could not finish (B-206 K-22): a
    SQLSTATE in :data:`_RECALL_FAILED_SQLSTATES` or its classes, a connection
    SQLAlchemy invalidated, or a driver-level disconnect that carries no SQLSTATE."""
    if exc.connection_invalidated:
        return True
    sqlstate = getattr(exc.orig, "sqlstate", None)
    if not isinstance(sqlstate, str):
        return isinstance(exc, (InterfaceError, OperationalError))
    return sqlstate in _RECALL_FAILED_SQLSTATES or sqlstate[:2] in _RECALL_FAILED_SQLSTATE_CLASSES


def _kb_keyword_tsquery(query: KbKeywordQuery) -> ColumnElement[Any]:
    """Any term or any identifier phrase of ``query``."""
    return _any_tsquery(_kb_unit_tsqueries(query))


def _bm25_idf(in_scope: ColumnElement[Any], with_unit: ColumnElement[Any]) -> ColumnElement[Any]:
    """Lucene's BM25 idf, ln(1 + (N - df + 0.5) / (df + 0.5)): above zero for
    every 0 ≤ df ≤ N, so the weights never sum to zero."""
    return func.ln(
        literal(1.0) + (in_scope - with_unit + literal(0.5)) / (with_unit + literal(0.5)),
        type_=Float,
    )


def _kb_chunk_tsvector(chunk: KnowledgeChunk) -> ColumnElement[Any]:
    """``content_tsv`` of a chunk: its document title as weight A, then the
    breadcrumb + body as unlabelled D — both as :func:`kb_index_text`
    normalises them and :func:`tokenize_for_search` segments them. Without a
    title only the D part (as before titles were stored)."""
    body = func.to_tsvector(
        _TS_CONFIG,
        tokenize_for_search(kb_index_text(chunk_retrieval_text(chunk.heading_path, chunk.content))),
    )
    if not chunk.doc_title:
        return body
    title = func.setweight(
        func.to_tsvector(_TS_CONFIG, tokenize_for_search(kb_index_text(chunk.doc_title))),
        literal_column("'A'"),
    )
    return title.op("||")(body)


def _kb_hits(
    query: KbKeywordQuery, in_scope: Sequence[ColumnElement[bool]], ts_query: ColumnElement[Any]
) -> CTE:
    """The chunks the query matches: per row ``hit_i`` for each unit (term or
    identifier phrase) — 1 when the body holds it, :data:`KB_DOC_TITLE_HIT_WEIGHT`
    when only the document title (weight A) does, else 0 — and ``base`` = the
    boosts that apply plus the ``ts_rank`` tie-break.

    Each row's tsvector is copied once (``tsv``, labels kept), then its body
    is filtered out of that copy (``body``, label D): on the stored value every
    ``@@`` unpacks it again, about 3x slower for a 40-term query. OFFSET 0
    stops the planner from inlining the copies back into each use. ``ts_rank``
    runs on the body, all D, which pins it below D's 0.1. ``ts_rank``, not
    ``ts_rank_cd``: on an OR query ``ts_rank_cd`` counts every occurrence of a
    common word."""
    copied = (
        select(
            KnowledgeChunkRow.id.label("id"),
            func.ts_filter(KnowledgeChunkRow.content_tsv, literal_column("'{a,b,c,d}'")).label(
                "tsv"
            ),
        )
        .where(*in_scope, KnowledgeChunkRow.content_tsv.op("@@")(ts_query))
        .offset(0)
        .subquery("copied")
    )
    matched = (
        select(
            copied.c.id,
            copied.c.tsv,
            func.ts_filter(copied.c.tsv, literal_column("'{d}'")).label("body"),
        )
        .offset(0)
        .subquery("matched")
    )
    tsv, body = matched.c.tsv, matched.c.body
    hits = [
        case(
            (
                tsv.op("@@")(unit),
                case(
                    (body.op("@@")(unit), literal(1.0)),
                    else_=literal(KB_DOC_TITLE_HIT_WEIGHT),
                ),
            ),
            else_=literal(0.0),
        ).label(f"hit_{index}")
        for index, unit in enumerate(_kb_unit_tsqueries(query))
    ]
    base: ColumnElement[Any] = func.ts_rank(body, _kb_keyword_tsquery(query))
    if query.phrases:
        identifier_hit = tsv.op("@@")(_any_tsquery(_kb_phrase_tsqueries(query)))
        base = base + case((identifier_hit, literal(KB_KEYWORD_PHRASE_BOOST)), else_=literal(0.0))
    if questions := query.question_phrases():
        question_hit = tsv.op("@@")(
            _any_tsquery([func.phraseto_tsquery(_TS_CONFIG, q) for q in questions])
        )
        base = base + case(
            (question_hit, literal(KB_KEYWORD_QUERY_PHRASE_BOOST)), else_=literal(0.0)
        )
    return select(matched.c.id, *hits, base.label("base")).cte("hits").prefix_with("MATERIALIZED")


def _parse_vector_version(raw: str | None) -> tuple[int, ...]:
    """pgvector version as integers (``"0.8.0.2"`` → ``(0, 8, 0, 2)``) so it
    compares numerically; ``()`` when missing, which never enables
    version-gated settings."""
    return tuple(int(part) for part in re.findall(r"\d+", raw or ""))


async def _read_vector_version(session: AsyncSession) -> tuple[int, ...]:
    raw = (
        await session.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
    ).scalar_one_or_none()
    return _parse_vector_version(raw)


async def _set_hnsw_scan(session: AsyncSession, limit: int, *, iterative: bool) -> None:
    """HNSW scan settings for the vector SELECT that follows in this session's
    transaction — ``set_config(..., true)`` is transaction-local, so nothing
    leaks into pooled connections. One statement, one round trip."""
    sql = "SELECT set_config('hnsw.ef_search', :ef_search, true)"
    params = {"ef_search": str(min(max(_HNSW_EF_SEARCH, limit), _HNSW_EF_SEARCH_MAX))}
    if iterative:
        sql += f", set_config('{_HNSW_ITERATIVE_SCAN_GUC}', :iterative_scan, true)"
        params["iterative_scan"] = _HNSW_ITERATIVE_SCAN
    await session.execute(text(sql), params)


def _chunk_row(chunk: KnowledgeChunk) -> KnowledgeChunkRow:
    # A parent row (B-206 K-1) is stored for reading only: no vector, no
    # keyword vector. Any other chunk without a vector is refused by the
    # table's CHECK (``knowledge_chunk_embedding_iff_parent``).
    parent = chunk.chunk_type is ChunkType.PARENT
    return KnowledgeChunkRow(
        id=chunk.id,
        tenant_id=chunk.tenant_id,
        kb_id=chunk.kb_id,
        document_id=chunk.document_id,
        chunk_index=chunk.chunk_index,
        content=chunk.content,
        embedding=None if parent or chunk.embedding is None else list(chunk.embedding),
        # Keyword-search vector over title + breadcrumb + body — segmented
        # app-side so it is correct for CJK (see text_search).
        content_tsv=None if parent else _kb_chunk_tsvector(chunk),
        chunk_type=chunk.chunk_type.value,
        heading_path=list(chunk.heading_path) or None,
        doc_title=chunk.doc_title,
        content_hash=chunk.content_hash,
        embedding_model=chunk.embedding_model,
        enabled=chunk.enabled,
        disabled_reason=None if chunk.enabled else chunk.disabled_reason,
        edited=chunk.edited,
        revision=chunk.revision,
        parent_chunk_id=chunk.parent_chunk_id,
        source_locator=chunk.source_locator,
    )


# -- passages (B-175 P3-PR2) --------------------------------------------------


def _passage_rows_where(tenant_id: UUID, document_id: UUID) -> tuple[ColumnElement[bool], ...]:
    """The document's passage rows: parent rows, and chunks with none."""
    return (
        KnowledgeChunkRow.tenant_id == tenant_id,
        KnowledgeChunkRow.document_id == document_id,
        or_(
            KnowledgeChunkRow.chunk_type == ChunkType.PARENT.value,
            and_(_chunk_type_in(None), KnowledgeChunkRow.parent_chunk_id.is_(None)),
        ),
    )


def _passages_stmt(tenant_id: UUID, document_id: UUID, *extra: ColumnElement[bool]) -> Select[Any]:
    """The document's passage rows in document order: a parent row where its
    first chunk is (one with no chunk last), a lone chunk where it is."""
    child = aliased(KnowledgeChunkRow)
    first = (
        select(
            child.parent_chunk_id.label("passage_id"),
            func.min(child.chunk_index).label("position"),
        )
        .where(
            child.tenant_id == tenant_id,
            child.document_id == document_id,
            child.parent_chunk_id.is_not(None),
            child.chunk_type.in_(_SEARCHABLE_TYPE_VALUES),
        )
        .group_by(child.parent_chunk_id)
        .subquery("first_chunk")
    )
    position = case(
        (KnowledgeChunkRow.chunk_type == ChunkType.PARENT.value, first.c.position),
        else_=KnowledgeChunkRow.chunk_index,
    )
    return (
        select(*_CHUNK_COLUMNS)
        .outerjoin(first, first.c.passage_id == KnowledgeChunkRow.id)
        .where(*_passage_rows_where(tenant_id, document_id), *extra)
        .order_by(
            position.asc().nulls_last(),
            KnowledgeChunkRow.chunk_type.asc(),
            KnowledgeChunkRow.chunk_index.asc(),
        )
    )


async def _read_passages(
    session: AsyncSession,
    tenant_id: UUID,
    document_id: UUID,
    stmt: Select[Any],
    *,
    enabled_only: bool = False,
) -> list[Passage]:
    """The passages ``stmt`` (a :func:`_passages_stmt`) selects, each with
    its chunks — one more query for the parent rows' chunks."""
    rows = [_to_chunk(row) for row in (await session.execute(stmt)).all()]
    parents = [row.id for row in rows if row.chunk_type is ChunkType.PARENT]
    under: dict[UUID | None, list[KnowledgeChunk]] = {}
    if parents:
        where = [
            KnowledgeChunkRow.tenant_id == tenant_id,
            KnowledgeChunkRow.document_id == document_id,
            KnowledgeChunkRow.parent_chunk_id.in_(parents),
            _chunk_type_in(None),
        ]
        if enabled_only:
            where.append(KnowledgeChunkRow.enabled.is_(True))
        children = select(*_CHUNK_COLUMNS).where(*where).order_by(KnowledgeChunkRow.chunk_index)
        for row in (await session.execute(children)).all():
            chunk = _to_chunk(row)
            under.setdefault(chunk.parent_chunk_id, []).append(chunk)
    return [
        Passage(row, tuple(under.get(row.id, ())) if row.chunk_type is ChunkType.PARENT else (row,))
        for row in rows
    ]


async def _read_passage(
    session: AsyncSession, tenant_id: UUID, document_id: UUID, passage_id: UUID
) -> Passage | None:
    stmt = _passages_stmt(tenant_id, document_id, KnowledgeChunkRow.id == passage_id)
    found = await _read_passages(session, tenant_id, document_id, stmt)
    return found[0] if found else None


async def _lock_settled_document(
    session: AsyncSession, tenant_id: UUID, document_id: UUID
) -> UUID | None:
    """Lock the document row a passage write changes (the order a fenced
    ``replace_chunks`` takes: the row, then its chunks) and return its base;
    ``None`` when there is no such document. :class:`PassageConflictError`
    while its chunks are about to be replaced (being ingested, or a new
    version waits)."""
    row = (
        await session.execute(
            select(
                KnowledgeDocumentRow.kb_id,
                KnowledgeDocumentRow.status,
                KnowledgeDocumentRow.has_pending,
            )
            .where(
                KnowledgeDocumentRow.tenant_id == tenant_id,
                KnowledgeDocumentRow.id == document_id,
            )
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        await session.rollback()
        return None
    if row.status != DocumentStatus.READY.value or row.has_pending:
        await session.rollback()
        raise PassageConflictError("document_busy")
    return UUID(str(row.kb_id))


async def _next_numbers(
    session: AsyncSession, tenant_id: UUID, document_id: UUID
) -> dict[str, int]:
    """The document's first free chunk number and first free parent number."""
    is_parent = KnowledgeChunkRow.chunk_type == ChunkType.PARENT.value
    last_chunk, last_parent = (
        await session.execute(
            select(
                func.max(case((_chunk_type_in(None), KnowledgeChunkRow.chunk_index))),
                func.max(case((is_parent, KnowledgeChunkRow.chunk_index))),
            ).where(
                KnowledgeChunkRow.tenant_id == tenant_id,
                KnowledgeChunkRow.document_id == document_id,
            )
        )
    ).one()
    return {
        "next_index": 0 if last_chunk is None else int(last_chunk) + 1,
        "next_parent_index": 0 if last_parent is None else int(last_parent) + 1,
    }


async def _write_passage(
    session: AsyncSession,
    layout: PassageLayout,
    *,
    content: str,
    heading_path: Sequence[str],
    children: Sequence[PassageChild],
    revision: int,
    edited: bool,
    enabled: bool,
    disabled_reason: DisabledReason | None = None,
) -> None:
    """Swap the passage's rows (:func:`passage_rows`) and recount the
    document's chunks, inside the caller's transaction."""
    rows = passage_rows(
        layout,
        content=content,
        heading_path=heading_path,
        children=children,
        revision=revision,
        edited=edited,
        enabled=enabled,
        disabled_reason=disabled_reason,
    )
    scope = (
        KnowledgeChunkRow.tenant_id == layout.tenant_id,
        KnowledgeChunkRow.document_id == layout.document_id,
    )
    if layout.old is not None:
        replaced = {layout.old.id, *(child.id for child in layout.old.children)}
        await session.execute(
            delete(KnowledgeChunkRow).where(*scope, KnowledgeChunkRow.id.in_(replaced))
        )
    session.add_all([_chunk_row(row) for row in rows])
    await session.flush()
    chunks = (
        select(func.count())
        .select_from(KnowledgeChunkRow)
        .where(*scope, _chunk_type_in(None))
        .scalar_subquery()
    )
    await session.execute(
        update(KnowledgeDocumentRow)
        .where(
            KnowledgeDocumentRow.tenant_id == layout.tenant_id,
            KnowledgeDocumentRow.id == layout.document_id,
        )
        .values(chunk_count=chunks, updated_at=datetime.now(UTC))
    )


def _revision_row(
    tenant_id: UUID, kb_id: UUID, document_id: UUID, record: PassageRevision
) -> KnowledgeChunkRevisionRow:
    return KnowledgeChunkRevisionRow(
        tenant_id=tenant_id,
        kb_id=kb_id,
        document_id=document_id,
        chunk_id=record.passage_id,
        revision=record.revision,
        content=record.content,
        heading_path=list(record.heading_path) or None,
        edited_by=record.edited_by,
    )


async def _current_revisions(
    session: AsyncSession,
    tenant_id: UUID,
    document_id: UUID,
    histories: Mapping[UUID, Sequence[PassageRevision]],
) -> dict[UUID, int]:
    """The current revision of each passage with a history that still exists."""
    if not histories:
        return {}
    rows = await session.execute(
        select(KnowledgeChunkRow.id, KnowledgeChunkRow.revision).where(
            *_passage_rows_where(tenant_id, document_id),
            KnowledgeChunkRow.id.in_(list(histories)),
        )
    )
    return {row.id: row.revision for row in rows.all()}


async def _stamp_orphans(session: AsyncSession, tenant_id: UUID, document_id: UUID) -> None:
    """Histories of passages the document no longer has get ``orphaned_at``
    (once); a passage that is there again clears it. Switch-off records keep
    their own (:func:`_write_switch_offs`)."""
    present = select(KnowledgeChunkRow.id).where(*_passage_rows_where(tenant_id, document_id))
    revisions = (
        KnowledgeChunkRevisionRow.tenant_id == tenant_id,
        KnowledgeChunkRevisionRow.document_id == document_id,
        KnowledgeChunkRevisionRow.switch_off_lost.is_(False),
    )
    await session.execute(
        update(KnowledgeChunkRevisionRow)
        .where(
            *revisions,
            KnowledgeChunkRevisionRow.orphaned_at.is_(None),
            KnowledgeChunkRevisionRow.chunk_id.not_in(present),
        )
        .values(orphaned_at=func.now())
    )
    await session.execute(
        update(KnowledgeChunkRevisionRow)
        .where(
            *revisions,
            KnowledgeChunkRevisionRow.orphaned_at.is_not(None),
            KnowledgeChunkRevisionRow.chunk_id.in_(present),
        )
        .values(orphaned_at=None)
    )


async def _read_history(
    session: AsyncSession,
    tenant_id: UUID,
    document_id: UUID,
    passage_id: UUID | None = None,
    *,
    switch_offs: bool = False,
) -> list[PassageRevision]:
    """The document's text revision rows (one passage's, given
    ``passage_id``), each passage's oldest first — or, ``switch_offs``, its
    switch-off records."""
    where = [
        KnowledgeChunkRevisionRow.tenant_id == tenant_id,
        KnowledgeChunkRevisionRow.document_id == document_id,
        KnowledgeChunkRevisionRow.switch_off_lost.is_(switch_offs),
    ]
    if passage_id is not None:
        where.append(KnowledgeChunkRevisionRow.chunk_id == passage_id)
    rows = (
        (
            await session.execute(
                select(KnowledgeChunkRevisionRow)
                .where(*where)
                .order_by(KnowledgeChunkRevisionRow.chunk_id, KnowledgeChunkRevisionRow.revision)
            )
        )
        .scalars()
        .all()
    )
    return [
        PassageRevision(
            passage_id=row.chunk_id,
            revision=row.revision,
            content=row.content,
            heading_path=tuple(row.heading_path or ()),
            edited_by=row.edited_by,
            created_at=row.created_at,
            orphaned_at=row.orphaned_at,
            switch_off_lost=row.switch_off_lost,
        )
        for row in rows
    ]


async def _write_switch_offs(
    session: AsyncSession, tenant_id: UUID, document_id: UUID, records: Collection[SwitchOffRecord]
) -> None:
    """``replace_chunks(switch_offs=…)``: a record this ingest made is inserted
    as revision :data:`SWITCH_OFF_REVISION` of its passage; an earlier one is
    only updated, if it still exists (one discarded or expired meanwhile stays
    gone). ``orphaned_at`` cleared when placed, else set once."""
    kb_id: UUID | None = None
    for record in records:
        orphaned = (
            None
            if record.placed
            else func.coalesce(KnowledgeChunkRevisionRow.orphaned_at, func.now())
        )
        if not record.new:
            await session.execute(
                update(KnowledgeChunkRevisionRow)
                .where(*_record_where(tenant_id, document_id, record.passage_id))
                .values(orphaned_at=orphaned)
            )
            continue
        if kb_id is None:
            kb_id = (
                await session.execute(
                    select(KnowledgeDocumentRow.kb_id).where(
                        KnowledgeDocumentRow.tenant_id == tenant_id,
                        KnowledgeDocumentRow.id == document_id,
                    )
                )
            ).scalar_one()
        insert = pg_insert(KnowledgeChunkRevisionRow).values(
            tenant_id=tenant_id,
            kb_id=kb_id,
            document_id=document_id,
            chunk_id=record.passage_id,
            revision=SWITCH_OFF_REVISION,
            content=record.content,
            heading_path=list(record.heading_path) or None,
            edited_by=None,
            switch_off_lost=True,
            orphaned_at=None if record.placed else func.now(),
        )
        await session.execute(
            insert.on_conflict_do_update(
                constraint="knowledge_chunk_revision_uniq", set_={"orphaned_at": orphaned}
            )
        )


def _record_where(
    tenant_id: UUID, document_id: UUID, passage_id: UUID
) -> tuple[ColumnElement[bool], ...]:
    """One passage's switch-off record."""
    return (
        KnowledgeChunkRevisionRow.tenant_id == tenant_id,
        KnowledgeChunkRevisionRow.document_id == document_id,
        KnowledgeChunkRevisionRow.chunk_id == passage_id,
        KnowledgeChunkRevisionRow.revision == SWITCH_OFF_REVISION,
        KnowledgeChunkRevisionRow.switch_off_lost.is_(True),
    )


async def _settle_switch_offs(
    session: AsyncSession, tenant_id: UUID, document_id: UUID, passage: Passage
) -> None:
    """A person decided about ``passage``, off by inheritance: its paragraphs
    leave the document's switch-off records (an emptied one is deleted)."""
    for record in await _read_history(session, tenant_id, document_id, switch_offs=True):
        settled = settle_switch_off(record.content, passage.row.content)
        if settled == record.content:
            continue
        mine = _record_where(tenant_id, document_id, record.passage_id)
        if settled:
            await session.execute(
                update(KnowledgeChunkRevisionRow).where(*mine).values(content=settled)
            )
        else:
            await session.execute(delete(KnowledgeChunkRevisionRow).where(*mine))


class SqlKnowledgeStore(KnowledgeStore):
    """Postgres-backed knowledge repository (pgvector)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory
        #: Installed pgvector version, read on the first vector search.
        self._vector_version: tuple[int, ...] | None = None
        #: Set when the server refused ``hnsw.iterative_scan``; never sent again.
        self._iterative_scan_rejected = False

    # -- knowledge bases ----------------------------------------------------

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
        row = KnowledgeBaseRow(
            tenant_id=tenant_id,
            name=name,
            description=description,
            created_by=created_by,
            chunk_max_tokens=chunk_max_tokens,
            chunk_overlap_tokens=chunk_overlap_tokens,
            retrieval_top_k=retrieval_top_k,
            retrieval_score_threshold=retrieval_score_threshold,
            retrieval_method=retrieval_method.value,
            rerank_enabled=rerank_enabled,
            rerank_score_threshold=rerank_score_threshold,
            rerank_threshold_model=rerank_threshold_model,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
        )
        try:
            async with self._sf() as session:
                session.add(row)
                await session.commit()
                await session.refresh(row)
        except IntegrityError as exc:
            raise DuplicateKnowledgeBaseError(tenant_id=tenant_id, name=name) from exc
        return _to_base(row)

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
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(KnowledgeBaseRow).where(
                        KnowledgeBaseRow.tenant_id == tenant_id,
                        KnowledgeBaseRow.id == kb_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if not isinstance(description, _Unset):
                row.description = description
            if chunk_max_tokens is not None:
                row.chunk_max_tokens = chunk_max_tokens
            if chunk_overlap_tokens is not None:
                row.chunk_overlap_tokens = chunk_overlap_tokens
            if retrieval_top_k is not None:
                row.retrieval_top_k = retrieval_top_k
            if not isinstance(retrieval_score_threshold, _Unset):
                row.retrieval_score_threshold = retrieval_score_threshold
            if retrieval_method is not None:
                row.retrieval_method = retrieval_method.value
            if rerank_enabled is not None:
                row.rerank_enabled = rerank_enabled
            if not isinstance(rerank_score_threshold, _Unset):
                row.rerank_score_threshold = rerank_score_threshold
            if not isinstance(rerank_threshold_model, _Unset):
                row.rerank_threshold_model = rerank_threshold_model
            row.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
            return _to_base(row)

    async def get_base(self, *, tenant_id: UUID, name: str) -> KnowledgeBase | None:
        stmt = select(KnowledgeBaseRow).where(
            KnowledgeBaseRow.tenant_id == tenant_id,
            KnowledgeBaseRow.name == name,
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _to_base(row) if row is not None else None

    async def list_bases(self, *, tenant_id: UUID) -> list[KnowledgeBase]:
        stmt = (
            select(KnowledgeBaseRow)
            .where(KnowledgeBaseRow.tenant_id == tenant_id)
            # ``id`` tiebreak — same order as ``list_bases_page``.
            .order_by(KnowledgeBaseRow.created_at.desc(), KnowledgeBaseRow.id.desc())
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_base(row) for row in rows]

    async def list_bases_all_tenants(
        self, *, limit: int = ALL_TENANTS_BASES_LIMIT
    ) -> list[KnowledgeBase]:
        # W4 — no tenant filter; caller must wrap in bypass_rls_session().
        stmt = (
            select(KnowledgeBaseRow)
            .order_by(KnowledgeBaseRow.created_at.desc(), KnowledgeBaseRow.id)
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_base(row) for row in rows]

    async def list_bases_page(
        self, *, tenant_id: UUID, limit: int, offset: int
    ) -> tuple[list[KnowledgeBase], int]:
        page_stmt = (
            select(KnowledgeBaseRow)
            .where(KnowledgeBaseRow.tenant_id == tenant_id)
            .order_by(KnowledgeBaseRow.created_at.desc(), KnowledgeBaseRow.id.desc())
            .offset(offset)
            .limit(limit)
        )
        count_stmt = (
            select(func.count())
            .select_from(KnowledgeBaseRow)
            .where(KnowledgeBaseRow.tenant_id == tenant_id)
        )
        async with self._sf() as session:
            rows = (await session.execute(page_stmt)).scalars().all()
            total = (await session.execute(count_stmt)).scalar_one()
        return [_to_base(row) for row in rows], int(total)

    async def list_bases_all_tenants_page(
        self, *, limit: int, offset: int
    ) -> tuple[list[KnowledgeBase], int]:
        # W4 — no tenant filter; caller must wrap in bypass_rls_session().
        page_stmt = (
            select(KnowledgeBaseRow)
            .order_by(KnowledgeBaseRow.created_at.desc(), KnowledgeBaseRow.id)
            .offset(offset)
            .limit(limit)
        )
        count_stmt = select(func.count()).select_from(KnowledgeBaseRow)
        async with self._sf() as session:
            rows = (await session.execute(page_stmt)).scalars().all()
            total = (await session.execute(count_stmt)).scalar_one()
        return [_to_base(row) for row in rows], int(total)

    async def base_stats(self, *, tenant_id: UUID, kb_id: UUID) -> tuple[int, int]:
        stmt = select(
            func.count(KnowledgeDocumentRow.id),
            func.coalesce(func.sum(KnowledgeDocumentRow.chunk_count), 0),
        ).where(
            KnowledgeDocumentRow.tenant_id == tenant_id,
            KnowledgeDocumentRow.kb_id == kb_id,
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).one()
        return int(row[0]), int(row[1])

    async def base_stats_many(self, *, tenant_id: UUID) -> dict[UUID, tuple[int, int]]:
        stmt = (
            select(
                KnowledgeDocumentRow.kb_id,
                func.count(KnowledgeDocumentRow.id),
                func.coalesce(func.sum(KnowledgeDocumentRow.chunk_count), 0),
            )
            .where(KnowledgeDocumentRow.tenant_id == tenant_id)
            .group_by(KnowledgeDocumentRow.kb_id)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        return {row[0]: (int(row[1]), int(row[2])) for row in rows}

    async def count_documents_capped(
        self, *, tenant_id: UUID, kb_ids: Sequence[UUID], cap: int
    ) -> dict[UUID, int]:
        ids = list(dict.fromkeys(kb_ids))
        if not ids:
            return {}

        def _capped(kb_id: UUID) -> Any:
            # LIMIT inside: the (tenant_id, kb_id, filename) unique index lets
            # each count stop after ``cap`` rows instead of reading the base.
            rows = (
                select(KnowledgeDocumentRow.id)
                .where(
                    KnowledgeDocumentRow.tenant_id == tenant_id,
                    KnowledgeDocumentRow.kb_id == kb_id,
                )
                .limit(cap)
                .subquery()
            )
            return select(func.count()).select_from(rows).scalar_subquery()

        stmt = select(*(_capped(kb_id) for kb_id in ids))
        async with self._sf() as session:
            row = (await session.execute(stmt)).one()
        return {kb_id: int(count) for kb_id, count in zip(ids, row, strict=True)}

    async def base_stats_many_all_tenants(
        self, *, kb_ids: Sequence[UUID]
    ) -> dict[UUID, tuple[int, int]]:
        # W4 — no tenant filter; caller must wrap in bypass_rls_session().
        # Bounded to the kb_ids of the (capped) aggregate base page.
        if not kb_ids:
            return {}
        stmt = (
            select(
                KnowledgeDocumentRow.kb_id,
                func.count(KnowledgeDocumentRow.id),
                func.coalesce(func.sum(KnowledgeDocumentRow.chunk_count), 0),
            )
            .where(KnowledgeDocumentRow.kb_id.in_(list(kb_ids)))
            .group_by(KnowledgeDocumentRow.kb_id)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        return {row[0]: (int(row[1]), int(row[2])) for row in rows}

    async def stamp_embedding_model(
        self, *, tenant_id: UUID, kb_id: UUID, embedding_provider: str, embedding_model: str
    ) -> None:
        async with self._sf() as session:
            await session.execute(
                update(KnowledgeBaseRow)
                .where(KnowledgeBaseRow.tenant_id == tenant_id, KnowledgeBaseRow.id == kb_id)
                .values(
                    embedding_provider=embedding_provider,
                    embedding_model=embedding_model,
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()

    async def request_reindex(self, *, tenant_id: UUID, kb_id: UUID) -> bool:
        async with self._sf() as session:
            exists = (
                await session.execute(
                    select(KnowledgeBaseRow.id).where(
                        KnowledgeBaseRow.tenant_id == tenant_id,
                        KnowledgeBaseRow.id == kb_id,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                return False
            await session.execute(
                update(KnowledgeBaseRow)
                .where(KnowledgeBaseRow.tenant_id == tenant_id, KnowledgeBaseRow.id == kb_id)
                .values(reindex_requested_at=datetime.now(UTC), updated_at=datetime.now(UTC))
            )
            await session.commit()
        return True

    async def clear_reindex(self, *, tenant_id: UUID, kb_id: UUID) -> None:
        async with self._sf() as session:
            await session.execute(
                update(KnowledgeBaseRow)
                .where(KnowledgeBaseRow.tenant_id == tenant_id, KnowledgeBaseRow.id == kb_id)
                .values(reindex_requested_at=None, updated_at=datetime.now(UTC))
            )
            await session.commit()

    async def delete_base(self, *, tenant_id: UUID, kb_id: UUID) -> bool:
        async with self._sf() as session:
            exists = (
                await session.execute(
                    select(KnowledgeBaseRow.id).where(
                        KnowledgeBaseRow.tenant_id == tenant_id,
                        KnowledgeBaseRow.id == kb_id,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                return False
            await session.execute(
                delete(KnowledgeChunkRow).where(
                    KnowledgeChunkRow.tenant_id == tenant_id,
                    KnowledgeChunkRow.kb_id == kb_id,
                )
            )
            # B-209 — the base's documents' recognized text (the FK cascades too).
            await session.execute(
                delete(KnowledgeParseCacheRow).where(
                    KnowledgeParseCacheRow.tenant_id == tenant_id,
                    KnowledgeParseCacheRow.document_id.in_(
                        select(KnowledgeDocumentRow.id).where(
                            KnowledgeDocumentRow.tenant_id == tenant_id,
                            KnowledgeDocumentRow.kb_id == kb_id,
                        )
                    ),
                )
            )
            # B-175 P3-PR2 — the passages' history (the FK cascades too, 0176).
            await session.execute(
                delete(KnowledgeChunkRevisionRow).where(
                    KnowledgeChunkRevisionRow.tenant_id == tenant_id,
                    KnowledgeChunkRevisionRow.kb_id == kb_id,
                )
            )
            await session.execute(
                delete(KnowledgeDocumentRow).where(
                    KnowledgeDocumentRow.tenant_id == tenant_id,
                    KnowledgeDocumentRow.kb_id == kb_id,
                )
            )
            await session.execute(
                delete(KnowledgeBaseRow).where(
                    KnowledgeBaseRow.tenant_id == tenant_id,
                    KnowledgeBaseRow.id == kb_id,
                )
            )
            await session.commit()
        return True

    # -- documents ----------------------------------------------------------

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
        async with self._sf() as session:
            existing = (
                await session.execute(
                    select(KnowledgeDocumentRow)
                    .where(
                        KnowledgeDocumentRow.tenant_id == tenant_id,
                        KnowledgeDocumentRow.kb_id == kb_id,
                        KnowledgeDocumentRow.filename == filename,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None:
                if (
                    not force
                    and content_sha256 is not None
                    and existing.status == DocumentStatus.READY.value
                    and existing.content_sha256 == content_sha256
                ):
                    await session.commit()
                    return _to_document(existing)  # unchanged re-upload: nothing to do
                existing.status = DocumentStatus.PENDING.value
                existing.error = None
                existing.error_code = None
                existing.stage = IngestStage.QUEUED.value
                existing.attempts = 0
                existing.ingest_generation = existing.ingest_generation + 1
                if existing.doc_seq is None:  # a row from before doc_seq existed
                    existing.doc_seq = await self._next_doc_seq(session, tenant_id)
                existing.claimed_at = None
                existing.lease_until = None
                if content is not None:
                    if not existing.has_content and existing.chunk_count == 0:
                        existing.content = content  # nothing serving yet
                        existing.content_sha256 = content_sha256
                        existing.size_bytes = len(content)
                    else:
                        # New bytes wait in the pending slot; the serving
                        # version (content + chunks) stays until the new
                        # ingest's fenced READY promotes it.
                        existing.pending_content = content
                        existing.pending_content_sha256 = content_sha256
                existing.updated_at = datetime.now(UTC)
                await session.commit()
                await session.refresh(existing)
                return _to_document(existing)
            facts = DocumentFileFacts.of(filename, content)
            row = KnowledgeDocumentRow(
                tenant_id=tenant_id,
                kb_id=kb_id,
                filename=filename,
                status=DocumentStatus.PENDING.value,
                content=content,
                content_sha256=content_sha256,
                doc_seq=await self._next_doc_seq(session, tenant_id),
                title=facts.title,
                mime=facts.mime,
                size_bytes=facts.size_bytes,
                source_type=DocumentSourceType.FILE.value,
                stage=IngestStage.QUEUED.value,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _to_document(row)

    @staticmethod
    async def _next_doc_seq(session: AsyncSession, tenant_id: UUID) -> int:
        """Bump the tenant's counter inside the caller's transaction: the row
        lock serialises concurrent inserts, and a rolled-back insert returns
        its number."""
        bumped = await session.execute(
            pg_insert(KnowledgeDocSeqRow)
            .values(tenant_id=tenant_id, last_seq=1)
            .on_conflict_do_update(
                index_elements=[KnowledgeDocSeqRow.tenant_id],
                set_={"last_seq": KnowledgeDocSeqRow.last_seq + 1},
            )
            .returning(KnowledgeDocSeqRow.last_seq)
        )
        return int(bumped.scalar_one())

    async def get_document(self, *, tenant_id: UUID, document_id: UUID) -> KnowledgeDocument | None:
        stmt = select(KnowledgeDocumentRow).where(
            KnowledgeDocumentRow.tenant_id == tenant_id,
            KnowledgeDocumentRow.id == document_id,
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _to_document(row) if row is not None else None

    async def get_documents(
        self, *, tenant_id: UUID, document_ids: Sequence[UUID]
    ) -> dict[UUID, KnowledgeDocument]:
        ids = list(dict.fromkeys(document_ids))
        if not ids:
            return {}
        stmt = select(KnowledgeDocumentRow).where(
            KnowledgeDocumentRow.tenant_id == tenant_id,
            KnowledgeDocumentRow.id.in_(ids),
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return {row.id: _to_document(row) for row in rows}

    async def get_document_content(self, *, tenant_id: UUID, document_id: UUID) -> bytes | None:
        stmt = select(KnowledgeDocumentRow.content).where(
            KnowledgeDocumentRow.tenant_id == tenant_id,
            KnowledgeDocumentRow.id == document_id,
        )
        async with self._sf() as session:
            return (await session.execute(stmt)).scalar_one_or_none()

    async def claim_document(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        now: datetime,
        lease_seconds: int,
        max_attempts: int,
    ) -> ClaimedIngestion | None:
        lease_until = now + timedelta(seconds=lease_seconds)
        async with self._sf() as session:
            row = (
                await session.execute(
                    update(KnowledgeDocumentRow)
                    .where(
                        KnowledgeDocumentRow.tenant_id == tenant_id,
                        KnowledgeDocumentRow.id == document_id,
                        _claimable(now, max_attempts),
                    )
                    .values(
                        status=DocumentStatus.PROCESSING.value,
                        claimed_at=now,
                        lease_until=lease_until,
                        attempts=KnowledgeDocumentRow.attempts + 1,
                    )
                    .returning(*_CLAIM_COLUMNS)
                )
            ).first()
            if row is None:
                await session.commit()
                return None
            claim = await self._build_claim(session, tenant_id, row)
            await session.commit()
            return claim

    async def claim_documents_for_ingest(
        self, *, now: datetime, lease_seconds: int, limit: int, max_attempts: int
    ) -> list[ClaimedIngestion]:
        lease_until = now + timedelta(seconds=lease_seconds)
        async with self._sf() as session:
            candidate_ids = (
                (
                    await session.execute(
                        select(KnowledgeDocumentRow.id)
                        .where(_claimable(now, max_attempts))
                        .order_by(KnowledgeDocumentRow.created_at.asc())
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            if not candidate_ids:
                await session.commit()
                return []
            rows = (
                await session.execute(
                    update(KnowledgeDocumentRow)
                    .where(KnowledgeDocumentRow.id.in_(candidate_ids))
                    .values(
                        status=DocumentStatus.PROCESSING.value,
                        claimed_at=now,
                        lease_until=lease_until,
                        attempts=KnowledgeDocumentRow.attempts + 1,
                    )
                    .returning(*_CLAIM_COLUMNS)
                )
            ).all()
            claims = [await self._build_claim(session, row.tenant_id, row) for row in rows]
            await session.commit()
            return claims

    async def _build_claim(
        self, session: AsyncSession, tenant_id: UUID, row: Row[Any]
    ) -> ClaimedIngestion:
        """``row``: a :data:`_CLAIM_COLUMNS` row."""
        params = (
            await session.execute(
                select(
                    KnowledgeBaseRow.chunk_max_tokens,
                    KnowledgeBaseRow.chunk_overlap_tokens,
                ).where(KnowledgeBaseRow.id == row.kb_id)
            )
        ).first()
        chunk_max, chunk_overlap = (
            (params.chunk_max_tokens, params.chunk_overlap_tokens)
            if params is not None
            else (DEFAULT_CHUNK_MAX_TOKENS, DEFAULT_CHUNK_OVERLAP_TOKENS)
        )
        return ClaimedIngestion(
            tenant_id=tenant_id,
            document_id=row.id,
            kb_id=row.kb_id,
            filename=row.filename,
            content=row.content,
            content_sha256=row.content_sha256,
            chunk_max_tokens=chunk_max,
            chunk_overlap_tokens=chunk_overlap,
            attempts=row.attempts,
            generation=row.ingest_generation,
        )

    async def mark_document_failed_terminal(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        error: str,
        error_code: IngestErrorCode | None = None,
        generation: int | None = None,
    ) -> bool:
        async with self._sf() as session:
            failed_ids = (
                (
                    await session.execute(
                        update(KnowledgeDocumentRow)
                        .where(
                            KnowledgeDocumentRow.tenant_id == tenant_id,
                            KnowledgeDocumentRow.id == document_id,
                            *_fence(generation),
                        )
                        .values(
                            status=DocumentStatus.FAILED.value,
                            error=error,
                            error_code=error_code.value if error_code is not None else None,
                            claimed_at=None,
                            lease_until=None,
                            updated_at=datetime.now(UTC),
                        )
                        .returning(KnowledgeDocumentRow.id)
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()
        return bool(failed_ids)

    async def set_document_stage(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        stage: IngestStage,
        generation: int | None = None,
    ) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                update(KnowledgeDocumentRow)
                .where(
                    KnowledgeDocumentRow.tenant_id == tenant_id,
                    KnowledgeDocumentRow.id == document_id,
                    *_fence(generation),
                )
                .values(stage=stage.value, updated_at=datetime.now(UTC))
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def discard_pending(
        self, *, tenant_id: UUID, document_id: UUID
    ) -> KnowledgeDocument | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(KnowledgeDocumentRow)
                    .where(
                        KnowledgeDocumentRow.tenant_id == tenant_id,
                        KnowledgeDocumentRow.id == document_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if not row.has_pending or _writing_chunks(row):
                await session.commit()
                return _to_document(row)
            serving = row.chunk_count > 0
            row.pending_content = None
            row.pending_content_sha256 = None
            row.status = (DocumentStatus.READY if serving else DocumentStatus.FAILED).value
            row.error = None if serving else _DISCARDED_ERROR
            row.error_code = None
            if serving:
                row.stage = IngestStage.READY.value
            row.ingest_generation = row.ingest_generation + 1
            row.attempts = 0
            row.claimed_at = None
            row.lease_until = None
            row.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
            return _to_document(row)

    async def cancel_documents(
        self, *, tenant_id: UUID, kb_id: UUID, document_ids: Sequence[UUID]
    ) -> dict[UUID, CancelOutcome]:
        ids = list(dict.fromkeys(document_ids))
        if not ids:
            return {}
        in_base = (
            KnowledgeDocumentRow.tenant_id == tenant_id,
            KnowledgeDocumentRow.kb_id == kb_id,
            KnowledgeDocumentRow.id.in_(ids),
        )
        async with self._sf() as session:
            cancelled = set(
                (
                    await session.execute(
                        update(KnowledgeDocumentRow)
                        .where(*in_base, _cancellable())
                        .values(
                            status=DocumentStatus.CANCELLED.value,
                            error=_CANCELLED_ERROR,
                            ingest_generation=KnowledgeDocumentRow.ingest_generation + 1,
                            claimed_at=None,
                            lease_until=None,
                            updated_at=datetime.now(UTC),
                        )
                        .returning(KnowledgeDocumentRow.id)
                    )
                )
                .scalars()
                .all()
            )
            found = set(
                (await session.execute(select(KnowledgeDocumentRow.id).where(*in_base)))
                .scalars()
                .all()
            )
            await session.commit()
        outcomes: dict[UUID, CancelOutcome] = {}
        for document_id in ids:
            if document_id in cancelled:
                outcomes[document_id] = "cancelled"
            elif document_id in found:
                outcomes[document_id] = "not_cancellable"
            else:
                outcomes[document_id] = "not_found"
        return outcomes

    async def sweep_terminal(self, *, now: datetime, max_attempts: int) -> int:
        async with self._sf() as session:
            failed_ids = (
                (
                    await session.execute(
                        update(KnowledgeDocumentRow)
                        .where(
                            KnowledgeDocumentRow.status == DocumentStatus.PROCESSING.value,
                            KnowledgeDocumentRow.attempts >= max_attempts,
                            or_(
                                KnowledgeDocumentRow.lease_until.is_(None),
                                KnowledgeDocumentRow.lease_until < now,
                            ),
                        )
                        .values(
                            status=DocumentStatus.FAILED.value,
                            error=_EXHAUSTED_ERROR,
                            error_code=IngestErrorCode.ATTEMPTS_EXHAUSTED.value,
                            claimed_at=None,
                            lease_until=None,
                            updated_at=now,
                        )
                        .returning(KnowledgeDocumentRow.id)
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()
        return len(failed_ids)

    async def renew_lease(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        generation: int,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                update(KnowledgeDocumentRow)
                .where(
                    KnowledgeDocumentRow.tenant_id == tenant_id,
                    KnowledgeDocumentRow.id == document_id,
                    *_fence(generation),
                )
                .values(lease_until=now + timedelta(seconds=lease_seconds))
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def list_documents(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        enabled: bool | None = None,
        statuses: Collection[DocumentStatus] | None = None,
    ) -> list[KnowledgeDocument]:
        stmt = self._documents_stmt(_documents_where(tenant_id, kb_id, enabled, statuses))
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_document(row) for row in rows]

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
        where = _documents_where(tenant_id, kb_id, enabled, statuses)
        page_stmt = self._documents_stmt(where).offset(offset).limit(limit)
        count_stmt = select(func.count()).select_from(KnowledgeDocumentRow).where(*where)
        async with self._sf() as session:
            rows = (await session.execute(page_stmt)).scalars().all()
            total = (await session.execute(count_stmt)).scalar_one()
        return [_to_document(row) for row in rows], int(total)

    @staticmethod
    def _documents_stmt(
        where: Sequence[ColumnElement[bool]],
    ) -> Select[tuple[KnowledgeDocumentRow]]:
        # ``id`` tiebreak keeps pages stable when several documents share a
        # ``created_at`` (batch upload).
        return (
            select(KnowledgeDocumentRow)
            .where(*where)
            .order_by(KnowledgeDocumentRow.created_at.desc(), KnowledgeDocumentRow.id.desc())
        )

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
        values: dict[str, object] = {
            "status": status.value,
            "error": error,
            "updated_at": datetime.now(UTC),
        }
        if chunk_count is not None:
            values["chunk_count"] = chunk_count
        # Reaching a terminal state releases the durability lease so the
        # document is never re-claimed.
        if status.value in _TERMINAL_STATUSES:
            values["claimed_at"] = None
            values["lease_until"] = None
        if status is DocumentStatus.READY:
            values["stage"] = IngestStage.READY.value
            values["error_code"] = None
        if status is DocumentStatus.READY and generation is not None:
            # The new version is now serving: promote pending → content in
            # the same (fenced) statement.
            values["content"] = func.coalesce(
                KnowledgeDocumentRow.pending_content, KnowledgeDocumentRow.content
            )
            values["content_sha256"] = func.coalesce(
                KnowledgeDocumentRow.pending_content_sha256, KnowledgeDocumentRow.content_sha256
            )
            values["size_bytes"] = func.octet_length(
                func.coalesce(KnowledgeDocumentRow.pending_content, KnowledgeDocumentRow.content)
            )
            values["pending_content"] = None
            values["pending_content_sha256"] = None
        async with self._sf() as session:
            result = await session.execute(
                update(KnowledgeDocumentRow)
                .where(
                    KnowledgeDocumentRow.tenant_id == tenant_id,
                    KnowledgeDocumentRow.id == document_id,
                    *_fence(generation),
                )
                .values(**values)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def delete_document(self, *, tenant_id: UUID, document_id: UUID) -> bool:
        async with self._sf() as session:
            # Lock the document row first — the same order as a fenced
            # ``replace_chunks`` (row, then its chunks), so the two never
            # deadlock against each other.
            exists = (
                await session.execute(
                    select(KnowledgeDocumentRow.id)
                    .where(
                        KnowledgeDocumentRow.tenant_id == tenant_id,
                        KnowledgeDocumentRow.id == document_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if exists is None:
                return False
            await session.execute(
                delete(KnowledgeChunkRow).where(
                    KnowledgeChunkRow.tenant_id == tenant_id,
                    KnowledgeChunkRow.document_id == document_id,
                )
            )
            # B-209 — the document's recognized text (the FK cascades too).
            await session.execute(
                delete(KnowledgeParseCacheRow).where(
                    KnowledgeParseCacheRow.tenant_id == tenant_id,
                    KnowledgeParseCacheRow.document_id == document_id,
                )
            )
            # B-175 P3-PR2 — the passages' history (the FK cascades too, 0176).
            await session.execute(
                delete(KnowledgeChunkRevisionRow).where(
                    KnowledgeChunkRevisionRow.tenant_id == tenant_id,
                    KnowledgeChunkRevisionRow.document_id == document_id,
                )
            )
            await session.execute(
                delete(KnowledgeDocumentRow).where(
                    KnowledgeDocumentRow.tenant_id == tenant_id,
                    KnowledgeDocumentRow.id == document_id,
                )
            )
            await session.commit()
        return True

    async def set_document_pages(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        pages: Sequence[DocumentPage],
        generation: int | None = None,
    ) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                update(KnowledgeDocumentRow)
                .where(
                    KnowledgeDocumentRow.tenant_id == tenant_id,
                    KnowledgeDocumentRow.id == document_id,
                    *_fence(generation),
                )
                .values(
                    page_records=[page.model_dump(mode="json") for page in pages],
                    page_stats=PageStats.of(pages).model_dump(mode="json"),
                    page_count=len(pages),
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def list_document_pages(
        self, *, tenant_id: UUID, document_id: UUID
    ) -> list[DocumentPage] | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(KnowledgeDocumentRow.page_records).where(
                        KnowledgeDocumentRow.tenant_id == tenant_id,
                        KnowledgeDocumentRow.id == document_id,
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        return [DocumentPage.model_validate(record) for record in row[0] or ()]

    async def set_document_figures(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        stats: FigureStats | _Unset | None,
        pixel_hashes: Sequence[str],
        generation: int | None = None,
    ) -> bool:
        values: dict[str, Any] = {
            "figure_hashes": list(dict.fromkeys(pixel_hashes)),
            "updated_at": datetime.now(UTC),
        }
        if not isinstance(stats, _Unset):
            values["figure_stats"] = stats.model_dump(mode="json") if stats is not None else None
        async with self._sf() as session:
            result = await session.execute(
                update(KnowledgeDocumentRow)
                .where(
                    KnowledgeDocumentRow.tenant_id == tenant_id,
                    KnowledgeDocumentRow.id == document_id,
                    *_fence(generation),
                )
                .values(**values)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def count_figure_documents(
        self, *, tenant_id: UUID, pixel_hashes: Sequence[str], exclude_document_id: UUID
    ) -> dict[str, int]:
        if not pixel_hashes:
            return {}
        wanted = list(dict.fromkeys(pixel_hashes))
        found = (
            func.unnest(KnowledgeDocumentRow.figure_hashes)
            .table_valued("digest")
            .render_derived(name="h")
        )
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(found.c.digest, func.count())
                    .select_from(KnowledgeDocumentRow)
                    .join(found, true())
                    .where(
                        KnowledgeDocumentRow.tenant_id == tenant_id,
                        KnowledgeDocumentRow.id != exclude_document_id,
                        KnowledgeDocumentRow.figure_hashes.overlap(wanted),
                        found.c.digest.in_(wanted),
                    )
                    .group_by(found.c.digest)
                )
            ).all()
        return {str(digest): int(count) for digest, count in rows}

    async def sees_every_tenant(self) -> bool:
        # Row security that applies to this connection's role filters the rows
        # by tenant (bypassing it application-side only skips the tenant
        # setting — the policy then shows nothing). Postgres says whether it
        # applies: superuser, BYPASSRLS, an owner without FORCE, or RLS off.
        async with self._sf() as session:
            active = (
                await session.execute(text("SELECT row_security_active('knowledge_document')"))
            ).scalar_one()
        return active is False

    async def existing_document_ids(self, *, document_ids: Sequence[UUID]) -> set[UUID]:
        if not document_ids:
            return set()
        async with self._sf() as session:
            # Same transaction as the lookup: a "not found" here is only
            # trusted when row security did not filter it.
            active = (
                await session.execute(text("SELECT row_security_active('knowledge_document')"))
            ).scalar_one()
            if active is not False:
                msg = "row security applies to this connection: the lookup would miss rows"
                raise RowSecurityActiveError(msg)
            rows = (
                await session.execute(
                    select(KnowledgeDocumentRow.id).where(
                        KnowledgeDocumentRow.id.in_(list(dict.fromkeys(document_ids)))
                    )
                )
            ).all()
        return {row[0] for row in rows}

    # -- recognition cache (B-209 §5.5) -------------------------------------

    async def get_parse_cache(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        kind: ParseCacheKind,
        keys: Sequence[str],
    ) -> dict[str, ParseCacheEntry]:
        if not keys:
            return {}
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(
                        KnowledgeParseCacheRow.cache_key,
                        KnowledgeParseCacheRow.model,
                        KnowledgeParseCacheRow.result,
                    ).where(
                        KnowledgeParseCacheRow.tenant_id == tenant_id,
                        KnowledgeParseCacheRow.document_id == document_id,
                        KnowledgeParseCacheRow.kind == kind,
                        KnowledgeParseCacheRow.cache_key.in_(list(keys)),
                    )
                )
            ).all()
        return {
            key: ParseCacheEntry(model=model, result=dict(result)) for key, model, result in rows
        }

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
        async with self._sf() as session:
            await session.execute(
                pg_insert(KnowledgeParseCacheRow)
                .values(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    kind=kind,
                    cache_key=key,
                    model=model,
                    result=result,
                )
                .on_conflict_do_update(
                    index_elements=["document_id", "kind", "cache_key"],
                    set_={"model": model, "result": result, "created_at": func.now()},
                )
            )
            await session.commit()

    # -- maintenance (B-175 P3-PR2) -------------------------------------------

    async def set_documents_enabled(
        self, *, tenant_id: UUID, kb_id: UUID, document_ids: Sequence[UUID], enabled: bool
    ) -> list[UUID]:
        if not document_ids:
            return []
        async with self._sf() as session:
            done = set(
                (
                    await session.execute(
                        update(KnowledgeDocumentRow)
                        .where(
                            *_documents_where(tenant_id, kb_id),
                            KnowledgeDocumentRow.id.in_(list(dict.fromkeys(document_ids))),
                        )
                        .values(enabled=enabled, updated_at=datetime.now(UTC))
                        .returning(KnowledgeDocumentRow.id)
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()
        return [document_id for document_id in dict.fromkeys(document_ids) if document_id in done]

    async def count_stale_documents(self, *, tenant_id: UUID, kb_id: UUID, params_hash: str) -> int:
        stmt = (
            select(func.count())
            .select_from(KnowledgeDocumentRow)
            .where(*_stale_where(tenant_id, kb_id, params_hash))
        )
        async with self._sf() as session:
            return int((await session.execute(stmt)).scalar_one())

    async def list_stale_documents(
        self, *, tenant_id: UUID, kb_id: UUID, params_hash: str
    ) -> list[KnowledgeDocument]:
        stmt = self._documents_stmt(_stale_where(tenant_id, kb_id, params_hash))
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_document(row) for row in rows]

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
        extra: list[ColumnElement[bool]] = []
        if disabled_reason is not None:
            extra.append(KnowledgeChunkRow.disabled_reason == disabled_reason)
        if enabled_only:
            extra.append(KnowledgeChunkRow.enabled.is_(True))
            extra.append(
                select(KnowledgeDocumentRow.id)
                .where(
                    KnowledgeDocumentRow.tenant_id == tenant_id,
                    KnowledgeDocumentRow.id == document_id,
                    KnowledgeDocumentRow.enabled.is_(True),
                )
                .exists()
            )
        stmt = _passages_stmt(tenant_id, document_id, *extra).offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._sf() as session:
            return await _read_passages(
                session, tenant_id, document_id, stmt, enabled_only=enabled_only
            )

    async def count_passages(self, *, tenant_id: UUID, document_id: UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(KnowledgeChunkRow)
            .where(*_passage_rows_where(tenant_id, document_id))
        )
        async with self._sf() as session:
            return int((await session.execute(stmt)).scalar_one())

    async def get_passage(
        self, *, tenant_id: UUID, document_id: UUID, passage_id: UUID
    ) -> Passage | None:
        async with self._sf() as session:
            return await _read_passage(session, tenant_id, document_id, passage_id)

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
        async with self._sf() as session:
            kb_id = await _lock_settled_document(session, tenant_id, document_id)
            if kb_id is None:
                return None
            old = await _read_passage(session, tenant_id, document_id, passage_id)
            if old is None:
                await session.rollback()
                return None
            if old.row.revision != expected_revision:
                await session.rollback()
                raise PassageConflictError("revision_conflict", current=old)
            history = await _read_history(session, tenant_id, document_id, passage_id)
            if not history:
                # The text ingested from the file, kept before its first change.
                history = [
                    PassageRevision(
                        passage_id=passage_id,
                        revision=old.row.revision,
                        content=old.row.content,
                        heading_path=old.row.heading_path,
                        edited_by=None,
                    )
                ]
                session.add(_revision_row(tenant_id, kb_id, document_id, history[0]))
            revision = max(old.row.revision, history[-1].revision) + 1
            session.add(
                _revision_row(
                    tenant_id,
                    kb_id,
                    document_id,
                    PassageRevision(
                        passage_id=passage_id,
                        revision=revision,
                        content=content,
                        heading_path=tuple(heading_path),
                        edited_by=edited_by,
                    ),
                )
            )
            await _write_passage(
                session,
                PassageLayout(
                    passage_id=passage_id,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    document_id=document_id,
                    old=old,
                    **await _next_numbers(session, tenant_id, document_id),
                ),
                content=content,
                heading_path=heading_path,
                children=children,
                revision=revision,
                edited=edited_since(history[0], content, heading_path),
                enabled=old.row.enabled,
                disabled_reason=old.row.disabled_reason,
            )
            written = await _read_passage(session, tenant_id, document_id, passage_id)
            await session.commit()
        return written

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
        passage_id = uuid4()
        async with self._sf() as session:
            kb_id = await _lock_settled_document(session, tenant_id, document_id)
            if kb_id is None:
                return None
            first = PassageRevision(
                passage_id=passage_id,
                revision=1,
                content=content,
                heading_path=tuple(heading_path),
                edited_by=edited_by,
            )
            session.add(_revision_row(tenant_id, kb_id, document_id, first))
            await _write_passage(
                session,
                PassageLayout(
                    passage_id=passage_id,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    document_id=document_id,
                    old=None,
                    **await _next_numbers(session, tenant_id, document_id),
                ),
                content=content,
                heading_path=heading_path,
                children=children,
                revision=1,
                edited=True,
                enabled=True,
            )
            written = await _read_passage(session, tenant_id, document_id, passage_id)
            await session.commit()
        return written

    async def set_passage_enabled(
        self, *, tenant_id: UUID, document_id: UUID, passage_id: UUID, enabled: bool
    ) -> Passage | None:
        async with self._sf() as session:
            if await _lock_settled_document(session, tenant_id, document_id) is None:
                return None
            passage = await _read_passage(session, tenant_id, document_id, passage_id)
            if passage is None:
                await session.rollback()
                return None
            await session.execute(
                update(KnowledgeChunkRow)
                .where(
                    KnowledgeChunkRow.tenant_id == tenant_id,
                    KnowledgeChunkRow.document_id == document_id,
                    KnowledgeChunkRow.id.in_({passage.id, *(c.id for c in passage.children)}),
                )
                .values(enabled=enabled, disabled_reason=None if enabled else "manual")
            )
            if passage.row.disabled_reason == "inherited":
                await _settle_switch_offs(session, tenant_id, document_id, passage)
            written = await _read_passage(session, tenant_id, document_id, passage_id)
            await session.commit()
        return written

    async def list_passage_revisions(
        self, *, tenant_id: UUID, document_id: UUID, passage_id: UUID
    ) -> list[PassageRevision]:
        async with self._sf() as session:
            return await _read_history(session, tenant_id, document_id, passage_id)

    async def list_unapplied_revisions(
        self, *, tenant_id: UUID, document_id: UUID
    ) -> list[PassageRevision]:
        async with self._sf() as session:
            histories: dict[UUID, list[PassageRevision]] = {}
            for record in await _read_history(session, tenant_id, document_id):
                histories.setdefault(record.passage_id, []).append(record)
            current = await _current_revisions(session, tenant_id, document_id, histories)
            records = await _read_history(session, tenant_id, document_id, switch_offs=True)
        return unapplied_revisions(histories, current, records)

    async def discard_unapplied_revisions(self, *, tenant_id: UUID, document_id: UUID) -> int:
        async with self._sf() as session:
            histories: dict[UUID, list[PassageRevision]] = {}
            for record in await _read_history(session, tenant_id, document_id):
                histories.setdefault(record.passage_id, []).append(record)
            current = await _current_revisions(session, tenant_id, document_id, histories)
            records = await _read_history(session, tenant_id, document_id, switch_offs=True)
            unapplied = unapplied_revisions(histories, current, records)
            if not unapplied:
                return 0
            # Gone: the whole history; still there: the revisions above its
            # own; a switch-off record: itself.
            doomed = [
                and_(
                    KnowledgeChunkRevisionRow.chunk_id == r.passage_id,
                    KnowledgeChunkRevisionRow.switch_off_lost.is_(r.switch_off_lost),
                    true()
                    if r.switch_off_lost
                    else KnowledgeChunkRevisionRow.revision > current.get(r.passage_id, 0),
                )
                for r in unapplied
            ]
            deleted = (
                (
                    await session.execute(
                        delete(KnowledgeChunkRevisionRow)
                        .where(
                            KnowledgeChunkRevisionRow.tenant_id == tenant_id,
                            KnowledgeChunkRevisionRow.document_id == document_id,
                            or_(*doomed),
                        )
                        .returning(KnowledgeChunkRevisionRow.id)
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()
        return len(deleted)

    async def list_manual_passages(
        self, *, tenant_id: UUID, document_id: UUID
    ) -> list[ManualPassage]:
        # Every passage row is read: a manual passage's ``occurrence`` counts
        # the identical passages before it, manual or not.
        stmt = _passages_stmt(tenant_id, document_id)
        async with self._sf() as session:
            rows = [_to_chunk(row) for row in (await session.execute(stmt)).all()]
            histories: dict[UUID, list[PassageRevision]] = {}
            for record in await _read_history(session, tenant_id, document_id):
                histories.setdefault(record.passage_id, []).append(record)
        return manual_passages(rows, histories)

    async def list_switch_offs(
        self, *, tenant_id: UUID, document_id: UUID
    ) -> list[PassageRevision]:
        async with self._sf() as session:
            return await _read_history(session, tenant_id, document_id, switch_offs=True)

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
        updated = 0
        async with self._sf() as session:
            # The document row first (the order a fenced replace_chunks and a
            # passage write take): a passage write waits for this swap, or
            # this swap sees its rows.
            owner = (
                await session.execute(
                    select(KnowledgeDocumentRow.id)
                    .where(
                        KnowledgeDocumentRow.tenant_id == tenant_id,
                        KnowledgeDocumentRow.id == document_id,
                        *_fence(generation, expect_status),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if owner is None:
                await session.rollback()
                return None
            for start in range(0, len(vectors), _CHUNK_INSERT_BATCH):
                batch = vectors[start : start + _CHUNK_INSERT_BATCH]
                incoming = sa_values(
                    column("chunk_id", PG_UUID(as_uuid=True)),
                    column("content_hash", Text),
                    column("embedding", Vector(EMBEDDING_DIM)),
                    name="incoming",
                ).data([(v.chunk_id, v.content_hash, list(v.embedding)) for v in batch])
                result = await session.execute(
                    update(KnowledgeChunkRow)
                    .where(
                        KnowledgeChunkRow.tenant_id == tenant_id,
                        KnowledgeChunkRow.document_id == document_id,
                        _chunk_type_in(None),
                        KnowledgeChunkRow.id == incoming.c.chunk_id,
                        KnowledgeChunkRow.content_hash.is_not_distinct_from(
                            incoming.c.content_hash
                        ),
                    )
                    # VALUES columns arrive as text: the vector is cast back.
                    .values(
                        embedding=cast(incoming.c.embedding, Vector(EMBEDDING_DIM)),
                        embedding_model=embedding_model,
                    )
                    .returning(KnowledgeChunkRow.id)
                )
                updated += len(result.scalars().all())
            await session.commit()
        return updated

    # -- chunks -------------------------------------------------------------

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
        async with self._sf() as session:
            if generation is not None:
                # Lock the document row for the length of the swap so a
                # concurrent fenced status write on the same document
                # serialises behind it; the fence itself is the predicate.
                owner = (
                    await session.execute(
                        select(KnowledgeDocumentRow.id)
                        .where(
                            KnowledgeDocumentRow.tenant_id == tenant_id,
                            KnowledgeDocumentRow.id == document_id,
                            *_fence(generation, expect_status),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if owner is None:
                    await session.rollback()
                    return False
            if switch_offs:
                await _write_switch_offs(session, tenant_id, document_id, switch_offs)
            await session.execute(
                delete(KnowledgeChunkRow).where(
                    KnowledgeChunkRow.tenant_id == tenant_id,
                    KnowledgeChunkRow.document_id == document_id,
                )
            )
            # Rows are built, flushed and let go a batch at a time, in the
            # caller's order: an ORM row with its vector, keyword vector and
            # bound parameters costs about as much again as the chunk itself,
            # and a document can have thousands (B-214) — so ``chunks`` (a lazy
            # sequence on the ingest path) is read one slice at a time, never
            # whole. Still one transaction. ``parent_chunk_id`` has no foreign
            # key, so parent rows (B-206 K-1) need not come first; the ingest
            # path yields them first anyway.
            for start in range(0, len(chunks), _CHUNK_INSERT_BATCH):
                session.add_all(
                    [_chunk_row(chunk) for chunk in chunks[start : start + _CHUNK_INSERT_BATCH]]
                )
                await session.flush()
                session.expunge_all()
            if chunk_params_hash is not None:
                await session.execute(
                    update(KnowledgeDocumentRow)
                    .where(
                        KnowledgeDocumentRow.tenant_id == tenant_id,
                        KnowledgeDocumentRow.id == document_id,
                    )
                    .values(chunk_params_hash=chunk_params_hash)
                )
            await _stamp_orphans(session, tenant_id, document_id)
            await session.commit()
        return True

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
        # Explicit column projection — never SELECT the large embedding vector
        # for a preview. The returned chunks carry ``embedding == ()`` (``None``
        # for a parent row).
        in_scope = (
            KnowledgeChunkRow.tenant_id == tenant_id,
            KnowledgeChunkRow.document_id == document_id,
            _chunk_type_in(chunk_types),
        )
        position = (
            tuple_(KnowledgeChunkRow.chunk_index, KnowledgeChunkRow.chunk_type)
            > tuple_(literal(after[0]), literal(after[1].value))
            if after is not None
            else true()
        )
        page_stmt = (
            select(*_CHUNK_COLUMNS)
            .where(*in_scope, position)
            .order_by(KnowledgeChunkRow.chunk_index.asc(), KnowledgeChunkRow.chunk_type.asc())
            .offset(offset)
            .limit(limit)
        )
        count_stmt = select(func.count()).select_from(KnowledgeChunkRow).where(*in_scope)
        async with self._sf() as session:
            rows = (await session.execute(page_stmt)).all()
            total = (await session.execute(count_stmt)).scalar_one()
        return [_to_chunk(row) for row in rows], int(total)

    async def get_chunks(
        self, *, tenant_id: UUID, chunk_ids: Sequence[UUID]
    ) -> list[KnowledgeChunk]:
        ids = list(dict.fromkeys(chunk_ids))
        if not ids:
            return []
        stmt = select(*_CHUNK_COLUMNS).where(
            KnowledgeChunkRow.tenant_id == tenant_id,
            KnowledgeChunkRow.id.in_(ids),
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        found = {row.id: _to_chunk(row) for row in rows}
        return [found[chunk_id] for chunk_id in ids if chunk_id in found]

    async def chunks_contain(self, *, tenant_id: UUID, document_id: UUID, text: str) -> bool:
        # One query on the document's chunks (``knowledge_chunk_document_idx``
        # on ``document_id`` narrows them), not a page-by-page read into the
        # application.
        stmt = select(
            select(KnowledgeChunkRow.id)
            .where(
                KnowledgeChunkRow.tenant_id == tenant_id,
                KnowledgeChunkRow.document_id == document_id,
                # The chunks ``list_chunks`` lists by default (no parent rows).
                _chunk_type_in(None),
                func.strpos(KnowledgeChunkRow.content, text) > 0,
            )
            .exists()
        )
        async with self._sf() as session:
            return bool((await session.execute(stmt)).scalar_one())

    async def chunk_embeddings_by_hash(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        content_hashes: Sequence[str],
        embedding_model: str,
    ) -> dict[str, Sequence[float]]:
        if not content_hashes:
            return {}
        wanted = sorted(set(content_hashes))
        found: dict[str, Sequence[float]] = {}
        async with self._sf() as session:
            for start in range(0, len(wanted), _EMBEDDING_READ_BATCH):
                # One ``text[]`` bind (``= ANY``), not one bind per hash: a
                # batch could be raised past asyncpg's 32767-parameter cap.
                hashes = literal(wanted[start : start + _EMBEDDING_READ_BATCH], ARRAY(Text))
                stmt = select(KnowledgeChunkRow.content_hash, KnowledgeChunkRow.embedding).where(
                    KnowledgeChunkRow.tenant_id == tenant_id,
                    KnowledgeChunkRow.document_id == document_id,
                    _chunk_type_in(None),
                    KnowledgeChunkRow.embedding_model == embedding_model,
                    KnowledgeChunkRow.content_hash == any_(hashes),
                )
                for row in (await session.execute(stmt)).all():
                    if row.content_hash is not None:
                        found[row.content_hash] = array("d", row.embedding)
        return found

    async def has_chunks_off_model(
        self, *, tenant_id: UUID, kb_id: UUID, embedding_model: str | None
    ) -> bool:
        stmt = (
            select(KnowledgeChunkRow.id)
            .where(
                KnowledgeChunkRow.tenant_id == tenant_id,
                KnowledgeChunkRow.kb_id == kb_id,
                _chunk_type_in(None),
                KnowledgeChunkRow.embedding_model.is_distinct_from(embedding_model),
            )
            .limit(1)
        )
        async with self._sf() as session:
            return (await session.execute(stmt)).first() is not None

    async def search(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        query_embedding: Sequence[float],
        limit: int = 5,
    ) -> list[KnowledgeChunk]:
        scored = await self.search_scored(
            tenant_id=tenant_id, kb_ids=kb_ids, query_embedding=query_embedding, limit=limit
        )
        return [hit.chunk for hit in scored]

    async def search_scored(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        query_embedding: Sequence[float],
        limit: int = 5,
    ) -> list[ScoredChunk]:
        if not kb_ids:
            return []
        vec = list(query_embedding)
        # Cosine *distance* (``<=>``) drives the (HNSW-backed) sort; similarity
        # = ``1 - distance`` is surfaced as the score.
        distance = KnowledgeChunkRow.embedding.cosine_distance(vec)
        stmt = (
            select(KnowledgeChunkRow, distance.label("distance"))
            # Searchable types only: parent rows have no vector, and a
            # sequential scan would still return them (NULL distance sorts last).
            .where(*_recall_scope(tenant_id, kb_ids))
            .order_by(distance)
            .limit(limit)
        )
        rows = await self._vector_rows(stmt, limit)
        return [
            ScoredChunk(
                chunk=_to_chunk_with_embedding(row), score=1.0 - float(dist), source="vector"
            )
            for row, dist in rows
        ]

    async def _vector_rows(self, stmt: Select[Any], limit: int) -> Sequence[Row[Any]]:
        """Run the vector SELECT after the HNSW scan settings, in one transaction.

        ``hnsw.ef_search`` is always safe (the HNSW index exists, so pgvector
        >= 0.5). ``hnsw.iterative_scan`` is sent on pgvector >= 0.8; if the
        server still refuses it (:data:`_SETTING_REJECTED_SQLSTATES`), that
        transaction is aborted: warn once, stop sending it for this store, and
        redo the search in a fresh transaction with ef_search only. Any other
        error propagates and turns nothing off.
        """
        async with self._sf() as session:
            version = self._vector_version
            if version is None:
                version = self._vector_version = await _read_vector_version(session)
            iterative = not self._iterative_scan_rejected and version >= _ITERATIVE_SCAN_MIN_VERSION
            try:
                await _set_hnsw_scan(session, limit, iterative=iterative)
            except DBAPIError as exc:
                sqlstate = getattr(exc.orig, "sqlstate", None)
                if not iterative or sqlstate not in _SETTING_REJECTED_SQLSTATES:
                    raise
                if not self._iterative_scan_rejected:
                    self._iterative_scan_rejected = True
                    logger.warning(
                        "knowledge.hnsw_iterative_scan_rejected — vector search continues "
                        "with hnsw.ef_search only (pgvector %s)",
                        version,
                        exc_info=True,
                    )
            else:
                return (await session.execute(stmt)).all()
        async with self._sf() as session:
            await _set_hnsw_scan(session, limit, iterative=False)
            return (await session.execute(stmt)).all()

    async def keyword_search(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        query: str,
        limit: int = 5,
    ) -> list[KnowledgeChunk]:
        scored = await self.keyword_search_scored(
            tenant_id=tenant_id, kb_ids=kb_ids, query=query, limit=limit
        )
        return [hit.chunk for hit in scored]

    async def keyword_search_scored(
        self,
        *,
        tenant_id: UUID,
        kb_ids: Sequence[UUID],
        query: str,
        limit: int = 5,
    ) -> list[ScoredChunk]:
        if not kb_ids:
            return []
        kb_query = build_kb_keyword_query(query)
        if kb_query is None:
            return []
        # Recallable chunks only — this scope is also BM25's N (``scope``
        # below): parent rows and disabled chunks would dilute every idf.
        in_scope = _recall_scope(tenant_id, kb_ids)
        hits = _kb_hits(kb_query, in_scope, _kb_keyword_tsquery(kb_query))
        units = len(kb_query.terms) + len(kb_query.phrases)
        # One row: each unit's BM25 idf. N = chunks in scope; df = matched
        # chunks holding the unit, in the body or the title — exact, since
        # every chunk holding a unit matches the any-unit predicate. Every
        # part is a MATERIALIZED CTE, so it runs exactly once whatever plan is
        # chosen: without statistics the planner put these one-row aggregates
        # inside a nested loop and re-ran them per matching row (10 to 44 s a
        # query); a CTE there re-reads its stored row. It also keeps ``ln``
        # out of the per-row expression.
        scope = (
            select(func.count().label("chunks"))
            .select_from(KnowledgeChunkRow)
            .where(*in_scope)
            .cte("scope")
            .prefix_with("MATERIALIZED")
        )
        dfs = (
            select(
                *(
                    func.sum(case((hits.c[f"hit_{i}"] > 0, literal(1)), else_=literal(0))).label(
                        f"df_{i}"
                    )
                    for i in range(units)
                )
            )
            .cte("dfs")
            .prefix_with("MATERIALIZED")
        )
        idf = [_bm25_idf(scope.c.chunks, dfs.c[f"df_{i}"]) for i in range(units)]
        weights = (
            select(
                *(weight.label(f"idf_{i}") for i, weight in enumerate(idf)),
                _balanced(idf, operator.add).label("idf_total"),
            )
            .select_from(dfs.join(scope, literal(True)))
            .cte("weights")
            .prefix_with("MATERIALIZED")
        )
        # Score = boosts + coverage (idf-weighted share of the query's terms and
        # identifier phrases present, in [0, 1]; a unit only in the document
        # title counts KB_DOC_TITLE_HIT_WEIGHT of its share) + ``ts_rank``
        # (< 0.1, only breaks coverage ties).
        weighted_hits = [weights.c[f"idf_{i}"] * hits.c[f"hit_{i}"] for i in range(units)]
        coverage = _balanced(weighted_hits, operator.add) / weights.c.idf_total
        rank = (coverage + hits.c.base).label("rank")
        # No id tie-break: chunk ids are random per ingest, so it would reorder
        # ties from one evaluation run to the next; ties keep the scan order.
        top = (
            select(hits.c.id, rank)
            .select_from(hits.join(weights, literal(True)))
            .order_by(rank.desc())
            .limit(limit)
            .cte("top")
            .prefix_with("MATERIALIZED")
        )
        stmt = (
            select(KnowledgeChunkRow, top.c.rank)
            .join(top, KnowledgeChunkRow.id == top.c.id)
            .order_by(top.c.rank.desc())
        )
        try:
            async with self._sf() as session:
                # A custom plan every time, for this transaction only: asyncpg keeps
                # the statement prepared, and a generic plan (Postgres' choice from
                # the sixth run on a connection) re-evaluates every unit's tsquery
                # per matching row — about 4x slower. The timeout is this
                # transaction's too (B-206 K-22).
                await session.execute(
                    text(
                        "SELECT set_config('plan_cache_mode', 'force_custom_plan', true), "
                        "set_config('statement_timeout', :timeout, true)"
                    ),
                    {"timeout": str(_KEYWORD_STATEMENT_TIMEOUT_MS)},
                )
                rows = (await session.execute(stmt)).all()
        except DBAPIError as exc:
            if not _recall_failed(exc):
                raise
            # The driver's text, so a statement timeout still reads as a timeout.
            raise KnowledgeRecallError(str(exc.orig)) from exc
        return [
            ScoredChunk(
                chunk=_to_chunk_with_embedding(row), score=float(rank_value), source="keyword"
            )
            for row, rank_value in rows
        ]
