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
