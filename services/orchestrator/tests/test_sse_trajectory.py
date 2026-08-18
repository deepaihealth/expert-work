"""Stream L.L7 — :func:`run_agent` ↔ :class:`TrajectoryRecorder` integration.

Pins the outcome routing (success / failed / max_steps / cancelled),
checks that ``aget_state`` is consulted for the final messages, and
proves the dispatch is fire-and-forget (a slow / failing recorder
does not stall the run's terminal path).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from expert_work.runtime.runs import DisconnectMode, RunManager, RunRecord
from expert_work.runtime.storage import InMemoryObjectStore
from expert_work.runtime.stream_bridge import InMemoryStreamBridge, is_end
from orchestrator.errors import MaxStepsExceededError
from orchestrator.sse import run_agent
from orchestrator.trajectory import TrajectoryRecorder

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _StateSnapshot:
    values: dict[str, Any]


@dataclass
class _ScriptedGraph:
    """``astream`` yields chunks; ``aget_state`` returns the final state."""

    chunks: list[Any] = field(default_factory=list)
    raise_with: BaseException | None = None
    final_messages: list[Any] = field(default_factory=list)
    aget_state_calls: int = 0

    async def astream(
        self,
        input: Any,
        config: Any = None,
        *,
        stream_mode: Any = None,
    ) -> AsyncIterator[Any]:
        del input, config, stream_mode
        for chunk in self.chunks:
            yield chunk
        if self.raise_with is not None:
            raise self.raise_with

    async def aget_state(self, config: Any) -> _StateSnapshot:
        del config
        self.aget_state_calls += 1
        return _StateSnapshot(values={"messages": list(self.final_messages)})


async def _new_record(
    rm: RunManager, *, on_disconnect: DisconnectMode = DisconnectMode.CANCEL
) -> RunRecord:
    return await rm.create(
        run_id=uuid4(),
        thread_id=uuid4(),
        tenant_id=uuid4(),
        on_disconnect=on_disconnect,
    )


async def _drain(bridge: InMemoryStreamBridge, run_id: Any) -> None:
    """Drain the bridge to END so the test doesn't leak the subscriber."""
    async for entry in bridge.subscribe(run_id, heartbeat_interval=5.0):
        if is_end(entry):
            break


async def _drain_trajectory_tasks() -> None:
    """Yield to the event loop so fire-and-forget tasks complete."""
    from orchestrator.sse import _BACKGROUND_TRAJECTORY_TASKS

    # Snapshot first — set may mutate as tasks complete.
    pending = list(_BACKGROUND_TRAJECTORY_TASKS)
    for task in pending:
        # The dispatch wrapper swallows its own errors; ``gather`` with
        # ``return_exceptions=True`` lets us wait for the task without
        # the test caring whether it succeeded — assertions run after.
        await asyncio.gather(task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Outcome routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_records_success_outcome_on_clean_finish() -> None:
    """A normal graph completion lands in the ``success/`` bucket."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryObjectStore()
    recorder = TrajectoryRecorder(object_store=store)
    graph = _ScriptedGraph(
        chunks=[{"agent": {"step_count": 1}}],
        final_messages=[HumanMessage(content="hi"), AIMessage(content="hello")],
    )

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        trajectory_recorder=recorder,
    )
    await _drain(bridge, record.run_id)
    await _drain_trajectory_tasks()

    success_keys = await store.list_prefix(f"trajectories/{record.tenant_id}/success/")
    assert len(success_keys) == 1
    assert str(record.thread_id) in success_keys[0]
    # Two reads: the J.8 pause-check + the L.L7 trajectory recorder.
    # +1: run_agent 开跑前读一次 checkpoint 补发 plan 快照(调试台重设计 PR1)
    assert graph.aget_state_calls == 3


@pytest.mark.asyncio
async def test_run_agent_manifest_optout_skips_recording() -> None:
    """``trajectory_enabled=False`` (policies.trajectory_recording) drops the
    recorder for the run even though the deployment configured one."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryObjectStore()
    recorder = TrajectoryRecorder(object_store=store)
    graph = _ScriptedGraph(
        chunks=[{"agent": {"step_count": 1}}],
        final_messages=[HumanMessage(content="hi"), AIMessage(content="hello")],
    )

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        trajectory_recorder=recorder,
        trajectory_enabled=False,
    )
    await _drain(bridge, record.run_id)
    await _drain_trajectory_tasks()

    assert await store.list_prefix(f"trajectories/{record.tenant_id}/") == []
    # Only the J.8 pause-check read — no trajectory read happened.
    # +1: run_agent 开跑前读一次 checkpoint 补发 plan 快照(调试台重设计 PR1)
    assert graph.aget_state_calls == 2


