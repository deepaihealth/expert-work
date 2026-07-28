"""Tests for ``run_agent`` ``event_store`` dual-write — Stream H.3 PR 3 (Mini-ADR H-7).

Verifies that every frame the worker emits to ``StreamBridge.publish``
is also mirrored to the durable :class:`RunEventStore`, AND that a
store failure neither blocks the SSE stream nor changes its content.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunEventStore,
    RunEventRecord,
    RunEventStore,
    RunManager,
    RunRecord,
    RunStatus,
)
from expert_work.runtime.stream_bridge import END_SENTINEL, InMemoryStreamBridge
from orchestrator import sse as sse_module
from orchestrator.sse import (
    _BACKGROUND_PERSIST_WRITERS,
    _run_event_persist_errors,
    _run_event_queue_dropped,
    run_agent,
)


@dataclass
class _ScriptedGraph:
    chunks: list[Any]
    chunk_delay_s: float = 0.0
    started: asyncio.Event = field(default_factory=asyncio.Event)
    final_state: dict[str, Any] = field(default_factory=dict)

    async def astream(
        self, _input: Any, _config: Any = None, *, stream_mode: str = "updates"
    ) -> AsyncIterator[Any]:
        for chunk in self.chunks:
            if self.chunk_delay_s:
                await asyncio.sleep(self.chunk_delay_s)
            self.started.set()
            yield chunk

    async def aget_state(self, _config: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(values=self.final_state)


async def _new_record(rm: RunManager) -> RunRecord:
    return await rm.create(
        run_id=uuid4(),
        thread_id=uuid4(),
        tenant_id=uuid4(),
        on_disconnect=DisconnectMode.CANCEL,
    )


async def _drain(bridge: InMemoryStreamBridge, run_id: UUID) -> list[Any]:
    events: list[Any] = []
    async for entry in bridge.subscribe(run_id, heartbeat_interval=5.0):
        if entry is END_SENTINEL:
            break
        events.append(entry)
    return events


async def _await_writers() -> None:
    """二期 PR3 — ``run_agent`` no longer awaits its persist writer directly;
    it hands off frames to a bounded queue and returns once the run itself
    is done. Tests asserting on ``RunEventStore`` contents must wait for the
    background writer task(s) to actually flush first. A no-op when no
    writer was started (e.g. ``event_store=None``)."""
    if _BACKGROUND_PERSIST_WRITERS:
        await asyncio.gather(*_BACKGROUND_PERSIST_WRITERS, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_agent_mirrors_metadata_and_updates_to_event_store() -> None:
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryRunEventStore()
    graph = _ScriptedGraph(chunks=[{"agent": {"step_count": 1}}, {"agent": {"step_count": 2}}])

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
        event_store=store,
    )
    await _await_writers()

    listed = await store.list(run_id=record.run_id)
    # Expect: 1 metadata frame + 2 updates frames = 3 persisted rows.
    assert [r.event_name for r in listed] == ["metadata", "updates", "updates"]
    # Monotonic seqs starting at 0.
    assert [r.seq for r in listed] == [0, 1, 2]
    # First row carries the metadata payload.
    assert listed[0].data["run_id"] == str(record.run_id)


@pytest.mark.asyncio
async def test_run_agent_threads_compaction_sink_publishes_and_persists() -> None:
    """RT-2 PR-4 — run_agent threads a COMPACTION event sink into config; a
    node that fires it (as ``agent_node`` does after the compressor produces a
    summary) lands a ``compaction`` frame on the bridge AND in the durable
    store, on the shared monotonic seq — before the turn's ``updates`` chunk."""
    from orchestrator.graph_builder._config import COMPACTION_SINK_KEY

    payload = {"passes": 2, "tokens_before": 1000, "tokens_after": 300, "summary_chars": 120}

    @dataclass
    class _CompactingGraph:
        async def astream(
            self, _input: Any, config: Any = None, *, stream_mode: str = "updates"
        ) -> AsyncIterator[Any]:
            # Mirror agent_node: read the injected sink and fire it mid-turn,
            # before the node's own update chunk is yielded.
            sink = (config.get("configurable") or {})[COMPACTION_SINK_KEY]
            await sink(payload)
            yield {"agent": {"step_count": 1}}

        async def aget_state(self, _config: Any) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(values={})

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryRunEventStore()

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_CompactingGraph(),
        graph_input={"messages": []},
        config={},
        event_store=store,
    )

    events = await _drain(bridge, record.run_id)
    assert [e.event for e in events] == ["metadata", "compaction", "updates"]
    compaction = next(e for e in events if e.event == "compaction")
    assert compaction.data == payload

    await _await_writers()
    listed = await store.list(run_id=record.run_id)
    assert [r.event_name for r in listed] == ["metadata", "compaction", "updates"]
    assert [r.seq for r in listed] == [0, 1, 2]  # gap-free monotonic
    assert next(r for r in listed if r.event_name == "compaction").data == payload


