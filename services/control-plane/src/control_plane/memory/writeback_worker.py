"""B-168 —— 长期记忆后台写回 worker。

``memory_writeback_mode == "background"`` 时每个控制面副本跑一个,随 lifespan 启停
(同 :class:`~control_plane.memory.dlq_worker.MemoryDLQWorker` /
:class:`~control_plane.run_queue_worker.RunQueueWorker` 的形状:``start`` / ``stop`` /
``run_once``,``asyncio.Event`` 停止标志)。

* **快路径**:本副本刚落了任务就 :meth:`MemoryWritebackWorker.wake`,不等轮询。
* **慢路径**:每 ``interval_s`` 轮询一次,接住别的副本落的、重启前没做完的、租约过期的任务。
* 一次领一条(``claim_next``,跨租户扫描在 bypass-RLS 下做),领到就接着领,领不到才等。
  同一用户串行由领取条件保证(见 ``expert_work.persistence.memory.writeback_job``)。
* 处理交给注入的处理器(生产是 :class:`~control_plane.memory.writeback_processor.
  MemoryWritebackProcessor`),在任务自己的租户 + 用户作用域里跑;处理中每
  ``renew_every_s`` 续一次租约。单条执行上限 = 租约时长,超时算一次失败。
* **清除**(设计稿 §3.6):处理器拿到 ``still_queued``,在写记忆之前问一次任务行还在不在,
  不在就放弃写入、回 ``discarded``。收尾的 ``finish`` 本身带持有凭证(``attempts``)+
  ``status = 'running'`` 条件:行被清除或被别的副本重新领走时它不生效,不会把行写回来,
  所以 worker 不在 ``finish`` 前另查一次。
* **收尾**:处理器正常返回 → ``finish``(``done``,结果记在行上);处理器抛错 / 超时 →
  ``fail_attempt``(未到上限退回 ``pending``,到上限 ``failed``)。
* **关机**:先停止领新任务,再等手上这条做完,最多 ``stop_timeout_s``;做不完就放着,
  租约过期后别的副本接手。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from expert_work.common.observability import (
    expert_work_counter,
    expert_work_gauge,
    expert_work_histogram,
)
from expert_work.persistence.memory import MemoryWritebackJob, MemoryWritebackJobStore
from expert_work.persistence.rls import (
    bypass_rls_var,
    current_tenant_id_var,
    current_user_id_var,
)

logger = logging.getLogger("expert_work.control_plane.memory.writeback_worker")

_cycle_errors = expert_work_counter(
    "expert_work_control_plane_memory_writeback_cycle_errors_total",
    "Memory writeback worker cycles that ended in a caught exception.",
)
_jobs = expert_work_counter(
    "expert_work_control_plane_memory_writeback_jobs_total",
    "Background memory writeback jobs settled, by outcome "
    "(done / done_failed / retry / failed / discarded / lost).",
    ("outcome",),
)
_queue_lag = expert_work_histogram(
    "expert_work_control_plane_memory_writeback_queue_lag_seconds",
    "Time from enqueue (run end) to claim for background memory writeback jobs.",
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 300, 1800),
)
_exec_duration = expert_work_histogram(
    "expert_work_control_plane_memory_writeback_exec_duration_seconds",
    "Processing time of one background memory writeback attempt.",
    buckets=(0.5, 1, 2, 5, 10, 20, 40, 80, 160, 300),
)
_backlog = expert_work_gauge(
    "expert_work_control_plane_memory_writeback_backlog",
    "Pending + running background memory writeback jobs (all tenants), "
    "sampled by each replica's worker when idle.",
)


@dataclass(frozen=True)
class WritebackOutcome:
    """处理器跑完一条任务的结果。

    ``failed`` 与 PR A 的 ``memory_writeback_failed`` 同义:出错 / 被拦截没写成,区别于
    「这轮没什么可记」(两者 ``written_count`` 都是 0)。``discarded``:任务行在写记忆之前
    已经没了(用户被清除 / 会话被删除),什么都没写。``note`` 记在行的 ``last_error`` 上,
    说明为什么没做(如 Agent 已删除 / 已关长期记忆)。
    """

    written_count: int
    failed: bool
    discarded: bool = False
    note: str | None = None


class WritebackProcessor(Protocol):
    """处理一条任务;``still_queued()`` 在写记忆之前调用,``False`` 就放弃写入。"""

    def __call__(
        self, job: MemoryWritebackJob, *, still_queued: Callable[[], Awaitable[bool]]
    ) -> Awaitable[WritebackOutcome]: ...


@contextmanager
def _bypass_rls() -> Iterator[None]:
    """RLS-bypass scope for the cross-tenant claim scan (reaper pattern)."""
    bypass = bypass_rls_var.set(True)
    tenant = current_tenant_id_var.set(None)
    try:
        yield
    finally:
        current_tenant_id_var.reset(tenant)
        bypass_rls_var.reset(bypass)


@contextmanager
def _tenant_scope(tenant_id: UUID, user_id: UUID) -> Iterator[None]:
    """Scope one job's work to its own tenant + user — ``memory_item`` is
    FORCE-RLS on both axes (same as ``dlq_worker._tenant_scope``)."""
    tenant = current_tenant_id_var.set(tenant_id)
    bypass = bypass_rls_var.set(False)
    user = current_user_id_var.set(user_id)
    try:
        yield
    finally:
        current_user_id_var.reset(user)
        bypass_rls_var.reset(bypass)
        current_tenant_id_var.reset(tenant)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ms(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds() * 1000))


class MemoryWritebackWorker:
    """Background task: claim → process → settle background memory writebacks."""

    def __init__(
        self,
        *,
        store: MemoryWritebackJobStore,
        processor: WritebackProcessor,
        interval_s: float = 2.0,
        lease_s: float = 300.0,
        max_attempts: int = 3,
        stop_timeout_s: float = 300.0,
        renew_every_s: float | None = None,
    ) -> None:
        if interval_s <= 0 or lease_s <= 0 or max_attempts <= 0:
            msg = "interval_s, lease_s and max_attempts must be positive"
            raise ValueError(msg)
        self._store = store
        self._processor = processor
        self._interval_s = interval_s
        self._lease_s = lease_s
        self._max_attempts = max_attempts
        self._stop_timeout_s = stop_timeout_s
        self._renew_every_s = renew_every_s if renew_every_s is not None else lease_s / 5
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Schedule the loop. Idempotent."""
        if self.is_running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="memory-writeback-worker")

    def wake(self) -> None:
        """Fast path: a job was just enqueued on this replica — claim now."""
        self._wake.set()

    async def stop(self) -> None:
        """Stop claiming, wait up to ``stop_timeout_s`` for the in-flight job,
        then cancel it (its lease expires and another replica takes over)."""
        if self._task is None:
            return
        self._stop.set()
        self._wake.set()
        try:
            await asyncio.wait_for(self._task, timeout=self._stop_timeout_s)
        except (TimeoutError, asyncio.CancelledError):
            logger.warning("memory.writeback_worker.stop_abandoned_in_flight")
        finally:
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            # Cleared before the claim, so a wake() that lands while this cycle
            # runs is still seen by the wait below.
            self._wake.clear()
            try:
                claimed = await self.run_once()
            except Exception:
                _cycle_errors.inc()
                logger.exception("memory.writeback_worker.cycle_failed")
                claimed = False
            if claimed:
                continue  # drain: there may be more ready jobs
            await self._sample_backlog()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue

    async def _sample_backlog(self) -> None:
        try:
            with _bypass_rls():
                _backlog.set(await self._store.count_backlog())
        except Exception:
            logger.warning("memory.writeback_worker.backlog_failed", exc_info=True)

    async def run_once(self) -> bool:
        """Claim and settle at most one job. ``True`` iff a job was claimed."""
        with _bypass_rls():
            job = await self._store.claim_next(
                now=_utcnow(), lease_s=self._lease_s, max_attempts=self._max_attempts
            )
        if job is None:
            return False
        queued_ms = _ms((job.started_at or job.created_at) - job.created_at)
        _queue_lag.observe(queued_ms / 1000)
        with _tenant_scope(job.tenant_id, job.user_id):
            await self._process(job, queued_ms=queued_ms)
        return True

    async def _process(self, job: MemoryWritebackJob, *, queued_ms: int) -> None:
        async def still_queued() -> bool:
            return await self._store.exists(job_id=job.id)

        started = time.monotonic()
        renewer = asyncio.create_task(self._renew(job), name="memory-writeback-renew")
        try:
            outcome = await asyncio.wait_for(
                self._processor(job, still_queued=still_queued), timeout=self._lease_s
            )
        except Exception as exc:
            exec_ms = int((time.monotonic() - started) * 1000)
            _exec_duration.observe(exec_ms / 1000)
            await self._fail(job, exc)
            return
        finally:
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
        exec_ms = int((time.monotonic() - started) * 1000)
        _exec_duration.observe(exec_ms / 1000)
        if outcome.discarded:
            _jobs.labels(outcome="discarded").inc()
            logger.info("memory.writeback_worker.discarded job_id=%s", job.id)
            return
        settled = await self._store.finish(
            job_id=job.id,
            attempt=job.attempts,
            written_count=outcome.written_count,
            failed=outcome.failed,
            queued_ms=queued_ms,
            exec_ms=exec_ms,
            error=outcome.note,
            now=_utcnow(),
        )
        if not settled:
            # 行没了(清除)或租约过期被别的副本接手 —— 收尾不生效,结果作废。
            _jobs.labels(outcome="lost").inc()
            logger.warning("memory.writeback_worker.finish_lost job_id=%s", job.id)
            return
        _jobs.labels(outcome="done_failed" if outcome.failed else "done").inc()
        logger.info(
            "memory.writeback_worker.done job_id=%s written=%d failed=%s queued_ms=%d exec_ms=%d",
            job.id,
            outcome.written_count,
            outcome.failed,
            queued_ms,
            exec_ms,
        )

    async def _fail(self, job: MemoryWritebackJob, exc: BaseException) -> None:
        error = f"{type(exc).__name__}: {exc}"[:2000]
        status = await self._store.fail_attempt(
            job_id=job.id,
            attempt=job.attempts,
            error=error,
            max_attempts=self._max_attempts,
            now=_utcnow(),
        )
        outcome = {"pending": "retry", "failed": "failed"}.get(status or "", "lost")
        _jobs.labels(outcome=outcome).inc()
        logger.warning(
            "memory.writeback_worker.attempt_failed job_id=%s attempt=%d outcome=%s error=%s",
            job.id,
            job.attempts,
            outcome,
            type(exc).__name__,
        )

    async def _renew(self, job: MemoryWritebackJob) -> None:
        while True:
            await asyncio.sleep(self._renew_every_s)
            try:
                held = await self._store.renew_lease(
                    job_id=job.id,
                    attempt=job.attempts,
                    lease_until=_utcnow() + timedelta(seconds=self._lease_s),
                )
            except Exception:
                logger.warning("memory.writeback_worker.renew_failed job_id=%s", job.id)
                continue
            if not held:
                logger.warning("memory.writeback_worker.lease_lost job_id=%s", job.id)
                return


__all__ = ["MemoryWritebackWorker", "WritebackOutcome", "WritebackProcessor"]
