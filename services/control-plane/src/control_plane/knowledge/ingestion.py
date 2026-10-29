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
import functools
import hashlib
import logging
import multiprocessing
import weakref
from array import array
from collections.abc import Callable, Collection, Sequence
from concurrent.futures import Executor, Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import httpx

from control_plane.knowledge.break_cache import load_break_memo, store_break_memo
from control_plane.knowledge.chunk_rows import ChunkRows, compact
from control_plane.knowledge.chunking import (
    PARENT_MAX_TOKENS,
    ParentChunk,
    chunk_markdown_parented,
    chunk_markdown_semantic_parented,
    chunk_params_hash,
    chunk_passage_body,
    semantic_breaks_enabled,
    structural_chunk_count,
)
from control_plane.knowledge.estimate import IngestEstimator
from control_plane.knowledge.parse_snapshot import decode_in_thread, encode_in_thread
from control_plane.knowledge.parse_workers import (
    FIGURE_POOL,
    FIGURE_POOL_WORKERS,
    PoolBudget,
    PoolSlot,
    limit_worker_memory,
    terminate_workers,
)
from control_plane.knowledge.parsed_size import (
    admitted,
    check_parsed_size,
    ensure_chunk_count,
    max_parsed_chars,
    parse_within_cap,
    raw_bytes_held,
)
from control_plane.knowledge.parsing import (
    DocumentParseError,
    is_plain_text_file,
    strip_lone_surrogates,
)
from control_plane.knowledge.passages import replay_manual_changes
from control_plane.knowledge.pricing import price_spend
from control_plane.knowledge.recognition.figure_pipeline import FigureDescriber
from control_plane.knowledge.recognition.ingest_parse import (
    LocalParse,
    ParseJob,
    PoolRunner,
    figures_enabled,
    finish_parse,
    local_parse,
    parse_upload,
)
from control_plane.knowledge.recognition.recognizer import BatchGuard, DocumentRecognizer
from control_plane.knowledge.stale import ModelsCheckFailedError as ModelsCheckFailedError
from control_plane.knowledge.stale import StaleIngestionError as StaleIngestionError
from control_plane.knowledge.usage import (
    IngestSpend,
    IngestUsageRecorder,
    billed_embeddings,
    spend_scope,
)
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge.base import ChunkVector, ClaimedIngestion, PassageChild
from expert_work.persistence.rls import current_tenant_id_var
from expert_work.protocol import (
    ChunkType,
    DocumentStatus,
    IngestErrorCode,
    IngestStage,
    KnowledgeChunk,
    KnowledgeDocument,
    chunk_retrieval_text,
    knowledge_document_title,
)
from expert_work.protocol.knowledge_estimate import (
    CONFIRM_WINDOW,
    IngestEstimate,
    IngestOutcome,
    needs_reconfirmation,
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

#: B-175 P3-PR2 —— a re-index's passes over one document (the first re-embeds
#: every chunk, the others the chunks still not on the new model) before it
#: gives up and leaves the base unstamped; and its page size when reading.
_REINDEX_PASSES = 3
_PAGE = 200

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
#: Address-space ceiling of one parse worker (bytes; 0 = none): :func:`limit_worker_memory`.
_parse_worker_max_vm_bytes = 0
#: B-209 PR-2 (review H3) —— the figure collection's share of a document's parse budget.
FIGURE_POOL_SHARE = 0.25
#: Parse pools killed for a timeout: a parse broken by one is a sibling's victim.
_killed_pools: weakref.WeakSet[Executor] = weakref.WeakSet()
#: Retries such a victim gets on top of its own two attempts.
_MAX_SPARED = 3

# Patched by tests so backoff does not actually sleep.
_sleep = asyncio.sleep


#: ``error`` for a legacy document whose original bytes were not retained.
CONTENT_MISSING_ERROR = "original file not retained; re-upload required"

#: Attribute an exception is tagged with on its way out of a pipeline step,
#: so the failure handler can record *why* without the exception changing
#: type (callers such as the eval runner match on the original type).
_ERROR_CODE_ATTR = "knowledge_error_code"

#: Chunks embedded per call while ingesting (B-214). The embedder's vectors
#: arrive as Python floats (~32 KiB each) and are compacted as each window
#: comes back, so a document never holds all of them in that form at once.
#: A multiple of the usual embedding batch sizes: the requests sent are the same.
CHUNK_EMBED_WINDOW = 200

#: Settled statuses whose chunks may be serving: READY, and a re-upload that
#: failed or was cancelled (B-222) while the previous version serves on. A
#: re-index re-embeds them all (and fences on the status it read). B-222 PR-2:
#: and a document being estimated or waiting (up to 7 days) for a confirmation
#: — its previous version serves meanwhile, and must not hold the base back.
_SERVING_STATUSES = frozenset(
    {
        DocumentStatus.READY,
        DocumentStatus.FAILED,
        DocumentStatus.CANCELLED,
        DocumentStatus.ESTIMATING,
        DocumentStatus.AWAITING_CONFIRMATION,
    }
)


def failure_code(exc: BaseException) -> IngestErrorCode:
    """The ``error_code`` recorded for an ingest that failed with ``exc``."""
    if isinstance(exc, DocumentParseError):
        return exc.code
    if isinstance(exc, ModelsCheckFailedError):
        return IngestErrorCode.MODELS_CHECK_FAILED
    tagged = getattr(exc, _ERROR_CODE_ATTR, None)
    return tagged if isinstance(tagged, IngestErrorCode) else IngestErrorCode.INTERNAL_ERROR


def configure_parse_pool(
    *, workers: int, timeout_s: float, max_vm_mb: int = 0, figure_max_vm_mb: int = 0
) -> None:
    """Pool size, parse timeout, parse / figure worker VM ceilings (before the first ingest)."""
    global _parse_workers, _parse_timeout_s, _parse_slots, _parse_worker_max_vm_bytes
    _parse_workers = max(1, workers)
    _parse_timeout_s = timeout_s
    _parse_worker_max_vm_bytes = max(0, max_vm_mb) * 1024 * 1024
    _parse_slots = None
    FIGURE_POOL.configure(workers=FIGURE_POOL_WORKERS, max_vm_bytes=max(0, figure_max_vm_mb) << 20)


def shutdown_parse_pool(*, kill: bool = False) -> None:
    """Tear down the parse pool and the figure pool (lifespan shutdown / tests)."""
    _shutdown_executor(kill=kill)
    FIGURE_POOL.shutdown(kill=kill)


def _shutdown_executor(*, kill: bool) -> None:
    """Drop the parse pool; ``kill`` terminates its workers — the only way to
    free a worker stuck in a parse that timed out."""
    global _parse_executor
    executor, _parse_executor = _parse_executor, None
    if executor is None:
        return
    if kill:
        terminate_workers(executor)
    executor.shutdown(wait=False, cancel_futures=True)


def _discard_parse_pool(executor: Executor) -> None:
    """Kill and drop ``executor`` — but only while it is still the current
    pool. Concurrent parses all see the same broken (or wedged) pool; the
    first to react replaces it, and a later one must not kill the
    replacement, which would break the parses already resubmitted to it
    (B-184: one dead worker failed several documents for good)."""
    if _parse_executor is executor:
        _shutdown_executor(kill=True)


def _get_parse_executor() -> Executor:
    global _parse_executor
    if _parse_executor is None:
        # ``spawn`` — never fork a process that already runs an event loop and
        # thread pools; the children only import the parsing module
        # (``control_plane.__init__`` exports the app lazily for this reason).
        _parse_executor = ProcessPoolExecutor(
            max_workers=_parse_workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=limit_worker_memory,
            initargs=(_parse_worker_max_vm_bytes,),
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
    return await _in_parse_pool(filename, parse_within_cap, filename, raw, max_parsed_chars())


def _kill_overdue(executor: Executor, filename: str) -> None:
    """A cancelled parse past its time (B-222, :class:`PoolSlot`): kill its
    pool like a timeout does — the parses broken with it are spared. (A pool
    already replaced was killed by whoever replaced it.)"""
    logger.warning("knowledge.parse_overdue document=%r — killing the parse pool", filename)
    _killed_pools.add(executor)
    _discard_parse_pool(executor)


async def _in_parse_pool[T](
    filename: str, fn: Callable[..., T], *args: object, budget: PoolBudget | None = None
) -> T:
    """``fn(*args)`` in the parse pool, with :func:`parse_document_in_pool`'s
    slot, timeout and broken-pool handling (B-209: PDF page analysis and page
    rendering use PyMuPDF too, so they run there as well). With ``budget`` the
    call may only use what is left of the document's budget."""
    attempt = spared = 0
    async with PoolSlot(_get_parse_slots()) as slot:
        while True:
            attempt += 1
            loop = asyncio.get_running_loop()
            timeout = _parse_timeout_s if budget is None else budget.remaining
            if timeout <= 0:
                msg = (
                    f"document {filename!r} parse timed out: its {_parse_timeout_s:g}s are used up"
                )
                raise DocumentParseError(msg)
            executor = _get_parse_executor()
            started = loop.time()
            charged = True
            try:
                # ``submit`` itself raises on a broken pool — keep it in the try.
                call: Future[T] = executor.submit(fn, *args)
                slot.holds(
                    call,
                    deadline=started + timeout,
                    kill=functools.partial(_kill_overdue, executor, filename),
                )
                return await asyncio.wait_for(asyncio.wrap_future(call), timeout=timeout)
            except TimeoutError as exc:
                logger.warning(
                    "knowledge.parse_timeout document=%r timeout=%gs — killing the parse pool",
                    filename,
                    timeout,
                )
                _killed_pools.add(executor)
                _discard_parse_pool(executor)
                msg = (
                    f"document {filename!r} parse timed out after {timeout:g}s"
                    f" (what was left of its {_parse_timeout_s:g}s)"
                )
                raise DocumentParseError(msg) from exc
            except BrokenProcessPool:
                # Killed for another document's timeout: not this one's
                # failure — retried without spending its attempt (review M2).
                sibling = executor in _killed_pools and spared < _MAX_SPARED
                logger.warning(
                    "knowledge.parse_pool_broken document=%r attempt=%d sibling=%s — rebuilding",
                    filename,
                    attempt,
                    sibling,
                )
                _discard_parse_pool(executor)
                if sibling:  # nor its time: the attempt was cut short by another
                    spared, attempt, charged = spared + 1, attempt - 1, False
                elif attempt == 2:
                    raise
            except MemoryError as exc:
                # The worker hit its address-space ceiling (S-M1): this
                # document's parse fails, the worker and the pod live on.
                msg = f"document {filename!r} needs more memory than a parse may use"
                raise DocumentParseError(msg) from exc
            finally:
                if budget is not None and charged:
                    budget.charge(loop.time() - started)


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


class _GuardedEmbedder:
    """B-222 —— runs ``guard`` before each ``embed`` call. Chunks and the
    semantic chunker's blocks are embedded a window (≤ 200 texts) at a time,
    so a document cancelled mid-ingest stops paying at the next window."""

    def __init__(self, inner: Embedder, guard: BatchGuard) -> None:
        self._inner = inner
        self._guard = guard

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        await self._guard()
        return await self._inner.embed(texts, tenant_id=tenant_id)


def lease_guard(
    store: KnowledgeStore,
    *,
    tenant_id: UUID,
    document_id: UUID,
    generation: int,
    lease_seconds: int,
) -> BatchGuard:
    """B-222 —— the checkpoint an ingest passes before each batch it pays
    for: a fenced lease renewal. Still this claim's document → the lease is
    extended; cancelled, re-uploaded, settled or deleted →
    :class:`StaleIngestionError` (the worker stops and writes nothing). A
    renewal that errors (a database blip) is not a refusal: the ingest goes
    on, as with the periodic renewal — its fenced writes still guard it."""

    async def guard() -> None:
        try:
            renewed = await store.renew_lease(
                tenant_id=tenant_id,
                document_id=document_id,
                generation=generation,
                now=datetime.now(UTC),
                lease_seconds=lease_seconds,
            )
        except Exception:
            logger.warning("knowledge.ingest_guard_failed document=%s", document_id, exc_info=True)
            return
        if not renewed:
            msg = f"document {document_id} was cancelled, re-uploaded or settled elsewhere"
            raise StaleIngestionError(msg)

    return guard


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
    figures: FigureDescriber | None = None,
    guard: BatchGuard | None = None,
    local: LocalParse | None = None,
    before_paid: BatchGuard | None = None,
    settle: Callable[[], None] | None = None,
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
    B-209 PR-2: ``figures`` describes the images inside PDFs / Word / slide
    files into the text, at their place (without it they are left out).
    B-222: ``guard`` (:func:`lease_guard`) runs before each embedding window;
    recognition batches and figure model calls check the fenced stage write.
    B-222 PR-2: ``local`` is the estimate's parse (:func:`parse_locally`, kept
    as its snapshot) — the parse pools are not run again; ``before_paid``
    (:attr:`PaidStart.before_paid`) runs before each recognition batch and
    figure call, after the fenced stage write.

    ``settle`` (B2b review; with ``generation``): the chunk swap also settles
    the document READY, in the same transaction (``replace_chunks``
    ``ready_chunk_count``) — a swap in place always means READY. It is
    called just before the swap (the drive's "only fenced writes follow").
    Without it the caller writes READY itself.

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
        job = _parse_job(
            store,
            tenant_id=tenant_id,
            kb_id=kb_id,
            document_id=document_id,
            filename=filename,
            raw=raw,
            generation=generation,
            before_paid=before_paid,
        )
        if local is None:
            markdown = await parse_upload(job, recognizer=recognizer, figures=figures)
        else:
            # B-222 PR-2: the estimate's parse — only the paid half is left.
            markdown = await finish_parse(job, local, recognizer=recognizer, figures=figures)
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
        guard=guard,
        settle=settle,
    )


def _parse_job(
    store: KnowledgeStore,
    *,
    tenant_id: UUID,
    kb_id: UUID,
    document_id: UUID,
    filename: str,
    raw: bytes,
    generation: int | None,
    before_paid: BatchGuard | None = None,
) -> ParseJob:
    run, run_figures = _pool_runners(filename)
    advance = functools.partial(
        _advance,
        store,
        tenant_id=tenant_id,
        document_id=document_id,
        stage=IngestStage.RECOGNIZING,
        generation=generation,
    )

    async def recognizing() -> None:
        await advance()
        if before_paid is not None:
            await before_paid()

    return ParseJob(
        store=store,
        tenant_id=tenant_id,
        kb_id=kb_id,
        document_id=document_id,
        filename=filename,
        raw=raw,
        generation=generation,
        run=run,
        run_figures=run_figures,
        recognizing=recognizing,
        lost=lambda: StaleIngestionError(f"document {document_id} was taken by another claim"),
    )


async def parse_locally(
    store: KnowledgeStore,
    *,
    tenant_id: UUID,
    kb_id: UUID,
    document_id: UUID,
    filename: str,
    raw: bytes,
    figures: FigureDescriber | None,
) -> LocalParse:
    """B-222 PR-2 —— the free half of :func:`ingest_document_bytes`'s parse:
    the parse pools only (same budgets), no model, nothing written. What an
    estimate counts; its snapshot spares the confirmed ingest from parsing
    again (``local``)."""
    job = _parse_job(
        store,
        tenant_id=tenant_id,
        kb_id=kb_id,
        document_id=document_id,
        filename=filename,
        raw=raw,
        generation=None,
    )
    on = figures is not None and await figures_enabled(figures, tenant_id=tenant_id)
    return await local_parse(job, figures_on=on)


def _pool_runners(filename: str) -> tuple[PoolRunner, PoolRunner]:
    """The document's parse-pool calls (analysis, renders, fallback parse)
    share one time budget (B-209 review M2); the figure collection runs on a
    sub-budget of it (:data:`FIGURE_POOL_SHARE`, review H3), in its own pool."""
    budget = PoolBudget(_parse_timeout_s)
    figures = PoolBudget(_parse_timeout_s * FIGURE_POOL_SHARE, parent=budget)

    async def run[T](fn: Callable[..., T], /, *args: object) -> T:
        return await _in_parse_pool(filename, fn, *args, budget=budget)

    async def run_figures[T](fn: Callable[..., T], /, *args: object) -> T:
        figures.remaining = min(figures.remaining, budget.remaining)
        return await FIGURE_POOL.run(filename, fn, *args, budget=figures)

    return run, run_figures


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
    guard: BatchGuard | None = None,
    settle: Callable[[], None] | None = None,
) -> int:
    """Chunk → embed → store already-parsed Markdown; return the chunk count
    (the chunks searched — parent rows are not counted).

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
    ``guard`` runs before each embedding window, topic-break ones included
    (B-222). ``settle``: as :func:`ingest_document_bytes`.

    B-206 K-1: the chunks are cut inside parent chunks, and each parent that
    is more than its one chunk is stored as a ``parent`` row next to them
    (never embedded; see :class:`~control_plane.knowledge.chunk_rows.ChunkRows`).
    """
    check_parsed_size(markdown)
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
            guard=guard,
            settle=settle,
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
    guard: BatchGuard | None,
    settle: Callable[[], None] | None,
) -> int:
    retrying: Embedder = _BackoffEmbedder(pinned)
    if guard is not None:
        retrying = _GuardedEmbedder(retrying, guard)
    stage_args = {"tenant_id": tenant_id, "document_id": document_id, "generation": generation}
    await _advance(store, stage=IngestStage.CHUNKING, **stage_args)
    text = strip_lone_surrogates(markdown)
    sizes = {
        "max_tokens": chunk_max_tokens,
        "overlap_tokens": chunk_overlap_tokens,
        "parent_max_tokens": PARENT_MAX_TOKENS,
    }
    params_hash = chunk_params_hash(**sizes)
    if semantic_breaks_enabled():
        # B-214: memory follows the chunk count. Refused before anything is
        # embedded when even the structural chunks are too many; topic shifts
        # only split further, so the semantic count is checked again below.
        ensure_chunk_count(
            structural_chunk_count(text, **sizes, plain_text=plain_text, doc_title=doc_title)
        )
        cache = {"tenant_id": tenant_id, "document_id": document_id, "model_key": model_key}
        memo = await load_break_memo(store, **cache)
        parents = await chunk_markdown_semantic_parented(
            text,
            **sizes,
            embedder=retrying,
            tenant_id=tenant_id,
            plain_text=plain_text,
            doc_title=doc_title,
            memo=memo,
        )
        await store_break_memo(store, **cache, memo=memo)
    else:
        # B-206 K-3: structure and size only — nothing is embedded to chunk.
        parents = chunk_markdown_parented(text, **sizes, plain_text=plain_text, doc_title=doc_title)
    # The chunks searched (and embedded) are what is capped and weighed; a
    # parent row holds text the chunks already hold, no vector.
    chunk_count = sum(len(parent.children) for parent in parents)
    ensure_chunk_count(chunk_count)
    # Chunked outside the budget (that holds the text and one window of block
    # vectors); embedded and stored inside it, weighed by its chunks. A text
    # with no chunk goes the same way: passages added by hand still go back.
    async with admitted(len(markdown), chunk_count):
        return await _embed_store(
            store,
            retrying,
            parents=parents,
            tenant_id=tenant_id,
            document_id=document_id,
            kb_id=kb_id,
            generation=generation,
            doc_title=doc_title,
            model_key=model_key,
            params_hash=params_hash,
            chunk_max_tokens=chunk_max_tokens,
            chunk_overlap_tokens=chunk_overlap_tokens,
            settle=settle,
        )


