"""工作区路径的作用域解析 —— agent 根 / ``shared/`` 的唯一真源(B-50)。

挂载仍按用户:热沙箱按 ``(tenant, user)`` 复用(``sandbox_instance`` 没有 agent 列、
``acquire()`` 不收 agent),CSI 的 ``subPath`` 在 create 时就钉死了 —— 挂载点不可能
按 agent 分(spec §三)。分层因此不再做在挂载点上,而是做在**每次 exec 自己的
mount namespace** 里(B-60):绑了 agent 时,``/workspace`` 这个视图本身就是那个
agent 在 NAS 上的目录(``agent_nas_root`` bind 成 ``EXEC_VIEW``);没绑时视图是
整个用户根。``build_*_wrapper(..., ws=...)`` 的 ``ws`` 因此恒为 ``EXEC_VIEW`` ——
不再需要 ``/workspace/agents/<agent_key>`` 这种子路径去分层,那个拼法现在只是
``agent_view_alias`` 折叠模型照旧写法用的别名,不指向真实目录。沙箱内片段既有的
``realpath`` + 前缀守卫(``file_ops.py`` 的 ``_PRELUDE``)—— 与它挡 ``..``
是同一道闸,不是新加的一道 —— 仍然把 agent 关在 ``EXEC_VIEW`` 里。

``shared/`` 存迁移期反推不出归属的 legacy,**可读不可写**,而且**不与默认根
合并**:要读必须显式写 ``shared:`` 前缀。不合并是有意的 —— 让归属不明的 legacy
文件默默混进日常列表,正是本设计要治的病。
"""

from __future__ import annotations

from pathlib import PurePosixPath

from expert_work.persistence import WORKSPACE_AGENTS_DIR, WORKSPACE_SHARED_DIR
from expert_work.protocol.agent_key import require_safe_key as require_safe_key
from orchestrator.tools.sandbox_image_contract import EXEC_VIEW, NAS_MOUNT

#: 显式跨到用户级 ``shared/`` 区的前缀(照 ADK 的 ``user:`` 约定)。
#: 由目录名拼出来,不写第二遍字面量 —— 前缀与它指向的目录必须永远同名。
SHARED_PREFIX = f"{WORKSPACE_SHARED_DIR}:"

#: 布局里的保留段。**导出**:``file_ops._require_path`` 要用它来拒绝以这一段
#: 开头的相对路径 —— 迁移期读回落会把 ``agents/<别人的 key>/x`` 变成一次合法的
#: 跨 agent 读(agent 根下找不到 → 回落用户根 → 正好命中别人的目录)。
#:
#: **别名,不是第二份定义。** 真源在 ``expert_work.persistence`` 的
#: ``workspace/layout.py`` —— 那里是布局前缀的既有单源(``skills`` /
#: ``uploads`` 也在那儿),而搬迁脚本、留存 job、控制台浏览面都够不着
#: orchestrator。两处各写各的字面量,搬迁把文件放进 ``agents/`` 而沙箱去
#: ``agent/`` 找,不会有任何测试红。``test_workspace_paths.py`` 钉了
#: ``AGENTS_DIR == WORKSPACE_AGENTS_DIR``(用 ``==`` 不是 ``is`` —— 字符串
#: 会被 intern,同值的第二份字面量 ``is`` 照样为真,那条判据是摆设)。
AGENTS_DIR = WORKSPACE_AGENTS_DIR
_SHARED_DIR = WORKSPACE_SHARED_DIR

#: ``require_safe_key`` 的定义已下沉到 ``expert_work.protocol.agent_key``
#: ——``parse_workspace_image_ref``(protocol 包,B-64 回修 C1)要校验 ref 字符
#: 串里 ``agents/<key>/`` 段的 key,而 protocol 不能反向 import orchestrator。
#: 这里公开**转出**同一个对象(见上面 import 的 ``as require_safe_key``),老
#: 导入点 ``from orchestrator.tools.workspace_paths import require_safe_key``
#: 照旧可用;绝不在这里复制第二份实现。

