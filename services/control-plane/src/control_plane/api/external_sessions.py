"""External session listing for third-party apps — ``/v1/agents/{agent_code}/sessions/...``.

Filled in by the external-API P1 plan, Task 3.

Both endpoints require ``user_id`` as a mandatory query parameter (no
default). The console's own session-listing endpoint treats a missing
ownership filter as "list every session in the tenant" — a degradation
this plane must never allow a machine identity to trigger, so FastAPI
rejects a missing ``user_id`` with 422 before a handler ever runs.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from control_plane.api._authz import require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    load_owned_session,
    lookup_external_user_id,
)
from control_plane.api._user_scope import get_user_repo
from control_plane.runtime import AgentRuntime
from control_plane.transcript import read_turns
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.runtime.runs import RunStatus, RunStore
from expert_work.runtime.runs.store import MAX_LIST_LIMIT

logger = logging.getLogger("expert_work.control_plane.api.external_sessions")


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime  # type: ignore[no-any-return]


def _get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store  # type: ignore[no-any-return]


#: A run in any of these statuses counts as "in flight" for the ``running``
#: field. ``QUEUED`` belongs here alongside ``PENDING``/``RUNNING``: a
#: ``mode=queue`` run (``runs.py:830-850``) is created with this exact status
#: and stays in it until some replica's ``RunQueueWorker`` claims it — the
#: third party's most common submit path is queue mode, so omitting it would
#: report ``running: false`` for a run the system has already accepted and
#: will execute, and would contradict the cancel endpoint (``store.py:485``),
#: which already treats ``QUEUED`` as live/cancellable.
_ACTIVE_RUN_STATUSES = (RunStatus.PENDING, RunStatus.QUEUED, RunStatus.RUNNING)


async def _inflight_thread_ids(
    runs: RunStore, *, tenant_id: UUID, thread_ids: Collection[UUID]
) -> set[UUID]:
    """Which of ``thread_ids`` have an active (``_ACTIVE_RUN_STATUSES``) run,
    batched (not one query — or worse, one ``RunManager`` lock acquisition —
    per session row).

    Reads the durable :class:`RunStore`, not :class:`RunManager.has_inflight`
    (a per-process in-memory registry, per its own docstring) — this service
    runs multi-replica, so a run executing on another instance must still
    show ``running: true`` here. ``RunStore.list_for_tenant`` takes at most
    one ``status`` per call, so this issues one tenant-scoped,
    thread-id-filtered query per active status (fixed count, independent of
    how many sessions are on the page) rather than one per row.
    """
    if not thread_ids:
        return set()
    ids: set[UUID] = set()
    for status in _ACTIVE_RUN_STATUSES:
        active = await runs.list_for_tenant(
            tenant_id=tenant_id,
            status=status,
            thread_ids=thread_ids,
            limit=MAX_LIST_LIMIT,
        )
        ids.update(r.thread_id for r in active)
    return ids


def build_external_sessions_router() -> APIRouter:
    """Mount the external session-listing endpoints."""
    router = APIRouter(prefix="/v1/agents", tags=["external"])

    @router.get(
        "/{agent_code}/sessions",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def list_sessions(
        agent_code: str,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> JSONResponse:
        """List an end-user's sessions with one agent.

        ``user_id`` is required — never defaulted — so an app that omits it
        gets a 422, not the tenant's entire session list.

        A ``user_id`` this tenant has never seen returns an empty list, not
        404: this is a read, so it must never mint a ``tenant_user`` row
        (``lookup_external_user_id`` — External-API-v1 P1 review, T3,
        Important), and "no sessions yet" / "no such user" are the same
        fact to a third party either way — an empty list leaks no more than
        a 404 would.
        """
        tenant_id = request.state.tenant_id
        try:
            end_user_id = await lookup_external_user_id(
                tenant_id=tenant_id, user_id=user_id, users=users
            )
        except ExternalScopeError as exc:
            return external_error(exc)
        if end_user_id is None:
            return JSONResponse(
                {
                    "success": True,
                    "data": {"sessions": [], "limit": limit, "offset": offset},
                    "error": None,
                }
            )
        rows = await threads.list_by_tenant(
            tenant_id,
            user_id=end_user_id,
            agent_name=agent_code,
            include_archived=False,
            limit=limit,
            offset=offset,
        )
        inflight = await _inflight_thread_ids(
            runs, tenant_id=tenant_id, thread_ids=[row.thread_id for row in rows]
        )
        sessions = [
            {
                "session_id": str(row.thread_id),
                "title": row.title,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "running": row.thread_id in inflight,
            }
            for row in rows
        ]
        return JSONResponse(
            {
                "success": True,
                "data": {"sessions": sessions, "limit": limit, "offset": offset},
                "error": None,
            }
        )

    @router.get(
        "/{agent_code}/sessions/{session_id}/messages",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def get_messages(
        agent_code: str,
        session_id: UUID,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runtime: Annotated[AgentRuntime, Depends(_get_runtime)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> JSONResponse:
        """A session's message history — 404s unless it belongs to ``(user, agent)``.

        ``mint=False``: a read must never mint a ``tenant_user`` row for a
        ``user_id`` this tenant has never seen — an unrecognized user simply
        cannot own the session, so it 404s the same as any other mismatch.
        """
        tenant_id = request.state.tenant_id
        try:
            await load_owned_session(
                tenant_id=tenant_id,
                agent_code=agent_code,
                user_id=user_id,
                session_id=session_id,
                threads=threads,
                users=users,
                mint=False,
            )
        except ExternalScopeError as exc:
            return external_error(exc)

        checkpointer = runtime.durable_checkpointer
        if checkpointer is None:
            return JSONResponse(
                {
                    "success": True,
                    "data": {"messages": [], "limit": limit, "offset": offset},
                    "error": None,
                }
            )
        try:
            # ``include_hidden=False`` — never surface orchestrator scaffolding
            # (e.g. the CM-1 recovery advisory) to a third-party app.
            turns = await read_turns(checkpointer, session_id, include_hidden=False)
        except Exception:
            logger.warning("external_messages.read_failed", exc_info=True)
            turns = []
        page = turns[offset : offset + limit]
        out = [{"role": t.role, "content": t.content, "channel": t.channel} for t in page]
        return JSONResponse(
            {
                "success": True,
                "data": {"messages": out, "limit": limit, "offset": offset},
                "error": None,
            }
        )

    return router
