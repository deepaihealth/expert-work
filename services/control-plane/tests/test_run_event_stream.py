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
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

import control_plane.api.runs as runs_module
from control_plane.api._run_event_stream import build_event_producer
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunEventStore,
    RunInfo,
    RunStatus,
    make_event_record,
)
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

    plan = await build_event_producer(
        run_id=run_id,
        run_status=RunStatus.SUCCESS,
        event_store=event_store,
        stream_bridge=InMemoryStreamBridge(),
        since_seq=None,
        scope=_spy_scope,
    )
    frames = [chunk async for chunk in plan.producer]
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
# P3 PR-1 / Task 3R —— live 分支认 ``since_seq``:补库 + 去重 + gap 帧
#
# 「验证条件矩阵」D 那一行:**run 仍在跑时重连**才走得到这段代码。下面每条
# 都传 ``run_status=RUNNING``;终态的 run 走的是 replay 分支,测了等于没测。
# ---------------------------------------------------------------------------

_LIVE_LOGGER = "control_plane.api._run_event_stream"


class _ScriptedBridge(StreamBridge):
    """按脚本推帧的 bridge —— 精确摆出「重叠」和「缺口」两种到达形态。

    脚本里的元素要么是一帧 :class:`StreamEvent`(直接推给订阅者),要么是一个
    零参协程函数(推帧途中执行的副作用,用来模拟「后台攒批 writer 此刻才把某
    几行落盘」)。脚本走完后自动补一帧 end。

    真 :class:`InMemoryStreamBridge` 摆不出「缓冲区滚过一段之后订阅者才挂上」
    这种时序。**注意脚本里的 seq 必须递增** —— Task 3R 之后 bridge 保证订阅者
    看到的帧顺序恒等于 seq 顺序,写一个递减的脚本就是在测一个真 bridge 产不出
    的形态。
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

    async def publish(self, run_id: UUID, event: str, data: Any) -> int:
        raise AssertionError("消费侧的测试不应该往 bridge 里发帧")

    async def publish_ephemeral(self, run_id: UUID, event: str, data: Any) -> None:
        raise AssertionError("消费侧的测试不应该往 bridge 里发帧")

    async def seed_seq(self, run_id: UUID, *, next_seq: int) -> None:
        raise AssertionError("消费侧的测试不应该动 bridge 的发号器")

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
    """一帧 bridge 实时帧,id 里的 seq 就是 ``publish`` 分配的那个号。"""
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
    plan = await build_event_producer(
        run_id=run_id,
        run_status=RunStatus.RUNNING,  # 不这样就走 replay 分支,测了等于没测
        event_store=event_store,
        stream_bridge=bridge,
        since_seq=since_seq,
        scope=scope,
    )
    assert plan.next_seq is None  # live 分支不截断
    return _parse_sse([chunk async for chunk in plan.producer])


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
async def test_live_unfillable_gap_emits_gap_frame(caplog: pytest.LogCaptureFixture) -> None:
    """真缺口:能从库里补的补上,补不齐的那一段发一帧 ``gap``。

    形态:补库给到 seq 2;后台 writer 随后才把 3、4 落盘;bridge 推 seq 8
    (5..7 既不在 bridge 缓冲区里、也没落库)。客户端必须依次收到
    3、4、一帧 ``gap {"from": 5, "to": 7}``、然后 8。

    Task 3R 之后跳号**没有歧义** —— bridge 在自己的临界区里发号并入队,订阅者
    看到的帧顺序恒等于 seq 顺序,所以 ``seq > last + 1`` 只可能是真缺口。
    """
    run_id = uuid4()
    store = InMemoryRunEventStore()
    await _seed_rows(store, run_id, range(3))  # 补库阶段:库里只有 0..2

    async def _late_persist() -> None:
        """后台攒批 writer 迟到 —— 补库读完之后 3、4 才落盘(5..7 始终没有)。"""
        await _seed_rows(store, run_id, [3, 4])

    bridge = _ScriptedBridge([_late_persist, _live_frame(8)])

    with caplog.at_level(logging.WARNING, logger=_LIVE_LOGGER):
        frames = await _collect_live(run_id=run_id, event_store=store, bridge=bridge, since_seq=2)

    assert [(name, data) for _fid, name, data in frames] == [
        ("updates", {"n": 3}),
        ("updates", {"n": 4}),
        ("gap", {"from": 5, "to": 7}),
        ("updates", {"n": 8}),
        ("end", {"status": "success", "run_id": str(run_id)}),
    ]
    # ``gap`` 帧不可回放 —— 它描述的是这条连接的状况,不是 run 的事件。
    assert next(fid for fid, name, _ in frames if name == "gap") is None
    gap_logs = [r.getMessage() for r in caplog.records if "live_stream.gap " in r.getMessage()]
    assert len(gap_logs) == 1, gap_logs


@pytest.mark.asyncio
async def test_live_backfill_pages_through_more_than_one_page() -> None:
    """补库是**循环**读到某页不满为止,不是只读一页。

    钉住计划 Step 7 最后那条变异(「把补库循环改成只读一页」)。顺带钉住
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
    """token 帧不可回放:不参与去重,原样放行且不带 ``id:``。"""
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


