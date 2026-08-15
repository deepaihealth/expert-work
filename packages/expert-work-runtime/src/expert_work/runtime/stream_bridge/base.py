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
            the number :meth:`StreamBridge.publish` allocated for this frame
            (and therefore also its durable ``run_event.seq``; the client
            feeds it back as ``since_seq`` / ``Last-Event-ID`` on reconnect).
            ``None`` means **this frame is not replayable** and therefore
            carries no ``id:`` line and consumes no sequence number — today
            that is only the ``token`` frame (a live-only preview; the
            authoritative ``updates`` frame is what replays) and the
            connection-scoped ``gap`` frame the API emits.
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
    async def publish(self, run_id: UUID, event: str, data: Any) -> int:
        """Enqueue one **replayable** event for ``run_id``; return its ``seq``.

        **发号权归日志,不归生产者。** 号的分配与入队必须在同一个临界区里完成
        —— 这是本类的核心不变式:**订阅者看到的帧顺序恒等于 seq 顺序**。调用方
        不得自己发号,拿到返回值原样落库(``run_event.seq``)即可,live 帧 id 与
        durable 行因此天然同源。

        这条不变式是 Redis Streams(``XADD *`` 由服务端在 append 那一刻发号)、
        Kafka(broker 作为单写者在 append 时分配 offset)和 LangGraph Platform
        的一致做法。曾经的写法是生产者先领号、再 ``await`` 推 bridge —— 领号与
        入队之间那个 await 就是乱序的来源,消费侧不得不为此建一整套重排窗口。
        """

    @abc.abstractmethod
    async def publish_ephemeral(self, run_id: UUID, event: str, data: Any) -> None:
        """Enqueue one **one-shot** event: no ``seq``, no ``id:``, not replayable.

        今天只有 ``token`` 帧(逐字预览;权威内容随后由 ``updates`` 帧给出)。
        与 :meth:`publish` 分成两个方法而不是一个可选参数,是为了不让
        ``int | None`` 这种返回值污染每一个调用点。
        """

    @abc.abstractmethod
    async def seed_seq(self, run_id: UUID, *, next_seq: int) -> None:
        """Push this run's allocator past ``next_seq`` (HA failover 接管用)。

        被接管的 run 在新副本上重入时,发号器从 0 起会与前任已落库的行撞
        ``(run_id, seq)`` 主键。实现必须用 ``max(现值, next_seq)`` 写入 ——
        一条迟到的播种不能把发号器往回拨。
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
