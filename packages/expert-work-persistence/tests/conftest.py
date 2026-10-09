"""Shared fixtures for the ``expert-work-persistence`` test suite.

B-168 —— ``memory_writeback_job`` 的 **store 行为契约**:同一条语义的断言只写一份,
内存实现与 SQL 实现各自把自己的 store 喂进来(同 ``expert-work-runtime/tests/conftest.py``
的做法)。本仓库有过「SQL 与内存 store 谓词分歧」的命门级教训 —— 两边各写各的断言时,
内存版在单测里全绿、SQL 版在真库上给出另一种结果,没有任何测试会红。契约住在
conftest 而不是单独模块,是因为 ``--import-mode=importlib`` 不把测试目录放进
``sys.path``,同目录的两个测试文件 import 不到彼此的兄弟模块。

``claim_next`` 是跨租户的全局扫描,所以每个场景都假定 store 里只有它自己造的行
(内存版每条测试新建 store,SQL 版每条测试先清表)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from expert_work.persistence.memory.writeback_job import (
    MemoryWritebackJob,
    MemoryWritebackJobStore,
)

_T0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
_LEASE_S = 300
_MAX = 3


async def _enqueue(
    store: MemoryWritebackJobStore,
    *,
    tenant_id: UUID,
    user_id: UUID,
    at: datetime,
    thread_id: UUID | None = None,
    run_id: UUID | None = None,
) -> UUID:
    return await store.enqueue(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_name="agent-a",
        agent_version="1.0.0",
        thread_id=thread_id or uuid4(),
        run_id=run_id or uuid4(),
        trace_id="0" * 31 + "1",
        checkpoint_id="ckpt-1",
        message_count=4,
        now=at,
    )


async def _claim(store: MemoryWritebackJobStore, at: datetime) -> MemoryWritebackJob | None:
    return await store.claim_next(now=at, lease_s=_LEASE_S, max_attempts=_MAX)


async def enqueue_then_claim_round_trips_the_pointer(store: MemoryWritebackJobStore) -> None:
    tenant, user, thread, run = uuid4(), uuid4(), uuid4(), uuid4()
    job_id = await _enqueue(
        store, tenant_id=tenant, user_id=user, at=_T0, thread_id=thread, run_id=run
    )
    claimed_at = _T0 + timedelta(seconds=2)

    job = await _claim(store, claimed_at)

    assert job is not None
    assert job.id == job_id
    assert (job.tenant_id, job.user_id, job.thread_id, job.run_id) == (tenant, user, thread, run)
    assert (job.agent_name, job.agent_version) == ("agent-a", "1.0.0")
    assert (job.checkpoint_id, job.message_count, job.trace_id) == ("ckpt-1", 4, "0" * 31 + "1")
    assert job.status == "running"
    assert job.attempts == 1
    assert job.lease_until == claimed_at + timedelta(seconds=_LEASE_S)
    assert job.started_at == claimed_at
    assert job.created_at == _T0
    # The only job is now running under a live lease — nothing else to claim.
    assert await _claim(store, claimed_at) is None


async def one_user_runs_one_job_at_a_time(store: MemoryWritebackJobStore) -> None:
    """(a) —— 同一 (租户, 用户) 串行;别的用户不受影响。"""
    tenant, user_a, user_b = uuid4(), uuid4(), uuid4()
    a1 = await _enqueue(store, tenant_id=tenant, user_id=user_a, at=_T0)
    a2 = await _enqueue(store, tenant_id=tenant, user_id=user_a, at=_T0 + timedelta(seconds=1))
    b1 = await _enqueue(store, tenant_id=tenant, user_id=user_b, at=_T0 + timedelta(seconds=2))
    now = _T0 + timedelta(seconds=3)

    first = await _claim(store, now)
    assert first is not None and first.id == a1
    # A's second job waits behind A's running one; B's job is claimable meanwhile.
    second = await _claim(store, now)
    assert second is not None and second.id == b1
    assert await _claim(store, now) is None

    assert await store.finish(
        job_id=a1,
        attempt=1,
        written_count=2,
        failed=False,
        queued_ms=0,
        exec_ms=5,
        error=None,
        now=now,
    )
    third = await _claim(store, now)
    assert third is not None and third.id == a2


async def same_user_same_user_in_another_tenant_is_independent(
    store: MemoryWritebackJobStore,
) -> None:
    """串行的键是 (tenant_id, user_id),不是 user_id 单列。"""
    user = uuid4()
    t1 = await _enqueue(store, tenant_id=uuid4(), user_id=user, at=_T0)
    t2 = await _enqueue(store, tenant_id=uuid4(), user_id=user, at=_T0 + timedelta(seconds=1))
    now = _T0 + timedelta(seconds=2)
    claimed = {(await _claim(store, now)).id, (await _claim(store, now)).id}  # type: ignore[union-attr]
    assert claimed == {t1, t2}


async def same_instant_jobs_order_by_id(store: MemoryWritebackJobStore) -> None:
    """``created_at`` 并列时按 id 定先后 —— 否则两条并列的行互相等、谁都领不到。"""
    tenant, user = uuid4(), uuid4()
    ids = sorted([await _enqueue(store, tenant_id=tenant, user_id=user, at=_T0) for _ in range(2)])
    job = await _claim(store, _T0)
    assert job is not None and job.id == ids[0]
    assert await _claim(store, _T0) is None


async def a_retried_job_keeps_its_place(store: MemoryWritebackJobStore) -> None:
    """失败退回 pending 的那条仍排在同一用户后来的任务前面。"""
    tenant, user = uuid4(), uuid4()
    a1 = await _enqueue(store, tenant_id=tenant, user_id=user, at=_T0)
    await _enqueue(store, tenant_id=tenant, user_id=user, at=_T0 + timedelta(seconds=1))
    now = _T0 + timedelta(seconds=2)
    first = await _claim(store, now)
    assert first is not None and first.id == a1

    status = await store.fail_attempt(
        job_id=a1, attempt=1, error="boom", max_attempts=_MAX, now=now
    )
    assert status == "pending"

    again = await _claim(store, now)
    assert again is not None and again.id == a1
    assert again.attempts == 2
    assert again.last_error == "boom"


async def an_expired_lease_is_reclaimed_and_fences_the_old_owner(
    store: MemoryWritebackJobStore,
) -> None:
    """(b) —— 租约过期后别人接手;旧持有者的续租 / 收尾不再生效。"""
    tenant, user = uuid4(), uuid4()
    a1 = await _enqueue(store, tenant_id=tenant, user_id=user, at=_T0)
    first = await _claim(store, _T0)
    assert first is not None and first.attempts == 1

    just_before = _T0 + timedelta(seconds=_LEASE_S - 1)
    assert await _claim(store, just_before) is None

    after = _T0 + timedelta(seconds=_LEASE_S)
    reclaimed = await _claim(store, after)
    assert reclaimed is not None and reclaimed.id == a1
    assert reclaimed.attempts == 2

    # The first owner wakes up late: its renew / finish are rejected.
    assert not await store.renew_lease(
        job_id=a1, attempt=1, lease_until=after + timedelta(seconds=_LEASE_S)
    )
    assert not await store.finish(
        job_id=a1,
        attempt=1,
        written_count=1,
        failed=False,
        queued_ms=0,
        exec_ms=1,
        error=None,
        now=after,
    )
    assert (
        await store.fail_attempt(job_id=a1, attempt=1, error="late", max_attempts=_MAX, now=after)
        is None
    )
    # The current owner's renew works and pushes the lease out.
    later_lease = after + timedelta(seconds=2 * _LEASE_S)
    assert await store.renew_lease(job_id=a1, attempt=2, lease_until=later_lease)
    assert await _claim(store, after + timedelta(seconds=_LEASE_S + 1)) is None


async def exhausted_expired_lease_becomes_failed_and_unblocks_the_user(
    store: MemoryWritebackJobStore,
) -> None:
    """(b) —— 租约过期且已到上限的任务收成 ``failed``,同一用户后面的任务接着跑。"""
    tenant, user, run = uuid4(), uuid4(), uuid4()
    a1 = await _enqueue(store, tenant_id=tenant, user_id=user, at=_T0, run_id=run)
    a2 = await _enqueue(store, tenant_id=tenant, user_id=user, at=_T0 + timedelta(seconds=1))
    at = _T0
    for attempt in range(1, _MAX + 1):
        job = await _claim(store, at)
        assert job is not None and job.id == a1 and job.attempts == attempt
        at = at + timedelta(seconds=_LEASE_S)  # crash: the lease simply runs out

    nxt = await _claim(store, at)
    assert nxt is not None and nxt.id == a2
    dead = await store.get_by_run(tenant_id=tenant, run_id=run)
    assert dead is not None
    assert dead.status == "failed"
    assert dead.finished_at == at
    assert dead.last_error is not None and "lease expired" in dead.last_error


async def fail_attempt_exhausts_into_failed(store: MemoryWritebackJobStore) -> None:
    tenant, user, run = uuid4(), uuid4(), uuid4()
    a1 = await _enqueue(store, tenant_id=tenant, user_id=user, at=_T0, run_id=run)
    for attempt in range(1, _MAX + 1):
        job = await _claim(store, _T0)
        assert job is not None and job.attempts == attempt
        status = await store.fail_attempt(
            job_id=a1, attempt=attempt, error=f"e{attempt}", max_attempts=_MAX, now=_T0
        )
        assert status == ("failed" if attempt == _MAX else "pending")
    assert await _claim(store, _T0) is None
    failed = await store.get_by_run(tenant_id=tenant, run_id=run)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.last_error == f"e{_MAX}"
    assert failed.finished_at == _T0
    assert failed.lease_until is None


async def finish_records_the_result(store: MemoryWritebackJobStore) -> None:
    tenant, user, run = uuid4(), uuid4(), uuid4()
    a1 = await _enqueue(store, tenant_id=tenant, user_id=user, at=_T0, run_id=run)
    await _claim(store, _T0)
    done_at = _T0 + timedelta(seconds=9)
    assert await store.finish(
        job_id=a1,
        attempt=1,
        written_count=3,
        failed=True,
        queued_ms=40,
        exec_ms=8000,
        error="flush failed",
        now=done_at,
    )
    job = await store.get_by_run(tenant_id=tenant, run_id=run)
    assert job is not None
    assert job.status == "done"
    assert (job.written_count, job.failed, job.queued_ms, job.exec_ms) == (3, True, 40, 8000)
    assert job.last_error == "flush failed"
    assert job.finished_at == done_at
    assert job.lease_until is None
    # Finishing twice is a no-op (the row is no longer running).
    assert not await store.finish(
        job_id=a1,
        attempt=1,
        written_count=0,
        failed=False,
        queued_ms=0,
        exec_ms=0,
        error=None,
        now=done_at,
    )
    # Tenant-scoped read: another tenant cannot see it.
    assert await store.get_by_run(tenant_id=uuid4(), run_id=run) is None


async def purge_deletes_by_user_and_by_thread(store: MemoryWritebackJobStore) -> None:
    """(d) —— 清除用户 / 删会话把任务删掉,范围不越界。"""
    tenant, user, other_user = uuid4(), uuid4(), uuid4()
    thread_x, thread_y = uuid4(), uuid4()
    u1 = await _enqueue(store, tenant_id=tenant, user_id=user, at=_T0, thread_id=thread_x)
    u2 = await _enqueue(store, tenant_id=tenant, user_id=user, at=_T0, thread_id=thread_y)
    keep = await _enqueue(store, tenant_id=tenant, user_id=other_user, at=_T0, thread_id=uuid4())
    elsewhere = await _enqueue(store, tenant_id=uuid4(), user_id=user, at=_T0, thread_id=thread_x)
    await _claim(store, _T0)  # one of them running — deletion does not care about status

    assert await store.delete_for_thread(tenant_id=tenant, thread_id=thread_x) == 1
    assert not await store.exists(job_id=u1)
    assert await store.exists(job_id=u2)
    assert await store.exists(job_id=elsewhere)  # same thread id, other tenant

    assert await store.delete_all_for_user(tenant_id=tenant, user_id=user) == 1
    assert not await store.exists(job_id=u2)
    assert await store.exists(job_id=keep)
    assert await store.exists(job_id=elsewhere)
    assert await store.delete_all_for_user(tenant_id=tenant, user_id=user) == 0


async def backlog_counts_unfinished_jobs(store: MemoryWritebackJobStore) -> None:
    tenant = uuid4()
    for _ in range(3):
        await _enqueue(store, tenant_id=tenant, user_id=uuid4(), at=_T0)
    assert await store.count_backlog() == 3
    job = await _claim(store, _T0)
    assert job is not None
    assert await store.count_backlog() == 3  # running still counts
    await store.finish(
        job_id=job.id,
        attempt=1,
        written_count=0,
        failed=False,
        queued_ms=0,
        exec_ms=0,
        error=None,
        now=_T0,
    )
    assert await store.count_backlog() == 2


_SCENARIOS: tuple[Callable[[MemoryWritebackJobStore], Awaitable[None]], ...] = (
    enqueue_then_claim_round_trips_the_pointer,
    one_user_runs_one_job_at_a_time,
    same_user_same_user_in_another_tenant_is_independent,
    same_instant_jobs_order_by_id,
    a_retried_job_keeps_its_place,
    an_expired_lease_is_reclaimed_and_fences_the_old_owner,
    exhausted_expired_lease_becomes_failed_and_unblocks_the_user,
    fail_attempt_exhausts_into_failed,
    finish_records_the_result,
    purge_deletes_by_user_and_by_thread,
    backlog_counts_unfinished_jobs,
)


@pytest.fixture
def writeback_job_scenarios() -> tuple[Callable[[MemoryWritebackJobStore], Awaitable[None]], ...]:
    """B-168 —— 记忆写回任务表的契约场景,内存版与 SQL 版共用。"""
    return _SCENARIOS