#
# 「验证条件矩阵」E 那一行:**帧数 > 页大小**才走得到截断分支;短 run 上
# 怎么写都绿。
# ---------------------------------------------------------------------------


async def _collect_replay(
    *,
    run_id: UUID,
    store: InMemoryRunEventStore,
    since_seq: int | None = None,
    run_status: RunStatus = RunStatus.SUCCESS,
) -> tuple[list[tuple[str | None, str, Any]], int | None]:
    plan = await build_event_producer(
        run_id=run_id,
        run_status=run_status,
        event_store=store,
        stream_bridge=InMemoryStreamBridge(),
        since_seq=since_seq,
        scope=None,
    )
    return _parse_sse([chunk async for chunk in plan.producer]), plan.next_seq


@pytest.mark.asyncio
async def test_replay_truncates_without_end_frame() -> None:
    """帧数超过一页 → 收尾是 ``truncated``,**不是** ``end``。

    以前这里无条件补一个 ``end``,客户端会以为流正常结束,把 500 帧之后的东西
    静默丢掉 —— 而且没有任何报错。
    """
    run_id = uuid4()
    store = InMemoryRunEventStore()
    await _seed_rows(store, run_id, range(MAX_LIST_LIMIT + 10))

    frames, next_seq = await _collect_replay(run_id=run_id, store=store)

    assert len(frames) == MAX_LIST_LIMIT + 1  # 一页帧 + 一帧 truncated
    assert [name for _fid, name, _d in frames].count("end") == 0
    assert frames[-1][1] == "truncated"
    assert frames[-1][2] == {"next_seq": MAX_LIST_LIMIT - 1}
    assert next_seq == MAX_LIST_LIMIT - 1
    assert _seqs(frames) == list(range(MAX_LIST_LIMIT))


@pytest.mark.asyncio
async def test_replay_exact_page_size_is_not_truncated() -> None:
    """恰好一页 → **没有** ``next_seq``,收尾是 ``end``。

    钉住那个 off-by-one:用「行数 == 页大小」单独判定截断,总帧数恰好整除页
    大小时会误报,客户端白拉一页空的、还永远等不到 ``end``。
    """
    run_id = uuid4()
    store = InMemoryRunEventStore()
    await _seed_rows(store, run_id, range(MAX_LIST_LIMIT))

    frames, next_seq = await _collect_replay(run_id=run_id, store=store)

    assert next_seq is None
    assert frames[-1][1] == "end"
    assert [name for _fid, name, _d in frames].count("truncated") == 0
    assert len(frames) == MAX_LIST_LIMIT + 1  # 一页帧 + 一帧 end


@pytest.mark.asyncio
async def test_replay_cursor_loop_covers_every_frame() -> None:
    """按 ``next_seq`` 循环拉到 ``end`` —— 拼起来必须是 ``range(总帧数)``。"""
    run_id = uuid4()
    store = InMemoryRunEventStore()
    total = MAX_LIST_LIMIT * 2 + 7
    await _seed_rows(store, run_id, range(total))

    collected: list[int] = []
    cursor: int | None = None
    pages = 0
    while True:
        pages += 1
        assert pages <= 10, "游标循环没有收敛"
        frames, next_seq = await _collect_replay(run_id=run_id, store=store, since_seq=cursor)
        collected += _seqs(frames)
        if next_seq is None:
            assert frames[-1][1] == "end"
            break
        assert frames[-1][1] == "truncated"
        cursor = next_seq

    assert pages == 3
    assert collected == list(range(total))  # 无重复、无缺口


