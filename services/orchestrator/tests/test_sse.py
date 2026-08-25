"""Tests for the SSE streaming layer — Stream E.14.

Covers test matrix #27 (event order + monotonic ids), #28 (drop-oldest
backpressure), #28b (client disconnect → cancel), #29 (heartbeat) from
STREAM-E-DESIGN § 5.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from expert_work.common.observability import init_tracing
from expert_work.protocol import AuditAction, AuditEntry, AuditResult
from expert_work.runtime.runs import DisconnectMode, RunManager, RunRecord, RunStatus
from expert_work.runtime.stream_bridge import InMemoryStreamBridge, is_end
from orchestrator.sse import (
    _to_jsonable,
    format_sse,
    run_agent,
    sse_consumer,
)
from orchestrator.stream_items import STREAM_FORMAT_ITEMS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _ScriptedGraph:
    """Stub compiled graph: ``astream`` yields a scripted chunk list.

    ``chunk_delay_s`` spaces the chunks out so a test can interleave a
    cancel / disconnect mid-stream. ``started`` flips on first chunk so
    a test can await the run actually beginning. ``final_state`` is what
    ``aget_state`` returns — a J.8 pause test sets ``pending_approval``
    in it.
    """

    chunks: list[Any]
    chunk_delay_s: float = 0.0
    started: asyncio.Event = field(default_factory=asyncio.Event)
    final_state: dict[str, Any] = field(default_factory=dict)

    async def astream(
        self,
        input: Any,
        config: Any = None,
        *,
        stream_mode: Any = None,
    ) -> AsyncIterator[Any]:
        del input, config, stream_mode
        self.started.set()
        for chunk in self.chunks:
            if self.chunk_delay_s:
                await asyncio.sleep(self.chunk_delay_s)
            yield chunk

    async def aget_state(self, config: Any) -> Any:
        """Stub final-state read — J.8 pause-check + L.L7 trajectory both
        consult it. Empty ``final_state`` → the run finalises normally."""
        del config
        return SimpleNamespace(values=dict(self.final_state))


async def _new_record(
    rm: RunManager, *, on_disconnect: DisconnectMode = DisconnectMode.CANCEL
) -> RunRecord:
    return await rm.create(
        run_id=uuid4(),
        thread_id=uuid4(),
        tenant_id=uuid4(),
        on_disconnect=on_disconnect,
    )


async def _drain(bridge: InMemoryStreamBridge, run_id: Any) -> list[Any]:
    """Collect all retained events for a finished run, up to END."""
    events: list[Any] = []
    async for entry in bridge.subscribe(run_id, heartbeat_interval=5.0):
        if is_end(entry):
            break
        events.append(entry)
    return events


# ---------------------------------------------------------------------------
# format_sse
# ---------------------------------------------------------------------------


def test_format_sse_with_id() -> None:
    frame = format_sse("updates", {"a": 1}, event_id="123-0")
    assert frame == b'id: 123-0\nevent: updates\ndata: {"a":1}\n\n'


def test_format_sse_without_id() -> None:
    frame = format_sse("end", None)
    assert frame == b"event: end\ndata: null\n\n"


# ---------------------------------------------------------------------------
# Test matrix #27 — event order + monotonic ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_publishes_metadata_then_chunks_then_end() -> None:
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    graph = _ScriptedGraph(
        chunks=[
            {"agent": {"step_count": 1}},
            {"tools": {"step_count": 1}},
            {"agent": {"step_count": 2}},
        ]
    )

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
    )

    events = await _drain(bridge, record.run_id)
    # metadata + 3 updates.
    assert [e.event for e in events] == ["metadata", "updates", "updates", "updates"]
    assert events[0].data == {
        "run_id": str(record.run_id),
        "thread_id": str(record.thread_id),
    }
    # Event ids are strictly monotonic by sequence suffix.
    seqs = [int(e.id.split("-")[1]) for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    # Run finished SUCCESS.
    assert rm.get(record.run_id).status is RunStatus.SUCCESS


@pytest.mark.asyncio
async def test_run_agent_stamps_node_duration_ms_on_updates() -> None:
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    graph = _ScriptedGraph(
        chunks=[
            {"agent": {"step_count": 1}},
            {"tools": {"step_count": 1}},
        ]
    )
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
    )
    events = await _drain(bridge, record.run_id)
    updates = [e for e in events if e.event == "updates"]
    assert updates, "expected updates frames"
    for e in updates:
        assert isinstance(e.data, dict)
        for node_val in e.data.values():
            assert isinstance(node_val, dict)
            assert "_duration_ms" in node_val
            assert isinstance(node_val["_duration_ms"], int)
            assert node_val["_duration_ms"] >= 0
    # metadata frame must NOT carry a node-level _duration_ms key.
    meta = next(e for e in events if e.event == "metadata")
    assert "_duration_ms" not in meta.data


@pytest.mark.asyncio
async def test_run_agent_serializes_base_messages() -> None:
    """LangGraph chunks carry ``BaseMessage`` objects — they must land
    in the bridge as JSON-safe dicts, not raw objects."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    graph = _ScriptedGraph(
        chunks=[{"agent": {"messages": [AIMessage(content="hello", id="ai-1")]}}]
    )

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
    )

    events = await _drain(bridge, record.run_id)
    update = events[1].data
    msg = update["agent"]["messages"][0]
    assert msg["content"] == "hello"
    assert msg["type"] == "ai"
    # The serialized chunk must round-trip through json.
    json.dumps(events[1].data)


