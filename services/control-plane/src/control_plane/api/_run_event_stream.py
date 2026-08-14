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
from dataclasses import dataclass
from uuid import UUID

from expert_work.runtime.runs import RunEventRecord, RunEventStore, RunStatus
from expert_work.runtime.runs.schemas import TERMINAL_RUN_STATUSES
from expert_work.runtime.runs.store import MAX_LIST_LIMIT
from expert_work.runtime.stream_bridge import (
    HEARTBEAT_SENTINEL,
    StreamBridge,
    StreamEvent,
    is_end,
)
from orchestrator.sse import end_frame_data, format_sse

logger = logging.getLogger(__name__)

#: ``RunStatus`` → 对外 ``end`` 帧的 status。词表与 ``sse.py`` 的
#: ``_EXTERNAL_END_STATUS`` 同源(见 ``EXTERNAL_END_STATUSES``):
#: ``PAUSED`` 必须独立 —— 它是"等人审批,对话还会继续",不是错误,客户端要
#: 弹审批界面而不是报错;``TIMEOUT`` 对客户端而言就是失败。
_RUN_STATUS_END_STATUS: dict[RunStatus, str] = {
    RunStatus.SUCCESS: "success",
    RunStatus.PAUSED: "paused",
    RunStatus.INTERRUPTED: "interrupted",
    RunStatus.ERROR: "error",
    RunStatus.TIMEOUT: "error",
}


#: live 接合跟踪「落库空洞」的容量上限。超出的部分立刻冲成 ``gap`` 帧、不再跟踪,
#: 所以这个集合永远有界。
_MAX_TRACKED_HOLES = 4096


def _merge_ranges(seqs: set[int]) -> list[tuple[int, int]]:
    """把一组 seq 合并成连续闭区间 —— 一个洞段只发一帧 ``gap``。"""
    merged: list[tuple[int, int]] = []
    for seq in sorted(seqs):
        if merged and seq == merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], seq)
        else:
            merged.append((seq, seq))
    return merged


