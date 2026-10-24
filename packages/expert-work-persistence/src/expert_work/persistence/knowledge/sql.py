"""SQLAlchemy + pgvector ``KnowledgeStore`` (Postgres / asyncpg)."""

from __future__ import annotations

import logging
import operator
import re
from array import array
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Float,
    Row,
    Select,
    Text,
    and_,
    any_,
    case,
    delete,
    func,
    literal,
    literal_column,
    or_,
    select,
    text,
    true,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import CTE

from expert_work.persistence.knowledge.base import (
    ALL_TENANTS_BASES_LIMIT,
    UNSET,
    ClaimedIngestion,
    DocumentFileFacts,
    DuplicateKnowledgeBaseError,
    KnowledgeStore,
    ParseCacheEntry,
    ParseCacheKind,
    RowSecurityActiveError,
    _Unset,
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
    KnowledgeChunkRow,
    KnowledgeDocSeqRow,
    KnowledgeDocumentRow,
    KnowledgeParseCacheRow,
)
from expert_work.protocol import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
    DEFAULT_RETRIEVAL_TOP_K,
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
    return KnowledgeDocument(
        id=row.id,
        tenant_id=row.tenant_id,
        kb_id=row.kb_id,
        filename=row.filename,
        status=DocumentStatus(row.status),
        error=row.error,
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
    KnowledgeChunkRow.edited,
    KnowledgeChunkRow.revision,
    KnowledgeChunkRow.parent_chunk_id,
    KnowledgeChunkRow.source_locator,
    KnowledgeChunkRow.created_at,
    KnowledgeChunkRow.doc_title,
)


def _to_chunk(row: Any, embedding: tuple[float, ...] = ()) -> KnowledgeChunk:
    """Build a chunk DTO from a ``KnowledgeChunkRow`` or a ``_CHUNK_COLUMNS``
    projection row (same attribute names)."""
    return KnowledgeChunk(
        id=row.id,
        tenant_id=row.tenant_id,
        kb_id=row.kb_id,
        document_id=row.document_id,
        chunk_index=row.chunk_index,
        content=row.content,
        embedding=embedding,
        chunk_type=_enum_or(ChunkType, row.chunk_type, ChunkType.TEXT) or ChunkType.TEXT,
        heading_path=tuple(row.heading_path or ()),
        doc_title=row.doc_title,
        content_hash=row.content_hash,
        embedding_model=row.embedding_model,
        enabled=row.enabled,
        edited=row.edited,
        revision=row.revision,
        parent_chunk_id=row.parent_chunk_id,
        source_locator=row.source_locator,
        created_at=row.created_at,
    )


