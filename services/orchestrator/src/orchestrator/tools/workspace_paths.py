"""工作区路径的作用域解析 —— agent 根 / ``shared/`` 的唯一真源(B-50)。

沙箱里的 ``/workspace`` 挂的是**用户根**:热沙箱按 ``(tenant, user)`` 复用
(``sandbox_instance`` 没有 agent 列、``acquire()`` 不收 agent),而 CSI 的
``subPath`` 在 create 时就钉死了 —— 挂载点不可能按 agent 分(spec §三)。
分层因此做在路径上:每个 agent 的默认根是 ``/workspace/agents/<agent_key>``,
交给 ``build_*_wrapper(..., ws=...)`` 的 ``ws`` 参数,由沙箱内片段既有的
``realpath`` + 前缀守卫强制(``file_ops.py`` 的 ``_PRELUDE``)—— 与它挡 ``..``
是同一道闸,不是新加的一道。

``shared/`` 存迁移期反推不出归属的 legacy,**可读不可写**,而且**不与默认根
合并**:要读必须显式写 ``shared:`` 前缀。不合并是有意的 —— 让归属不明的 legacy
文件默默混进日常列表,正是本设计要治的病。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

#: 显式跨到用户级 ``shared/`` 区的前缀(照 ADK 的 ``user:`` 约定)。
SHARED_PREFIX = "shared:"

#: 沙箱内的用户工作区根(挂载点)。**导出而非私有**:``file_ops`` 的迁移期
#: 读回落要用它,而那个模块自己也有一个同名私有常量 —— 两处各改各的就会静默
#: 分叉,回落目标和挂载点对不上时没有任何测试会红。
USER_ROOT = "/workspace"

#: 布局里的保留段。**导出**:``file_ops._require_path`` 要用它来拒绝以这一段
#: 开头的相对路径 —— 迁移期读回落会把 ``agents/<别人的 key>/x`` 变成一次合法的
#: 跨 agent 读(agent 根下找不到 → 回落用户根 → 正好命中别人的目录)。
AGENTS_DIR = "agents"
_SHARED_DIR = "shared"

#: ``agent_key`` 来自 ``config["configurable"]`` —— 不可信。它会被拼进 ``ws``,
#: 一个带 ``/`` 或 ``..`` 的值能把整个作用域撬到 ``/workspace`` 之外。形状与
#: ``sanitize_agent_key()`` 的产物一致:``[A-Za-z0-9._-]+``(见
#: ``expert_work.protocol.agent_key``)。
_AGENT_KEY_OK = re.compile(r"\A[A-Za-z0-9._-]+\Z")

#: 会改字节的工具 —— 它们不许写进 ``shared/``。
_WRITE_TOOLS = frozenset({"write_file", "edit_file"})


class WriteToSharedError(ValueError):
    """写 ``shared:`` —— 显式拒绝,不静默改写目标。

    静默改写(把 ``shared:x.md`` 当成 ``x.md`` 写进 agent 目录)会让模型以为
    自己更新了共享文件,而实际上造了个同名副本。报错让它当场看见。
    """


def agent_workspace_root(agent_key: str) -> str:
    """该 agent 在沙箱内的默认根;``agent_key`` 为空时回落用户根。

    空串 = 未绑定 agent(与 ``agent_key_envs("")`` 同一个口径),行为与 B-50
    之前一致。任何非空但不是单个安全路径段的取值一律拒 —— 它是不可信输入。
    """
    if not agent_key:
        return USER_ROOT
    if not _AGENT_KEY_OK.match(agent_key):
        msg = f"agent_key is not a safe path segment: {agent_key!r}"
        raise ValueError(msg)
    return f"{USER_ROOT}/{AGENTS_DIR}/{agent_key}"


def resolve_scope(path: str, *, agent_key: str, tool: str) -> tuple[str, str]:
    """把工具收到的 ``path`` 拆成 ``(ws, rel)``。

    ``ws`` 交给 ``build_*_wrapper(..., ws=ws)``,``rel`` 是相对它的路径。
    ``..`` / 绝对路径 / NUL 的校验仍由调用方的 ``_require_path`` 负责 —— 本函数
    只负责作用域选择,外加保证 ``shared:`` 不是绕过那些校验的后门(前缀是在
    ``_require_path`` **之后**才被剥掉的,所以剥完要自己再查一遍)。
    """
    raw = path.strip()
    if raw.startswith(SHARED_PREFIX):
        if tool in _WRITE_TOOLS:
            msg = (
                f"{tool} cannot write to the shared area; "
                f"drop the {SHARED_PREFIX!r} prefix to write into your own workspace"
            )
            raise WriteToSharedError(msg)
        rel = raw[len(SHARED_PREFIX) :].strip()
        if not rel or rel.startswith("/") or ".." in PurePosixPath(rel).parts:
            msg = f"{tool} path must be relative and free of '..': {path!r}"
            raise ValueError(msg)
        return f"{USER_ROOT}/{_SHARED_DIR}", rel
    return agent_workspace_root(agent_key), raw
