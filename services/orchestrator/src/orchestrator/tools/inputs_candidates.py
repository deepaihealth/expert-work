"""B-317 复审 L1 —— 输入文件路径打不开时,列出别的轮次里同名的文件。

之后的轮次要接着用前一轮的输入文件,得写 ``inputs/<那一轮的 run>/…``:36 位的 run id 要模型
手抄(长串抄错一位就打不开),而回执末尾那句「之后的轮次请用 …」可能已被上下文裁剪删掉。所以在
宿主侧补:路径参数写的是 ``inputs/current/<rest>`` 或 ``inputs/<run>/<rest>`` 且这个文件不在
时,看工作区里还有哪些 ``inputs/*/<rest>``,新的在前、最多 :data:`MAX_CANDIDATES` 个,报错里
原样列出,模型挑一个用,不必再抄。

列出来的只有平台目录名、run id 与模型自己写的 ``<rest>``(且那个文件确实存在),不含租户数据。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from expert_work.persistence import WORKSPACE_INPUTS_DIR
from orchestrator.tools.nas_workspace_store import MISSING_DIR_ERRORS
from orchestrator.tools.registry import ToolContext
from orchestrator.tools.sandbox import SandboxSupervisorError
from orchestrator.tools.sandbox_image_contract import EXEC_VIEW
from orchestrator.tools.workspace_paths import WorkspaceView, resolve_workspace_path
from orchestrator.tools.workspace_scope import store_scope
from orchestrator.tools.workspace_store import WorkspaceStore

#: 报错里最多列几个。
MAX_CANDIDATES = 5
#: 最多看几个 run 目录(按修改时间新的在前);每个要列一次目录,这里给出上界。
MAX_RUN_DIRS_SCANNED = 50
#: 本轮没有输入时,``inputs/current/`` 先改写到这个不存在的 run 上,再走同一份解析规则。
_NO_RUN = UUID(int=0)
_OLDEST = datetime.min.replace(tzinfo=UTC)


def _is_canonical_uuid(text: str) -> bool:
    try:
        return str(UUID(text)) == text
    except ValueError:
        return False


def input_file_rest(raw: str, view: WorkspaceView) -> tuple[str, str] | None:
    """``raw`` 指向某一轮输入目录下的文件时 → ``(视图相对的真实路径, <rest>)``;否则 ``None``。

    解析走 ``resolve_workspace_path``(别名、``/workspace/`` 折叠、对话视图都同一个口径);本轮
    没有输入时 ``inputs/current/`` 改写到一个不存在的 run 上。只认 ``/workspace`` 视图下的。
    """
    probe = view if view.inputs_run is not None else replace(view, inputs_run=_NO_RUN)
    try:
        target = resolve_workspace_path(raw, view=probe, tool="inputs")
    except ValueError:
        return None
    if target.ws != EXEC_VIEW:
        return None
    parts = target.rel.split("/")
    if len(parts) < 3 or parts[0] != WORKSPACE_INPUTS_DIR or not _is_canonical_uuid(parts[1]):
        return None
    return target.rel, "/".join(parts[2:])


async def _run_dirs(store: WorkspaceStore, binding: tuple[UUID, UUID], scope: str) -> list[str]:
    """``inputs/`` 下的 run 目录名,按修改时间新的在前,最多 :data:`MAX_RUN_DIRS_SCANNED` 个。

    **截断(第四轮 LOW-3,已知不修):** ``list_dir`` 自己有条目上限(NAS 实现 2000),超出时按
    **名字**排序截断,之后才在这里按时间排。run 目录多过那个上限时,被截掉的可能恰好是最新的
    几个,候选会漏。这只是报错后面的一句提示,漏了模型照旧可以 ``list_dir inputs/``;不为它多
    走一遍全量列目录。
    """
    tenant_id, user_id = binding
    try:
        listing = await store.list_dir(
            tenant_id=tenant_id, user_id=user_id, scope=scope, path=WORKSPACE_INPUTS_DIR
        )
    except MISSING_DIR_ERRORS:
        return []
    runs = [e for e in listing.entries if e.is_dir and _is_canonical_uuid(e.name)]
    runs.sort(key=lambda e: e.mtime or _OLDEST, reverse=True)
    return [e.name for e in runs[:MAX_RUN_DIRS_SCANNED]]


async def input_candidates(store: WorkspaceStore, ctx: ToolContext, raw: str) -> tuple[str, ...]:
    """``raw`` 指向的输入文件不在时,别的轮次里同名的文件(视图相对、新的在前、有上界)。

    文件在、不是输入目录下的路径、没绑工作区、存储没有 ``path_exists``(只做 ``lstat`` 的存在性
    检查,不列目录),都是空。某一个 run 查不了(中间一段是符号链接、权限……)只跳过那一个。
    开销:一次列 ``inputs/``,加至多 ``1 + MAX_RUN_DIRS_SCANNED`` 次逐段打开 + ``lstat``;调用方
    另有总时限(``builder._CANDIDATES_TIMEOUT_S``)。
    """
    view = ctx.workspace_view
    parsed = input_file_rest(raw, view)
    exists = getattr(store, "path_exists", None)
    if parsed is None or exists is None or ctx.tenant_id is None or ctx.user_id is None:
        return ()
    requested, rest = parsed
    binding = (ctx.tenant_id, ctx.user_id)
    scope = store_scope(
        EXEC_VIEW,
        agent_key=view.agent_key,
        conv_id=view.conv_id,
        thread_id=None if view.child_run else view.own_thread,
        child_run=view.child_run,
    )

    async def _has(rel: str) -> bool:
        return bool(
            await exists(tenant_id=ctx.tenant_id, user_id=ctx.user_id, scope=scope, path=rel)
        )

    if await _has(requested):
        return ()
    found: list[str] = []
    for run in await _run_dirs(store, binding, scope):
        path = f"{WORKSPACE_INPUTS_DIR}/{run}/{rest}"
        try:
            hit = await _has(path)
        except (OSError, SandboxSupervisorError, ValueError):
            continue
        if hit:
            found.append(path)
            if len(found) == MAX_CANDIDATES:
                break
    return tuple(found)


def candidates_note(paths: tuple[str, ...]) -> str:
    """接在报错后面的那一句;``paths`` 为空时是空串。"""
    if not paths:
        return ""
    return "其他轮次的输入目录里有同名文件（新的在前）：" + "、".join(paths)  # noqa: RUF001
