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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api._authz import require
from control_plane.api._external import (
    ExternalScopeError,
    external_error,
    load_owned_run,
)
from control_plane.api._user_scope import get_user_repo
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.protocol import Principal
from expert_work.runtime.runs import RunStore


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
    router = APIRouter(prefix="/v1/agents", tags=["external"])

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