@pytest.mark.asyncio
async def test_run_agent_publishes_error_on_graph_exception() -> None:
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)

    @dataclass
    class _BoomGraph:
        async def astream(
            self, input: Any, config: Any = None, *, stream_mode: Any = None
        ) -> AsyncIterator[Any]:
            del input, config, stream_mode
            raise RuntimeError("graph exploded")
            yield  # pragma: no cover - unreachable, makes this an async gen

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_BoomGraph(),
        graph_input={"messages": []},
        config={},
    )

    events = await _drain(bridge, record.run_id)
    error_events = [e for e in events if e.event == "error"]
    assert len(error_events) == 1
    assert error_events[0].data["name"] == "RuntimeError"
    assert "graph exploded" in error_events[0].data["message"]
    assert rm.get(record.run_id).status is RunStatus.ERROR


@pytest.mark.asyncio
async def test_paused_run_registers_approval_row_and_audits() -> None:
    """Stream J.8 — a run ending with ``pending_approval`` set finalises
    PAUSED, writes an ``agent_approval`` row, and emits APPROVAL_REQUESTED."""
    from datetime import UTC, datetime, timedelta

    from expert_work.persistence import InMemoryApprovalStore
    from expert_work.protocol import ApprovalRequest

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    now = datetime.now(UTC)
    request = ApprovalRequest(
        request_id="approval:deadbeef",
        node="tools",
        reason_kind="policy_gate",
        action_summary="approval-gated tool 'send_email'",
        proposed_args={"to": "ops@example.com"},
        requested_at=now,
        timeout_at=now + timedelta(hours=24),
    )
    graph = _ScriptedGraph(
        chunks=[{"tools": {"step_count": 1}}],
        final_state={"pending_approval": request.model_dump(mode="json")},
    )
    approvals = InMemoryApprovalStore()
    audit = _RecordingAuditLogger()

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
        audit_logger=audit,
        approval_store=approvals,
    )

    assert rm.get(record.run_id).status is RunStatus.PAUSED
    row = await approvals.get_by_run(run_id=record.run_id, tenant_id=record.tenant_id)
    assert row is not None
    assert row.reason_kind == "policy_gate"
    assert row.action_summary == "approval-gated tool 'send_email'"
    actions = [e.action for e in audit.entries]
    assert AuditAction.APPROVAL_REQUESTED in actions
    # A paused run does NOT emit RUN_COMPLETED.
    assert AuditAction.RUN_COMPLETED not in actions


@pytest.mark.asyncio
async def test_sse_consumer_frames_in_order_end_terminates() -> None:
    """End-to-end: worker fills the bridge, sse_consumer drains it into
    SSE frames, last frame is ``event: end``."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    graph = _ScriptedGraph(chunks=[{"agent": {"step_count": 1}}])

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
    )

    frames: list[bytes] = []
    async for frame in sse_consumer(
        bridge=bridge,
        record=record,
        run_manager=rm,
        is_disconnected=_never_disconnected,
        heartbeat_interval=5.0,
    ):
        frames.append(frame)

    text = b"".join(frames).decode()
    assert "event: metadata" in text
    assert "event: updates" in text
    # P3 PR-1 Task 5 —— end 帧不再是 ``data: null``,带终局状态 + run_id。
    assert text.rstrip().endswith(
        'event: end\ndata: {"status":"success","run_id":"' + str(record.run_id) + '"}'
    )


async def _never_disconnected() -> bool:
    return False


# ---------------------------------------------------------------------------
# Test matrix #28 — drop-oldest backpressure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backpressure_drop_oldest() -> None:
    """StreamBridge bounded buffer drops the oldest events on overflow
    (Mini-ADR E-8, M0). A consumer subscribing after overflow resumes
    from the earliest *retained* event — not the earliest published."""
    bridge = InMemoryStreamBridge(queue_maxsize=8)
    run_id = uuid4()

    for seq in range(20):
        await bridge.publish(run_id, "updates", {"seq": seq})
    await bridge.publish_end(run_id, status="success")

    events = await _drain(bridge, run_id)
    # Only the last 8 survive; the first 12 were dropped.
    assert len(events) == 8
    assert [e.data["seq"] for e in events] == list(range(12, 20))


# ---------------------------------------------------------------------------
# Test matrix #28b — client disconnect → cancel run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_disconnect_cancels_inflight_run() -> None:
    """sse_consumer's finally cancels a still-running run when the
    client disconnects and on_disconnect is CANCEL."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm, on_disconnect=DisconnectMode.CANCEL)
    await rm.set_status(record.run_id, RunStatus.RUNNING)

    # Consumer sees a heartbeat (idle bridge), checks disconnect → True.
    frames: list[bytes] = []
    async for frame in sse_consumer(
        bridge=bridge,
        record=record,
        run_manager=rm,
        is_disconnected=_always_disconnected,
        heartbeat_interval=0.05,
    ):
        frames.append(frame)

    assert record.abort_event.is_set()
    assert rm.get(record.run_id).status is RunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_client_disconnect_continue_mode_does_not_cancel() -> None:
    """on_disconnect=CONTINUE leaves the run alone on disconnect."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm, on_disconnect=DisconnectMode.CONTINUE)
    await rm.set_status(record.run_id, RunStatus.RUNNING)

    async for _frame in sse_consumer(
        bridge=bridge,
        record=record,
        run_manager=rm,
        is_disconnected=_always_disconnected,
        heartbeat_interval=0.05,
    ):
        pass

    assert not record.abort_event.is_set()
    assert rm.get(record.run_id).status is RunStatus.RUNNING


@pytest.mark.asyncio
async def test_disconnect_after_run_finished_is_noop() -> None:
    """A finished run is never re-cancelled even on a late disconnect."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    await rm.set_status(record.run_id, RunStatus.SUCCESS)
    await bridge.publish_end(record.run_id, status="success")

    async for _frame in sse_consumer(
        bridge=bridge,
        record=record,
        run_manager=rm,
        is_disconnected=_always_disconnected,
        heartbeat_interval=0.05,
    ):
        pass

    assert not record.abort_event.is_set()
    assert rm.get(record.run_id).status is RunStatus.SUCCESS