@pytest.mark.asyncio
async def test_paused_run_emits_and_persists_approval_event() -> None:
    """A run pausing at an approval gate emits a dedicated ``approval`` event
    (mirrored to the event store) so a client surfaces the gate deterministically
    — no need to infer the pause by polling after the terminal ``end`` frame."""
    from datetime import UTC, datetime, timedelta

    from expert_work.persistence import InMemoryApprovalStore
    from expert_work.protocol import ApprovalRequest

    class _SlowStore(InMemoryRunEventStore):
        """Adds latency to ``append_batch`` so the assertion right after
        ``run_agent`` returns actually exercises the drain guarantee —
        against a bare ``InMemoryRunEventStore`` the background writer
        keeps up via incidental scheduling in ``run_agent``'s own
        ``finally`` block (heartbeat-cancel gather + ``publish_end``
        awaits) even without an explicit drain, which would make this
        test pass for the wrong reason."""

        async def append_batch(self, records: Sequence[RunEventRecord]) -> None:
            await asyncio.sleep(0.15)
            await super().append_batch(records)

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = _SlowStore()
    now = datetime.now(UTC)
    request = ApprovalRequest(
        request_id="approval:deadbeef",
        node="tools",
        reason_kind="policy_gate",
        action_summary="approval-gated tool 'bash'",
        proposed_args={"command": "pip install reportlab"},
        requested_at=now,
        timeout_at=now + timedelta(hours=24),
    )
    graph = _ScriptedGraph(
        chunks=[{"tools": {"step_count": 1}}],
        final_state={"pending_approval": request.model_dump(mode="json")},
    )

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
        event_store=store,
        approval_store=InMemoryApprovalStore(),
    )
    # No ``_await_writers()`` here on purpose: the PAUSED path drains the
    # persist queue (``await _drain_persist_queue()``) right after enqueuing
    # the approval frame, before ``run_agent`` returns — the approval row
    # must already be durable at this point, not just eventually once the
    # background writer gets around to it.

    listed = await store.list(run_id=record.run_id)
    assert [r.event_name for r in listed] == ["metadata", "updates", "approval"]
    approval_row = listed[-1]
    assert approval_row.data["run_id"] == str(record.run_id)
    assert approval_row.data["thread_id"] == str(record.thread_id)
    assert approval_row.data["action_summary"] == "approval-gated tool 'bash'"
    assert approval_row.data["proposed_args"] == {"command": "pip install reportlab"}


