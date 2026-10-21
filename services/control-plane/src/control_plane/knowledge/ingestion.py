"""Async knowledge-base document ingestion — Stream J.5.

An uploaded document is processed off the request path: the upload
endpoint records the document as ``pending`` and submits it here; this
runner then parses → chunks → embeds → stores it as a background
``asyncio.Task``, advancing ``knowledge_document.status`` along the way
(``processing`` → ``ready`` / ``failed``) so the caller can poll it.

Reliability (B-175 L2-PR2):

* **generation fence** — every write-back carries the ``ingest_generation``
  the worker claimed; a re-upload bumps it, so a stale worker's chunks /
  status never land on the newer document (:class:`StaleIngestionError`);
* **lease renewal** — while a document is processing its lease is renewed
  every ``lease_seconds / 3`` so a slow (large) document is not re-claimed
  by the recovery worker mid-flight;
* **embedding backoff** — 429 / 5xx / timeouts from the embedding API are
  retried with exponential backoff (:func:`embed_with_backoff`);
* **bounded concurrency** — one ``asyncio.Semaphore`` per runner caps the
  documents ingested at once (a 200-file folder upload no longer fans out
  200 parses + embed batches);
* **parse in a process pool** — PyMuPDF is not thread-safe, so parsing runs
  in a (spawned) process pool with a per-document timeout instead of
  ``asyncio.to_thread``.

See ``docs/streams/STREAM-J-DESIGN.md`` § 12.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import multiprocessing
from collections.abc import Callable, Sequence
from concurrent.futures import Executor, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from control_plane.knowledge.chunking import chunk_markdown_semantic
from control_plane.knowledge.parsing import (
    DocumentParseError,
    is_plain_text_file,
    parse_document,
    strip_lone_surrogates,
)
from control_plane.knowledge.recognition.assembly import (
    assemble_pages,
    no_text_message,
    page_records,
)
from control_plane.knowledge.recognition.pages import analyse_pdf, render_pages
from control_plane.knowledge.recognition.recognizer import DocumentRecognizer, PageOutcome
from control_plane.knowledge.usage import IngestUsageRecorder, billed_embeddings
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge.base import ClaimedIngestion
from expert_work.persistence.rls import current_tenant_id_var
from expert_work.protocol import (
    DocumentStatus,
    IngestErrorCode,
    IngestStage,
    KnowledgeChunk,
    KnowledgeDocument,
    PageStatus,
    chunk_retrieval_text,
    knowledge_document_title,
)
from orchestrator.llm import Embedder

logger = logging.getLogger(__name__)

#: A failed document's ``error`` is truncated to this many characters.
_ERROR_CAP = 500

#: Fast-path durability defaults. The lease is generous (longer than any
#: single-document ingest) so the recovery worker only reclaims genuinely
#: crashed work; ``max_attempts`` bounds crash-recovery retries.
_DEFAULT_LEASE_SECONDS = 300
_DEFAULT_MAX_ATTEMPTS = 5

#: Documents ingested at once per runner (``settings.knowledge_ingest_concurrency``).
_DEFAULT_CONCURRENCY = 4

#: Embedding retry schedule — 429 / 5xx / timeouts only. Three retries, then
#: the error propagates and the document fails with the vendor's message.
_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)

#: Texts per embedding request on the ingestion side — the same 10 the
#: OpenAI-compatible embedder splits on, so a retry re-sends one request,
#: never the whole document's worth.
_EMBED_BATCH_SIZE = 10

#: Process-pool parsing. The pool is process-global (one per control-plane
#: process), created lazily on first use with the configured worker count.
#: ``_parse_slots`` admits at most ``_parse_workers`` parses at a time so the
#: per-document timeout measures parsing, not queueing behind other parses.
_parse_executor: Executor | None = None
#: ``(loop, semaphore)`` — an asyncio semaphore binds to the loop that first
#: awaits it, so it is recreated when the running loop changes (tests).
_parse_slots: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None
_parse_workers = 2
_parse_timeout_s: float = 600.0

# Patched by tests so backoff does not actually sleep.
_sleep = asyncio.sleep


class StaleIngestionError(Exception):
    """A fenced write-back was rejected: the document was re-uploaded (new
    ``ingest_generation``) or already settled while this worker was busy.
    Nothing was written; the worker must stop quietly — the document now
    belongs to another claim."""


#: ``error`` for a legacy document whose original bytes were not retained.
CONTENT_MISSING_ERROR = "original file not retained; re-upload required"

#: Attribute an exception is tagged with on its way out of a pipeline step,
#: so the failure handler can record *why* without the exception changing
#: type (callers such as the eval runner match on the original type).
_ERROR_CODE_ATTR = "knowledge_error_code"


def failure_code(exc: BaseException) -> IngestErrorCode:
    """The ``error_code`` recorded for an ingest that failed with ``exc``."""
    if isinstance(exc, DocumentParseError):
        return exc.code
    tagged = getattr(exc, _ERROR_CODE_ATTR, None)
    return tagged if isinstance(tagged, IngestErrorCode) else IngestErrorCode.INTERNAL_ERROR


def configure_parse_pool(*, workers: int, timeout_s: float) -> None:
    """Set the parse pool's size and per-document timeout (lifespan wiring).
    Takes effect for the next pool creation; call before the first ingest."""
    global _parse_workers, _parse_timeout_s, _parse_slots
    _parse_workers = max(1, workers)
    _parse_timeout_s = timeout_s
    _parse_slots = None


def shutdown_parse_pool(*, kill: bool = False) -> None:
    """Tear down the process pool (lifespan shutdown / tests / recovery from a
    broken or wedged pool). ``kill`` terminates the worker processes — the
    only way to free a worker stuck in a parse that timed out."""
    global _parse_executor
    executor, _parse_executor = _parse_executor, None
    if executor is None:
        return
    if kill:
        _terminate_workers(executor)
    executor.shutdown(wait=False, cancel_futures=True)


def _discard_parse_pool(executor: Executor) -> None:
    """Kill and drop ``executor`` — but only while it is still the current
    pool. Concurrent parses all see the same broken (or wedged) pool; the
    first to react replaces it, and a later one must not kill the
    replacement, which would break the parses already resubmitted to it
    (B-184: one dead worker failed several documents for good)."""
    if _parse_executor is executor:
        shutdown_parse_pool(kill=True)


def _terminate_workers(executor: Executor) -> None:
    # ``ProcessPoolExecutor`` has no public kill; its ``_processes`` map is
    # the stable-enough way to reach the workers (guarded: a thread pool or a
    # test double has none).
    processes = getattr(executor, "_processes", None) or {}
    for process in list(processes.values()):
        try:
            process.terminate()
        except Exception:  # pragma: no cover - best effort
            logger.warning("knowledge.parse_pool_terminate_failed", exc_info=True)


def _get_parse_executor() -> Executor:
    global _parse_executor
    if _parse_executor is None:
        # ``spawn`` — never fork a process that already runs an event loop and
        # thread pools; the children only import the parsing module
        # (``control_plane.__init__`` exports the app lazily for this reason).
        _parse_executor = ProcessPoolExecutor(
            max_workers=_parse_workers, mp_context=multiprocessing.get_context("spawn")
        )
    return _parse_executor


def _get_parse_slots() -> asyncio.Semaphore:
    global _parse_slots
    loop = asyncio.get_running_loop()
    if _parse_slots is None or _parse_slots[0] is not loop:
        _parse_slots = (loop, asyncio.Semaphore(_parse_workers))
    return _parse_slots[1]


async def parse_document_in_pool(filename: str, raw: bytes) -> str:
    """Run :func:`parse_document` in the process pool with a timeout.

    * The timeout starts once a worker slot is held, so a document queued
      behind other parses is not charged for the wait.
    * A timeout kills and rebuilds the pool: the asyncio side cannot cancel
      a parse already running in a worker, and a wedged worker would
      otherwise hold its slot until the pod restarts.
    * A pool broken by a crashed worker (PyMuPDF segfault, OOM kill) is
      rebuilt and the parse retried once — the crash is the pool's, not
      necessarily this document's. Only the parse that finds the broken pool
      still current rebuilds it; the others retry on the replacement.
    """
    return await _in_parse_pool(filename, parse_document, filename, raw)


async def _in_parse_pool[T](filename: str, fn: Callable[..., T], *args: object) -> T:
    """``fn(*args)`` in the parse pool, with :func:`parse_document_in_pool`'s
    slot, timeout and broken-pool handling (B-209: PDF page analysis and page
    rendering use PyMuPDF too, so they run there as well)."""
    async with _get_parse_slots():
        for attempt in (1, 2):
            loop = asyncio.get_running_loop()
            executor = _get_parse_executor()
            try:
                # ``submit`` itself raises on a broken pool — keep it in the try.
                future = loop.run_in_executor(executor, fn, *args)
                return await asyncio.wait_for(future, timeout=_parse_timeout_s)
            except TimeoutError as exc:
                logger.warning(
                    "knowledge.parse_timeout document=%r timeout=%gs — killing the parse pool",
                    filename,
                    _parse_timeout_s,
                )
                _discard_parse_pool(executor)
                msg = f"document {filename!r} parse timed out after {_parse_timeout_s:g}s"
                raise DocumentParseError(msg) from exc
            except BrokenProcessPool:
                logger.warning(
                    "knowledge.parse_pool_broken document=%r attempt=%d — rebuilding",
                    filename,
                    attempt,
                )
                _discard_parse_pool(executor)
                if attempt == 2:
                    raise
    raise AssertionError("unreachable")  # pragma: no cover


def _is_retryable(exc: BaseException) -> bool:
    # Timeouts and transport-level failures (connection reset, protocol error)
    # are transient; so are 429 and 5xx. Other 4xx will not get better.
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError | TimeoutError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


#: B-206 K-8 —— DashScope reports some of its own transient faults as HTTP 400
#: with an ``InternalError.*`` code (an embedding warm-up hit
#: ``InternalError.Algo.Embedding_pipeline_Error``). Retried once only: a 400
#: may as well be a real input error, which no number of retries fixes.
_VENDOR_INTERNAL_400 = "InternalError"


def _is_vendor_internal_400(exc: BaseException) -> bool:
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code == 400
        and _VENDOR_INTERNAL_400 in exc.response.text
    )


async def embed_with_backoff(
    embedder: Embedder, texts: Sequence[str], *, tenant_id: UUID
) -> list[tuple[float, ...]]:
    """``embedder.embed`` with exponential backoff on 429 / 5xx / timeouts.

    Ingestion-side only: a throttled embedding API should slow a bulk upload
    down, not fail every document in it. Texts go in slices of
    :data:`_EMBED_BATCH_SIZE` and each slice retries on its own — a 429 on the
    90th request of a large document re-sends that one request, not all 90.
    Client errors (4xx other than 429, e.g. an over-long input) are not
    retried — they will not get better — except a vendor-internal 400, retried
    once (B-206 K-8, :data:`_VENDOR_INTERNAL_400`)."""
    vectors: list[tuple[float, ...]] = []
    items = list(texts)
    for start in range(0, len(items), _EMBED_BATCH_SIZE):
        vectors.extend(
            await _embed_slice_with_backoff(
                embedder, items[start : start + _EMBED_BATCH_SIZE], tenant_id=tenant_id
            )
        )
    return vectors


async def _embed_slice_with_backoff(
    embedder: Embedder, texts: Sequence[str], *, tenant_id: UUID
) -> list[tuple[float, ...]]:
    retried_internal_400 = False
    for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
        try:
            return await embedder.embed(texts, tenant_id=tenant_id)
        except Exception as exc:
            internal_400 = not retried_internal_400 and _is_vendor_internal_400(exc)
            if delay is None or not (_is_retryable(exc) or internal_400):
                raise
            retried_internal_400 = retried_internal_400 or internal_400
            logger.warning(
                "knowledge.embed_retry attempt=%d delay=%.0fs error=%s", attempt + 1, delay, exc
            )
            await _sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


class _BackoffEmbedder:
    """Wraps an :class:`Embedder` so the semantic chunker's block embeddings
    get the same retry policy as the chunk embeddings. A final failure is
    re-raised as is, tagged ``embedding_failed`` (see :func:`failure_code`)."""

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        try:
            return await embed_with_backoff(self._inner, texts, tenant_id=tenant_id)
        except Exception as exc:
            setattr(exc, _ERROR_CODE_ATTR, IngestErrorCode.EMBEDDING_FAILED)
            raise


async def _advance(
    store: KnowledgeStore,
    *,
    tenant_id: UUID,
    document_id: UUID,
    stage: IngestStage,
    generation: int | None,
) -> None:
    """Record the stage the ingest reached; a refused (fenced) write means
    the document is no longer ours — stop before doing the next step."""
    if not await store.set_document_stage(
        tenant_id=tenant_id, document_id=document_id, stage=stage, generation=generation
    ):
        msg = f"document {document_id} was re-uploaded or settled by another claim"
        raise StaleIngestionError(msg)


async def pin_embedder(embedder: Embedder, *, tenant_id: UUID) -> tuple[str | None, Embedder]:
    """Fix one embedding model for a whole ingest / re-index: an embedder with
    ``pin`` (the live-config ``DynamicResolvingEmbedder``) resolves once and
    returns ``provider/model`` + an embedder bound to it; one with a
    ``model_key`` attribute is already fixed; anything else (eval / test
    doubles) has no known model → ``None``, and its chunks are never reused."""
    pin = getattr(embedder, "pin", None)
    if pin is not None:
        try:
            pinned: tuple[str, Embedder] = await pin(tenant_id=tenant_id)
        except Exception as exc:
            setattr(exc, _ERROR_CODE_ATTR, IngestErrorCode.EMBEDDING_FAILED)
            raise
        return pinned
    model_key = getattr(embedder, "model_key", None)
    return (model_key if isinstance(model_key, str) else None), embedder


def content_hash(text: str) -> str:
    """``knowledge_chunk.content_hash`` — sha256 of the retrieval text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def ingest_document_bytes(
    *,
    store: KnowledgeStore,
    embedder: Embedder,
    tenant_id: UUID,
    document_id: UUID,
    kb_id: UUID,
    filename: str,
    raw: bytes,
    chunk_max_tokens: int,
    chunk_overlap_tokens: int,
    generation: int | None = None,
    parsed_markdown: str | None = None,
    recognizer: DocumentRecognizer | None = None,
    usage: IngestUsageRecorder | None = None,
) -> int:
    """Parse → chunk → embed → store one document; return the chunk count.

    The single source of truth for the pipeline, shared by the fast in-process
    path (:class:`KnowledgeIngestionRunner`), the durable recovery worker and
    the eval runner. ``generation`` fences the chunk write-back (pass the
    claim's generation; ``None`` only for callers that own the document
    outright, e.g. a fresh eval corpus). ``parsed_markdown`` skips parsing —
    for callers that already hold the text (pre-parsed corpus).

    B-209: a PDF is decided page by page; scanned pages go to ``recognizer``
    (``RECOGNIZING`` stage) and every page's outcome is recorded on the
    document — no page is silently empty. Without a ``recognizer`` scanned
    pages are recorded as waiting for recognition. ``usage`` bills the
    recognition and embedding calls to the tenant (``knowledge_ingest``).

    :raises StaleIngestionError: the fenced write-back was rejected.
    """
    if parsed_markdown is not None:
        markdown = parsed_markdown
    else:
        await _advance(
            store,
            tenant_id=tenant_id,
            document_id=document_id,
            stage=IngestStage.PARSING,
            generation=generation,
        )
        if Path(filename).suffix.lower() == ".pdf":
            markdown = await _parse_pdf_pages(
                store,
                recognizer,
                tenant_id=tenant_id,
                document_id=document_id,
                filename=filename,
                raw=raw,
                generation=generation,
            )
        else:
            markdown = await parse_document_in_pool(filename, raw)
    return await ingest_markdown(
        store=store,
        embedder=embedder,
        tenant_id=tenant_id,
        document_id=document_id,
        kb_id=kb_id,
        markdown=markdown,
        chunk_max_tokens=chunk_max_tokens,
        chunk_overlap_tokens=chunk_overlap_tokens,
        generation=generation,
        doc_title=knowledge_document_title(filename),
        plain_text=is_plain_text_file(filename),
        usage=usage,
    )


