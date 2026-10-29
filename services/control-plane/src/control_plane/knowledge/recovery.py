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

B-222 PR-2: it also claims ``estimating`` documents (the free estimate only;
``awaiting_confirmation`` is never claimed), withdraws estimates nobody
confirmed within 7 days and drops expired parse snapshots, every cycle.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from control_plane.audit import emit
from control_plane.knowledge.estimate import IngestEstimator
from control_plane.knowledge.ingestion import (
    CONTENT_MISSING_ERROR,
    LocalIngests,
    ModelsCheckFailedError,
    StaleIngestionError,
    estimate_document,
    failure_code,
    ingest_document_bytes,
    lease_guard,
    record_actual,
    renew_lease_periodically,
    start_paid_ingest,
)
from control_plane.knowledge.parsed_size import DocumentTooLargeError, Hold, reserve_raw_bytes
from control_plane.knowledge.recognition.figure_pipeline import FigureDescriber
from control_plane.knowledge.recognition.figure_storage import delete_figures_quietly
from control_plane.knowledge.recognition.recognizer import DocumentRecognizer
from control_plane.knowledge.usage import IngestSpend, IngestUsageRecorder, spend_scope
from expert_work.common.observability import current_trace_id_hex, expert_work_counter
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge.base import ClaimedIngestion
from expert_work.persistence.knowledge.estimates import ExpiredEstimate
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var
from expert_work.protocol import AuditAction, IngestErrorCode
from expert_work.protocol.knowledge_estimate import IngestOutcome
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.storage import ObjectStore
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
_expired = expert_work_counter(
    "expert_work_knowledge_estimate_expired_total",
    "Ingest estimates withdrawn because nobody confirmed them in time (B-222).",
)

