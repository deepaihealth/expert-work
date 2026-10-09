"""B-168 —— 长期记忆后台写回 worker。

``memory_writeback_mode == "background"`` 时每个控制面副本跑一个,随 lifespan 启停
(同 :class:`~control_plane.memory.dlq_worker.MemoryDLQWorker` /
:class:`~control_plane.run_queue_worker.RunQueueWorker` 的形状:``start`` / ``stop`` /
``run_once``,``asyncio.Event`` 停止标志)。

* **快路径**:本副本刚落了任务就 :meth:`MemoryWritebackWorker.wake`,不等轮询。
* **慢路径**:每 ``interval_s`` 轮询一次,接住别的副本落的、重启前没做完的、租约过期的任务。
* **并发**:一个副本同时最多处理 ``concurrency`` 条,每领到一条就起一个任务去做,有空位就
  接着领。同一用户串行由领取保证(见 ``expert_work.persistence.memory.writeback_job``),
  不同用户并行 —— 一个卡住的厂商只占一个空位,不会让整个副本停摆。
* 处理交给注入的处理器(生产是 :class:`~control_plane.memory.writeback_processor.
  MemoryWritebackProcessor`),在任务自己的租户 + 用户作用域里跑;处理中每
  ``renew_every_s`` 续一次租约。
* **持有与围栏**:单次执行上限 = ``lease_s - renew_every_s``(最后一次续租之后、租约到期
  之前收手),超时算一次失败。续租发现租约已丢(被别的副本接手、或行被清除)就取消正在
  做的处理。处理器在**每次**改记忆库之前问 ``still_held``(这条任务是否仍在本次领取手上),
  不在就整批放弃、回 ``discarded``。收尾的 ``finish`` / ``fail_attempt`` 同样带持有凭证
  (``attempts``),迟到的旧持有者改不了行。
* **至少一次(at-least-once)**:以上围栏缩小、但消不掉重复执行的窗口 —— 旧持有者在
  ``still_held`` 返回 True 之后、那次写入落库之前恰好丢了租约,新持有者会把这一轮再抽取、
  再写一遍。完全相同的内容由记忆库的内容哈希去重;改了措辞的同一件事可能写两条、去重合并
  的增删改可能做两次。为此不做跨进程的写入事务:窗口只有一次库写入那么长。
* **清除**(设计稿 §3.6):用户被清除 / 会话被删除时任务行被删,``still_held`` 随即为 False。
* **收尾**:处理器正常返回 → ``finish``(``done``,结果记在行上);处理器抛错 / 超时 →
  ``fail_attempt``(未到上限退回 ``pending``,到上限 ``failed``)。
* **关机**:先停止领新任务,再等手上这几条做完,最多 ``stop_timeout_s``;做不完就取消,
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
    """处理一条任务;``still_held()`` 在写记忆之前调用,``False`` 就放弃写入。"""

    def __call__(
        self, job: MemoryWritebackJob, *, still_held: Callable[[], Awaitable[bool]]
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
        concurrency: int = 4,
        stop_timeout_s: float = 300.0,
        renew_every_s: float | None = None,
    ) -> None:
        if interval_s <= 0 or lease_s <= 0 or max_attempts <= 0 or concurrency <= 0:
            msg = "interval_s, lease_s, max_attempts and concurrency must be positive"
            raise ValueError(msg)
        renew = renew_every_s if renew_every_s is not None else lease_s / 5
        if not 0 < renew < lease_s:
            msg = "renew_every_s must be positive and shorter than lease_s"
            raise ValueError(msg)
        self._store = store
        self._processor = processor
        self._interval_s = interval_s
        self._lease_s = lease_s
        self._max_attempts = max_attempts
        self._concurrency = concurrency
        self._stop_timeout_s = stop_timeout_s
        self._renew_every_s = renew
        #: 最后一次续租之后、租约到期之前收手 —— 不在别的副本可能接手的时候还在写。
        self._attempt_timeout_s = lease_s - renew
        self._task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()
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
        """Stop claiming, wait up to ``stop_timeout_s`` for the in-flight jobs,
        then cancel them (their leases expire and another replica takes over)."""
        if self._task is None:
            return
        self._stop.set()
        self._wake.set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._stop_timeout_s
        try:
            await asyncio.wait_for(self._task, timeout=self._stop_timeout_s)
        except (TimeoutError, asyncio.CancelledError):
            pass
        finally:
            self._task = None
        if self._inflight:
            _done, pending = await asyncio.wait(
                set(self._inflight), timeout=max(0.0, deadline - loop.time())
            )
            if pending:
                logger.warning(
                    "memory.writeback_worker.stop_abandoned_in_flight count=%d", len(pending)
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            # Cleared before the claim, so a wake() that lands while this cycle
            # runs is still seen by the wait below.
            self._wake.clear()
            if len(self._inflight) >= self._concurrency:
                await self._wait_for_slot()
                continue
            try:
                job = await self._claim()
            except Exception:
                _cycle_errors.inc()
                logger.exception("memory.writeback_worker.cycle_failed")
                job = None
            if job is not None:
                task = asyncio.create_task(self._run_claimed(job), name="memory-writeback-job")
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
                continue  # fill the free slots: there may be more ready jobs
            await self._sample_backlog()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue

    async def _wait_for_slot(self) -> None:
        stopping = asyncio.ensure_future(self._stop.wait())
        try:
            await asyncio.wait({*self._inflight, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)

    async def _sample_backlog(self) -> None:
        try:
            with _bypass_rls():
                _backlog.set(await self._store.count_backlog())
        except Exception:
            logger.warning("memory.writeback_worker.backlog_failed", exc_info=True)

    async def _claim(self) -> MemoryWritebackJob | None:
        with _bypass_rls():
            return await self._store.claim_next(
                now=_utcnow(), lease_s=self._lease_s, max_attempts=self._max_attempts
            )

    async def run_once(self) -> bool:
        """Claim and settle at most one job, inline. ``True`` iff a job was claimed."""
        job = await self._claim()
        if job is None:
            return False
        await self._settle(job)
        return True

    async def _run_claimed(self, job: MemoryWritebackJob) -> None:
        try:
            await self._settle(job)
        except Exception:
            # The claim stays behind; its lease expiry hands it to a later attempt.
            _cycle_errors.inc()
            logger.exception("memory.writeback_worker.job_failed job_id=%s", job.id)

    async def _settle(self, job: MemoryWritebackJob) -> None:
        queued_ms = _ms((job.started_at or job.created_at) - job.created_at)
        _queue_lag.observe(queued_ms / 1000)
        with _tenant_scope(job.tenant_id, job.user_id):
            await self._process(job, queued_ms=queued_ms)

    async def _process(self, job: MemoryWritebackJob, *, queued_ms: int) -> None:
        async def still_held() -> bool:
            return await self._store.still_held(job_id=job.id, attempt=job.attempts)

        started = time.monotonic()
        lease_lost = asyncio.Event()
        work = asyncio.ensure_future(self._processor(job, still_held=still_held))
        renewer = asyncio.create_task(
            self._renew(job, work, lease_lost), name="memory-writeback-renew"
        )
        try:
            outcome = await asyncio.wait_for(work, timeout=self._attempt_timeout_s)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if lease_lost.is_set() and (current is None or current.cancelling() == 0):
                # 续租发现租约已丢、取消了处理:行归新持有者(或已被清除),这里什么都不改。
                _jobs.labels(outcome="lost").inc()
                logger.warning("memory.writeback_worker.lease_lost_cancelled job_id=%s", job.id)
                return
            raise
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

    async def _renew(
        self, job: MemoryWritebackJob, work: asyncio.Future[WritebackOutcome], lost: asyncio.Event
    ) -> None:
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
                # 别的副本已接手(或行被清除):停下正在做的处理,别再写。
                lost.set()
                work.cancel()
                return


__all__ = ["MemoryWritebackWorker", "WritebackOutcome", "WritebackProcessor"]