async def _always_disconnected() -> bool:
    return True


@pytest.mark.asyncio
async def test_run_agent_stops_early_on_abort_event() -> None:
    """The worker polls record.abort_event between chunks — a cancel
    mid-stream stops it before the remaining chunks are published."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    graph = _ScriptedGraph(
        chunks=[{"agent": {"n": i}} for i in range(10)],
        chunk_delay_s=0.02,
    )

    async def _cancel_mid_stream() -> None:
        """Wait for the run to start + a couple chunks, then abort it."""
        await graph.started.wait()
        await asyncio.sleep(0.03)
        await rm.cancel(record.run_id)

    # Run the worker and the canceller concurrently. ``gather`` (a call,
    # not a bare ``await <name>``) keeps CodeQL's py/ineffectual-statement
    # quiet while still awaiting both to completion.
    await asyncio.gather(
        run_agent(
            bridge=bridge,
            run_manager=rm,
            record=record,
            graph=graph,
            graph_input={"messages": []},
            config={},
        ),
        _cancel_mid_stream(),
    )

    events = await _drain(bridge, record.run_id)
    update_events = [e for e in events if e.event == "updates"]
    # Aborted well before all 10 chunks streamed.
    assert len(update_events) < 10
    assert rm.get(record.run_id).status is RunStatus.INTERRUPTED


# ---------------------------------------------------------------------------
# Test matrix #29 — heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_emitted_while_idle() -> None:
    """An idle bridge (run in flight, no events) makes sse_consumer
    emit SSE comment heartbeats so proxies don't close the connection."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    await rm.set_status(record.run_id, RunStatus.RUNNING)

    seen: list[bytes] = []
    disconnect_after = 3

    async def _disconnect_after_n() -> bool:
        return len(seen) >= disconnect_after

    async for frame in sse_consumer(
        bridge=bridge,
        record=record,
        run_manager=rm,
        is_disconnected=_disconnect_after_n,
        heartbeat_interval=0.05,
    ):
        seen.append(frame)

    assert any(frame == b": heartbeat\n\n" for frame in seen)


async def _beats_while_frames_are_swallowed(**consumer_kwargs: Any) -> tuple[list[bytes], bool]:
    """喂一串「到得了 bridge、出不了 wire」的帧,收集 ``sse_consumer`` 的输出。

    投喂间隔远小于心跳间隔,所以 bridge 一刻不闲 —— 这正是本组测试要制造的
    条件:``InMemoryStreamBridge`` 的心跳只在**它自己**连续 N 秒收不到帧时才
    触发,而帧到得了 bridge 不等于对外有字节。
    """
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    await rm.set_status(record.run_id, RunStatus.RUNNING)

    stop = asyncio.Event()
    seen: list[bytes] = []

    async def _flood() -> None:
        while not stop.is_set():
            # ``updates`` 的 data 不是 Mapping —— ItemStreamConverter._on_updates
            # 的第一个守卫就返回 []。hide_events 变体里它照样进不了 wire。
            await bridge.publish(record.run_id, "updates", "not-a-mapping")
            await asyncio.sleep(0.005)

    async def _disconnect_once_beaten() -> bool:
        return any(f == b": heartbeat\n\n" for f in seen)

    flood = asyncio.create_task(_flood())
    timed_out = False
    try:
        # 没有心跳时这个循环永远不会自己停(投喂不止),靠超时兜住 —— 超时
        # 本身不是断言,断言在调用方看 seen 里有没有心跳。回传这个标志只为
        # 让失败信息分得清「挂到超时」和「流自己结束了」两种形态。
        async with asyncio.timeout(2.0):
            async for frame in sse_consumer(
                bridge=bridge,
                record=record,
                run_manager=rm,
                is_disconnected=_disconnect_once_beaten,
                heartbeat_interval=0.05,
                **consumer_kwargs,
            ):
                seen.append(frame)
    except TimeoutError:
        timed_out = True
    finally:
        stop.set()
        await flood
    return seen, timed_out


