"""Approval timeout sweep — Stream 9.5 (J.8 human-in-the-loop completion).

A run that pauses for human approval sits in ``agent_approval`` with
``status='pending'`` until a verdict. Each approval carries a ``timeout_at``
deadline, but until now *nothing* enforced it — the ``ApprovalStore.list_expired``
primitive existed yet had no consumer, so a run whose reviewer never answered
stayed paused forever. This resident worker is that missing enforcer: it scans
for pending approvals past their deadline and auto-rejects them (``TIMEOUT``),
resuming the paused run through the exact same continuation path a human
``reject`` takes (:func:`control_plane.api.runs.resolve_approval_decision`).

Exactly-once across instances: the auto-reject goes through
``ApprovalStore.mark_decided``, a CAS on ``status='pending'``. When several
instances (or a racing human verdict) hit the same expired row, only one wins
the transition + spawns the continuation; the losers see the conflict and skip.
So this worker runs in *every* control-plane instance — a sibling of
:class:`OrphanSweep` / :class:`ReservationReaper` (same lifespan loop +
bypass-RLS scan + per-tenant resolve).

Cross-tenant scan runs under ``_bypass_rls`` (``agent_approval`` is ENABLE-only
RLS, so the owner exemption covers the read — no GUC emitted); each row's
resolve runs under ``_tenant_scope`` so the ``mark_decided`` write lands.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException

from control_plane.agent_disable_status import AgentDisableService
from control_plane.api.runs import resolve_approval_decision
from control_plane.runtime import AgentRuntime
from control_plane.tenant_status import TenantStatusService
from expert_work.common.observability import expert_work_counter
from expert_work.persistence import ApprovalStore, ThreadMetaStore
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.persistence.rls import (
    bypass_rls_var,
    current_tenant_id_var,
    current_user_id_var,
)
from expert_work.protocol import ApprovalStatus, approval_kind_class
from expert_work.runtime.audit.logger import AuditLogger

logger = logging.getLogger("expert_work.control_plane.approval_timeout_sweep")

#: Default cadence — one sweep per 5 minutes. Approvals time out on a 24h
#: horizon, so a few minutes of detection lag is immaterial.
_DEFAULT_INTERVAL_S = 300.0

_timed_out_total = expert_work_counter(
    "expert_work_control_plane_approval_timeouts_total",
    "Pending approvals auto-rejected by the timeout sweep (past timeout_at).",
)
_cycle_errors = expert_work_counter(
    "expert_work_control_plane_approval_timeout_cycle_errors_total",
    "Approval timeout sweep cycles that ended in a caught exception.",
)


@contextmanager
def _bypass_rls() -> Iterator[None]:
    """RLS-bypass scope for the cross-tenant expired-approval scan."""
    bypass = bypass_rls_var.set(True)
    tenant = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tenant)
        bypass_rls_var.reset(bypass)


@contextmanager
def _tenant_scope(tenant_id: UUID, user_id: UUID | None) -> Iterator[None]:
    """Scope a row's resolve (mark_decided + spawn) to its own tenant + user."""
    tenant = current_tenant_id_var.set(tenant_id)
    bypass = bypass_rls_var.set(False)
    user = current_user_id_var.set(user_id)
    try:
        yield
    finally:
        current_user_id_var.reset(user)
        bypass_rls_var.reset(bypass)
        current_tenant_id_var.reset(tenant)


