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
async def test_token_frames_carry_no_id_and_do_not_consume_seq() -> None:
    bridge = InMemoryStreamBridge()
    run_id = uuid4()
    await bridge.publish(run_id, "metadata", {"a": 1}, seq=0)
    await bridge.publish(run_id, "token", {"text": "hi"})  # 无 seq
    await bridge.publish(run_id, "updates", {"b": 2}, seq=1)
    await bridge.publish_end(run_id, status="success")

    got = [e async for e in bridge.subscribe(run_id, heartbeat_interval=0.05)]
    frames = [e for e in got if e.event not in ("__heartbeat__", "__end__")]
    assert [f.event for f in frames] == ["metadata", "token", "updates"]
    assert frames[1].id is None
    assert [f.id.rsplit("-", 1)[1] for f in frames if f.id is not None] == ["0", "1"]


@pytest.mark.asyncio
async def test_last_event_id_resumes_after_cursor() -> None:
    bridge = InMemoryStreamBridge()
    run_id = uuid4()

    # ``seq`` is what mints the frame id — a cursor test needs real ids.
    await bridge.publish(run_id, "updates", {"step": 1}, seq=0)
    await bridge.publish(run_id, "updates", {"step": 2}, seq=1)
    await bridge.publish(run_id, "updates", {"step": 3}, seq=2)
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