def _to_chunk_with_embedding(row: KnowledgeChunkRow) -> KnowledgeChunk:
    return _to_chunk(row, tuple(float(value) for value in row.embedding))


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
    return KnowledgeChunkRow(
        id=chunk.id,
        tenant_id=chunk.tenant_id,
        kb_id=chunk.kb_id,
        document_id=chunk.document_id,
        chunk_index=chunk.chunk_index,
        content=chunk.content,
        embedding=list(chunk.embedding),
        # Keyword-search vector over title + breadcrumb + body — segmented
        # app-side so it is correct for CJK (see text_search).
        content_tsv=_kb_chunk_tsvector(chunk),
        chunk_type=chunk.chunk_type.value,
        heading_path=list(chunk.heading_path) or None,
        doc_title=chunk.doc_title,
        content_hash=chunk.content_hash,
        embedding_model=chunk.embedding_model,
        enabled=chunk.enabled,
        edited=chunk.edited,
        revision=chunk.revision,
        parent_chunk_id=chunk.parent_chunk_id,
        source_locator=chunk.source_locator,
    )


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
            if not row.has_pending:
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

    async def list_documents(self, *, tenant_id: UUID, kb_id: UUID) -> list[KnowledgeDocument]:
        async with self._sf() as session:
            rows = (await session.execute(self._documents_stmt(tenant_id, kb_id))).scalars().all()
        return [_to_document(row) for row in rows]

    async def list_documents_page(
        self, *, tenant_id: UUID, kb_id: UUID, limit: int, offset: int
    ) -> tuple[list[KnowledgeDocument], int]:
        page_stmt = self._documents_stmt(tenant_id, kb_id).offset(offset).limit(limit)
        count_stmt = (
            select(func.count())
            .select_from(KnowledgeDocumentRow)
            .where(
                KnowledgeDocumentRow.tenant_id == tenant_id,
                KnowledgeDocumentRow.kb_id == kb_id,
            )
        )
        async with self._sf() as session:
            rows = (await session.execute(page_stmt)).scalars().all()
            total = (await session.execute(count_stmt)).scalar_one()
        return [_to_document(row) for row in rows], int(total)

    @staticmethod
    def _documents_stmt(tenant_id: UUID, kb_id: UUID) -> Select[tuple[KnowledgeDocumentRow]]:
        # ``id`` tiebreak keeps pages stable when several documents share a
        # ``created_at`` (batch upload).
        return (
            select(KnowledgeDocumentRow)
            .where(
                KnowledgeDocumentRow.tenant_id == tenant_id,
                KnowledgeDocumentRow.kb_id == kb_id,
            )
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
            await session.execute(
                delete(KnowledgeChunkRow).where(
                    KnowledgeChunkRow.tenant_id == tenant_id,
                    KnowledgeChunkRow.document_id == document_id,
                )
            )
            # Rows are built, flushed and let go a batch at a time: an ORM row
            # with its vector, keyword vector and bound parameters costs about
            # as much again as the chunk itself, and a document can have
            # thousands (B-214). Still one transaction.
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
            await session.commit()
        return True

    async def list_chunks(
        self, *, tenant_id: UUID, document_id: UUID, offset: int = 0, limit: int = 50
    ) -> tuple[list[KnowledgeChunk], int]:
        # Explicit column projection — never SELECT the large embedding vector
        # for a preview. The returned chunks carry ``embedding == ()``.
        page_stmt = (
            select(*_CHUNK_COLUMNS)
            .where(
                KnowledgeChunkRow.tenant_id == tenant_id,
                KnowledgeChunkRow.document_id == document_id,
            )
            .order_by(KnowledgeChunkRow.chunk_index.asc())
            .offset(offset)
            .limit(limit)
        )
        count_stmt = (
            select(func.count())
            .select_from(KnowledgeChunkRow)
            .where(
                KnowledgeChunkRow.tenant_id == tenant_id,
                KnowledgeChunkRow.document_id == document_id,
            )
        )
        async with self._sf() as session:
            rows = (await session.execute(page_stmt)).all()
            total = (await session.execute(count_stmt)).scalar_one()
        return [_to_chunk(row) for row in rows], int(total)

    async def chunks_contain(self, *, tenant_id: UUID, document_id: UUID, text: str) -> bool:
        # One query on the document's chunks (``knowledge_chunk_document_idx``
        # on ``document_id`` narrows them), not a page-by-page read into the
        # application.
        stmt = select(
            select(KnowledgeChunkRow.id)
            .where(
                KnowledgeChunkRow.tenant_id == tenant_id,
                KnowledgeChunkRow.document_id == document_id,
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
                    KnowledgeChunkRow.embedding_model == embedding_model,
                    KnowledgeChunkRow.content_hash == any_(hashes),
                )
                for row in (await session.execute(stmt)).all():
                    if row.content_hash is not None:
                        found[row.content_hash] = array("d", row.embedding)
        return found

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
            .where(
                KnowledgeChunkRow.tenant_id == tenant_id,
                KnowledgeChunkRow.kb_id.in_(list(kb_ids)),
            )
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
        in_scope = (
            KnowledgeChunkRow.tenant_id == tenant_id,
            KnowledgeChunkRow.kb_id.in_(list(kb_ids)),
        )
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
        async with self._sf() as session:
            # A custom plan every time, for this transaction only: asyncpg keeps
            # the statement prepared, and a generic plan (Postgres' choice from
            # the sixth run on a connection) re-evaluates every unit's tsquery
            # per matching row — about 4x slower.
            await session.execute(
                text("SELECT set_config('plan_cache_mode', 'force_custom_plan', true)")
            )
            rows = (await session.execute(stmt)).all()
        return [
            ScoredChunk(
                chunk=_to_chunk_with_embedding(row), score=float(rank_value), source="keyword"
            )
            for row, rank_value in rows
        ]
