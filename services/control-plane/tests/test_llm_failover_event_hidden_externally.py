"""B-297 PR2 — the ``llm_failover`` run event ("switched to the backup model")
is console-only: ``run_agent`` publishes and persists it, the console replay /
live paths show it, and every external path — live stream-mode
(``sse_consumer``) and replay / reconnect (``build_event_producer``), legacy
and items formats — hides it. User decision: no new external event."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from control_plane.api._run_event_stream import EXTERNAL_HIDDEN_EVENTS, build_event_producer
from expert_work.runtime.runs import DisconnectMode, InMemoryRunEventStore, RunManager, RunStatus
from expert_work.runtime.stream_bridge import InMemoryStreamBridge
from orchestrator.graph_builder._config import FAILOVER_SINK_KEY
from orchestrator.sse import LLM_FAILOVER_EVENT, run_agent, sse_consumer
from orchestrator.stream_items import (
    ITEMS_WIRE_EVENTS,
    PUBLISHED_EVENTS,
    STREAM_FORMAT_ITEMS,
    STREAM_FORMAT_LEGACY,
)

_NOTICE = {
    "step": 1,
    "from_model": "m-primary",
    "to_model": "m-backup",
    "reason": "reasoning_stall",
}
_FORMATS = (STREAM_FORMAT_LEGACY, STREAM_FORMAT_ITEMS)


class _FailoverGraph:
    """Publishes one notice through the injected sink, then one agent update."""

    async def astream(
        self, input: Any, config: Any = None, *, stream_mode: Any = None
    ) -> AsyncIterator[Any]:
        del input, stream_mode
        await config["configurable"][FAILOVER_SINK_KEY](dict(_NOTICE))
        yield {"agent": {"step_count": 1}}


async def _run() -> tuple[InMemoryStreamBridge, RunManager, Any, InMemoryRunEventStore]:
    bridge, rm, store = InMemoryStreamBridge(), RunManager(), InMemoryRunEventStore()
    record = await rm.create(
        run_id=uuid4(), thread_id=uuid4(), tenant_id=uuid4(), on_disconnect=DisconnectMode.CANCEL
    )
    await run_agent(
        bridge=bridge,
        run_manager=rm,
        record=record,
        graph=_FailoverGraph(),
        graph_input={},
        config={"configurable": {"thread_id": str(record.thread_id)}},
        event_store=store,
    )
    return bridge, rm, record, store


async def _never() -> bool:
    return False


def test_the_event_is_in_the_external_hidden_set_and_off_the_items_wire() -> None:
    assert LLM_FAILOVER_EVENT == "llm_failover"
    assert LLM_FAILOVER_EVENT in EXTERNAL_HIDDEN_EVENTS
    assert LLM_FAILOVER_EVENT in PUBLISHED_EVENTS
    assert LLM_FAILOVER_EVENT not in ITEMS_WIRE_EVENTS


@pytest.mark.asyncio
async def test_run_agent_persists_the_notice_for_console_replay() -> None:
    _, _, record, store = await _run()
    rows = [
        e
        for e in await store.list(run_id=record.run_id, limit=500)
        if e.event_name == LLM_FAILOVER_EVENT
    ]
    assert [r.data for r in rows] == [_NOTICE]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_format", _FORMATS)
async def test_live_stream_mode_hides_it_externally_and_shows_it_on_the_console(
    stream_format: str,
) -> None:
    bridge, rm, record, _ = await _run()

    async def _wire(hide: frozenset[str]) -> str:
        chunks = [
            c
            async for c in sse_consumer(
                bridge=bridge,
                record=record,
                run_manager=rm,
                is_disconnected=_never,
                heartbeat_interval=5.0,
                hide_events=hide,
                stream_format=stream_format,
            )
        ]
        return b"".join(chunks).decode()

    external = await _wire(EXTERNAL_HIDDEN_EVENTS)
    assert "event: end" in external  # sanity: the stream really ran to its end
    assert LLM_FAILOVER_EVENT not in external and "m-backup" not in external
    if stream_format == STREAM_FORMAT_LEGACY:
        assert f"event: {LLM_FAILOVER_EVENT}" in await _wire(frozenset())


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_format", _FORMATS)
async def test_replay_hides_it_externally_and_shows_it_on_the_console(stream_format: str) -> None:
    _, _, record, store = await _run()

    async def _wire(hide: frozenset[str]) -> str:
        plan = await build_event_producer(
            run_id=record.run_id,
            run_status=RunStatus.SUCCESS,
            event_store=store,
            stream_bridge=InMemoryStreamBridge(),
            since_seq=None,
            scope=None,
            hide_events=hide,
            stream_format=stream_format,
        )
        return b"".join([c async for c in plan.producer]).decode()

    external = await _wire(EXTERNAL_HIDDEN_EVENTS)
    assert "event: end" in external
    assert LLM_FAILOVER_EVENT not in external and "m-backup" not in external
    if stream_format == STREAM_FORMAT_LEGACY:
        assert f"event: {LLM_FAILOVER_EVENT}" in await _wire(frozenset())
