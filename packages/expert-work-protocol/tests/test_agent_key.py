"""``sanitize_agent_key`` 搬进 protocol —— 一处定义,上下层都拿得到。

它以前住在 ``orchestrator.tools.skill_seed``,只服务沙箱里的 ``/opt/skills/<key>``。
工作区分层(B-50)之后它同时是:沙箱技能目录段、``PYTHONUSERBASE`` 段、
``artifact.agent_key`` 列的值、NAS 工作区 ``agents/<key>/`` 段。持久层
(只依赖 protocol、拿不到 orchestrator)也要算它,所以定义下沉到 protocol。

**只许有一种算法** —— 这里的断言与 ``orchestrator/tests/test_skill_seed.py``
的那组是同一套判据,搬家不许改行为。
"""

from __future__ import annotations

import hashlib
import re

from expert_work.protocol.agent_key import sanitize_agent_key


def _expected(name: str) -> str:
    """独立复刻 docstring 里的规则 —— 与实现分开写,才是判据而不是重言式。"""
    prefix = re.sub(r"[^a-zA-Z0-9._-]", "-", name)[:96] or "agent"
    return f"{prefix}-{hashlib.sha256(name.encode('utf-8')).hexdigest()[:8]}"


def test_sanitize_agent_key_matches_the_documented_rule() -> None:
    assert sanitize_agent_key("pptx-skill-test") == _expected("pptx-skill-test")
    assert sanitize_agent_key("my_agent.v2") == _expected("my_agent.v2")
    assert sanitize_agent_key("") == _expected("")


def test_digest_suffix_is_unconditional() -> None:
    """摘要后缀恒在 —— 干净的名字也不例外,否则「干净名 vs 被清洗名」会撞。"""
    assert sanitize_agent_key("pptx-skill-test") != "pptx-skill-test"
    assert sanitize_agent_key("").startswith("agent-")


def test_prefix_collisions_stay_distinct() -> None:
    """字符清洗不是单射:``a/b`` 与 ``a-b`` 清洗后同形,摘要必须把它们分开。"""
    assert sanitize_agent_key("a/b") != sanitize_agent_key("a-b")
    assert sanitize_agent_key("团队/结账") != sanitize_agent_key("团队-结账")


def test_length_is_bounded() -> None:
    """``AgentMetadata.name`` 上限 128;键必须留在任何文件系统的路径段限内。"""
    assert len(sanitize_agent_key("x" * 128)) == 96 + 1 + 8


def test_orchestrator_reexport_is_the_same_object() -> None:
    """老导入点 ``orchestrator.tools.skill_seed`` 必须是**转出**,不是第二份实现。

    两份实现漂移 = 沙箱里的 ``/opt/skills/<key>`` 与库里的 ``artifact.agent_key``
    指向不同的 agent,而且不会报错。
    """
    from orchestrator.tools.skill_seed import sanitize_agent_key as reexported

    assert reexported is sanitize_agent_key
