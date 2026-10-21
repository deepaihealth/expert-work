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

import re
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Default per-base chunking parameters (token-counted). A base may
#: override them at creation to tune chunk granularity to its content.
DEFAULT_CHUNK_MAX_TOKENS: Final = 512
DEFAULT_CHUNK_OVERLAP_TOKENS: Final = 64

#: A document title in a chunk's retrieval text is cut to this many
#: characters: long, noisy titles hurt retrieval more than they help.
KB_DOCUMENT_TITLE_MAX_CHARS: Final = 64

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


class IngestStage(StrEnum):
    """``knowledge_document.stage`` — the furthest pipeline step an ingest
    reached. ``READY`` on success; a failed ingest stays on the step that
    failed (``status`` says it failed, ``error_code`` says why)."""

    QUEUED = "queued"
    PARSING = "parsing"
    #: B-209 —— 扫描页送识别模型(只有判出要识别的页时才经过)。
    RECOGNIZING = "recognizing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    READY = "ready"


class IngestErrorCode(StrEnum):
    """``knowledge_document.error_code`` — machine-readable failure reason
    next to the human-readable ``error``."""

    UNSUPPORTED_FILE = "unsupported_file"
    INVALID_FILE = "invalid_file"
    PARSE_FAILED = "parse_failed"
    EMBEDDING_FAILED = "embedding_failed"
    #: Reserved — written once per-tenant knowledge quotas exist.
    QUOTA_EXCEEDED = "quota_exceeded"
    #: A legacy document whose original bytes were not retained.
    CONTENT_MISSING = "content_missing"
    #: Crash recovery used up its retry budget.
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    INTERNAL_ERROR = "internal_error"


class PageDecision(StrEnum):
    """B-209 §5.2 —— how one PDF page's text was obtained."""

    #: The page's own text layer (incl. a hidden OCR layer a scanner left).
    TEXT = "text"
    #: The whole page is rendered and read by a recognition model.
    OCR = "ocr"
    #: No text and nothing drawn — nothing to read.
    BLANK = "blank"


class PageReason(StrEnum):
    """B-209 §5.2 —— why a page was decided the way it was (recorded per page)."""

    #: Signal 1: fewer than 50 characters, with an image or a non-rectangular
    #: vector path on the page.
    SPARSE_TEXT = "sparse_text"
    #: Signal 2: images cover at least 80 % of the page.
    FULL_PAGE_IMAGE = "full_page_image"
    #: Signal 3: at least 30 % of the text layer is private-use / U+FFFD / CID.
    GARBLED_TEXT = "garbled_text"
    #: A page image with the scanner's invisible OCR text layer: the layer is
    #: kept, the page is not recognized again (a bad layer trips signal 3).
    HIDDEN_TEXT_LAYER = "hidden_text_layer"


class PageStatus(StrEnum):
    """B-209 §5.6 —— where a page that needs recognition stands. Text / blank
    pages have none."""

    RECOGNIZED = "recognized"
    #: Primary and fallback model both failed — can be recognized again.
    FAILED = "failed"
    #: No recognition model configured / vendor unavailable — recognized once
    #: it is configured (「补识别」).
    PENDING = "pending"
    #: Past the per-document recognition page cap.
    OVER_LIMIT = "over_limit"


class DocumentPage(BaseModel):
    """B-209 §5.6 —— one PDF page's record: how its text was obtained and,
    for a recognized page, by which model and with what result. No page is
    ever silently empty: a page that needed recognition and got no text says
    so in ``status`` / ``error``."""

    model_config = ConfigDict(frozen=True)

    page: int = Field(ge=1, description="1-based page number")
    decision: PageDecision
    reason: PageReason | None = None
    status: PageStatus | None = Field(default=None, description="only for decision=ocr")
    provider: str | None = None
    model: str | None = Field(default=None, description="the model whose text was kept")
    chars: int = Field(default=0, ge=0, description="characters of text the page contributed")
    images: int = Field(default=0, ge=0, description="image objects on the page")
    cached: bool = Field(default=False, description="text reused from the recognition cache")
    error: str | None = None


