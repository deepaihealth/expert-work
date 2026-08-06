"""Tests for :class:`ApprovalGaugeWorker` — Stream HX-4 (§ 5.4)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from control_plane import approval_metrics as mod
from control_plane.approval_metrics import ApprovalGaugeWorker, _approvals_pending
from expert_work.persistence import InMemoryApprovalStore
from expert_work.protocol import ApprovalRecord, ApprovalStatus


def _record(status: ApprovalStatus = ApprovalStatus.PENDING) -> ApprovalRecord:
    now = datetime.now(UTC)
    return ApprovalRecord(
        id=uuid4(),
        tenant_id=uuid4(),
        user_id=None,
        run_id=uuid4(),
        thread_id=uuid4(),
        request_id=f"approval:{uuid4().hex[:8]}",
        node="tools",
        reason_kind="policy_gate",
        action_summary="approval-gated tool 'send_email'",
        proposed_args={},
        requested_at=now,
        timeout_at=now + timedelta(hours=24),
        status=status,
    )


def _gauge_value() -> float:
    return _approvals_pending._value.get()  # type: ignore[attr-defined,no-any-return]


@pytest.mark.asyncio
async def test_refresh_sets_gauge_to_pending_count() -> None:
    store = InMemoryApprovalStore()
    await store.create(_record())
    await store.create(_record())
    decided = _record()
    await store.create(decided)
    await store.mark_decided(
        run_id=decided.run_id,
        tenant_id=decided.tenant_id,
        status=ApprovalStatus.APPROVED,
        decided_by="admin",
        decided_at=datetime.now(UTC),
    )

    worker = ApprovalGaugeWorker(approval_store=store)
    assert await worker.refresh_once()
    assert _gauge_value() == 2.0


@pytest.mark.asyncio
async def test_count_pending_in_memory_counts_only_pending() -> None:
    store = InMemoryApprovalStore()
    assert await store.count_pending() == 0
    await store.create(_record())
    assert await store.count_pending() == 1


@pytest.mark.asyncio
async def test_failed_read_skips_cycle_without_dying() -> None:
    class _ExplodingStore(InMemoryApprovalStore):
        async def count_pending(self) -> int:
            raise RuntimeError("db away")

    worker = ApprovalGaugeWorker(approval_store=_ExplodingStore())
    assert not await worker.refresh_once()  # logged + counted, no raise


@pytest.mark.asyncio
async def test_start_refreshes_immediately_and_stop_joins() -> None:
    store = InMemoryApprovalStore()
    await store.create(_record())
    worker = ApprovalGaugeWorker(approval_store=store, interval_s=3600)
    worker.start()
    try:
        # start() refreshes once before the first interval elapses.
        import asyncio

        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert _gauge_value() == 1.0
        assert worker.is_running
    finally:
        await worker.stop()
    assert not worker.is_running


@pytest.mark.asyncio
async def test_stop_is_bounded_when_a_sweep_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    """卡死的 sweep 不能把关机拖到 SIGKILL —— stop() 等一小会儿就取消它。

    lifespan 顺序 await 每个 worker 的 stop();这里少了上界,一个连不上
    数据库的 refresh 就能把整个进程的关机挂到 K8s 优雅期耗尽。
    """

    class _ExplodingStore(InMemoryApprovalStore):
        async def count_pending(self) -> int:
            raise RuntimeError("db away")

    worker = ApprovalGaugeWorker(approval_store=_ExplodingStore(), interval_s=0.01)
    entered = asyncio.Event()

    async def _never_returns() -> bool:
        entered.set()
        await asyncio.sleep(3600)
        return False

    monkeypatch.setattr(worker, "refresh_once", _never_returns)
    monkeypatch.setattr(mod, "_STOP_TIMEOUT_S", 0.05, raising=False)

    worker.start()
    await asyncio.wait_for(entered.wait(), timeout=2)

    # 修复前:stop() 永远等下去,这里超时失败。
    await asyncio.wait_for(worker.stop(), timeout=2)
    assert worker._task is None
