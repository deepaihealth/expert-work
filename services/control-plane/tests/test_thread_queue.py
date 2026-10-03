"""B-139 —— ``control_plane.thread_queue``:接单判断与排队那一轮的 SSE。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from control_plane.thread_queue import (
    MAX_QUEUED_PER_THREAD,
    ThreadQueueFullError,
    admit,
    queued_turn_stream,
)
from expert_work.runtime.runs import DisconnectMode, InMemoryRunStore, RunInfo, RunStatus
from expert_work.runtime.stream_bridge.base import HEARTBEAT_FRAME

_ME = "replica-me"


def _row(
    *, tenant: UUID, thread: UUID, status: RunStatus, minutes: int, owner: str | None = None
) -> RunInfo:
    at = datetime.now(UTC) - timedelta(hours=1) + timedelta(minutes=minutes)
    return RunInfo(
        run_id=uuid4(),
        tenant_id=tenant,
        thread_id=thread,
        user_id=None,
        status=status,
        on_disconnect=DisconnectMode.CONTINUE,
        is_resume=False,
        error=None,
        created_at=at,
        updated_at=at,
        finished_at=None,
        claimed_by=owner,
        lease_until=datetime.now(UTC) + timedelta(seconds=30) if owner else None,
        enqueued_input={"input": "second"} if status is RunStatus.QUEUED else None,
    )


# --- admit -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admit_idle_thread() -> None:
    store = InMemoryRunStore()
    tenant, thread = uuid4(), uuid4()
    await store.create(_row(tenant=tenant, thread=thread, status=RunStatus.SUCCESS, minutes=0))
    admission = await admit(store, thread_id=thread, tenant_id=tenant)
    assert not admission.busy
    assert admission.ahead == 0


@pytest.mark.asyncio
async def test_admit_counts_every_unfinished_turn_ahead() -> None:
    store = InMemoryRunStore()
    tenant, thread = uuid4(), uuid4()
    await store.create(_row(tenant=tenant, thread=thread, status=RunStatus.RUNNING, minutes=0))
    await store.create(_row(tenant=tenant, thread=thread, status=RunStatus.QUEUED, minutes=1))
    admission = await admit(store, thread_id=thread, tenant_id=tenant)
    assert admission.busy
    assert admission.ahead == 2


@pytest.mark.asyncio
async def test_admit_refuses_when_the_queue_is_full() -> None:
    store = InMemoryRunStore()
    tenant, thread = uuid4(), uuid4()
    await store.create(_row(tenant=tenant, thread=thread, status=RunStatus.RUNNING, minutes=0))
    for i in range(MAX_QUEUED_PER_THREAD - 1):
        await store.create(
            _row(tenant=tenant, thread=thread, status=RunStatus.QUEUED, minutes=i + 1)
        )
    assert (await admit(store, thread_id=thread, tenant_id=tenant)).ahead == MAX_QUEUED_PER_THREAD
    await store.create(_row(tenant=tenant, thread=thread, status=RunStatus.QUEUED, minutes=9))
    with pytest.raises(ThreadQueueFullError):
        await admit(store, thread_id=thread, tenant_id=tenant)


# --- queued_turn_stream ------------------------------------------------------


class _Harness:
    """一个忙会话:``leader`` 在跑, ``follower`` 排在后面、预订给 ``_ME``。"""

    def __init__(self) -> None:
        self.store = InMemoryRunStore()
        self.tenant, self.thread = uuid4(), uuid4()
        self.leader = _row(
            tenant=self.tenant, thread=self.thread, status=RunStatus.RUNNING, minutes=0
        )
        self.follower = _row(
            tenant=self.tenant, thread=self.thread, status=RunStatus.QUEUED, minutes=1, owner=_ME
        )
        self.launched: list[RunInfo] = []
        self.disconnected = False

    async def setup(self) -> None:
        await self.store.create(self.leader)
        await self.store.create(self.follower)

    async def launch(self, claimed: RunInfo) -> AsyncIterator[bytes]:
        self.launched.append(claimed)

        async def frames() -> AsyncIterator[bytes]:
            yield b"event: metadata\n\n"
            yield b"event: end\n\n"

        return frames()

    async def is_disconnected(self) -> bool:
        return self.disconnected

    async def finish_leader(self) -> None:
        await self.store.set_status(
            run_id=self.leader.run_id,
            tenant_id=self.tenant,
            status=RunStatus.SUCCESS,
            updated_at=datetime.now(UTC),
        )

    def stream(self, *, cancel_on_disconnect: bool = False, **kw: float) -> AsyncIterator[bytes]:
        async def no_sleep(_: float) -> None:
            return None

        return queued_turn_stream(
            store=self.store,
            run_id=self.follower.run_id,
            thread_id=self.thread,
            tenant_id=self.tenant,
            owner=_ME,
            lease_ttl_s=30,
            ahead=1,
            launch=self.launch,
            is_disconnected=self.is_disconnected,
            cancel_on_disconnect=cancel_on_disconnect,
            sleep=kw.pop("sleep", no_sleep),  # type: ignore[arg-type]
            **kw,
        )

    async def row(self) -> RunInfo:
        row = await self.store.get(run_id=self.follower.run_id, tenant_id=self.tenant)
        assert row is not None
        return row


@pytest.mark.asyncio
async def test_waits_for_the_leader_then_runs_here() -> None:
    h = _Harness()
    await h.setup()
    sleeps = 0

    async def sleep(_: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            await h.finish_leader()

    frames = [f async for f in h.stream(sleep=sleep)]  # type: ignore[arg-type]

    assert frames[0].startswith(b"event: queued\n")
    assert b'"ahead":1' in frames[0]
    assert frames[-2:] == [b"event: metadata\n\n", b"event: end\n\n"]
    assert len(h.launched) == 1
    assert sleeps == 2  # 上一轮没结束之前一直没开跑
    row = await h.row()
    assert row.status is RunStatus.RUNNING
    assert row.claimed_by == _ME


@pytest.mark.asyncio
async def test_cancelled_while_waiting_ends_without_running() -> None:
    h = _Harness()
    await h.setup()

    async def sleep(_: float) -> None:
        await h.store.request_cancel(
            run_id=h.follower.run_id, tenant_id=h.tenant, updated_at=datetime.now(UTC)
        )

    frames = [f async for f in h.stream(sleep=sleep)]  # type: ignore[arg-type]

    assert h.launched == []
    assert frames[-1].startswith(b"event: end\n")
    assert b'"status":"interrupted"' in frames[-1]


@pytest.mark.asyncio
async def test_console_disconnect_while_waiting_cancels_the_turn() -> None:
    h = _Harness()
    await h.setup()
    h.disconnected = True

    _ = [f async for f in h.stream(cancel_on_disconnect=True)]

    assert h.launched == []
    assert (await h.row()).status is RunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_external_disconnect_hands_the_turn_to_the_queue_worker() -> None:
    """对外「断开继续跑」:放掉预订,后台队列不用等预订过期就能接手。"""
    h = _Harness()
    await h.setup()
    h.disconnected = True

    _ = [f async for f in h.stream(cancel_on_disconnect=False)]

    row = await h.row()
    assert row.status is RunStatus.QUEUED
    assert row.claimed_by is None
    await h.finish_leader()
    worker_view = await h.store.list_queued(limit=10, now=datetime.now(UTC))
    assert [r.run_id for r in worker_view] == [h.follower.run_id]


@pytest.mark.asyncio
async def test_writes_heartbeats_while_waiting() -> None:
    h = _Harness()
    await h.setup()
    sleeps = 0

    async def sleep(_: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            await h.finish_leader()

    frames = [
        f
        async for f in h.stream(
            sleep=sleep,  # type: ignore[arg-type]
            heartbeat_interval_s=0,
            renew_interval_s=0,
        )
    ]

    assert frames.count(HEARTBEAT_FRAME) == 3
    assert len(h.launched) == 1


@pytest.mark.asyncio
async def test_the_reservation_is_renewed_while_waiting() -> None:
    h = _Harness()
    await h.setup()
    before = (await h.row()).lease_until
    seen: list[datetime | None] = []

    async def sleep(_: float) -> None:
        seen.append((await h.row()).lease_until)
        h.disconnected = True

    _ = [f async for f in h.stream(sleep=sleep, renew_interval_s=0)]  # type: ignore[arg-type]

    assert before is not None and seen[0] is not None
    assert seen[0] > before
