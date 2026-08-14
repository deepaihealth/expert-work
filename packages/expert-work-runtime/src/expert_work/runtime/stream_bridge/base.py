# ============================================================
# Adapted from bytedance/deer-flow @ 813d3c94efa7fdea6aafcb4f459304db91fcaed0
# Source: backend/packages/harness/deerflow/runtime/stream_bridge/base.py
# License: MIT (see vendor LICENSE)
# Modifications:
#   - run_id typed as UUID (expert-work uses UUID throughout)
#   - HEARTBEAT_SENTINEL / END_SENTINEL retained verbatim
# Last sync: 2026-05-11
# ============================================================

"""SSE stream bridge protocol.

Decouples orchestrator workers (producers) from FastAPI SSE endpoints
(consumers); structurally mirrors LangGraph Platform's Queue + StreamManager.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class StreamEvent:
    """Single stream event.

    Attributes:
        id: SSE ``id:`` field, ``f"{created_at_ms}-{seq}"`` where ``seq`` is
            the frame's durable ``run_event.seq`` (used by the client as
            ``since_seq`` / ``Last-Event-ID`` on reconnect).
            ``None`` means **this frame is not replayable** and therefore
            carries no ``id:`` line and consumes no sequence number — today
            that is only the ``token`` frame (a live-only preview; the
            authoritative ``updates`` frame is what replays).
        event: SSE event name, e.g. ``"metadata"`` / ``"updates"`` /
            ``"events"`` / ``"error"`` / ``"end"``.
        data: JSON-serialisable payload.
    """

    id: str | None
    event: str
    data: Any


HEARTBEAT_SENTINEL = StreamEvent(id="", event="__heartbeat__", data=None)
#: Identity marker for "the stream ended". Only its ``event`` name is
#: meaningful: :meth:`StreamBridge.subscribe` yields a **fresh**
#: ``StreamEvent`` carrying this run's terminal status, never this
#: singleton — hanging per-run state on a module-level object would leak
#: one run's status into another's. Consumers must test with :func:`is_end`.
END_SENTINEL = StreamEvent(id="", event="__end__", data=None)


def is_end(entry: StreamEvent) -> bool:
    """True when ``entry`` is a stream's terminal marker.

    Consumers must use this instead of ``entry is END_SENTINEL``: the end
    frame is minted per subscription so it can carry that run's terminal
    status in ``data``.
    """
    return entry.event == END_SENTINEL.event


class StreamBridge(abc.ABC):
    """Abstract async pub/sub bus per ``run_id``."""

    @abc.abstractmethod
    async def publish(self, run_id: UUID, event: str, data: Any, *, seq: int | None = None) -> None:
        """Enqueue a single event for ``run_id`` (producer side).

        ``seq`` is allocated **by the caller** and must be the very same
        number the frame is persisted under (``run_event.seq``) — the bridge
        does not hand out sequence numbers. There used to be two independent
        counters (one here, one in ``run_agent``) and they disagreed on every
        run that streamed tokens, so the ``seq`` a client parsed out of a live
        frame id skipped real frames when replayed as ``since_seq``.

        ``seq=None`` marks a frame that is **not replayable**: it gets no
        ``id:`` line and burns no sequence number. Only ``token`` frames
        qualify today.
        """

    @abc.abstractmethod
    async def publish_end(self, run_id: UUID, *, status: str) -> None:
        """Signal that no more events will be produced for ``run_id``.

        ``status`` is this run's terminal status as the client should see it;
        it rides on the ``data`` of the end frame :meth:`subscribe` yields, so
        a consumer can tell "answered" from "cancelled" without a second REST
        call. Required — a caller that has finished a run always knows how it
        finished.
        """

    @abc.abstractmethod
    def subscribe(
        self,
        run_id: UUID,
        *,
        last_event_id: str | None = None,
        heartbeat_interval: float = 15.0,
    ) -> AsyncIterator[StreamEvent]:
        """Async iterator yielding events for ``run_id`` (consumer side).

        - Yields :data:`HEARTBEAT_SENTINEL` when no event arrives within
          ``heartbeat_interval`` seconds (prevents proxies from closing
          idle connections).
        - Yields exactly one end frame (:func:`is_end` is ``True``, ``data``
          is ``{"status": <this run's terminal status>}``) after the producer
          calls :meth:`publish_end`; the iterator then terminates.
        """

    @abc.abstractmethod
    async def cleanup(self, run_id: UUID, *, delay: float = 0) -> None:
        """Release resources associated with ``run_id``.

        ``delay > 0`` gives late subscribers time to drain remaining events.
        """

    async def close(self) -> None:  # noqa: B027 — intentional no-op default
        """Release backend resources. Default is a no-op (memory backend)."""
