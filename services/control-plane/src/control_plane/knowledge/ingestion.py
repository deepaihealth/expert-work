"""Async knowledge-base document ingestion — Stream J.5.

An uploaded document is processed off the request path: the upload
endpoint records the document as ``pending`` and submits it here; this
runner then parses → chunks → embeds → stores it as a background
``asyncio.Task``, advancing ``knowledge_document.status`` along the way
(``processing`` → ``ready`` / ``failed``) so the caller can poll it.

See ``docs/streams/STREAM-J-DESIGN.md`` § 12.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from control_plane.knowledge.chunking import chunk_markdown_semantic
from control_plane.knowledge.parsing import parse_document
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.rls import current_tenant_id_var
from expert_work.protocol import DocumentStatus, KnowledgeChunk
from orchestrator.llm import Embedder

logger = logging.getLogger(__name__)

#: A failed document's ``error`` is truncated to this many characters.
_ERROR_CAP = 500

#: Fast-path durability defaults. The lease is generous (longer than any
#: single-document ingest) so the recovery worker only reclaims genuinely
#: crashed work; ``max_attempts`` bounds crash-recovery retries.
_DEFAULT_LEASE_SECONDS = 300
_DEFAULT_MAX_ATTEMPTS = 5


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
) -> int:
    """Parse → chunk → embed → store one document; return the chunk count.

    The single source of truth for the pipeline, shared by the fast in-process
    path (:class:`KnowledgeIngestionRunner`) and the durable recovery worker.
    """
    # Parsing is CPU-bound — run it off the event loop.
    markdown = await asyncio.to_thread(parse_document, filename, raw)
    chunk_texts = await chunk_markdown_semantic(
        markdown,
        max_tokens=chunk_max_tokens,
        overlap_tokens=chunk_overlap_tokens,
        embedder=embedder,
        tenant_id=tenant_id,
    )
    if not chunk_texts:
        await store.replace_chunks(tenant_id=tenant_id, document_id=document_id, chunks=[])
        return 0
    embeddings = await embedder.embed(chunk_texts, tenant_id=tenant_id)
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
    await store.replace_chunks(tenant_id=tenant_id, document_id=document_id, chunks=chunks)
    return len(chunks)


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
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._tasks: set[asyncio.Task[None]] = set()

    def submit(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        kb_id: UUID,
        filename: str,
        raw: bytes,
        chunk_max_tokens: int,
        chunk_overlap_tokens: int,
    ) -> asyncio.Task[None]:
        """Schedule ingestion of one document; return its background task."""
        task: asyncio.Task[None] = asyncio.create_task(
            self._run(
                tenant_id=tenant_id,
                document_id=document_id,
                kb_id=kb_id,
                filename=filename,
                raw=raw,
                chunk_max_tokens=chunk_max_tokens,
                chunk_overlap_tokens=chunk_overlap_tokens,
            )
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

    async def _run(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        kb_id: UUID,
        filename: str,
        raw: bytes,
        chunk_max_tokens: int,
        chunk_overlap_tokens: int,
    ) -> None:
        # The background task is not inside an HTTP request — set the RLS
        # tenant context explicitly so the store's SQL passes row-level
        # security (the reaper does the same, quota/reaper.py).
        token = current_tenant_id_var.set(tenant_id)
        try:
            # CAS-claim before ingesting so a racing recovery sweep never
            # double-processes this document. If the claim is lost (the reaper
            # already took it), this fast path no-ops.
            claim = await self._store.claim_document(
                tenant_id=tenant_id,
                document_id=document_id,
                now=datetime.now(UTC),
                lease_seconds=self._lease_seconds,
                max_attempts=self._max_attempts,
            )
            if claim is None:
                return
            chunk_count = await ingest_document_bytes(
                store=self._store,
                embedder=self._embedder,
                tenant_id=tenant_id,
                document_id=document_id,
                kb_id=kb_id,
                filename=filename,
                raw=raw,
                chunk_max_tokens=chunk_max_tokens,
                chunk_overlap_tokens=chunk_overlap_tokens,
            )
            await self._store.set_document_status(
                tenant_id=tenant_id,
                document_id=document_id,
                status=DocumentStatus.READY,
                chunk_count=chunk_count,
            )
            logger.info("knowledge.ingest_ready document=%s chunks=%d", document_id, chunk_count)
        except Exception as exc:
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
            # is handled separately by the recovery worker.
            logger.warning("knowledge.ingest_failed document=%s", document_id, exc_info=True)
            await self._store.mark_document_failed_terminal(
                tenant_id=tenant_id,
                document_id=document_id,
                error=str(exc)[:_ERROR_CAP],
            )
        finally:
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
                    await self._reindex_document(tenant_id=tenant_id, document_id=document.id)
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

    async def _reindex_document(self, *, tenant_id: UUID, document_id: UUID) -> None:
        existing = await self._collect_chunks(tenant_id=tenant_id, document_id=document_id)
        if not existing:
            return
        texts = [chunk.content for chunk in existing]
        embeddings = await self._embedder.embed(texts, tenant_id=tenant_id)
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
        await self._store.replace_chunks(
            tenant_id=tenant_id, document_id=document_id, chunks=rebuilt
        )

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
