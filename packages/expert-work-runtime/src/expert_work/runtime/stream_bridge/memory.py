# ============================================================
# Adapted from bytedance/deer-flow @ 813d3c94efa7fdea6aafcb4f459304db91fcaed0
# Source: backend/packages/harness/deerflow/runtime/stream_bridge/memory.py
# License: MIT (see vendor LICENSE)
# Modifications:
#   - run_id typed as UUID
#   - Class renamed MemoryStreamBridge -> InMemoryStreamBridge (consistent
#     with our InMemoryEventStore / InMemoryThreadMetaStore naming)
#   - Asyncio Condition usage and bounded-buffer logic preserved verbatim
# Last sync: 2026-05-11
# ============================================================

"""In-memory ``StreamBridge`` — single-process pub/sub for SSE."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from expert_work.runtime.stream_bridge.base import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    StreamBridge,
    StreamEvent,
    is_end,
)

logger = logging.getLogger(__name__)


@dataclass
class _RunStream:
    events: list[StreamEvent] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    ended: bool = False
    start_offset: int = 0
    #: Terminal status handed to :meth:`InMemoryStreamBridge.publish_end`.
    #: Lives per run — the end frame is minted from it at subscribe time
    #: rather than stored on the module-level ``END_SENTINEL`` singleton,
    #: which would leak one run's status into every other run's stream.
    end_status: str | None = None


class InMemoryStreamBridge(StreamBridge):
    """Per-run in-memory event log.

    Events for each ``run_id`` are retained in a bounded buffer
    (``queue_maxsize``); when the buffer overflows, the oldest entries
    are dropped and the ``start_offset`` advances accordingly. Late
    subscribers reconnecting with ``Last-Event-ID`` resume from the
    matching offset; if their cursor has fallen out of the buffer they
    resume from the earliest retained event (with a warning).
    """

    def __init__(self, *, queue_maxsize: int = 256) -> None:
        self._maxsize = queue_maxsize
        self._streams: dict[UUID, _RunStream] = {}

    # -- helpers ---------------------------------------------------------------

    def _get_or_create_stream(self, run_id: UUID) -> _RunStream:
        if run_id not in self._streams:
            self._streams[run_id] = _RunStream()
        return self._streams[run_id]

    def _resolve_start_offset(self, stream: _RunStream, last_event_id: str | None) -> int:
        if last_event_id is None:
            return stream.start_offset
        for index, entry in enumerate(stream.events):
            # ``entry.id`` is ``None`` for token frames; ``None == str`` is
            # always False, so they can never match a cursor. Intentional —
            # a non-replayable frame is not a legal resume anchor.
            if entry.id == last_event_id:
                return stream.start_offset + index + 1
        if stream.events:
            logger.warning(
                "stream_bridge.last_event_id_not_found id=%s; replaying from earliest retained",
                last_event_id,
            )
        return stream.start_offset

    # -- StreamBridge API ------------------------------------------------------

    async def publish(self, run_id: UUID, event: str, data: Any, *, seq: int | None = None) -> None:
        stream = self._get_or_create_stream(run_id)
        # ``seq`` comes from the caller (``run_agent``'s durable counter) —
        # the bridge no longer numbers frames itself. ``None`` ⇒ no ``id:``.
        entry_id = None if seq is None else f"{int(time.time() * 1000)}-{seq}"
        entry = StreamEvent(id=entry_id, event=event, data=data)
        async with stream.condition:
            stream.events.append(entry)
            if len(stream.events) > self._maxsize:
                overflow = len(stream.events) - self._maxsize
                del stream.events[:overflow]
                stream.start_offset += overflow
            stream.condition.notify_all()

    async def publish_end(self, run_id: UUID, *, status: str) -> None:
        stream = self._get_or_create_stream(run_id)
        async with stream.condition:
            stream.ended = True
            stream.end_status = status
            stream.condition.notify_all()

    async def subscribe(
        self,
        run_id: UUID,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        stream = self._get_or_create_stream(run_id)
        async with stream.condition:
            next_offset = self._resolve_start_offset(stream, last_event_id)

        while True:
            async with stream.condition:
                if next_offset < stream.start_offset:
                    logger.warning(
                        "stream_bridge.subscriber_fell_behind run_id=%s resumed_offset=%s",
                        run_id,
                        stream.start_offset,
                    )
                    next_offset = stream.start_offset

                local_index = next_offset - stream.start_offset
                if 0 <= local_index < len(stream.events):
                    entry = stream.events[local_index]
                    next_offset += 1
                elif stream.ended:
                    # Minted per subscription so it carries *this* run's
                    # terminal status; read under the lock with the flag.
                    entry = StreamEvent(
                        id=None,
                        event=END_SENTINEL.event,
                        data={"status": stream.end_status},
                    )
                else:
                    try:
                        await asyncio.wait_for(stream.condition.wait(), timeout=heartbeat_interval)
                    except TimeoutError:
                        entry = HEARTBEAT_SENTINEL
                    else:
                        continue

            yield entry
            if is_end(entry):
                return

    async def cleanup(self, run_id: UUID, *, delay: float = 0) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        self._streams.pop(run_id, None)

    async def close(self) -> None:
        self._streams.clear()