async def _parse_pdf_pages(
    store: KnowledgeStore,
    recognizer: DocumentRecognizer | None,
    *,
    tenant_id: UUID,
    document_id: UUID,
    filename: str,
    raw: bytes,
    generation: int | None,
) -> str:
    """B-209 §5.1 / §5.6 —— decide every page, recognize the scanned ones,
    record each page's outcome, then join the pages back into Markdown in
    page order. A PDF with no scanned page parses exactly as before."""
    analysis = await _in_parse_pool(filename, analyse_pdf, filename, raw)
    outcomes: dict[int, PageOutcome] = {}
    if analysis.markdown is None:
        await _advance(
            store,
            tenant_id=tenant_id,
            document_id=document_id,
            stage=IngestStage.RECOGNIZING,
            generation=generation,
        )
        scanned = [page.index for page in analysis.ocr_pages]
        if recognizer is None:
            pending = PageOutcome(
                status=PageStatus.PENDING, error="document recognition is not set up"
            )
            outcomes = dict.fromkeys(scanned, pending)
        else:

            async def render(indexes: list[int], max_pixels: int | None) -> list[bytes]:
                return await _in_parse_pool(filename, render_pages, raw, indexes, max_pixels)

            outcomes = await recognizer.recognize_document(
                tenant_id=tenant_id, pages=scanned, render=render
            )
    pages = page_records(analysis, outcomes)
    if not await store.set_document_pages(
        tenant_id=tenant_id, document_id=document_id, pages=pages, generation=generation
    ):
        msg = f"document {document_id} was re-uploaded or settled by another claim"
        raise StaleIngestionError(msg)
    if analysis.markdown is not None:
        return analysis.markdown
    markdown = assemble_pages(analysis, outcomes)
    if not strip_lone_surrogates(markdown).strip():
        raise DocumentParseError(no_text_message(filename, pages))
    return markdown