async def _embed_store(
    store: KnowledgeStore,
    retrying: Embedder,
    *,
    parents: Sequence[ParentChunk],
    tenant_id: UUID,
    document_id: UUID,
    kb_id: UUID,
    generation: int | None,
    doc_title: str | None,
    model_key: str | None,
    params_hash: str,
    chunk_max_tokens: int,
    chunk_overlap_tokens: int,
    settle: Callable[[], None] | None,
) -> int:
    stage_args = {"tenant_id": tenant_id, "document_id": document_id, "generation": generation}
    await _advance(store, stage=IngestStage.EMBEDDING, **stage_args)
    # B-175 P3-PR2: the manual changes go back onto the new passages.
    plan = replay_manual_changes(
        parents,
        await store.list_manual_passages(tenant_id=tenant_id, document_id=document_id),
        switch_offs=await store.list_switch_offs(tenant_id=tenant_id, document_id=document_id),
        max_tokens=chunk_max_tokens,
        overlap_tokens=chunk_overlap_tokens,
        doc_title=doc_title,
    )
    if any(plan.overrides) or plan.switch_offs:
        logger.info(
            "knowledge.manual_replay document=%s edits_applied=%d disabled_applied=%d "
            "added=%d disabled_inherited=%d unapplied=%d switch_offs_lost=%d",
            document_id,
            plan.edits_applied,
            plan.disabled_applied,
            plan.added,
            plan.disabled_inherited,
            len(plan.unapplied),
            plan.switch_offs_unplaced,
        )
    parents = plan.parents
    # Only the chunks are embedded; a parent is stored for reading.
    pieces = [child for parent in parents for child in parent.children]
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
    chunks = ChunkRows(
        parents=parents,
        hashes=hashes,
        vectors=vectors,
        tenant_id=tenant_id,
        kb_id=kb_id,
        document_id=document_id,
        doc_title=doc_title,
        model_key=model_key,
        overrides=plan.overrides,
    )
    await _advance(store, stage=IngestStage.INDEXING, **stage_args)
    if settle is not None:
        settle()
    _ensure_written(
        await store.replace_chunks(
            tenant_id=tenant_id,
            document_id=document_id,
            chunks=chunks,
            generation=generation,
            chunk_params_hash=params_hash,
            switch_offs=plan.switch_offs,
            ready_chunk_count=len(pieces) if settle is not None else None,
        ),
        document_id,
    )
    return len(pieces)


