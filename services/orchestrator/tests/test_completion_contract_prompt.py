"""B-85 ③ 第二条腿 —— completion contract 提示词块。

A 腿(``exit_reason`` / ``completed``)只看得见「run 结束得紧挨着一批失败」这个
外在形态;模型**换了个方法绕过失败、最后交出一个其实没做成的东西**时,
``last_batch_failures`` 是空的,A 腿判 ``completed=true``。那一格只有让模型自己
说出来。做法照 openclaw 的 ``<completion_contract>``(``gpt5-prompt-overlay.ts``)。

**平台不解析模型正文里的 ``[blocked]``** —— 解析自由文本是脆的,而且一旦解析
就变成了另一种形式的猜。这条腿产出的是**给人读的诚实文本**,不是机器信号。
"""

from __future__ import annotations

from orchestrator.agent_factory import _assemble_system_prompt


def _contract_block(prompt: str) -> str:
    assert "<completion-contract>" in prompt, "平台级块,必须恒在"
    return prompt.split("<completion-contract>")[1].split("</completion-contract>")[0]


def test_contract_is_present_even_with_nothing_else_to_splice() -> None:
    """一个没有任何 skill / patch / memory 的 agent 也必须拿到这一段。

    ``_assemble_system_prompt`` 开头有一条「什么都没有就直接返回 base」的短路。
    本块是**无条件**的,不一并调整那个短路的话,最朴素的那种 agent 恰好拿不到 ——
    而平台级的东西正是不该让人去配、也不该看 agent 配置脸色的。
    """
    prompt = _assemble_system_prompt(base="你是一个助手。", skill_fragments=[])
    assert "<completion-contract>" in prompt


def test_contract_tells_the_model_to_name_what_is_blocked() -> None:
    block = _contract_block(_assemble_system_prompt(base="x", skill_fragments=[]))
    assert "[blocked]" in block
    assert "缺" in block, "要说清缺什么,不是只说一句『做不了』"


def test_contract_forbids_papering_over_a_tool_failure() -> None:
    """本条的病根就是「用一段看起来完整的话把失败盖过去」。"""
    block = _contract_block(_assemble_system_prompt(base="x", skill_fragments=[]))
    assert "工具" in block


def test_base_prompt_is_still_first() -> None:
    """平台块只能**追加**,不能挤掉或改写 agent 自己的提示词。"""
    prompt = _assemble_system_prompt(base="ONLY-MINE", skill_fragments=[])
    assert prompt.startswith("ONLY-MINE")