async def ingest_markdown(
    *,
    store: KnowledgeStore,
    embedder: Embedder,
    tenant_id: UUID,
    document_id: UUID,
    kb_id: UUID,
    markdown: str,
    chunk_max_tokens: int,
    chunk_overlap_tokens: int,
    generation: int | None = None,
    doc_title: str | None = None,
    plain_text: bool = False,
    usage: IngestUsageRecorder | None = None,
) -> int:
    """Chunk → embed → store already-parsed Markdown; return the chunk count.

    The post-parse half of :func:`ingest_document_bytes`, exposed so a caller
    that parses elsewhere (the eval runner, an OCR path) still runs the exact
    production chunk / embed / store path. Same fence semantics.

    Chunks are stored with a clean body plus their heading path and
    ``doc_title`` (the document's cleaned name, see
    ``knowledge_document_title``; ``None`` adds none); the embedded / indexed
    text is the three joined (``KnowledgeChunk.retrieval_text``). A
    chunk whose retrieval text is unchanged from the version being replaced
    keeps its stored vector (matched by ``content_hash`` **and** the pinned
    embedding model) instead of being embedded again. ``plain_text`` chunks
    the text as plain text, not Markdown (a ``.txt`` file). ``usage`` bills
    the embedding calls to the tenant (B-209 §5.8, ``knowledge_ingest``).
    """
    model_key, pinned = await pin_embedder(embedder, tenant_id=tenant_id)
    async with billed_embeddings(usage, tenant_id=tenant_id, model_key=model_key):
        return await _chunk_embed_store(
            store=store,
            pinned=pinned,
            model_key=model_key,
            tenant_id=tenant_id,
            document_id=document_id,
            kb_id=kb_id,
            markdown=markdown,
            chunk_max_tokens=chunk_max_tokens,
            chunk_overlap_tokens=chunk_overlap_tokens,
            generation=generation,
            doc_title=doc_title,
            plain_text=plain_text,
        )