async def _vectors_for(
    store: KnowledgeStore,
    embedder: Embedder,
    *,
    tenant_id: UUID,
    document_id: UUID,
    texts: Sequence[str],
    hashes: Sequence[str],
    model_key: str | None,
) -> dict[str, array[float]]:
    """One vector per distinct hash: the document's stored vectors for
    unchanged chunks made by the same model, fresh embeddings for the rest
    (each distinct text once). No known model → nothing is reused. Kept
    compact (:func:`~control_plane.knowledge.chunk_rows.compact`): there is
    one per chunk."""
    stored = (
        await store.chunk_embeddings_by_hash(
            tenant_id=tenant_id,
            document_id=document_id,
            content_hashes=hashes,
            embedding_model=model_key,
        )
        if model_key is not None
        else {}
    )
    vectors = {digest: compact(vector) for digest, vector in stored.items()}
    del stored
    missing = dict(zip(hashes, texts, strict=True))
    for digest in vectors:
        missing.pop(digest, None)
    pending = list(missing.items())
    for start in range(0, len(pending), CHUNK_EMBED_WINDOW):
        window = pending[start : start + CHUNK_EMBED_WINDOW]
        fresh = await embedder.embed([text for _, text in window], tenant_id=tenant_id)
        vectors.update(
            (digest, compact(vector)) for (digest, _), vector in zip(window, fresh, strict=True)
        )
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


