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

import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from expert_work.persistence import (
    SANDBOX_SKILLS_ROOT,
    WORKSPACE_AGENTS_DIR,
    WORKSPACE_INPUTS_DIR,
    WORKSPACE_OVERFLOW_DIR,
    WORKSPACE_SHARED_DIR,
    WORKSPACE_SKILLS_DIR,
    WORKSPACE_UPLOADS_DIR,
)
from expert_work.protocol.agent_key import require_safe_key as require_safe_key
from orchestrator.tools.sandbox_image_contract import AGENT_VIEW, EXEC_VIEW, NAS_MOUNT

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

#: B-225 —— skill files are seeded outside the workspace, under
#: ``SANDBOX_SKILLS_ROOT``; models reach for them with ``../opt/skills/…``. The
#: rejection names the two ways that work, so the next call does not guess.
_SKILLS_SEGMENT = SANDBOX_SKILLS_ROOT.lstrip("/") + "/"
_SKILLS_PATH_HINT = (
    ". Skill files are outside the workspace: load one with "
    "skill_view(skill_name, path), or read or run it with bash under "
    "$EXPERT_WORK_SKILLS_DIR/<skill name>/."
)


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


#: B-236 —— 工具描述里讲路径规矩的那一句,文件类工具共用一份(措辞两种视图都成立:
#: 老视图里 ``/workspace`` 与 ``/agent`` 是同一个目录,没有副本可言)。对话视图里
#: ``/workspace`` 开场时带着 agent 根各目录的副本(``conversation_seed``),改副本只影响本对话。
WORKSPACE_PATH_RULE = (
    "Relative paths and /workspace/... refer to the sandbox working directory — in a "
    "conversation started after this release, that conversation's own directory, which "
    "starts with copies of the agent's directories; /agent/... is this agent's directory, "
    "kept across all of its conversations. Editing a copy changes only this conversation; "
    "put anything meant for later conversations under /agent/."
)
#: B-236 —— ``bash`` / ``exec_python`` 描述里讲工作目录的那一句。口径照 B-60 spec §4.7/§五:
#: 不说「隔离」「看不见」,只说默认视图里有什么 —— 命名空间对故意的代码不是绝对边界。
#: 每一种视图里都得是真话:工具描述随 agent 构建缓存、不分对话,发布前已开的对话(老视图)
#: 读到的也是这一句,那里 ``/workspace`` 就是 agent 目录。
EXEC_WORKSPACE_NOTE = (
    "Runs in /workspace. In a conversation started after this release, /workspace is "
    "that conversation's own working directory, which starts with copies of the agent's "
    "directories (editing a copy changes only this conversation); in an older "
    "conversation it is the agent's directory. /agent/ is always this agent's directory, "
    "shared by all of its conversations — put files meant for later conversations "
    "(templates, style guides, helper scripts) there. Other agents' files are not "
    "part of the default view. Uploaded files are in uploads/."
)
#: B-236 —— 文件类工具 ``path`` 参数的说明。
PATH_ARG_DESCRIPTION = "File path relative to /workspace, or under /agent/ (no '..')."

#: B-236 —— 对话视图里 agent 根下的子目录名,每个对话一格:``threads/<conv>/``(状态
#: 投影 ``PLAN.md`` 等)与 ``threads/<conv>/work/``(该对话的 ``/workspace``)。
THREADS_DIR = "threads"
#: 对话目录在 ``threads/<conv>/`` 底下的那一格 —— 不直接用 ``threads/<conv>/`` 本身,
#: 免得模型自己写的 ``PLAN.md`` / ``MEMORY.md`` 被每轮结束时的投影覆盖。
CONVERSATION_WORK_DIR = "work"