async def _chunk_embed_store(
    *,
    store: KnowledgeStore,
    pinned: Embedder,
    model_key: str | None,
    tenant_id: UUID,
    document_id: UUID,
    kb_id: UUID,
    markdown: str,
    chunk_max_tokens: int,
    chunk_overlap_tokens: int,
    generation: int | None,
    doc_title: str | None,
    plain_text: bool,
) -> int:
    retrying = _BackoffEmbedder(pinned)
    stage_args = {"tenant_id": tenant_id, "document_id": document_id, "generation": generation}
    await _advance(store, stage=IngestStage.CHUNKING, **stage_args)
    pieces = await chunk_markdown_semantic(
        strip_lone_surrogates(markdown),
        max_tokens=chunk_max_tokens,
        overlap_tokens=chunk_overlap_tokens,
        embedder=retrying,
        tenant_id=tenant_id,
        plain_text=plain_text,
        doc_title=doc_title,
    )
    if not pieces:
        _ensure_written(
            await store.replace_chunks(
                tenant_id=tenant_id, document_id=document_id, chunks=[], generation=generation
            ),
            document_id,
        )
        return 0
    await _advance(store, stage=IngestStage.EMBEDDING, **stage_args)
    texts = [chunk_retrieval_text(piece.heading_path, piece.body, doc_title) for piece in pieces]
    hashes = [content_hash(text) for text in texts]
    vectors = await _vectors_for(
        store,
        retrying,
        tenant_id=tenant_id,
        document_id=document_id,
        texts=texts,
        hashes=hashes,
        model_key=model_key,
    )
    chunks = [
        KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant_id,
            kb_id=kb_id,
            document_id=document_id,
            chunk_index=index,
            content=piece.body,
            heading_path=piece.heading_path,
            doc_title=doc_title,
            content_hash=digest,
            embedding=vectors[digest],
            embedding_model=model_key,
        )
        for index, (piece, digest) in enumerate(zip(pieces, hashes, strict=True))
    ]
    await _advance(store, stage=IngestStage.INDEXING, **stage_args)
    _ensure_written(
        await store.replace_chunks(
            tenant_id=tenant_id, document_id=document_id, chunks=chunks, generation=generation
        ),
        document_id,
    )
    return len(chunks)


