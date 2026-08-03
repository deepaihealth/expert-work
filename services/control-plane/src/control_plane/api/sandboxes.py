"""``/v1/sandboxes`` admin operations — Stream P (Mini-ADR P-14).

``POST /v1/sandboxes/reap?force=true`` proxies a forced idle-session sweep to
the sandbox-supervisor. system_admin only — it tears down sandboxes across the
platform. Persistent workspace volumes are preserved (the reaper destroys
sessions, not volumes), so the M0→M1 Gate E2E can force a cold start and verify
the workspace survives.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from control_plane.api._authz import _principal
from control_plane.audit import emit
from expert_work.common.observability import current_trace_id_hex
from expert_work.protocol import AuditAction, Principal
from expert_work.runtime.audit.logger import AuditLogger
from orchestrator.tools.sandbox import SandboxRuntime


def _get_sandbox_runtime(request: Request) -> SandboxRuntime | None:
    return getattr(request.app.state, "sandbox_runtime", None)


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def build_sandboxes_router() -> APIRouter:
    router = APIRouter(prefix="/v1/sandboxes", tags=["sandboxes"])

    @router.post("/reap")
    async def reap_sandboxes(
        principal: Annotated[Principal, Depends(_principal)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        request: Request,
        force: Annotated[bool, Query()] = False,
    ) -> dict[str, object]:
        if not principal.is_system_admin:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "PLATFORM_SCOPE_FORBIDDEN",
                    "message": "only a system admin may reap sandboxes",
                },
            )
        client = _get_sandbox_runtime(request)
        if client is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "SANDBOX_SUPERVISOR_UNCONFIGURED",
                    "message": "no sandbox supervisor is wired in this deployment",
                },
            )
        reaped_count = await client.reap(force=force)
        await emit(
            audit,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject_id,
            action=AuditAction.SANDBOX_FORCE_DESTROY,
            resource_type="sandbox",
            resource_id="reap",
            trace_id=current_trace_id_hex(),
            details={"force": force, "reaped_count": reaped_count},
        )
        return {"success": True, "data": {"reaped_count": reaped_count}, "error": None}

    return router
