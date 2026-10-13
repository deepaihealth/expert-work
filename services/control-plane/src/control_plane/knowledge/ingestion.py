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
import logging
import multiprocessing
from collections.abc import Callable, Sequence
from concurrent.futures import Executor, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx

from control_plane.knowledge.chunking import chunk_markdown_semantic
from control_plane.knowledge.parsing import (
    DocumentParseError,
    parse_document,
    strip_lone_surrogates,
)
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge.base import ClaimedIngestion
from expert_work.persistence.rls import current_tenant_id_var
from expert_work.protocol import DocumentStatus, KnowledgeChunk, KnowledgeDocument
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
      necessarily this document's.
    """
    async with _get_parse_slots():
        for attempt in (1, 2):
            loop = asyncio.get_running_loop()
            try:
                # ``submit`` itself raises on a broken pool — keep it in the try.
                future = loop.run_in_executor(_get_parse_executor(), parse_document, filename, raw)
                return await asyncio.wait_for(future, timeout=_parse_timeout_s)
            except TimeoutError as exc:
                logger.warning(
                    "knowledge.parse_timeout document=%r timeout=%gs — killing the parse pool",
                    filename,
                    _parse_timeout_s,
                )
                shutdown_parse_pool(kill=True)
                msg = f"document {filename!r} parse timed out after {_parse_timeout_s:g}s"
                raise DocumentParseError(msg) from exc
            except BrokenProcessPool:
                logger.warning(
                    "knowledge.parse_pool_broken document=%r attempt=%d — rebuilding",
                    filename,
                    attempt,
                )
                shutdown_parse_pool(kill=True)
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


async def embed_with_backoff(
    embedder: Embedder, texts: Sequence[str], *, tenant_id: UUID
) -> list[tuple[float, ...]]:
    """``embedder.embed`` with exponential backoff on 429 / 5xx / timeouts.

    Ingestion-side only: a throttled embedding API should slow a bulk upload
    down, not fail every document in it. Texts go in slices of
    :data:`_EMBED_BATCH_SIZE` and each slice retries on its own — a 429 on the
    90th request of a large document re-sends that one request, not all 90.
    Client errors (4xx other than 429, e.g. an over-long input) are not
    retried — they will not get better."""
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
    for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
        try:
            return await embedder.embed(texts, tenant_id=tenant_id)
        except Exception as exc:
            if delay is None or not _is_retryable(exc):
                raise
            logger.warning(
                "knowledge.embed_retry attempt=%d delay=%.0fs error=%s", attempt + 1, delay, exc
            )
            await _sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


class _BackoffEmbedder:
    """Wraps an :class:`Embedder` so the semantic chunker's block embeddings
    get the same retry policy as the chunk embeddings."""

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner

    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        return await embed_with_backoff(self._inner, texts, tenant_id=tenant_id)


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
) -> int:
    """Parse → chunk → embed → store one document; return the chunk count.

    The single source of truth for the pipeline, shared by the fast in-process
    path (:class:`KnowledgeIngestionRunner`), the durable recovery worker and
    the eval runner. ``generation`` fences the chunk write-back (pass the
    claim's generation; ``None`` only for callers that own the document
    outright, e.g. a fresh eval corpus). ``parsed_markdown`` skips parsing —
    for callers that already hold the text (pre-parsed corpus, OCR path).

    :raises StaleIngestionError: the fenced write-back was rejected.
    """
    markdown = (
        parsed_markdown
        if parsed_markdown is not None
        else await parse_document_in_pool(filename, raw)
    )
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
    )


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
) -> int:
    """Chunk → embed → store already-parsed Markdown; return the chunk count.

    The post-parse half of :func:`ingest_document_bytes`, exposed so a caller
    that parses elsewhere (the eval runner, an OCR path) still runs the exact
    production chunk / embed / store path. Same fence semantics.
    """
    retrying = _BackoffEmbedder(embedder)
    chunk_texts = await chunk_markdown_semantic(
        strip_lone_surrogates(markdown),
        max_tokens=chunk_max_tokens,
        overlap_tokens=chunk_overlap_tokens,
        embedder=retrying,
        tenant_id=tenant_id,
    )
    if not chunk_texts:
        _ensure_written(
            await store.replace_chunks(
                tenant_id=tenant_id, document_id=document_id, chunks=[], generation=generation
            ),
            document_id,
        )
        return 0
    embeddings = await retrying.embed(chunk_texts, tenant_id=tenant_id)
    chunks = [
        KnowledgeChunk(
            id=uuid4(),
            tenant_id=tenant_id,
            kb_id=kb_id,
            document_id=document_id,
            chunk_index=index,
            content=text,
            embedding=embedding,
        )
        for index, (text, embedding) in enumerate(zip(chunk_texts, embeddings, strict=True))
    ]
    _ensure_written(
        await store.replace_chunks(
            tenant_id=tenant_id, document_id=document_id, chunks=chunks, generation=generation
        ),
        document_id,
    )
    return len(chunks)


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
    ) -> None:
        self._store = store
        self._embedder = embedder
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
                        error="original file not retained; re-upload required",
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
            documents = await self._store.list_documents(tenant_id=tenant_id, kb_id=kb_id)
            for document in documents:
                if document.status is not DocumentStatus.READY:
                    continue  # only documents with chunks can be re-embedded
                try:
                    await self._reindex_document(tenant_id=tenant_id, document=document)
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

    async def _reindex_document(self, *, tenant_id: UUID, document: KnowledgeDocument) -> None:
        document_id = document.id
        existing = await self._collect_chunks(tenant_id=tenant_id, document_id=document_id)
        if not existing:
            return
        texts = [chunk.content for chunk in existing]
        embeddings = await embed_with_backoff(self._embedder, texts, tenant_id=tenant_id)
        rebuilt = [
            KnowledgeChunk(
                id=chunk.id,
                tenant_id=tenant_id,
                kb_id=chunk.kb_id,
                document_id=document_id,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                embedding=embedding,
            )
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