#: B-222 PR-2 —— estimates past their deadline one cycle withdraws (the rest
#: wait for the next cycle).
_EXPIRE_BATCH = 200


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
        local: LocalIngests | None = None,
        estimator: IngestEstimator | None = None,
        audit: AuditLogger | None = None,
        object_store: ObjectStore | None = None,
    ) -> None:
        if interval_s <= 0:
            msg = "interval_s must be positive"
            raise ValueError(msg)
        self._store = store
        #: B-222 PR-2 —— a new upload withdrawn on its deadline loses its figures too.
        self._object_store = object_store
        self._embedder = embedder
        self._recognizer = recognizer
        self._usage = usage
        self._figures = figures
        #: B-222 PR-2 —— estimates claimed documents, prices what ingests cost
        #: (the runner's, so both price alike).
        self.estimator = estimator or IngestEstimator(store=store, recognizer=recognizer)
        self._interval_s = interval_s
        # One cycle claims at most ``concurrency`` documents and drives them
        # all at once — never more claims than can be worked, so no lease
        # sits idle long enough to expire and be re-claimed elsewhere.
        self._concurrency = max(1, concurrency)
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        #: B-222 —— the process's running ingests (the runner's ``local``), so
        #: a cancel on this pod stops a drive at once.
        self.local = local if local is not None else LocalIngests()
        #: B-222 PR-2 —— audits each estimate withdrawn on its deadline.
        self._audit = audit
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
        """Sweep exhausted documents to ``failed``, then claim + re-drive up to
        ``concurrency`` stuck documents concurrently; return how many of the
        driven documents reached a terminal state (ready or failed) this pass.

        Each claim first reserves room for the document's bytes in the
        server's raw-bytes budget (shared with the upload path), so documents
        are claimed one at a time — and each starts being driven, its lease
        renewed, before the next waits for room."""
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
        finally:
            current_tenant_id_var.reset(token_tenant)
            bypass_rls_var.reset(token_bypass)
        await self._expire_estimates()

        drives: list[tuple[ClaimedIngestion, asyncio.Task[bool]]] = []
        try:
            for _ in range(self._concurrency):
                claimed = await self._claim_holding()
                if claimed is None:
                    break
                claim, hold = claimed
                _claimed.inc()
                drives.append((claim, asyncio.create_task(self._drive_holding(claim, hold))))
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                for _, task in drives:
                    task.cancel()
            await asyncio.gather(*(task for _, task in drives), return_exceptions=True)
            raise
        if not drives:
            return 0
        outcomes = await asyncio.gather(*(task for _, task in drives), return_exceptions=True)
        settled = 0
        for (claim, _), outcome in zip(drives, outcomes, strict=True):
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

    async def _expire_estimates(self) -> None:
        """B-222 PR-2 (§6.5) —— withdraw the estimates nobody confirmed by their
        deadline (a new upload deleted, anything else put back as it was), one
        ``knowledge:document_estimate_expired`` audit row each (actor
        ``system``; a deleted upload's figures removed), drop parse snapshots
        past their lifetime, and release duplicates whose original is gone
        (one never parsed goes ``estimating``: this cycle's claims pick it
        up). Several pods sweeping at once withdraw each estimate once. A
        failure here is logged and leaves the claims of this cycle alone."""
        token_bypass = bypass_rls_var.set(True)
        token_tenant = current_tenant_id_var.set(None)
        try:
            now = datetime.now(UTC)
            expired = await self._store.expire_awaiting(now=now, limit=_EXPIRE_BATCH)
            purged = await self._store.purge_expired_snapshots(now=now)
            released = await self._store.release_stale_duplicates(now=now)
        except Exception:
            _cycle_errors.inc()
            logger.exception("knowledge.recovery.expire_failed")
            return
        finally:
            current_tenant_id_var.reset(token_tenant)
            bypass_rls_var.reset(token_bypass)
        if expired:
            _expired.inc(len(expired))
            logger.info("knowledge.recovery.estimates_expired count=%d", len(expired))
        if purged:
            logger.info("knowledge.recovery.snapshots_purged count=%d", purged)
        if released:
            logger.info("knowledge.recovery.duplicates_released count=%d", len(released))
        for withdrawn in expired:
            if withdrawn.outcome == "withdrawn_deleted":
                await delete_figures_quietly(
                    self._object_store,
                    tenant_id=withdrawn.tenant_id,
                    kb_id=withdrawn.kb_id,
                    document_id=withdrawn.document_id,
                )
            await self._audit_expired(withdrawn)

    async def _audit_expired(self, withdrawn: ExpiredEstimate) -> None:
        if self._audit is None:
            return
        try:
            await emit(
                self._audit,
                tenant_id=withdrawn.tenant_id,
                actor_id="system",
                action=AuditAction.KNOWLEDGE_DOCUMENT_ESTIMATE_EXPIRED,
                resource_type="knowledge_document",
                resource_id=str(withdrawn.document_id),
                trace_id=current_trace_id_hex(),
                details={
                    "kb_id": str(withdrawn.kb_id),
                    "filename": withdrawn.filename,
                    "outcome": withdrawn.outcome,
                },
            )
        except Exception:
            logger.exception(
                "knowledge.recovery.expire_audit_failed document=%s", withdrawn.document_id
            )

    async def _claim_holding(self) -> tuple[ClaimedIngestion, Hold] | None:
        """Reserve room for one document's bytes, then claim one stuck
        document; ``None`` (nothing held) when there is none left."""
        hold = await reserve_raw_bytes()
        try:
            token_bypass = bypass_rls_var.set(True)
            token_tenant = current_tenant_id_var.set(None)
            try:
                claims = await self._store.claim_documents_for_ingest(
                    now=datetime.now(UTC),
                    lease_seconds=self._lease_seconds,
                    limit=1,
                    max_attempts=self._max_attempts,
                )
            finally:
                current_tenant_id_var.reset(token_tenant)
                bypass_rls_var.reset(token_bypass)
        except BaseException:
            hold.release()
            raise
        if not claims:
            hold.release()
            return None
        hold.shrink(len(claims[0].content or b""))
        return claims[0], hold

    async def _drive_holding(self, claim: ClaimedIngestion, hold: Hold) -> bool:
        try:
            return await self._drive_in_tenant_scope(claim)
        finally:
            hold.release()

    async def _drive_in_tenant_scope(self, claim: ClaimedIngestion) -> bool:
        # Each re-drive runs in its owning tenant's RLS context so the store
        # writes (chunks, status) pass row-level security. Set inside the
        # coroutine: ``gather`` copies the context at task creation.
        token_tenant = current_tenant_id_var.set(claim.tenant_id)
        token_bypass = bypass_rls_var.set(False)
        lost = asyncio.Event()
        finishing = asyncio.Event()
        owner = asyncio.current_task()

        def stop() -> None:
            # B-222: the document was cancelled on this pod, or a lease renewal
            # was refused (cancelled / re-uploaded elsewhere) — stop as a lost
            # claim (nothing written). Once the paid work is over a refusal is
            # expected (READY refuses it): the drive finishes, tidy-up included.
            if finishing.is_set():
                return
            lost.set()
            if owner is not None:
                owner.cancel()

        self.local.add(claim.document_id, stop)
        renewer = asyncio.create_task(
            renew_lease_periodically(
                self._store,
                tenant_id=claim.tenant_id,
                document_id=claim.document_id,
                generation=claim.generation,
                lease_seconds=self._lease_seconds,
                on_lost=stop,
            )
        )
        try:
            return await self._drive(claim, finishing=finishing, lost=lost)
        except asyncio.CancelledError:
            if not lost.is_set():
                raise
            logger.info("knowledge.recovery_lost_claim document=%s", claim.document_id)
            return False
        finally:
            self.local.discard(claim.document_id, stop)
            renewer.cancel()
            current_tenant_id_var.reset(token_tenant)
            bypass_rls_var.reset(token_bypass)

    async def _drive(
        self, claim: ClaimedIngestion, *, finishing: asyncio.Event, lost: asyncio.Event
    ) -> bool:
        """``finishing`` is set once the paid work is over (whatever its
        outcome): from then on only fenced writes and the tidy-up follow.
        ``lost`` is set when the drive is stopped because the claim is no
        longer its own (any other cancellation is the pod shutting down)."""
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
        ready = written = settled_failure = retrying = interrupted = False
        #: B-222 PR-2 —— what the paid ingest was billed (none for an estimate).
        spend: IngestSpend | None = None
        try:
            try:
                if claim.phase == "estimate":
                    # B-222 PR-2: the free estimate; then it waits for its confirmation.
                    await estimate_document(
                        self._store, self.estimator, self._embedder, claim, figures=self._figures
                    )
                    return True
                start = await start_paid_ingest(
                    self._store, self.estimator, self._embedder, claim, figures=self._figures
                )
                if start.returned:
                    return True  # asked again: nothing spent
                spend = IngestSpend()
                with spend_scope(spend):
                    await ingest_document_bytes(
                        store=self._store,
                        embedder=start.embedder or self._embedder,
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
                        guard=lease_guard(
                            self._store,
                            tenant_id=claim.tenant_id,
                            document_id=claim.document_id,
                            generation=claim.generation,
                            lease_seconds=self._lease_seconds,
                        ),
                        local=start.local,
                        before_paid=start.before_paid,
                        settle=finishing.set,
                    )
            except asyncio.CancelledError:
                # Lost / cancelled here (``lost``), or the pod shutting down —
                # then the claim is retried later and keeps its snapshot.
                interrupted = not lost.is_set()
                raise
            finally:
                finishing.set()
            # This claim's chunks are in place and READY, in one transaction.
            written = ready = True
        except Exception as exc:
            if isinstance(exc, StaleIngestionError) and not isinstance(exc, ModelsCheckFailedError):
                # 重传 / 已被别的认领收尾 —— 本次驱动的结果作废,什么都不写。
                logger.info(
                    "knowledge.recovery_stale document=%s generation=%d",
                    claim.document_id,
                    claim.generation,
                )
                return False
            # B2b review: a models check that could not run is retried like
            # any failure (snapshot kept), with its own code on the last one.
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
            # Over the size cap is decided by the text itself: every retry would
            # re-parse the whole document only to be refused again.
            if claim.attempts >= self._max_attempts or isinstance(exc, DocumentTooLargeError):
                await self._store.mark_document_failed_terminal(
                    tenant_id=claim.tenant_id,
                    document_id=claim.document_id,
                    error=str(exc)[:_ERROR_CAP],
                    error_code=failure_code(exc),
                    generation=claim.generation,
                )
                settled_failure = True
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
            retrying = True
            logger.warning(
                "knowledge.recovery_retry document=%s attempts=%d",
                claim.document_id,
                claim.attempts,
                exc_info=True,
            )
            return False
        finally:
            if spend is not None:
                outcome: IngestOutcome = (
                    "ready" if ready else "failed" if settled_failure or retrying else "stopped"
                )
                # A retry adds to this attempt's spend (same generation).
                await record_actual(
                    self._store,
                    self.estimator,
                    claim,
                    spend,
                    outcome=outcome,
                    settled=not (retrying or interrupted),
                )
            if self._figures is not None and claim.phase == "ingest":
                # B-209 PR-2 (review H5): same tidy-up as the fast path.
                await self._figures.tidy(claim, settled=ready, serving=written)
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
