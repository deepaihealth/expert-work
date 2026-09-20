"""Tool-call-rate uplift — tool-use enforcement block wiring.

``policies.tool_use_enforcement`` decides whether the system prompt carries the
enforcement directive (call a tool for real/current facts, act now, never
fabricate). ``auto`` (default) is a DENYLIST: on for every model EXCEPT the
Claude / GPT families that already self-initiate tool calls — so a newly added
weaker model is enforced without a manifest edit.
"""

from __future__ import annotations

import pytest

from expert_work.protocol import ModelSpec
from orchestrator.agent_factory import (
    _TOOL_USE_ENFORCEMENT_BLOCK,
    _assemble_system_prompt,
    _tool_use_enforcement_active,
)


@pytest.mark.parametrize(
    "provider",
    ["qwen", "deepseek", "glm", "kimi", "doubao", "self-hosted"],
)
def test_auto_enforces_non_exempt_families(provider: str) -> None:
    model = ModelSpec(provider=provider, name=f"{provider}-model")
    assert _tool_use_enforcement_active(mode="auto", model=model) is True


@pytest.mark.parametrize("provider", ["anthropic", "openai", "azure"])
def test_auto_exempts_strong_providers(provider: str) -> None:
    model = ModelSpec(provider=provider, name=f"{provider}-model")
    assert _tool_use_enforcement_active(mode="auto", model=model) is False


@pytest.mark.parametrize("name", ["claude-3-5-sonnet", "gpt-4o-local", "my-gpt-proxy"])
def test_auto_exempts_strong_family_by_name_on_generic_provider(name: str) -> None:
    # A Claude / GPT served through a generic OpenAI-compatible / self-hosted
    # provider is still exempt — matched by the name fragment.
    model = ModelSpec(provider="self-hosted", name=name)
    assert _tool_use_enforcement_active(mode="auto", model=model) is False


def test_on_forces_enforcement_even_for_strong_model() -> None:
    model = ModelSpec(provider="anthropic", name="claude-3-5-sonnet")
    assert _tool_use_enforcement_active(mode="on", model=model) is True


def test_off_disables_even_for_weak_model() -> None:
    model = ModelSpec(provider="qwen", name="qwen3-max")
    assert _tool_use_enforcement_active(mode="off", model=model) is False


def test_assemble_includes_enforcement_block() -> None:
    prompt = _assemble_system_prompt(
        base="BASE", skill_fragments=[], tool_use_enforcement=_TOOL_USE_ENFORCEMENT_BLOCK
    )
    assert prompt.startswith("BASE")
    assert "# Tool-use enforcement" in prompt
    assert "you MUST call the relevant tool" in prompt


def test_assemble_without_enforcement_leaves_base_unchanged() -> None:
    prompt = _assemble_system_prompt(base="BASE", skill_fragments=[], tool_use_enforcement=None)
    # B-85 ③ —— 原来这里断言的是 ``prompt == base``。那只是「自己那个块不出现」
    # 的廉价代理,而平台从此恒追加一段 completion contract(无开关),代理失效。
    # 改成断言真正在乎的那件事:base 原样在最前 + 自己那个块确实不在。
    assert prompt.startswith("BASE")
    assert "Tool-use enforcement" not in prompt


def test_enforcement_and_current_date_coexist() -> None:
    prompt = _assemble_system_prompt(
        base="BASE",
        skill_fragments=[],
        tool_use_enforcement=_TOOL_USE_ENFORCEMENT_BLOCK,
        current_date="DATELINE",
    )
    assert "# Tool-use enforcement" in prompt
    assert "# Current date" in prompt
    assert "DATELINE" in prompt
