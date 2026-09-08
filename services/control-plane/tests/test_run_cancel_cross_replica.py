"""跨副本取消亚秒化 —— 两副本端到端(共享一个 store、同一条假 Redis 总线)。

此前非属主副本的 cancel 只能靠 ``RunStore.request_cancel`` CAS 改行,属主副本
要到 ``_heartbeat_loop`` 下一次续租(``lease_ttl_s / 3``,生产默认 30s/3 = 10s)
CAS 失败才置位 ``abort_event``。这里证明:CAS 赢了 → ``run_cancel`` 事件 →
属主 handler 立刻做一次同样的心跳检查 → abort,< 1s,周期心跳根本没轮到;
总线不通时,周期心跳仍然兜底,取消结果不受影响。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from control_plane.invalidation_bus import InvalidationBus, build_invalidation_handlers
from control_plane.run_cancel import cancel_run_two_level
from expert_work.runtime.runs import InMemoryRunStore, InterruptReason, RunManager, RunStatus
from orchestrator.sse import _heartbeat_loop
from tests.test_invalidation_bus import _BrokenRedis, _FakeRedis, _wait_until


def _bus_for(redis: Any, manager: RunManager, origin: str) -> InvalidationBus:
    bus = InvalidationBus(
        redis_client=redis, origin=origin, reconnect_initial_s=0.01, reconnect_max_s=0.05
    )
    bus.start(
        build_invalidation_handlers(
            SimpleNamespace(agent_runtime=SimpleNamespace(run_manager=manager))
        )
    )
    return bus


async def _owned_running_run(manager: RunManager) -> Any:
    record = await manager.create(run_id=uuid4(), thread_id=uuid4(), tenant_id=uuid4())
    assert await manager.set_status(record.run_id, RunStatus.RUNNING)
    return record


@pytest.mark.asyncio
async def test_peer_cancel_aborts_the_owner_within_a_second_not_at_the_next_heartbeat() -> None:
    redis = _FakeRedis()
    store = InMemoryRunStore()
    # 生产形态:lease_ttl_s=30 → 周期心跳每 10s 一次。没有总线就得等它。
    owner = RunManager(store=store, instance_id="pod-a", lease_ttl_s=30.0)
    peer = RunManager(store=store, instance_id="pod-b", lease_ttl_s=30.0)
    bus_owner = _bus_for(redis, owner, "pod-a")
    bus_peer = _bus_for(redis, peer, "pod-b")
    record = await _owned_running_run(owner)
    heartbeat = asyncio.create_task(_heartbeat_loop(owner, record.run_id, record))
    try:
        await _wait_until(lambda: len(redis.queues) == 2)

        started = time.monotonic()
        stopped = await cancel_run_two_level(
            run_manager=peer,
            run_store=store,
            bus=bus_peer,
            run_id=record.run_id,
            tenant_id=record.tenant_id,
            reason=InterruptReason.USER_CANCEL,
        )
        assert stopped
        await asyncio.wait_for(record.abort_event.wait(), timeout=1.0)
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, f"属主 {elapsed:.2f}s 才停 —— 还在等周期心跳"
        # 周期心跳(10s 一次)还在睡,不是它叫停的。
        assert not heartbeat.done()
        row = await store.get(run_id=record.run_id, tenant_id=record.tenant_id)
        assert row is not None
        assert row.status is RunStatus.INTERRUPTED
        assert row.error == "user_cancel"
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        await bus_owner.stop()
        await bus_peer.stop()


@pytest.mark.asyncio
async def test_heartbeat_fallback_still_stops_the_owner_when_the_bus_is_down() -> None:
    """Redis 不通:publish 静默失败,取消结果照旧(CAS 已赢),属主由周期心跳
    兜底叫停 —— 与今天的行为一致。"""
    store = InMemoryRunStore()
    # 测试把周期缩到 1s(lease_ttl_s=3),证明兜底那条路真的还在。
    owner = RunManager(store=store, instance_id="pod-a", lease_ttl_s=3.0)
    peer = RunManager(store=store, instance_id="pod-b", lease_ttl_s=3.0)
    bus_peer = InvalidationBus(redis_client=_BrokenRedis(), origin="pod-b")
    record = await _owned_running_run(owner)
    heartbeat = asyncio.create_task(_heartbeat_loop(owner, record.run_id, record))
    try:
        stopped = await cancel_run_two_level(
            run_manager=peer,
            run_store=store,
            bus=bus_peer,
            run_id=record.run_id,
            tenant_id=record.tenant_id,
            reason=InterruptReason.USER_CANCEL,
        )
        assert stopped, "总线挂了不能拖累取消本身"
        await asyncio.wait_for(record.abort_event.wait(), timeout=3.0)
        row = await store.get(run_id=record.run_id, tenant_id=record.tenant_id)
        assert row is not None
        assert row.status is RunStatus.INTERRUPTED
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        await bus_peer.stop()


@pytest.mark.asyncio
async def test_local_run_is_aborted_in_process_and_nothing_is_broadcast() -> None:
    """属主副本自己收到 cancel:``RunManager.cancel`` 就地叫停,没有 CAS 那一步,
    也就没有事件 —— 总线只为「非属主」那条路服务。"""
    redis = _FakeRedis()
    store = InMemoryRunStore()
    owner = RunManager(store=store, instance_id="pod-a")
    bus_owner = _bus_for(redis, owner, "pod-a")
    record = await _owned_running_run(owner)
    try:
        await _wait_until(lambda: len(redis.queues) == 1)
        stopped = await cancel_run_two_level(
            run_manager=owner,
            run_store=store,
            bus=bus_owner,
            run_id=record.run_id,
            tenant_id=record.tenant_id,
            reason=InterruptReason.USER_CANCEL,
        )
        assert stopped
        assert record.abort_event.is_set()
        await asyncio.sleep(0.05)
        assert redis.published == []
    finally:
        await bus_owner.stop()


@pytest.mark.asyncio
async def test_cancel_of_a_terminal_row_neither_stops_nor_broadcasts() -> None:
    redis = _FakeRedis()
    store = InMemoryRunStore()
    owner = RunManager(store=store, instance_id="pod-a")
    peer = RunManager(store=store, instance_id="pod-b")
    bus_peer = _bus_for(redis, peer, "pod-b")
    record = await _owned_running_run(owner)
    assert await owner.set_status(record.run_id, RunStatus.SUCCESS)
    try:
        stopped = await cancel_run_two_level(
            run_manager=peer,
            run_store=store,
            bus=bus_peer,
            run_id=record.run_id,
            tenant_id=record.tenant_id,
            reason=InterruptReason.USER_CANCEL,
        )
        assert not stopped
        await asyncio.sleep(0.05)
        assert redis.published == []
    finally:
        await bus_peer.stop()
