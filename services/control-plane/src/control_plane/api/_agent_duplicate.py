"""B-278 —— 新建 / 从模板复制撞上同名同版本时的 409。

撞上的是一个**已删除**的版本时,单独给 ``MANIFEST_DUPLICATE_DELETED``:控制台据此提示
「可以恢复,或者彻底删除后再新建」,而不是一句让人无从下手的「已存在」。名字正在彻底
删除时(任一版本 ``purging``)给 ``AGENT_PURGING``,不论版本号撞没撞上。
"""

from __future__ import annotations

from uuid import UUID

from fastapi.responses import JSONResponse

from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.protocol import AgentSpecStatus

MANIFEST_DUPLICATE = "MANIFEST_DUPLICATE"
MANIFEST_DUPLICATE_DELETED = "MANIFEST_DUPLICATE_DELETED"
#: B-278 —— 这个名字正在彻底删除(有版本处于 ``purging``):不能新建、不能恢复。
AGENT_PURGING = "AGENT_PURGING"


async def duplicate_agent_response(
    repo: AgentSpecStore, *, tenant_id: UUID, name: str, version: str
) -> JSONResponse:
    versions = await repo.list_by_tenant(tenant_id=tenant_id, name=name, limit=1000)
    if any(r.status is AgentSpecStatus.PURGING for r in versions):
        # ``AgentSpecPurgingError`` (a ``DuplicateAgentSpecError``) lands here too.
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": AGENT_PURGING,
                    "message": "this agent name is being purged; retry once the purge finishes",
                    "details": {"name": name, "version": version, "status": "purging"},
                },
            },
        )
    existing = next((r for r in versions if r.version == version), None)
    if existing is not None and existing.status is AgentSpecStatus.DELETED:
        return JSONResponse(
            status_code=409,
            content={
                "success": False,
                "data": None,
                "error": {
                    "code": MANIFEST_DUPLICATE_DELETED,
                    "message": (
                        "a deleted agent with this name and version exists; "
                        "restore it, or purge it before creating a new one"
                    ),
                    "details": {"name": name, "version": version, "status": "deleted"},
                },
            },
        )
    return JSONResponse(
        status_code=409,
        content={
            "success": False,
            "data": None,
            "error": {
                "code": MANIFEST_DUPLICATE,
                "message": "an agent with this name and version already exists",
            },
        },
    )
