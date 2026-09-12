"""对外平面的 agent 收口 —— ``agent_code`` ↔ ``agent_key`` 与工作区路径投影(B-50 PR4)。

产物、工作区、上传三个对外模块都要回答同一个问题:「URL 里这个 ``agent_code``
对应存储层的哪个 ``agent_key``」。答案只能有**一处**实现 —— 三份各自拼接的话,
将来改一处就静默分叉(``workspace_user_root()`` 的 docstring 记着同一类事故:
两处各自拼 ``root/tenant/user`` 差点漂开)。

**为什么不查库。** ``agent_code`` **就是** ``spec.metadata.name``:
``_external.py`` 的 ``load_owned_thread`` 用 ``meta.agent_name != agent_code``
判 404,而写入侧 ``runs.py`` 落的是 ``sanitize_agent_key(spec.metadata.name)``。
所以 ``sanitize_agent_key(agent_code)`` 与写入侧逐字节同源,查库拿回来的是同一个
字符串。查库只会多出一条失败路径 —— spec 被删或改名 → 算出另一个 key →
**归属正确**的产物 404 —— 而「某个 run 写入的产物必须能被同一个 ``agent_code``
取回」正是对接方的 P0(他们 404 会 ``HarvestError`` → 那一轮不计费)。同理
不做大小写归一:归一会把两个真正不同的 agent 折进同一棵树。

**路径投影为什么是投影而不是过滤。** 产物按 ``name`` 取、上传按 ``upload_id``
取,这两种标识搬迁后不变,加个谓词就够。工作区按 ``path`` 取,而 ``path`` 正是
搬迁会改的东西(``客户案例/x.md`` → ``agents/<agent_key>/客户案例/x.md``)。
原样透出新路径会让对接方缓存过的 path **永久失效**,并且把内部命名
``<agent_key>``(带 sha256 后缀)漏到第三方面前。对外平面本来就已经按
``agent_code`` 分区,所以对外的 path 相对**该 agent 的根**:

    存储层   {tenant}/{user}/agents/ai-health-plan-1a2b3c4d/客户案例/x.md
    对外给   客户案例/x.md                  ← 与搬迁前逐字节相同
    对外收   客户案例/x.md
    服务端拼 agents/<该 code 的 agent_key>/客户案例/x.md

于是对接方契约零变更、代码零改动;跨 agent 仍然 404(A 的 code 拼出 A 的根,
B 的文件不在那儿);``agent_key`` 挡在对外面之外。

**这三个函数都不做校验。** 调用方必须**先**把对接方给的原串过
``_safe_workspace_relpath`` 再加前缀 —— 加前缀会掩盖掉「绝对路径一律拒」和
「空路径一律拒」两条判据(``/etc/passwd`` → ``agents/<key>//etc/passwd`` 不再
以 ``/`` 开头;``""`` → ``agents/<key>/`` 不再是空的)。:func:`external_storage_path`
把这两步锁在一起,调用方没有写反的机会。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from fastapi import HTTPException

from control_plane.api._workspace_shared import (
    INVALID_WORKSPACE_PATH,
    _safe_workspace_relpath,
    workspace_agent_path,
)
from expert_work.protocol.agent_key import sanitize_agent_key
from orchestrator.tools.workspace_paths import AGENTS_DIR

#: 三个列表端点的 ``?scope``。``agent``(默认)= 只看本 agent;``user`` =
#: 该终端用户全部 agent 的并集(对接方点名要的:他们一个 app 编排两个 agent、
#: 服务同一批终端用户)。**下载端点不认这个参数** —— 放开 scope 等于让 A 的
#: code 取到 B 的字节,那正是本设计要挡的。要下别的 agent 的东西,用那个
#: agent 的 code。
ExternalScope = Literal["agent", "user"]

__all__ = [
    "ExternalScope",
    "agent_code_by_key",
    "agent_key_for_code",
    "external_storage_path",
    "external_to_storage",
    "storage_to_external",
]


def agent_key_for_code(agent_code: str) -> str:
    """URL 里的 ``agent_code`` → 该 agent 写工作区/产物时用的 ``agent_key``。

    纯函数,不查库 —— 理由见模块 docstring。
    """
    return sanitize_agent_key(agent_code)


def agent_code_by_key(agent_codes: Iterable[str]) -> dict[str, str]:
    """``{agent_key: agent_code}`` —— ``scope=user`` 的反查表。

    ``sanitize_agent_key`` 带 sha256 后缀,单向,所以反查只能靠正向重算一遍
    候选集合。候选来自 ``ThreadMetaStore.list_agent_names_for_user``(这个用户
    实际跑过的 agent),正好是 key 可能出现在其树里的那个集合。

    空白名字不进表:``sanitize_agent_key("")`` 会因为 ``or "agent"`` 兜底而
    返回 ``agent-e3b0c442``,放进去就凭空造出一个第三方看得见、却对不上任何真
    agent 的 ``agent_code``。
    """
    return {agent_key_for_code(code): code for code in agent_codes if code.strip()}


def external_to_storage(rel: str, *, agent_key: str) -> str:
    """对外相对路径 → 存储层相对路径。入口用(下载)。

    ``agent_key`` 为空 → 原样返回(用户根),与工具层
    ``agent_workspace_root("")`` 的回落语义一致。

    投影本身在 ``_workspace_shared.workspace_agent_path`` —— 控制台侧的
    上传写入与会话内读写也要同一套投影,两份实现分叉的话对外能下到的文件
    控制台下不到(反之亦然),而且两边各自的测试都不会红。
    """
    return workspace_agent_path(rel, agent_key=agent_key)


def storage_to_external(rel: str, *, agent_key: str) -> str | None:
    """存储层相对路径 → 对外相对路径;不属于该 agent 的返回 ``None``。出口用(列表)。

    返回 ``None`` 的三类,都是有意不对外投影的:
    * ``agents/<别的 key>/…`` —— 跨 agent,本设计要挡的正是它;
    * ``shared/…`` —— 搬迁时反推不出归属的 legacy,归属不明就没有可用的
      ``agent_code`` 去下载它;
    * 顶层扁平残留 —— 搬迁前的老布局,搬迁跑完就不存在了。

    前缀比较带尾部分隔符:少了它,key 是另一个 key 前缀的 agent 会被串进来
    (``agents/<key>x/…`` 匹配上 ``agents/<key>``)。剥完是空串也返回
    ``None`` —— ``agents/<key>/`` 是目录本身,不是一个文件条目。
    """
    if not agent_key:
        return rel or None
    prefix = f"{AGENTS_DIR}/{agent_key}/"
    if not rel.startswith(prefix):
        return None
    return rel[len(prefix) :] or None


def external_storage_path(raw: str, *, agent_key: str) -> str:
    """对接方给的 ``path`` → 存储层相对路径,**先校验再投影**。

    顺序是这个函数存在的全部理由,但**不是**因为 ``..`` —— 那条判据两种顺序
    都拦得住::func:`_safe_workspace_relpath` 拒绝**任何** ``..`` 段,不只是
    真能爬出用户根的那种(实测对照过,原先写在这里的理由是错的)。真正被前缀
    掩盖掉的是另外两条判据:

    * ``/etc/passwd`` 拼上前缀变成 ``agents/<key>//etc/passwd``,不再以 ``/``
      开头 → 「绝对路径一律拒」那条判据失效,放行的是一条对接方从没指名过的
      路径;
    * ``""`` / ``"   "`` 拼上前缀变成 ``agents/<key>/``,不再是空的 → 「空路径
      一律拒」失效,读到的是 agent 目录本身。

    把两步锁在一个函数里,调用方就没有把顺序写反的机会。

    抛 ``HTTPException(400)`` 而不是返回 ``None``:调用方的 ``except
    HTTPException`` 已经在渲染对外信封,多一条返回值分支只会多一处要保持同步
    的错误形状。
    """
    safe = _safe_workspace_relpath(raw)
    if safe is None:
        raise HTTPException(status_code=400, detail=INVALID_WORKSPACE_PATH)
    return external_to_storage(safe, agent_key=agent_key)
