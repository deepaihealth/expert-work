"""Shared ``workspace_store.list_files`` handling — console + external planes.

``api/workspace.py`` (console, user-scoped) and ``api/external_workspace.py``
(third-party, P2-b Task 1) both list a user's persistent workspace after
already resolving ``(tenant_id, user_id)`` by their own identity rules —
those rules differ (console: ``resolve_target_user_id``; external:
``lookup_external_user_id``, ``mint=False``), but everything *after* that
point is the same security-relevant plumbing, and it must not be
implemented twice: a :class:`WorkspacePermissionError` (server-side
misconfig — shared uid not set up, a legacy directory whose owner never
migrated, wrong mode) must surface as a 500, never degrade to an empty
list — a user seeing "workspace is empty" instead of "something is
broken" pushes the entire diagnosis cost onto server logs. A generic
:class:`SandboxSupervisorError` (supervisor unreachable, etc.) DOES degrade
to an empty list — losing that distinction in a second, hand-copied
implementation is exactly the kind of drift that motivated pulling this
out: change the handling on one side, forget the other, and the forgotten
side fails silently (no test breaks, nothing errors — it just quietly
does the wrong thing).
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import HTTPException

from orchestrator.tools import SandboxSupervisorError, WorkspacePermissionError, WorkspaceStore

logger = logging.getLogger("expert_work.control_plane.workspace")


async def _workspace_files_payload(
    workspace_store: WorkspaceStore | None, *, tenant_id: UUID, user_id: UUID
) -> dict[str, list[dict[str, object]]]:
    """The ``{"files": [...]}`` payload for one already-resolved ``(tenant_id, user_id)``.

    No store wired (``workspace_store is None`` — no supervisor configured)
    degrades to an empty list, same as a generic :class:`SandboxSupervisorError`.
    A :class:`WorkspacePermissionError` (its subclass) is the one case that must
    NOT degrade — see the module docstring.
    """
    if workspace_store is None:
        return {"files": []}
    try:
        entries = await workspace_store.list_files(tenant_id=tenant_id, user_id=user_id)
    except WorkspacePermissionError as exc:
        # 权限失败(共享 uid 没配上/存量目录属主没迁移/mode 不对)是服务端配置
        # 问题,不是"这个用户没有文件"。这里如果和下面的 SandboxSupervisorError
        # 一样吞成空列表,用户会看到"工作区是空的"——比报错更坏,连"出错了"
        # 都看不到,诊断成本全压到服务端日志上。detail 只给固定文案,路径/uid/
        # mode 只进下面这条结构化日志。
        #
        # ⚠️ except 顺序不能反:WorkspacePermissionError 是 SandboxSupervisorError
        # 的子类,反了这一支永远走不到。
        logger.warning("workspace.list_permission_denied", exc_info=True)
        raise HTTPException(status_code=500, detail="workspace listing unavailable") from exc
    except SandboxSupervisorError:
        logger.warning("workspace.list_failed", exc_info=True)
        return {"files": []}
    return {"files": [{"path": e.path, "size": e.size} for e in entries]}