#: B-236 —— 对话视图里**仍在 agent 根**、读写 bind 进 ``/workspace`` 的平台目录。与
#: ``exec_view.EXEC_VIEW_SCRIPT`` 内嵌 python 里的 ``PLATFORM_DIRS`` 是同一组
#: (``test_exec_view`` 钉两边相等):平台发给模型的 ``uploads/<名>`` /
#: ``inputs/<run>/…`` / ``.tool_results/<run>/…`` 在任何对话里都照常可读。
PLATFORM_DIRS: tuple[str, ...] = (
    WORKSPACE_UPLOADS_DIR,
    WORKSPACE_INPUTS_DIR,
    WORKSPACE_OVERFLOW_DIR,
)

#: B-236 —— agent 根下不算「基础目录」的一级名字:布局与平台自己的目录。开场复制
#: (``conversation_seed``)与运行结束时的版本留存(``agent_file_versions``)都不碰它们;
#: 点开头的名字另由调用方跳过。
AGENT_ROOT_RESERVED = frozenset(
    {THREADS_DIR, *PLATFORM_DIRS, WORKSPACE_SHARED_DIR, WORKSPACE_SKILLS_DIR, AGENTS_DIR}
)

#: B-236 第三层 —— agent 自己的记忆索引(agent 根下,模型看到的是 ``/agent/AGENT_NOTES.md``)。
#: 刻意不叫 ``MEMORY.md``:那个名字是老视图里平台投影长期记忆的文件。委派子代不许写它
#: (``file_ops``),对话第一次 run 开场时读一次(``graph_builder.agent_notes``)。
AGENT_NOTES_FILE = "AGENT_NOTES.md"

ResolvedKind = Literal["conversation", "agent", "platform", "shared", "legacy"]


@dataclass(frozen=True)
class WorkspaceView:
    """一次工具调用看到的工作区:哪个 agent、哪个对话。

    ``conv_id`` 为 ``None`` = 老视图(``/workspace`` 与 ``/agent`` 都是 agent 根),
    发布前已开的对话、合成执行路径与未绑 agent 的调用都是这一档。

    ``thread_id`` 是本对话的线程 id:两种视图里 agent 根下的 ``threads/`` 都只留它那一格
    (老视图也不许看见别的对话 —— 包括发布后新开的对话的工作目录)。对话视图里它就是
    ``conv_id``(见 :attr:`own_thread`);``None`` = 一格都不留。
    """

    agent_key: str = ""
    conv_id: UUID | None = None
    thread_id: UUID | None = None

    @property
    def per_conversation(self) -> bool:
        return bool(self.agent_key) and self.conv_id is not None

    @property
    def own_thread(self) -> UUID | None:
        """``threads/`` 底下留给本对话的那一格。"""
        return self.conv_id if self.conv_id is not None else self.thread_id


@dataclass(frozen=True)
class ResolvedPath:
    """:func:`resolve_workspace_path` 的结果。

    ``ws`` 交给 ``build_*_wrapper(..., ws=ws)`` 与 ``store_scope``,``rel`` 是相对它的路径
    (``"."`` = 这个视图的根)。``path`` 是归一化后的、模型可再传回来的写法(``shared:``
    前缀保留),给报错与 ``read_page`` 的文档记录用。
    """

    ws: str
    rel: str
    kind: ResolvedKind
    path: str


def _view_alias_rest(cleaned: str, agent_key: str) -> str | None:
    """``/workspace/agents/<自己>[/x]`` 这一老写法 —— 它一直指 agent 根,折成 ``/agent`` 的余部。"""
    own = agent_view_alias(agent_key)
    if cleaned in (own, f"{own}/"):
        return ""
    if cleaned.startswith(f"{own}/"):
        return cleaned[len(own) + 1 :]
    return None