class PageStats(BaseModel):
    """B-209 §5.6 —— a document's page counts, for the list badge
    「已识别 N 页 · 失败 M 页 · 待识别 K 页」."""

    model_config = ConfigDict(frozen=True)

    pages_total: int = Field(default=0, ge=0)
    pages_ocr: int = Field(default=0, ge=0, description="pages recognized by a model")
    pages_failed: int = Field(default=0, ge=0)
    pages_pending: int = Field(default=0, ge=0)
    pages_over_limit: int = Field(default=0, ge=0)

    @classmethod
    def of(cls, pages: Sequence[DocumentPage]) -> PageStats:
        def count(status: PageStatus) -> int:
            return sum(1 for page in pages if page.status is status)

        return cls(
            pages_total=len(pages),
            pages_ocr=count(PageStatus.RECOGNIZED),
            pages_failed=count(PageStatus.FAILED),
            pages_pending=count(PageStatus.PENDING),
            pages_over_limit=count(PageStatus.OVER_LIMIT),
        )

    @property
    def needs_recognition(self) -> bool:
        """Pages are failed or pending — 「补识别」 has something to do."""
        return self.pages_failed > 0 or self.pages_pending > 0


class KnowledgeBaseKind(StrEnum):
    """``knowledge_base.kind`` — decides the import / index / retrieval
    branch. Only ``DOCUMENT`` exists today; the others are reserved."""

    DOCUMENT = "document"
    FAQ = "faq"
    MATERIAL = "material"


class DocumentSourceType(StrEnum):
    """``knowledge_document.source_type`` — where the document came from.
    Uploads are ``FILE``; the others are reserved for later importers."""

    FILE = "file"
    URL = "url"
    OSS = "oss"
    MANUAL = "manual"
    FAQ = "faq"
    ENTRY = "entry"


class ChunkType(StrEnum):
    """``knowledge_chunk.chunk_type``. The ingestion pipeline writes
    ``TEXT``; the others are reserved for FAQ / entries / materials."""

    TEXT = "text"
    QUESTION = "question"
    SUMMARY = "summary"
    FAQ = "faq"
    FAQ_QUESTION = "faq_question"
    ENTRY = "entry"
    MATERIAL = "material"
    MATERIAL_PHRASE = "material_phrase"


#: A file extension: up to five letters / digits, at least one a letter — so a
#: dotted number (``ZL202410048761.8``) or version (``v1.2 report``) stays.
_FILE_EXTENSION_RE = re.compile(r"(?<=.)\.(?=[A-Za-z0-9]{0,4}[A-Za-z])[A-Za-z0-9]{1,5}$")


def knowledge_document_title(filename: str) -> str | None:
    """The document title a chunk's retrieval text carries: the file name
    without its directory (``/`` or ``\\``) and extension, ``_`` and
    whitespace runs folded to one space, cut to
    :data:`KB_DOCUMENT_TITLE_MAX_CHARS`. ``None`` when nothing is left."""
    name = _FILE_EXTENSION_RE.sub("", re.split(r"[/\\]", filename)[-1])
    title = re.sub(r"[_\s]+", " ", name).strip()[:KB_DOCUMENT_TITLE_MAX_CHARS].rstrip()
    return title or None