def _seq_of(entry: StreamEvent) -> int | None:
    """从帧 id 里解析落库 ``seq``;``None`` 表示这帧不参与接合。

    ``entry.id is None`` 是一次性帧 —— 不可回放、不占号(今天只有 ``token``,
    见 :meth:`StreamBridge.publish_ephemeral`)。
    id 形状不认识时同样返回 ``None``:放行总比把它当成某个号去参与去重安全。
    """
    if entry.id is None:
        return None
    try:
        return int(entry.id.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        logger.warning("live_stream.unparsable_frame_id id=%s", entry.id)
        return None


@dataclass(frozen=True)
class EventStreamPlan:
    """一次 SSE 响应的构造结果 —— 字节生成器 + 回放游标。

    ``next_seq`` **只在回放被截断时**非 ``None``。它是客户端下一次请求应当原样
    传回来的 ``since_seq`` 值(也就是本页最后一帧的 seq),调用方据此加一个
    ``X-Expert-Work-Next-Seq`` 响应头。

    为什么要有这个 dataclass:HTTP 响应头在流开始之前就发完了,而"这一页是不
    是被截断了"只有读完一页才知道 —— **在生成器体内没有任何办法再改响应头**。
    所以第一页在返回迭代器**之前**就读掉,``next_seq`` 因此在构造
    ``StreamingResponse`` 时已知。附带好处:数据库出错变成正常的 500 JSON,
    而不是一个已经开始流式输出、半截截断的 body。
    """

    producer: AsyncIterator[bytes]
    next_seq: int | None


async def build_event_producer(
    *,
    run_id: UUID,
    run_status: RunStatus,
    event_store: RunEventStore | None,
    stream_bridge: StreamBridge,
    since_seq: int | None,
    scope: Callable[[], AbstractAsyncContextManager[None]] | None,
) -> EventStreamPlan:
    """Return the SSE byte producer for one run, plus the replay cursor.

    收的是 ``run_status`` 而不是 ``is_terminal``:终态与否**在函数内部推导**,
    所以只有一处可能弄错;而回放分支的 ``end`` 帧还需要这个状态本身
    (P3 PR-1 Task 5 —— 取消与答完必须可区分)。

    * Terminal run → :meth:`RunEventStore.list`,**一页**(``MAX_LIST_LIMIT``),
      按 seq 排序。后面还有的话流以 ``truncated`` 帧收尾而**不发 ``end``** ——
      流并没有结束,客户端得带 ``next_seq`` 再来一次。
    * Active run → 先把 ``since_seq`` 之后的落库帧补齐,再挂
      :meth:`StreamBridge.subscribe` 的实时流,按 seq 去重 + 回填真缺口
      (见 ``_stream_live`` 的 docstring)。live 分支**不截断**。

    ``scope`` is a *factory* for a tenant-scope context manager, not an
    already-constructed one — ``applied_scope(...)`` returns a
    single-use ``_AsyncGeneratorContextManager`` (``__aenter__`` a second
    time raises ``RuntimeError: generator didn't yield``), and both branches
    read the store more than once (replay: 一页 + 一次"还有没有"的探测;
    live: 补库分页 + 缺口回填). The console
    caller passes ``lambda: applied_scope(scope)`` (its DB read must stay
    bound to the resolved target tenant, not the request middleware's
    home-tenant GUC); the external caller has no cross-tenant concept and
    passes ``None`` explicitly — there is no default, so a caller cannot
    silently forget this and fall back to an unscoped read.
    """

    async def _list_page(
        after: int | None, *, limit: int = MAX_LIST_LIMIT
    ) -> Sequence[RunEventRecord]:
        """读一页落库帧(``seq > after``,最多 ``limit`` 条)。

        ``scope`` 工厂**每次读都要重新调**:它返回的 CM 是单次可用的,复用会
        炸 ``generator didn't yield``。
        """
        if event_store is None:
            return []
        if scope is not None:
            async with scope():
                return await event_store.list(run_id=run_id, since_seq=after, limit=limit)
        return await event_store.list(run_id=run_id, since_seq=after, limit=limit)

    async def _stream_replay(
        rows: Sequence[RunEventRecord], next_seq: int | None
    ) -> AsyncIterator[bytes]:
        """把已经读好的一页帧吐出去;截断时以 ``truncated`` 收尾。"""
        for row in rows:
            yield format_sse(
                row.event_name,
                row.data,
                event_id=f"{row.created_at_ms}-{row.seq}",
            )
        if next_seq is not None:
            # 截断 —— **不发 end**。以前这里补一个 end,客户端会以为流正常结束,
            # 把后面的帧静默丢掉。``truncated`` 帧与 ``X-Expert-Work-Next-Seq``
            # 头同时给:浏览器 ``EventSource`` 读不到响应头,只给 header 的信号
            # 对一整类客户端不可用。
            yield format_sse("truncated", {"next_seq": next_seq})
            return
        yield format_sse(
            "end",
            end_frame_data(run_id=run_id, status=_RUN_STATUS_END_STATUS.get(run_status)),
        )

    async def _stream_live() -> AsyncIterator[bytes]:
        """Live attach —— 先补库,再接实时流;接合就是**去重**(Task 3R)。

        1. **补库**:从 ``since_seq`` 起循环读 :meth:`RunEventStore.list`,直到
           某页不满一页为止,逐行发出。live 分支**不做**分页截断 —— 截断是
           replay 分支的语义。
        2. **挂实时流**:无 seq 的 token 帧直接放行;``seq <= last`` 丢弃
           (补库阶段已经发过);``seq == last + 1`` 直接发。
        3. ``seq > last + 1`` 是**真缺口**,没有第二种解释 ——
           :meth:`StreamBridge.publish` 在自己的临界区里发号并入队,所以订阅者
           看到的帧顺序恒等于 seq 顺序,"先分到号的帧后进 bridge"在物理上不
           可能发生。处置:先去库里**翻页**补能补的,补不齐的那一段发一帧 ``gap``。
        4. **落库的行不一定连续**(Task 3R-fix):``_flush_batch`` 在 DB 出错时按
           H-7 立场整批吞掉(只打 warning),落库队列满时 drop-oldest,所以
           ``run_event`` 表里真的会有内部空洞。补库遇到跳号不能让 ``last`` 无声
           推过去 —— 那一段记进 ``holes``。

        ``holes`` 的处置:看到 bridge 上出现 ``seq`` 时,所有 ``< seq`` 的洞就
        **永远不会再来**(帧顺序恒等于 seq 顺序),合并成连续区间冲成 ``gap`` 帧;
        而正好等于某个洞的帧说明 bridge 缓冲区里还留着它,直接补发。``end`` 之前
        把剩下的冲干净。

        ``gap`` 帧(``{"from": N, "to": M}``,**无 ``id:``、不落库**)描述的是
        **这条连接**的状况,不是 run 的事件:这段帧在这里补不到了,不代表它们
        不存在 —— run 结束后重新回放通常能拿到。

        **``holes`` 不是被删掉的 ``missing`` 借尸还魂,别"顺手"删它。**
        ``missing`` 治的是**乱序歧义**("跳号到底是丢了还是还在路上"),那个歧义
        已经随发号权归 bridge **彻底消失**,重排窗口不会回来。``holes`` 治的是
        **落库真实空洞** —— H-7 主动吞批 / 队满 drop-oldest 的设计后果,一直存在,
        与乱序无关。而且它有界(:data:`_MAX_TRACKED_HOLES`,超出立刻冲成 ``gap``),
        不是当年那个无上限的集合。

        Disconnect is handled via the iterator's GeneratorExit when
        the StreamingResponse is cancelled; the bridge subscription
        naturally tears down.
        """
        last = since_seq if since_seq is not None else -1
        holes: set[int] = set()

        def _gap_frames(seqs: set[int]) -> list[bytes]:
            frames: list[bytes] = []
            for lo, hi in _merge_ranges(seqs):
                logger.warning("live_stream.gap run_id=%s from=%s to=%s", run_id, lo, hi)
                frames.append(format_sse("gap", {"from": lo, "to": hi}))
            return frames

        def _flush_holes_below(bound: int) -> list[bytes]:
            """``< bound`` 的洞再也不会从 bridge 补上了 —— 冲成 ``gap`` 帧。"""
            doomed = {h for h in holes if h < bound}
            holes.difference_update(doomed)
            return _gap_frames(doomed)

        def _record_holes(lo: int, end_exclusive: int) -> list[bytes]:
            """记下 ``[lo, end_exclusive)`` 这段落库空洞;装不下的立刻冲成 gap。"""
            if end_exclusive <= lo:
                return []
            frames: list[bytes] = []
            room = max(_MAX_TRACKED_HOLES - len(holes), 0)
            if end_exclusive - lo > room:
                # 老的那一段不再跟踪 —— bridge 的 256 帧缓冲里只可能还留着最新的。
                cut = end_exclusive - room
                logger.warning(
                    "live_stream.holes_overflow run_id=%s from=%s to=%s", run_id, lo, cut - 1
                )
                frames.append(format_sse("gap", {"from": lo, "to": cut - 1}))
                lo = cut
            holes.update(range(lo, end_exclusive))
            return frames

        # 1. 补库 —— 跳号不能让 last 无声推过去。
        while True:
            rows = await _list_page(last)
            for row in rows:
                for chunk in _record_holes(last + 1, row.seq):
                    yield chunk
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
                # 还没决出结果的洞不能跟着流一起消失。
                for chunk in _gap_frames(holes):
                    yield chunk
                holes.clear()
                # P3 PR-1 Task 5 —— 终局状态从 bridge 的 end 帧 data 里取
                # (``publish_end(status=...)`` 存的)。
                status = entry.data.get("status") if isinstance(entry.data, dict) else None
                yield format_sse("end", end_frame_data(run_id=run_id, status=status))
                return

            seq = _seq_of(entry)
            if seq is None:
                # token 帧:一次性预览,重复或缺失都无害 —— 原样放行。
                yield format_sse(entry.event, entry.data, event_id=None)
                continue

            # 帧顺序恒等于 seq 顺序 ⇒ 看到 seq 之后,比它小的洞判死刑。
            for chunk in _flush_holes_below(seq):
                yield chunk
            if seq in holes:
                # 落库没有它,但 bridge 缓冲区里还留着 —— 补发。
                holes.discard(seq)
                yield format_sse(entry.event, entry.data, event_id=entry.id)
                continue
            if seq <= last:
                continue  # 补库阶段已经发过
            if seq > last + 1:
                # 3. 真缺口 —— 先尽量从库里补,**翻页**直到够到 seq 或读完。
                reached = False
                while not reached:
                    rows = await _list_page(last)
                    for row in rows:
                        if row.seq >= seq:
                            reached = True
                            break
                        for chunk in _record_holes(last + 1, row.seq):
                            yield chunk
                        yield format_sse(
                            row.event_name, row.data, event_id=f"{row.created_at_ms}-{row.seq}"
                        )
                        last = row.seq
                    if len(rows) < MAX_LIST_LIMIT:
                        break
                if last + 1 < seq:
                    logger.warning(
                        "live_stream.gap run_id=%s from=%s to=%s", run_id, last + 1, seq - 1
                    )
                    yield format_sse("gap", {"from": last + 1, "to": seq - 1})

            yield format_sse(entry.event, entry.data, event_id=entry.id)
            last = seq

    if run_status not in TERMINAL_RUN_STATUSES:
        return EventStreamPlan(producer=_stream_live(), next_seq=None)

    # 回放:第一页在**返回迭代器之前**读掉,好让 next_seq 在构造
    # StreamingResponse 时就已知(响应头没法在流开始后再改)。
    rows = await _list_page(since_seq)
    next_seq: int | None = None
    if len(rows) == MAX_LIST_LIMIT:
        # **不能**用「行数 == 页大小」单独判定 —— 总帧数恰好整除页大小时会误报
        # 截断,客户端白拉一页空的。真去看后面还有没有东西。
        if await _list_page(rows[-1].seq, limit=1):
            next_seq = rows[-1].seq
    return EventStreamPlan(producer=_stream_replay(rows, next_seq), next_seq=next_seq)