async def embed_passage(
    *,
    store: KnowledgeStore,
    embedder: Embedder,
    document: KnowledgeDocument,
    content: str,
    heading_path: Sequence[str],
    chunk_max_tokens: int,
    chunk_overlap_tokens: int,
    usage: IngestUsageRecorder | None = None,
) -> list[PassageChild]:
    """The chunks of a passage's new text (B-175 P3-PR2: an edit, a restore, a
    passage added by hand), cut by the base's parameters
    (:func:`~control_plane.knowledge.chunking.chunk_passage_body`) and embedded
    the way an ingest embeds: a chunk whose retrieval text the document already
    has keeps its stored vector, so only what changed is embedded — and billed
    to the tenant (``usage``)."""
    tenant_id = document.tenant_id
    doc_title = knowledge_document_title(document.filename)
    model_key, pinned = await pin_embedder(embedder, tenant_id=tenant_id)
    pieces = chunk_passage_body(
        content,
        heading_path,
        max_tokens=chunk_max_tokens,
        overlap_tokens=chunk_overlap_tokens,
        doc_title=doc_title,
    )
    texts = [chunk_retrieval_text(piece.heading_path, piece.body, doc_title) for piece in pieces]
    hashes = [content_hash(text) for text in texts]
    async with billed_embeddings(usage, tenant_id=tenant_id, model_key=model_key):
        vectors = await _vectors_for(
            store,
            _BackoffEmbedder(pinned),
            tenant_id=tenant_id,
            document_id=document.id,
            texts=texts,
            hashes=hashes,
            model_key=model_key,
        )
    return [
        PassageChild(
            content=piece.body,
            heading_path=piece.heading_path,
            content_hash=digest,
            embedding=vectors[digest],
            embedding_model=model_key,
            doc_title=doc_title,
        )
        for piece, digest in zip(pieces, hashes, strict=True)
    ]


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


