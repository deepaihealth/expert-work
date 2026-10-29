"""B-236 过渡期映射 —— 老 agent 根下已有的目录在对话视图里照旧按相对路径可用。

B-236 之前每个 agent 只有一个 ``/workspace``(agent 根),存量 agent 的提示词与脚本
按相对路径读写那里的长期目录(风格锚、模板……)。切到「每个对话一个 ``/workspace``」
之后这些相对路径会落进对话自己的空目录。过渡期内,agent 根下**已有的一级目录**在
对话视图里也能按原来的相对路径用:宿主侧解析器把它们解析到 ``/agent``,exec 脚本把
它们读写 bind 进 ``/workspace``。两边用的是本模块算出来的**同一份**名单。

* **只映射目录,根下的单个文件从不映射** —— 它们正是并发对话互相覆盖的那类固定文件名
  中间产物。
* **本对话自己已有同名条目就不映射**,除非那只是上一次 exec 留下的空挂载桩(exec 脚本
  的判据与这里相同:不存在,或是一个空的真目录)。
* 窗口由运维设置 ``EXPERT_WORK_WORKSPACE_COMPAT_ROOT_DIRS_UNTIL``(ISO 日期,含当天)
  决定;没设、设错、已过期 = 不映射。不是对接方配置。
* 每次命中计数(``expert_work_workspace_compat_dir_hits_total``)并记一条结构化日志,
  日志里目录名只留形状(不带客户内容),好据此决定什么时候收掉。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import replace
from datetime import UTC, date, datetime
from uuid import UUID

from expert_work.common.observability import expert_work_counter
from orchestrator.tools.nas_workspace_store import _WorkspacePathNotFoundError
from orchestrator.tools.registry import ToolContext
from orchestrator.tools.sandbox import SandboxSupervisorError, WorkspaceFileNotFoundError
from orchestrator.tools.workspace_paths import WorkspaceView, require_compat_dir_name
from orchestrator.tools.workspace_scope import agent_scope, conversation_scope
from orchestrator.tools.workspace_store import WorkspaceStore

logger = logging.getLogger(__name__)

#: 过渡期截止日(ISO 日期,含当天)。运维设置,不是对接方配置。
COMPAT_UNTIL_ENV = "EXPERT_WORK_WORKSPACE_COMPAT_ROOT_DIRS_UNTIL"

#: 「目录不存在」在各个 store 里的两种形态 —— NAS 对不存在的目录抛它模块内部的
#: ``_WorkspacePathNotFoundError``(与 ``figure_lookup`` 同一个口径)。新对话的目录要到
#: 第一次 exec 才建出来,它的根是空的,不是故障。
MISSING_DIR_ERRORS = (WorkspaceFileNotFoundError, _WorkspacePathNotFoundError)

#: 一个 agent 根下最多映射多少个目录 —— 与 supervisor ``ExecRequest.compat_dirs`` 的上限
#: 同一个数(超出的按名字排序截掉;真到这个量级说明根下堆的是别的东西,不是长期模板)。
MAX_COMPAT_DIRS = 64

compat_dir_hits_total = expert_work_counter(
    "expert_work_workspace_compat_dir_hits_total",
    "B-236 transition-window hits on agent-root directories mapped into a conversation "
    "workspace, by channel (file_tool = a file tool resolved a mapped path; exec_ref = "
    "bash / exec_python code mentions a mapped directory).",
    ("channel", "agent_key"),
)

_SHAPE_RE = re.compile(r"(?P<N>[0-9]+)|(?P<A>[A-Za-z]+)|(?P<H>[^\x00-\x7f]+)")


def compat_until(raw: str | None = None) -> date | None:
    """过渡期截止日;没设或写错是 ``None``(写错记一条 warning,按没设处理)。"""
    value = os.environ.get(COMPAT_UNTIL_ENV) if raw is None else raw
    if not value or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        logger.warning("workspace_compat.invalid_until value=%r — mapping disabled", value)
        return None


def compat_window_open(*, today: date | None = None) -> bool:
    until = compat_until()
    if until is None:
        return False
    return (today or datetime.now(UTC).date()) <= until


async def discover_compat_dirs(
    store: WorkspaceStore,
    *,
    tenant_id: UUID,
    user_id: UUID,
    view: WorkspaceView,
    today: date | None = None,
) -> tuple[str, ...]:
    """本对话视图要映射哪些 agent 根目录(窗口没开 / 不是对话视图 = 空)。

    列两次目录:agent 根(找候选)与本对话目录(排除对话自己已有内容的同名条目)。
    列不动只记日志、返回空 —— 映射是过渡期的便利,不能让它把一次工具调用拖垮。
    """
    if not view.per_conversation or view.conv_id is None or not compat_window_open(today=today):
        return ()
    try:
        root = await store.list_dir(
            tenant_id=tenant_id, user_id=user_id, scope=agent_scope(view.agent_key), path="."
        )
        candidates = [e.name for e in root.entries if e.is_dir and _mappable(e.name)]
        if not candidates:
            return ()
        conv_scope = conversation_scope(view.agent_key, view.conv_id)
        own = await _conversation_entries(store, tenant_id, user_id, conv_scope)
        keep: list[str] = []
        for name in candidates:
            if name not in own:
                keep.append(name)
            elif own[name] and await _is_empty_dir(store, tenant_id, user_id, conv_scope, name):
                keep.append(name)
    except SandboxSupervisorError:
        logger.warning("workspace_compat.discovery_failed", exc_info=True)
        return ()
    return tuple(sorted(keep)[:MAX_COMPAT_DIRS])


async def with_workspace_compat(
    ctx: ToolContext, store: WorkspaceStore | None, *, today: date | None = None
) -> ToolContext:
    """``ctx`` 带上本步的过渡期名单(没有工作区存储 / 没绑用户 = 原样返回)。"""
    view = ctx.workspace_view
    if store is None or ctx.tenant_id is None or ctx.user_id is None or not view.per_conversation:
        return ctx
    dirs = await discover_compat_dirs(
        store, tenant_id=ctx.tenant_id, user_id=ctx.user_id, view=view, today=today
    )
    return replace(ctx, workspace_compat_dirs=dirs) if dirs else ctx


def record_compat_hit(*, channel: str, agent_key: str, name: str) -> None:
    """记一次命中:计数 + 结构化日志(目录名只留形状)。"""
    compat_dir_hits_total.labels(channel=channel, agent_key=agent_key).inc()
    logger.info(
        "workspace.compat_hit channel=%s agent_key=%s dir_shape=%s",
        channel,
        agent_key,
        name_shape(name),
    )


def record_exec_refs(ctx: ToolContext, text: str) -> None:
    """bash / exec_python 的代码文本里提到 ``<映射目录>/`` 就记一次 ``exec_ref`` 命中。

    只是粗略的字符串判断(提到不等于用到),目的是看过渡期映射还有没有人在用。
    """
    view = ctx.workspace_view
    for name in view.compat_dirs:
        if f"{name}/" in text:
            record_compat_hit(channel="exec_ref", agent_key=view.agent_key, name=name)


def name_shape(name: str) -> str:
    """目录名的形状:数字串 → ``<N>``,ASCII 字母串 → ``<A>``,其余非 ASCII 串 → ``<H>``。"""
    return _SHAPE_RE.sub(lambda m: f"<{m.lastgroup}>", name)


def _mappable(name: str) -> bool:
    if name.startswith("."):
        return False
    try:
        require_compat_dir_name(name)
    except ValueError:
        return False
    return True


async def _conversation_entries(
    store: WorkspaceStore, tenant_id: UUID, user_id: UUID, scope: str
) -> dict[str, bool]:
    """本对话目录根下的条目 → 是不是(真)目录。目录还没建出来 = 空。"""
    try:
        listing = await store.list_dir(tenant_id=tenant_id, user_id=user_id, scope=scope, path=".")
    except MISSING_DIR_ERRORS:
        return {}
    return {e.name: e.is_dir for e in listing.entries}


async def _is_empty_dir(
    store: WorkspaceStore, tenant_id: UUID, user_id: UUID, scope: str, name: str
) -> bool:
    listing = await store.list_dir(tenant_id=tenant_id, user_id=user_id, scope=scope, path=name)
    return not listing.entries