async def _vectors_for(
    store: KnowledgeStore,
    embedder: Embedder,
    *,
    tenant_id: UUID,
    document_id: UUID,
    texts: Sequence[str],
    hashes: Sequence[str],
    model_key: str | None,
) -> dict[str, tuple[float, ...]]:
    """One vector per distinct hash: the document's stored vectors for
    unchanged chunks made by the same model, fresh embeddings for the rest
    (each distinct text once). No known model → nothing is reused."""
    vectors = (
        await store.chunk_embeddings_by_hash(
            tenant_id=tenant_id,
            document_id=document_id,
            content_hashes=hashes,
            embedding_model=model_key,
        )
        if model_key is not None
        else {}
    )
    missing = dict(zip(hashes, texts, strict=True))
    for digest in vectors:
        missing.pop(digest, None)
    if missing:
        fresh = await embedder.embed(list(missing.values()), tenant_id=tenant_id)
        vectors.update(zip(missing, fresh, strict=True))
    # The only place a live check can see reuse: an embedding API returns the
    # same vector for the same text, so comparing stored vectors cannot tell.
    logger.info(
        "knowledge.ingest_vectors document=%s reused=%d embedded=%d",
        document_id,
        len(set(hashes)) - len(missing),
        len(missing),
    )
    return vectors


