"""Stream 9.4 (HA failover) — orphaned-run sweep + automatic hot-handoff.

A run executes as an in-process ``asyncio.Task`` in one control-plane instance.
If that instance crashes mid-run the durable checkpoint + ``agent_run`` row
survive, but the live task evaporates — the run is stranded at ``status=running``
forever. This sweep is the recovery orchestration: it periodically scans for
running runs whose ownership lease expired (the owner stopped heartbeating =
crashed), and either

* **auto hot-handoff** (default): reclaims the run on this instance and
  re-spawns ``run_agent(graph_input=None)`` so it resumes from its durable
  LangGraph checkpoint — the run continues where it left off. Idempotency is
  inherited from the checkpoint (already-committed super-steps are not redone,
  same as the Stream HX-3 transient-retry path); a per-run reclaim cap stops a
  run that crashes its owner *every* time (OOM / segfault) from respawning
  forever, marking it errored past the cap; or
* **conservative** (``auto_reclaim=False``): marks the orphan errored so a
  human / client sees the failure (no automatic continuation).

Single mechanism, every instance runs it: the reclaim CAS
(:meth:`RunStore.reclaim`) serialises competing sweepers so exactly one takes
over each orphan. Structurally a sibling of :class:`TriggerScheduler` — same
in-process lifespan loop + bypass-RLS cross-tenant scan + per-tenant spawn.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig

from control_plane.agent_disable_status import AgentDisableService
from control_plane.audit import emit
from control_plane.kill_switch import run_block_reason
from control_plane.runtime import AgentRuntime
from control_plane.tenant_status import TenantStatusService
from expert_work.common.observability import current_trace_id_hex, expert_work_counter
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.persistence.rls import (
    bypass_rls_var,
    current_tenant_id_var,
    current_user_id_var,
)
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.protocol import AuditAction, AuditResult
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.runs import RunInfo, RunStatus, RunStore
from orchestrator import AgentFactoryError, run_agent

logger = logging.getLogger("expert_work.control_plane.orphan_sweep")

_reclaimed_total = expert_work_counter(
    "expert_work_run_orphan_reclaimed_total",
    "Orphaned runs the failover sweep reclaimed + resumed from checkpoint.",
)
_failed_total = expert_work_counter(
    "expert_work_run_orphan_failed_total",
    "Orphaned runs the failover sweep marked errored, by reason.",
    ("reason",),
)

_DEFAULT_MAX_RECLAIMS = 3


@contextmanager
def _bypass_rls() -> Iterator[None]:
    """RLS-bypass scope for the cross-tenant orphan scan (reaper pattern)."""
    bypass = bypass_rls_var.set(True)
    tenant = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tenant)
        bypass_rls_var.reset(bypass)


@contextmanager
def _tenant_scope(tenant_id: UUID, user_id: UUID | None = None) -> Iterator[None]:
    """Scope per-orphan work to the run's own tenant (+ user)."""
    tenant = current_tenant_id_var.set(tenant_id)
    bypass = bypass_rls_var.set(False)
    user = current_user_id_var.set(user_id)
    try:
        yield
    finally:
        current_user_id_var.reset(user)
        bypass_rls_var.reset(bypass)
        current_tenant_id_var.reset(tenant)


