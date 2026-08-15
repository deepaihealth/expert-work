"""External run control for third-party apps — ``/v1/agents/{agent_code}/runs/...``.

Only run-level cancel lives here. Session-level cancel (``POST
/v1/sessions/{id}:cancel``) is an irreversible close — it flips the thread to
CANCELLED so every later run is refused — and stays a console-only operation.
An end user's "stop" button wants this endpoint: it aborts the current
execution and leaves the conversation usable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api._authz import external_only, require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    load_owned_run,
    load_owned_session,
    lookup_external_user_id,
    reject_nul_path_params,
)
from control_plane.api._user_scope import get_user_repo
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.protocol import Principal
from expert_work.runtime.runs import RunStatus, RunStore
from expert_work.runtime.runs.schemas import TERMINAL_RUN_STATUSES


class ExternalCancelRequest(BaseModel):
    """Body for ``POST /v1/agents/{agent_code}/runs/{run_id}:cancel``.

    ``user_id`` is the app's own end-user identifier and is verified against the
    run's session — an app cannot cancel another end user's run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, max_length=255)


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_run_store(request: Request) -> RunStore:
    return request.app.state.run_store  # type: ignore[no-any-return]


def build_external_runs_router() -> APIRouter:
    """Mount the external run-control endpoints."""
    router = APIRouter(
        prefix="/v1/agents",
        tags=["external"],
        dependencies=[Depends(reject_nul_path_params), Depends(external_only())],
    )

    @router.get(
        "/{agent_code}/runs",
        response_model=None,
        dependencies=[Depends(require("session", "read"))],
    )
    async def list_runs(
        agent_code: str,
        request: Request,
        runs: Annotated[RunStore, Depends(_get_run_store)],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        user_id: Annotated[str, Query(min_length=1, max_length=255)],
        session_id: Annotated[UUID | None, Query()] = None,
        status: Annotated[RunStatus | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> JSONResponse:
        """列出这个终端用户在这个 agent 上跑过的 run。

        ``user_id`` 必填、无默认 —— 漏传是 422,不是「列出整个租户」。

        ``session_id`` 选填:给了就先验它属于 ``(user, agent)``(不属于就
        404,不是空列表 —— 响应不能携带存在性信息),再按那个 thread 过滤。

        ``mint=False``:一个这个租户从没见过的 ``user_id`` 返回空列表,
        不建 ``tenant_user`` 行(P1 复审 T3)。「没跑过 run」和「没这个人」
        对第三方是同一个事实,空列表泄露的信息不比 404 多。

        ``error`` 与 SSE ``error`` 帧携带的是同一个字符串(两者都是
        ``str(exc)``,``orchestrator/sse.py:745``/``:779``),所以这里给出
        它没有新开任何泄露面。
        """
        tenant_id: UUID = request.state.tenant_id
        try:
            end_user_id = await lookup_external_user_id(
                tenant_id=tenant_id, user_id=user_id, users=users
            )
        except ExternalScopeError as exc:
            return external_error(exc)

        thread_ids: list[UUID] | None = None
        if session_id is not None:
            # ``session_id`` given: ownership is authoritative even when
            # ``end_user_id`` is None (this same ``user_id`` never acted
            # before) — ``load_owned_session(mint=False)`` re-derives it and
            # 404s either way, so an unseen ``user_id`` probing a real
            # session still gets SESSION_NOT_FOUND, not an empty-list 200
            # that would leak "that session belongs to someone".
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
            thread_ids = [session_id]
        elif end_user_id is None:
            return JSONResponse(
                {
                    "success": True,
                    "data": {"runs": [], "limit": limit, "offset": offset},
                    "error": None,
                }
            )

        rows = await runs.list_for_tenant(
            tenant_id=tenant_id,
            user_id=end_user_id,
            agent_name=agent_code,
            thread_ids=thread_ids,
            status=status,
            limit=limit,
            offset=offset,
        )
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "runs": [
                        {
                            "run_id": str(r.run_id),
                            "session_id": str(r.thread_id),
                            "status": r.status.value,
                            "created_at": r.created_at.isoformat() if r.created_at else None,
                            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                            "error": r.error,
                        }
                        for r in rows
                    ],
                    "limit": limit,
                    "offset": offset,
                },
                "error": None,
            }
        )

    @router.post("/{agent_code}/runs/{run_id}:cancel", response_model=None)
    async def cancel_run(
        agent_code: str,
        run_id: UUID,
        payload: ExternalCancelRequest,
        request: Request,
        principal: Annotated[Principal, Depends(require("session", "write"))],
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runs: Annotated[RunStore, Depends(_get_run_store)],
    ) -> JSONResponse:
        """Abort an in-flight run. Works in both ``stream`` and ``queue`` mode.

        Two-level, reusing the primitive the tenant-suspend / agent-kill switches
        already rely on: a run owned by THIS replica is aborted immediately via
        the manager's abort event; a run owned by another replica is CAS-flipped
        to INTERRUPTED in the store, and its owner stops within one lease
        heartbeat. Idempotent — cancelling a finished run reports
        ``stopped: false`` rather than erroring.
        """
        tenant_id: UUID = request.state.tenant_id
        try:
            run, _meta = await load_owned_run(
                tenant_id=tenant_id,
                agent_code=agent_code,
                user_id=payload.user_id,
                run_id=run_id,
                runs=runs,
                threads=threads,
                users=users,
            )
        except ExternalScopeError as exc:
            return external_error(exc)

        # A terminal (SUCCESS/ERROR/TIMEOUT/INTERRUPTED) or PAUSED run must
        # report stopped=False without touching either cancel primitive.
        # RunManager.cancel() returns True iff an in-memory record exists —
        # regardless of its status — and that record lingers ~5 minutes past
        # completion (its TTL sweep delay), so calling it unconditionally
        # would report an already-finished run as freshly stopped.
        if run.status in TERMINAL_RUN_STATUSES:
            stopped = False
        else:
            runtime = request.app.state.agent_runtime
            stopped = await runtime.run_manager.cancel(run.run_id) or await runs.request_cancel(
                run_id=run.run_id, tenant_id=tenant_id, updated_at=datetime.now(UTC)
            )
        return JSONResponse(
            {
                "success": True,
                "data": {"run_id": str(run.run_id), "stopped": bool(stopped)},
                "error": None,
            }
        )

    return router