def chunk_retrieval_text(
    heading_path: Sequence[str], content: str, doc_title: str | None = None
) -> str:
    """The text a chunk is embedded, keyword-indexed and reranked as: its
    document title, its section breadcrumb, then its body. The single source
    of the ``[Document: …]`` and ``[Section: a > b]`` prefixes — the body
    itself is stored clean.

    Legacy chunks (ingested before the heading path was stored separately)
    carry the prefix inside ``content`` and an empty ``heading_path``, and
    chunks ingested before titles were stored have no ``doc_title``; both
    read exactly as they did."""
    text = content if not heading_path else f"[Section: {' > '.join(heading_path)}]\n\n{content}"
    if not doc_title:
        return text
    separator = "\n" if heading_path else "\n\n"
    return f"[Document: {doc_title}]{separator}{text}"


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
    rerank_score_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "minimum rerank score to keep a hit (B-175 wave 2 PR3); a search whose every "
            "hit falls below it returns nothing. Only applies when the reranker scores "
            "(not a chat-model reranker, not when rerank failed). None disables it"
        ),
    )
    #: Embedding model that produced this base's vectors. Captured at create
    #: time; compared against the live platform model to derive ``needs_reindex``.
    embedding_provider: str | None = None
    embedding_model: str | None = None
    #: Set while a re-index is in flight (the recovery/runner re-embeds the
    #: base's retained chunk text), cleared on completion. Surfaced so the UI
    #: can show a "re-indexing" state.
    reindex_requested_at: datetime | None = None
    kind: KnowledgeBaseKind = Field(
        default=KnowledgeBaseKind.DOCUMENT, description="document / faq / material"
    )
    #: Declared metadata fields (name, type, filterable, description) that
    #: documents' ``metadata`` is validated against. Reserved (P3-PR2).
    metadata_schema: dict[str, Any] | None = None
    api_access_enabled: bool = Field(
        default=False, description="whether the open API may read this base"
    )
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
    #: Tenant-wide monotonic number (short id ``D{doc_seq}``); kept across
    #: re-uploads, never reused. ``None`` only for rows from before it existed.
    doc_seq: int | None = Field(default=None, ge=1, description="tenant-wide document number")
    title: str | None = Field(default=None, description="display title (filename by default)")
    source_type: DocumentSourceType = DocumentSourceType.FILE
    source_uri: str | None = None
    external_id: str | None = Field(default=None, description="caller's id for idempotent sync")
    metadata: dict[str, Any] = Field(default_factory=dict, description="user metadata")
    enabled: bool = Field(default=True, description="document-level on / off switch")
    stage: IngestStage | None = Field(default=None, description="furthest pipeline step reached")
    progress: int | None = Field(default=None, ge=0, le=100)
    error_code: IngestErrorCode | None = Field(
        default=None, description="machine-readable failure reason"
    )
    page_count: int | None = Field(default=None, ge=0)
    #: B-209 —— page counts of the latest PDF ingest (``None``: not a PDF, or
    #: ingested before page records existed).
    page_stats: PageStats | None = None
    size_bytes: int | None = Field(
        default=None, ge=0, description="size of the serving version's original file"
    )
    mime: str | None = None
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
    content: str = Field(description="the chunk body, without its section breadcrumb")
    embedding: tuple[float, ...] = Field(
        repr=False, description="semantic embedding vector of ``retrieval_text``"
    )
    chunk_type: ChunkType = ChunkType.TEXT
    #: Section breadcrumb (outermost heading first), stored apart from the
    #: body; :attr:`retrieval_text` joins the two.
    heading_path: tuple[str, ...] = ()
    #: The document's title when the chunk was ingested
    #: (:func:`knowledge_document_title`) — part of :attr:`retrieval_text`.
    #: ``None`` for chunks ingested before titles were stored. Renaming a
    #: document leaves it stale until the document is ingested again.
    doc_title: str | None = None
    #: sha256 of :attr:`retrieval_text` — an unchanged chunk keeps its vector
    #: across re-ingests instead of being embedded again.
    content_hash: str | None = None
    #: ``provider/model`` that produced :attr:`embedding` — a re-ingest only
    #: reuses a stored vector made by the model it is embedding with now.
    #: ``None`` for chunks written before it was recorded (never reused).
    embedding_model: str | None = None
    enabled: bool = True
    edited: bool = False
    revision: int = Field(default=1, ge=1)
    parent_chunk_id: UUID | None = None
    #: Where the chunk sits in the source: ``[{"type": "pdf", "page": 3}]``,
    #: a sheet + row range, a slide. Reserved (P5).
    source_locator: list[dict[str, Any]] | None = None
    created_at: datetime | None = None

    @property
    def retrieval_text(self) -> str:
        """Title + breadcrumb + body — see :func:`chunk_retrieval_text`."""
        return chunk_retrieval_text(self.heading_path, self.content, self.doc_title)


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
