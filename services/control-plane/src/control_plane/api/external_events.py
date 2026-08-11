"""External event replay / reconnect — ``GET /v1/agents/{agent_code}/runs/{run_id}/events``.

Wire format and both backends (terminal → replay from the durable store, active
→ live attach to the bridge) are identical to the console endpoint; only the
ownership gate differs. Three known limitations carried over verbatim, all
scheduled for P3 (see docs/superpowers/specs/2026-08-11-external-api-v1-design.md §六):

1. ``token`` frames are live-only — a replay never returns them.
2. ``since_seq`` is honoured on the replay path only; the live path ignores it.
3. Replay is capped at one page and appends ``end`` even when truncated.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from control_plane.api._authz import require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    load_owned_run,
)
from control_plane.api._run_event_stream import build_event_producer
from control_plane.api._user_scope import get_user_repo
from control_plane.runtime import AgentRuntime
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.protocol import Principal
from expert_work.runtime.runs import RunEventStore, RunStore
from expert_work.runtime.runs.schemas import TERMINAL_RUN_STATUSES


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store  # type: ignore[no-any-return]


def _get_run_event_store(request: Request) -> RunEventStore | None:
    store: RunEventStore | None = getattr(request.app.state, "run_event_store", None)
    return store


def _get_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime  # type: ignore[no-any-return]


def build_external_events_router() -> APIRouter:
    """Mount the external event-replay endpoints."""
    router = APIRouter(prefix="/v1/agents", tags=["external"])

    @router.get("/{agent_code}/runs/{run_id}/events", response_model=None)
    async def stream_run_events(
        agent_code: str,
        run_id: UUID,
        request: Request,
        principal: Annotated[Principal, Depends(require("session", "read"))],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        event_store: Annotated[RunEventStore | None, Depends(_get_run_event_store)],
        runtime: Annotated[AgentRuntime, Depends(_get_runtime)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
        since_seq: Annotated[int | None, Query(ge=0)] = None,
    ) -> StreamingResponse | JSONResponse:
        """Replay (terminal run) or live-attach (active run) a run's SSE frames.

        The ownership gate — ``load_owned_run`` — runs to completion BEFORE a
        ``StreamingResponse`` is constructed, so a run that doesn't belong to
        this ``(user_id, agent_code)`` is a plain 404 JSON error, never an SSE
        frame inside a ``text/event-stream`` body.
        """
        del principal
        tenant_id: UUID = request.state.tenant_id
        try:
            run, _meta = await load_owned_run(
                tenant_id=tenant_id,
                agent_code=agent_code,
                user_id=user_id,
                run_id=run_id,
                runs=runs,
                threads=threads,
                users=users,
            )
        except ExternalScopeError as exc:
            return external_error(exc)

        is_terminal = run.status in TERMINAL_RUN_STATUSES
        producer = build_event_producer(
            run_id=run_id,
            is_terminal=is_terminal,
            event_store=event_store,
            stream_bridge=runtime.stream_bridge,
            since_seq=since_seq,
            scope=None,
        )
        return StreamingResponse(
            producer,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Expert-Work-Run-Id": str(run_id),
                "X-Expert-Work-Stream-Mode": "replay" if is_terminal else "live",
            },
        )

    return router
