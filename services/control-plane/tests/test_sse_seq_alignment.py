"""P3 PR-1 / Task 2 —— C(帧序号错位)的端到端自证。

Task 1 的单测证明的是 bridge **层**的行为(`publish(seq=...)` 怎么铸 id)。
这里证明的是**端到端**:同一次真 run,live 流里某帧 id 里的 seq,与它落库
那一行的 `run_event.seq` 是同一个数;并且客户端照文档"从帧 id 里取 seq 当
`since_seq`"去回放时,一帧都不会被跳过。

**这条测试只在 run 含 token 帧时才可能失败**(计划的「验证条件矩阵」):
缺陷的形态是 bridge 自己的计数器把 token 帧也算进去,而落库计数器跳过
token 帧,于是 live id 的 seq 恒 ≥ 落库 seq。**无 token 的 run 上两个计数器
恰好相等,测了等于没测。** 所以下面的图桩刻意在 `updates` 之间穿插 token。

live 帧的采集方式:run 跑完后再 `subscribe`(bridge 缓冲区 256 帧、
drop-oldest,这条 run 只有 6 帧,全在缓冲区里)。帧的 `id` 是 **publish
当时**铸好的、随帧一起存进缓冲区的,所以"事后订阅"拿到的 id 与真正的实时
订阅者拿到的逐字节相同 —— 换成并发订阅只会引入调度竞态,不会增强判据。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunEventStore,
    RunManager,
    RunRecord,
)
from expert_work.runtime.runs.store import MAX_LIST_LIMIT
from expert_work.runtime.stream_bridge import InMemoryStreamBridge, StreamEvent, is_end
from orchestrator.graph_builder._config import TOKEN_SINK_KEY
from orchestrator.sse import _BACKGROUND_PERSIST_WRITERS, run_agent


class _TokenInterleavedGraph:
    """在两个 `updates` chunk 之间穿插 token 帧。

    产出的帧序列(run_agent 自己先发一帧 metadata):
    ``metadata, token, token, updates, token, updates``
    —— token 帧夹在落库帧中间,两个计数器因此从第 2 帧起就分叉。
    """

    async def astream(
        self, _input: Any, config: Any = None, *, stream_mode: str = "updates"
    ) -> AsyncIterator[Any]:
        sink = (config.get("configurable") or {})[TOKEN_SINK_KEY]
        await sink({"step": 0, "channel": "content", "text": "he"})
        await sink({"step": 0, "channel": "content", "text": "llo"})
        yield {"agent": {"step_count": 1}}
        await sink({"step": 1, "channel": "content", "text": "!"})
        yield {"agent": {"step_count": 2}}

    async def aget_state(self, _config: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(values={})


async def _new_record(rm: RunManager) -> RunRecord:
    return await rm.create(
        run_id=uuid4(),
        thread_id=uuid4(),
        tenant_id=uuid4(),
        on_disconnect=DisconnectMode.CANCEL,
    )


async def _drain_live(bridge: InMemoryStreamBridge, run_id: Any) -> list[StreamEvent]:
    frames: list[StreamEvent] = []
    async for entry in bridge.subscribe(run_id, heartbeat_interval=5.0):
        if is_end(entry):
            break
        frames.append(entry)
    return frames


async def _await_writers() -> None:
    """落库走后台攒批 writer;断言库内容前必须等它冲干净。"""
    if _BACKGROUND_PERSIST_WRITERS:
        await asyncio.gather(*_BACKGROUND_PERSIST_WRITERS, return_exceptions=True)


def _seq_of(frame: StreamEvent) -> int:
    assert frame.id is not None
    return int(frame.id.rsplit("-", 1)[1])


@pytest.mark.asyncio
async def test_live_frame_ids_and_persisted_seqs_are_the_same_numbers() -> None:
    """live 帧 id 里的 seq == 落库行的 seq,且 token 帧不带 id、不占号。"""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryRunEventStore()

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_TokenInterleavedGraph(),
        graph_input={"messages": []},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        event_store=store,
    )
    live_frames = await _drain_live(bridge, record.run_id)
    await _await_writers()
    rows = await store.list(run_id=record.run_id, limit=MAX_LIST_LIMIT)

    # 前置条件:这条 run 真的含 token 帧。缺了它,下面的断言全部恒绿。
    assert [f.event for f in live_frames] == [
        "metadata",
        "token",
        "token",
        "updates",
        "token",
        "updates",
    ]

    # token 帧不可回放 → 无 id 行。
    assert all(f.id is None for f in live_frames if f.event == "token")

    live_ids = [f.id for f in live_frames if f.event != "token"]
    assert all(i is not None for i in live_ids)
    live_seqs = [_seq_of(f) for f in live_frames if f.event != "token"]

    assert live_seqs == [r.seq for r in rows]
    assert live_seqs == list(range(len(live_seqs)))  # 连续无缺口


@pytest.mark.asyncio
async def test_seq_parsed_off_a_live_frame_replays_without_skipping() -> None:
    """客户端行为的直白复现:拿 live 帧 id 里的 seq 当 `since_seq` 回放。

    对**每一个**可回放的 live 帧都做一次往返 —— 回放结果必须恰好是该帧之后
    的那些可回放帧,一帧不多一帧不少。缺陷未修时 live seq 被 token 帧顶高,
    这里会静默少拿到真实存在的帧(spec §十 的"静默丢数据")。
    """
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryRunEventStore()

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_TokenInterleavedGraph(),
        graph_input={"messages": []},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        event_store=store,
    )
    live_frames = await _drain_live(bridge, record.run_id)
    await _await_writers()

    # 前置条件:token 帧真的出现在可回放帧之间(否则本测试恒绿)。
    assert [f.event for f in live_frames].count("token") == 3
    assert live_frames[1].event == "token"

    replayable = [f for f in live_frames if f.event != "token"]
    assert len(replayable) == 3  # metadata + 2 条 updates

    for index, frame in enumerate(replayable):
        since = _seq_of(frame)
        rows = await store.list(run_id=record.run_id, since_seq=since, limit=MAX_LIST_LIMIT)
        expected = [f.event for f in replayable[index + 1 :]]
        assert [r.event_name for r in rows] == expected, (
            f"since_seq={since}(取自第 {index} 个可回放帧 {frame.event})回放跳帧:"
            f"期望 {expected},实得 {[r.event_name for r in rows]}"
        )
