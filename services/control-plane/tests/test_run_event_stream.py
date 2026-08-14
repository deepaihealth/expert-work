"""Tests for the shared SSE event producer (``api/_run_event_stream.py``).

Task 4 (external-API v1 P1) extracted ``_stream_replay`` / ``_stream_live``
out of ``api/runs.py`` into ``build_event_producer`` so the console and
external endpoints share one implementation instead of two copies drifting
apart. The HTTP-level integration suites (``test_runs_api.py`` /
``test_external_events.py``) both drive **in-memory** stores that
structurally cannot observe whether the resolved tenant ``scope`` was
actually applied around the replay read: ``InMemoryRunEventStore.list``
takes no ``tenant_id`` argument at all (RLS scoping is a Postgres-only
concept, per that store's own module docstring). That means a call site
that quietly regresses from ``scope=lambda: applied_scope(scope)`` to
``scope=None`` passes every existing HTTP test unchanged — confirmed by a
code-review round on this task (Important 3): mutating the console call
site to ``scope=None`` left ``test_runs_api.py`` + ``test_external_events.py``
at 75/75 green.

These two tests close that gap without needing a real Postgres/RLS
integration test:

1. ``build_event_producer`` itself actually enters whatever scope factory
   it is given, exactly once, around the replay read.
2. The console call site (``api/runs.py``) really does pass a live,
   invokable scope factory — not ``None`` — to ``build_event_producer``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

import control_plane.api.runs as runs_module
from control_plane.api._run_event_stream import build_event_producer
from expert_work.runtime.runs import InMemoryRunEventStore, make_event_record
from expert_work.runtime.runs.store import MAX_LIST_LIMIT
from expert_work.runtime.stream_bridge import (
    END_SENTINEL,
    InMemoryStreamBridge,
    StreamBridge,
    StreamEvent,
)
from tests.test_runs_api import _seed_completed_run, audit_store, runs_client  # noqa: F401


@pytest.mark.asyncio
async def test_replay_enters_the_given_scope_factory_exactly_once() -> None:
    """Direct unit test on ``build_event_producer`` — a caller-supplied scope
    factory must be invoked exactly once around the durable-store read."""
    run_id = uuid4()
    event_store = InMemoryRunEventStore()
    await event_store.append(
        make_event_record(run_id=run_id, seq=1, event_name="metadata", data={})
    )

    entered = 0

    @asynccontextmanager
    async def _spy_scope() -> AsyncIterator[None]:
        nonlocal entered
        entered += 1
        yield

    producer = build_event_producer(
        run_id=run_id,
        is_terminal=True,
        event_store=event_store,
        stream_bridge=InMemoryStreamBridge(),
        since_seq=None,
        scope=_spy_scope,
    )
    frames = [chunk async for chunk in producer]
    assert frames  # sanity: the replay actually produced real frames
    assert entered == 1


@pytest.mark.asyncio
async def test_console_events_endpoint_passes_a_live_scope_factory(
    runs_client: AsyncClient,  # noqa: F811 -- pytest fixture injection, not a redefinition
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins ``api/runs.py``'s call site — the one place that genuinely
    differs from the external caller (which deliberately passes ``None``).
    A regression to ``scope=None`` here silently drops the resolved-tenant
    binding on the replay DB read; per the module docstring, no HTTP-level
    assertion against the in-memory store can ever catch that, so this test
    spies on the call site directly instead.
    """
    captured: dict[str, Any] = {}
    real_build_event_producer = runs_module.build_event_producer

    def _spy(**kwargs: Any) -> AsyncIterator[bytes]:
        captured.update(kwargs)
        return real_build_event_producer(**kwargs)

    monkeypatch.setattr(runs_module, "build_event_producer", _spy)

    thread_id, run_id = await _seed_completed_run(runs_client)
    resp = await runs_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}/events")
    assert resp.status_code == 200, resp.text

    assert "scope" in captured
    scope_factory = captured["scope"]
    assert scope_factory is not None
    # It must be a genuine, still-usable factory — entering the context
    # manager it returns must not raise.
    async with scope_factory():
        pass