@pytest.mark.asyncio
async def test_heartbeat_when_items_converter_swallows_every_frame() -> None:
    """items 转换器吃掉全部帧时,连接上仍要有心跳。

    ``ItemStreamConverter.convert`` 有十个返回空列表的分支。它们叠加 bridge
    「有帧就不发心跳」的语义,会让连接**长时间一个字节都没有**,而客户端的
    读超时按文档承诺的心跳周期设(15s 心跳 → 45s 读超时)。静默一旦越过那道
    闸,客户端判断线重连 —— 而 stream 模式下断开等于取消 run。真栈实测:同一
    个 agent,legacy 最大静默 5.8s,items 28.6s,两边心跳都是 0。
    """
    seen, timed_out = await _beats_while_frames_are_swallowed(stream_format=STREAM_FORMAT_ITEMS)
    assert any(f == b": heartbeat\n\n" for f in seen), (
        f"转换器吃掉全部帧时连接静默、无心跳;挂到超时={timed_out},收到 {len(seen)} 帧:{seen[:5]}"
    )


@pytest.mark.asyncio
async def test_heartbeat_when_hide_events_swallows_every_frame() -> None:
    """``hide_events`` 滤掉全部帧时同理 —— 这条洞不是 items 模式独有的。"""
    seen, timed_out = await _beats_while_frames_are_swallowed(hide_events=frozenset({"updates"}))
    assert any(f == b": heartbeat\n\n" for f in seen), (
        f"hide_events 滤掉全部帧时连接静默、无心跳;挂到超时={timed_out},"
        f"收到 {len(seen)} 帧:{seen[:5]}"
    )


# ---------------------------------------------------------------------------
# Integration — run_agent over a real compiled ReAct graph
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_over_real_react_graph() -> None:
    """run_agent must work against an actual ``build_react_graph`` +
    ``GraphRunner.compile`` graph, not just the scripted stub —
    confirms ``.astream`` chunks serialize cleanly end to end."""
    from collections.abc import Awaitable, Callable, Sequence

    from langchain_core.messages import BaseMessage, HumanMessage

    from expert_work.runtime.checkpointer import make_checkpointer
    from orchestrator import (
        GraphRunner,
        ToolRegistry,
        ToolSpec,
        build_react_graph,
    )
    from orchestrator.llm.providers._streaming import LLMDelta

    @dataclass
    class _EchoLLM:
        async def __call__(
            self,
            *,
            messages: Sequence[BaseMessage],
            tools: Sequence[ToolSpec],
            on_delta: Callable[[LLMDelta], Awaitable[None]] | None = None,
        ) -> AIMessage:
            del tools, on_delta
            return AIMessage(content=f"echo: {messages[-1].content}", id="ai-1")

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)

    async with make_checkpointer("memory") as cp:
        runner = GraphRunner(checkpointer=cp)
        graph = runner.compile(
            build_react_graph(llm_caller=_EchoLLM(), tool_registry=ToolRegistry())
        )
        await run_agent(
            bridge=bridge,
            run_manager=rm,
            record=record,
            graph=graph,
            graph_input={
                "messages": [HumanMessage(content="ping")],
                "step_count": 0,
                "max_steps": 5,
            },
            config={"configurable": {"thread_id": uuid4().hex}},
        )

    events = await _drain(bridge, record.run_id)
    assert events[0].event == "metadata"
    update_events = [e for e in events if e.event == "updates"]
    assert update_events  # at least one node update streamed
    # Every published chunk is JSON-serialisable.
    for ev in events:
        json.dumps(ev.data)
    assert rm.get(record.run_id).status is RunStatus.SUCCESS


# ---------------------------------------------------------------------------
# run-completion audit (F-3)
# ---------------------------------------------------------------------------


@dataclass
class _RecordingAuditLogger:
    """Captures :class:`AuditEntry` writes — structural stand-in for
    ``AuditLogger`` (``run_agent`` only calls ``write``)."""

    entries: list[AuditEntry] = field(default_factory=list)

    async def write(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


@pytest.mark.asyncio
async def test_run_agent_audits_run_completed_on_success() -> None:
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    audit = _RecordingAuditLogger()

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_ScriptedGraph(chunks=[{"agent": {"x": 1}}]),
        graph_input={"messages": []},
        config={},
        audit_logger=audit,
    )

    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry.action is AuditAction.RUN_COMPLETED
    assert entry.result is AuditResult.SUCCESS
    assert entry.resource_id == str(record.thread_id)
    assert entry.details == {"run_id": str(record.run_id), "status": "success"}


