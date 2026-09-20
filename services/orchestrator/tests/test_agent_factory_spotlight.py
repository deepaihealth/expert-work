"""Spotlighting system-prompt clause wiring — Stream PI-1.

``_assemble_system_prompt`` appends the spotlighting clause when the agent's
``defenses.prompt_injection`` resolves to ``spotlight`` (the manifest default).
Channel content-wrapping (memory/RAG, tool results) is PI-1b.
"""

from __future__ import annotations

from expert_work.common.spotlight import SPOTLIGHT_SYSTEM_CLAUSE
from orchestrator.agent_factory import _assemble_system_prompt


def test_spotlight_appends_clause_to_base() -> None:
    prompt = _assemble_system_prompt(base="BASE", skill_fragments=[], spotlight=True)
    assert prompt.startswith("BASE")
    assert SPOTLIGHT_SYSTEM_CLAUSE in prompt


def test_no_spotlight_leaves_base_unchanged() -> None:
    prompt = _assemble_system_prompt(base="BASE", skill_fragments=[], spotlight=False)
    # B-85 ③ —— 原来这里断言的是 ``prompt == base``。那只是「自己那个块不出现」
    # 的廉价代理,而平台从此恒追加一段 completion contract(无开关),代理失效。
    # 改成断言真正在乎的那件事:base 原样在最前 + 自己那个块确实不在。
    assert prompt.startswith("BASE")
    assert SPOTLIGHT_SYSTEM_CLAUSE not in prompt


def test_spotlight_clause_coexists_with_skill_blocks() -> None:
    prompt = _assemble_system_prompt(
        base="BASE", skill_fragments=["<skill>BODY</skill>"], spotlight=True
    )
    assert SPOTLIGHT_SYSTEM_CLAUSE in prompt
    assert "BODY" in prompt
