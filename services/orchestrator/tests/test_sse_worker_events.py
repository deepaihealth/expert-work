"""B2 — run_agent 注入 worker sink:发布 + 持久化 + 并发 seq."""

from __future__ import annotations

import asyncio
import itertools
import json
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
    interleave, which is the precondition for observing anything at all about
    concurrent numbering.

    P3 PR-1 Task 3R —— 额外做两件事:

    1. 记录每帧「bridge 分配的号 ↔ 帧内容」的对应关系。发号权归 bridge 之后,
       生产者侧唯一还能出错的地方是**没把返回值当回事**(自己另发一个号、或者
       把号配错帧),而那种错误只有把这份对应关系与落库行逐帧比对才看得出来 ——
       光看「号的集合对不对」是看不出来的。
    2. **按帧给出不等长的延迟**。真 bridge(Redis / 网络)每次调用耗时不同,
       所以生产者的**调用顺序不等于帧到达 bridge 的顺序**。这一条是判据的命门:
       延迟等长时,单线程事件循环的 FIFO 就绪队列会让"生产者自己发号"与
       "bridge 发号"给出**一模一样**的结果 —— 那种变异就会存活,测试实际上什么
       也没测到(本仓库记录过的恒绿形态)。实测:延迟等长时变异存活,不等长时
       立刻红。
    """

    #: worker ``a`` 的帧比别的帧多让出几轮 —— 制造"调用顺序 ≠ 到达顺序"。
    _SLOW_WORKER = "a"
    _SLOW_YIELDS = 3
    _FAST_YIELDS = 1

    def __init__(self, *, queue_maxsize: int = 256) -> None:
        super().__init__(queue_maxsize=queue_maxsize)
        #: ``(event, data, bridge 分配的 seq)``,按帧进入 bridge 的顺序。
        self.assigned: list[tuple[str, Any, int]] = []

    def _yields_for(self, event: str, data: Any) -> int:
        if (
            event == "worker"
            and isinstance(data, dict)
            and data.get("worker_id") == self._SLOW_WORKER
        ):
            return self._SLOW_YIELDS
        return self._FAST_YIELDS

    async def publish(self, run_id: UUID, event: str, data: Any) -> int:
        for _ in range(self._yields_for(event, data)):
            await asyncio.sleep(0)  # 让出事件循环 — 强制并发 sink 交错(且不等长)
        seq = await super().publish(run_id, event, data)
        self.assigned.append((event, data, seq))
        return seq


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
async def test_persisted_seq_is_exactly_what_the_bridge_assigned() -> None:
    """P3 PR-1 Task 3R —— 钉住新不变式:**落库的号必须是 bridge 发的那个号**。

    这条测试取代了 Task 2.5 的 ``test_concurrent_publish_frame_allocates_a_
    contiguous_seq_range``。那条钉的是「seq 同步分配抢在 await 之前」——
    发号权归 bridge 之后,生产者根本不再持有计数器,那条不变式**已不存在**,
    留着它就是一条测不到任何东西的绿灯。

    新不变式是生产者侧现在唯一还能出错的地方:``_publish_frame`` 必须把
    ``await bridge.publish(...)`` 的返回值原样交给 ``_enqueue_event``。
    自己另发一个号、或者把号配错帧,live 帧 id 与 durable 行就再次对不上 ——
    正是本 PR 头号缺陷 C 的形态。所以断言是**逐帧**的对应关系,不是「号的集合
    对不对」(集合相等挡不住"号配错了帧"这一类)。

    并发**且延迟不等长**是判据的一部分:单协程下、或者每帧延迟一样长时,生产者
    自己发号也会给出一模一样的结果(FIFO 就绪队列把调用顺序原样保住),错配根本
    不会发生 —— 实测过,那种条件下"生产者自己发号"的变异存活。``_YieldingBridge``
    给 worker ``a`` 的帧更长的延迟,调用顺序与到达 bridge 的顺序因此分岔。
    下面三条元断言钉住这个前提。
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
        "本测试依赖强制交错的桩;换成裸 InMemoryStreamBridge 并发不会交错,错配就藏得住"
    )
    # 元断言 2 —— 桩**真的还在**交错:两个协程的 worker 帧必须交替进 bridge。
    worker_order = [d["worker_id"] for name, d, _seq in bridge.assigned if name == "worker"]
    transitions = sum(1 for x, y in itertools.pairwise(worker_order) if x != y)
    assert transitions >= n // 2, (
        f"两个协程的帧没有真正交错(transitions={transitions},order={worker_order})"
        " —— 强制交错的前提没了,本测试已退化"
    )
    # 元断言 3 —— 桩的延迟**确实不等长**:调用顺序 ≠ 到达 bridge 的顺序。
    # 等长的话本测试测不出"生产者自己发号"(实测该变异会存活)。
    assert bridge._SLOW_YIELDS != bridge._FAST_YIELDS, (
        "桩的延迟被改成等长了 —— 调用顺序会等于到达顺序,本测试退化成恒绿"
    )

    # bridge 发的号:锁内原子分配 → 无重复、无缺口。
    assigned_seqs = [seq for _name, _data, seq in bridge.assigned]
    duplicated = sorted({s for s in assigned_seqs if assigned_seqs.count(s) > 1})
    assert not duplicated, f"bridge 发号撞号:{duplicated};依次为 {assigned_seqs}"
    assert sorted(assigned_seqs) == list(range(expected_total))

    # **主断言** —— 逐帧对应:每一帧落库行的 seq 必须等于 bridge 给这一帧的号。
    assigned_by_frame = {
        (name, json.dumps(data, sort_keys=True, default=str)): seq
        for name, data, seq in bridge.assigned
    }
    rows = await store.list(run_id=record.run_id, limit=500)
    persisted_by_frame = {
        (r.event_name, json.dumps(r.data, sort_keys=True, default=str)): r.seq for r in rows
    }
    assert len(assigned_by_frame) == expected_total, "帧内容不唯一,逐帧比对失效"
    assert persisted_by_frame == assigned_by_frame, (
        "落库的号与 bridge 发的号对不上(生产者又在自己发号 / 号配错了帧)"
    )
    assert len(rows) == expected_total
    assert [r.seq for r in rows] == list(range(expected_total))


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
