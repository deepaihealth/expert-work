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
"""

from __future__ import annotations

import hashlib
import re

__all__ = ["sanitize_agent_key"]

#: ``AgentMetadata.name`` 只约束长度不约束字符集,路径段里任何不在
#: this set 的字符都收敛成 ``-``。
_AGENT_KEY_DISALLOWED = re.compile(r"[^a-zA-Z0-9._-]")
#: Cap on the sanitized *prefix* (before the digest suffix). ``AgentMetadata.name``
#: allows up to 128 chars (``agent_spec.py``); ``<prefix>-<8-hex>`` at this cap
#: is at most 96 + 1 + 8 = 105 bytes — comfortably inside any filesystem's
#: path-segment limit (e.g. ext4/NAS's 255 bytes) with room to spare.
_AGENT_KEY_PREFIX_MAX_LEN = 96


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