# -- B-222 PR-2: estimate first, confirm, then pay ------------------------------------


async def estimate_document(
    store: KnowledgeStore,
    estimator: IngestEstimator,
    embedder: Embedder,
    claim: ClaimedIngestion,
    *,
    figures: FigureDescriber | None,
) -> KnowledgeDocument:
    """The estimate phase of a claim (``phase == "estimate"``): the local
    parse, counts and prices — no model called — recorded with the parse
    snapshot; the document then waits for its confirmation. What the ingest
    would refuse (unparseable, over the caps) is raised here, before anything
    is spent.

    :raises StaleIngestionError: the estimate was withdrawn, or the document
        re-uploaded, meanwhile (nothing written)."""
    assert claim.content is not None  # noqa: S101 — the caller checked
    local = await parse_locally(
        store,
        tenant_id=claim.tenant_id,
        kb_id=claim.kb_id,
        document_id=claim.document_id,
        filename=claim.filename,
        raw=claim.content,
        figures=figures,
    )
    model_key, _ = await pin_embedder(embedder, tenant_id=claim.tenant_id)
    now = datetime.now(UTC)
    estimate = await estimator.estimate(claim, local, embedding_model=model_key, now=now)
    finished = await store.finish_estimate(
        tenant_id=claim.tenant_id,
        document_id=claim.document_id,
        generation=claim.generation,
        estimate=estimate.model_dump(mode="json"),
        snapshot=await encode_in_thread(local),
        confirm_by=now + CONFIRM_WINDOW,
        now=now,
    )
    if finished is None:
        msg = f"document {claim.document_id} was withdrawn or re-uploaded while it was estimated"
        raise StaleIngestionError(msg)
    logger.info(
        "knowledge.estimated document=%s total_micros=%d items=%d",
        claim.document_id,
        estimate.total_micros,
        len(estimate.items),
    )
    return finished


@dataclass(frozen=True)
class PaidStart:
    """How a confirmed ingest starts: from ``local`` (the estimate's parse —
    ``None``: parse as ever), or not at all: ``returned`` — its estimate no
    longer holds and the document waits for the new one. ``embedder`` (the
    model checked against the confirmed estimate, pinned — ``None``: the
    worker's own) and ``before_paid`` (run before each recognition batch and
    figure call: a model switched since the check sends the document back for
    a new confirmation) keep the ingest on the models the user confirmed."""

    local: LocalParse | None
    returned: bool = False
    embedder: Embedder | None = None
    before_paid: BatchGuard | None = None


@dataclass(frozen=True)
class _KeyedEmbedder:
    """An embedder fixed to the model pinned at the §6.3 check (``model_key``
    is what :func:`pin_embedder` reads): the ingest embeds with the model the
    user confirmed, whatever the platform switched to since."""

    inner: Embedder
    model_key: str | None

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        return await self.inner.embed(texts, tenant_id=tenant_id)


class _ModelsGuard:
    """§14 ④ —— before a recognition batch or figure call of a confirmed
    ingest: the recognition / figure model it would use now is still the one
    the user confirmed, else the document goes back for a confirmation of a
    fresh estimate (what is cached by now is not counted again) and the
    ingest stops, writing nothing more.

    Fails closed (B2b review M6): once a switch is seen every later call is
    refused with the same :class:`StaleIngestionError` — whatever goes wrong
    sending the document back — and the check, re-estimate and snapshot run
    once per claim however many figure calls ask at the same time (a lock).
    A check that fails is a refusal too (nothing is sent back: the claim is
    left for a retry once its lease runs out)."""

    def __init__(
        self,
        store: KnowledgeStore,
        estimator: IngestEstimator,
        claim: ClaimedIngestion,
        local: LocalParse,
        confirmed: IngestEstimate,
    ) -> None:
        self._store = store
        self._estimator = estimator
        self._claim = claim
        self._local = local
        self._confirmed = confirmed
        self._lock = asyncio.Lock()
        self.refusal: StaleIngestionError | None = None

    async def __call__(self) -> None:
        if self.refusal is not None:
            raise self.refusal
        async with self._lock:
            if self.refusal is not None:
                raise self.refusal
            claim = self._claim
            try:
                changed = await self._estimator.models_changed(claim, self._confirmed.models)
            except Exception:
                # B2b review: a check that cannot tell (settings or key lookup
                # failed) refuses too — the checkpoint would let the call go.
                logger.warning(
                    "knowledge.models_check_failed document=%s", claim.document_id, exc_info=True
                )
                self.refusal = ModelsCheckFailedError(
                    f"document {claim.document_id}: its models could not be checked"
                )
                raise self.refusal from None
            if not changed:
                return
            self.refusal = StaleIngestionError(
                f"document {claim.document_id}: a model changed since its estimate was confirmed"
            )
            try:
                await self._send_back()
            except Exception:
                logger.warning(
                    "knowledge.estimate_reconfirm_failed document=%s",
                    claim.document_id,
                    exc_info=True,
                )
            raise self.refusal

    async def _send_back(self) -> None:
        claim, now = self._claim, datetime.now(UTC)
        fresh = await self._estimator.estimate(
            claim, self._local, embedding_model=self._confirmed.models.embedding, now=now
        )
        await self._store.return_to_confirmation(
            tenant_id=claim.tenant_id,
            document_id=claim.document_id,
            generation=claim.generation,
            estimate=fresh.model_dump(mode="json"),
            snapshot=await encode_in_thread(self._local),
            confirm_by=now + CONFIRM_WINDOW,
        )
        logger.info("knowledge.estimate_reconfirm_mid_ingest document=%s", claim.document_id)