#: 会改字节的工具 —— 它们不许写进 ``shared/``。B-64 回修 I6 —— ``read_page``
#: 也落在里面:它虽然不叫 write_file/edit_file,但会往 out_dir 里
#: makedirs+落 jpg/中间 pdf,``shared/`` 是只读 bind,makedirs 会 OSError,而且
#: 不挡的话 ``shared:d.pptx`` 与 ``d.pptx`` 会被判成同一个 ``doc_sha``(两个不同
#: 文档共用一个 out_dir)。
_WRITE_TOOLS = frozenset({"write_file", "edit_file", "read_page"})


class WriteToSharedError(ValueError):
    """写 ``shared:`` —— 显式拒绝,不静默改写目标。

    静默改写(把 ``shared:x.md`` 当成 ``x.md`` 写进 agent 目录)会让模型以为
    自己更新了共享文件,而实际上造了个同名副本。报错让它当场看见。
    """


def agent_nas_root(agent_key: str) -> str:
    """NAS 挂载下该 agent 的真实目录 —— **只给两个后端拼 exec 用**(命名空间里 bind
    到 ``EXEC_VIEW`` 上的源;B-60 spec §4.3)。空 key 拒绝:未绑 agent 没有「自己的
    目录」,调用方自己分支(未绑 → bind 整个用户根),别让它悄悄拿到 ``NAS_MOUNT``。
    """
    require_safe_key(agent_key)
    return f"{NAS_MOUNT}/{AGENTS_DIR}/{agent_key}"


def agent_view_alias(agent_key: str) -> str:
    """模型照旧可能写的 ``/workspace/agents/<key>`` 拼法 —— **只用于折叠**
    (``file_ops._require_path`` / ``artifact._validate_path``),不指向任何真实目录:
    视图里没有 ``agents/``。空 key 拒绝,同上。
    """
    require_safe_key(agent_key)
    return f"{EXEC_VIEW}/{AGENTS_DIR}/{agent_key}"


def resolve_scope(path: str, *, agent_key: str, tool: str) -> tuple[str, str]:
    """把工具收到的 ``path`` 拆成 ``(ws, rel)``。

    ``ws`` 交给 ``build_*_wrapper(..., ws=ws)``,``rel`` 是相对它的路径。
    ``..`` / 绝对路径 / NUL 的校验仍由调用方的 ``_require_path`` 负责 —— 本函数
    只负责作用域选择,外加保证 ``shared:`` 不是绕过那些校验的后门(前缀是在
    ``_require_path`` **之后**才被剥掉的,所以剥完要自己再查一遍)。
    """
    raw = path.strip()
    if raw.startswith(SHARED_PREFIX):
        rel = raw[len(SHARED_PREFIX) :].strip()
        if tool == "read_page":
            # 终审 #5 —— read_page 被挡是因为它要往文档旁边落渲染产物,不是模型想写
            # 共享区;照抄写工具那句「去掉前缀」会把它指向另一个(多半不存在的)文件。
            msg = (
                f"read_page 不能直接渲染共享区里的文档 {rel!r} —— 先把它复制到你自己的工作区"
                f"(比如 cp {EXEC_VIEW}/{_SHARED_DIR}/{rel} {rel}),再对副本调用 read_page。"
            )
            raise WriteToSharedError(msg)
        if tool in _WRITE_TOOLS:
            msg = (
                f"{tool} cannot write to the shared area; "
                f"drop the {SHARED_PREFIX!r} prefix to write into your own workspace"
            )
            raise WriteToSharedError(msg)
        if not rel or rel.startswith("/") or ".." in PurePosixPath(rel).parts:
            msg = f"{tool} path must be relative and free of '..': {path!r}"
            raise ValueError(msg)
        return f"{EXEC_VIEW}/{_SHARED_DIR}", rel
    # B-60 —— 绑不绑都是视图根:文件工具的片段与用户代码在同一个命名空间里,绑了
    # agent 时 /workspace 就是 agent 目录。agent_key 仍校验(不可信输入,坏 key 早点炸)。
    if agent_key:
        agent_view_alias(agent_key)
    return EXEC_VIEW, raw