@pytest.mark.asyncio
async def test_external_response_carries_next_seq_header_only_when_truncated() -> None:
    """外部面 ``build_events_response``:截断时才带 ``X-Expert-Work-Next-Seq``。

    头和 ``truncated`` 帧**两者都要**:浏览器 ``EventSource`` 读不到响应头,
    只给 header 的信号对一整类客户端不可用;而非浏览器客户端读头最省事。
    """
    from control_plane.api.external_events import build_events_response

    async def _response_for(frame_count: int) -> Any:
        run_id = uuid4()
        store = InMemoryRunEventStore()
        await _seed_rows(store, run_id, range(frame_count))
        run = RunInfo(
            run_id=run_id,
            tenant_id=uuid4(),
            thread_id=uuid4(),
            user_id=None,
            status=RunStatus.SUCCESS,  # 终态 → 走 replay 分支
            on_disconnect=DisconnectMode.CANCEL,
            is_resume=False,
            error=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        return await build_events_response(
            run=run, event_store=store, stream_bridge=InMemoryStreamBridge()
        )

    truncated = await _response_for(MAX_LIST_LIMIT + 10)
    assert truncated.headers["x-expert-work-next-seq"] == str(MAX_LIST_LIMIT - 1)

    short = await _response_for(3)
    assert "x-expert-work-next-seq" not in short.headers


@pytest.mark.asyncio
async def test_console_events_endpoint_carries_next_seq_header(
    runs_client: AsyncClient,  # noqa: F811 -- pytest fixture injection, not a redefinition
) -> None:
    """控制台面那条路也要带这个头 —— 两个调用点的头集合不能分叉。

    P2-a 刚修过一次同类问题(重放响应的头是首次响应的真子集)。只在
    ``external_events.py`` 上加头、忘了 ``runs.py``,这条会红。
    """
    thread_id, run_id = await _seed_completed_run(runs_client)
    store = runs_client._transport.app.state.run_event_store  # type: ignore[attr-defined]
    existing = await store.list(run_id=UUID(run_id), limit=MAX_LIST_LIMIT)
    base = max(r.seq for r in existing) + 1
    await _seed_rows(store, UUID(run_id), range(base, base + MAX_LIST_LIMIT + 10))

    resp = await runs_client.get(f"/v1/sessions/{thread_id}/runs/{run_id}/events")
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-expert-work-next-seq"] == str(MAX_LIST_LIMIT - 1)
    assert _parse_sse([resp.content])[-1][1] == "truncated"


# ---------------------------------------------------------------------------
# P3 PR-1 / Task 5 —— ``end`` 帧带终局状态
#
# 「验证条件矩阵」F 那一行:**必须用被取消 / PAUSED 的 run**。正常跑完的 run
# 上 status 恒 ``success``,任何写错的映射表都能跑绿。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (RunStatus.INTERRUPTED, "interrupted"),
        (RunStatus.PAUSED, "paused"),  # 等审批不是失败
        (RunStatus.ERROR, "error"),
        (RunStatus.TIMEOUT, "error"),
        (RunStatus.SUCCESS, "success"),
    ],
)
@pytest.mark.asyncio
async def test_replay_end_frame_carries_status(status: RunStatus, expected: str) -> None:
    """回放分支的 ``end`` 帧带 ``{"status", "run_id"}``。

    以前 run 被取消也只发 ``end`` + ``data: null``,第三方分不清"正常答完"和
    "被取消",得再查一次 REST。
    """
    run_id = uuid4()
    store = InMemoryRunEventStore()
    await _seed_rows(store, run_id, range(3))

    frames, _ = await _collect_replay(run_id=run_id, store=store, run_status=status)

    assert frames[-1][1] == "end"
    assert frames[-1][2] == {"status": expected, "run_id": str(run_id)}


