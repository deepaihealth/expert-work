"""Knowledge ingestion recovery worker — Stream KB (durability).

The fast in-process ingest path (:class:`KnowledgeIngestionRunner`) is a
latency optimisation; durability lives here. On a periodic schedule this
worker CAS-claims documents that are stuck — ``pending`` (uploaded but never
picked up, e.g. a crash before the fast-path task ran) or ``processing`` with
an expired lease (the worker that claimed them died mid-ingest) — and
re-drives them from the document's retained original bytes. One scan both
drains the backlog and recovers crashed work.

Mirrors :class:`control_plane.quota.reaper.ReservationReaper`: a never-raising
periodic loop with an ``asyncio.Event`` stop, started/stopped from the FastAPI
lifespan. Bounded by ``max_attempts`` so a document that repeatedly kills the
process eventually goes terminally ``failed`` instead of looping forever.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from control_plane.knowledge.ingestion import (
    CONTENT_MISSING_ERROR,
    StaleIngestionError,
    failure_code,
    ingest_document_bytes,
    renew_lease_periodically,
)
from control_plane.knowledge.recognition.figure_pipeline import FigureDescriber
from control_plane.knowledge.recognition.recognizer import DocumentRecognizer
from control_plane.knowledge.usage import IngestUsageRecorder
from expert_work.common.observability import expert_work_counter
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge.base import ClaimedIngestion
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var
from expert_work.protocol import DocumentStatus, IngestErrorCode
from orchestrator.llm import Embedder

logger = logging.getLogger("expert_work.control_plane.knowledge.recovery")

_ERROR_CAP = 500

_claimed = expert_work_counter(
    "expert_work_knowledge_ingest_claimed_total",
    "Documents claimed by the knowledge ingestion recovery worker.",
)
_recovered = expert_work_counter(
    "expert_work_knowledge_ingest_recovered_total",
    "Stuck documents re-driven to ready by the recovery worker.",
)
_failed_terminal = expert_work_counter(
    "expert_work_knowledge_ingest_failed_terminal_total",
    "Documents marked terminally failed by the recovery worker (retries exhausted).",
)
_cycle_errors = expert_work_counter(
    "expert_work_knowledge_ingest_cycle_errors_total",
    "Recovery worker cycles that ended in a caught exception.",
)


class KnowledgeIngestRecoveryWorker:
    """Background task: claim + re-drive stuck knowledge-document ingestions."""

    def __init__(
        self,
        *,
        store: KnowledgeStore,
        embedder: Embedder,
        interval_s: int,
        concurrency: int,
        lease_seconds: int,
        max_attempts: int,
        recognizer: DocumentRecognizer | None = None,
        usage: IngestUsageRecorder | None = None,
        figures: FigureDescriber | None = None,
    ) -> None:
        if interval_s <= 0:
            msg = "interval_s must be positive"
            raise ValueError(msg)
        self._store = store
        self._embedder = embedder
        self._recognizer = recognizer
        self._usage = usage
        self._figures = figures
        self._interval_s = interval_s
        # One cycle claims at most ``concurrency`` documents and drives them
        # all at once — never more claims than can be worked, so no lease
        # sits idle long enough to expire and be re-claimed elsewhere.
        self._concurrency = max(1, concurrency)
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Schedule the periodic loop. Idempotent: re-calling is a no-op."""
        if self.is_running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="knowledge-ingest-recovery")

    async def stop(self) -> None:
        """Signal stop + await the loop's clean exit."""
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=self._interval_s + 5)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        finally:
            self._task = None

    async def run_once(self) -> int:
        """Sweep exhausted documents to ``failed``, then claim + re-drive one
        batch of stuck documents concurrently; return how many of the driven
        documents reached a terminal state (ready or failed) this pass."""
        # The sweep and the claim scan are cross-tenant — bypass RLS to see
        # every tenant's stuck rows (mirrors quota/reaper.py).
        token_bypass = bypass_rls_var.set(True)
        token_tenant = current_tenant_id_var.set(None)
        try:
            now = datetime.now(UTC)
            swept = await self._store.sweep_terminal(now=now, max_attempts=self._max_attempts)
            if swept:
                _failed_terminal.inc(swept)
                logger.warning("knowledge.recovery.swept_exhausted count=%d", swept)
            claims = await self._store.claim_documents_for_ingest(
                now=now,
                lease_seconds=self._lease_seconds,
                limit=self._concurrency,
                max_attempts=self._max_attempts,
            )
        finally:
            current_tenant_id_var.reset(token_tenant)
            bypass_rls_var.reset(token_bypass)

        if not claims:
            return 0
        _claimed.inc(len(claims))
        outcomes = await asyncio.gather(
            *(self._drive_in_tenant_scope(claim) for claim in claims), return_exceptions=True
        )
        settled = 0
        for claim, outcome in zip(claims, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                _cycle_errors.inc()
                logger.error(
                    "knowledge.recovery.drive_crashed document=%s",
                    claim.document_id,
                    exc_info=outcome,
                )
            elif outcome:
                settled += 1
        return settled

    async def _drive_in_tenant_scope(self, claim: ClaimedIngestion) -> bool:
        # Each re-drive runs in its owning tenant's RLS context so the store
        # writes (chunks, status) pass row-level security. Set inside the
        # coroutine: ``gather`` copies the context at task creation.
        token_tenant = current_tenant_id_var.set(claim.tenant_id)
        token_bypass = bypass_rls_var.set(False)
        renewer = asyncio.create_task(
            renew_lease_periodically(
                self._store,
                tenant_id=claim.tenant_id,
                document_id=claim.document_id,
                generation=claim.generation,
                lease_seconds=self._lease_seconds,
            )
        )
        try:
            return await self._drive(claim)
        finally:
            renewer.cancel()
            current_tenant_id_var.reset(token_tenant)
            bypass_rls_var.reset(token_bypass)

    async def _drive(self, claim: ClaimedIngestion) -> bool:
        if claim.content is None:
            # No retained bytes (legacy row) — cannot recover; fail it.
            await self._store.mark_document_failed_terminal(
                tenant_id=claim.tenant_id,
                document_id=claim.document_id,
                error=CONTENT_MISSING_ERROR,
                error_code=IngestErrorCode.CONTENT_MISSING,
                generation=claim.generation,
            )
            _failed_terminal.inc()
            return True
        ready = False
        try:
            chunk_count = await ingest_document_bytes(
                store=self._store,
                embedder=self._embedder,
                tenant_id=claim.tenant_id,
                document_id=claim.document_id,
                kb_id=claim.kb_id,
                filename=claim.filename,
                raw=claim.content,
                chunk_max_tokens=claim.chunk_max_tokens,
                chunk_overlap_tokens=claim.chunk_overlap_tokens,
                generation=claim.generation,
                recognizer=self._recognizer,
                usage=self._usage,
                figures=self._figures,
            )
            settled = await self._store.set_document_status(
                tenant_id=claim.tenant_id,
                document_id=claim.document_id,
                status=DocumentStatus.READY,
                chunk_count=chunk_count,
                generation=claim.generation,
            )
            if not settled:
                raise StaleIngestionError(claim.document_id)
            ready = True
        except StaleIngestionError:
            # 重传 / 已被别的认领收尾 —— 本次驱动的结果作废,什么都不写。
            logger.info(
                "knowledge.recovery_stale document=%s generation=%d",
                claim.document_id,
                claim.generation,
            )
            return False
        except Exception as exc:
            gone = False
            try:
                gone = (
                    await self._store.get_document(
                        tenant_id=claim.tenant_id, document_id=claim.document_id
                    )
                ) is None
            except Exception:  # noqa: S110  # pragma: no cover - 判定失败按未删处理
                pass
            if gone:
                # 文档已被并发删除 — FK/守卫拒绝写回是正常终止,不算失败。
                logger.debug("knowledge.recovery_document_gone document=%s", claim.document_id)
                return False
            if claim.attempts >= self._max_attempts:
                await self._store.mark_document_failed_terminal(
                    tenant_id=claim.tenant_id,
                    document_id=claim.document_id,
                    error=str(exc)[:_ERROR_CAP],
                    error_code=failure_code(exc),
                    generation=claim.generation,
                )
                _failed_terminal.inc()
                logger.warning(
                    "knowledge.recovery_failed_terminal document=%s attempts=%d",
                    claim.document_id,
                    claim.attempts,
                    exc_info=True,
                )
                return True
            # Retries remain — leave it processing; the lease will expire and a
            # later sweep re-claims it.
            logger.warning(
                "knowledge.recovery_retry document=%s attempts=%d",
                claim.document_id,
                claim.attempts,
                exc_info=True,
            )
            return False
        finally:
            if self._figures is not None:
                # B-209 PR-2 (review H5): same tidy-up as the fast path.
                await self._figures.tidy(claim, settled=ready)
        _recovered.inc()
        return True

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                settled = await self.run_once()
                if settled:
                    logger.info("knowledge.recovery.settled count=%d", settled)
            except Exception:
                _cycle_errors.inc()
                logger.exception("knowledge.recovery.cycle_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except TimeoutError:
                pass  # normal periodic wake-up
