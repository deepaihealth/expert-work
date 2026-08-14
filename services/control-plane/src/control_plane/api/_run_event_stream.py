"""Shared SSE event producer — replay (terminal run) vs live attach (active run).

Extracted out of ``api/runs.py`` (Stream H.3 PR 4) so the console endpoint
(``GET /v1/sessions/{thread_id}/runs/{run_id}/events``) and the external
endpoint (``GET /v1/agents/{agent_code}/runs/{run_id}/events``) drive the
exact same wire format off the exact same two backends instead of carrying
two copies that silently drift. This repo has a documented failure mode where
the same semantics get implemented twice and a later constraint lands on only
one copy — P3 is about to fix three bugs on this stream (seq misalignment,
live ignoring ``since_seq``, unpaginated replay), so a single call site is the
point.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from expert_work.runtime.runs import RunEventRecord, RunEventStore
from expert_work.runtime.runs.store import MAX_LIST_LIMIT
from expert_work.runtime.stream_bridge import (
    HEARTBEAT_SENTINEL,
    StreamBridge,
    StreamEvent,
    is_end,
)
from orchestrator.sse import format_sse

logger = logging.getLogger(__name__)

#: 实时接合的**乱序重排窗口**。``_publish_frame`` 是「同步分号 → await publish」,
#: 并发 worker 下先分到号的帧可能后进 bridge,所以 seq 跳号的第一解释是「乱序
#: 到达」而不是「丢帧」。见缺口就查库有两个后果:一是把还在路上的帧当丢帧去查
#: 一次库(此时后台攒批 writer 多半还没落盘,查了也拿不到),二是等真帧从
#: bridge 到达时它已经 ``<= last`` 被丢弃 —— **本来没丢的帧被那个逻辑弄丢**。
#: 所以先攒进 pending 给乱序一个收敛机会,攒到超过这个数才认定是真缺口。
_REORDER_WINDOW = 32


def _seq_of(entry: StreamEvent) -> int | None:
    """从帧 id 里解析落库 ``seq``;``None`` 表示这帧不参与接合。

    ``entry.id is None`` 是 token 帧 —— 不可回放、不占号(Task 1 的契约)。
    id 形状不认识时同样返回 ``None``:放行总比把它当成某个号去参与去重安全。
    """
    if entry.id is None:
        return None
    try:
        return int(entry.id.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        logger.warning("live_stream.unparsable_frame_id id=%s", entry.id)
        return None


def build_event_producer(
    *,
    run_id: UUID,
    is_terminal: bool,
    event_store: RunEventStore | None,
    stream_bridge: StreamBridge,
    since_seq: int | None,
    scope: Callable[[], AbstractAsyncContextManager[None]] | None,
) -> AsyncIterator[bytes]:
    """Return the SSE byte producer for one run.

    * Terminal run → :meth:`RunEventStore.list`, one shot, ordered by seq.
    * Active run → 先把 ``since_seq`` 之后的落库帧补齐,再挂
      :meth:`StreamBridge.subscribe` 的实时流,按 seq 去重 / 重排 / 回填缺口
      (见 ``_stream_live`` 的 docstring)。

    ``scope`` is a *factory* for a tenant-scope context manager, not an
    already-constructed one — ``applied_scope(...)`` returns a
    single-use ``_AsyncGeneratorContextManager`` (``__aenter__`` a second
    time raises ``RuntimeError: generator didn't yield``), and P3's
    paginated replay will need to enter scope once per page. The console
    caller passes ``lambda: applied_scope(scope)`` (its DB read must stay
    bound to the resolved target tenant, not the request middleware's
    home-tenant GUC); the external caller has no cross-tenant concept and
    passes ``None`` explicitly — there is no default, so a caller cannot
    silently forget this and fall back to an unscoped read.
    """

    async def _list_page(after: int) -> Sequence[RunEventRecord]:
        """读一页落库帧(``seq > after``,最多 ``MAX_LIST_LIMIT`` 条)。

        ``scope`` 工厂**每次读都要重新调**:它返回的 CM 是单次可用的,复用会
        炸 ``generator didn't yield``。
        """
        if event_store is None:
            return []
        if scope is not None:
            async with scope():
                return await event_store.list(run_id=run_id, since_seq=after, limit=MAX_LIST_LIMIT)
        return await event_store.list(run_id=run_id, since_seq=after, limit=MAX_LIST_LIMIT)

    async def _stream_replay() -> AsyncIterator[bytes]:
        """Pull from RunEventStore (one shot, ordered by seq)."""
        if event_store is None:
            # No store wired — yield an end frame so the client closes
            # cleanly instead of waiting forever.
            yield format_sse("end", None)
            return
        # The generator body runs after the handler returned — re-apply
        # the resolved scope (when given) so this DB read stays bound to
        # the target tenant. Call the factory fresh each time — the CM it
        # returns is single-use.
        if scope is not None:
            async with scope():
                rows = await event_store.list(
                    run_id=run_id, since_seq=since_seq, limit=MAX_LIST_LIMIT
                )
        else:
            rows = await event_store.list(run_id=run_id, since_seq=since_seq, limit=MAX_LIST_LIMIT)
        for row in rows:
            yield format_sse(
                row.event_name,
                row.data,
                event_id=f"{row.created_at_ms}-{row.seq}",
            )
        yield format_sse("end", None)

    async def _stream_live() -> AsyncIterator[bytes]:
        """Live attach —— 先补库,再接实时流(P3 PR-1 Task 3 的接合算法)。

        1. **补库**:从 ``since_seq`` 起循环读 :meth:`RunEventStore.list`,直到
           某页不满一页为止,逐行发出。live 分支**不做**分页截断 —— 截断是
           replay 分支的语义。
        2. **挂实时流**:``seq <= last`` 丢弃(补库阶段已经发过),**但落在
           ``missing`` 名单里的照发**(见第 5 条);``seq == last + 1`` 直接发,
           并把 pending 里紧接着的连续段一并排空;``seq > last + 1`` 先进
           pending,**不要立刻当缺口处理**(见 :data:`_REORDER_WINDOW`);
           无 seq 的 token 帧直接放行。
        3. **泄压**,两个维度**任一**触发:(a) ``len(pending)`` 超过重排窗口,
           或 (b) 跳号宽度 ``min(pending) - last - 1`` 超过重排窗口。触发后才
           认定 ``last + 1`` 起真的缺了 —— 去库里把 ``last+1 .. min(pending)-1``
           补齐(补到多少算多少),再排空 pending 里连续的部分。
           只有 (a) 的话,「跳号之后 run 恰好安静下来」永远撑不爆窗口,缺的那段
           在这条连接上根本不补 —— 而服务端补齐正是本项对外承诺的东西。
        4. 补不齐的那些 seq 记进 ``missing``,打一条 warning,不抛异常。
           bridge 缓冲区只有 256 帧且 drop-oldest,而落库走攒批后台 writer,
           所以「补库读完 → 挂上实时流」这个缝里确实可能有帧既不在缓冲区、
           也还没落库;这一步是把它变成**可观测的 warning** 而不是静默丢帧。
        5. ``missing`` 是「写过检讨但还没死心」的名单:后到的帧只要在里面就
           照发,不因为 ``seq <= last`` 被丢。只有 (b) 而没有这份名单会**重新
           引入它本来要避免的伤害** —— 并发 worker 上限可配到 64,真有可能 40
           帧同时在飞、跳号 40 却一帧没丢,那时查库(攒批 writer 还没落盘)拿
           不到,等真帧到达又被 ``<= last`` 丢掉。**我们可以判断错,但不能因为
           判断错就把真实存在过的帧扔掉**;代价只是它可能乱序到达,而客户端
           本来就按 seq 排。
        6. **end 之前**把 pending 里剩下的按 seq 升序全放出去,别吞。

        Disconnect is handled via the iterator's GeneratorExit when
        the StreamingResponse is cancelled; the bridge subscription
        naturally tears down.
        """
        last = since_seq if since_seq is not None else -1
        pending: dict[int, StreamEvent] = {}
        #: 回填补不齐、已经被「写掉」的 seq。后到的帧只要在这里面就照发 ——
        #: 判断可以错,但不能因为判断错就把真实存在过的帧扔掉。
        missing: set[int] = set()

        async def _drain_pending() -> AsyncIterator[bytes]:
            """把 pending 里从 ``last + 1`` 起连续的那一段排空。"""
            nonlocal last
            while last + 1 in pending:
                entry = pending.pop(last + 1)
                yield format_sse(entry.event, entry.data, event_id=entry.id)
                last += 1

        # 1. 补库。
        while True:
            rows = await _list_page(last)
            for row in rows:
                yield format_sse(
                    row.event_name, row.data, event_id=f"{row.created_at_ms}-{row.seq}"
                )
                last = row.seq
            if len(rows) < MAX_LIST_LIMIT:
                break

        # 2. 挂实时流。
        async for entry in stream_bridge.subscribe(run_id, heartbeat_interval=15.0):
            if entry is HEARTBEAT_SENTINEL:
                yield b": heartbeat\n\n"
                continue
            if is_end(entry):
                # 4. 压在窗口里的帧不能跟着流一起消失。
                for pending_seq in sorted(pending):
                    leftover = pending.pop(pending_seq)
                    yield format_sse(leftover.event, leftover.data, event_id=leftover.id)
                # Task 5 will surface ``entry.data``'s terminal status here;
                # for now the wire format is unchanged (``data: null``).
                yield format_sse("end", None)
                return

            seq = _seq_of(entry)
            if seq is None:
                # token 帧:一次性预览,重复或缺失都无害 —— 原样放行。
                yield format_sse(entry.event, entry.data, event_id=None)
                continue
            if seq in missing:
                # 5. 之前判它丢了、还写了检讨 —— 它自己回来了,照发。
                missing.discard(seq)
                yield format_sse(entry.event, entry.data, event_id=entry.id)
                continue
            if seq <= last:
                continue  # 补库阶段已经发过
            if seq > last + 1:
                pending[seq] = entry
                target = min(pending)
                # 3. 泄压两个维度,任一触发才认定 last + 1 是真缺口:
                #    (a) 攒的帧数超过窗口;(b) 跳号宽度超过窗口 —— 后者管
                #    「跳号之后 run 恰好安静下来」那种永远撑不爆 (a) 的情况。
                if len(pending) > _REORDER_WINDOW or target - last - 1 > _REORDER_WINDOW:
                    for row in await _list_page(last):
                        if row.seq >= target:
                            break
                        yield format_sse(
                            row.event_name, row.data, event_id=f"{row.created_at_ms}-{row.seq}"
                        )
                        last = row.seq
                    if last + 1 < target:
                        logger.warning(
                            "live_stream.gap_unfilled run_id=%s missing_from=%s missing_to=%s",
                            run_id,
                            last + 1,
                            target - 1,
                        )
                        # 4. 补不齐的记进名单再跨过去 —— 不跨的话 pending 永远
                        #    排不空,后面每一帧都会再查一次库;记名单是为了它们
                        #    后到时还能被认出来(第 5 条)。
                        missing.update(range(last + 1, target))
                        last = target - 1
                    async for chunk in _drain_pending():
                        yield chunk
                continue

            # seq == last + 1
            yield format_sse(entry.event, entry.data, event_id=entry.id)
            last = seq
            async for chunk in _drain_pending():
                yield chunk

    return _stream_replay() if is_terminal else _stream_live()
