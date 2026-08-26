"""Unit tests for InMemoryStreamBridge + the make_stream_bridge factory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from expert_work.runtime.stream_bridge import (
    HEARTBEAT_SENTINEL,
    InMemoryStreamBridge,
    StreamEvent,
    is_end,
    make_stream_bridge,
)


async def _drain(
    it: AsyncIterator[StreamEvent],
    *,
    max_items: int = 100,
) -> list[StreamEvent]:
    """Helper: collect events from the iterator until the end frame or limit."""
    out: list[StreamEvent] = []
    async for ev in it:
        out.append(ev)
        if is_end(ev) or len(out) >= max_items:
            return out
    return out


@pytest.mark.asyncio
async def test_publish_subscribe_round_trip() -> None:
    bridge = InMemoryStreamBridge()
    run_id = uuid4()

    await bridge.publish(run_id, "metadata", {"agent": "demo"})
    await bridge.publish(run_id, "updates", {"step": 1})
    await bridge.publish_end(run_id, status="success")

    events = await _drain(bridge.subscribe(run_id))
    assert [e.event for e in events] == ["metadata", "updates", "__end__"]
    assert is_end(events[-1])
    assert events[0].data == {"agent": "demo"}


@pytest.mark.asyncio
async def test_bridge_order_equals_seq_order_under_concurrency() -> None:
    """**本重做的命门** —— 发号与入队原子完成,订阅者看到的帧顺序恒等于 seq 顺序。

    生产者预分配的老设计里,领号和 ``await publish`` 之间隔着一个 await,并发
    worker 一交错就乱序 —— Task 3 那一整套 pending 重排窗口 / missing 名单就是
    为收拾那个乱序而生的。这条测试证明的正是「不需要收拾」这个前提;其余的简化
    全建立在它之上。

    三个断言面,合起来才钉得住「原子」:

    1. **号本身**:并发下无重复、无缺口。
    2. **原子性**(白盒探针):一个并发观察者在每个调度点检查
       ``next_seq == 缓冲区里 numbered 帧的条数``。发号一旦挪出临界区,这两个
       数之间就会出现一个可观测的窗口 —— 这是「发号在锁内」唯一**直接**的
       可观测差异。**没有这个探针,把发号移出锁的变异会存活**:单线程事件循环
       下每个协程在发号与入队之间都恰好只让出一次,FIFO 就绪队列把相对顺序
       原样保住了,黑盒只看顺序看不出区别。
    3. **顺序**:订阅者拿到的帧 id 里的 seq 严格递增。
    """
    bridge = InMemoryStreamBridge(queue_maxsize=1024)
    run_id = uuid4()
    n = 32
    done = asyncio.Event()
    violations: list[tuple[int, int]] = []

    async def _watch() -> None:
        """并发观察者 —— 在每个调度点检查「发号器」与「已入队 numbered 帧数」一致。"""
        while not done.is_set():
            await asyncio.sleep(0)
            stream = bridge._streams.get(run_id)
            if stream is None:
                continue
            numbered = sum(1 for e in stream.events if e.id is not None)
            if stream.next_seq != numbered + stream.start_offset:
                violations.append((stream.next_seq, numbered + stream.start_offset))

    async def _burst(worker: str) -> list[int]:
        out: list[int] = []
        for i in range(n):
            await asyncio.sleep(0)  # 让两个协程在每次 publish 之间真正交错
            out.append(await bridge.publish(run_id, "worker", {"w": worker, "i": i}))
        return out

    watcher = asyncio.create_task(_watch())
    groups = await asyncio.gather(_burst("a"), _burst("b"))
    done.set()
    # ``await asyncio.gather(watcher)`` 而不是裸 ``await watcher``:CodeQL 把
    # await 一个裸名字读成"这条语句没有效果"并卡住合并(本仓既有先例)。
    await asyncio.gather(watcher)
    await bridge.publish_end(run_id, status="success")

    # 探针不变式的**前置条件**:整条 run 没触发过缓冲区溢出。溢出时
    # ``start_offset`` 会把 ephemeral 帧也数进去,``next_seq == numbered +
    # start_offset`` 就会多算 —— 今天 ``queue_maxsize=1024``、64 帧、零 ephemeral
    # 所以成立。谁改了这条测试的规模或加了 ephemeral 帧,先看这一行:那时上面的
    # violations 是**假红**,不是真 bug。
    assert bridge._streams[run_id].start_offset == 0, (
        "缓冲区溢出了 —— 原子性探针的不变式在这种条件下会多算,先修前置条件"
    )

    # 1. 号本身。
    assigned = sorted(seq for group in groups for seq in group)
    duplicated = sorted({s for s in assigned if assigned.count(s) > 1})
    assert not duplicated, f"发号撞号:{duplicated}"
    assert assigned == list(range(2 * n)), "发号有缺口"

    # 2. 原子性 —— 一次都不许被观察到「号已发、帧还没进队」。
    assert not violations, (
        f"发号与入队不再原子:观察到 {len(violations)} 次 next_seq 领先于已入队帧数,"
        f"首次 (next_seq, 已入队)={violations[0]}"
    )

    # 3. 顺序。
    events = await _drain(bridge.subscribe(run_id), max_items=1000)
    frames = [e for e in events if not is_end(e)]
    seqs = [int(f.id.rsplit("-", 1)[1]) for f in frames if f.id is not None]
    assert len(seqs) == 2 * n
    assert seqs == list(range(2 * n)), f"bridge 帧顺序 != seq 顺序(乱序回来了):{seqs}"


@pytest.mark.asyncio
async def test_ephemeral_frames_carry_no_id_and_no_seq() -> None:
    """``publish_ephemeral`` 的帧不发号、不带 ``id:``、不可回放。"""
    bridge = InMemoryStreamBridge()
    run_id = uuid4()
    assert await bridge.publish(run_id, "metadata", {"a": 1}) == 0
    await bridge.publish_ephemeral(run_id, "token", {"text": "hi"})
    # 前后两帧 numbered 的 seq 必须连续 —— 一次性帧不能占号。
    assert await bridge.publish(run_id, "updates", {"b": 2}) == 1
    await bridge.publish_end(run_id, status="success")

    got = [e async for e in bridge.subscribe(run_id, heartbeat_interval=0.05)]
    frames = [e for e in got if e.event not in ("__heartbeat__", "__end__")]
    assert [f.event for f in frames] == ["metadata", "token", "updates"]
    assert frames[1].id is None
    assert [int(f.id.rsplit("-", 1)[1]) for f in frames if f.id is not None] == [0, 1]


@pytest.mark.asyncio
async def test_seed_seq_pushes_counter_past_durable_tail() -> None:
    """HA 接管播种:发号器推过前任已落库的尾部,且**只进不退**。

    不推的话新号会与前任的行撞 ``(run_id, seq)`` 主键;能往回拨的话,一条迟到的
    播种会把发号器拨回去,同样撞主键。
    """
    bridge = InMemoryStreamBridge()
    run_id = uuid4()

    await bridge.seed_seq(run_id, next_seq=7)
    assert await bridge.publish(run_id, "updates", {"a": 1}) == 7

    await bridge.seed_seq(run_id, next_seq=3)  # 迟到的小值播种
    assert await bridge.publish(run_id, "updates", {"a": 2}) == 8, "发号器被往回拨了"


@pytest.mark.asyncio
async def test_last_event_id_resumes_after_cursor() -> None:
    bridge = InMemoryStreamBridge()
    run_id = uuid4()

    await bridge.publish(run_id, "updates", {"step": 1})
    await bridge.publish(run_id, "updates", {"step": 2})
    await bridge.publish(run_id, "updates", {"step": 3})
    await bridge.publish_end(run_id, status="success")

    full = await _drain(bridge.subscribe(run_id))
    # Reconnect from id of the 1st event — should resume from event 2 onwards
    resume_id = full[0].id
    resumed = await _drain(bridge.subscribe(run_id, last_event_id=resume_id))
    assert [e.data for e in resumed if not is_end(e)] == [{"step": 2}, {"step": 3}]


@pytest.mark.asyncio
async def test_last_event_id_unknown_replays_from_earliest_retained() -> None:
    bridge = InMemoryStreamBridge()
    run_id = uuid4()

    await bridge.publish(run_id, "updates", {"step": 1})
    await bridge.publish(run_id, "updates", {"step": 2})
    await bridge.publish_end(run_id, status="success")

    replayed = await _drain(bridge.subscribe(run_id, last_event_id="nonexistent-cursor"))
    assert [e.data for e in replayed if not is_end(e)] == [{"step": 1}, {"step": 2}]


@pytest.mark.asyncio
async def test_heartbeat_on_idle() -> None:
    bridge = InMemoryStreamBridge()
    run_id = uuid4()
    # No events published, no publish_end. Subscriber must hit heartbeat.

    async def _collect() -> list[StreamEvent]:
        out: list[StreamEvent] = []
        async for ev in bridge.subscribe(run_id, heartbeat_interval=0.05):
            out.append(ev)
            if len(out) >= 2:
                return out
        return out

    events = await asyncio.wait_for(_collect(), timeout=1.0)
    assert all(e is HEARTBEAT_SENTINEL for e in events)


@pytest.mark.asyncio
async def test_buffer_overflow_drops_oldest() -> None:
    bridge = InMemoryStreamBridge(queue_maxsize=3)
    run_id = uuid4()

    for i in range(5):
        await bridge.publish(run_id, "updates", {"step": i})
    await bridge.publish_end(run_id, status="success")

    events = await _drain(bridge.subscribe(run_id))
    payload_steps = [e.data["step"] for e in events if not is_end(e)]
    assert payload_steps == [2, 3, 4]  # 0,1 dropped (maxsize=3)


@pytest.mark.asyncio
async def test_cleanup_releases_state() -> None:
    bridge = InMemoryStreamBridge()
    run_id = uuid4()
    await bridge.publish(run_id, "updates", {"step": 1})
    assert run_id in bridge._streams

    await bridge.cleanup(run_id)
    assert run_id not in bridge._streams


@pytest.mark.asyncio
async def test_factory_memory_default() -> None:
    async with make_stream_bridge() as bridge:
        assert isinstance(bridge, InMemoryStreamBridge)


@pytest.mark.asyncio
async def test_factory_redis_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="redis"):
        async with make_stream_bridge("redis"):
            pass


@pytest.mark.asyncio
async def test_factory_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown stream_bridge backend"):
        async with make_stream_bridge("kafka"):  # type: ignore[arg-type]
            pass


@pytest.mark.asyncio
async def test_publish_end_artifacts_ride_the_end_frame() -> None:
    """产物清单契约 —— publish_end 捎带的清单在 end 帧 data 上;不传则字段缺席
    (老调用方零变化)。"""
    bridge = InMemoryStreamBridge()
    run_id = uuid4()
    manifest = [{"name": "plan.pptx", "kind": "document", "version": 1, "created_at": "t"}]
    await bridge.publish(run_id, "metadata", {})
    await bridge.publish_end(run_id, status="success", artifacts=manifest)

    events = await _drain(bridge.subscribe(run_id))
    assert is_end(events[-1])
    assert events[-1].data == {"status": "success", "artifacts": manifest}

    bridge2 = InMemoryStreamBridge()
    run2 = uuid4()
    await bridge2.publish(run2, "metadata", {})
    await bridge2.publish_end(run2, status="success")
    events2 = await _drain(bridge2.subscribe(run2))
    assert events2[-1].data == {"status": "success"}


@pytest.mark.asyncio
async def test_has_live_stream_is_publisher_fed_only() -> None:
    """PROD-1 —— 属主判别:只有发布者路径喂过的流才算「本进程有实时流」。

    订阅自动建的空流**不算** —— 否则先 attach 的连接会把后续 attach 全骗进
    一条永远没有帧的订阅(多副本下正是非属主副本的形态)。
    """
    bridge = InMemoryStreamBridge()
    run_id = uuid4()
    assert bridge.has_live_stream(run_id) is False

    # 订阅一次(拉到心跳为止)—— 流被自动创建,但仍是 unfed。
    sub = bridge.subscribe(run_id, heartbeat_interval=0.01)
    first = await anext(sub)
    assert first is HEARTBEAT_SENTINEL
    await sub.aclose()  # type: ignore[attr-defined]  # 协议声明是 AsyncIterator,实现是生成器
    assert bridge.has_live_stream(run_id) is False

    # 四条发布者路径逐一置 fed。
    await bridge.publish(run_id, "updates", {"n": 0})
    assert bridge.has_live_stream(run_id) is True

    run_ephemeral = uuid4()
    await bridge.publish_ephemeral(run_ephemeral, "token", {"text": "hi"})
    assert bridge.has_live_stream(run_ephemeral) is True

    run_end = uuid4()
    await bridge.publish_end(run_end, status="success")
    assert bridge.has_live_stream(run_end) is True

    run_seeded = uuid4()
    await bridge.seed_seq(run_seeded, next_seq=7)
    assert bridge.has_live_stream(run_seeded) is True

    # cleanup 之后回到「本进程无此 run」。
    await bridge.cleanup(run_id)
    assert bridge.has_live_stream(run_id) is False