@pytest.mark.asyncio
async def test_run_agent_mirrors_error_event_when_graph_raises() -> None:
    """A failed run still mirrors metadata + error frames to the store
    so RunDetail can replay the failure."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryRunEventStore()

    @dataclass
    class _FailingGraph:
        async def astream(
            self, _input: Any, _config: Any = None, *, stream_mode: str = "updates"
        ) -> AsyncIterator[Any]:
            yield {"agent": {"step_count": 1}}
            raise RuntimeError("graph failed")

        async def aget_state(self, _config: Any) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(values={})

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_FailingGraph(),
        graph_input={},
        config={},
        event_store=store,
    )
    await _await_writers()

    listed = await store.list(run_id=record.run_id)
    assert [r.event_name for r in listed] == ["metadata", "updates", "error"]
    assert listed[-1].data["name"] == "RuntimeError"


@pytest.mark.asyncio
async def test_store_append_failure_does_not_block_sse() -> None:
    """The durable mirror is graceful-degradation — a store error must
    NEVER stop the live SSE stream. 二期 PR3: the failure now lives in the
    background writer's ``append_batch`` call (the SSE hot path only ever
    does a synchronous ``put_nowait``), so the injection point moves there."""

    class _FailingStore(RunEventStore):
        async def append(self, record: RunEventRecord) -> None:
            raise NotImplementedError("the persist writer calls append_batch, not append")

        async def append_batch(self, records: Sequence[RunEventRecord]) -> None:
            raise RuntimeError("simulated DB outage")

        async def list(
            self,
            *,
            run_id: UUID,
            since_seq: int | None = None,
            limit: int = 100,
        ) -> Sequence[RunEventRecord]:
            return []

        async def delete_for_runs(self, *, run_ids: Sequence[UUID]) -> int:
            return 0

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = _FailingStore()
    graph = _ScriptedGraph(chunks=[{"agent": {"step_count": 1}}])

    before = _run_event_persist_errors.labels(event_name="metadata")._value.get()
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
        event_store=store,
    )
    await _await_writers()

    # SSE stream still delivers metadata + updates + (graph terminates ok).
    events = await _drain(bridge, record.run_id)
    types = [e.event for e in events]
    assert "metadata" in types
    assert "updates" in types
    after = _run_event_persist_errors.labels(event_name="metadata")._value.get()
    assert after > before


@pytest.mark.asyncio
async def test_event_store_optional_keeps_sse_working_without_it() -> None:
    """Backwards-compat: ``event_store=None`` (default) behaves exactly as
    before this PR — no mirror, no errors."""
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
    await _await_writers()  # no-op: event_store=None never starts a writer
    events = await _drain(bridge, record.run_id)
    assert any(e.event == "metadata" for e in events)


@pytest.mark.asyncio
async def test_run_agent_token_sink_publishes_live_only_not_persisted() -> None:
    """Token frames go to the bridge (live) but NOT the durable store — they are
    provisional; the authoritative ``updates`` frame is what replays. Mirrors the
    COMPACTION_SINK_KEY pattern (a node fires the injected sink mid-turn)."""
    from orchestrator.graph_builder._config import TOKEN_SINK_KEY

    @dataclass
    class _TokenGraph:
        async def astream(
            self, _input: Any, config: Any = None, *, stream_mode: str = "updates"
        ) -> AsyncIterator[Any]:
            sink = (config.get("configurable") or {})[TOKEN_SINK_KEY]
            await sink({"step": 0, "channel": "content", "text": "he"})
            await sink({"step": 0, "channel": "content", "text": "llo"})
            yield {"agent": {"step_count": 1}}

        async def aget_state(self, _config: Any) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(values={})

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryRunEventStore()

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_TokenGraph(),
        graph_input={"messages": []},
        config={},
        event_store=store,
    )

    events = await _drain(bridge, record.run_id)
    assert [e.event for e in events] == ["metadata", "token", "token", "updates"]
    token_frames = [e.data for e in events if e.event == "token"]
    assert token_frames == [
        {"step": 0, "channel": "content", "text": "he"},
        {"step": 0, "channel": "content", "text": "llo"},
    ]
    await _await_writers()
    listed = await store.list(run_id=record.run_id)
    assert [r.event_name for r in listed] == ["metadata", "updates"]  # token NOT persisted


# ---------------------------------------------------------------------------
# 二期 PR3 — bounded queue + background batch writer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frames_persist_via_background_writer() -> None:
    """A run with many frames still lands every one in the store, gap-free
    and in order, once the background writer catches up — proving the
    batching (≤_PERSIST_BATCH_MAX or _PERSIST_FLUSH_INTERVAL_S) doesn't
    drop or reorder anything under normal (non-overflow) load."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryRunEventStore()
    total = 10
    graph = _ScriptedGraph(chunks=[{"agent": {"step_count": i}} for i in range(total)])

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
        event_store=store,
    )
    await _await_writers()

    listed = await store.list(run_id=record.run_id)
    assert [r.event_name for r in listed] == ["metadata"] + ["updates"] * total
    assert [r.seq for r in listed] == list(range(total + 1))  # gap-free, no dup