@pytest.mark.asyncio
async def test_run_agent_audits_run_failed_on_exception() -> None:
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    audit = _RecordingAuditLogger()

    @dataclass
    class _BoomGraph:
        async def astream(
            self, input: Any, config: Any = None, *, stream_mode: Any = None
        ) -> AsyncIterator[Any]:
            del input, config, stream_mode
            raise RuntimeError("graph exploded")
            yield  # pragma: no cover - unreachable, makes this an async gen

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_BoomGraph(),
        graph_input={"messages": []},
        config={},
        audit_logger=audit,
    )

    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry.action is AuditAction.RUN_FAILED
    assert entry.result is AuditResult.ERROR
    assert entry.reason is not None
    assert "graph exploded" in entry.reason


@pytest.mark.asyncio
async def test_run_agent_no_audit_logger_does_not_crash() -> None:
    """``audit_logger`` omitted — the run still finishes cleanly."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_ScriptedGraph(chunks=[{"agent": {"x": 1}}]),
        graph_input={"messages": []},
        config={},
    )

    assert rm.get(record.run_id).status is RunStatus.SUCCESS


# ---------------------------------------------------------------------------
# Stream K.K10 — TTFT + durable-resume histogram emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_observes_session_first_node_histogram() -> None:
    """First ``updates`` chunk after ``RUNNING`` writes the first-node
    histogram (renamed from ``ttft`` by 一期 Task 3). Every run emits —
    there is no ``is_resume`` gate on it."""
    from orchestrator.sse import _session_first_node_seconds

    before = _session_first_node_seconds._sum.get()  # type: ignore[attr-defined]
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_ScriptedGraph(chunks=[{"agent": {"step_count": 1}}]),
        graph_input={"messages": []},
        config={},
    )
    after = _session_first_node_seconds._sum.get()  # type: ignore[attr-defined]
    assert after > before


@pytest.mark.asyncio
async def test_run_agent_observes_durable_resume_only_when_is_resume() -> None:
    """``expert_work_durable_resume_seconds`` is gated on ``record.is_resume``.

    A first run on a fresh thread (``is_resume=False``) must NOT touch
    the resume histogram; only a run that the caller flagged as
    resuming an existing thread emits.
    """
    from orchestrator.sse import _durable_resume_seconds

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    cold_record = await _new_record(rm)
    before = _durable_resume_seconds._sum.get()  # type: ignore[attr-defined]
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=cold_record,
        graph=_ScriptedGraph(chunks=[{"agent": {"step_count": 1}}]),
        graph_input={"messages": []},
        config={},
    )
    mid = _durable_resume_seconds._sum.get()  # type: ignore[attr-defined]
    assert mid == before, "cold run must not touch the resume histogram"

    resume_record = await rm.create(
        run_id=uuid4(), thread_id=uuid4(), tenant_id=uuid4(), is_resume=True
    )
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=resume_record,
        graph=_ScriptedGraph(chunks=[{"agent": {"step_count": 1}}]),
        graph_input={"messages": []},
        config={},
    )
    after = _durable_resume_seconds._sum.get()  # type: ignore[attr-defined]
    assert after > mid, "resume run must observe the histogram"


# ---------------------------------------------------------------------------
# Stream M Gate — session end-to-end duration histogram emission
# ---------------------------------------------------------------------------


def _session_duration_sum(outcome: str) -> float:
    """Read the labelled ``_sum`` child of expert_work_session_duration_seconds."""
    from orchestrator.sse import _session_duration_seconds

    child = _session_duration_seconds.labels(outcome=outcome)
    return child._sum.get()  # type: ignore[attr-defined,no-any-return]


@pytest.mark.asyncio
async def test_run_agent_session_duration_success_outcome() -> None:
    """A clean run emits to outcome=success on the duration histogram."""
    before = _session_duration_sum("success")
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_ScriptedGraph(chunks=[{"agent": {"step_count": 1}}]),
        graph_input={"messages": []},
        config={},
    )
    after = _session_duration_sum("success")
    assert after > before


@pytest.mark.asyncio
async def test_run_agent_session_duration_interrupted_outcome() -> None:
    """Setting ``abort_event`` mid-stream emits outcome=interrupted."""
    before = _session_duration_sum("interrupted")
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    record.abort_event.set()  # set before astream → first iteration breaks
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_ScriptedGraph(chunks=[{"agent": {"step_count": 1}}]),
        graph_input={"messages": []},
        config={},
    )
    after = _session_duration_sum("interrupted")
    assert after > before


@pytest.mark.asyncio
async def test_run_agent_session_duration_max_steps_outcome() -> None:
    """MaxStepsExceededError from the graph emits outcome=max_steps."""
    from orchestrator.errors import MaxStepsExceededError

    @dataclass
    class _MaxStepsGraph:
        async def astream(
            self,
            input: Any,
            config: Any = None,
            *,
            stream_mode: Any = None,
        ) -> AsyncIterator[Any]:
            del input, config, stream_mode
            raise MaxStepsExceededError(step_count=10, max_steps=5)
            yield  # pragma: no cover — unreachable; satisfies async-iter contract

        async def aget_state(self, config: Any) -> Any:
            return None

    before = _session_duration_sum("max_steps")
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_MaxStepsGraph(),
        graph_input={"messages": []},
        config={},
    )
    after = _session_duration_sum("max_steps")
    assert after > before


@pytest.mark.asyncio
async def test_run_agent_session_duration_error_outcome() -> None:
    """An uncaught exception from the graph emits outcome=error."""

    @dataclass
    class _ErroringGraph:
        async def astream(
            self,
            input: Any,
            config: Any = None,
            *,
            stream_mode: Any = None,
        ) -> AsyncIterator[Any]:
            del input, config, stream_mode
            raise RuntimeError("boom")
            yield  # pragma: no cover

        async def aget_state(self, config: Any) -> Any:
            return None

    before = _session_duration_sum("error")
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_ErroringGraph(),
        graph_input={"messages": []},
        config={},
    )
    after = _session_duration_sum("error")
    assert after > before


# ---------------------------------------------------------------------------
# Stream HX-3 — run-level transient retry (STREAM-HX-DESIGN § 4.5)
# ---------------------------------------------------------------------------


def _retry_count(outcome: str) -> float:
    from orchestrator.run_retry import run_retry_total

    child = run_retry_total.labels(outcome=outcome)
    return child._value.get()  # type: ignore[attr-defined,no-any-return]


@dataclass
class _FlakyGraph:
    """``astream`` raises ``exc`` for the first ``fail_times`` attempts,
    then streams ``chunks``. Records each attempt's ``input`` so a test
    can assert the retry re-invoked with ``None`` (checkpoint
    continuation, J-24)."""

    exc: BaseException
    chunks: list[Any] = field(default_factory=lambda: [{"agent": {"step_count": 1}}])
    fail_times: int = 1
    inputs: list[Any] = field(default_factory=list)
    final_state: dict[str, Any] = field(default_factory=dict)

    async def astream(
        self, input: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del config, stream_mode
        self.inputs.append(input)
        if len(self.inputs) <= self.fail_times:
            raise self.exc
        for chunk in self.chunks:
            yield chunk

    async def aget_state(self, config: Any) -> Any:
        del config
        return SimpleNamespace(values=dict(self.final_state))


def _transient_exc() -> Exception:
    from orchestrator.llm.router import AllProvidersExhaustedError

    return AllProvidersExhaustedError(TimeoutError("529 overloaded"))


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("orchestrator.sse.retry_backoff_s", lambda: 0.01)


@pytest.mark.asyncio
async def test_run_agent_retries_transient_failure_and_recovers(fast_backoff: None) -> None:
    """One transient failure → retry from checkpoint → SUCCESS; the retry
    event is published + persisted ordering-wise, and the second astream
    call gets ``input=None``."""
    before = _retry_count("recovered")
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    graph = _FlakyGraph(exc=_transient_exc())

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
    )

    assert rm.get(record.run_id).status is RunStatus.SUCCESS
    assert graph.inputs == [{"messages": []}, None]
    events = await _drain(bridge, record.run_id)
    retry_events = [e for e in events if e.event == "retry"]
    assert len(retry_events) == 1
    assert retry_events[0].data["attempt"] == 1
    assert retry_events[0].data["error_class"] == "AllProvidersExhaustedError"
    assert not [e for e in events if e.event == "error"]
    assert _retry_count("recovered") == before + 1


@pytest.mark.asyncio
async def test_run_agent_second_failure_lands_error(fast_backoff: None) -> None:
    """Both attempts die → single retry, then the unchanged ERROR path."""
    before = _retry_count("failed_again")
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    graph = _FlakyGraph(exc=_transient_exc(), fail_times=2)

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
    )

    assert rm.get(record.run_id).status is RunStatus.ERROR
    assert len(graph.inputs) == 2  # exactly one retry, never more
    events = await _drain(bridge, record.run_id)
    assert len([e for e in events if e.event == "retry"]) == 1
    assert len([e for e in events if e.event == "error"]) == 1
    assert _retry_count("failed_again") == before + 1


@pytest.mark.asyncio
async def test_run_agent_permanent_error_is_not_retried(fast_backoff: None) -> None:
    from expert_work.runtime.middleware import LLMClientError

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    graph = _FlakyGraph(exc=LLMClientError("400 bad request"))

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
    )

    assert rm.get(record.run_id).status is RunStatus.ERROR
    assert len(graph.inputs) == 1
    events = await _drain(bridge, record.run_id)
    assert not [e for e in events if e.event == "retry"]


@pytest.mark.asyncio
async def test_run_agent_retry_disabled_via_env(
    fast_backoff: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXPERT_WORK_RUN_TRANSIENT_RETRY", "0")
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    graph = _FlakyGraph(exc=_transient_exc())

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
    )

    assert rm.get(record.run_id).status is RunStatus.ERROR
    assert len(graph.inputs) == 1


@pytest.mark.asyncio
async def test_run_agent_guard_rejects_unsafe_dangling_batch(fast_backoff: None) -> None:
    """A trailing dangling tool-call batch that fails the capability check
    blocks the retry — the run takes the unchanged ERROR path."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    dangling = AIMessage(
        content="",
        tool_calls=[{"name": "bash", "args": {}, "id": "call-0"}],
    )
    graph = _FlakyGraph(
        exc=_transient_exc(),
        final_state={"messages": [dangling]},
    )

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
        tool_replay_safe=lambda name: False,
    )

    assert rm.get(record.run_id).status is RunStatus.ERROR
    assert len(graph.inputs) == 1
    events = await _drain(bridge, record.run_id)
    assert not [e for e in events if e.event == "retry"]


