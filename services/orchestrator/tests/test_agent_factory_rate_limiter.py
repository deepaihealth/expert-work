"""波 2 线 A — ``rate_limiter_factory`` injection through the agent factory.

* ``build_llm_router`` hands every provider handle to the injected factory
  with its handle key, credential ref and the **undivided** ``rate_limit_rpm``;
* without a factory the pre-波2 path is untouched: a per-process
  ``AsyncLimiter`` at ``effective_rpm`` (PROD-12 replica division);
* ``build_step_routers`` / ``build_agent`` forward the factory.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiolimiter import AsyncLimiter

from expert_work.protocol import AgentSpec, ModelSpec
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import build_agent, build_llm_router, build_step_routers
from orchestrator.llm import RateLimitedProvider
from orchestrator.llm.rate_limit import AdmissionLimiter

_ANTHROPIC_REF = "secret://expert-work/dev/llm/anthropic"
_OPENAI_REF = "secret://expert-work/dev/llm/openai"

_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "rl-agent", "version": "1.0.0", "tenant": "platform-eng"},
    "spec": {
        "tenant_config": {},
        "model": {
            "provider": "anthropic",
            "name": "claude-sonnet-4-6",
            "rate_limit_rpm": 60,
            "fallback": [{"provider": "openai", "name": "gpt-4o", "rate_limit_rpm": 12}],
        },
        "system_prompt": {"template": "you are a test agent"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _spec() -> AgentSpec:
    return AgentSpec.model_validate(deepcopy(_SPEC))


def _secret_store() -> LocalDevSecretStore:
    return LocalDevSecretStore.from_mapping(
        {
            "expert-work/dev/llm/anthropic": "sk-ant-test",
            "expert-work/dev/llm/openai": "sk-openai-test",
        }
    )


async def _platform_resolver(provider: str) -> list[str]:
    return [{"anthropic": _ANTHROPIC_REF, "openai": _OPENAI_REF}[provider]]


@dataclass
class _SpyFactory:
    calls: list[dict[str, Any]] = field(default_factory=list)
    built: list[AsyncLimiter] = field(default_factory=list)

    def __call__(self, *, key: str, secret_ref: str, rate_limit_rpm: int) -> AdmissionLimiter:
        self.calls.append({"key": key, "secret_ref": secret_ref, "rate_limit_rpm": rate_limit_rpm})
        limiter = AsyncLimiter(max_rate=1000, time_period=60)
        self.built.append(limiter)
        return limiter


@pytest.mark.asyncio
async def test_build_llm_router_hands_each_handle_to_the_factory_undivided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXPERT_WORK_REPLICA_COUNT", "3")  # must NOT reach the factory
    spy = _SpyFactory()

    router = await build_llm_router(
        _spec().spec.model,
        secret_store=_secret_store(),
        provider_key_resolver=_platform_resolver,
        ignore_api_key_ref=True,
        rate_limiter_factory=spy,
    )

    assert spy.calls == [
        {"key": "anthropic:claude-sonnet-4-6", "secret_ref": _ANTHROPIC_REF, "rate_limit_rpm": 60},
        {"key": "openai:gpt-4o", "secret_ref": _OPENAI_REF, "rate_limit_rpm": 12},
    ]
    limiters = [h.provider.limiter for h in router.providers]  # type: ignore[attr-defined]
    assert limiters == spy.built, "each handle wraps exactly the limiter the factory returned"


@pytest.mark.asyncio
async def test_build_llm_router_without_factory_keeps_local_replica_division(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXPERT_WORK_REPLICA_COUNT", "3")

    router = await build_llm_router(
        _spec().spec.model,
        secret_store=_secret_store(),
        provider_key_resolver=_platform_resolver,
        ignore_api_key_ref=True,
    )

    for handle, expected in zip(router.providers, (20, 4), strict=True):
        assert isinstance(handle.provider, RateLimitedProvider)
        limiter = handle.provider.limiter
        assert isinstance(limiter, AsyncLimiter)
        assert limiter.max_rate == expected, "ceil(rpm / 3) — PROD-12 unchanged"
        assert limiter.time_period == 60.0


@pytest.mark.asyncio
async def test_build_step_routers_forwards_factory() -> None:
    spy = _SpyFactory()

    await build_step_routers(
        _spec(),
        secret_store=_secret_store(),
        provider_key_resolver=_platform_resolver,
        ignore_api_key_ref=True,
        rate_limiter_factory=spy,
    )

    assert [c["key"] for c in spy.calls] == ["anthropic:claude-sonnet-4-6", "openai:gpt-4o"]


@pytest.mark.asyncio
async def test_build_agent_forwards_factory() -> None:
    spy = _SpyFactory()

    async with make_checkpointer("memory") as cp:
        await build_agent(
            _spec(),
            secret_store=_secret_store(),
            checkpointer=cp,
            provider_key_resolver=_platform_resolver,
            rate_limiter_factory=spy,
        )

    assert {c["key"] for c in spy.calls} >= {"anthropic:claude-sonnet-4-6", "openai:gpt-4o"}


def test_model_spec_rate_limit_rpm_is_what_the_factory_sees() -> None:
    """Guard the assumption the wiring test above rests on."""
    assert ModelSpec.model_validate({"provider": "openai", "name": "x"}).rate_limit_rpm == 60
