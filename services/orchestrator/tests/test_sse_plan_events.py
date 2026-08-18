"""调试台重设计 PR1 —— 顶层 ``plan`` 事件:从 updates 派生 / 开跑补发 / 降级 / 落库。

spec: docs/superpowers/specs/2026-08-17-debug-console-redesign-design.md §二.4。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from expert_work.protocol import EventType
from expert_work.runtime.runs import DisconnectMode, RunManager, RunRecord, RunStatus
from expert_work.runtime.runs.event_store import InMemoryRunEventStore
from expert_work.runtime.stream_bridge import InMemoryStreamBridge, is_end
from orchestrator.sse import run_agent

_PLAN_V1: dict[str, Any] = {
    "goal": "给客户出续约建议",
    "steps": [
        {"id": "1", "description": "查档案", "status": "completed"},
        {"id": "2", "description": "分析工单", "status": "in_progress"},
    ],
}
_PLAN_V2: dict[str, Any] = {
    "goal": "给客户出续约建议",
    "steps": [
        {"id": "1", "description": "查档案", "status": "completed"},
        {"id": "2", "description": "分析工单", "status": "completed"},
        {"id": "3", "description": "出建议", "status": "pending"},
    ],
}


@dataclass
class _Graph:
    """astream 按脚本吐 updates chunk;aget_state 返回 initial_state(开跑前那次读)
    与 final_state(结束时那次读)—— 两次读用同一个 stub,按调用次序区分。"""

    chunks: list[Any]
    initial_state: dict[str, Any] = field(default_factory=dict)
    final_state: dict[str, Any] = field(default_factory=dict)
    aget_state_raises: bool = False
    aget_state_calls: int = 0

    async def astream(
        self, input: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del input, config, stream_mode
        for chunk in self.chunks:
            yield chunk

    async def aget_state(self, config: Any) -> Any:
        del config
        self.aget_state_calls += 1
        if self.aget_state_raises:
            raise RuntimeError("checkpoint backend down")
        values = self.initial_state if self.aget_state_calls == 1 else self.final_state
        return SimpleNamespace(values=dict(values))


async def _new_record(rm: RunManager) -> RunRecord:
    return await rm.create(
        run_id=uuid4(),
        thread_id=uuid4(),
        tenant_id=uuid4(),
        on_disconnect=DisconnectMode.CANCEL,
    )


async def _drain(bridge: InMemoryStreamBridge, run_id: Any) -> list[Any]:
    events: list[Any] = []
    async for entry in bridge.subscribe(run_id, heartbeat_interval=5.0):
        if is_end(entry):
            break
        events.append(entry)
    return events


async def _run(
    graph: _Graph, *, store: InMemoryRunEventStore | None = None
) -> tuple[list[Any], RunRecord, RunManager]:
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        event_store=store,
    )
    return await _drain(bridge, record.run_id), record, rm


def test_event_type_has_plan_member() -> None:
    assert EventType.PLAN.value == "plan"


@pytest.mark.asyncio
async def test_updates_with_plan_derives_one_plan_frame_after_it() -> None:
    """tools 节点值里带 plan(update_plan 跑完)→ 紧跟一条顶层 plan 帧,payload 与快照相等。"""
    events, _, _ = await _run(
        _Graph(
            chunks=[
                {"agent": {"step_count": 1}},
                {"tools": {"step_count": 1, "plan": _PLAN_V1}},
                {"agent": {"step_count": 2}},
            ]
        )
    )
    names = [e.event for e in events]
    assert names == ["metadata", "updates", "updates", "plan", "updates"]
    plan_frame = events[3]
    assert plan_frame.data == _PLAN_V1
    # updates 里的 plan 键保留(前端 parseTimeline 在读)
    assert events[2].data["tools"]["plan"] == _PLAN_V1
    # plan 帧走 _publish_frame:有 id(seq),且 seq 严格递增
    seqs = [int(e.id.split("-")[1]) for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


@pytest.mark.asyncio
async def test_planner_node_plan_also_derives() -> None:
    """plan_execute 模式:planner 节点通道带 plan → 同样派生。"""
    events, _, _ = await _run(
        _Graph(chunks=[{"planner": {"plan": _PLAN_V1}}, {"agent": {"step_count": 1}}])
    )
    assert [e.event for e in events] == ["metadata", "updates", "plan", "updates"]
    assert events[2].data == _PLAN_V1


@pytest.mark.asyncio
async def test_each_plan_change_derives_its_own_frame() -> None:
    """两次 update_plan → 两条 plan 帧,各自是当时的整份快照(第三方整段覆盖即可)。"""
    events, _, _ = await _run(
        _Graph(
            chunks=[
                {"tools": {"plan": _PLAN_V1}},
                {"agent": {"step_count": 2}},
                {"tools": {"plan": _PLAN_V2}},
            ]
        )
    )
    plans = [e.data for e in events if e.event == "plan"]
    assert plans == [_PLAN_V1, _PLAN_V2]


@pytest.mark.asyncio
async def test_parallel_nodes_last_plan_wins() -> None:
    """同一 chunk 多个节点都写 plan(并行分支)→ 只派生一条,取最后一个节点的(与
    tools 节点 accumulated_state「后写赢」同语义)。"""
    events, _, _ = await _run(
        _Graph(chunks=[{"tools": {"plan": _PLAN_V1}, "planner": {"plan": _PLAN_V2}}])
    )
    plans = [e.data for e in events if e.event == "plan"]
    assert plans == [_PLAN_V2]


@pytest.mark.asyncio
async def test_updates_without_plan_derive_nothing() -> None:
    """节点值没有 plan、或 plan 为 null → 不派生(不能给没用计划的 run 多发空帧)。"""
    events, _, _ = await _run(
        _Graph(chunks=[{"agent": {"step_count": 1}}, {"tools": {"plan": None}}])
    )
    assert "plan" not in [e.event for e in events]


@pytest.mark.asyncio
async def test_plan_frame_is_persisted_for_replay() -> None:
    """plan 帧落库(RunEventStore),replay / 续传端点才能重放它。"""
    store = InMemoryRunEventStore()
    events, record, _ = await _run(_Graph(chunks=[{"tools": {"plan": _PLAN_V1}}]), store=store)
    rows = await store.list(run_id=record.run_id, limit=500)
    plan_rows = [r for r in rows if r.event_name == "plan"]
    assert len(plan_rows) == 1
    assert plan_rows[0].data == _PLAN_V1
    live_seq = int(next(e for e in events if e.event == "plan").id.split("-")[1])
    assert plan_rows[0].seq == live_seq  # 实时 id 与落库 seq 同源


@pytest.mark.asyncio
async def test_existing_plan_is_replayed_right_after_metadata() -> None:
    """会话已有计划(上一 run 留下 / 空闲时 PUT 改的)→ metadata 之后、第一条业务帧之前
    先补发一条 plan,第三方冷启动打开页面就能画任务卡。"""
    graph = _Graph(chunks=[{"agent": {"step_count": 1}}], initial_state={"plan": _PLAN_V1})
    events, _, _ = await _run(graph)
    assert [e.event for e in events] == ["metadata", "plan", "updates"]
    assert events[1].data == _PLAN_V1


@pytest.mark.asyncio
async def test_no_existing_plan_no_extra_frame() -> None:
    """新会话 / 没用过计划 → 不补发。"""
    events, _, _ = await _run(_Graph(chunks=[{"agent": {"step_count": 1}}]))
    assert [e.event for e in events] == ["metadata", "updates"]


@pytest.mark.asyncio
async def test_initial_snapshot_read_failure_does_not_fail_run() -> None:
    """checkpoint 读失败只记日志:run 照常跑完,序列与没有计划时一样。"""
    graph = _Graph(chunks=[{"agent": {"step_count": 1}}], aget_state_raises=True)
    events, record, rm = await _run(graph)
    assert [e.event for e in events] == ["metadata", "updates"]
    assert rm.get(record.run_id).status is RunStatus.SUCCESS


@pytest.mark.asyncio
async def test_initial_snapshot_then_change_gives_two_frames() -> None:
    """开跑补发 v1,run 里 update_plan 改成 v2 → 两条 plan,依次是 v1、v2。"""
    graph = _Graph(
        chunks=[{"agent": {"step_count": 1}}, {"tools": {"plan": _PLAN_V2}}],
        initial_state={"plan": _PLAN_V1},
    )
    events, _, _ = await _run(graph)
    assert [e.data for e in events if e.event == "plan"] == [_PLAN_V1, _PLAN_V2]