async def _estimate_again(
    estimator: IngestEstimator,
    claim: ClaimedIngestion,
    local: LocalParse,
    model_key: str | None,
    now: datetime,
) -> IngestEstimate:
    """The confirmed ingest's estimate made again (models, settings, keys and
    prices of now). A document's own refusal (too large, too many chunks,
    unreadable) stays terminal; anything else — a settings, key or price
    lookup that failed — is the same failure as a model check mid-ingest
    (B2b review): :class:`ModelsCheckFailedError`, retried."""
    try:
        return await estimator.estimate(claim, local, embedding_model=model_key, now=now)
    except (DocumentParseError, StaleIngestionError):
        raise
    except Exception as exc:
        msg = f"document {claim.document_id}: its estimate could not be made again"
        raise ModelsCheckFailedError(msg) from exc


async def start_paid_ingest(
    store: KnowledgeStore,
    estimator: IngestEstimator,
    embedder: Embedder,
    claim: ClaimedIngestion,
    *,
    figures: FigureDescriber | None,
) -> PaidStart:
    """§6.3 —— before a confirmed ingest spends anything: its parse comes
    from the estimate's snapshot (parsed again, free, when there is none, it
    cannot be read, or figure description was switched since), and it is
    estimated again with the models and prices of now. A model changed, or
    the total rose past the threshold (``needs_reconfirmation``): the
    document goes back to waiting for a confirmation of the new estimate.
    A document queued without an estimate starts as it always did.

    :raises StaleIngestionError: the claim was lost meanwhile.
    :raises ModelsCheckFailedError: the estimate could not be made again
        (a settings, key or price lookup failed) — retried."""
    confirmed = claim.estimate
    if confirmed is None or confirmed.confirmed_at is None or claim.content is None:
        return PaidStart(local=None)
    local: LocalParse | None = None
    snapshot = await store.get_parse_snapshot(
        tenant_id=claim.tenant_id, document_id=claim.document_id, estimate_id=confirmed.id
    )
    if snapshot is not None:
        local = await decode_in_thread(snapshot)
    figures_on = figures is not None and await figures_enabled(figures, tenant_id=claim.tenant_id)
    if local is None or local.figures_on != figures_on:
        local = await parse_locally(
            store,
            tenant_id=claim.tenant_id,
            kb_id=claim.kb_id,
            document_id=claim.document_id,
            filename=claim.filename,
            raw=claim.content,
            figures=figures,
        )
    else:
        logger.info("knowledge.snapshot_used document=%s", claim.document_id)
    model_key, pinned = await pin_embedder(embedder, tenant_id=claim.tenant_id)
    now = datetime.now(UTC)
    fresh = await _estimate_again(estimator, claim, local, model_key, now)
    if not needs_reconfirmation(confirmed, fresh):
        return PaidStart(
            local=local,
            embedder=_KeyedEmbedder(pinned, model_key),
            before_paid=_ModelsGuard(store, estimator, claim, local, confirmed),
        )
    returned = await store.return_to_confirmation(
        tenant_id=claim.tenant_id,
        document_id=claim.document_id,
        generation=claim.generation,
        estimate=fresh.model_dump(mode="json"),
        snapshot=await encode_in_thread(local),
        confirm_by=now + CONFIRM_WINDOW,
    )
    if not returned:
        msg = f"document {claim.document_id} was taken by another claim"
        raise StaleIngestionError(msg)
    logger.info(
        "knowledge.estimate_reconfirm document=%s confirmed_micros=%d fresh_micros=%d "
        "models_changed=%s",
        claim.document_id,
        confirmed.total_micros,
        fresh.total_micros,
        confirmed.models_fingerprint != fresh.models_fingerprint,
    )
    return PaidStart(local=None, returned=True)


async def record_actual(
    store: KnowledgeStore,
    estimator: IngestEstimator,
    claim: ClaimedIngestion,
    spend: IngestSpend,
    *,
    outcome: IngestOutcome,
    settled: bool,
) -> None:
    """Keep what the paid ingest of ``claim`` was billed on the document,
    priced like the bill (§14 ⑥ 「预估 ¥x · 实际 ¥y」). Best effort: the usage
    rows themselves are the bill."""
    estimate_id = claim.estimate.id if claim.estimate is not None else None
    try:
        actual = await price_spend(
            estimator.prices,
            spend.rows,
            estimate_id=estimate_id,
            generation=claim.generation,
            outcome=outcome,
            now=datetime.now(UTC),
        )
        await store.record_ingest_actual(
            tenant_id=claim.tenant_id,
            document_id=claim.document_id,
            generation=claim.generation,
            estimate_id=estimate_id,
            actual=actual.model_dump(mode="json"),
            settled=settled,
        )
    except Exception:
        logger.warning(
            "knowledge.ingest_actual_failed document=%s", claim.document_id, exc_info=True
        )


