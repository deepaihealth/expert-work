"""Unit tests for ``RunManager``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunStore,
    RunManager,
    RunStatus,
)


@pytest.mark.asyncio
async def test_create_registers_run_in_pending_state() -> None:
    mgr = RunManager()
    run_id, thread_id, tenant_id = uuid4(), uuid4(), uuid4()

    record = await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)

    assert record.status is RunStatus.PENDING
    assert record.on_disconnect is DisconnectMode.CANCEL
    assert record.run_id == run_id
    assert mgr.get(run_id) is record


@pytest.mark.asyncio
async def test_create_rejects_duplicate_run_id() -> None:
    mgr = RunManager()
    run_id, thread_id, tenant_id = uuid4(), uuid4(), uuid4()

    await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)
    with pytest.raises(ValueError, match="already exists"):
        await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)


@pytest.mark.asyncio
async def test_list_by_thread_filters_by_tenant() -> None:
    mgr = RunManager()
    thread_id, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    for _ in range(3):
        await mgr.create(run_id=uuid4(), thread_id=thread_id, tenant_id=tenant_a)
    await mgr.create(run_id=uuid4(), thread_id=thread_id, tenant_id=tenant_b)

    a_runs = await mgr.list_by_thread(thread_id, tenant_id=tenant_a)
    b_runs = await mgr.list_by_thread(thread_id, tenant_id=tenant_b)
    assert len(a_runs) == 3
    assert len(b_runs) == 1


@pytest.mark.asyncio
async def test_delete_by_thread_clears_registry_and_store() -> None:
    store = InMemoryRunStore()
    mgr = RunManager(store=store)
    thread_a, thread_b, tenant = uuid4(), uuid4(), uuid4()
    a_ids = [uuid4(), uuid4()]
    for rid in a_ids:
        await mgr.create(run_id=rid, thread_id=thread_a, tenant_id=tenant)
    keep = uuid4()
    await mgr.create(run_id=keep, thread_id=thread_b, tenant_id=tenant)

    removed = await mgr.delete_by_thread(thread_a, tenant_id=tenant)
    assert removed == 2
    # In-memory registry cleared for the purged thread…
    assert await mgr.list_by_thread(thread_a, tenant_id=tenant) == []
    assert mgr.get(a_ids[0]) is None
    # …and the durable mirror too, while the other thread survives.
    assert await store.list_by_thread(thread_id=thread_a, tenant_id=tenant) == []
    assert await store.get(run_id=keep, tenant_id=tenant) is not None


@pytest.mark.asyncio
async def test_set_status_transitions() -> None:
    mgr = RunManager()
    run_id, thread_id, tenant_id = uuid4(), uuid4(), uuid4()
    await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)

    transitioned = await mgr.set_status(run_id, RunStatus.RUNNING)
    assert transitioned is True
    assert mgr.get(run_id) is not None
    assert mgr.get(run_id).status is RunStatus.RUNNING  # type: ignore[union-attr]

    succeeded = await mgr.set_status(run_id, RunStatus.SUCCESS)
    assert succeeded is True
    assert mgr.get(run_id).status is RunStatus.SUCCESS  # type: ignore[union-attr]

    missing = await mgr.set_status(uuid4(), RunStatus.SUCCESS)
    assert missing is False


@pytest.mark.asyncio
async def test_cancel_signals_abort_event_and_marks_interrupted() -> None:
    mgr = RunManager()
    run_id, thread_id, tenant_id = uuid4(), uuid4(), uuid4()
    record = await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)
    await mgr.set_status(run_id, RunStatus.RUNNING)

    cancelled = await mgr.cancel(run_id)
    assert cancelled is True
    assert record.abort_event.is_set()
    assert mgr.get(run_id).status is RunStatus.INTERRUPTED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_cancel_does_not_overwrite_terminal_status() -> None:
    mgr = RunManager()
    run_id, thread_id, tenant_id = uuid4(), uuid4(), uuid4()
    await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)
    await mgr.set_status(run_id, RunStatus.SUCCESS)

    cancelled = await mgr.cancel(run_id)
    assert cancelled is True
    # SUCCESS is terminal — cancel signals abort_event but should not flip status
    assert mgr.get(run_id).status is RunStatus.SUCCESS  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_has_inflight_returns_true_for_running_runs() -> None:
    mgr = RunManager()
    thread_id, tenant_id = uuid4(), uuid4()
    run_id = uuid4()
    await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)

    flying = await mgr.has_inflight(thread_id, tenant_id=tenant_id)
    assert flying is True

    await mgr.set_status(run_id, RunStatus.SUCCESS)
    not_flying = await mgr.has_inflight(thread_id, tenant_id=tenant_id)
    assert not_flying is False


@pytest.mark.asyncio
async def test_has_inflight_tenant_isolation() -> None:
    mgr = RunManager()
    thread_id, tenant_a, tenant_b = uuid4(), uuid4(), uuid4()
    await mgr.create(run_id=uuid4(), thread_id=thread_id, tenant_id=tenant_a)

    a = await mgr.has_inflight(thread_id, tenant_id=tenant_a)
    b = await mgr.has_inflight(thread_id, tenant_id=tenant_b)
    assert a is True
    assert b is False


@pytest.mark.asyncio
async def test_cleanup_removes_run() -> None:
    mgr = RunManager()
    run_id, thread_id, tenant_id = uuid4(), uuid4(), uuid4()
    await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)

    await mgr.cleanup(run_id, delay=0)
    assert mgr.get(run_id) is None


# ---------------------------------------------------------------------------
# Durable RunStore mirroring — Mini-ADR J-41
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_mirrors_to_store() -> None:
    store = InMemoryRunStore()
    mgr = RunManager(store=store)
    run_id, thread_id, tenant_id, user_id = uuid4(), uuid4(), uuid4(), uuid4()

    await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id, user_id=user_id)

    persisted = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert persisted is not None
    assert persisted.status is RunStatus.PENDING
    assert persisted.thread_id == thread_id
    assert persisted.user_id == user_id


@pytest.mark.asyncio
async def test_set_status_mirrors_to_store() -> None:
    store = InMemoryRunStore()
    mgr = RunManager(store=store)
    run_id, thread_id, tenant_id = uuid4(), uuid4(), uuid4()
    await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)

    await mgr.set_status(run_id, RunStatus.RUNNING)
    running = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert running is not None
    assert running.status is RunStatus.RUNNING
    assert running.finished_at is None  # RUNNING is not terminal

    await mgr.set_status(run_id, RunStatus.SUCCESS)
    done = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert done is not None
    assert done.status is RunStatus.SUCCESS
    assert done.finished_at is not None  # terminal → finished_at stamped


@pytest.mark.asyncio
async def test_set_status_error_mirrors_detail() -> None:
    store = InMemoryRunStore()
    mgr = RunManager(store=store)
    run_id, thread_id, tenant_id = uuid4(), uuid4(), uuid4()
    await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)

    await mgr.set_status(run_id, RunStatus.ERROR, error="provider 503")
    failed = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert failed is not None
    assert failed.status is RunStatus.ERROR
    assert failed.error == "provider 503"
    assert failed.finished_at is not None


@pytest.mark.asyncio
async def test_cancel_mirrors_interrupted_to_store() -> None:
    store = InMemoryRunStore()
    mgr = RunManager(store=store)
    run_id, thread_id, tenant_id = uuid4(), uuid4(), uuid4()
    await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)

    await mgr.cancel(run_id, reason="client_disconnect")
    persisted = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert persisted is not None
    assert persisted.status is RunStatus.INTERRUPTED
    assert persisted.finished_at is not None
    # 中断原因(InterruptReason 词表)随状态一起入账 —— 界面靠它把「断流被杀」
    # 与「用户主动取消」分开(2026-08-26 用户反馈)。
    assert persisted.error == "client_disconnect"


@pytest.mark.asyncio
async def test_cleanup_keeps_durable_row() -> None:
    """The 5-minute TTL drops the in-memory record but not the agent_run row."""
    store = InMemoryRunStore()
    mgr = RunManager(store=store)
    run_id, thread_id, tenant_id = uuid4(), uuid4(), uuid4()
    await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)
    await mgr.set_status(run_id, RunStatus.SUCCESS)

    await mgr.cleanup(run_id, delay=0)

    assert mgr.get(run_id) is None  # in-memory record gone
    persisted = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert persisted is not None  # durable row survives the TTL sweep
    assert persisted.status is RunStatus.SUCCESS


# --- Stream 9.4 (HA failover) — lease claim + heartbeat ----------------------


@pytest.mark.asyncio
async def test_running_transition_claims_lease() -> None:
    store = InMemoryRunStore()
    mgr = RunManager(store, instance_id="inst-a", lease_ttl_s=30.0)
    run_id, thread_id, tenant_id = uuid4(), uuid4(), uuid4()
    await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)
    await mgr.set_status(run_id, RunStatus.RUNNING)
    row = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert row is not None
    assert row.claimed_by == "inst-a"
    assert row.lease_until is not None  # leased
    assert row.heartbeat_at is not None


@pytest.mark.asyncio
async def test_heartbeat_renews_for_owner_only() -> None:
    store = InMemoryRunStore()
    mgr = RunManager(store, instance_id="inst-a", lease_ttl_s=30.0)
    run_id, thread_id, tenant_id = uuid4(), uuid4(), uuid4()
    await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)
    await mgr.set_status(run_id, RunStatus.RUNNING)
    assert await mgr.heartbeat(run_id) is True
    # A peer reclaims (changes claimed_by) → the original owner's heartbeat fails.
    now = datetime.now(UTC)
    await store.reclaim(
        run_id=run_id,
        new_owner="inst-b",
        lease_until=now + timedelta(seconds=30),
        heartbeat_at=now,
        now=now + timedelta(hours=1),  # force the stale-lease CAS to pass
    )
    assert await mgr.heartbeat(run_id) is False


@pytest.mark.asyncio
async def test_heartbeat_noop_without_store() -> None:
    mgr = RunManager()  # no store
    run_id, thread_id, tenant_id = uuid4(), uuid4(), uuid4()
    await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)
    assert await mgr.heartbeat(run_id) is True  # no-op true


def test_instance_id_is_stable_and_unique() -> None:
    a, b = RunManager(), RunManager()
    assert a.instance_id and b.instance_id
    assert a.instance_id != b.instance_id  # random suffix disambiguates


# ---------------------------------------------------------------------------
# 多副本 CAS 守卫 —— 洞 A(取消复活)与洞 B(reclaim 后终局互踩)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_transition_refused_after_cross_replica_cancel() -> None:
    """洞 A:PENDING 窗口里被跨副本 ``request_cancel`` 的 run,属主随后的
    → RUNNING 写必须被守卫拒绝 —— 不复活、不 claim、record 镜像真实终态并
    置位 abort_event,让执行方一步图都不跑。"""
    store = InMemoryRunStore()
    mgr = RunManager(store=store)
    run_id, thread_id, tenant_id = uuid4(), uuid4(), uuid4()
    record = await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)

    # 另一副本的取消赢了(guarded CAS 命中 pending)。
    assert await store.request_cancel(
        run_id=run_id, tenant_id=tenant_id, updated_at=datetime.now(UTC), reason="user_cancel"
    )

    assert await mgr.set_status(run_id, RunStatus.RUNNING) is False
    assert record.status is RunStatus.INTERRUPTED
    assert record.abort_event.is_set()
    row = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert row is not None
    assert row.status is RunStatus.INTERRUPTED, "durable 行被复活成 running 了"
    assert row.error == "user_cancel", "取消原因被 → RUNNING 写抹掉了"
    assert row.claimed_by is None, "被取消的 run 不该被 claim"


@pytest.mark.asyncio
async def test_terminal_write_after_peer_reclaim_is_dropped() -> None:
    """洞 B:租约被 orphan sweep 判死、run 被别的副本 reclaim 之后,旧属主
    迟到的终局写必须 no-op —— 不把新属主的 running 盖掉(否则两边全停,
    failover 白做)。"""
    store = InMemoryRunStore()
    mgr = RunManager(store=store, instance_id="pod-a")
    run_id, thread_id, tenant_id = uuid4(), uuid4(), uuid4()
    await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)
    assert await mgr.set_status(run_id, RunStatus.RUNNING) is True

    # 新属主 reclaim(orphan sweep 的效果:行归 pod-b)。
    now = datetime.now(UTC)
    await store.claim(
        run_id=run_id,
        tenant_id=tenant_id,
        claimed_by="pod-b",
        lease_until=now + timedelta(seconds=30),
        heartbeat_at=now,
    )

    assert await mgr.set_status(run_id, RunStatus.INTERRUPTED) is False
    row = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert row is not None
    assert row.status is RunStatus.RUNNING, "旧属主的终局写盖掉了新属主的 running"
    assert row.claimed_by == "pod-b"
    # 本进程的镜像照旧终局 —— 本副本的执行确实结束了。
    rec = mgr.get(run_id)
    assert rec is not None and rec.status is RunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_terminal_write_on_never_claimed_run_still_lands() -> None:
    """守卫的 NULL 分支:从未 claim 过的 run(如 PENDING 直接失败)的终局写
    是合法的,不能被 claimed_by 守卫误伤。"""
    store = InMemoryRunStore()
    mgr = RunManager(store=store)
    run_id, thread_id, tenant_id = uuid4(), uuid4(), uuid4()
    await mgr.create(run_id=run_id, thread_id=thread_id, tenant_id=tenant_id)

    assert await mgr.set_status(run_id, RunStatus.ERROR, error="boom") is True
    row = await store.get(run_id=run_id, tenant_id=tenant_id)
    assert row is not None and row.status is RunStatus.ERROR
