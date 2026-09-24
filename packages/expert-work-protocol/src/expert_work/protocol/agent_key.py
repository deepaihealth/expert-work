"""``agent_key`` —— agent 在路径与数据库里的命名空间段。

从 ``AgentMetadata.name`` 算出的、安全且抗碰撞的单个路径段。同一个值出现在
四个地方,**必须是同一个算法算出来的**:

* 沙箱技能目录 ``{SANDBOX_SKILLS_ROOT}/<agent_key>/<skill>/``
  (``orchestrator.tools.skill_seed``);
* ``PYTHONUSERBASE`` 的 per-agent 段(``orchestrator.tools.sandbox.agent_key_envs``);
* ``artifact.agent_key`` 列(B-50 工作区分层,迁移 ``0154``);
* NAS 工作区的 ``agents/<agent_key>/`` 子树。

定义住在 protocol 而不是 orchestrator,是因为持久层(``expert-work-persistence``)
只依赖 protocol、拿不到 orchestrator,而迁移 ``0154`` 的回填要算这个键。
``orchestrator.tools.skill_seed`` 从这里**转出**同名符号,老导入点照旧可用。

B-64 回修 C1 —— ``require_safe_key`` 也下沉到这里,理由是同一条:
``expert_work.protocol.multimodal.parse_workspace_image_ref`` 要校验 ref 字符串
里 ``agents/<key>/`` 段的 ``key`` 是不是一个安全的路径段,而 protocol 包不能反向
依赖 ``orchestrator``(它原来住在 ``orchestrator.tools.workspace_paths``)。这里
是**唯一**实现,``orchestrator.tools.workspace_paths.require_safe_key`` 转出同一个
符号,不再自己维护第二份正则——两份字面量正是本仓这一类 bug 的常见根因
(见 ``sanitize_agent_key`` 上面这段注释,同一个教训)。
"""

from __future__ import annotations

import hashlib
import re

__all__ = ["require_safe_key", "sanitize_agent_key"]

#: ``AgentMetadata.name`` 只约束长度不约束字符集,路径段里任何不在
#: this set 的字符都收敛成 ``-``。
_AGENT_KEY_DISALLOWED = re.compile(r"[^a-zA-Z0-9._-]")
#: Cap on the sanitized *prefix* (before the digest suffix). ``AgentMetadata.name``
#: allows up to 128 chars (``agent_spec.py``); ``<prefix>-<8-hex>`` at this cap
#: is at most 96 + 1 + 8 = 105 bytes — comfortably inside any filesystem's
#: path-segment limit (e.g. ext4/NAS's 255 bytes) with room to spare.
_AGENT_KEY_PREFIX_MAX_LEN = 96

#: ``require_safe_key`` 的字符集闸 —— ``sanitize_agent_key`` 的产物
#: (``<清洗前缀>-<8 位 hex>``)恒过这条正则;这里反过来用它去**校验**一个
#: 不可信字符串是不是一个安全的路径段。
_AGENT_KEY_OK = re.compile(r"\A[A-Za-z0-9._-]+\Z")
#: 单独的 ``.`` / ``..`` 能过上面那条正则(两个点都在字符集里),必须单列拒绝。
_DOTTED = frozenset({".", ".."})


def require_safe_key(agent_key: str) -> None:
    """``agent_key`` 当成路径段安全吗。坏 key 一律 ``ValueError``,别悄悄回落。

    B-84 —— 从 ``_require_safe_key`` 改成公开名:宿主侧的作用域解析
    (``orchestrator.tools.workspace_scope.scope_parts``)要用同一道闸,而不是在
    旁边再写一条正则。
    """
    if not agent_key or agent_key in _DOTTED or not _AGENT_KEY_OK.match(agent_key):
        msg = f"agent_key is not a safe path segment: {agent_key!r}"
        raise ValueError(msg)


def sanitize_agent_key(name: str) -> str:
    """Turn an agent's manifest name into a safe, collision-resistant path segment.

    Character-cleaning alone is NOT injective: ``AgentMetadata.name`` only
    constrains length (``min_length=1, max_length=128``, ``agent_spec.py``),
    not a character set, and the DB uniqueness constraint is on the raw text
    ``(tenant_id, name, version)`` — so two distinct, equally valid agent
    names can clean to the identical prefix (e.g. ``"a/b"`` and ``"a-b"``
    both collapse to ``"a-b"``). A collision here would silently defeat the
    whole point of the namespace: two agents' skill files would land in the
    same seed subtree (cross-visible), share the same ``PYTHONUSERBASE``
    (pip installs clobbering each other), and — since B-50 — collapse back
    into one ``artifact`` row and one workspace subtree, which is the exact
    bug that feature exists to fix. A short digest of the ORIGINAL
    (pre-clean) name is therefore appended so the combined key stays unique
    across distinct inputs even when their cleaned prefixes collide; hashing
    the *cleaned* prefix instead would reproduce the same collision this
    exists to avoid. The prefix is capped (see
    :data:`_AGENT_KEY_PREFIX_MAX_LEN`) purely for path-length hygiene —
    uniqueness comes entirely from the digest, not the prefix.
    """
    sanitized = _AGENT_KEY_DISALLOWED.sub("-", name)[:_AGENT_KEY_PREFIX_MAX_LEN] or "agent"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{sanitized}-{digest}"