class LocalIngests:
    """B-222 —— the ingests this process is driving now (claimed), each with
    the call that stops it as a lost claim: its task is cancelled, in-flight
    model calls are aborted, nothing is written. One per process, shared by
    the ingestion runner and the recovery worker."""

    def __init__(self) -> None:
        self._stops: dict[UUID, Callable[[], None]] = {}

    def add(self, document_id: UUID, stop: Callable[[], None]) -> None:
        self._stops[document_id] = stop

    def discard(self, document_id: UUID, stop: Callable[[], None]) -> None:
        """Forget ``stop`` — only while it is still the one registered: a newer
        claim of the same document may have taken its place."""
        if self._stops.get(document_id) is stop:
            del self._stops[document_id]

    def cancel(self, document_ids: Collection[UUID]) -> int:
        """Stop the ingests of ``document_ids`` driven here; how many were."""
        stopped = 0
        for document_id in dict.fromkeys(document_ids):
            stop = self._stops.get(document_id)
            if stop is not None:
                logger.info("knowledge.ingest_cancelled_local document=%s", document_id)
                stop()
                stopped += 1
        return stopped


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
        figures: FigureDescriber | None = None,
        local: LocalIngests | None = None,
        estimator: IngestEstimator | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._recognizer = recognizer
        self._usage = usage
        self._figures = figures
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._tasks: set[asyncio.Task[None]] = set()
        #: B-222 —— this process's running ingests; hand it to the recovery
        #: worker so :meth:`cancel_local` stops its drives too.
        self.local = local if local is not None else LocalIngests()
        #: B-222 PR-2 —— estimates claimed documents and prices what ingests
        #: cost (none given: no prices, no figure settings — test wiring).
        self.estimator = estimator or IngestEstimator(store=store, recognizer=recognizer)

    def cancel_local(self, document_ids: Collection[UUID]) -> int:
        """B-222 —— stop at once the ingests of ``document_ids`` running in
        this process, by this runner or the recovery worker sharing
        :attr:`local` (their in-flight model calls are aborted); return how
        many were running. Call it only after the store cancelled them: the
        task ends as a lost claim and writes nothing. Ingests on other pods
        stop at their next checkpoint (:func:`lease_guard`) or lease renewal."""
        return self.local.cancel(document_ids)

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

    async def passage_children(
        self,
        *,
        document: KnowledgeDocument,
        content: str,
        heading_path: Sequence[str],
        chunk_max_tokens: int,
        chunk_overlap_tokens: int,
    ) -> list[PassageChild]:
        """:func:`embed_passage` with this runner's embedder, billed like its
        ingests (B-175 P3-PR2: the console's passage edits)."""
        return await embed_passage(
            store=self._store,
            embedder=self._embedder,
            document=document,
            content=content,
            heading_path=heading_path,
            chunk_max_tokens=chunk_max_tokens,
            chunk_overlap_tokens=chunk_overlap_tokens,
            usage=self._usage,
        )

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
        ready = written = failed = stale = retrying = False
        #: B-222 PR-2 —— what the paid ingest was billed (none for an estimate).
        spend: IngestSpend | None = None
        lost = asyncio.Event()
        #: B-222 —— set once the paid work is over (whatever its outcome).
        finishing = asyncio.Event()
        owner = asyncio.current_task()
        stop: Callable[[], None] | None = None
        try:
            async with self._semaphore, raw_bytes_held() as raw_bytes:
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
                raw_bytes.shrink(len(claim.content or b""))
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
                    if finishing.is_set():
                        # Only fenced writes follow; a refusal now is READY
                        # landing (or the claim lost — those writes then fail).
                        return
                    lost.set()
                    if owner is not None:
                        owner.cancel()

                stop = _on_lost
                self.local.add(document_id, stop)
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
                try:
                    if claim.phase == "estimate":
                        # B-222 PR-2: the free estimate; the document then
                        # waits for its confirmation.
                        await estimate_document(
                            self._store,
                            self.estimator,
                            self._embedder,
                            claim,
                            figures=self._figures,
                        )
                        return
                    start = await start_paid_ingest(
                        self._store, self.estimator, self._embedder, claim, figures=self._figures
                    )
                    if start.returned:
                        return  # asked again: nothing spent
                    spend = IngestSpend()
                    with spend_scope(spend):
                        chunk_count = await ingest_document_bytes(
                            store=self._store,
                            embedder=start.embedder or self._embedder,
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
                            figures=self._figures,
                            guard=lease_guard(
                                self._store,
                                tenant_id=tenant_id,
                                document_id=document_id,
                                generation=claim.generation,
                                lease_seconds=self._lease_seconds,
                            ),
                            local=start.local,
                            before_paid=start.before_paid,
                            settle=finishing.set,
                        )
                finally:
                    finishing.set()
                # This claim's chunks are in place and READY, in one transaction.
                written = ready = True
                logger.info(
                    "knowledge.ingest_ready document=%s chunks=%d", document_id, chunk_count
                )
        except asyncio.CancelledError:
            if not lost.is_set():
                raise
            # The lease renewal was refused (the document was re-uploaded or
            # settled by another claim) or the ingest was cancelled here
            # (B-222) — stop working on a result that can no longer be written.
            logger.info("knowledge.ingest_lost_lease document=%s", document_id)
        except ModelsCheckFailedError as exc:
            # B2b review: the check could not run — not stale, and no fault of
            # the document. Left ``processing`` for the recovery worker once
            # the lease runs out (its snapshot kept); the last attempt fails
            # with its own code instead of ``attempts_exhausted``.
            logger.warning("knowledge.ingest_models_unchecked document=%s", document_id)
            retrying = True  # a failed attempt, as the recovery worker records one
            if claim is not None and claim.attempts >= self._max_attempts:
                failed = await self._store.mark_document_failed_terminal(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    error=str(exc)[:_ERROR_CAP],
                    error_code=failure_code(exc),
                    generation=claim.generation,
                )
        except StaleIngestionError:
            # 重传 / 已被别的认领收尾 —— 本 worker 的结果作废,什么都不写。
            stale = True
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
            failed = await self._store.mark_document_failed_terminal(
                tenant_id=tenant_id,
                document_id=document_id,
                error=str(exc)[:_ERROR_CAP],
                error_code=failure_code(exc),
                generation=claim.generation,
            )
        finally:
            if stop is not None:
                self.local.discard(document_id, stop)
            await _stop_renewer(renewer)
            if claim is not None and spend is not None:
                outcome: IngestOutcome = (
                    "ready" if ready else "failed" if failed or retrying else "stopped"
                )
                # A shutdown leaves the claim for a retry: its snapshot stays.
                settled = ready or failed or stale or lost.is_set()
                await record_actual(
                    self._store, self.estimator, claim, spend, outcome=outcome, settled=settled
                )
            if claim is not None and claim.phase == "ingest" and self._figures is not None:
                await self._figures.tidy(claim, settled=ready, serving=written)
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
            if model_key is not None and not await self._store.has_chunks_off_model(
                tenant_id=tenant_id, kb_id=kb_id, embedding_model=model_key
            ):
                # B-222 PR-2: every chunk is on the model already (an earlier
                # re-index got them all but a document then re-uploading, whose
                # new version came in on the new model): stamp, embed nothing.
                documents = []
                logger.info("knowledge.reindex_stamp_only kb=%s", kb_id)
            else:
                documents = await self._store.list_documents(tenant_id=tenant_id, kb_id=kb_id)
            for document in documents:
                if document.status not in _SERVING_STATUSES:
                    continue  # only settled documents' chunks are re-embedded
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
            if not failed and await self._store.has_chunks_off_model(
                tenant_id=tenant_id, kb_id=kb_id, embedding_model=model_key
            ):
                # B-222: chunks this pass could not re-embed still serve — a
                # document re-uploading (now, or since it was listed) keeps its
                # previous version until the new one is in, and for good if the
                # re-upload is cancelled or fails. Leave the base flagged.
                failed = True
                logger.info("knowledge.reindex_chunks_left_on_old_model kb=%s", kb_id)
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
        """Re-embed the document's chunks with ``embedder`` — those not on
        ``model_key`` (B-222 PR-2: a chunk an earlier re-index, or the ingest
        of a new version, already embedded with it is not paid for again;
        with no known model, every chunk) — then, up to
        :data:`_REINDEX_PASSES` passes in all, the chunks still not on it
        (written meanwhile with the model being replaced, e.g. an edit that
        started before the switch). Raises when some are still not, or a
        write-back is refused without a re-upload (the document left the
        status it was read in), so the base is not stamped with the new model."""
        document_id = document.id
        pending = await self._collect_chunks(tenant_id=tenant_id, document_id=document_id)
        if model_key is not None:
            pending = [chunk for chunk in pending if chunk.embedding_model != model_key]
        for done in range(_REINDEX_PASSES):
            if not pending:
                return
            if done:
                logger.info(
                    "knowledge.reindex_document_stragglers document=%s pass=%d chunks=%d",
                    document_id,
                    done + 1,
                    len(pending),
                )
            if not await self._reembed(
                tenant_id=tenant_id,
                document=document,
                chunks=pending,
                embedder=embedder,
                model_key=model_key,
            ):
                return  # superseded: a re-upload owns the document now
            current = await self._collect_chunks(tenant_id=tenant_id, document_id=document_id)
            pending = [chunk for chunk in current if chunk.embedding_model != model_key]
        if pending:
            msg = (
                f"document {document_id}: {len(pending)} chunks still not on the new "
                f"embedding model after {_REINDEX_PASSES} passes"
            )
            raise RuntimeError(msg)

    async def _reembed(
        self,
        *,
        tenant_id: UUID,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
        embedder: Embedder,
        model_key: str | None,
    ) -> bool:
        """One re-index pass over ``chunks``; ``False`` when superseded (a
        re-upload owns the document now). A write refused for any other
        reason raises: the base must not be stamped while these chunks keep
        the old model."""
        # The retrieval text (title + breadcrumb + body) is what the vectors
        # encode.
        texts = [chunk.retrieval_text for chunk in chunks]
        embeddings = await embed_with_backoff(embedder, texts, tenant_id=tenant_id)
        # B-175 P3-PR2: only the vectors (and the model) are rewritten, in
        # place, and only for chunks that still have the id and text read
        # above. A passage write that landed while we were embedding (a
        # switch-off, an edit, an added passage) is never undone: rows it
        # replaced are skipped, and switches, text and parent rows are not
        # touched; the rows it wrote are checked for their model after the
        # pass. Fenced on the generation and status we read the document in.
        updated = await self._store.update_chunk_embeddings(
            tenant_id=tenant_id,
            document_id=document.id,
            vectors=[
                ChunkVector(chunk.id, chunk.content_hash, embedding)
                for chunk, embedding in zip(chunks, embeddings, strict=True)
            ],
            embedding_model=model_key,
            generation=document.ingest_generation,
            expect_status=document.status,
        )
        if updated is None:
            now = await self._store.get_document(tenant_id=tenant_id, document_id=document.id)
            if now is None or now.ingest_generation != document.ingest_generation:
                # Re-uploaded (or deleted) meanwhile: the new version is
                # embedded with the live model by its own ingest.
                logger.info("knowledge.reindex_document_superseded document=%s", document.id)
                return False
            msg = (
                f"document {document.id}: re-index write-back refused "
                f"({document.status} -> {now.status}, same generation)"
            )
            raise RuntimeError(msg)
        if updated < len(chunks):
            logger.info(
                "knowledge.reindex_document_changed_meanwhile document=%s skipped=%d",
                document.id,
                len(chunks) - updated,
            )
        return True

    async def _collect_chunks(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        chunk_types: Collection[ChunkType] | None = None,
    ) -> list[KnowledgeChunk]:
        """The document's rows of ``chunk_types`` (``None``: the chunks
        searched, :meth:`KnowledgeStore.list_chunks`'s default), read a page
        at a time by position — rows written or dropped meanwhile (a passage
        edit) do not shift the pages."""
        collected: list[KnowledgeChunk] = []
        after: tuple[int, ChunkType] | None = None
        while True:
            page, _ = await self._store.list_chunks(
                tenant_id=tenant_id,
                document_id=document_id,
                limit=_PAGE,
                chunk_types=chunk_types,
                after=after,
            )
            collected.extend(page)
            if len(page) < _PAGE:
                return collected
            after = (page[-1].chunk_index, page[-1].chunk_type)
