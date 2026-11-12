"""B-302 PR 2 —— 两层的开关怎么落到一次构建上(§4.2.1、§4.2.9;T2-8、T2-10)。

* 第 1 层读取规矩:只给**主模型自己能看图**(``supports_vision``)且开关开着的构建,进系统提示词;
  不按「有没有 read_page」判 —— ``read_page`` 是所有智能体都有的基础能力。
* 第 2 层 ``figure_view_steps``:交给 ``build_react_graph``。
* 平台设置在 ``ToolEnv``(默认关 / 0);智能体的 ``policies`` 覆盖字段为空时跟平台,填了用自己的。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from expert_work.protocol import AgentSpec
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import BuiltAgent, ToolEnv, build_agent
from orchestrator import agent_factory as factory_module
from orchestrator.tools.figure_reads import READING_RULE

_KEY_NAME = "anthropic-key"

_MANIFEST: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "reader", "version": "1.0.0", "tenant": "t"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-6"},
        "system_prompt": {"template": "you help"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "none", "allowlist": []},
            "filesystem": {"readonly_root": True, "writable": []},
        },
    },
}


def _spec(*, vision: bool, **policies: Any) -> AgentSpec:
    doc = deepcopy(_MANIFEST)
    doc["spec"]["model"]["supports_vision"] = vision
    if policies:
        doc["spec"]["policies"] = policies
    return AgentSpec.model_validate(doc)


async def _key_resolver(provider: str) -> list[str]:
    del provider
    return [f"secret://{_KEY_NAME}"]


async def _build(spec: AgentSpec, env: ToolEnv) -> BuiltAgent:
    async with make_checkpointer("memory") as checkpointer:
        return await build_agent(
            spec,
            secret_store=LocalDevSecretStore.from_mapping({_KEY_NAME: "sk-ant-test"}),
            checkpointer=checkpointer,
            tool_env=env,
            provider_key_resolver=_key_resolver,
        )


# ---------------------------------------------------------------------------
# T2-8 —— 第 1 层规矩
# ---------------------------------------------------------------------------


def test_the_rule_text_is_generic_and_keeps_the_two_deliberate_phrases() -> None:
    assert "or the user corrects it" in READING_RULE
    assert "In your reply" in READING_RULE
    assert "candidate readings" in READING_RULE
    assert "number" not in READING_RULE.lower()


@pytest.mark.asyncio
async def test_a_seeing_model_with_the_switch_on_gets_the_rule() -> None:
    built = await _build(_spec(vision=True), ToolEnv(reading_rule=True))
    assert READING_RULE in built.system_prompt
    assert built.system_prompt.count(READING_RULE) == 1


@pytest.mark.parametrize(
    ("vision", "platform", "override"),
    [
        (False, True, None),  # 看不了图(含只走 ask_image 的):read_page 还在,规矩不加
        (False, True, True),
        (True, False, None),  # 开关关着(平台默认)
        (True, True, False),  # 智能体明确关掉
    ],
)
@pytest.mark.asyncio
async def test_no_rule_otherwise(vision: bool, platform: bool, override: bool | None) -> None:
    policies = {} if override is None else {"reading_rule": override}
    built = await _build(_spec(vision=vision, **policies), ToolEnv(reading_rule=platform))
    assert READING_RULE not in built.system_prompt
    assert "candidate readings" not in built.system_prompt


@pytest.mark.asyncio
async def test_an_agent_can_turn_the_rule_on_over_a_platform_off() -> None:
    built = await _build(_spec(vision=True, reading_rule=True), ToolEnv())
    assert READING_RULE in built.system_prompt


@pytest.mark.asyncio
async def test_with_everything_default_the_prompt_is_todays() -> None:
    """平台默认关、智能体不填:与没有这一层时逐字节相同(拿「显式关」那一次比)。"""
    default = await _build(_spec(vision=True), ToolEnv())
    off = await _build(_spec(vision=True, reading_rule=False), ToolEnv(reading_rule=False))
    assert default.system_prompt == off.system_prompt


# ---------------------------------------------------------------------------
# T2-10 —— 第 2 层:平台值 / 智能体覆盖 → build_react_graph
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "override", "expected"),
    [
        (0, None, 0),  # 平台默认:关
        (3, None, 3),  # 跟平台
        (3, 0, 0),  # 智能体明确关
        (0, 4, 4),  # 智能体单独打开(A/B 的 C 组)
    ],
)
@pytest.mark.asyncio
async def test_the_window_reaches_the_graph(
    monkeypatch: pytest.MonkeyPatch, platform: int, override: int | None, expected: int
) -> None:
    seen: dict[str, Any] = {}
    real = factory_module.build_react_graph

    def _capture(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(factory_module, "build_react_graph", _capture)
    policies = {} if override is None else {"figure_view_steps": override}
    await _build(_spec(vision=True, **policies), ToolEnv(figure_view_steps=platform))
    assert seen["figure_view_steps"] == expected


def test_tool_env_defaults_are_off() -> None:
    env = ToolEnv()
    assert env.reading_rule is False
    assert env.figure_view_steps == 0
