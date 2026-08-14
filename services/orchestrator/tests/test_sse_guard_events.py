"""B3 — run_agent 注入 guard sink(_publish_guard)+ TokenBudget:发布 + 持久化 + 并发 seq."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.runtime.runs import DisconnectMode, RunManager, RunRecord
from expert_work.runtime.runs.event_store import InMemoryRunEventStore
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator.sse import run_agent
from orchestrator.tools._guards import GUARD_SINK_KEY, TOKEN_BUDGET_KEY, TokenBudget


class _YieldingBridge(InMemoryStreamBridge):
    """Test-only bridge that yields to the event loop before publishing.

    Same rationale as ``test_sse_worker_events.py``'s ``_YieldingBridge``:
    ``InMemoryStreamBridge.publish`` never actually suspends, so concurrent
    ``sink()`` calls under ``asyncio.gather`` never interleave without a
    forced ``await`` here — the vacuous-test bug this class exists to fix.
    """

    async def publish(self, run_id: UUID, event: str, data: Any, *, seq: int | None = None) -> None:
        await asyncio.sleep(0)  # 让出事件循环 — 强制并发 sink 交错
        await super().publish(run_id, event, data, seq=seq)


async def _new_record(rm: RunManager) -> RunRecord:
    return await rm.create(
        run_id=uuid4(),
        thread_id=uuid4(),
        tenant_id=uuid4(),
        on_disconnect=DisconnectMode.CANCEL,
    )


class _GuardGraph:
    """astream 期间经注入的 GUARD_SINK_KEY 发 guard 帧(模拟 builder.py 的
    token_budget/max_steps/no_progress tripped-warning 帧);顺带捕获注入的
    config 供测试断言 TOKEN_BUDGET_KEY 的有无。
    """

    def __init__(self, frames: list[dict[str, Any]], *, concurrent: bool = False) -> None:
        self.frames = frames
        self.concurrent = concurrent
        self.captured_config: Any = None

    async def astream(
        self, input: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del input, stream_mode
        self.captured_config = config
        sink = config["configurable"][GUARD_SINK_KEY]
        if self.concurrent:
            await asyncio.gather(*(sink(f) for f in self.frames))
        else:
            for frame in self.frames:
                await sink(frame)
        yield {"agent": {"step_count": 1}}


@pytest.mark.asyncio
async def test_guard_frames_published_and_persisted_with_monotonic_seq() -> None:
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryRunEventStore()
    frames = [
        {"kind": "warning", "guard": "token_budget", "detail": {"spent": 800, "limit": 1000}},
        {"kind": "tripped", "guard": "token_budget", "detail": {"spent": 1000, "limit": 1000}},
    ]

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_GuardGraph(frames),
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        event_store=store,
    )

    events = await store.list(run_id=record.run_id, limit=500)
    guard_rows = [e for e in events if e.event_name == "guard"]
    assert [r.data["kind"] for r in guard_rows] == ["warning", "tripped"]
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)  # 无重复


@pytest.mark.asyncio
async def test_token_budget_injected_only_when_positive() -> None:
    rm = RunManager()
    store = InMemoryRunEventStore()

    # token_budget=1000 → configurable[TOKEN_BUDGET_KEY] 是新建 TokenBudget(limit=1000)
    bridge_a = InMemoryStreamBridge()
    record_a = await _new_record(rm)
    graph_a = _GuardGraph([])
    await run_agent(
        bridge=bridge_a,
        run_manager=rm,
        record=record_a,
        graph=graph_a,
        graph_input={},
        config={"configurable": {"thread_id": str(record_a.thread_id)}},
        event_store=store,
        token_budget=1000,
    )
    configurable_a = graph_a.captured_config["configurable"]
    tb = configurable_a[TOKEN_BUDGET_KEY]
    assert isinstance(tb, TokenBudget)
    assert tb.limit == 1000
    assert tb.spent == 0
    assert GUARD_SINK_KEY in configurable_a  # 无条件注入

    # token_budget=0(默认)→ 无 TOKEN_BUDGET_KEY,但 GUARD_SINK_KEY 仍在
    bridge_b = InMemoryStreamBridge()
    record_b = await _new_record(rm)
    graph_b = _GuardGraph([])
    await run_agent(
        bridge=bridge_b,
        run_manager=rm,
        record=record_b,
        graph=graph_b,
        graph_input={},
        config={"configurable": {"thread_id": str(record_b.thread_id)}},
        event_store=store,
    )
    configurable_b = graph_b.captured_config["configurable"]
    assert TOKEN_BUDGET_KEY not in configurable_b
    assert GUARD_SINK_KEY in configurable_b


@pytest.mark.asyncio
async def test_concurrent_guard_frames_do_not_collide_on_seq() -> None:
    bridge = _YieldingBridge()  # 强制真交错,否则并发 sink 永不 interleave
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryRunEventStore()  # append 对重复 (run_id, seq) 直接 raise
    frames = [
        {"kind": "warning", "guard": "token_budget", "detail": {"i": 0}},
        {"kind": "tripped", "guard": "max_steps", "detail": {"i": 1}},
        {"kind": "tripped", "guard": "no_progress", "detail": {"i": 2}},
        {"kind": "warning", "guard": "token_budget", "detail": {"i": 3}},
    ]

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_GuardGraph(frames, concurrent=True),
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        event_store=store,
    )

    events = await store.list(run_id=record.run_id, limit=500)
    guard_rows = [e for e in events if e.event_name == "guard"]
    assert len(guard_rows) == 4
    assert len({e.seq for e in events}) == len(events)