def _ensure_written(written: bool, document_id: UUID) -> None:
    if not written:
        msg = f"document {document_id} was re-uploaded or settled by another claim"
        raise StaleIngestionError(msg)


async def renew_lease_periodically(
    store: KnowledgeStore,
    *,
    tenant_id: UUID,
    document_id: UUID,
    generation: int,
    lease_seconds: int,
    on_lost: Callable[[], None] | None = None,
) -> None:
    """Renew the document's lease every ``lease_seconds / 3`` until cancelled
    or the renewal is refused (the document is no longer ours — re-uploaded,
    settled elsewhere, deleted). On refusal ``on_lost`` is called so the
    owner can stop working on a result that can no longer be written."""
    interval = max(lease_seconds / 3, 0.05)
    while True:
        await asyncio.sleep(interval)
        try:
            renewed = await store.renew_lease(
                tenant_id=tenant_id,
                document_id=document_id,
                generation=generation,
                now=datetime.now(UTC),
                lease_seconds=lease_seconds,
            )
        except Exception:  # pragma: no cover - a failed renewal is not fatal
            logger.warning("knowledge.lease_renew_failed document=%s", document_id, exc_info=True)
            continue
        if not renewed:
            logger.debug("knowledge.lease_renew_refused document=%s", document_id)
            if on_lost is not None:
                on_lost()
            return


async def _stop_renewer(renewer: asyncio.Task[None] | None) -> None:
    if renewer is None:
        return
    renewer.cancel()
    await asyncio.gather(renewer, return_exceptions=True)


