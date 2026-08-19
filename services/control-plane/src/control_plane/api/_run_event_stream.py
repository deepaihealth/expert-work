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
from typing import Any
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
from orchestrator.sse import SYSTEM_PROMPT_EVENT, end_frame_data, format_sse

logger = logging.getLogger(__name__)

#: 对外平面(API key)恒不可见的帧集合 —— 目前只有 ``system_prompt``
#: (PR-A.3 §十.1 服务端合成的系统提示词全文,是控制台调试专用的可观测性
#: 帧,不是产品输出)。与 ``orchestrator.sse.sse_consumer`` 的同名参数同源
#: 常量,两条对外调用路径(这个模块的回放/live-接合 + 那个模块的
#: stream-mode SSE consumer)因此共享同一份"对外隐藏什么"的定义,不会分叉。
EXTERNAL_HIDDEN_EVENTS: frozenset[str] = frozenset({SYSTEM_PROMPT_EVENT})

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


#: CodeQL 的 ``py/log-injection`` 会盯上本模块三条 ``logger.warning`` —— 它追踪
#: ``since_seq`` 这个 query 参数流进日志。**判定为误报,理由逐项可核**:
#:
#: * ``since_seq`` 的声明是 ``Annotated[int | None, Query(ge=0)]`` —— pydantic
#:   校验过的整数,不是字符串;由它派生的 ``last`` / ``lo`` / ``hi`` / ``seq``
#:   同样是整数,格式符也已收紧成 ``%d``(真混进字符串会当场抛错,而不是默默拼进去)。
#: * ``run_id`` 是路由上的 ``UUID`` 路径参数,由 FastAPI 解析成 ``UUID`` 对象。
#: * ``reason`` 在全部四个调用点都是模块内的字面量常量,不接受外部输入。
#:
#: 日志注入的前提是把换行塞进日志行伪造条目;上述取值里没有任何一个能承载换行。
#: 若将来这三条日志新增了**字符串**参数,必须重新评估并删掉对应的抑制注释。


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
    hide_events: frozenset[str] = frozenset(),
) -> EventStreamPlan:
    """Return the SSE byte producer for one run, plus the replay cursor.

    收的是 ``run_status`` 而不是 ``is_terminal``:终态与否**在函数内部推导**,
    所以只有一处可能弄错;而回放分支的 ``end`` 帧还需要这个状态本身
    (P3 PR-1 Task 5 —— 取消与答完必须可区分)。

    ``hide_events``(PR-A.3 Task 8)—— 对外平面传 :data:`EXTERNAL_HIDDEN_EVENTS`
    过滤掉 console-only 帧(如 ``system_prompt``)。过滤**只**发生在
    ``_encode``(把一条落库记录 / 一帧实时事件编码成 SSE 字节的唯一一点)——
    分页游标(``next_seq``)、``truncated`` 判定、``_stream_live`` 的去重
    (``last``)与缺口回填(``holes``)全部继续用未过滤的记录计算,被过滤帧
    因此不会打乱这些不变式:即便它是页里最后一条记录,``next_seq`` /
    ``truncated`` / ``end`` 的判定依旧基于真实的原始行,只是这一条本身不会
    被写进 wire。控制台调用方(``runs.py`` 的 console 回放 / live)不传这个
    参数,继续拿到未过滤的全量帧。

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

    def _encode(event_name: str, data: Any, *, event_id: str | None) -> list[bytes]:
        """编码一条记录 / 一帧实时事件成 SSE 字节 —— 对外过滤在**这一点且只
        在这一点**发生:``event_name in hide_events`` 时不产出字节,调用方的
        seq 游标(``last`` / ``next_seq`` / ``holes``)照旧照真实记录推进,
        不受影响。"""
        if event_name in hide_events:
            return []
        return [format_sse(event_name, data, event_id=event_id)]

    async def _stream_replay(
        rows: Sequence[RunEventRecord], next_seq: int | None
    ) -> AsyncIterator[bytes]:
        """把已经读好的一页帧吐出去;截断时以 ``truncated`` 收尾。"""
        for row in rows:
            for chunk in _encode(
                row.event_name,
                row.data,
                event_id=f"{row.created_at_ms}-{row.seq}",
            ):
                yield chunk
        if next_seq is not None:
            # 截断 —— **不发 end**。以前这里补一个 end,客户端会以为流正常结束,
            # 把后面的帧静默丢掉。``truncated`` 帧与 ``X-Expert-Work-Next-Seq``
            # 头同时给:**中间代理会剥掉不认识的响应头**,而 body 里的帧不会被剥,
            # 所以信号放在流里比放在头里稳;头照发,能读到的客户端直接用。
            # (别把理由写成"浏览器 EventSource 读不到响应头"—— EventSource 设不了
            # Authorization / API-key 头,本来就用不了这套 API,那等于宣称一个
            # 我们并不提供的能力。)
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

        def _gap_frames(seqs: set[int], *, reason: str) -> list[bytes]:
            """``reason`` 只进日志,不进 wire —— 客户端不需要区分 ``gap`` 的来源
            (处置方式都是"这段在这条连接上拿不到"),但排障时分得清是
            "落库空洞被判死刑" 还是 "缺口分支翻页也补不齐" 能省一轮猜。"""
            frames: list[bytes] = []
            for lo, hi in _merge_ranges(seqs):
                logger.warning(  # codeql[py/log-injection]
                    "live_stream.gap run_id=%s from=%d to=%d reason=%s",
                    run_id,  # codeql[py/log-injection]
                    lo,  # codeql[py/log-injection]
                    hi,  # codeql[py/log-injection]
                    reason,
                )
                frames.append(format_sse("gap", {"from": lo, "to": hi}))
            return frames

        def _flush_holes_below(bound: int) -> list[bytes]:
            """``< bound`` 的洞再也不会从 bridge 补上了 —— 冲成 ``gap`` 帧。"""
            doomed = {h for h in holes if h < bound}
            holes.difference_update(doomed)
            return _gap_frames(doomed, reason="hole_passed")

        def _record_holes(end_exclusive: int) -> list[bytes]:
            """记下 ``(last, end_exclusive)`` 这段落库空洞;装不下的立刻冲成 gap。

            **起点不由调用方给** —— 它恒等于 ``last + 1``。这不是省一个参数,是把
            "``holes`` 里的号恒 ``< last``"这条不变式做成**结构性**的:消费侧
            ``if seq in holes`` 之所以能排在 ``if seq <= last`` 前面,靠的正是它。
            起点若可由调用方指定,将来有人记进一个 ``>= last`` 的号,补洞分支会
            重发一帧却**不推进 ``last``**,下一帧就走进一个虚假的缺口分支。
            (原本想在这里加一句 ``assert`` 钉住,但本仓 lint 禁止 src 用 assert
            —— 改成结构上不可能发生,比断言更彻底。)

            淘汰策略是**丢老的、留新的** —— 依据是 bridge 的 256 帧缓冲里只可能
            还留着最新的,老洞就算继续跟踪也永远等不到补发,不如立刻告诉客户端。
            裁剪要对 ``holes`` **整体**做,不能只裁新进来的这一段:只裁新段会
            把还可能补上的新洞判死刑、却把补不上的老洞留着,方向正好反过来。
            """
            lo = last + 1
            if end_exclusive <= lo:
                return []
            frames: list[bytes] = []
            # 超过上限的那一段直接一帧 gap 带过,**不物化成 set** ——
            # 所以百万级的洞也只花常数内存。
            if end_exclusive - lo > _MAX_TRACKED_HOLES:
                cut = end_exclusive - _MAX_TRACKED_HOLES
                logger.warning(  # codeql[py/log-injection]
                    "live_stream.holes_overflow run_id=%s from=%d to=%d",
                    run_id,  # codeql[py/log-injection]
                    lo,  # codeql[py/log-injection]
                    cut - 1,  # codeql[py/log-injection]
                )
                frames.append(format_sse("gap", {"from": lo, "to": cut - 1}))
                lo = cut
            holes.update(range(lo, end_exclusive))
            if len(holes) > _MAX_TRACKED_HOLES:
                evicted = set(sorted(holes)[: len(holes) - _MAX_TRACKED_HOLES])
                holes.difference_update(evicted)
                frames.extend(_gap_frames(evicted, reason="holes_overflow"))
            return frames

        # 1. 补库 —— 跳号不能让 last 无声推过去。
        while True:
            rows = await _list_page(last)
            for row in rows:
                for chunk in _record_holes(row.seq):
                    yield chunk
                for chunk in _encode(
                    row.event_name, row.data, event_id=f"{row.created_at_ms}-{row.seq}"
                ):
                    yield chunk
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
                for chunk in _gap_frames(holes, reason="hole_unfilled_at_end"):
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
                for chunk in _encode(entry.event, entry.data, event_id=entry.id):
                    yield chunk
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
                        for chunk in _record_holes(row.seq):
                            yield chunk
                        for chunk in _encode(
                            row.event_name, row.data, event_id=f"{row.created_at_ms}-{row.seq}"
                        ):
                            yield chunk
                        last = row.seq
                    if len(rows) < MAX_LIST_LIMIT:
                        break
                if last + 1 < seq:
                    logger.warning(  # codeql[py/log-injection]
                        "live_stream.gap run_id=%s from=%d to=%d reason=%s",
                        run_id,  # codeql[py/log-injection]
                        last + 1,  # codeql[py/log-injection]
                        seq - 1,  # codeql[py/log-injection]
                        "backfill_short",
                    )
                    yield format_sse("gap", {"from": last + 1, "to": seq - 1})

            for chunk in _encode(entry.event, entry.data, event_id=entry.id):
                yield chunk
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
