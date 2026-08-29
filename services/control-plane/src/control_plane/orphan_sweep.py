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
from control_plane.run_trace import bind_exec_trace
from control_plane.runtime import AgentRuntime
from control_plane.tenant_status import TenantStatusService
from expert_work.common.observability import (
    ExpertWorkComponent,
    current_trace_id_hex,
    expert_work_counter,
    expert_work_span,
)
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.persistence.rls import (
    bypass_rls_var,
    current_tenant_id_var,
    current_user_id_var,
)
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.protocol import AuditAction, AuditResult
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.runs import RunInfo, RunStore
from orchestrator import AgentFactoryError, run_agent

logger = logging.getLogger("expert_work.control_plane.orphan_sweep")

#: ``stop()`` 等待当前这一轮 sweep 收尾的上限,超时就取消。刻意不写成别处
#: 那种 ``interval + 5``:上界该由关机预算定,不是从轮询间隔派生 —— 同批
#: 几个 worker 的 interval 是分钟级,那个式子给出的「上界」比 K8s 默认 30s
#: 优雅期还长,等于没有上界。统一 5 秒:一轮正常 sweep 足够收尾,收不了尾
#: 就取消 —— 与 reap/approval/egress 三个 sweep 不同,这里的 ``_task`` 只是
#: 轮询循环:``_respawn`` 把实际的 agent run 铸成独立的 ``asyncio.create_task``
#: 就返回,不 await 它,所以 5 秒超时最多打断的是"reclaim 到 spawn 之间"那个
#: 窗口,而不是正在跑的 agent run 本身。这个模块自己就是"真正兜底"的那一层
#: (模块 docstring 开头);它自己的 ``stop()`` 超时不能靠"下次启动重来"
#: 兜底——这里被强制 cancel 打断,行为上与整个进程被 SIGKILL 时留下的
#: abandoned-claim 状态等价(下一次 sweep 循环 —— 自己这个实例或另一个副本
#: 的 —— 靠租约过期后重新发现并从 checkpoint 接管),只是回收时机更早。
_STOP_TIMEOUT_S = 5.0

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

#: W1-PR3 Task 1 — how long a run may sit PENDING before the sweep treats it
#: as stuck. The synchronous SSE path's create→RUNNING transition is a
#: milliseconds-scale in-process step (``RunStatus.PENDING`` docstring); a
#: row still PENDING past this many seconds means its owning replica
#: crashed inside that window before ever stamping a lease. 600s is far
#: past any normal window, so this never fires on a healthy run.
_PENDING_STALE_S = 600


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
            try:
                await asyncio.wait_for(self._task, timeout=_STOP_TIMEOUT_S)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            finally:
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

        # W1-PR3 Task 1 — a run whose owner crashed in the create→RUNNING
        # window (before ever stamping a lease) never shows up in the
        # ``list_orphans`` scan above (running + expired lease only). It's
        # otherwise stuck PENDING forever — and blocks its thread's external
        # plan writes forever too (``_WRITE_BLOCKED_STATUSES``). The run
        # never started, so there is nothing to resume: fail it straight
        # through the existing reused CAS guard, same as the conservative
        # orphan path.
        cutoff = now - timedelta(seconds=_PENDING_STALE_S)
        with _bypass_rls():
            stale_pending = await self._runs.list_stale_pending(
                cutoff=cutoff, limit=self._batch_size
            )
        for pending in stale_pending:
            try:
                await self._fail_orphan(pending, now=now, reason="stale_pending")
                handled += 1
            except Exception:
                logger.exception(
                    "orphan_sweep.handle_failed", extra={"run_id": str(pending.run_id)}
                )
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
            won = await self._runs.fail_if_active(
                run_id=orphan.run_id,
                tenant_id=orphan.tenant_id,
                error=f"orphaned run failover: {reason}",
                now=now,
            )
        if not won:
            # A peer replica's sweep already failed (or otherwise terminated)
            # this orphan — the CAS guard makes this a no-op, not a re-failure.
            logger.debug(
                "orphan_sweep.fail_orphan_lost_cas run_id=%s reason=%s", orphan.run_id, reason
            )
            return
        _failed_total.labels(reason=reason).inc()
        logger.warning("orphan_sweep.failed run_id=%s reason=%s", orphan.run_id, reason)
        await self._emit_audit(orphan, result=AuditResult.ERROR, reason=reason)

    async def _respawn(self, orphan: RunInfo) -> None:
        """Re-spawn a reclaimed run, resuming from its durable checkpoint.

        续跑段整个包在一个 ``expert_work.control_plane.reclaimed_run`` span 里,
        并把它的 trace 回写到 ``agent_run.trace_id`` —— 见
        :mod:`control_plane.run_trace` 为什么非做不可。``asyncio.create_task``
        复制当前 OTel context,所以 ``run_agent`` 里的
        ``expert_work.session.run`` 根 span 挂在这个 span 下面,续跑段的
        ``token_usage`` 和这一行同 trace。

        和 :class:`RunQueueWorker` 那条的差别:sweep 刻意**不 await** 派出去的
        任务(轮询循环要立刻回去扫下一个),所以这个 span 会在 run 结束之前就
        闭合,Tempo 瀑布图上父 span 比子 span 先结束。trace id 不受影响 ——
        我们要的就是它。
        """
        with (
            expert_work_span(
                ExpertWorkComponent.CONTROL_PLANE,
                "reclaimed_run",
                attributes={
                    "run_id": str(orphan.run_id),
                    "thread_id": str(orphan.thread_id),
                },
            ),
            _tenant_scope(orphan.tenant_id, orphan.user_id),
        ):
            await bind_exec_trace(
                runs=self._runs,
                run_id=orphan.run_id,
                tenant_id=orphan.tenant_id,
                known_trace_id=orphan.trace_id,
                exec_trace_id=current_trace_id_hex(),
                source="orphan_sweep",
            )

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
                # W1-PR2 Task 5 — CAS-guarded terminal transition, same
                # rationale as ``_fail_orphan``'s ``fail_if_active`` guard:
                # two replicas can both reclaim-and-resume the same orphan
                # in the same sweep cycle and both observe ``blocked``. An
                # unconditional ``set_status`` would let both "succeed",
                # double-counting the failed-orphan counter and emitting a
                # duplicate audit record for one terminal transition.
                won = await self._runs.request_cancel(
                    run_id=orphan.run_id,
                    tenant_id=orphan.tenant_id,
                    updated_at=now,
                    # kill_switch 的 blocked 值与 InterruptReason 同词表
                    # (tenant_suspended / agent_disabled),原样透传。
                    reason=blocked,
                )
                if not won:
                    # A peer replica's kill-switch branch (or some other
                    # terminal transition) already won — no-op, skip the
                    # counter/audit side effects.
                    logger.debug(
                        "orphan_sweep.kill_switch_lost_cas run_id=%s reason=%s",
                        orphan.run_id,
                        blocked,
                    )
                    return
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
                    # P2 块 2 — run 终局重算 thread_meta.message_count。
                    thread_stats_recorder=self._runtime.thread_stats_recorder,
                    token_budget=built.token_budget,
                    worker_spawn_budget=await self._runtime.new_worker_spawn_budget(
                        requested_max_concurrent=built.worker_max_concurrent,
                        requested_max_per_run=built.worker_max_per_run,
                    ),
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
