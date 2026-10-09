"""B-168 —— ``MemoryWritebackWorker``:领取 → 处理 → 收尾,租约、重试、关机、清除。

处理器是注入的,这里用假处理器,不调模型。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from prometheus_client import REGISTRY

from control_plane.memory.writeback_worker import MemoryWritebackWorker, WritebackOutcome
from expert_work.persistence.memory import (
    InMemoryMemoryWritebackJobStore,
    MemoryWritebackJob,
)
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var, current_user_id_var

StillQueued = Callable[[], Awaitable[bool]]


async def _enqueue(store: InMemoryMemoryWritebackJobStore, *, user_id: UUID | None = None) -> UUID:
    return await store.enqueue(
        tenant_id=uuid4(),
        user_id=user_id or uuid4(),
        agent_name="agent-a",
        agent_version="1",
        thread_id=uuid4(),
        run_id=uuid4(),
        trace_id=None,
        now=datetime.now(UTC),
    )


def _only(store: InMemoryMemoryWritebackJobStore) -> MemoryWritebackJob:
    [job] = store._rows.values()
    return job


def _settled(outcome: str) -> float:
    value = REGISTRY.get_sample_value(
        "expert_work_control_plane_memory_writeback_jobs_total", {"outcome": outcome}
    )
    return value or 0.0


class _Recorder:
    """A processor that records what it saw and returns a fixed outcome."""

    def __init__(self, outcome: WritebackOutcome | None = None, *, delay_s: float = 0.0) -> None:
        self.outcome = outcome or WritebackOutcome(written_count=2, failed=False)
        self.delay_s = delay_s
        self.seen: list[MemoryWritebackJob] = []
        self.scope: list[tuple[object, object, bool]] = []

    async def __call__(
        self, job: MemoryWritebackJob, *, still_queued: StillQueued
    ) -> WritebackOutcome:
        self.seen.append(job)
        self.scope.append(
            (current_tenant_id_var.get(), current_user_id_var.get(), bypass_rls_var.get())
        )
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        return self.outcome


@pytest.mark.asyncio
async def test_run_once_processes_and_finishes_a_job() -> None:
    store = InMemoryMemoryWritebackJobStore()
    await _enqueue(store)
    processor = _Recorder(WritebackOutcome(written_count=3, failed=True))
    worker = MemoryWritebackWorker(store=store, processor=processor)

    assert await worker.run_once() is True

    job = _only(store)
    assert job.status == "done"
    assert (job.written_count, job.failed) == (3, True)
    assert job.queued_ms is not None and job.queued_ms >= 0
    assert job.exec_ms is not None and job.exec_ms >= 0
    # Per-row work runs scoped to the job's own tenant + user, not under bypass.
    assert processor.scope == [(job.tenant_id, job.user_id, False)]
    assert await worker.run_once() is False


@pytest.mark.asyncio
async def test_raising_processor_retries_then_fails() -> None:
    """(b) —— 处理器抛错:未到上限退回 pending,到上限 failed。"""
    store = InMemoryMemoryWritebackJobStore()
    await _enqueue(store)

    async def boom(job: MemoryWritebackJob, *, still_queued: StillQueued) -> WritebackOutcome:
        raise RuntimeError("provider down")

    worker = MemoryWritebackWorker(store=store, processor=boom, max_attempts=2)

    assert await worker.run_once() is True
    job = _only(store)
    assert (job.status, job.attempts) == ("pending", 1)
    assert job.last_error is not None and "provider down" in job.last_error

    assert await worker.run_once() is True
    job = _only(store)
    assert (job.status, job.attempts) == ("failed", 2)
    assert await worker.run_once() is False


@pytest.mark.asyncio
async def test_a_hung_processor_times_out_into_a_retry() -> None:
    store = InMemoryMemoryWritebackJobStore()
    await _enqueue(store)
    worker = MemoryWritebackWorker(
        store=store, processor=_Recorder(delay_s=5), lease_s=0.05, max_attempts=3
    )

    assert await worker.run_once() is True

    job = _only(store)
    assert job.status == "pending"
    assert job.last_error is not None and "TimeoutError" in job.last_error


@pytest.mark.asyncio
async def test_the_lease_is_renewed_while_processing() -> None:
    store = InMemoryMemoryWritebackJobStore()
    await _enqueue(store)
    leases: list[datetime | None] = []

    async def slow(job: MemoryWritebackJob, *, still_queued: StillQueued) -> WritebackOutcome:
        leases.append(_only(store).lease_until)
        await asyncio.sleep(0.2)
        leases.append(_only(store).lease_until)
        return WritebackOutcome(written_count=0, failed=False)

    worker = MemoryWritebackWorker(store=store, processor=slow, lease_s=30, renew_every_s=0.02)
    await worker.run_once()

    first, last = leases
    assert first is not None and last is not None
    assert last > first
    assert _only(store).status == "done"


@pytest.mark.asyncio
async def test_a_purged_job_is_discarded_not_finished() -> None:
    """(d) —— 执行中被清除:处理器问 ``still_queued`` 得到 False、放弃写入;worker 不把行
    写回来,也不算失败重试。"""
    store = InMemoryMemoryWritebackJobStore()
    await _enqueue(store)
    answers: list[bool] = []

    async def purged_mid_way(
        job: MemoryWritebackJob, *, still_queued: StillQueued
    ) -> WritebackOutcome:
        answers.append(await still_queued())
        await store.delete_all_for_user(tenant_id=job.tenant_id, user_id=job.user_id)
        answers.append(await still_queued())
        return WritebackOutcome(written_count=0, failed=False, discarded=True)

    worker = MemoryWritebackWorker(store=store, processor=purged_mid_way)
    before = _settled("discarded")
    assert await worker.run_once() is True

    assert answers == [True, False]
    assert store._rows == {}
    assert _settled("discarded") == before + 1


@pytest.mark.asyncio
async def test_wake_skips_the_poll_interval() -> None:
    store = InMemoryMemoryWritebackJobStore()
    processor = _Recorder()
    worker = MemoryWritebackWorker(store=store, processor=processor, interval_s=60)
    worker.start()
    try:
        await asyncio.sleep(0.05)  # the first (empty) cycle is now waiting out 60 s
        await _enqueue(store)
        worker.wake()
        for _ in range(100):
            if processor.seen:
                break
            await asyncio.sleep(0.01)
        assert len(processor.seen) == 1
    finally:
        await worker.stop()
    assert not worker.is_running


@pytest.mark.asyncio
async def test_stop_abandons_an_unfinished_job_to_lease_expiry() -> None:
    store = InMemoryMemoryWritebackJobStore()
    await _enqueue(store)
    started = asyncio.Event()

    async def hangs(job: MemoryWritebackJob, *, still_queued: StillQueued) -> WritebackOutcome:
        started.set()
        await asyncio.sleep(60)
        return WritebackOutcome(written_count=0, failed=False)

    worker = MemoryWritebackWorker(store=store, processor=hangs, stop_timeout_s=0.05)
    worker.start()
    await asyncio.wait_for(started.wait(), timeout=2)

    await asyncio.wait_for(worker.stop(), timeout=2)

    assert not worker.is_running
    job = _only(store)
    # Left as claimed: another replica picks it up once the lease runs out.
    assert job.status == "running"
    assert job.lease_until is not None


@pytest.mark.asyncio
async def test_stop_waits_for_the_in_flight_job_to_finish() -> None:
    store = InMemoryMemoryWritebackJobStore()
    await _enqueue(store)
    started = asyncio.Event()

    async def quick(job: MemoryWritebackJob, *, still_queued: StillQueued) -> WritebackOutcome:
        started.set()
        await asyncio.sleep(0.1)
        return WritebackOutcome(written_count=1, failed=False)

    worker = MemoryWritebackWorker(store=store, processor=quick, stop_timeout_s=5)
    worker.start()
    await asyncio.wait_for(started.wait(), timeout=2)
    await worker.stop()

    assert _only(store).status == "done"