class OrphanSweep:
    """In-process lifespan loop that recovers orphaned (crashed-owner) runs."""

    def __init__(
        self,
        *,
        run_store: RunStore,
        thread_store: ThreadMetaStore,
        agent_spec_store: AgentSpecStore,
        runtime: AgentRuntime,
        audit_logger: AuditLogger,
        approval_store: Any,
        interval_s: float = 15.0,
        batch_size: int = 20,
        max_reclaims: int = _DEFAULT_MAX_RECLAIMS,
        auto_reclaim: bool = True,
        # Stream RT-4 (RT-ADR-16) — kill-switch gate for orphan respawn.
        agent_disable_service: AgentDisableService | None = None,
        tenant_status_service: TenantStatusService | None = None,
    ) -> None:
        self._runs = run_store
        self._threads = thread_store
        self._agents = agent_spec_store
        self._runtime = runtime
        self._audit = audit_logger
        self._approvals = approval_store
        self._interval_s = interval_s
        self._batch_size = batch_size
        self._max_reclaims = max_reclaims
        self._auto_reclaim = auto_reclaim
        self._agent_disable = agent_disable_service
        self._tenant_status = tenant_status_service
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="orphan-sweep")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("orphan_sweep.cycle_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except TimeoutError:
                # Interval elapsed with no stop signal — fall through to the next
                # sweep cycle. (A real stop wakes the wait early and exits the loop.)
                continue

    async def run_once(self) -> int:
        """Scan + handle one batch of orphans. Returns how many were handled."""
        now = datetime.now(UTC)
        with _bypass_rls():
            orphans = await self._runs.list_orphans(now=now, limit=self._batch_size)
        handled = 0
        for orphan in orphans:
            try:
                if await self._handle_orphan(orphan, now=now):
                    handled += 1
            except Exception:
                logger.exception("orphan_sweep.handle_failed", extra={"run_id": str(orphan.run_id)})
        return handled

    async def _handle_orphan(self, orphan: RunInfo, *, now: datetime) -> bool:
        # Conservative path: auto-reclaim off, or the run already burned its
        # reclaim budget (it crashes its owner every time) → mark it errored.
        if not self._auto_reclaim or orphan.reclaim_count >= self._max_reclaims:
            reason = "max_reclaims" if self._auto_reclaim else "auto_reclaim_off"
            await self._fail_orphan(orphan, now=now, reason=reason)
            return True

        new_lease = now + timedelta(seconds=self._runtime.run_manager.lease_ttl_s)
        with _bypass_rls():
            won = await self._runs.reclaim(
                run_id=orphan.run_id,
                new_owner=self._runtime.run_manager.instance_id,
                lease_until=new_lease,
                heartbeat_at=now,
                now=now,
            )
        if not won:
            # A peer reclaimed it first (or the owner's heartbeat returned) —
            # the reclaim CAS guarantees exactly one winner.
            return False
        await self._respawn(orphan)
        return True

    async def _fail_orphan(self, orphan: RunInfo, *, now: datetime, reason: str) -> None:
        with _tenant_scope(orphan.tenant_id):
            await self._runs.set_status(
                run_id=orphan.run_id,
                tenant_id=orphan.tenant_id,
                status=RunStatus.ERROR,
                updated_at=now,
                error=f"orphaned run failover: {reason}",
                finished_at=now,
            )
        _failed_total.labels(reason=reason).inc()
        logger.warning("orphan_sweep.failed run_id=%s reason=%s", orphan.run_id, reason)
        await self._emit_audit(orphan, result=AuditResult.ERROR, reason=reason)

    async def _respawn(self, orphan: RunInfo) -> None:
        """Re-spawn a reclaimed run, resuming from its durable checkpoint."""
        with _tenant_scope(orphan.tenant_id, orphan.user_id):
            meta = await self._threads.get(orphan.thread_id, tenant_id=orphan.tenant_id)
            if meta is None or meta.agent_name is None or meta.agent_version is None:
                await self._fail_orphan(orphan, now=datetime.now(UTC), reason="no_agent")
                return
            # Stream RT-4 (RT-ADR-16) — a reclaimed run for a disabled agent /
            # suspended tenant must not resume. Terminate it (INTERRUPTED, the
            # kill-switch terminal state) rather than re-run an emergency-stopped
            # agent — and so it stops looping through the sweep as a fresh orphan.
            blocked = await run_block_reason(
                tenant_status=self._tenant_status,
                agent_disable=self._agent_disable,
                tenant_id=orphan.tenant_id,
                agent_name=meta.agent_name,
            )
            if blocked is not None:
                now = datetime.now(UTC)
                await self._runs.set_status(
                    run_id=orphan.run_id,
                    tenant_id=orphan.tenant_id,
                    status=RunStatus.INTERRUPTED,
                    updated_at=now,
                    finished_at=now,
                )
                _failed_total.labels(reason=blocked).inc()
                logger.warning(
                    "orphan_sweep.kill_switch run_id=%s reason=%s", orphan.run_id, blocked
                )
                await self._emit_audit(orphan, result=AuditResult.DENIED, reason=blocked)
                return
            record = await self._agents.get(
                tenant_id=orphan.tenant_id, name=meta.agent_name, version=meta.agent_version
            )
            if record is None:
                await self._fail_orphan(orphan, now=datetime.now(UTC), reason="agent_gone")
                return
            try:
                built = await self._runtime.get_agent(
                    tenant_id=orphan.tenant_id,
                    name=meta.agent_name,
                    version=meta.agent_version,
                    spec=record.spec,
                    user_id=str(orphan.user_id) if orphan.user_id is not None else None,
                )
            except AgentFactoryError:
                await self._fail_orphan(orphan, now=datetime.now(UTC), reason="unbuildable")
                return

            # Adopt the existing durable run into THIS instance's registry (no
            # new agent_run row — the reclaim CAS already took ownership).
            run_record = await self._runtime.run_manager.adopt(
                run_id=orphan.run_id,
                thread_id=orphan.thread_id,
                tenant_id=orphan.tenant_id,
                user_id=orphan.user_id,
            )
            run_record.bound_distilled_skills = built.bound_distilled_skills

            configurable: dict[str, Any] = {
                "thread_id": str(orphan.thread_id),
                "tenant_id": str(orphan.tenant_id),
                "run_id": str(orphan.run_id),
            }
            if orphan.user_id is not None:
                configurable["user_id"] = str(orphan.user_id)
            if built.run_deadline_s > 0:
                configurable["deadline_at"] = time.monotonic() + float(built.run_deadline_s)
            config: RunnableConfig = {"configurable": configurable}

            worker = asyncio.create_task(
                run_agent(
                    bridge=self._runtime.stream_bridge,
                    run_manager=self._runtime.run_manager,
                    record=run_record,
                    graph=built.graph,  # type: ignore[arg-type]
                    graph_input=None,  # resume from the durable checkpoint
                    config=config,
                    audit_logger=self._audit,
                    approval_store=self._approvals,
                    event_store=self._runtime.run_event_store,
                    skill_run_usage_recorder=self._runtime.skill_run_usage_recorder,
                    trajectory_recorder=self._runtime.trajectory_recorder,
                    trajectory_enabled=built.trajectory_recording,
                    token_budget=built.token_budget,
                    worker_spawn_budget=await self._runtime.new_worker_spawn_budget(),
                    # perf phase2 PR3 T3 — process-wide delegation concurrency gate.
                    delegation_gate=self._runtime.delegation_gate(),
                    tool_replay_safe=built.tool_replay_safe,
                )
            )
            await self._runtime.run_manager.attach_task(orphan.run_id, worker)

        _reclaimed_total.inc()
        logger.info(
            "orphan_sweep.reclaimed run_id=%s by=%s attempt=%d",
            orphan.run_id,
            self._runtime.run_manager.instance_id,
            orphan.reclaim_count + 1,
        )
        await self._emit_audit(orphan, result=AuditResult.SUCCESS, reason="reclaimed")

    async def _emit_audit(self, orphan: RunInfo, *, result: AuditResult, reason: str) -> None:
        try:
            await emit(
                self._audit,
                tenant_id=orphan.tenant_id,
                actor_id="system",
                action=AuditAction.RUN_FAILOVER,
                resource_type="run",
                resource_id=str(orphan.run_id),
                result=result,
                reason=reason,
                trace_id=current_trace_id_hex(),
                details={
                    "thread_id": str(orphan.thread_id),
                    "reclaim_count": orphan.reclaim_count,
                    "instance": self._runtime.run_manager.instance_id,
                },
            )
        except Exception:
            logger.exception("orphan_sweep.audit_failed run_id=%s", orphan.run_id)


__all__ = ["OrphanSweep"]
