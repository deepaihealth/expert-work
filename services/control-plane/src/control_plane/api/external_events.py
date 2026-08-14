"""External event replay / reconnect — ``GET /v1/agents/{agent_code}/runs/{run_id}/events``.

Wire format and both backends (terminal → replay from the durable store, active
→ live attach to the bridge) are identical to the console endpoint; only the
ownership gate differs. Three known limitations carried over verbatim, all
scheduled for P3 (see docs/superpowers/specs/2026-08-11-external-api-v1-design.md §六):

1. ``token`` frames are live-only — a replay never returns them.
2. (P3 PR-1 Task 3 已修)``since_seq`` 两条分支都生效。
3. (P3 PR-1 Task 4 已修)回放仍是一页,但截断时以 ``truncated`` 帧 +
   ``X-Expert-Work-Next-Seq`` 头收尾,不再补一个假的 ``end``。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from control_plane.api._authz import external_only, require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    load_owned_run,
    reject_nul_path_params,
)
from control_plane.api._run_event_stream import build_event_producer
from control_plane.api._user_scope import get_user_repo
from control_plane.runtime import AgentRuntime
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.protocol import Principal
from expert_work.runtime.runs import RunEventStore, RunInfo, RunStore
from expert_work.runtime.runs.schemas import TERMINAL_RUN_STATUSES
from expert_work.runtime.stream_bridge import StreamBridge


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store  # type: ignore[no-any-return]


def _get_run_event_store(request: Request) -> RunEventStore | None:
    store: RunEventStore | None = getattr(request.app.state, "run_event_store", None)
    return store


def _get_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime  # type: ignore[no-any-return]


async def build_events_response(
    *,
    run: RunInfo,
    event_store: RunEventStore | None,
    stream_bridge: StreamBridge,
    since_seq: int | None = None,
) -> StreamingResponse:
    """Build the SSE ``StreamingResponse`` for one run — replay or live-attach.

    External-API-v1 P2-a Task 14 — extracted out of ``stream_run_events``'s
    endpoint body (below) so the stream-mode idempotency-replay branch in
    ``agents.py`` (a retried ``POST .../runs`` call that hit an existing
    ``Idempotency-Key`` bound to a stream-mode run) can hand the client the
    exact same wire format this endpoint already produces, instead of
    re-deriving a second copy that could silently drift.

    The ownership gate (``load_owned_run``) is deliberately NOT part of this
    extraction — a replay caller already knows the run belongs to it (it just
    looked it up via the key under its own tenant), so re-running that check
    here would be redundant, not extra safety. Callers pass in the already-
    resolved ``run: RunInfo`` (from ``load_owned_run`` or ``RunStore.
    find_by_idempotency_key`` — both return the same type).

    ``is_terminal`` is derived from ``run.status`` inside this function
    (not accepted as a separate bool parameter) so there is exactly one
    place a caller could get it wrong.

    External-API-v1 P2-a security-review fix (Important) —— the headers now
    also carry ``X-Expert-Work-Session-Id`` (``run.thread_id``), matching the
    first-response header set ``run_agent_for_user`` (``agents.py``) sends
    via ``extra_headers``. Before this fix a stream-mode idempotency replay
    (a retried ``POST .../runs`` that hit the ``Idempotency-Key`` cache, or
    the concurrent-conflict-loser requery) dropped the session id — the
    header set on retry was a strict subset of the header set on first
    response. Every docs-site page describing stream mode tells the caller
    to read this header to continue the conversation; a caller that starts a
    session with no ``session_id`` (mint-on-first-use) *and* an
    ``Idempotency-Key`` *and* only reads response headers (not the SSE body)
    would get the session id on the first response but never again on
    retry — unable to continue the conversation it just started. This
    function backs BOTH the idempotency-replay call sites in ``agents.py``
    and the plain ``GET .../runs/{run_id}/events`` reconnect endpoint below,
    so the latter gains the header too — an addition, not a behavior change,
    for a caller that was already free to ignore headers it doesn't know.
    """
    is_terminal = run.status in TERMINAL_RUN_STATUSES
    plan = await build_event_producer(
        run_id=run.run_id,
        run_status=run.status,
        event_store=event_store,
        stream_bridge=stream_bridge,
        since_seq=since_seq,
        scope=None,
    )
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "X-Expert-Work-Run-Id": str(run.run_id),
        "X-Expert-Work-Session-Id": str(run.thread_id),
        "X-Expert-Work-Stream-Mode": "replay" if is_terminal else "live",
    }
    if plan.next_seq is not None:
        # P3 PR-1 Task 4 —— 回放被截断。同一个值也在 body 末尾的 ``truncated``
        # 帧里(浏览器 EventSource 读不到响应头)。
        headers["X-Expert-Work-Next-Seq"] = str(plan.next_seq)
    return StreamingResponse(
        plan.producer,
        media_type="text/event-stream",
        headers=headers,
    )


def build_external_events_router() -> APIRouter:
    """Mount the external event-replay endpoints."""
    router = APIRouter(
        prefix="/v1/agents",
        tags=["external"],
        dependencies=[Depends(reject_nul_path_params), Depends(external_only())],
    )

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

        return await build_events_response(
            run=run,
            event_store=event_store,
            stream_bridge=runtime.stream_bridge,
            since_seq=since_seq,
        )

    return router
