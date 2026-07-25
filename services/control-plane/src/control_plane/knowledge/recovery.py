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

from control_plane.knowledge.ingestion import ingest_document_bytes
from expert_work.common.observability import expert_work_counter
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge.base import ClaimedIngestion
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var
from expert_work.protocol import DocumentStatus
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
        batch_size: int,
        lease_seconds: int,
        max_attempts: int,
    ) -> None:
        if interval_s <= 0:
            msg = "interval_s must be positive"
            raise ValueError(msg)
        self._store = store
        self._embedder = embedder
        self._interval_s = interval_s
        self._batch_size = batch_size
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
        """Claim + re-drive one batch of stuck documents; return how many
        reached a terminal state (ready or failed) this pass."""
        # The claim scan is cross-tenant — bypass RLS to see every tenant's
        # stuck rows (mirrors quota/reaper.py).
        token_bypass = bypass_rls_var.set(True)
        token_tenant = current_tenant_id_var.set(None)
        try:
            claims = await self._store.claim_documents_for_ingest(
                now=datetime.now(UTC),
                lease_seconds=self._lease_seconds,
                limit=self._batch_size,
                max_attempts=self._max_attempts,
            )
        finally:
            current_tenant_id_var.reset(token_tenant)
            bypass_rls_var.reset(token_bypass)

        if claims:
            _claimed.inc(len(claims))
        settled = 0
        for claim in claims:
            # Each re-drive runs in its owning tenant's RLS context so the
            # store writes (chunks, status) pass row-level security.
            token_tenant_drive = current_tenant_id_var.set(claim.tenant_id)
            token_bypass_drive = bypass_rls_var.set(False)
            try:
                if await self._drive(claim):
                    settled += 1
            finally:
                current_tenant_id_var.reset(token_tenant_drive)
                bypass_rls_var.reset(token_bypass_drive)
        return settled

    async def _drive(self, claim: ClaimedIngestion) -> bool:
        if claim.content is None:
            # No retained bytes (legacy row) — cannot recover; fail it.
            await self._store.mark_document_failed_terminal(
                tenant_id=claim.tenant_id,
                document_id=claim.document_id,
                error="original file not retained; re-upload required",
            )
            _failed_terminal.inc()
            return True
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
            )
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
        await self._store.set_document_status(
            tenant_id=claim.tenant_id,
            document_id=claim.document_id,
            status=DocumentStatus.READY,
            chunk_count=chunk_count,
        )
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
