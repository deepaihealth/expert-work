"""B-278 —— 已删除智能体的「恢复」与「彻底删除」。

三个端点都只给控制台、都按删除权限(``manifest:delete``,租户管理员)判:

* ``POST /v1/agents/{name}/{version}/restore`` —— 把这一个已删除的版本恢复成可用;对话、
  记忆原样都在。删除时被一起停掉的定时任务**不会**自动打开,响应里给出还停着几个。
* ``GET /v1/agents/{name}/{version}/purge-preview`` —— 彻底删除前,各类数据各有几条、
  什么在拦着(还有没删除的版本 / 被别的智能体当子智能体引用)。
* ``POST /v1/agents/{name}/{version}/purge`` —— 彻底删除整个名字(全部版本 + 数据),
  请求体 ``{"confirm_name": "<名字>"}`` 必须与名字完全一致。

后两个路径里的 ``version`` 只用来定位这一行做权限判断,操作对象是整个名字。路由放在单独
的模块里,``agents.py`` 只改了读已删除版本与 409 两处。
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from control_plane.api._authz import console_only, ensure_resource_access, manifest_record_attrs
from control_plane.api._external import reject_nul_path_params
from control_plane.api.agents import _invalidate_agent_build_cache
from control_plane.audit import emit
from control_plane.invalidation_bus import InvalidationEvent
from control_plane.purge.agent_purge import (
    AgentPurgeDeps,
    find_blockers,
    preview_agent_purge,
    purge_agent,
)
from control_plane.tenancy.tenant_config import ServiceBackedTenantConfigStore
from expert_work.common.observability import current_trace_id_hex
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.protocol import AgentSpecRecord, AgentSpecStatus, AuditAction, AuditResult
from expert_work.runtime.audit.logger import AuditLogger
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore

logger = logging.getLogger("expert_work.control_plane.api.agent_lifecycle")

AGENT_NOT_DELETED = "AGENT_NOT_DELETED"
AGENT_PURGE_BLOCKED = "AGENT_PURGE_BLOCKED"
CONFIRM_NAME_MISMATCH = "CONFIRM_NAME_MISMATCH"

_CONSOLE_ONLY = [Depends(console_only())]


class AgentPurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 必须与路径里的名字完全一致(大小写、空格都算)—— 防手滑。
    confirm_name: str = Field(min_length=1, max_length=256)


def _get_repo(request: Request) -> AgentSpecStore:
    return request.app.state.agent_spec_repo  # type: ignore[no-any-return]


def _get_audit(request: Request) -> AuditLogger:
    return request.app.state.audit_logger  # type: ignore[no-any-return]


def build_agent_purge_deps(request: Request) -> AgentPurgeDeps:
    """从 ``app.state`` 装配(唯一的生产装配点,测试钉住每个 store 都接上了)。"""
    state = request.app.state
    workspace_store = getattr(state, "workspace_store", None)
    return AgentPurgeDeps(
        agent_specs=state.agent_spec_repo,
        agent_disable=state.agent_disable_repo,
        threads=state.thread_meta_repo,
        runtime=state.agent_runtime,
        runs=state.run_store,
        approvals=state.approval_store,
        feedback=state.feedback_store,
        retrieval_logs=state.knowledge_retrieval_log_store,
        memory=state.memory_repo,
        memory_dlq=state.memory_writeback_dlq,
        memory_writeback_jobs=state.memory_writeback_job_store,
        artifacts=state.artifact_store,
        agent_instances=state.agent_instance_store,
        triggers=state.trigger_store,
        trigger_runs=state.trigger_run_store,
        webhook_endpoints=state.webhook_endpoint_store,
        webhook_deliveries=state.webhook_delivery_store,
        image_uploads=state.image_upload_store,
        user_uploads=state.user_upload_store,
        quality_scores=state.quality_score_store,
        drift_alerts=state.quality_drift_alert_store,
        eval_datasets=state.eval_dataset_store,
        curation_candidates=state.curation_candidate_store,
        secret_allowlist=state.secret_allowlist_store,
        # 经 service 走:清掉默认智能体后本副本的配置缓存立刻失效。
        tenant_config=ServiceBackedTenantConfigStore(service=state.tenant_config_service),
        object_store=getattr(state, "object_store", None),
        workspace_store=workspace_store,
        workspace_root=(
            workspace_store.root if isinstance(workspace_store, NasWorkspaceStore) else None
        ),
        artifact_snapshots=getattr(state, "artifact_snapshots", None),
        agent_file_versions=getattr(state, "agent_file_versions", None),
    )


def _error(code: str, message: str, status_code: int, details: object = None) -> JSONResponse:
    error: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse(
        status_code=status_code, content={"success": False, "data": None, "error": error}
    )


async def _load_for_delete(
    request: Request, repo: AgentSpecStore, *, name: str, version: str
) -> AgentSpecRecord:
    """这一行(含已删除的)+ 删除权限。租户只取自登录态,没有跨租户参数。"""
    record = await repo.get(
        tenant_id=request.state.tenant_id, name=name, version=version, include_deleted=True
    )
    if record is None:
        raise HTTPException(status_code=404, detail="agent not found")
    await ensure_resource_access(
        request, resource="manifest", action="delete", attrs=manifest_record_attrs(record)
    )
    return record


def build_agent_lifecycle_router() -> APIRouter:
    router = APIRouter(
        prefix="/v1/agents",
        tags=["agents"],
        dependencies=[Depends(reject_nul_path_params)],
    )

    @router.post("/{name}/{version}/restore", dependencies=_CONSOLE_ONLY)
    async def restore_agent(
        name: str,
        version: str,
        request: Request,
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        record = await _load_for_delete(request, repo, name=name, version=version)
        restored = None
        if record.status is AgentSpecStatus.DELETED:
            restored = await repo.restore(tenant_id=tenant_id, name=name, version=version)
        if restored is None:
            # 没删除的版本「恢复」是调用方搞错了状态,不是无害的空操作:明确 409。
            return _error(AGENT_NOT_DELETED, "this agent version is not deleted", 409)
        await _invalidate_agent_build_cache(request, tenant_id)
        triggers = await request.app.state.trigger_store.list_by_tenant(
            tenant_id=tenant_id, agent_name=name, agent_version=version
        )
        # 删除时停掉的定时任务不自动打开(用户拍板):告诉管理员还停着几个,让他自己决定。
        still_disabled = sum(1 for t in triggers if not t.enabled)
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=request.state.actor_id,
            action=AuditAction.MANIFEST_RESTORE,
            resource_type="manifest",
            resource_id=f"{name}/{version}",
            trace_id=current_trace_id_hex(),
            details={"triggers_disabled": still_disabled},
        )
        return JSONResponse(
            {
                "success": True,
                "data": {
                    "name": name,
                    "version": version,
                    "status": restored.status.value,
                    "triggers_disabled": still_disabled,
                },
                "error": None,
            }
        )

    @router.get("/{name}/{version}/purge-preview", dependencies=_CONSOLE_ONLY)
    async def preview_purge(
        name: str,
        version: str,
        request: Request,
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
    ) -> JSONResponse:
        await _load_for_delete(request, repo, name=name, version=version)
        preview = await preview_agent_purge(
            build_agent_purge_deps(request), tenant_id=request.state.tenant_id, name=name
        )
        return JSONResponse({"success": True, "data": preview, "error": None})

    @router.post("/{name}/{version}/purge", dependencies=_CONSOLE_ONLY)
    async def purge(
        name: str,
        version: str,
        payload: AgentPurgeRequest,
        request: Request,
        repo: Annotated[AgentSpecStore, Depends(_get_repo)],
        audit: Annotated[AuditLogger, Depends(_get_audit)],
    ) -> JSONResponse:
        tenant_id: UUID = request.state.tenant_id
        actor_id: str = request.state.actor_id
        trace_id = current_trace_id_hex()
        await _load_for_delete(request, repo, name=name, version=version)
        if payload.confirm_name != name:
            return _error(CONFIRM_NAME_MISMATCH, "confirm_name does not match the agent name", 422)
        blockers = await find_blockers(repo, tenant_id=tenant_id, name=name)
        if blockers.blocked:
            details: dict[str, object] = {
                "active_versions": blockers.active_versions,
                "referenced_by": blockers.referenced_by,
            }
            await emit(
                audit,
                tenant_id=tenant_id,
                actor_id=actor_id,
                action=AuditAction.MANIFEST_PURGE,
                resource_type="manifest",
                resource_id=name,
                result=AuditResult.DENIED,
                reason="blocked",
                trace_id=trace_id,
                details=details,
            )
            return _error(
                AGENT_PURGE_BLOCKED,
                "every version must be deleted and no other agent may use it as a subagent",
                409,
                details,
            )

        summary = await purge_agent(build_agent_purge_deps(request), tenant_id=tenant_id, name=name)

        # 构建缓存与停用开关缓存:本副本立刻失效,其它副本走失效总线。
        await _invalidate_agent_build_cache(request, tenant_id)
        disable_service = getattr(request.app.state, "agent_disable_service", None)
        if disable_service is not None:
            disable_service.invalidate(tenant_id, name)
        bus = getattr(request.app.state, "invalidation_bus", None)
        if bus is not None:
            await bus.publish(InvalidationEvent(kind="agent_disable", tenant_id=str(tenant_id)))
        await emit(
            audit,
            tenant_id=tenant_id,
            actor_id=actor_id,
            action=AuditAction.MANIFEST_PURGE,
            resource_type="manifest",
            resource_id=name,
            result=AuditResult.SUCCESS if summary.ok else AuditResult.ERROR,
            reason=None if summary.ok else "one or more purge steps failed",
            trace_id=trace_id,
            details=summary.as_dict(),
        )
        return JSONResponse({"success": True, "data": summary.as_dict(), "error": None})

    return router