@pytest.mark.asyncio
async def test_run_agent_guard_allows_safe_dangling_batch(fast_backoff: None) -> None:
    """A dangling batch whose every call is capability-safe retries fine."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    dangling = AIMessage(
        content="",
        tool_calls=[{"name": "read_file", "args": {}, "id": "call-0"}],
    )
    graph = _FlakyGraph(
        exc=_transient_exc(),
        final_state={"messages": [dangling]},
    )

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
        tool_replay_safe=lambda name: name == "read_file",
    )

    assert rm.get(record.run_id).status is RunStatus.SUCCESS
    assert graph.inputs == [{"messages": []}, None]


@pytest.mark.asyncio
async def test_run_agent_binds_run_id_contextvar(fast_backoff: None) -> None:
    """Stream HX-4 — the worker binds ``current_run_id`` for its whole
    body (log formatter scope) and resets it on exit."""
    from expert_work.common.context import get_current_run_id

    seen: list[Any] = []

    @dataclass
    class _ProbeGraph:
        async def astream(
            self, input: Any, config: Any = None, *, stream_mode: Any = None
        ) -> AsyncIterator[Any]:
            del input, config, stream_mode
            seen.append(get_current_run_id())
            yield {"agent": {"step_count": 1}}

        async def aget_state(self, config: Any) -> Any:
            del config
            return SimpleNamespace(values={})

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_ProbeGraph(),
        graph_input={"messages": []},
        config={},
    )

    assert seen == [record.run_id]
    assert get_current_run_id() is None  # reset after the worker exits


@pytest.mark.asyncio
async def test_run_agent_emits_session_root_span() -> None:
    """10.1 — one ``expert_work.session.run`` root span wraps the whole run,
    carrying ``run_id`` / ``thread_id`` so a run becomes one connected trace."""
    exporter = InMemorySpanExporter()
    init_tracing(
        service_name="test-sse-tracing",
        env="test",
        span_processor=SimpleSpanProcessor(exporter),
    )
    exporter.clear()

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    graph = _ScriptedGraph(chunks=[{"agent": {"step_count": 1}}])

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
    )

    roots = [s for s in exporter.get_finished_spans() if s.name == "expert_work.session.run"]
    assert len(roots) == 1
    assert roots[0].attributes is not None
    assert roots[0].attributes["run_id"] == str(record.run_id)
    assert roots[0].attributes["thread_id"] == str(record.thread_id)
    exporter.clear()


def test_to_jsonable_serializes_pydantic_basemodel() -> None:
    from pydantic import BaseModel

    class _Step(BaseModel):
        id: str
        status: str

    class _Plan(BaseModel):
        goal: str
        steps: tuple[_Step, ...]

    plan = _Plan(goal="ship it", steps=(_Step(id="1", status="pending"),))
    out = _to_jsonable({"planner": {"plan": plan}})
    assert out == {
        "planner": {"plan": {"goal": "ship it", "steps": [{"id": "1", "status": "pending"}]}}
    }


def test_to_jsonable_serializes_dataclass() -> None:
    @dataclass(frozen=True)
    class _Err:
        tool_name: str
        error_class: str
        retryable: bool

    err = _Err(tool_name="exec_python", error_class="transient", retryable=True)
    out = _to_jsonable({"tools": {"tool_failures": [err]}})
    assert out == {
        "tools": {
            "tool_failures": [
                {"tool_name": "exec_python", "error_class": "transient", "retryable": True}
            ]
        }
    }


# ---------------------------------------------------------------------------
# P3 PR-1 Task 5 —— sse_consumer 的 end 帧带终局状态
#
# 这条流是 **POST /v1/agents/{code}/runs 且 mode: "stream"** 走的路
# (runs.py:1091 / runs.py:1797 / external_approvals.py:311),第三方最常用的
# 路径。只改 ``_run_event_stream.py`` 的话它依然发 ``data: null``,两条流的
# end 帧字段集合会分叉。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sse_consumer_end_frame_carries_status_on_cancelled_run() -> None:
    """被取消的 run 走 ``sse_consumer`` —— end 帧必须说清它是被取消的。

    **必须用被取消的 run**:正常跑完的 run 上 status 恒 ``success``,写错的
    映射表也能跑绿(「验证条件矩阵」F 那一行)。
    """
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    record.abort_event.set()  # set before astream → 第一轮就 break → INTERRUPTED

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_ScriptedGraph(chunks=[{"agent": {"step_count": 1}}]),
        graph_input={"messages": []},
        config={},
    )
    assert rm.get(record.run_id).status is RunStatus.INTERRUPTED  # 前置条件

    frames: list[bytes] = []
    async for frame in sse_consumer(
        bridge=bridge,
        record=record,
        run_manager=rm,
        is_disconnected=_never_disconnected,
        heartbeat_interval=5.0,
    ):
        frames.append(frame)

    text = b"".join(frames).decode()
    payload = json.loads(text.rstrip().rsplit("data: ", 1)[1])
    assert payload == {"status": "interrupted", "run_id": str(record.run_id)}


# ---------------------------------------------------------------------------
# PR-A.3 §十.1 —— system_prompt 帧 + sse_consumer(hide_events)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_emits_system_prompt_frame_right_after_metadata() -> None:
    """PR-A.3 §十.1 —— graph_input 首条是 SystemMessage 时,metadata 之后紧跟一帧
    system_prompt(落库、占 seq),再才是 updates。"""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    graph = _ScriptedGraph(chunks=[{"agent": {"step_count": 1}}])

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": [SystemMessage(content="你是评审员"), HumanMessage(content="hi")]},
        config={},
    )

    events = await _drain(bridge, record.run_id)
    assert [e.event for e in events] == ["metadata", "system_prompt", "updates"]
    assert events[1].data == {"text": "你是评审员"}
    seqs = [int(e.id.split("-")[1]) for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


@pytest.mark.asyncio
async def test_run_agent_system_prompt_frame_carries_prompt_inputs() -> None:
    """BUG-16 —— ``prompt_inputs`` 非空时原始 Jinja k/v 捎带进 system_prompt 帧;
    不传 / 空 dict 保持老帧形状(连 ``inputs`` 键都没有)。"""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_ScriptedGraph(chunks=[{"agent": {"step_count": 1}}]),
        graph_input={"messages": [SystemMessage(content="你是评审员"), HumanMessage(content="hi")]},
        config={},
        prompt_inputs={"a": "1"},
    )
    events = await _drain(bridge, record.run_id)
    frame = next(e for e in events if e.event == "system_prompt")
    assert frame.data == {"text": "你是评审员", "inputs": {"a": "1"}}

    for empty in (None, {}):
        bridge = InMemoryStreamBridge()
        rm = RunManager()
        record = await _new_record(rm)
        await run_agent(
            bridge=bridge,
            run_manager=rm,
            record=record,
            graph=_ScriptedGraph(chunks=[{"agent": {"step_count": 1}}]),
            graph_input={
                "messages": [SystemMessage(content="你是评审员"), HumanMessage(content="hi")]
            },
            config={},
            prompt_inputs=empty,
        )
        events = await _drain(bridge, record.run_id)
        frame = next(e for e in events if e.event == "system_prompt")
        assert "inputs" not in frame.data, empty


@pytest.mark.asyncio
async def test_run_agent_skips_system_prompt_frame_without_system_message() -> None:
    """resume(graph_input=None)/ 审批续跑(Command)/ 没有 SystemMessage 首条 —— 不发。"""
    for graph_input in (None, {"messages": [HumanMessage(content="hi")]}, {"messages": []}):
        bridge = InMemoryStreamBridge()
        rm = RunManager()
        record = await _new_record(rm)
        await run_agent(
            bridge=bridge,
            run_manager=rm,
            record=record,
            graph=_ScriptedGraph(chunks=[{"agent": {"step_count": 1}}]),
            graph_input=graph_input,
            config={},
        )
        events = await _drain(bridge, record.run_id)
        assert "system_prompt" not in [e.event for e in events], graph_input


@pytest.mark.asyncio
async def test_sse_consumer_hide_events_filters_frames_but_keeps_ids_monotonic() -> None:
    """对外平面用 hide_events 滤掉 system_prompt;其余帧原样(id 不重排)。"""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_ScriptedGraph(chunks=[{"agent": {"step_count": 1}}]),
        graph_input={"messages": [SystemMessage(content="secret"), HumanMessage(content="hi")]},
        config={},
    )

    async def never_disconnected() -> bool:
        return False

    wire = b"".join(
        [
            chunk
            async for chunk in sse_consumer(
                bridge=bridge,
                record=record,
                run_manager=rm,
                is_disconnected=never_disconnected,
                hide_events=frozenset({"system_prompt"}),
            )
        ]
    )
    assert b"event: system_prompt" not in wire
    assert b"secret" not in wire
    assert b"event: metadata" in wire and b"event: updates" in wire and b"event: end" in wire
