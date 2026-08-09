"""``/v1/sessions`` CRUD + lifecycle — Stream B.6.

Owns the thin HTTP surface around :class:`ThreadMetaStore` (A.7) that
implements the durable-execution state machine from subsystems/19
§ 3.1. The orchestrator (Stream E) will consume the same rows; this
endpoint is what tenants drive directly.

State transitions enforced:

* create     → ``ACTIVE``
* ``ACTIVE`` → ``PAUSED`` (pause) / ``CANCELLED`` (cancel)
* ``PAUSED`` → ``ACTIVE`` (resume) / ``CANCELLED`` (cancel)
* terminal (``COMPLETED`` / ``FAILED`` / ``CANCELLED``): all transitions
  rejected with ``HTTP 409``

Same exception-to-response policy as ``/v1/agents``: ``str(exc)`` is
never echoed; the public message is a fixed sentence and the cause is
logged server-side.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api._artifact_mime import content_disposition_header, infer_content_type
from control_plane.api._quota_admission import check_admission
from control_plane.api._session_title import first_message_title
from control_plane.api._user_scope import (
    caller_owns_thread,
    get_user_repo,
    resolve_caller_user_id,
    thread_list_filter,
)
from control_plane.audit import emit
from control_plane.quota.base import QuotaService
from control_plane.runtime import AgentRuntime
from control_plane.tenant_scope import (
    CrossTenant,
    applied_scope,
    cross_tenant_query_enabled,
    ensure_single_tenant_scope,
    ensure_tenant_scope,
)
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.persistence.approval import ApprovalStore
from expert_work.persistence.artifact import ArtifactStore
from expert_work.persistence.tenant_config import TenantConfigStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.persistence.workspace import UserWorkspaceStore
from expert_work.protocol import (
    AgentSpecStatus,
    AuditAction,
    AuditResult,
    ThreadMeta,
    ThreadStatus,
)
from expert_work.runtime.audit.logger import AuditLogger
from orchestrator.tools import SandboxSupervisorError, WorkspacePermissionError, WorkspaceStore

logger = logging.getLogger("expert_work.control_plane.sessions")


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


#: Platform fallback agent when a tenant has set no ``default_agent_name``
#: and the caller didn't pick one (Stream R Mini-ADR R-9).
_PLATFORM_FALLBACK_AGENT = "canonical-agent"


class CreateSessionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Stream R (R-9): both optional. When ``agent_name`` is omitted the
    # session resolves the tenant's ``default_agent_name`` (or the platform
    # fallback ``canonical-agent``) so an employee can just "start a chat"
    # without knowing an agent name. ``agent_version`` omitted → the latest
    # ACTIVE version of the resolved agent.
    agent_name: str | None = Field(default=None, min_length=1)
    agent_version: str | None = Field(default=None, min_length=1)


class TransitionPayload(BaseModel):
    """Body shared by ``pause`` / ``cancel`` — ``reason`` is operator-facing."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=512)


