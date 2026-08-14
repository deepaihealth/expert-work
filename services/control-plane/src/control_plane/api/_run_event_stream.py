"""Shared SSE event producer — replay (terminal run) vs live attach (active run).

Extracted out of ``api/runs.py`` (Stream H.3 PR 4) so the console endpoint
(``GET /v1/sessions/{thread_id}/runs/{run_id}/events``) and the external
endpoint (``GET /v1/agents/{agent_code}/runs/{run_id}/events``) drive the
exact same wire format off the exact same two backends instead of carrying
two copies that silently drift. This repo has a documented failure mode where
the same semantics get implemented twice and a later constraint lands on only
one copy — P3 is about to fix three bugs on this stream (seq misalignment,
live ignoring ``since_seq``, unpaginated replay), so a single call site is the
point.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from uuid import UUID

from expert_work.runtime.runs import RunEventStore
from expert_work.runtime.runs.store import MAX_LIST_LIMIT
from expert_work.runtime.stream_bridge import HEARTBEAT_SENTINEL, StreamBridge, is_end
from orchestrator.sse import format_sse


def build_event_producer(
    *,
    run_id: UUID,
    is_terminal: bool,
    event_store: RunEventStore | None,
    stream_bridge: StreamBridge,
    since_seq: int | None,
    scope: Callable[[], AbstractAsyncContextManager[None]] | None,
) -> AsyncIterator[bytes]:
    """Return the SSE byte producer for one run.

    * Terminal run → :meth:`RunEventStore.list`, one shot, ordered by seq.
    * Active run → live attach via :meth:`StreamBridge.subscribe` (the
      bridge buffer holds up to 256 events, drop-oldest, so a late opener
      still catches the last 256 frames; older frames depend on the
      durable store).

    ``scope`` is a *factory* for a tenant-scope context manager, not an
    already-constructed one — ``applied_scope(...)`` returns a
    single-use ``_AsyncGeneratorContextManager`` (``__aenter__`` a second
    time raises ``RuntimeError: generator didn't yield``), and P3's
    paginated replay will need to enter scope once per page. The console
    caller passes ``lambda: applied_scope(scope)`` (its DB read must stay
    bound to the resolved target tenant, not the request middleware's
    home-tenant GUC); the external caller has no cross-tenant concept and
    passes ``None`` explicitly — there is no default, so a caller cannot
    silently forget this and fall back to an unscoped read.
    """

    async def _stream_replay() -> AsyncIterator[bytes]:
        """Pull from RunEventStore (one shot, ordered by seq)."""
        if event_store is None:
            # No store wired — yield an end frame so the client closes
            # cleanly instead of waiting forever.
            yield format_sse("end", None)
            return
        # The generator body runs after the handler returned — re-apply
        # the resolved scope (when given) so this DB read stays bound to
        # the target tenant. Call the factory fresh each time — the CM it
        # returns is single-use.
        if scope is not None:
            async with scope():
                rows = await event_store.list(
                    run_id=run_id, since_seq=since_seq, limit=MAX_LIST_LIMIT
                )
        else:
            rows = await event_store.list(run_id=run_id, since_seq=since_seq, limit=MAX_LIST_LIMIT)
        for row in rows:
            yield format_sse(
                row.event_name,
                row.data,
                event_id=f"{row.created_at_ms}-{row.seq}",
            )
        yield format_sse("end", None)

    async def _stream_live() -> AsyncIterator[bytes]:
        """Subscribe to the in-memory bridge (live attach).

        Disconnect is handled via the iterator's GeneratorExit when
        the StreamingResponse is cancelled; the bridge subscription
        naturally tears down.
        """
        async for entry in stream_bridge.subscribe(run_id, heartbeat_interval=15.0):
            if entry is HEARTBEAT_SENTINEL:
                yield b": heartbeat\n\n"
                continue
            if is_end(entry):
                # Task 5 will surface ``entry.data``'s terminal status here;
                # for now the wire format is unchanged (``data: null``).
                yield format_sse("end", None)
                return
            yield format_sse(entry.event, entry.data, event_id=entry.id or None)

    return _stream_replay() if is_terminal else _stream_live()
