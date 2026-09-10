"""FeedbackConsumerWorker — Stream HX-2 (STREAM-HX-DESIGN § 3.2-③).

A single-replica background worker inside the control-plane (same
lifecycle shape as :class:`CurationWorker` / ``MemoryConsolidator``).
Each ``run_once`` sweep consumes unprocessed user 👎 rows from the
``feedback`` table and feeds the memory half of the learning loop:

1. **Enumerate** (cross-tenant): ``rating='down' AND processed_at IS
   NULL``, oldest first. ``feedback`` is FORCE-RLS, so the store read
   assumes the ``audit_reader`` BYPASSRLS role under a bypass scope
   (Mini-ADR HX-B1 — the ledger / audit precedent). The feedback table
   is the loss-free source: J.12's curation candidates miss late 👎
   (uniqueness pre-check) and require a trajectory to exist.
2. **Resolve the user** (per-tenant scope): ``thread_meta`` maps the
   thread to its owning ``user_id`` — the ``memory_item`` policy needs
   both GUC axes. A missing meta / pre-J.14 NULL user is a recorded
   no-op, not an error.
3. **Flag memories** (tenant+user scope): every live transient item
   with ``source_thread_id == thread_id`` gets ``review_flagged_at``
   stamped; the MemoryConsolidator's SUB-PASS 2a reviews them via the
   U-37 single-item path regardless of age (Mini-ADR HX-B3).
4. **Stamp** ``processed_at`` (tenant scope) — idempotent, replay-safe.

The skill half is pull-based and lives in the rollback gate (Mini-ADR
HX-B2); this worker deliberately does nothing skill-side. 👍 rows are
never consumed here — the positive path (J.12 golden curation + SE
distill) already exists. Per-row failures are logged and skipped (the
row stays unprocessed and is retried next sweep); the sweep itself
never dies.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from expert_work.common.observability import expert_work_counter
from expert_work.persistence.feedback_store import FeedbackRecord, FeedbackStore
from expert_work.persistence.memory.base import MemoryStore
from expert_work.persistence.rls import (
    bypass_rls_var,
    current_tenant_id_var,
    current_user_id_var,
)
from expert_work.persistence.thread_meta.base import ThreadMetaStore
from expert_work.protocol import AuditAction, AuditEntry, AuditResult
from expert_work.runtime.audit.logger import AuditLogger

logger = logging.getLogger(__name__)

_feedback_consumed_total = expert_work_counter(
    "expert_work_control_plane_feedback_consumed_total",
    "User 👎 rows consumed by the learning loop, by action taken (Stream HX-2).",
    ("action",),
)
_cycle_errors = expert_work_counter(
    "expert_work_control_plane_feedback_consumer_cycle_errors_total",
    "FeedbackConsumerWorker cycles that ended in a caught exception.",
)


@contextmanager
def _bypass_rls() -> Iterator[None]:
    """Bypass scope for the cross-tenant enumeration (reaper pattern)."""
    bypass = bypass_rls_var.set(True)
    tenant = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tenant)
        bypass_rls_var.reset(bypass)


@contextmanager
def _scope(tenant_id: UUID, user_id: UUID | None = None) -> Iterator[None]:
    """Per-row store-call scope — tenant axis always, user axis when known."""
    tenant = current_tenant_id_var.set(tenant_id)
    bypass = bypass_rls_var.set(False)
    user = current_user_id_var.set(user_id)
    try:
        yield
    finally:
        current_user_id_var.reset(user)
        bypass_rls_var.reset(bypass)
        current_tenant_id_var.reset(tenant)


@dataclass(frozen=True)
class FeedbackConsumeTally:
    """One sweep's outcome — returned by :meth:`run_once` for tests."""

    scanned: int = 0
    memory_flagged: int = 0
    noop: int = 0
    errors: int = 0


@dataclass
class FeedbackConsumerWorker:
    """Periodic consumer of unprocessed 👎 feedback (Stream HX-2)."""

    feedback_store: FeedbackStore
    thread_meta_store: ThreadMetaStore
    memory_store: MemoryStore
    audit_logger: AuditLogger | None = None
    interval_s: int = 600
    batch_size: int = 100

    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
                return
            except TimeoutError:
                pass
            try:
                await self.run_once()
            except Exception:
                logger.exception("feedback_consumer.cycle_failed")
                _cycle_errors.inc()

    async def run_once(self) -> FeedbackConsumeTally:
        """One sweep. Idempotent — processed rows carry the stamp."""
        # PR4 — 反馈来源(console/external)在这里**刻意不参与判断**:这里问的是
        # 「这条记忆是不是该复核」,员工与终端用户的 👎 同等有效(spec §6)。展示
        # 面要区分来源,判断面不区分 —— 不要在这里补一个 source 过滤。
        with _bypass_rls():
            rows = await self.feedback_store.list_unprocessed_down_all_tenants(
                limit=self.batch_size
            )
        scanned = flagged_rows = noop = errors = 0
        for row in rows:
            scanned += 1
            try:
                flagged = await self._consume_row(row)
            except Exception:
                # Row stays unprocessed → retried next sweep.
                logger.exception(
                    "feedback_consumer.row_failed feedback_id=%s tenant=%s",
                    row.id,
                    row.tenant_id,
                )
                errors += 1
                continue
            if flagged:
                flagged_rows += 1
                _feedback_consumed_total.labels(action="memory_flagged").inc()
            else:
                noop += 1
                _feedback_consumed_total.labels(action="noop").inc()
        return FeedbackConsumeTally(
            scanned=scanned, memory_flagged=flagged_rows, noop=noop, errors=errors
        )

    async def _consume_row(self, row: FeedbackRecord) -> int:
        """Process one 👎 row; returns the number of memories flagged."""
        now = datetime.now(UTC)
        with _scope(row.tenant_id):
            meta = await self.thread_meta_store.get(row.thread_id, tenant_id=row.tenant_id)
        flagged = 0
        if meta is not None and meta.user_id is not None:
            with _scope(row.tenant_id, meta.user_id):
                flagged = await self.memory_store.flag_for_review(
                    tenant_id=row.tenant_id,
                    user_id=meta.user_id,
                    source_thread_id=str(row.thread_id),
                )
        # Stamp last — a crash before this point leaves the row
        # unprocessed and the (idempotent) flags simply re-applied.
        with _scope(row.tenant_id):
            if row.id is not None:
                await self.feedback_store.mark_processed(feedback_id=row.id, processed_at=now)
        await self._audit(row, flagged)
        return flagged

    async def _audit(self, row: FeedbackRecord, flagged: int) -> None:
        if self.audit_logger is None:
            return
        await self.audit_logger.write(
            AuditEntry(
                tenant_id=row.tenant_id,
                actor_type="system",
                actor_id="feedback-consumer-worker",
                action=AuditAction.FEEDBACK_CONSUMED,
                resource_type="feedback",
                resource_id=str(row.id),
                result=AuditResult.SUCCESS,
                details={"memories_flagged": flagged},
            )
        )
