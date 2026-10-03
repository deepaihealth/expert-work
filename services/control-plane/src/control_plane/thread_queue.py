"""B-139 —— 同一会话的多轮串行执行(排队)。

上一轮还在跑时再发一轮,两轮今天会**同时**跑:各自从同一个检查点出发、各写
各的分支,后写完的成为「最新」,另一轮的对话从历史里静默消失(B-75 的来源)。
设计稿 ``docs/superpowers/specs/2026-10-02-b139-same-thread-queue-design.md``。

本模块两件事:

* :func:`admit` —— 接单时(在会话锁里)判断会话忙不忙、前面有几轮、排没排满。
  读库(:meth:`RunStore.list_inflight_by_thread`),看得到别的副本与排队中的
  run;进程内存版 ``RunManager.has_inflight`` 两样都看不到。
* :func:`queued_turn_stream` —— 流式请求排在后面时的那条 SSE:先发一帧
  ``queued``,轮到了由**持着这条连接的本副本**自己领走执行(逐字输出不落库,
  只在执行它的进程里有),之后原样转发 ``sse_consumer`` 的帧。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from expert_work.runtime.runs import InterruptReason, RunInfo, RunStatus
from expert_work.runtime.runs.store import RunStore
from expert_work.runtime.stream_bridge.base import HEARTBEAT_FRAME
from orchestrator.sse import end_frame_data, format_sse

logger = logging.getLogger(__name__)

#: 同一会话最多排几轮。第 4 轮 409 —— 真实数据里两轮重叠都罕见(测试环境
#: 30 天 590 个 run 里 2 对),排到第 4 轮几乎只可能是程序在死循环。平台常量,
#: 不做成配置。
MAX_QUEUED_PER_THREAD = 3

THREAD_QUEUE_FULL = "THREAD_QUEUE_FULL"

#: 排队那一轮的 SSE 事件名。新增事件:对接方客户端按白名单收事件,不认识的忽略。
QUEUED_EVENT = "queued"

#: 等待中多久试一次「轮到没有」(一次带条件的领取 CAS)。
POLL_INTERVAL_S = 1.0
#: 预订多久续一次;预订有效期取 ``RunManager.lease_ttl_s``(30 秒),续三次。
RENEW_INTERVAL_S = 10.0
#: 等待期间多久写一个心跳(与 ``sse_consumer`` 同一节奏)。
HEARTBEAT_INTERVAL_S = 15.0

#: 排队那一轮没等到执行就结束了(被取消 / 失败)时,``end`` 帧的状态。
_END_STATUS: dict[RunStatus, str] = {
    RunStatus.SUCCESS: "success",
    RunStatus.PAUSED: "paused",
    RunStatus.INTERRUPTED: "interrupted",
    RunStatus.ERROR: "error",
    RunStatus.TIMEOUT: "error",
}


class ThreadQueueFullError(Exception):
    """会话里已经排了 :data:`MAX_QUEUED_PER_THREAD` 轮。"""

    message = (
        f"this session already has {MAX_QUEUED_PER_THREAD} turns waiting; "
        "wait for one to finish or cancel one"
    )


@dataclass(frozen=True)
class Admission:
    """接单判断。``ahead`` = 前面还有几轮没结束(含正在跑的那一轮)。"""

    ahead: int

    @property
    def busy(self) -> bool:
        return self.ahead > 0


async def admit(store: RunStore, *, thread_id: UUID, tenant_id: UUID) -> Admission:
    """会话忙不忙 + 排没排满。**必须在会话接单锁里调用** —— 锁保证两个同时
    打到空闲会话的请求只有一个看到「空闲」。"""
    busy = await store.list_inflight_by_thread(thread_id=thread_id, tenant_id=tenant_id)
    waiting = sum(1 for r in busy if r.status is RunStatus.QUEUED)
    if waiting >= MAX_QUEUED_PER_THREAD:
        raise ThreadQueueFullError
    return Admission(ahead=len(busy))


async def queued_turn_stream(
    *,
    store: RunStore,
    run_id: UUID,
    thread_id: UUID,
    tenant_id: UUID,
    owner: str,
    lease_ttl_s: float,
    ahead: int,
    launch: Callable[[RunInfo], Awaitable[AsyncIterator[bytes]]],
    is_disconnected: Callable[[], Awaitable[bool]],
    cancel_on_disconnect: bool,
    poll_interval_s: float = POLL_INTERVAL_S,
    renew_interval_s: float = RENEW_INTERVAL_S,
    heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[bytes]:
    """排在后面的那一轮的 SSE。

    1. 先发一帧 ``queued``(``ahead`` = 前面几轮)。
    2. 每 ``poll_interval_s`` 试领一次(:meth:`RunStore.claim_queued` 的条件 =
       同会话没有更早的忙 run;预订者自己可领)。领到 → ``launch`` 起这一轮并
       返回它的 ``sse_consumer`` 帧流,之后原样转发。
    3. 等待中:每 ``renew_interval_s`` 续一次预订;按真正写出的字节计时补心跳。
    4. 行已不再排队(被取消 / 失败)→ 发 ``end`` 帧收尾。被别的副本领走(本副本
       的预订过期了)→ 直接收尾,客户端按 ``/events`` 续传的老办法去读。

    没等到执行就断开:``cancel_on_disconnect``(控制台「断开即取消」)→ 取消
    这一轮;否则(对外「断开继续跑」)→ 放掉预订,后台队列立刻能接手。
    """
    yield format_sse(
        QUEUED_EVENT, {"run_id": str(run_id), "thread_id": str(thread_id), "ahead": ahead}
    )
    launched = False
    last_renew = last_write = time.monotonic()
    try:
        while True:
            if await is_disconnected():
                logger.info("thread_queue.client_disconnected run_id=%s", run_id)
                return
            now = datetime.now(UTC)
            claimed = await store.claim_queued(
                run_id=run_id,
                new_owner=owner,
                lease_until=now + timedelta(seconds=lease_ttl_s),
                heartbeat_at=now,
            )
            if claimed is not None:
                launched = True
                frames = await launch(claimed)
                async for frame in frames:
                    yield frame
                return
            row = await store.get(run_id=run_id, tenant_id=tenant_id)
            if row is None or row.status is not RunStatus.QUEUED:
                if row is not None and row.status in _END_STATUS:
                    yield format_sse(
                        "end", end_frame_data(run_id=run_id, status=_END_STATUS[row.status])
                    )
                logger.info(
                    "thread_queue.left_queue run_id=%s status=%s",
                    run_id,
                    row.status.value if row is not None else None,
                )
                return
            if time.monotonic() - last_renew >= renew_interval_s:
                await store.renew_queued_reservation(
                    run_id=run_id,
                    owner=owner,
                    lease_until=now + timedelta(seconds=lease_ttl_s),
                    now=now,
                )
                last_renew = time.monotonic()
            if time.monotonic() - last_write >= heartbeat_interval_s:
                yield HEARTBEAT_FRAME
                last_write = time.monotonic()
            await sleep(poll_interval_s)
    finally:
        if not launched:
            if cancel_on_disconnect:
                await store.request_cancel(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    updated_at=datetime.now(UTC),
                    reason=InterruptReason.CLIENT_DISCONNECT.value,
                )
            else:
                await store.release_queued_reservation(run_id=run_id, owner=owner)