class RenamePayload(BaseModel):
    """Body for ``PATCH /v1/sessions/{id}`` — rename the session (title)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------------------
# Dependency providers (pull from request.app.state)
# ---------------------------------------------------------------------------


def _get_thread_repo(request: Request) -> ThreadMetaStore:
    return request.app.state.thread_meta_repo  # type: ignore[no-any-return]


def _get_agent_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime  # type: ignore[no-any-return]


def _get_agent_repo(request: Request) -> AgentSpecStore:
    return request.app.state.agent_spec_repo  # type: ignore[no-any-return]


def _get_approval_store(request: Request) -> ApprovalStore:
    return request.app.state.approval_store  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def _get_quota(request: Request) -> QuotaService:
    return request.app.state.quota_service  # type: ignore[no-any-return]


def _get_tenant_config_repo(request: Request) -> TenantConfigStore:
    return request.app.state.tenant_config_repo  # type: ignore[no-any-return]


def _get_workspace_store(request: Request) -> UserWorkspaceStore:
    return request.app.state.user_workspace_store  # type: ignore[no-any-return]


def _get_artifact_store(request: Request) -> ArtifactStore:
    return request.app.state.artifact_store  # type: ignore[no-any-return]


def _get_workspace_file_client(request: Request) -> WorkspaceStore | None:
    # Named distinctly from ``_get_workspace_store`` above (that one serves
    # the ``UserWorkspaceStore`` volume-metadata row, not file bytes).
    return request.app.state.workspace_store  # type: ignore[no-any-return]


async def _backfill_titles(
    items: list[ThreadMeta],
    *,
    threads: ThreadMetaStore,
    checkpointer: BaseCheckpointSaver[Any] | None,
) -> list[ThreadMeta]:
    """Fill in a title for any listed thread that has none.

    Threads created before auto-titling carry a NULL title and render as a
    ``thread_id`` hash. Derive the title from the thread's checkpoint (its first
    user message) and persist it, so the fix is one-time per thread. Bounded to
    the listed page. Best-effort — a missing checkpoint / read error leaves the
    hash fallback. Callers run this inside the tenant scope so the persist
    respects RLS.
    """
    if checkpointer is None:
        return items
    out: list[ThreadMeta] = []
    for m in items:
        if m.title is None:
            title = await first_message_title(checkpointer, m.thread_id)
            if title:
                await threads.update_title(m.thread_id, title, tenant_id=m.tenant_id)
                m = m.model_copy(update={"title": title})
        out.append(m)
    return out


def _safe_workspace_relpath(path: str) -> str | None:
    """Return the cleaned relative path, or ``None`` if it escapes the workspace.

    The ``path`` query param round-trips through the client untrusted, so the
    download endpoint re-checks it here (the supervisor re-validates again at
    its own boundary — defence in depth). Rejects absolute paths and any
    ``..`` segment that would climb out of ``/workspace``.
    """
    cleaned = path.strip()
    if not cleaned or cleaned.startswith("/") or ".." in PurePosixPath(cleaned).parts:
        return None
    return cleaned


async def _resolve_agent_selection(
    *,
    tenant_id: UUID,
    payload_name: str | None,
    payload_version: str | None,
    agents: AgentSpecStore,
    tenant_config: TenantConfigStore,
) -> tuple[str, str] | None:
    """Resolve ``(agent_name, agent_version)`` for a session create (R-9).

    Precedence for the name: explicit ``payload_name`` → the tenant's
    ``default_agent_name`` → the platform fallback ``canonical-agent``. When
    ``payload_version`` is absent the latest ACTIVE version of the resolved
    name is used. Returns ``None`` when no ACTIVE version exists (the caller
    surfaces ``AGENT_NOT_FOUND``).
    """
    name = payload_name
    if name is None:
        config = await tenant_config.get(tenant_id=tenant_id)
        name = (config.default_agent_name if config else None) or _PLATFORM_FALLBACK_AGENT

    if payload_version is not None:
        return name, payload_version

    # Latest ACTIVE version (list_by_tenant is newest-first).
    active = await agents.list_by_tenant(
        tenant_id=tenant_id, status=AgentSpecStatus.ACTIVE, name=name, limit=1
    )
    if not active:
        return None
    return name, active[0].version


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _envelope_error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "data": None,
            "error": {"code": code, "message": message},
        },
    )


def _conflict(message: str) -> JSONResponse:
    return _envelope_error("SESSION_STATE_CONFLICT", message, 409)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_sessions_router() -> APIRouter:
    router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

    @router.post("", status_code=201)
    async def create_session(
        payload: CreateSessionPayload,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        agents: Annotated[AgentSpecStore, Depends(_get_agent_repo)],
        tenant_config: Annotated[TenantConfigStore, Depends(_get_tenant_config_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        quota: Annotated[QuotaService, Depends(_get_quota)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        trace_id = current_trace_id_hex()

        # Stream R (R-9): resolve which agent to run. An employee may omit
        # the name (→ tenant default → platform canonical-agent) and/or the
        # version (→ latest ACTIVE).
        selection = await _resolve_agent_selection(
            tenant_id=tenant_id,
            payload_name=payload.agent_name,
            payload_version=payload.agent_version,
            agents=agents,
            tenant_config=tenant_config,
        )
        if selection is None:
            return _envelope_error(
                "AGENT_NOT_FOUND",
                "no active agent for this tenant (set a default or register one)",
                422,
            )
        agent_name, agent_version = selection

        # Admission (Stream C.5b): consume one token from the tenant's
        # QPS bucket before doing any other work. Denial emits a
        # ``quota:rate_limit_denied`` audit row and returns 429 with
        # ``Retry-After``; we never proceed to DB writes.
        denial = await check_admission(
            quota=quota,
            audit=audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            agent=agent_name,
            resource_kind="session",
        )
        if denial is not None:
            return denial

        # The agent must exist + be ACTIVE (not deprecated / soft-deleted)
        # for the tenant. Otherwise the session would point at a row a
        # later GET would fail to resolve.
        record = await agents.get(
            tenant_id=tenant_id,
            name=agent_name,
            version=agent_version,
        )
        if record is None or record.status is not AgentSpecStatus.ACTIVE:
            await emit(
                audit,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=AuditAction.SESSION_WRITE,
                resource_type="session",
                resource_id=f"{agent_name}/{agent_version}",
                result=AuditResult.ERROR,
                reason="agent_not_found",
                trace_id=trace_id,
            )
            return _envelope_error(
                "AGENT_NOT_FOUND",
                "agent does not exist or is not active for this tenant",
                422,
            )

        # Stream J.14 — stamp the owning user (the caller's own tenant_user.id).
        # None for machine principals (service / service_account) → an unowned
        # thread. The playground always runs as the logged-in user; there is no
        # impersonation.
        user_id = await resolve_caller_user_id(request, users)
        thread_id = uuid4()
        meta = await threads.create(
            thread_id=thread_id,
            tenant_id=tenant_id,
            created_by=actor_id,
            user_id=user_id,
            agent_name=agent_name,
            agent_version=agent_version,
        )
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.SESSION_WRITE,
            resource_type="session",
            resource_id=str(thread_id),
            trace_id=trace_id,
            details={"agent": f"{agent_name}/{agent_version}"},
        )
        return JSONResponse(
            status_code=201,
            content={"success": True, "data": meta.model_dump(mode="json")},
        )

    @router.get("/{thread_id}")
    async def get_session(
        thread_id: UUID,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W2 read scope — a concrete id lets a system_admin drill into a
        # foreign tenant's session from the tenant switcher; "*" is
        # meaningless (a thread belongs to one tenant).
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/sessions/{thread_id}",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target_tenant = scope.tenant_id
        async with applied_scope(scope):
            meta = await threads.get(thread_id, tenant_id=target_tenant)
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        # Stream J.14 — a user-owned thread is private to its owner.
        # 404 (not 403) so cross-user existence is never revealed.
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")
        await emit(
            audit,
            tenant_id=request.state.tenant_id,
            actor_id=request.state.actor_id,
            action=AuditAction.SESSION_READ,
            resource_type="session",
            resource_id=str(thread_id),
            trace_id=current_trace_id_hex(),
        )
        return JSONResponse({"success": True, "data": meta.model_dump(mode="json")})

    @router.get("/{thread_id}/workspace")
    async def get_session_workspace(
        thread_id: UUID,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        workspaces: Annotated[UserWorkspaceStore, Depends(_get_workspace_store)],
        artifacts: Annotated[ArtifactStore, Depends(_get_artifact_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W2 read scope — see ``get_session``.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        """Playground-Uplift D4 — the thread user's persistent workspace + artifacts.

        Read-only: ``workspaces.get`` never provisions a row, so a ``null``
        workspace truthfully means "no VM has ever started for this user". Keyed
        on the thread's ``user_id`` (its owning user), gated by the same
        thread-ownership check as GET.
        """
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/sessions/{thread_id}/workspace",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target_tenant = scope.tenant_id
        async with applied_scope(scope):
            meta = await threads.get(thread_id, tenant_id=target_tenant)
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")
        if meta.user_id is None:
            # Machine/unowned thread — no per-user workspace.
            return JSONResponse({"success": True, "data": {"workspace": None, "artifacts": []}})
        async with applied_scope(scope):
            workspace = await workspaces.get(tenant_id=target_tenant, user_id=meta.user_id)
            arts = await artifacts.list_for_user(tenant_id=target_tenant, user_id=meta.user_id)
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "workspace": workspace.model_dump(mode="json") if workspace else None,
                    "artifacts": [a.model_dump(mode="json") for a in arts],
                },
            }
        )

    @router.get("/{thread_id}/workspace/files")
    async def list_session_workspace_files(
        thread_id: UUID,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_file_client)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W2 read scope — see ``get_session``.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> JSONResponse:
        """Workspace browse — the files in the thread user's persistent volume.

        Read-only inventory for the playground inspector. Same ownership gate
        as the workspace endpoint; keyed on the thread's ``user_id`` (its
        owning user). A machine/unowned thread, an absent supervisor, or an
        empty volume all return ``[]``.
        """
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/sessions/{thread_id}/workspace/files",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target_tenant = scope.tenant_id
        async with applied_scope(scope):
            meta = await threads.get(thread_id, tenant_id=target_tenant)
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")
        if meta.user_id is None or workspace_store is None:
            return JSONResponse({"success": True, "data": {"files": []}})
        try:
            entries = await workspace_store.list_files(
                tenant_id=target_tenant, user_id=meta.user_id
            )
        except WorkspacePermissionError as exc:
            # 与 /v1/workspace/files 同一坑:权限失败(共享 uid 没配上/存量目录属
            # 主没迁移/mode 不对)是服务端配置问题,不是"这个用户没有文件"。吞成
            # 空列表会让用户看到"工作区是空的"——比 404 更坏,连"出错了"都看不
            # 到。detail 只给固定文案,路径/uid/mode 只进下面这条结构化日志。
            logger.warning("session_workspace.list_permission_denied", exc_info=True)
            raise HTTPException(status_code=500, detail="workspace listing unavailable") from exc
        except SandboxSupervisorError:
            logger.warning("session_workspace.list_failed", exc_info=True)
            return JSONResponse({"success": True, "data": {"files": []}})
        files = [{"path": e.path, "size": e.size} for e in entries]
        return JSONResponse({"success": True, "data": {"files": files}})

    @router.get("/{thread_id}/workspace/file", response_model=None)
    async def download_session_workspace_file(
        thread_id: UUID,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_file_client)],
        path: Annotated[str, Query()],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W2 read scope — see ``get_session``.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> Response:
        """Download one file from the thread user's persistent workspace volume.

        MIME-aware + XSS-safe (active content always ``attachment`` +
        ``nosniff``), mirroring the artifact download. ``path`` is validated
        here and again at the supervisor boundary. 404 hides cross-user /
        missing-file / no-supervisor behind one opaque response.
        """
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/sessions/{thread_id}/workspace/file",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target_tenant = scope.tenant_id
        async with applied_scope(scope):
            meta = await threads.get(thread_id, tenant_id=target_tenant)
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")
        safe_path = _safe_workspace_relpath(path)
        if safe_path is None:
            raise HTTPException(status_code=400, detail="invalid workspace path")
        if meta.user_id is None or workspace_store is None:
            raise HTTPException(status_code=404, detail="file not found")
        try:
            data = await workspace_store.read_file(
                tenant_id=target_tenant, user_id=meta.user_id, path=safe_path
            )
        except WorkspacePermissionError as exc:
            # 权限失败是服务端配置问题,不是"这个文件不存在"——404 的语义是
            # "不存在 / 你不该知道它存在";塞进 404 会让用户看到一份列在上一屏
            # 却"文件不存在"的报错。必须排在下面的 SandboxSupervisorError 之
            # 前——它是那个类的子类,顺序反了这一分支永远走不到。
            logger.warning("session_workspace.read_permission_denied", exc_info=True)
            raise HTTPException(status_code=500, detail="workspace file unavailable") from exc
        except SandboxSupervisorError as exc:
            logger.warning("session_workspace.read_failed", exc_info=True)
            raise HTTPException(status_code=404, detail="file not found") from exc
        filename = PurePosixPath(safe_path).name or "download"
        inferred = infer_content_type(kind="other", path=safe_path)
        headers = {
            "Content-Disposition": content_disposition_header(
                filename, disposition=inferred.disposition
            ),
            "X-Content-Type-Options": "nosniff",
        }
        return Response(content=data, media_type=inferred.content_type, headers=headers)

    @router.delete("/{thread_id}/workspace/file", response_model=None)
    async def delete_session_workspace_file(
        thread_id: UUID,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_file_client)],
        path: Annotated[str, Query()],
    ) -> JSONResponse:
        """Delete one file from the thread user's persistent workspace volume.

        Playground cleanup. Same ownership gate as browse/download; the
        supervisor refuses reserved prefixes (seeded machinery). 404 hides
        cross-user / no-supervisor; a missing file is an idempotent no-op.
        """
        tenant_id: UUID = request.state.tenant_id
        meta = await threads.get(thread_id, tenant_id=tenant_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")
        safe_path = _safe_workspace_relpath(path)
        if safe_path is None:
            raise HTTPException(status_code=400, detail="invalid workspace path")
        if meta.user_id is None or workspace_store is None:
            raise HTTPException(status_code=404, detail="file not found")
        try:
            await workspace_store.delete_file(
                tenant_id=tenant_id, user_id=meta.user_id, path=safe_path
            )
        except WorkspacePermissionError as exc:
            # 同上——权限失败不是"这个文件不存在",必须排在 SandboxSupervisorError
            # 之前(它的子类,顺序反了永远走不到)。
            logger.warning("session_workspace.delete_permission_denied", exc_info=True)
            raise HTTPException(status_code=500, detail="workspace file unavailable") from exc
        except SandboxSupervisorError as exc:
            logger.warning("session_workspace.delete_failed", exc_info=True)
            raise HTTPException(status_code=404, detail="file not found") from exc
        return JSONResponse({"success": True, "data": {"deleted": safe_path}})

    @router.get("/{thread_id}/workspace/artifacts/{name:path}/download", response_model=None)
    async def download_session_artifact(
        thread_id: UUID,
        name: str,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        artifacts: Annotated[ArtifactStore, Depends(_get_artifact_store)],
        workspace_store: Annotated[WorkspaceStore | None, Depends(_get_workspace_file_client)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        # W2 read scope — see ``get_session``.
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,
    ) -> Response:
        """Download the thread user's artifact by logical name (latest version).

        Thread-scoped (the thread's owning user), unlike the caller-scoped
        ``/v1/artifacts/download``. Resolves the latest version's workspace
        path + proxies the bytes via the supervisor. 404 hides cross-user /
        missing / no-supervisor.
        """
        scope = await ensure_single_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/sessions/{thread_id}/workspace/artifacts/{name}/download",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        target_tenant = scope.tenant_id
        async with applied_scope(scope):
            meta = await threads.get(thread_id, tenant_id=target_tenant)
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")
        if meta.user_id is None or workspace_store is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        async with applied_scope(scope):
            version = await artifacts.get_latest_version(
                tenant_id=target_tenant, user_id=meta.user_id, name=name
            )
        if version is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        try:
            data = await workspace_store.read_file(
                tenant_id=target_tenant, user_id=meta.user_id, path=version.path_in_workspace
            )
        except WorkspacePermissionError as exc:
            # 元数据行存在,内容读不动是权限问题(服务端配置),不是"这个 artifact
            # 不存在"——同六处 workspace 端点的坑,必须排在下面的
            # SandboxSupervisorError 之前(它的子类,顺序反了永远走不到)。
            logger.warning("session_artifact.permission_denied", exc_info=True)
            raise HTTPException(status_code=500, detail="artifact content unavailable") from exc
        except SandboxSupervisorError as exc:
            logger.warning("session_artifact.content_unavailable", exc_info=True)
            raise HTTPException(status_code=404, detail="artifact content not found") from exc
        # Path-based MIME + XSS-safe disposition (active content → attachment),
        # same as the workspace-file download; filename is the logical name.
        inferred = infer_content_type(kind="other", path=version.path_in_workspace)
        headers = {
            "Content-Disposition": content_disposition_header(
                name, disposition=inferred.disposition
            ),
            "X-Content-Type-Options": "nosniff",
        }
        return Response(content=data, media_type=inferred.content_type, headers=headers)

    @router.delete("/{thread_id}/workspace/artifacts/{name:path}", response_model=None)
    async def delete_session_artifact(
        thread_id: UUID,
        name: str,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        artifacts: Annotated[ArtifactStore, Depends(_get_artifact_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Soft-delete the thread user's artifact by name (playground cleanup).

        Thread-scoped (the thread's owning user), unlike caller-scoped
        ``DELETE /v1/artifacts/{name}``. Metadata only — the workspace bytes
        remain (deletable separately as a file). 404 hides cross-user /
        already-deleted / unknown.
        """
        tenant_id: UUID = request.state.tenant_id
        meta = await threads.get(thread_id, tenant_id=tenant_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")
        if meta.user_id is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        hit = await artifacts.soft_delete(
            tenant_id=tenant_id, user_id=meta.user_id, name=name, now=datetime.now(UTC)
        )
        if not hit:
            raise HTTPException(status_code=404, detail="artifact not found")
        actor_id: str = getattr(request.state, "actor_id", "anonymous")
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.ARTIFACT_DELETE,
            resource_type="artifact",
            resource_id=name,
            result=AuditResult.SUCCESS,
            trace_id=current_trace_id_hex(),
            details={"user_id": str(meta.user_id), "via": "playground"},
        )
        return JSONResponse({"success": True, "data": {"deleted": name}})

    @router.get("")
    async def list_sessions(
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
        runtime: Annotated[AgentRuntime, Depends(_get_agent_runtime)],
        status: ThreadStatus | None = None,
        q: Annotated[str | None, Query(max_length=200)] = None,
        agent_name: Annotated[str | None, Query(max_length=256)] = None,
        include_archived: Annotated[bool, Query()] = False,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        tenant_id: Annotated[UUID | Literal["*"] | None, Query()] = None,  # Stream N
    ) -> JSONResponse:
        # Stream N — resolve ``?tenant_id=`` against the caller's scope.
        scope = await ensure_tenant_scope(
            request.state.principal,
            tenant_id,
            audit,
            trace_id=current_trace_id_hex(),
            endpoint="GET /v1/sessions",
            cross_tenant_enabled=cross_tenant_query_enabled(request),
        )
        async with applied_scope(scope):
            if isinstance(scope, CrossTenant):
                # Platform-admin view aggregates every user's sessions
                # across every tenant — per-user filter is intentionally
                # dropped (system_admin sees the whole picture).
                items = await threads.list_all_tenants(
                    status=status,
                    agent_name=agent_name,
                    nonempty=True,
                    q=q,
                    include_archived=include_archived,
                    limit=limit,
                    offset=offset,
                )
            else:
                # Every caller — plain user or admin — lists only their own
                # threads; the debug console has no cross-user history view.
                caller_user_id = await resolve_caller_user_id(request, users)
                user_filter = thread_list_filter(caller_user_id=caller_user_id)
                items = await threads.list_by_tenant(
                    scope.tenant_id,
                    status=status,
                    user_id=user_filter,
                    agent_name=agent_name,
                    nonempty=True,
                    q=q,
                    include_archived=include_archived,
                    limit=limit,
                    offset=offset,
                )
                # Lazy backfill — threads created before auto-titling have a
                # NULL title and show as a thread_id hash. Derive it from the
                # checkpoint's first user message and persist (one-time per
                # thread; only the listed page, so bounded). Best-effort: a
                # read failure just leaves the hash fallback.
                items = await _backfill_titles(
                    items, threads=threads, checkpointer=runtime.durable_checkpointer
                )
        audit_tenant = (
            request.state.principal.tenant_id if isinstance(scope, CrossTenant) else scope.tenant_id
        )
        await emit(
            audit,
            tenant_id=audit_tenant,
            actor_id=request.state.actor_id,
            action=AuditAction.SESSION_READ,
            resource_type="session",
            trace_id=current_trace_id_hex(),
            details={"count": len(items)},
        )
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "items": [m.model_dump(mode="json") for m in items],
                    "total": len(items),
                    "cross_tenant": isinstance(scope, CrossTenant),
                },
            }
        )

    async def _load_owned_session(
        thread_id: UUID,
        request: Request,
        threads: ThreadMetaStore,
        users: TenantUserStore,
    ) -> ThreadMeta:
        """Fetch the thread + enforce the tenant/ownership gate, or raise 404.

        Shared by rename / archive / purge. A plain user may only reach their
        own thread; an admin reaches the whole tenant. 404 (never 403) hides
        cross-user / cross-tenant existence.
        """
        tenant_id: UUID = request.state.tenant_id
        meta = await threads.get(thread_id, tenant_id=tenant_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")
        return meta

    @router.patch("/{thread_id}")
    async def rename_session(
        thread_id: UUID,
        payload: RenamePayload,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Rename a session — sets ``title`` (overrides the auto-title)."""
        await _load_owned_session(thread_id, request, threads, users)
        tenant_id: UUID = request.state.tenant_id
        title = payload.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="title must not be empty")
        updated = await threads.update_title(thread_id, title, tenant_id=tenant_id)
        if not updated:
            raise HTTPException(status_code=404, detail="session not found")
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=request.state.actor_id,
            action=AuditAction.SESSION_WRITE,
            resource_type="session",
            resource_id=str(thread_id),
            trace_id=current_trace_id_hex(),
            details={"op": "rename"},
        )
        fresh = await threads.get(thread_id, tenant_id=tenant_id)
        if fresh is None:
            raise HTTPException(status_code=404, detail="session not found")
        return JSONResponse({"success": True, "data": fresh.model_dump(mode="json")})

    @router.delete("/{thread_id}")
    async def archive_session(
        thread_id: UUID,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Soft-delete — archive the session (hidden from the default list,
        reversible). The checkpoint / runs / workspace are untouched; use
        ``:purge`` for an irreversible hard delete."""
        await _load_owned_session(thread_id, request, threads, users)
        tenant_id: UUID = request.state.tenant_id
        updated = await threads.update_status(thread_id, ThreadStatus.ARCHIVED, tenant_id=tenant_id)
        if not updated:
            raise HTTPException(status_code=404, detail="session not found")
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=request.state.actor_id,
            action=AuditAction.SESSION_WRITE,
            resource_type="session",
            resource_id=str(thread_id),
            trace_id=current_trace_id_hex(),
            details={"op": "archive"},
        )
        return JSONResponse({"success": True, "data": {"archived": str(thread_id)}})

    @router.post("/{thread_id}:purge")
    async def purge_session(
        thread_id: UUID,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        runtime: Annotated[AgentRuntime, Depends(_get_agent_runtime)],
        approvals: Annotated[ApprovalStore, Depends(_get_approval_store)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        """Hard-delete — irreversibly purge the whole conversation.

        Removes ONLY thread-scoped data (checkpoint messages, run rows +
        their run_event children, agent_approval rows, the thread_meta row).
        The user's persistent workspace + artifacts are keyed by ``user_id``
        and SHARED across that user's other threads, so they are
        intentionally left untouched. Best-effort: a failed step is logged
        AND flagged in the audit details (``*_delete_failed``), not fatal,
        and the thread_meta row is deleted LAST so a partial failure never
        orphans the metadata.
        """
        await _load_owned_session(thread_id, request, threads, users)
        tenant_id: UUID = request.state.tenant_id
        deleted: dict[str, object] = {"checkpoint": False, "runs": 0, "approvals": 0}

        checkpointer = runtime.durable_checkpointer
        adelete = getattr(checkpointer, "adelete_thread", None)
        if adelete is not None:
            try:
                await adelete(str(thread_id))
                deleted["checkpoint"] = True
            except Exception:
                logger.warning("session_purge.checkpoint_failed", exc_info=True)
        try:
            deleted["runs"] = await runtime.run_manager.delete_by_thread(
                thread_id, tenant_id=tenant_id
            )
        except Exception:
            logger.warning("session_purge.runs_failed", exc_info=True)
            deleted["runs_delete_failed"] = True
        try:
            deleted["approvals"] = await approvals.delete_for_threads(
                thread_ids=[thread_id], tenant_id=tenant_id
            )
        except Exception:
            logger.warning("session_purge.approvals_failed", exc_info=True)
            deleted["approvals_delete_failed"] = True

        removed = await threads.delete(thread_id, tenant_id=tenant_id)
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=request.state.actor_id,
            action=AuditAction.SESSION_WRITE,
            resource_type="session",
            resource_id=str(thread_id),
            trace_id=current_trace_id_hex(),
            details={"op": "purge", "meta_removed": removed, **deleted},
        )
        return JSONResponse({"success": True, "data": {"purged": str(thread_id), **deleted}})

    async def _transition(
        *,
        thread_id: UUID,
        request: Request,
        threads: ThreadMetaStore,
        users: TenantUserStore,
        audit: AuditLogger,
        target: ThreadStatus,
        allowed_from: frozenset[ThreadStatus],
        audit_action: AuditAction,
        reason: str | None,
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        trace_id = current_trace_id_hex()

        meta = await threads.get(thread_id, tenant_id=tenant_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="session not found")
        # Stream J.14 — only the owning user (or an admin) may transition.
        caller_user_id = await resolve_caller_user_id(request, users)
        if not caller_owns_thread(
            meta=meta, caller_user_id=caller_user_id, principal=request.state.principal
        ):
            raise HTTPException(status_code=404, detail="session not found")

        if meta.status not in allowed_from:
            await emit(
                audit,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=audit_action,
                resource_type="session",
                resource_id=str(thread_id),
                result=AuditResult.ERROR,
                reason=f"illegal_transition_from_{meta.status.value}",
                trace_id=trace_id,
            )
            return _conflict(f"cannot transition from {meta.status.value} to {target.value}")

        updated = await threads.update_status(thread_id, target, tenant_id=tenant_id)
        if not updated:
            # The row vanished between get + update — treat as 404 for tenant safety.
            raise HTTPException(status_code=404, detail="session not found")

        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=audit_action,
            resource_type="session",
            resource_id=str(thread_id),
            trace_id=trace_id,
            details={"to": target.value, "reason": reason} if reason else {"to": target.value},
        )
        # Re-fetch so the response reflects the row after the update.
        fresh = await threads.get(thread_id, tenant_id=tenant_id)
        if fresh is None:
            raise HTTPException(status_code=404, detail="session not found")
        return JSONResponse({"success": True, "data": fresh.model_dump(mode="json")})

    @router.post("/{thread_id}:pause")
    async def pause_session(
        thread_id: UUID,
        payload: TransitionPayload,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        return await _transition(
            thread_id=thread_id,
            request=request,
            threads=threads,
            users=users,
            audit=audit,
            target=ThreadStatus.PAUSED,
            allowed_from=frozenset({ThreadStatus.ACTIVE}),
            audit_action=AuditAction.SESSION_WRITE,
            reason=payload.reason,
        )

    @router.post("/{thread_id}:resume")
    async def resume_session(
        thread_id: UUID,
        payload: TransitionPayload,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        return await _transition(
            thread_id=thread_id,
            request=request,
            threads=threads,
            users=users,
            audit=audit,
            target=ThreadStatus.ACTIVE,
            allowed_from=frozenset({ThreadStatus.PAUSED}),
            audit_action=AuditAction.SESSION_WRITE,
            reason=payload.reason,
        )

    @router.post("/{thread_id}:cancel")
    async def cancel_session(
        thread_id: UUID,
        payload: TransitionPayload,
        request: Request,
        threads: Annotated[ThreadMetaStore, Depends(_get_thread_repo)],
        users: Annotated[TenantUserStore, Depends(get_user_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        return await _transition(
            thread_id=thread_id,
            request=request,
            threads=threads,
            users=users,
            audit=audit,
            target=ThreadStatus.CANCELLED,
            allowed_from=frozenset({ThreadStatus.ACTIVE, ThreadStatus.PAUSED}),
            audit_action=AuditAction.SESSION_CANCEL,
            reason=payload.reason,
        )

    return router