@pytest.mark.asyncio
async def test_live_end_frame_carries_status() -> None:
    """live 分支的 ``end`` 帧同样带 status —— 从 bridge 的 end 帧 data 里取。"""
    run_id = uuid4()
    store = InMemoryRunEventStore()
    await _seed_rows(store, run_id, range(2))
    bridge = _ScriptedBridge([], end_status="interrupted")

    frames = await _collect_live(run_id=run_id, event_store=store, bridge=bridge, since_seq=None)

    assert frames[-1][1] == "end"
    assert frames[-1][2] == {"status": "interrupted", "run_id": str(run_id)}


@pytest.mark.asyncio
async def test_live_gap_frame_does_not_disturb_end_status() -> None:
    """Task 3R 的 ``gap`` 帧与 Task 5 的 end status 不互相踩。"""
    run_id = uuid4()
    store = InMemoryRunEventStore()
    await _seed_rows(store, run_id, range(3))
    bridge = _ScriptedBridge([_live_frame(5)], end_status="paused")

    frames = await _collect_live(run_id=run_id, event_store=store, bridge=bridge, since_seq=2)

    assert [name for _fid, name, _d in frames] == ["gap", "updates", "end"]
    assert frames[0][2] == {"from": 3, "to": 4}
    assert _seqs(frames) == [5]
    assert frames[-1][2]["status"] == "paused"


@pytest.mark.asyncio
async def test_both_sse_paths_emit_the_same_end_shape() -> None:
    """防分叉哨兵 —— 同一个 run,两条 SSE 流的 ``end`` 帧 data 必须一模一样。

    路径一:``sse_consumer``(``POST /v1/agents/{code}/runs`` 的 ``mode:
    "stream"``,第三方主路径,经 ``spawn_run`` 走到);
    路径二:``GET .../runs/{run_id}/events``(断线重连,本模块)。

    只改其中一条时这条必须红。这个仓库刚在 P2-a 修过一次同类的「重放响应的
    字段集合是首次响应的真子集」问题 —— 结构上共用一个构造口(``sse.py`` 的
    ``end_frame_data``)是根治,这条测试是它的看门狗。
    """
    from expert_work.runtime.runs import RunManager
    from orchestrator.sse import run_agent, sse_consumer

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await rm.create(
        run_id=uuid4(),
        thread_id=uuid4(),
        tenant_id=uuid4(),
        on_disconnect=DisconnectMode.CANCEL,
    )
    store = InMemoryRunEventStore()
    record.abort_event.set()  # → INTERRUPTED,不是恒 success 的正常收尾

    class _OneChunkGraph:
        async def astream(
            self, _input: Any, _config: Any = None, *, stream_mode: str = "updates"
        ) -> AsyncIterator[Any]:
            yield {"agent": {"step_count": 1}}

        async def aget_state(self, _config: Any) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(values={})

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_OneChunkGraph(),
        graph_input={"messages": []},
        config={},
        event_store=store,
    )
    assert rm.get(record.run_id).status is RunStatus.INTERRUPTED  # 前置条件

    async def _never_disconnected() -> bool:
        return False

    consumer_frames = _parse_sse(
        [
            chunk
            async for chunk in sse_consumer(
                bridge=bridge,
                record=record,
                run_manager=rm,
                is_disconnected=_never_disconnected,
                heartbeat_interval=5.0,
            )
        ]
    )
    events_frames, _ = await _collect_replay(
        run_id=record.run_id, store=store, run_status=RunStatus.INTERRUPTED
    )

    consumer_end = consumer_frames[-1]
    events_end = events_frames[-1]
    assert consumer_end[1] == events_end[1] == "end"
    # 字段集合先比。``data`` 不是 dict(比如某条路径还在发 ``null``)时归一成
    # ``None``,好让失败信息直接指出是哪条路径掉队,而不是抛 TypeError。
    consumer_fields = sorted(consumer_end[2]) if isinstance(consumer_end[2], dict) else None
    events_fields = sorted(events_end[2]) if isinstance(events_end[2], dict) else None
    assert consumer_fields == events_fields, (
        f"两条流的 end 帧 data 分叉了:sse_consumer={consumer_end[2]!r} "
        f"vs GET events={events_end[2]!r}"
    )
    # 字段集合相同还不够 —— 同一个 run 的值也必须一致。
    assert (
        consumer_end[2]
        == events_end[2]
        == {
            "status": "interrupted",
            "run_id": str(record.run_id),
        }
    )