@pytest.mark.asyncio
async def test_queue_overflow_drops_oldest_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run producing more frames than the queue can hold drops the
    OLDEST ones (never the newest) and counts every drop. The constant is
    read inside ``run_agent`` when it builds the queue, so monkeypatching
    the module attribute before the call takes effect."""
    monkeypatch.setattr(sse_module, "_PERSIST_QUEUE_MAX", 4)

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryRunEventStore()
    total = 50
    graph = _ScriptedGraph(chunks=[{"agent": {"step_count": i}} for i in range(total)])

    before = _run_event_queue_dropped.labels(event_name="updates")._value.get()
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
        event_store=store,
    )
    await _await_writers()
    after = _run_event_queue_dropped.labels(event_name="updates")._value.get()

    assert after > before
    listed = await store.list(run_id=record.run_id)
    # Fewer rows landed than were emitted — some were dropped.
    assert len(listed) < total + 1
    # The very last frame emitted is never evicted (nothing newer displaces
    # it) — drop-oldest must have kept it.
    assert listed[-1].seq == total  # metadata=0, updates 1..total
    assert listed[-1].event_name == "updates"


@pytest.mark.asyncio
async def test_terminal_status_waits_for_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """The drain awaited right before the terminal ``set_status`` call
    (二期 PR3) must block until the queued frames actually landed in the
    store — not just until the writer picked them up."""
    flush_finished_at: list[float] = []

    class _SlowStore(InMemoryRunEventStore):
        async def append_batch(self, records: Sequence[RunEventRecord]) -> None:
            await asyncio.sleep(0.05)
            await super().append_batch(records)
            flush_finished_at.append(time.monotonic())

    set_status_entered_at: dict[RunStatus, float] = {}
    original_set_status = RunManager.set_status

    async def _spy_set_status(
        self: RunManager, run_id: UUID, status: RunStatus, **kwargs: Any
    ) -> bool:
        set_status_entered_at[status] = time.monotonic()
        return await original_set_status(self, run_id, status, **kwargs)

    monkeypatch.setattr(RunManager, "set_status", _spy_set_status)

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = _SlowStore()
    graph = _ScriptedGraph(chunks=[{"agent": {"step_count": 1}}])

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
        event_store=store,
    )
    await _await_writers()

    assert flush_finished_at, "the slow store's append_batch never ran"
    assert RunStatus.SUCCESS in set_status_entered_at
    assert set_status_entered_at[RunStatus.SUCCESS] >= flush_finished_at[-1]


@pytest.mark.asyncio
async def test_cancelled_run_does_not_await_drain() -> None:
    """``asyncio.CancelledError`` bypasses the drain entirely — same
    existing law as the ``finally`` teardown comment (awaits during loop
    shutdown are unreliable). ``run_agent`` must re-raise promptly even
    with a slow store; the background writer still flushes the
    already-queued frames afterward."""

    class _SlowStore(InMemoryRunEventStore):
        async def append_batch(self, records: Sequence[RunEventRecord]) -> None:
            await asyncio.sleep(1.0)
            await super().append_batch(records)

    @dataclass
    class _CancellingGraph:
        async def astream(
            self, _input: Any, _config: Any = None, *, stream_mode: str = "updates"
        ) -> AsyncIterator[Any]:
            yield {"agent": {"step_count": 1}}
            raise asyncio.CancelledError

        async def aget_state(self, _config: Any) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(values={})

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = _SlowStore()

    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await run_agent(
            bridge=bridge,
            run_manager=rm,
            record=record,
            graph=_CancellingGraph(),
            graph_input={"messages": []},
            config={},
            event_store=store,
        )
    elapsed = time.monotonic() - started
    # Nowhere near the store's 1s append_batch delay — proves no drain was
    # awaited on this path.
    assert elapsed < 0.5

    await _await_writers()
    listed = await store.list(run_id=record.run_id)
    assert [r.event_name for r in listed] == ["metadata", "updates"]


@pytest.mark.asyncio
async def test_dead_writer_drain_returns_fast() -> None:
    """If the background persist writer dies mid-run, ``_drain_persist_queue``
    must notice via ``writer_task.done()`` and return immediately instead of
    waiting out the full ``_PERSIST_DRAIN_TIMEOUT_S`` (5s) — once the writer
    is gone, nothing will ever call ``queue.task_done()`` again, so
    ``persist_queue.join()`` could otherwise only ever resolve by timing out.

    The writer dies because ``append_batch`` raises ``asyncio.CancelledError``
    — a ``BaseException``, not an ``Exception`` — which ``_flush_batch``'s
    ``except Exception`` does not catch; it propagates out of
    ``_persist_writer`` and asyncio's Task machinery marks the task
    cancelled/done."""

    class _DyingStore(InMemoryRunEventStore):
        async def append_batch(self, records: Sequence[RunEventRecord]) -> None:
            raise asyncio.CancelledError

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = _DyingStore()
    # A tiny real delay before the graph's first chunk (_ScriptedGraph sleeps
    # before yielding when chunk_delay_s is set) is needed so the event loop
    # actually gets a turn to schedule and run writer_task before run_agent
    # reaches ``_drain_persist_queue()`` — with an all-synchronous graph +
    # in-memory stubs nothing in this test ever suspends beforehand, so the
    # writer wouldn't have had a chance to pick up the metadata frame, call
    # the (instantly-raising) ``append_batch``, and die yet.
    graph = _ScriptedGraph(chunks=[{"agent": {"step_count": 1}}], chunk_delay_s=0.02)

    started = time.monotonic()
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={"messages": []},
        config={},
        event_store=store,
    )
    elapsed = time.monotonic() - started
    # Nowhere near the 5s drain timeout — proves the dead-writer fast path
    # returned immediately instead of waiting out persist_queue.join().
    assert elapsed < 1.0

    await _await_writers()  # no-op cleanup: the writer is already done