@pytest.mark.asyncio
async def test_run_agent_records_failed_outcome_on_generic_exception() -> None:
    """An arbitrary exception lands in the ``failed/`` bucket — distinct
    from ``max_steps/``."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryObjectStore()
    recorder = TrajectoryRecorder(object_store=store)
    graph = _ScriptedGraph(
        chunks=[{"agent": {"step_count": 1}}],
        raise_with=RuntimeError("boom"),
        final_messages=[HumanMessage(content="hi")],
    )

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        trajectory_recorder=recorder,
    )
    await _drain(bridge, record.run_id)
    await _drain_trajectory_tasks()

    failed_keys = await store.list_prefix(f"trajectories/{record.tenant_id}/failed/")
    success_keys = await store.list_prefix(f"trajectories/{record.tenant_id}/success/")
    assert len(failed_keys) == 1
    assert len(success_keys) == 0


@pytest.mark.asyncio
async def test_run_agent_records_max_steps_outcome_separately() -> None:
    """``MaxStepsExceededError`` is recognised distinctly so the J.13
    eval gate can weigh budget-exhaustion separately from real
    failures."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryObjectStore()
    recorder = TrajectoryRecorder(object_store=store)
    graph = _ScriptedGraph(
        chunks=[{"agent": {"step_count": 20}}],
        raise_with=MaxStepsExceededError(step_count=20, max_steps=20),
        final_messages=[HumanMessage(content="hi")],
    )

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        trajectory_recorder=recorder,
    )
    await _drain(bridge, record.run_id)
    await _drain_trajectory_tasks()

    max_steps_keys = await store.list_prefix(f"trajectories/{record.tenant_id}/max_steps/")
    failed_keys = await store.list_prefix(f"trajectories/{record.tenant_id}/failed/")
    assert len(max_steps_keys) == 1
    assert len(failed_keys) == 0


@pytest.mark.asyncio
async def test_run_agent_records_cancelled_outcome_on_abort_event() -> None:
    """An abort_event set while streaming routes the trajectory into
    the ``cancelled/`` bucket."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    record.abort_event.set()  # signal cancel before run starts
    store = InMemoryObjectStore()
    recorder = TrajectoryRecorder(object_store=store)
    graph = _ScriptedGraph(
        chunks=[{"agent": {"step_count": 1}}],
        final_messages=[HumanMessage(content="hi")],
    )

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        trajectory_recorder=recorder,
    )
    await _drain(bridge, record.run_id)
    await _drain_trajectory_tasks()

    cancelled_keys = await store.list_prefix(f"trajectories/{record.tenant_id}/cancelled/")
    assert len(cancelled_keys) == 1


# ---------------------------------------------------------------------------
# Opt-out path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_with_no_recorder_does_not_break() -> None:
    """``trajectory_recorder=None`` is the default — opt-out for agents
    whose manifest sets ``policies.trajectory_recording: false``."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    graph = _ScriptedGraph(
        chunks=[{"agent": {"step_count": 1}}],
        final_messages=[],
    )

    # The default is None; passing nothing must not crash.
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
    )
    await _drain(bridge, record.run_id)
    # With no recorder the trajectory read is skipped, but the J.8
    # pause-check still consults final state once.
    # +1: run_agent 开跑前读一次 checkpoint 补发 plan 快照(调试台重设计 PR1)
    assert graph.aget_state_calls == 2


# ---------------------------------------------------------------------------
# Failure isolation — slow / broken recorder does not stall the run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_recorder_does_not_block_run_completion() -> None:
    """A recorder whose ObjectStore put hangs must not delay
    :func:`run_agent` from returning — the L.L7 dispatch is
    fire-and-forget with a 5s outer deadline."""
    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)

    class _HangingStore:
        async def put(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            await asyncio.sleep(60)  # would block past test timeout

    recorder = TrajectoryRecorder(object_store=_HangingStore())  # type: ignore[arg-type]
    graph = _ScriptedGraph(
        chunks=[{"agent": {"step_count": 1}}],
        final_messages=[HumanMessage(content="hi")],
    )

    # If the fire-and-forget dispatch were awaited inline, the run_agent
    # await below would hang. wait_for guards the test itself.
    await asyncio.wait_for(
        run_agent(
            bridge=bridge,
            run_manager=rm,
            record=record,
            graph=graph,
            graph_input={},
            config={"configurable": {"thread_id": str(record.thread_id)}},
            trajectory_recorder=recorder,
        ),
        timeout=2.0,
    )
    await _drain(bridge, record.run_id)


@pytest.mark.asyncio
async def test_aget_state_failure_still_writes_envelope_without_messages() -> None:
    """If the graph cannot serve the final state we still write the
    trajectory envelope (so the J.13 eval gate sees the run happened);
    the ``messages`` list is empty."""

    @dataclass
    class _BrokenAGetState:
        chunks: list[Any] = field(default_factory=list)

        async def astream(
            self,
            input: Any,
            config: Any = None,
            *,
            stream_mode: Any = None,
        ) -> AsyncIterator[Any]:
            del input, config, stream_mode
            for chunk in self.chunks:
                yield chunk

        async def aget_state(self, config: Any) -> Any:
            del config
            msg = "checkpointer broken"
            raise RuntimeError(msg)

    bridge = InMemoryStreamBridge()
    rm = RunManager()
    record = await _new_record(rm)
    store = InMemoryObjectStore()
    recorder = TrajectoryRecorder(object_store=store)
    graph = _BrokenAGetState(chunks=[{"agent": {"step_count": 1}}])

    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=graph,  # type: ignore[arg-type]
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        trajectory_recorder=recorder,
    )
    await _drain(bridge, record.run_id)
    await _drain_trajectory_tasks()

    import json

    success_keys = await store.list_prefix(f"trajectories/{record.tenant_id}/success/")
    assert len(success_keys) == 1
    raw = await store.get(success_keys[0])
    envelope = json.loads(raw.decode("utf-8"))
    # Envelope still produced; messages list is empty when state fetch fails.
    assert envelope["messages"] == []