class ApprovalTimeoutSweep:
    """Background task: auto-reject pending approvals past their deadline."""

    def __init__(
        self,
        *,
        approval_store: ApprovalStore,
        thread_store: ThreadMetaStore,
        agent_spec_store: AgentSpecStore,
        runtime: AgentRuntime,
        audit_logger: AuditLogger,
        interval_s: float = _DEFAULT_INTERVAL_S,
        batch_size: int = 100,
        agent_disable_service: AgentDisableService | None = None,
        tenant_status_service: TenantStatusService | None = None,
    ) -> None:
        if interval_s <= 0:
            msg = "interval_s must be positive"
            raise ValueError(msg)
        self._approvals = approval_store
        self._threads = thread_store
        self._agents = agent_spec_store
        self._runtime = runtime
        self._audit = audit_logger
        self._interval_s = interval_s
        self._batch_size = batch_size
        # Stream RT-4 (RT-ADR-16) — an auto-timeout still spawns a continuation;
        # a disabled agent / suspended tenant must not resume via the sweep either.
        self._agent_disable_service = agent_disable_service
        self._tenant_status_service = tenant_status_service
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the periodic loop. Idempotent."""
        if self.is_running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="approval-timeout-sweep")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=self._interval_s + 5)
        except (TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        finally:
            self._task = None

    async def _loop(self) -> None:
        # Sleep first (the platform likely just restarted); first sweep after
        # one interval. A failed cycle is logged + counted, never fatal.
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
                return  # stop event fired
            except TimeoutError:
                pass
            try:
                await self.run_once()
            except Exception:
                _cycle_errors.inc()
                logger.exception("approval_timeout_sweep.cycle_failed")

    async def run_once(self) -> int:
        """Auto-reject every expired pending approval once. Returns the count
        this instance actually transitioned (peers' wins are not counted)."""
        now = datetime.now(UTC)
        with _bypass_rls():
            expired = await self._approvals.list_expired(before=now, limit=self._batch_size)
        swept = 0
        for appr in expired:
            with _tenant_scope(appr.tenant_id, appr.user_id):
                won = await self._resolve_one(
                    appr.tenant_id,
                    appr.thread_id,
                    appr.run_id,
                    appr.user_id,
                    reason_kind=appr.reason_kind,
                )
                if won:
                    swept += 1
        return swept

    async def _resolve_one(
        self,
        tenant_id: UUID,
        thread_id: UUID,
        run_id: UUID,
        user_id: UUID | None,
        *,
        reason_kind: str = "policy_gate",
    ) -> bool:
        """Auto-reject one expired approval; ``True`` iff this call won the CAS."""
        # B-20 approval triage — a timed-out *clarification* (agent question,
        # non-terminal reject) tells the agent to continue on its own
        # conservative default; a safety row keeps the plain timeout reason
        # (terminal for the declarative gate).
        if approval_kind_class(reason_kind) == "clarification":
            timeout_reason = (
                "no human response before the clarification deadline — proceed "
                "with your own conservative default and state the assumptions you made"
            )
        else:
            timeout_reason = "approval timed out"
        try:
            run_record, _continuation, replayed = await resolve_approval_decision(
                tenant_id=tenant_id,
                actor_id="approval_timeout_sweep",
                caller_user_id=user_id,
                # Per-user OAuth MCP pool key — the run's owner.
                oauth_user_id=str(user_id) if user_id is not None else None,
                thread_id=thread_id,
                run_id=run_id,
                # An expired approval denies the gated tool call (graph reject);
                # the row records the distinct TIMEOUT status.
                graph_decision="reject",
                db_status=ApprovalStatus.TIMEOUT,
                modified_args=None,
                reason=timeout_reason,
                threads=self._threads,
                audit=self._audit,
                agent_repo=self._agents,
                runtime=self._runtime,
                approvals=self._approvals,
                agent_disable_service=self._agent_disable_service,
                tenant_status_service=self._tenant_status_service,
            )
        except HTTPException as exc:
            # 409 — a peer sweep / a human verdict won the CAS first (the normal
            # multi-instance loser path). 404 — the run / agent vanished. Either
            # way the row is no longer ours to time out; skip without counting.
            logger.info(
                "approval_timeout_sweep.skipped",
                extra={"run_id": str(run_id), "status_code": exc.status_code},
            )
            return False
        if run_record is None or replayed:
            return False
        _timed_out_total.inc()
        logger.info("approval_timeout_sweep.timed_out", extra={"run_id": str(run_id)})
        return True


__all__ = ["ApprovalTimeoutSweep"]
