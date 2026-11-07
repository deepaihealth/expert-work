"""B-295 —— 有人读了别人对话里的文件内容(附件 / 产物)时留一条 ``user:data_view`` 审计。

对话里的附件和产物跟对话同一个可见范围(有 ``session:read`` 的员工都能看,2026-11-07 用户
拍板),内容常是健康资料,所以「谁看了谁的」要留痕。沿用既有的读审计词汇:
:attr:`AuditAction.USER_DATA_VIEW`(``resource_type="user"``,``resource_id`` = 被看的人),
与工作区 / 记忆的跨用户读取同一个动作,只在读的人不是对话主人时写。

``details`` 只放 ``view`` 和 ``thread_id``:不放文件名、产物名或任何内容 —— 名字本身就可能是
健康信息。只在字节真的读到之后调用(读失败不算看过)。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Request

from control_plane.audit import emit
from expert_work.common.observability import current_trace_id_hex
from expert_work.protocol import AuditAction, ThreadMeta
from expert_work.runtime.audit.logger import AuditLogger


async def audit_conversation_data_view(
    request: Request,
    audit: AuditLogger,
    *,
    tenant_id: UUID,
    meta: ThreadMeta,
    caller_user_id: UUID | None,
    view: str,
) -> None:
    """Write the row when the caller is not ``meta``'s owner; a no-op otherwise.

    ``tenant_id`` is the conversation's tenant (a system admin's cross-tenant read is
    recorded in that tenant, next to the scope audit :func:`ensure_single_tenant_scope`
    already writes). An unowned thread (``user_id is None``) has nobody to be viewed.
    """
    if meta.user_id is None or caller_user_id == meta.user_id:
        return
    await emit(
        audit,
        tenant_id=tenant_id,
        actor_id=getattr(request.state, "actor_id", "anonymous"),
        action=AuditAction.USER_DATA_VIEW,
        resource_type="user",
        resource_id=str(meta.user_id),
        trace_id=current_trace_id_hex(),
        details={"view": view, "thread_id": str(meta.thread_id)},
    )