# ---------------------------------------------------------------------------
# P3 PR-1 / Task 3 —— live 分支认 ``since_seq``:补库 + 重排窗口 + 缺口回填
#
# 「验证条件矩阵」D 那一行:**run 仍在跑时重连**才走得到这段代码。下面每条
# 都传 ``is_terminal=False``;终态的 run 走的是 replay 分支,测了等于没测。
# ---------------------------------------------------------------------------

_LIVE_LOGGER = "control_plane.api._run_event_stream"


class _ScriptedBridge(StreamBridge):
    """按脚本推帧的 bridge —— 精确摆出「乱序 / 重叠 / 缺口」三种到达形态。

    脚本里的元素要么是一帧 :class:`StreamEvent`(直接推给订阅者),要么是一个
    零参协程函数(推帧途中执行的副作用,用来模拟「后台攒批 writer 此刻才把某
    几行落盘」)。脚本走完后自动补一帧 end。

    真 :class:`InMemoryStreamBridge` 到达顺序由生产者决定,没法在测试里摆出
    「seq 5 先于 seq 3 到」这种形态 —— 而那恰恰是并发 worker 下的真实形态
    (先分到号的帧可能后进 bridge),也是重排窗口存在的理由。
    """

    def __init__(
        self,
        script: Sequence[StreamEvent | Callable[[], Awaitable[None]]],
        *,
        end_status: str = "success",
    ) -> None:
        self._script = list(script)
        self._end_status = end_status
        self.subscribe_calls = 0

    async def publish(self, run_id: UUID, event: str, data: Any, *, seq: int | None = None) -> None:
        raise AssertionError("消费侧的测试不应该往 bridge 里发帧")

    async def publish_end(self, run_id: UUID, *, status: str) -> None:
        raise AssertionError("消费侧的测试不应该往 bridge 里发帧")

    async def subscribe(
        self,
        run_id: UUID,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        self.subscribe_calls += 1
        for item in self._script:
            if isinstance(item, StreamEvent):
                yield item
            else:
                await item()
        yield StreamEvent(id=None, event=END_SENTINEL.event, data={"status": self._end_status})

    async def cleanup(self, run_id: UUID, *, delay: float = 0) -> None:
        return None


def _live_frame(seq: int, *, event: str = "updates") -> StreamEvent:
    """一帧 bridge 实时帧,id 与落库 seq 同源(Task 1 起的契约)。"""
    return StreamEvent(id=f"1700000000000-{seq}", event=event, data={"n": seq})


def _token_frame() -> StreamEvent:
    """token 帧:不可回放 → 无 id、不占号。"""
    return StreamEvent(id=None, event="token", data={"text": "hi"})


async def _seed_rows(store: InMemoryRunEventStore, run_id: UUID, seqs: Sequence[int]) -> None:
    for seq in seqs:
        await store.append(
            make_event_record(
                run_id=run_id,
                seq=seq,
                event_name="updates",
                data={"n": seq},
                created_at_ms=1700000000000,
            )
        )


def _parse_sse(chunks: Sequence[bytes]) -> list[tuple[str | None, str, Any]]:
    """把 SSE 字节流拆成 ``(id, event, data)`` 三元组;心跳行(``:`` 开头)跳过。"""
    frames: list[tuple[str | None, str, Any]] = []
    for raw in b"".join(chunks).split(b"\n\n"):
        text = raw.decode("utf-8").strip()
        if not text or text.startswith(":"):
            continue
        event_id: str | None = None
        name = ""
        data: Any = None
        for line in text.split("\n"):
            if line.startswith("id: "):
                event_id = line[len("id: ") :]
            elif line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        frames.append((event_id, name, data))
    return frames


def _seqs(frames: Sequence[tuple[str | None, str, Any]]) -> list[int]:
    """客户端可回放帧的 seq,按到达顺序。``end`` / token / 无 id 帧不计入。"""
    return [int(fid.rsplit("-", 1)[1]) for fid, _name, _data in frames if fid is not None]


async def _collect_live(
    *,
    run_id: UUID,
    event_store: InMemoryRunEventStore | None,
    bridge: StreamBridge,
    since_seq: int | None,
    scope: Callable[[], Any] | None = None,
) -> list[tuple[str | None, str, Any]]:
    producer = build_event_producer(
        run_id=run_id,
        is_terminal=False,  # RUNNING —— 不这样就走 replay 分支,测了等于没测
        event_store=event_store,
        stream_bridge=bridge,
        since_seq=since_seq,
        scope=scope,
    )
    return _parse_sse([chunk async for chunk in producer])


@pytest.mark.asyncio
async def test_live_reconnect_backfills_from_store() -> None:
    """run 还在跑时带 ``since_seq`` 重连 —— 断点之后的落库帧必须先补上。

    改之前 ``_stream_live`` 连 ``since_seq`` 都没引用(参数被静默丢弃),客户端
    拿到的只是 bridge 缓冲区里当时还留着的帧,从头重推。
    """
    run_id = uuid4()
    store = InMemoryRunEventStore()
    await _seed_rows(store, run_id, range(5))  # 库里 0..4
    bridge = _ScriptedBridge([])  # 实时流上此刻没有新帧

    frames = await _collect_live(run_id=run_id, event_store=store, bridge=bridge, since_seq=1)

    assert _seqs(frames) == [2, 3, 4]
    assert frames[-1][1] == "end"


@pytest.mark.asyncio
async def test_live_reconnect_dedupes_overlap() -> None:
    """补库给到 seq 4,bridge 随后又推 3/4/5 —— 只能收到 5。"""
    run_id = uuid4()
    store = InMemoryRunEventStore()
    await _seed_rows(store, run_id, range(5))  # 库里 0..4
    bridge = _ScriptedBridge([_live_frame(3), _live_frame(4), _live_frame(5)])

    frames = await _collect_live(run_id=run_id, event_store=store, bridge=bridge, since_seq=None)

    # 0..4 来自补库,5 来自实时流;3/4 不重复。
    assert _seqs(frames) == [0, 1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_live_out_of_order_frames_are_reordered_not_dropped() -> None:
    """bridge 按 ``5,3,4,6`` 推(并发 worker 下先分号后进 bridge)—— 一帧不丢。

    这条钉住重排窗口本身。「见缺口就立刻查库」的写法下:5 到达时 last 会被推到
    4,等真正的 3、4 到达时它们已经 ``<= last`` 被丢弃 —— **本来没丢的帧被那个
    逻辑弄丢**。那种写法下本测试收到的是 ``[5, 6]``。
    """
    run_id = uuid4()
    store = InMemoryRunEventStore()
    await _seed_rows(store, run_id, range(3))  # 库里 0..2
    bridge = _ScriptedBridge([_live_frame(5), _live_frame(3), _live_frame(4), _live_frame(6)])

    frames = await _collect_live(run_id=run_id, event_store=store, bridge=bridge, since_seq=2)

    assert _seqs(frames) == [3, 4, 5, 6]


@pytest.mark.asyncio
async def test_pending_frames_are_flushed_before_end(caplog: pytest.LogCaptureFixture) -> None:
    """窗口没撑爆就到了 end —— 压在 pending 里的帧必须在 end 之前放出去,不能吞。"""
    run_id = uuid4()
    store = InMemoryRunEventStore()
    await _seed_rows(store, run_id, range(3))  # 库里 0..2
    bridge = _ScriptedBridge([_live_frame(3), _live_frame(5)])

    with caplog.at_level(logging.WARNING, logger=_LIVE_LOGGER):
        frames = await _collect_live(run_id=run_id, event_store=store, bridge=bridge, since_seq=2)

    assert _seqs(frames) == [3, 5]
    assert frames[-1][1] == "end"  # 5 在 end 之前
    # 窗口没撑爆 → 不该去查库、也不该报缺口。
    assert [r.getMessage() for r in caplog.records if "gap_unfilled" in r.getMessage()] == []


@pytest.mark.asyncio
async def test_live_reconnect_fills_gap(caplog: pytest.LogCaptureFixture) -> None:
    """重排窗口撑爆 → 才认定真缺口,去库里回填,补不齐的那段报 warning。

    形态:补库给到 seq 2;随后后台 writer 才把 3、4 落盘;bridge 从 seq 35 起
    连推 33 帧(> 重排窗口 32)。客户端必须按序收到 3、4,然后 35…67,
    并且日志里有一条 warning 说明 5..34 补不齐。

    **与计划 Step 1 第 3 条的偏差**:计划那条写「bridge **只**推 seq 35」。泄压
    条件是 ``len(pending) > _REORDER_WINDOW``(计划自己写死的算法),单独一帧
    只会让 ``len(pending) == 1``,永远撑不爆窗口 —— 那样写的测试根本走不到回填
    分支。这里保持算法逐字不变,改成推满 33 帧让窗口真的撑爆。
    """
    run_id = uuid4()
    store = InMemoryRunEventStore()
    await _seed_rows(store, run_id, range(3))  # 补库阶段:库里只有 0..2

    async def _late_persist() -> None:
        """后台攒批 writer 迟到 —— 补库读完之后 3、4 才落盘。"""
        await _seed_rows(store, run_id, [3, 4])

    script: list[StreamEvent | Callable[[], Awaitable[None]]] = [_late_persist]
    script += [_live_frame(s) for s in range(35, 68)]  # 33 帧 > 窗口 32
    bridge = _ScriptedBridge(script)

    with caplog.at_level(logging.WARNING, logger=_LIVE_LOGGER):
        frames = await _collect_live(run_id=run_id, event_store=store, bridge=bridge, since_seq=2)

    assert _seqs(frames) == [3, 4, *range(35, 68)]

    gap_logs = [r.getMessage() for r in caplog.records if "gap_unfilled" in r.getMessage()]
    assert len(gap_logs) == 1, gap_logs
    assert "missing_from=5" in gap_logs[0]
    assert "missing_to=34" in gap_logs[0]


@pytest.mark.asyncio
async def test_live_backfill_pages_through_more_than_one_page() -> None:
    """补库是**循环**读到某页不满为止,不是只读一页。

    钉住计划 Step 5 最后那条变异(「把补库循环改成只读一页」)。顺带钉住
    ``scope`` 工厂在 live 分支的每一次读上都被重新调用一次 —— 工厂返回的 CM
    是单次可用的,复用会炸 ``generator didn't yield``。
    """
    run_id = uuid4()
    store = InMemoryRunEventStore()
    total = MAX_LIST_LIMIT + 10
    await _seed_rows(store, run_id, range(total))
    bridge = _ScriptedBridge([])

    entered = 0

    @asynccontextmanager
    async def _spy_scope() -> AsyncIterator[None]:
        nonlocal entered
        entered += 1
        yield

    frames = await _collect_live(
        run_id=run_id,
        event_store=store,
        bridge=bridge,
        since_seq=None,
        scope=_spy_scope,
    )

    assert _seqs(frames) == list(range(total))
    # 满页 500 + 不满页 10 = 两次读,每次重新调一遍工厂。
    assert entered == 2


@pytest.mark.asyncio
async def test_live_token_frames_pass_through_without_id() -> None:
    """token 帧不可回放:不参与去重 / 重排,原样放行且不带 ``id:``。"""
    run_id = uuid4()
    store = InMemoryRunEventStore()
    await _seed_rows(store, run_id, range(3))  # 库里 0..2
    bridge = _ScriptedBridge([_token_frame(), _live_frame(3), _token_frame()])

    frames = await _collect_live(run_id=run_id, event_store=store, bridge=bridge, since_seq=2)

    assert [(fid, name) for fid, name, _ in frames] == [
        (None, "token"),
        ("1700000000000-3", "updates"),
        (None, "token"),
        (None, "end"),
    ]
