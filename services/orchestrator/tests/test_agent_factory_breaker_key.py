"""B-297 — ``build_llm_router`` stamps every handle with a breaker key that
separates credentials (tenant overrides) and endpoints, without the secret.

The breaker registry is process-wide, so two tenants whose ``provider:model``
resolves to different API keys must not trip each other's breaker.
"""

from __future__ import annotations

import pytest

from expert_work.protocol import ModelSpec
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import build_llm_router

_PLATFORM_REF = "secret://expert-work/dev/llm/deepseek"
_OVERRIDE_REF = "secret://expert-work/dev/llm/deepseek-override"


def _store() -> LocalDevSecretStore:
    return LocalDevSecretStore.from_mapping(
        {
            "expert-work/dev/llm/deepseek": "sk-platform",
            "expert-work/dev/llm/deepseek-override": "sk-override",
        }
    )


def _resolver(refs: list[str]):  # type: ignore[no-untyped-def]
    async def _resolve(_provider: str) -> list[str]:
        return refs

    return _resolve


async def _keys(refs: list[str]) -> list[tuple[str, str]]:
    router = await build_llm_router(
        ModelSpec(provider="deepseek", name="deepseek-chat"),
        secret_store=_store(),
        provider_key_resolver=_resolver(refs),
    )
    return [(h.key, h.breaker_key) for h in router.providers]


@pytest.mark.asyncio
async def test_breaker_key_differs_per_credential_same_handle_key() -> None:
    [(key_a, breaker_a)] = await _keys([_PLATFORM_REF])
    [(key_b, breaker_b)] = await _keys([_OVERRIDE_REF])
    assert key_a == key_b == "deepseek:deepseek-chat"  # metrics label unchanged
    assert breaker_a.startswith("deepseek:deepseek-chat@")
    assert breaker_a != breaker_b
    for breaker in (breaker_a, breaker_b):
        assert "sk-" not in breaker
        assert "secret://" not in breaker


@pytest.mark.asyncio
async def test_breaker_key_stable_for_same_credential() -> None:
    assert await _keys([_PLATFORM_REF]) == await _keys([_PLATFORM_REF])


@pytest.mark.asyncio
async def test_sibling_keys_get_distinct_breakers() -> None:
    handles = await _keys([_PLATFORM_REF, _OVERRIDE_REF])
    assert [k for k, _ in handles] == ["deepseek:deepseek-chat#0", "deepseek:deepseek-chat#1"]
    assert handles[0][1] != handles[1][1]
