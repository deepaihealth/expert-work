"""Shared ``workspace_store`` handling — console + external planes.

``api/workspace.py`` (console, user-scoped) and ``api/external_workspace.py``
(third-party, P2-b Task 1/2) both browse and download a user's persistent
workspace after already resolving ``(tenant_id, user_id)`` by their own
identity rules — those rules differ (console: ``resolve_target_user_id``;
external: ``lookup_external_user_id``, ``mint=False``), but everything
*after* that point is the same security-relevant plumbing, and it must not
be implemented twice: a :class:`WorkspacePermissionError` (server-side
misconfig — shared uid not set up, a legacy directory whose owner never
migrated, wrong mode) must surface as a 500, never degrade to an empty
list / opaque 404 — a user seeing "workspace is empty" / "file not found"
instead of "something is broken" pushes the entire diagnosis cost onto
server logs. A generic :class:`SandboxSupervisorError` (supervisor
unreachable, missing file, etc.) DOES degrade (empty list / 404) — losing
that distinction in a second, hand-copied implementation is exactly the
kind of drift that motivated pulling this out: change the handling on one
side, forget the other, and the forgotten side fails silently (no test
breaks, nothing errors — it just quietly does the wrong thing).

This module intentionally has no notion of "console" vs "external" — no
``if is_external:`` branch anywhere below. Each caller resolves its own
identity first and passes in a plain ``(tenant_id, user_id)`` (``user_id``
may be ``None`` — "no such target", e.g. a machine principal on the
console side, or an unrecognized third-party ``user_id`` on the external
side); from that point on the two planes are, by construction, running
the same code.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from uuid import UUID

from fastapi import HTTPException
from fastapi.responses import Response

from control_plane.api._artifact_mime import content_disposition_header, infer_content_type
from expert_work.protocol import ThreadMeta
from expert_work.protocol.agent_key import sanitize_agent_key
from orchestrator.tools import (
    SandboxSupervisorError,
    WorkspaceFileEntry,
    WorkspaceFileTooLargeError,
    WorkspacePermissionError,
    WorkspaceStore,
)
from orchestrator.tools.workspace_paths import AGENTS_DIR

logger = logging.getLogger("expert_work.control_plane.workspace")

#: 路径校验失败的固定文案。导出成常量是因为对外平面在**加 agent 前缀之前**
#: 自己跑一次校验(``_external_agent_scope.external_storage_path``),两处必须
#: 报同一句话 —— 不同的文案会让第三方能分辨「路径本身不合法」和「投影之后
#: 不合法」,而那两件事对他们应当是同一个不透明的 400。
INVALID_WORKSPACE_PATH = "invalid workspace path"


def workspace_agent_path(rel: str, *, agent_key: str) -> str:
    """把一个 agent 相对路径投影成用户根下的存储路径(B-50)。

    ``agent_key`` 为空 = 这个会话没绑 agent(建表早于该列 / 机器线程),
    保持搬迁前的扁平位置 —— 与 ``agent_workspace_root("")`` 同一个口径。

    **控制平面直接调 ``workspace_store`` 的地方都要过这一道。** 沙箱里的
    文件工具自己按 agent 根解析(PR3),但 ``workspace_store`` 是按
    ``(tenant, user)`` 开的,它不知道 agent —— 上传写入、附件下载、会话内
    的工作区读写都走它。漏掉一处的后果是不对称的:写侧漏了,新上传落回
    扁平根,搬迁完第二天又堆起来;读侧漏了,搬迁后立刻 404。

    这两种漏法今天都被 PR3 的迁移期读回落兜着(找不到就回落用户根一次),
    所以**现在不会报错** —— 而 Task 14 摘掉回落的那一刻一起爆。
    """
    if not agent_key:
        return rel
    return f"{AGENTS_DIR}/{agent_key}/{rel}"


def thread_agent_key(meta: ThreadMeta) -> str:
    """这个会话的工作区子树名;``""`` = 没绑 agent,用扁平根。

    与 ``sessions._session_agent_key`` 刻意不同名也不同返回类型:那个返回
    ``None`` 表示「不要按 agent 过滤产物」,是查询谓词;这个返回空串表示
    「就用用户根」,是路径前缀。两者取值同源但用途相反,合并成一个函数会让
    「不过滤」和「根目录」这两件事变得无法区分。
    """
    return sanitize_agent_key(meta.agent_name) if meta.agent_name else ""


def _safe_workspace_relpath(path: str) -> str | None:
    """Return the cleaned relative path, or ``None`` if it escapes the workspace.

    The ``path`` query param round-trips through the client untrusted, so the
    download / delete endpoints re-check it here (the supervisor re-validates
    at its own boundary — defence in depth). Rejects absolute paths, any
    ``..`` segment that would climb out of ``/workspace``, and an embedded
    NUL byte (``\\x00`` is a C-string terminator in POSIX path APIs — a
    real filesystem-backed store could silently truncate
    ``"report\\x00.txt"`` to ``"report"``, reading/deleting a different file
    in the *same* workspace than the caller named; External-API-v1 P2-b
    Task 2 review). This is now the single shared implementation — used by
    the console workspace endpoints in this package, the external plane's
    ``GET /v1/agents/{agent_code}/workspace/file``, and the thread-scoped
    routes in :mod:`control_plane.api.sessions`.
    """
    cleaned = path.strip()
    if (
        not cleaned
        or cleaned.startswith("/")
        or "\x00" in cleaned
        or ".." in PurePosixPath(cleaned).parts
    ):
        return None
    return cleaned


async def list_workspace_entries(
    workspace_store: WorkspaceStore | None, *, tenant_id: UUID, user_id: UUID
) -> list[WorkspaceFileEntry]:
    """The raw entries for one already-resolved ``(tenant_id, user_id)``.

    No store wired (``workspace_store is None`` — no supervisor configured)
    degrades to an empty list, same as a generic :class:`SandboxSupervisorError`.
    A :class:`WorkspacePermissionError` (its subclass) is the one case that must
    NOT degrade — see the module docstring.

    Split out from :func:`_workspace_files_payload` for B-50 PR4: the external
    plane projects each entry through its own agent scoping
    (``_external_agent_scope``) and therefore needs the entries, not the
    finished payload — but it must keep running the **same** error handling,
    which is the whole reason this module exists. Console callers keep using
    the payload wrapper below.
    """
    if workspace_store is None:
        return []
    try:
        return await workspace_store.list_files(tenant_id=tenant_id, user_id=user_id)
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
        return []


async def _workspace_files_payload(
    workspace_store: WorkspaceStore | None, *, tenant_id: UUID, user_id: UUID
) -> dict[str, list[dict[str, object]]]:
    """The unscoped ``{"files": [...]}`` payload — the console plane's view.

    Lists the whole user root, agent subtrees included. The external plane
    does **not** use this (see :func:`list_workspace_entries`): a third party
    asking by ``agent_code`` must never be handed another agent's paths.
    """
    entries = await list_workspace_entries(workspace_store, tenant_id=tenant_id, user_id=user_id)
    return {"files": [{"path": e.path, "size": e.size} for e in entries]}


async def _workspace_file_response(
    workspace_store: WorkspaceStore | None,
    *,
    tenant_id: UUID,
    user_id: UUID | None,
    path: str,
) -> Response:
    """Download one file from an already-resolved ``(tenant_id, user_id)``.

    ``user_id is None`` — no such target (console: machine principal;
    external: an unrecognized ``user_id``, ``mint=False``) — degrades to
    the same opaque 404 as a missing file / absent supervisor. See the
    module docstring: cross-user, missing-file, and no-supervisor must be
    indistinguishable, or a caller can fingerprint which case they hit.

    ``path`` is untrusted input — validated here via
    :func:`_safe_workspace_relpath` (400 on escape) *before* the
    ``user_id``/store check, matching the console endpoint's original
    order: a malformed path is rejected the same way regardless of whether
    the target user exists.
    """
    safe_path = _safe_workspace_relpath(path)
    if safe_path is None:
        raise HTTPException(status_code=400, detail=INVALID_WORKSPACE_PATH)
    if user_id is None or workspace_store is None:
        raise HTTPException(status_code=404, detail="file not found")
    try:
        data = await workspace_store.read_file(tenant_id=tenant_id, user_id=user_id, path=safe_path)
    except WorkspacePermissionError as exc:
        # 权限失败是服务端配置问题,不是"这个文件不存在"——404 的语义是
        # "不存在 / 你不该知道它存在";塞进 404 会让用户看到一份列在上一屏
        # 却"文件不存在"的报错。必须排在下面的 SandboxSupervisorError 之
        # 前——它是那个类的子类,顺序反了这一分支永远走不到。
        logger.warning("workspace.read_permission_denied", exc_info=True)
        raise HTTPException(status_code=500, detail="workspace file unavailable") from exc
    except WorkspaceFileTooLargeError as exc:
        # 「太大」≠「不存在」—— 文件列在浏览列表里,只是超过单文件下载闸;
        # 折进 404 会让用户以为文件丢了。子类,必须排在宽 except 之前。
        logger.warning("workspace.file_too_large reason=%s", exc)
        raise HTTPException(status_code=413, detail="file exceeds the download size limit") from exc
    except SandboxSupervisorError as exc:
        logger.warning("workspace.read_failed", exc_info=True)
        raise HTTPException(status_code=404, detail="file not found") from exc
    filename = PurePosixPath(safe_path).name or "download"
    inferred = infer_content_type(path=safe_path)
    headers = {
        "Content-Disposition": content_disposition_header(
            filename, disposition=inferred.disposition
        ),
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=data, media_type=inferred.content_type, headers=headers)