class KnowledgeIngestionRunner:
    """Runs the parse → chunk → embed → store pipeline off the request path.

    One per process, held on ``app.state``. :meth:`submit` is fire-and-
    forget — the upload endpoint returns immediately and the caller polls
    the document's status. RLS context is set explicitly per task (the
    background task is not inside an HTTP request).
    """

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        embedder: Embedder,
        lease_seconds: int = _DEFAULT_LEASE_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        concurrency: int = _DEFAULT_CONCURRENCY,
        recognizer: DocumentRecognizer | None = None,
        usage: IngestUsageRecorder | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._recognizer = recognizer
        self._usage = usage
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._tasks: set[asyncio.Task[None]] = set()

    def submit(self, *, tenant_id: UUID, document_id: UUID) -> asyncio.Task[None]:
        """Schedule ingestion of one document; return its background task.

        Only the identity is carried: bytes, filename and chunking parameters
        are read from the claim at run time, so a re-upload that lands while
        this task is queued behind the concurrency cap is what gets ingested
        (the generation fence alone cannot tell stale *bytes* from fresh ones
        when the claim itself is fresh)."""
        task: asyncio.Task[None] = asyncio.create_task(
            self._run(tenant_id=tenant_id, document_id=document_id)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def submit_reindex(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        embedding_provider: str,
        embedding_model: str,
    ) -> asyncio.Task[None]:
        """Schedule a re-index: re-embed the base's retained chunk text with
        the current platform model (chunk boundaries/text are preserved — a
        pure embedding-model swap, valid only for a same-dimension model).
        Stamps the base's model on success; always clears the reindex flag.
        """
        task: asyncio.Task[None] = asyncio.create_task(
            self._run_reindex(
                tenant_id=tenant_id,
                kb_id=kb_id,
                embedding_provider=embedding_provider,
                embedding_model=embedding_model,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        """Await all outstanding ingestion tasks (used by tests / shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel and drain outstanding tasks — bounded shutdown."""
        for task in list(self._tasks):
            task.cancel()
        await self.drain()
        shutdown_parse_pool()

    async def _run(self, *, tenant_id: UUID, document_id: UUID) -> None:
        # The background task is not inside an HTTP request — set the RLS
        # tenant context explicitly so the store's SQL passes row-level
        # security (the reaper does the same, quota/reaper.py).
        token = current_tenant_id_var.set(tenant_id)
        renewer: asyncio.Task[None] | None = None
        claim: ClaimedIngestion | None = None
        lost = asyncio.Event()
        owner = asyncio.current_task()
        try:
            async with self._semaphore:
                # CAS-claim before ingesting so a racing recovery sweep never
                # double-processes this document. If the claim is lost (the
                # reaper already took it), this fast path no-ops. The claim
                # carries the bytes current *now*, not at submit time.
                claim = await self._store.claim_document(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    now=datetime.now(UTC),
                    lease_seconds=self._lease_seconds,
                    max_attempts=self._max_attempts,
                )
                if claim is None:
                    return
                if claim.content is None:
                    await self._store.mark_document_failed_terminal(
                        tenant_id=tenant_id,
                        document_id=document_id,
                        error=CONTENT_MISSING_ERROR,
                        error_code=IngestErrorCode.CONTENT_MISSING,
                        generation=claim.generation,
                    )
                    return

                def _on_lost() -> None:
                    lost.set()
                    if owner is not None:
                        owner.cancel()

                renewer = asyncio.create_task(
                    renew_lease_periodically(
                        self._store,
                        tenant_id=tenant_id,
                        document_id=document_id,
                        generation=claim.generation,
                        lease_seconds=self._lease_seconds,
                        on_lost=_on_lost,
                    )
                )
                chunk_count = await ingest_document_bytes(
                    store=self._store,
                    embedder=self._embedder,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    kb_id=claim.kb_id,
                    filename=claim.filename,
                    raw=claim.content,
                    chunk_max_tokens=claim.chunk_max_tokens,
                    chunk_overlap_tokens=claim.chunk_overlap_tokens,
                    generation=claim.generation,
                    recognizer=self._recognizer,
                    usage=self._usage,
                )
                settled = await self._store.set_document_status(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    status=DocumentStatus.READY,
                    chunk_count=chunk_count,
                    generation=claim.generation,
                )
                if not settled:
                    raise StaleIngestionError(document_id)
                logger.info(
                    "knowledge.ingest_ready document=%s chunks=%d", document_id, chunk_count
                )
        except asyncio.CancelledError:
            if not lost.is_set():
                raise
            # The lease renewal was refused: the document was re-uploaded or
            # settled by another claim — stop working on a result that can no
            # longer be written.
            logger.info("knowledge.ingest_lost_lease document=%s", document_id)
        except StaleIngestionError:
            # 重传 / 已被别的认领收尾 —— 本 worker 的结果作废,什么都不写。
            logger.info(
                "knowledge.ingest_stale document=%s generation=%s",
                document_id,
                claim.generation if claim is not None else None,
            )
        except Exception as exc:
            if claim is None:
                # Failed before (or at) the claim — nothing of ours is in the
                # database; the recovery worker owns the document from here.
                # Writing an unfenced FAILED could clobber its in-flight work.
                logger.warning(
                    "knowledge.ingest_claim_failed document=%s", document_id, exc_info=True
                )
                return
            gone = False
            try:
                gone = (
                    await self._store.get_document(tenant_id=tenant_id, document_id=document_id)
                ) is None
            except Exception:  # noqa: S110  # pragma: no cover - 判定失败按未删处理
                pass
            if gone:
                # 文档已被并发删除 — FK/守卫拒绝写回是正常终止,不算失败。
                logger.debug("knowledge.ingest_document_gone document=%s", document_id)
                return
            # A fast-path failure (parse error, etc.) is terminal — re-ingest is
            # an explicit user action. Crash recovery (process died mid-ingest)
            # is handled separately by the recovery worker. Fenced: a stale
            # worker's failure must not mark the newer generation failed.
            logger.warning("knowledge.ingest_failed document=%s", document_id, exc_info=True)
            await self._store.mark_document_failed_terminal(
                tenant_id=tenant_id,
                document_id=document_id,
                error=str(exc)[:_ERROR_CAP],
                error_code=failure_code(exc),
                generation=claim.generation,
            )
        finally:
            await _stop_renewer(renewer)
            current_tenant_id_var.reset(token)

    async def _run_reindex(
        self,
        *,
        tenant_id: UUID,
        kb_id: UUID,
        embedding_provider: str,
        embedding_model: str,
    ) -> None:
        token = current_tenant_id_var.set(tenant_id)
        failed = False
        try:
            # One model for the whole base; every rebuilt chunk is tagged with it.
            try:
                model_key, pinned = await pin_embedder(self._embedder, tenant_id=tenant_id)
            except Exception:
                logger.warning("knowledge.reindex_pin_failed kb=%s", kb_id, exc_info=True)
                return  # nothing re-embedded; the model stays unstamped
            documents = await self._store.list_documents(tenant_id=tenant_id, kb_id=kb_id)
            for document in documents:
                if document.status is not DocumentStatus.READY:
                    continue  # only documents with chunks can be re-embedded
                try:
                    await self._reindex_document(
                        tenant_id=tenant_id,
                        document=document,
                        embedder=pinned,
                        model_key=model_key,
                    )
                except Exception:
                    # replace_chunks is transactional — a failed re-embed (e.g.
                    # a model whose dimension differs from the fixed column) rolls
                    # back, so existing vectors are preserved. Leave the model
                    # unstamped so ``needs_reindex`` stays true and the admin sees
                    # the re-index did not take.
                    failed = True
                    logger.warning(
                        "knowledge.reindex_document_failed kb=%s document=%s",
                        kb_id,
                        document.id,
                        exc_info=True,
                    )
            if not failed:
                await self._store.stamp_embedding_model(
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    embedding_provider=embedding_provider,
                    embedding_model=embedding_model,
                )
                logger.info("knowledge.reindex_ready kb=%s", kb_id)
        finally:
            await self._store.clear_reindex(tenant_id=tenant_id, kb_id=kb_id)
            current_tenant_id_var.reset(token)

    async def _reindex_document(
        self,
        *,
        tenant_id: UUID,
        document: KnowledgeDocument,
        embedder: Embedder,
        model_key: str | None,
    ) -> None:
        document_id = document.id
        existing = await self._collect_chunks(tenant_id=tenant_id, document_id=document_id)
        if not existing:
            return
        # The retrieval text (title + breadcrumb + body) is what the vectors
        # encode.
        texts = [chunk.retrieval_text for chunk in existing]
        embeddings = await embed_with_backoff(embedder, texts, tenant_id=tenant_id)
        # Only the vector (and the model that made it) changes — title,
        # heading path, hash, type and flags stay.
        rebuilt = [
            chunk.model_copy(update={"embedding": embedding, "embedding_model": model_key})
            for chunk, embedding in zip(existing, embeddings, strict=True)
        ]
        # Fenced on the generation we read: a re-upload that landed while we
        # were embedding owns the document now (and was embedded with the
        # live model anyway) — do not put the old chunks back.
        written = await self._store.replace_chunks(
            tenant_id=tenant_id,
            document_id=document_id,
            chunks=rebuilt,
            generation=document.ingest_generation,
            expect_status=DocumentStatus.READY,
        )
        if not written:
            logger.info("knowledge.reindex_document_superseded document=%s", document_id)

    async def _collect_chunks(self, *, tenant_id: UUID, document_id: UUID) -> list[KnowledgeChunk]:
        collected: list[KnowledgeChunk] = []
        offset = 0
        while True:
            page, total = await self._store.list_chunks(
                tenant_id=tenant_id, document_id=document_id, offset=offset, limit=200
            )
            collected.extend(page)
            offset += len(page)
            if not page or offset >= total:
                break
        return collected