def _shared_target(raw: str, rel: str, *, tool: str) -> ResolvedPath:
    if tool == "read_page":
        # 终审 #5 —— read_page 被挡是因为它要往文档旁边落渲染产物,不是模型想写
        # 共享区;照抄写工具那句「去掉前缀」会把它指向另一个(多半不存在的)文件。
        # 命令要能原样执行:上传文件名常带空格与括号(``报告 (1).pdf``),不加引号
        # 在 bash 里是语法错误;目标取文件名本身,落在工作区根,不依赖子目录存在。
        copy = PurePosixPath(rel).name
        source = shlex.quote(f"{EXEC_VIEW}/{_SHARED_DIR}/{rel}")
        msg = (
            f"read_page 不能直接渲染共享区里的文档 {rel!r} —— 先把它复制到你自己的工作区"
            f"(比如 cp {source} {shlex.quote(copy)}),再对副本 {copy!r} 调用 read_page。"
        )
        raise WriteToSharedError(msg)
    if tool in _WRITE_TOOLS:
        msg = (
            f"{tool} cannot write to the shared area; "
            f"drop the {SHARED_PREFIX!r} prefix to write into your own workspace"
        )
        raise WriteToSharedError(msg)
    if not rel or rel.startswith("/") or ".." in PurePosixPath(rel).parts:
        msg = f"{tool} path must be relative and free of '..': {raw!r}"
        raise ValueError(msg)
    return ResolvedPath(
        ws=f"{EXEC_VIEW}/{_SHARED_DIR}", rel=rel, kind="shared", path=f"{SHARED_PREFIX}{rel}"
    )


def _reject_reserved_head(
    parts: tuple[str, ...], *, raw: str, tool: str, agent_view: bool, own_thread: str | None
) -> None:
    """布局保留段:``agents/`` 是别人的树、``shared/`` 只能经 ``shared:`` 读;agent 根下的
    ``threads/`` 是各对话自己的目录 —— ``/agent`` 那一侧一律不许寻址,老视图里只许寻址
    本对话自己那一格 ``threads/<本对话>/``。"""
    head = parts[0] if parts else ""
    other_thread = head == THREADS_DIR and (
        agent_view or own_thread is None or parts[1:2] != (own_thread,)
    )
    if head == AGENTS_DIR or other_thread:
        # B-50 —— 不拒的话迁移期读回落正好把 ``agents/<别人的 key>/x`` 办成一次合法的
        # 跨 agent 读:自己根下找不到 → 回落用户根 → 不偏不倚命中别人的目录。
        msg = (
            f"{tool} path must be relative to your own workspace; "
            f"{head!r} is a reserved layout segment: {raw!r}"
        )
        raise ValueError(msg)
    if head == _SHARED_DIR:
        # B-60 —— 视图里 /workspace/shared 是只读 bind(spec §4.3),裸相对路径写进去是
        # EROFS、读则读到 legacy 区。要读就显式 ``shared:``,写一律不许。
        msg = (
            f"{tool}: {_SHARED_DIR!r} is the read-only shared area — read it with "
            f"the {SHARED_PREFIX!r} prefix; it is not a directory in your workspace: {raw!r}"
        )
        raise ValueError(msg)


