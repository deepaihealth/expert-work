"""``stream_format="items"`` 在回放 / live 接合两条路径上的行为(条目 program PR3)。

``test_stream_items.py``(orchestrator)测的是转换器自身;这里测的是它挂在
``_encode`` 上之后,与**分页截断 / 补库 / 陈旧帧重放**这些既有机制的相互作用 ——
硬约束 (d) 只有在这一层才摆得出真实形态。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from control_plane.api._run_event_stream import build_event_producer
from expert_work.runtime.runs import InMemoryRunEventStore, RunStatus, make_event_record
from expert_work.runtime.runs.store import MAX_LIST_LIMIT
from expert_work.runtime.stream_bridge import InMemoryStreamBridge, StreamEvent
from orchestrator.stream_items import (
    ITEM_ADDED,
    ITEM_DELTA,
    ITEM_DONE,
    STREAM_FORMAT_ITEMS,
    STREAM_FORMAT_LEGACY,
)

from tests.test_run_event_stream import _parse_sse, _ScriptedBridge

MS = 1700000000000


def _ai(content: str, *, calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"type": "ai", "content": content, "tool_calls": calls or [], "additional_kwargs": {}}


def _agent_chunk(step: int, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {"agent": {"step_count": step, "messages": messages}}


async def _seed(
    store: InMemoryRunEventStore, run_id: UUID, rows: list[tuple[int, str, Any]]
) -> None:
    for seq, name, data in rows:
        await store.append(
            make_event_record(
                run_id=run_id, seq=seq, event_name=name, data=data, created_at_ms=MS
            )
        )


async def _collect(
    *,
    run_id: UUID,
    store: InMemoryRunEventStore | None,
    bridge: Any,
    status: RunStatus,
    since_seq: int | None = None,
    stream_format: str = STREAM_FORMAT_ITEMS,
) -> tuple[list[tuple[str | None, str, Any]], int | None]:
    plan = await build_event_producer(
        run_id=run_id,
        run_status=status,
        event_store=store,
        stream_bridge=bridge,
        since_seq=since_seq,
        scope=None,
        stream_format=stream_format,
    )
    return _parse_sse([chunk async for chunk in plan.producer]), plan.next_seq


def _names(frames: list[tuple[str | None, str, Any]]) -> list[str]:
    return [name for _fid, name, _d in frames]


# ---------------------------------------------------------------------------
# 回放
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_has_only_item_done() -> None:
    """重新发送已完成的一轮时只有 ``item.done``,没有 ``added`` / ``delta``。

    token 事件从不记录(它是一次性预览),所以这条路径上根本没有可转成
    ``item.delta`` 的输入。**客户端 reducer 因此必须把 ``item.done`` 当 upsert
    处理** —— 按「先 added 再 done」严格配对写的 reducer 会在这条路径上崩。
    """
    run_id = uuid4()
    store = InMemoryRunEventStore()
    await _seed(
        store,
        run_id,
        [
            (0, "metadata", {"run_id": str(run_id)}),
            (1, "updates", _agent_chunk(1, [_ai("你好")])),
            (2, "plan", {"goal": "做事", "steps": []}),
        ],
    )

    frames, next_seq = await _collect(
        run_id=run_id, store=store, bridge=InMemoryStreamBridge(), status=RunStatus.SUCCESS
    )

    assert next_seq is None
    names = _names(frames)
    # 先证兄弟事件在:条目确实发出来了,下面两条否定断言才不是空转。
    assert names.count(ITEM_DONE) == 3, frames
    assert ITEM_ADDED not in names
    assert ITEM_DELTA not in names
    assert names == ["metadata", ITEM_DONE, ITEM_DONE, ITEM_DONE, "end"]
    # 最后一条是结束前补发的 final 改判。
    assert frames[-2][2]["channel"] == "final"


@pytest.mark.asyncio
async def test_truncated_page_does_not_carry_the_final_correction() -> None:
    """截断的那一页不能补发 ``final`` —— 这条流还没结束,下一页还会来。

    补早了,客户端会把一条中间说明当成本轮正文。
    """
    run_id = uuid4()
    store = InMemoryRunEventStore()
    rows: list[tuple[int, str, Any]] = [
        (seq, "updates", _agent_chunk(seq, [_ai(f"第 {seq} 段")]))
        for seq in range(MAX_LIST_LIMIT + 1)
    ]
    await _seed(store, run_id, rows)

    frames, next_seq = await _collect(
        run_id=run_id, store=store, bridge=InMemoryStreamBridge(), status=RunStatus.SUCCESS
    )

    assert next_seq == MAX_LIST_LIMIT - 1
    names = _names(frames)
    assert names[-1] == "truncated"
    assert "end" not in names
    # 先证兄弟事件在:这一页真的发了条目,只是一条都不是 final。
    assert names.count(ITEM_DONE) == MAX_LIST_LIMIT
    channels = {d.get("channel") for _fid, name, d in frames if name == ITEM_DONE}
    assert channels == {"commentary"}


@pytest.mark.asyncio
async def test_items_mode_does_not_move_the_replay_cursor() -> None:
    """条目模式与 legacy 的 ``next_seq`` 必须完全一致。

    续传位置由未编码的原始记录算出,转换器够不到它。这条钉住那个事实 ——
    一转多的扇出不许影响分页。
    """
    run_id = uuid4()
    store = InMemoryRunEventStore()
    await _seed(
        store,
        run_id,
        [
            (seq, "updates", _agent_chunk(seq, [_ai(f"第 {seq} 段")]))
            for seq in range(MAX_LIST_LIMIT + 3)
        ],
    )

    legacy_frames, legacy_next = await _collect(
        run_id=run_id,
        store=store,
        bridge=InMemoryStreamBridge(),
        status=RunStatus.SUCCESS,
        stream_format=STREAM_FORMAT_LEGACY,
    )
    item_frames, item_next = await _collect(
        run_id=run_id, store=store, bridge=InMemoryStreamBridge(), status=RunStatus.SUCCESS
    )

    assert legacy_next == item_next == MAX_LIST_LIMIT - 1
    # 两种形态下最后一条带 id 的事件指向同一个位置。
    assert _last_id(legacy_frames) == _last_id(item_frames)


def _last_id(frames: list[tuple[str | None, str, Any]]) -> str | None:
    return next((fid for fid, _n, _d in reversed(frames) if fid is not None), None)


# ---------------------------------------------------------------------------
# (d) live 接合重放陈旧 token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_attach_drops_stale_tokens_for_settled_steps() -> None:
    """「对话进行中刷新页面」——  接合时重放的陈旧 token 不能重开已完成的条目。

    形态照真实链路摆:接上一条还在跑的 run 时,服务端先把已记录的事件补齐(于是
    第 1 步的条目已经完成),再挂实时流;而实时流会从它自己的缓冲区最早一条重新
    发一遍。带位置号的事件在这里被去重挡掉,**token 没有位置号,无条件放行** ——
    仓库里有一条测试正面确认了这个行为。legacy 下这只是多看到几段陈旧的打字机
    文本;条目模式下它会给一个已经完成的条目重开 ``item.added``,界面上那段正文
    被打回半成品。

    这条单测很难自然撞到:要同时具备「补库已经发过这一步的权威事件」和「缓冲区
    里还留着这一步的 token」两个条件。
    """
    run_id = uuid4()
    store = InMemoryRunEventStore()
    await _seed(store, run_id, [(0, "updates", _agent_chunk(1, [_ai("第一步的完整答案")]))])
    bridge = _ScriptedBridge(
        [
            # 缓冲区里还留着第 1 步的 token —— 无位置号,去重挡不住。
            StreamEvent(id=None, event="token", data={"step": 1, "channel": "content", "text": "第一"}),
            # 正在跑的第 2 步的 token —— 这个必须照常流。
            StreamEvent(id=None, event="token", data={"step": 2, "channel": "content", "text": "第二"}),
            StreamEvent(id=f"{MS}-1", event="updates", data=_agent_chunk(2, [_ai("第二步答案")])),
        ]
    )

    frames, _next = await _collect(
        run_id=run_id, store=store, bridge=bridge, status=RunStatus.RUNNING
    )

    step1 = f"{run_id}:step:1"
    step2 = f"{run_id}:step:2"
    ids_added = [d["id"] for _fid, name, d in frames if name == ITEM_ADDED]
    ids_delta = [d["id"] for _fid, name, d in frames if name == ITEM_DELTA]
    # 先证兄弟事件在:第 2 步的打字机确实还在流,所以下面不是「转换器整体失灵」。
    assert step2 in ids_added and step2 in ids_delta, frames
    assert step1 not in ids_added, "已经完成的条目被陈旧 token 重开了"
    assert step1 not in ids_delta, "已经完成的条目被陈旧 token 追加了"
    # 而它的权威条目照常在。
    done_ids = [d["id"] for _fid, name, d in frames if name == ITEM_DONE]
    assert step1 in done_ids


@pytest.mark.asyncio
async def test_live_attach_keeps_streaming_when_nothing_settled_yet() -> None:
    """还没有任何权威事件时,缓冲区里的 token 照常转成打字机。

    没有这条,上面那条的实现可以退化成「live 分支一律丢 token」而仍然全绿。
    """
    run_id = uuid4()
    bridge = _ScriptedBridge(
        [
            StreamEvent(id=None, event="token", data={"step": 1, "channel": "content", "text": "开"}),
            StreamEvent(id=None, event="token", data={"step": 1, "channel": "content", "text": "头"}),
        ]
    )

    frames, _next = await _collect(
        run_id=run_id, store=InMemoryRunEventStore(), bridge=bridge, status=RunStatus.RUNNING
    )

    names = _names(frames)
    assert names.count(ITEM_ADDED) == 1
    assert [d["text"] for _fid, name, d in frames if name == ITEM_DELTA] == ["开", "头"]


@pytest.mark.asyncio
async def test_legacy_live_wire_is_unchanged() -> None:
    """默认形态下 live 接合的 wire 与转换器引入之前一致。"""
    run_id = uuid4()
    store = InMemoryRunEventStore()
    chunk = _agent_chunk(1, [_ai("你好")])
    await _seed(store, run_id, [(0, "updates", chunk)])
    bridge = _ScriptedBridge(
        [StreamEvent(id=None, event="token", data={"step": 1, "channel": "content", "text": "你"})]
    )

    frames, _next = await _collect(
        run_id=run_id,
        store=store,
        bridge=bridge,
        status=RunStatus.RUNNING,
        stream_format=STREAM_FORMAT_LEGACY,
    )

    assert _names(frames) == ["updates", "token", "end"]
    assert frames[0][2] == chunk
