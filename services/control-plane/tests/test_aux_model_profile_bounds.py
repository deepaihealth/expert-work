"""B-267 —— the base-profile call bounds its answer: an output cap, and thinking
off where the model catalog declares a switch (reasoning tokens would run past
the cap the estimate counted). Other models are left as they are — setting
``thinking_enabled`` on them fails the build."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from control_plane.aux_model_adapter import LLMRouterAuxModelAdapter


class _Resolver:
    async def resolve_provider(self, *, tenant_id: Any, provider: Any) -> str:
        return f"secret://{provider}"


async def _spec_sent(
    monkeypatch: pytest.MonkeyPatch, provider: Any, model: str, **call: Any
) -> Any:
    seen: dict[str, Any] = {}

    async def _router(**kwargs: Any) -> AIMessage:
        return AIMessage(
            content="{}", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        )

    async def _build(spec: Any, **kwargs: Any) -> Any:
        seen["spec"] = spec
        return _router

    monkeypatch.setattr("orchestrator.build_llm_router", _build)
    aux = LLMRouterAuxModelAdapter(
        resolver=_Resolver(),  # type: ignore[arg-type]
        secret_store=object(),  # type: ignore[arg-type]
        default_provider=provider,
        default_model=model,
    )
    await aux(prompt="p", model=None, tenant_id=uuid4(), **call)
    return seen["spec"]


async def test_cap_and_thinking_off_on_a_model_with_a_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = await _spec_sent(monkeypatch, "glm", "glm-5.3-flash", max_tokens=800, thinking_off=True)
    assert (spec.max_tokens, spec.thinking_enabled) == (800, False)


async def test_a_model_without_a_switch_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = await _spec_sent(
        monkeypatch, "anthropic", "claude-haiku-4-5", max_tokens=800, thinking_off=True
    )
    assert spec.thinking_enabled is None


async def test_other_callers_are_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = await _spec_sent(monkeypatch, "glm", "glm-5.3-flash")
    assert (spec.max_tokens, spec.thinking_enabled) == (None, None)
