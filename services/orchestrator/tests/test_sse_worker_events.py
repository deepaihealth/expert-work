"""B2 — run_agent 注入 worker sink:发布 + 持久化 + 并发 seq."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.runtime.runs import DisconnectMode, RunManager, RunRecord
from expert_work.runtime.runs.event_store import InMemoryRunEventStore
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator.sse import run_agent
from orchestrator.tools._worker_events import WORKER_EVENT_SINK_KEY


class _YieldingBridge(InMemoryStreamBridge):
    """Test-only bridge that yields to the event loop before publishing.

    ``InMemoryStreamBridge.publish`` never actually suspends (no real I/O),
    so concurrent ``sink()`` calls under ``asyncio.gather`` never interleave
    — the vacuous-test bug this class exists to fix. Forcing a genuine
    ``await`` here makes concurrent ``_publish_worker`` invocations actually
    interleave at their own internal await point, so a regression that
    splits the ``seq`` read from its write-back across that await (a
    classic TOCTOU: read ``event_seq``, await, then commit the increment)
    manifests as a duplicate-seq collision the in-memory event store
    raises on. A merely *late but still atomic* allocation (both the read
    and the increment happening back-to-back after the await, with no
    await between them) is not racy under single-threaded cooperative
    asyncio — the danger is specifically a split read/write, matching
    ``_publish_worker``'s own guarding comment in ``sse.py``.

    P3 PR-1 Task 2.5 —— 额外记录每次 ``publish`` 收到的 ``seq`` 和帧本身。撞号在
    生产链路上会先被后台 persist writer 的 H-7 swallow 吃掉(``append_batch`` 抛
    ``duplicate seq=...`` → 整批丢 → 只剩一条 warning),所以**落库行里永远不会
    出现重复 seq**;分配出来、交给 bridge 的那串号是撞号唯一可以被直接观察到的
    地方,断言的失败信息因此才能直接点名撞的是哪个号,而不是去翻日志。
    """

    def __init__(self, *, queue_maxsize: int = 256) -> None:
        super().__init__(queue_maxsize=queue_maxsize)
        #: 每次 ``publish`` 收到的 ``seq``,按到达 bridge 的顺序(token 帧为 ``None``)。
        self.published_seqs: list[int | None] = []
        #: ``(event, data)`` 同序 —— 用来验证这个桩确实还在强制交错。
        self.published_frames: list[tuple[str, Any]] = []

    async def publish(self, run_id: UUID, event: str, data: Any, *, seq: int | None = None) -> None:
        await asyncio.sleep(0)  # 让出事件循环 — 强制并发 sink 交错
        self.published_seqs.append(seq)
        self.published_frames.append((event, data))
        await super().publish(run_id, event, data, seq=seq)


async def _new_record(rm: RunManager) -> RunRecord:
    return await rm.create(
        run_id=uuid4(),
        thread_id=uuid4(),
        tenant_id=uuid4(),
        on_disconnect=DisconnectMode.CANCEL,
    )


class _WorkerGraph:
    """astream 期间经注入的 sink 发 worker 帧(模拟 child run 桥接)."""

    def __init__(self, frames: list[dict[str, Any]], *, concurrent: bool = False) -> None:
        self.frames = frames
        self.concurrent = concurrent

    async def astream(
        self, input: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del input, stream_mode
        sink = config["configurable"][WORKER_EVENT_SINK_KEY]
        if self.concurrent:
            await asyncio.gather(*(sink(f) for f in self.frames))
        else:
            for frame in self.frames:
                await sink(frame)
        yield {"agent": {"step_count": 1}}


class _TwoCoroutineSinkGraph:
    """两个并发协程,各自顺序调 sink ``n`` 次 —— 每帧都走 ``_publish_frame``。

    ``_WorkerGraph(concurrent=True)`` 是 N 个协程各调一次;这里是 2 个协程各调
    N 次,更贴近真实的并发 worker(每个 worker 顺序发自己的 start/update/end)。
    """

    def __init__(self, n: int) -> None:
        self.n = n

    async def astream(
        self, input: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del input, stream_mode
        sink = config["configurable"][WORKER_EVENT_SINK_KEY]

        async def _burst(worker_id: str) -> None:
            for i in range(self.n):
                await sink({"worker_id": worker_id, "kind": "update", "wseq": i})

        await asyncio.gather(_burst("a"), _burst("b"))
        yield {"agent": {"step_count": 1}}


@pytest.mark.asyncio
async def test_concurrent_publish_frame_allocates_a_contiguous_seq_range() -> None:
    """P3 PR-1 —— 钉住「seq 同步分配不跨 await」。

    ``_publish_frame`` 先同步 ``_alloc_seq()`` 再 ``await bridge.publish``。把
    自增挪到 await 之后(经典 TOCTOU:读号 → await → 才写回),两个协程会读到
    同一个 ``event_seq``。

    **观察点是 bridge 收到的 seq,不是落库行**(Task 2.5 的加固点)。撞号在生产
    链路上先被后台 persist writer 的 H-7 swallow 吃掉:``append_batch`` 抛
    ``duplicate seq=...`` → 整批丢 → 只剩一条 warning。也就是说**落库行里永远
    不会出现重复 seq**,拿落库行做「无重复」断言是恒真的假验证;它只能间接红在
    「行数少了」上,归因还得去翻日志。分配出来交给 bridge 的那串号才是撞号唯一
    可直接观察的地方,失败信息因此能直接点名撞的是哪个号。落库行的连续性作为
    「撞号会变成静默丢帧」这个后果层,保留在最后一组断言里。

    **本测试必须用强制交错的桩 ``_YieldingBridge``,换成裸
    ``InMemoryStreamBridge`` 会退化成恒绿** —— 裸 bridge 的 ``publish`` 全程不
    挂起,``asyncio.gather`` 下两个协程根本不交错,TOCTOU 永远不发生。下面两条
    元断言把这个前提钉死:一条认桩的身份,一条认桩**真的还在**强制交错(光认
    身份挡不住有人把桩里那句 ``await asyncio.sleep(0)`` 删掉)。
    """
    n = 8
    bridge = _YieldingBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryRunEventStore()  # append 对重复 (run_id, seq) 直接 raise

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_TwoCoroutineSinkGraph(n),
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        event_store=store,
    )

    # metadata 1 帧 + 两个协程各 n 帧 worker + astream 的 1 帧 updates
    expected_total = 2 * n + 2

    # 元断言 1 —— 桩没被换成裸 bridge。
    assert isinstance(bridge, _YieldingBridge), (
        "本测试依赖强制交错的桩;换成裸 InMemoryStreamBridge 会退化成恒绿的假验证"
    )
    # 元断言 2 —— 桩**真的还在**交错:两个协程的 worker 帧必须交替到达 bridge。
    # 裸 bridge(或桩里的 sleep(0) 被删)下顺序是 aaaa…bbbb,transitions == 1。
    worker_order = [d["worker_id"] for name, d in bridge.published_frames if name == "worker"]
    transitions = sum(1 for x, y in itertools.pairwise(worker_order) if x != y)
    assert transitions >= n, (
        f"两个协程的帧没有真正交错(transitions={transitions},order={worker_order})"
        " —— 强制交错的前提没了,本测试已退化成恒绿"
    )

    # 主断言 —— 分配出去的号无重复,失败信息直接点名撞的是哪个号。
    allocated = [s for s in bridge.published_seqs if s is not None]
    duplicated = sorted({s for s in allocated if allocated.count(s) > 1})
    assert not duplicated, f"seq 撞号:{duplicated};分配给 bridge 的号依次为 {allocated}"
    assert sorted(allocated) == list(range(expected_total))

    # 后果层 —— 撞号会被 H-7 吃成静默丢帧,落库行必须仍然连续完整。
    events = await store.list(run_id=record.run_id, limit=500)
    assert len(events) == expected_total
    assert [e.seq for e in events] == list(range(expected_total))  # 无重复、无缺口
    assert len([e for e in events if e.event_name == "worker"]) == 2 * n


@pytest.mark.asyncio
async def test_worker_frames_published_and_persisted_with_monotonic_seq() -> None:
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryRunEventStore()
    frames = [
        {"worker_id": "w1", "kind": "start", "wseq": 0},
        {"worker_id": "w1", "kind": "end", "wseq": 1},
    ]

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_WorkerGraph(frames),
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        event_store=store,
    )

    events = await store.list(run_id=record.run_id, limit=500)
    worker_rows = [e for e in events if e.event_name == "worker"]
    assert [r.data["kind"] for r in worker_rows] == ["start", "end"]
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)  # 无重复


@pytest.mark.asyncio
async def test_concurrent_worker_frames_do_not_collide_on_seq() -> None:
    bridge = _YieldingBridge()  # 强制真交错,否则并发 sink 永不 interleave
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryRunEventStore()  # append 对重复 (run_id, seq) 直接 raise
    pairs = [("a", "start"), ("b", "start"), ("a", "end"), ("b", "end")]
    frames = [{"worker_id": w, "kind": k, "wseq": i} for i, (w, k) in enumerate(pairs)]

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_WorkerGraph(frames, concurrent=True),
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        event_store=store,
    )

    events = await store.list(run_id=record.run_id, limit=500)
    worker_rows = [e for e in events if e.event_name == "worker"]
    assert len(worker_rows) == 4
    assert len({e.seq for e in events}) == len(events)