def resolve_workspace_path(path: str, *, view: WorkspaceView, tool: str) -> ResolvedPath:
    """把工具收到的 ``path`` 解析成 ``(视图, 相对路径)`` —— 文件工具、``save_artifact``、
    ``read_page`` 与渲染页查找共用的唯一一份规则(B-236 之前分散在 ``_require_path`` /
    ``artifact._validate_path`` / ``resolve_scope`` 三处)。

    规则按顺序:

    * NUL、``..``、``/workspace`` 与 ``/agent`` 以外的绝对路径 —— 拒。
    * ``shared:x`` —— 只读共享区(写工具拒)。
    * ``/agent`` / ``/agent/x`` —— agent 根(所有对话共用);``threads`` / ``agents`` /
      ``shared`` 开头拒。老写法 ``/workspace/agents/<自己>/x`` 一直指 agent 根,同此。
    * ``/workspace/x`` 或相对 ``x``:老视图 —— agent 根,与 B-236 之前逐字相同;对话视图 ——
      首段是平台目录(``uploads`` / ``inputs`` / ``.tool_results``)走 agent 根,其余是本对话
      自己的目录(开场复制进来的基础目录副本也在这里)。
    """
    raw = path.strip()
    if not raw:
        msg = f"{tool} requires a non-empty 'path'"
        raise ValueError(msg)
    if "\x00" in raw:
        msg = f"{tool} path must not contain a NUL byte"
        raise ValueError(msg)
    if raw.startswith(SHARED_PREFIX):
        rel = raw[len(SHARED_PREFIX) :].strip()
        if not rel:
            msg = f"{tool} requires a non-empty 'path'"
            raise ValueError(msg)
        return _shared_target(path, rel, tool=tool)
    agent_key = view.agent_key
    cleaned = raw
    if agent_key:
        alias_rest = _view_alias_rest(cleaned, agent_key)
        if alias_rest is not None:
            cleaned = f"{AGENT_VIEW}/{alias_rest}" if alias_rest else AGENT_VIEW
    agent_view = cleaned in (AGENT_VIEW, f"{AGENT_VIEW}/") or cleaned.startswith(f"{AGENT_VIEW}/")
    # Fold the well-known sandbox roots: every tool description anchors paths at
    # /workspace (and /agent), so models routinely pass "/workspace/foo" — treat
    # that as the view-relative "foo" instead of bouncing the call (a reject costs
    # a whole recovery round). Any other absolute path still rejects below, and the
    # in-sandbox realpath re-check remains the actual escape boundary.
    for root in (AGENT_VIEW, EXEC_VIEW):
        if cleaned in (root, f"{root}/"):
            cleaned = "."
            break
        if cleaned.startswith(f"{root}/"):
            cleaned = cleaned[len(root) + 1 :]
            break
    if cleaned.startswith("/") or ".." in PurePosixPath(cleaned).parts:
        msg = f"{tool} path must be a relative workspace path without '..': {path!r}"
        if _SKILLS_SEGMENT in path:
            msg += _SKILLS_PATH_HINT
        raise ValueError(msg)
    parts = PurePosixPath(cleaned).parts
    head = parts[0] if parts else ""
    rel = "/".join(parts) if parts else "."
    if agent_key and not (head == THREADS_DIR and view.per_conversation and not agent_view):
        own = str(view.own_thread) if view.own_thread is not None else None
        _reject_reserved_head(parts, raw=path, tool=tool, agent_view=agent_view, own_thread=own)
    if agent_view:
        return ResolvedPath(ws=AGENT_VIEW, rel=rel, kind="agent", path=_agent_spelling(rel))
    if not view.per_conversation:
        return ResolvedPath(ws=EXEC_VIEW, rel=rel, kind="legacy", path=rel)
    if head in PLATFORM_DIRS:
        return ResolvedPath(ws=AGENT_VIEW, rel=rel, kind="platform", path=rel)
    return ResolvedPath(ws=EXEC_VIEW, rel=rel, kind="conversation", path=rel)


def _agent_spelling(rel: str) -> str:
    return AGENT_VIEW if rel == "." else f"{AGENT_VIEW}/{rel}"


def agent_relative(target: ResolvedPath, view: WorkspaceView) -> str:
    """``target`` 相对 **agent 根**(``/agent``)的写法 —— 产物登记、渲染页落点与宿主侧
    查找都按这个物理位置算,不随模型用的是哪种写法变。

    对话目录里的 ``x`` 是 ``threads/<conv>/work/x``;其余(agent 根、平台目录、老视图)本来
    就相对 agent 根。共享区不在 agent 根下,没有这种写法。
    """
    if target.kind == "shared":
        msg = "the shared area is not under the agent root"
        raise ValueError(msg)
    if target.kind != "conversation":
        return target.rel
    base = f"{THREADS_DIR}/{view.conv_id}/{CONVERSATION_WORK_DIR}"
    return base if target.rel == "." else f"{base}/{target.rel}"
