"""Unit tests for the agent factory — manifest → runnable agent."""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

import pytest
from langgraph.graph.state import CompiledStateGraph

from expert_work.persistence import InMemoryKnowledgeStore, InMemoryMemoryStore
from expert_work.protocol import AgentSpec, BuiltinToolSpec, ModelSpec, StructuredOutputSpec
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.middleware import RecordingLangfuseClient
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import (
    AgentFactoryError,
    AnthropicProvider,
    BuiltAgent,
    LLMRouter,
    MemoryEnv,
    MiddlewareEnv,
    OpenAIProvider,
    ToolEnv,
    build_agent,
    build_llm_router,
    build_step_routers,
)
from orchestrator.agent_factory import _build_provider, _chat_stream_deadline_s
from orchestrator.llm import FakeEmbedder, RateLimitedProvider
from orchestrator.llm.providers.openai_compatible import OpenAICompatibleProvider
from orchestrator.tools import KnowledgeRetriever, RecordingTavilyClient
from orchestrator.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ANTHROPIC_KEY_NAME = "expert-work/dev/llm/anthropic"
_OPENAI_KEY_NAME = "expert-work/dev/llm/openai"
_KIMI_KEY_NAME = "expert-work/dev/llm/kimi"

_MINIMAL_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "test-agent", "version": "1.0.0", "tenant": "platform-eng"},
    "spec": {
        "tenant_config": {},
        "model": {
            "provider": "anthropic",
            "name": "claude-sonnet-4-6",
            "api_key_ref": f"secret://{_ANTHROPIC_KEY_NAME}",
        },
        "system_prompt": {"template": "you are a test agent"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _spec(**model_overrides: Any) -> AgentSpec:
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["model"].update(model_overrides)
    return AgentSpec.model_validate(doc)


def _secret_store() -> LocalDevSecretStore:
    return LocalDevSecretStore.from_mapping(
        {
            _ANTHROPIC_KEY_NAME: "sk-ant-test",
            _OPENAI_KEY_NAME: "sk-openai-test",
            _KIMI_KEY_NAME: "sk-kimi-test",
        }
    )


# Stream Y-2 — agent builds resolve every manifest model's key through the
# platform resolver (manifest-pinned ``api_key_ref`` is ignored). The dev
# secret store seeds a key per provider, so map each provider to its ref.
_PROVIDER_KEY_NAMES = {
    "anthropic": _ANTHROPIC_KEY_NAME,
    "openai": _OPENAI_KEY_NAME,
    "kimi": _KIMI_KEY_NAME,
    "self-hosted": _OPENAI_KEY_NAME,
    "azure": _OPENAI_KEY_NAME,
    "qwen": _OPENAI_KEY_NAME,
}


async def _platform_resolver(provider: str) -> list[str]:
    return [f"secret://{_PROVIDER_KEY_NAMES[provider]}"]


async def _build(spec: AgentSpec, **kwargs: Any) -> BuiltAgent:
    """``build_agent`` with the platform key resolver defaulted (Stream Y-2).

    Agent builds ignore manifest ``api_key_ref`` and require a
    ``provider_key_resolver``; tests that don't care about credential
    plumbing go through this wrapper so the resolver is supplied once.
    """
    kwargs.setdefault("provider_key_resolver", _platform_resolver)
    return await build_agent(spec, **kwargs)


# ---------------------------------------------------------------------------
# build_llm_router
# ---------------------------------------------------------------------------


def test_chat_stream_deadline_floors_low_values() -> None:
    # Existing agents baked the old 90s default into their stored spec — the
    # build-time floor bumps them to 180 so a heavy step doesn't get killed
    # mid-stream and drop the run into a retry loop.
    assert _chat_stream_deadline_s(90) == 180.0
    assert _chat_stream_deadline_s(120) == 180.0
    # An explicit higher value is respected; 0 keeps the deadline disabled.
    assert _chat_stream_deadline_s(240) == 240.0
    assert _chat_stream_deadline_s(0) is None


def test_chat_idle_timeout_default_and_off() -> None:
    from orchestrator.agent_factory import _chat_idle_timeout_s

    assert _chat_idle_timeout_s(45) == 45.0
    assert _chat_idle_timeout_s(0) is None  # 0 disables the idle timer
    assert _chat_idle_timeout_s(30) == 30.0


@pytest.mark.asyncio
async def test_build_llm_router_single_anthropic_provider() -> None:
    router = await build_llm_router(_spec().spec.model, secret_store=_secret_store())
    assert isinstance(router, LLMRouter)
    assert len(router.providers) == 1
    handle = router.providers[0]
    assert handle.key == "anthropic:claude-sonnet-4-6"
    # The provider is rate-limited (E.12) wrapping the real adapter.
    assert isinstance(handle.provider, RateLimitedProvider)
    assert isinstance(handle.provider.inner, AnthropicProvider)


@pytest.mark.asyncio
async def test_build_llm_router_resolves_key_from_secret_store() -> None:
    """The api_key_ref is resolved through the SecretStore — the
    resolved value reaches the provider's HTTP client."""
    router = await build_llm_router(_spec().spec.model, secret_store=_secret_store())
    inner = router.providers[0].provider.inner
    assert isinstance(inner, AnthropicProvider)
    # HTTPAnthropicClient holds the resolved key.
    assert inner.client.api_key == "sk-ant-test"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_build_llm_router_flattens_fallback_chain_preorder() -> None:
    model = _spec(
        fallback=[
            {
                "provider": "openai",
                "name": "gpt-4o",
                "api_key_ref": f"secret://{_OPENAI_KEY_NAME}",
            },
            {
                "provider": "kimi",
                "name": "moonshot-v1-128k",
                "api_key_ref": f"secret://{_KIMI_KEY_NAME}",
            },
        ]
    ).spec.model
    router = await build_llm_router(model, secret_store=_secret_store())
    assert [h.key for h in router.providers] == [
        "anthropic:claude-sonnet-4-6",
        "openai:gpt-4o",
        "kimi:moonshot-v1-128k",
    ]


@pytest.mark.asyncio
async def test_build_llm_router_nested_fallback_preorder() -> None:
    """A fallback that itself has a fallback flattens depth-first."""
    model = _spec(
        fallback=[
            {
                "provider": "openai",
                "name": "gpt-4o",
                "api_key_ref": f"secret://{_OPENAI_KEY_NAME}",
                "fallback": [
                    {
                        "provider": "kimi",
                        "name": "moonshot-v1-128k",
                        "api_key_ref": f"secret://{_KIMI_KEY_NAME}",
                    }
                ],
            },
        ]
    ).spec.model
    router = await build_llm_router(model, secret_store=_secret_store())
    assert [h.key for h in router.providers] == [
        "anthropic:claude-sonnet-4-6",
        "openai:gpt-4o",
        "kimi:moonshot-v1-128k",
    ]


@pytest.mark.asyncio
async def test_build_llm_router_extra_fallbacks_appended_after_chain() -> None:
    """Mini-ADR J-33 (J.6.补强-4) — ``extra_fallbacks`` lands after the
    primary's own ``.fallback`` chain so priority is
    ``primary → primary.fallback... → extra_fallbacks[0]...``."""
    primary = _spec(
        fallback=[
            {
                "provider": "openai",
                "name": "gpt-4o",
                "api_key_ref": f"secret://{_OPENAI_KEY_NAME}",
            },
        ],
    ).spec.model
    extra = _spec(
        provider="kimi",
        name="moonshot-v1-128k",
        api_key_ref=f"secret://{_KIMI_KEY_NAME}",
    ).spec.model
    router = await build_llm_router(
        primary,
        secret_store=_secret_store(),
        extra_fallbacks=[extra],
    )
    assert [h.key for h in router.providers] == [
        "anthropic:claude-sonnet-4-6",
        "openai:gpt-4o",
        "kimi:moonshot-v1-128k",
    ]


@pytest.mark.asyncio
async def test_build_llm_router_rate_limit_from_model_spec() -> None:
    model = _spec(rate_limit_rpm=3).spec.model
    router = await build_llm_router(model, secret_store=_secret_store())
    limiter = router.providers[0].provider.limiter  # type: ignore[union-attr]
    assert limiter.max_rate == 3


@pytest.mark.asyncio
async def test_build_llm_router_temperature_from_model_spec() -> None:
    """ModelSpec.temperature is plumbed onto the provider adapter (F-5)."""
    model = _spec(temperature=0.9).spec.model
    router = await build_llm_router(model, secret_store=_secret_store())
    inner = router.providers[0].provider.inner  # type: ignore[union-attr]
    assert isinstance(inner, AnthropicProvider)
    assert inner.temperature == 0.9


@pytest.mark.asyncio
async def test_build_llm_router_missing_api_key_ref_raises() -> None:
    """Direct ``build_llm_router`` (default ``ignore_api_key_ref=False``) with
    neither a manifest ref nor a resolver → the no-platform-credential error."""
    model = ModelSpec.model_validate({"provider": "anthropic", "name": "claude"})
    with pytest.raises(AgentFactoryError, match="no platform credential"):
        await build_llm_router(model, secret_store=_secret_store())


@pytest.mark.asyncio
async def test_build_llm_router_uses_provider_key_resolver_when_no_ref() -> None:
    """Stream Q (Q-5) — no manifest api_key_ref → the platform resolver supplies
    the ref, which then resolves through the SecretStore."""
    model = ModelSpec.model_validate({"provider": "anthropic", "name": "claude"})
    seen: list[str] = []

    async def resolver(provider: str) -> list[str]:
        seen.append(provider)
        return [f"secret://{_ANTHROPIC_KEY_NAME}"]

    router = await build_llm_router(
        model, secret_store=_secret_store(), provider_key_resolver=resolver
    )
    assert seen == ["anthropic"]
    assert router is not None


@pytest.mark.asyncio
async def test_build_llm_router_expands_multikey_into_sibling_handles() -> None:
    """Stream Y-MK — a resolver returning N keys yields N sibling
    ``ProviderHandle``s sharing one ``group``, with ``#idx`` breaker keys."""
    model = ModelSpec.model_validate({"provider": "anthropic", "name": "claude"})

    async def resolver(provider: str) -> list[str]:
        return [f"secret://{_ANTHROPIC_KEY_NAME}", f"secret://{_OPENAI_KEY_NAME}"]

    router = await build_llm_router(
        model, secret_store=_secret_store(), provider_key_resolver=resolver
    )
    handles = list(router.providers)
    assert len(handles) == 2
    assert [h.key for h in handles] == ["anthropic:claude#0", "anthropic:claude#1"]
    assert {h.group for h in handles} == {"anthropic:claude"}


@pytest.mark.asyncio
async def test_build_llm_router_empty_keylist_raises() -> None:
    """A resolver returning no keys is a configuration error, not an empty router."""
    model = ModelSpec.model_validate({"provider": "anthropic", "name": "claude"})

    async def resolver(provider: str) -> list[str]:
        return []

    with pytest.raises(AgentFactoryError, match="resolved no platform credential"):
        await build_llm_router(model, secret_store=_secret_store(), provider_key_resolver=resolver)


@pytest.mark.asyncio
async def test_build_llm_router_manifest_ref_wins_over_resolver() -> None:
    """Stream Q (Q-5) — manifest api_key_ref is the explicit override; the
    platform resolver is not consulted when a ref is present."""
    called = False

    async def resolver(provider: str) -> list[str]:
        nonlocal called
        called = True
        return ["secret://unused"]

    router = await build_llm_router(
        _spec().spec.model, secret_store=_secret_store(), provider_key_resolver=resolver
    )
    assert called is False
    assert router is not None


@pytest.mark.asyncio
async def test_build_llm_router_missing_secret_propagates() -> None:
    """A ref pointing at a non-existent secret surfaces the
    SecretStore's error, not a silent empty key."""
    model = _spec(api_key_ref="secret://does/not/exist").spec.model
    with pytest.raises(KeyError):
        await build_llm_router(model, secret_store=_secret_store())


@pytest.mark.asyncio
async def test_build_llm_router_openai_compatible_vendor() -> None:
    model = _spec(
        provider="kimi",
        name="moonshot-v1-128k",
        api_key_ref=f"secret://{_KIMI_KEY_NAME}",
    ).spec.model
    router = await build_llm_router(model, secret_store=_secret_store())
    inner = router.providers[0].provider.inner  # type: ignore[union-attr]
    # Domestic vendors reuse OpenAIProvider (E.11.5).
    assert isinstance(inner, OpenAIProvider)


@pytest.mark.asyncio
async def test_build_llm_router_self_hosted_provider() -> None:
    """self-hosted reuses OpenAIProvider over a custom base_url (F-4)."""
    model = _spec(
        provider="self-hosted",
        name="llama-3.1-70b",
        api_key_ref=f"secret://{_OPENAI_KEY_NAME}",
        base_url="http://vllm.internal:8000",
    ).spec.model
    router = await build_llm_router(model, secret_store=_secret_store())
    inner = router.providers[0].provider.inner  # type: ignore[union-attr]
    assert isinstance(inner, OpenAIProvider)


@pytest.mark.asyncio
async def test_build_llm_router_self_hosted_missing_base_url_raises() -> None:
    model = _spec(
        provider="self-hosted",
        name="llama-3.1-70b",
        api_key_ref=f"secret://{_OPENAI_KEY_NAME}",
    ).spec.model
    with pytest.raises(AgentFactoryError, match="base_url"):
        await build_llm_router(model, secret_store=_secret_store())


@pytest.mark.asyncio
async def test_build_llm_router_azure_provider() -> None:
    """azure reuses OpenAIProvider over the deployment-style URL (F-4)."""
    model = _spec(
        provider="azure",
        name="gpt-4o",
        api_key_ref=f"secret://{_OPENAI_KEY_NAME}",
        base_url="https://res.openai.azure.com",
        azure_deployment="gpt-4o-deploy",
        azure_api_version="2024-10-21",
    ).spec.model
    router = await build_llm_router(model, secret_store=_secret_store())
    inner = router.providers[0].provider.inner  # type: ignore[union-attr]
    assert isinstance(inner, OpenAIProvider)


@pytest.mark.asyncio
async def test_build_llm_router_azure_missing_config_raises() -> None:
    model = _spec(
        provider="azure",
        name="gpt-4o",
        api_key_ref=f"secret://{_OPENAI_KEY_NAME}",
        base_url="https://res.openai.azure.com",
    ).spec.model  # azure_deployment / azure_api_version omitted
    with pytest.raises(AgentFactoryError, match="azure_deployment"):
        await build_llm_router(model, secret_store=_secret_store())


# ---------------------------------------------------------------------------
# build_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_agent_returns_built_agent() -> None:
    async with make_checkpointer("memory") as cp:
        built = await _build(_spec(), secret_store=_secret_store(), checkpointer=cp)
    assert isinstance(built, BuiltAgent)
    assert isinstance(built.graph, CompiledStateGraph)
    # PI-1: spotlighting is on by default, so the base template is the prefix
    # and the untrusted-content clause is appended.
    assert built.system_prompt.startswith("you are a test agent")
    assert "## Untrusted content" in built.system_prompt
    # Default WorkflowSpec.max_iterations.
    assert built.max_steps == 30


@pytest.mark.asyncio
async def test_build_agent_max_steps_from_workflow() -> None:
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["workflow"] = {"type": "react", "max_iterations": 5}
    spec = AgentSpec.model_validate(doc)
    async with make_checkpointer("memory") as cp:
        built = await _build(spec, secret_store=_secret_store(), checkpointer=cp)
    assert built.max_steps == 5


@pytest.mark.asyncio
async def test_build_agent_token_budget_defaults_zero() -> None:
    """B3 — ``policies.token_budget`` default 0 ⇒ BuiltAgent carries no
    breaker (opt-in, zero behaviour change for existing manifests)."""
    async with make_checkpointer("memory") as cp:
        built = await _build(_spec(), secret_store=_secret_store(), checkpointer=cp)
    assert built.token_budget == 0


@pytest.mark.asyncio
async def test_build_agent_token_budget_propagates_from_policies() -> None:
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["policies"] = {"token_budget": 123_456}
    spec = AgentSpec.model_validate(doc)
    async with make_checkpointer("memory") as cp:
        built = await _build(spec, secret_store=_secret_store(), checkpointer=cp)
    assert built.token_budget == 123_456


@pytest.mark.asyncio
async def test_build_agent_supports_vision_defaults_to_false() -> None:
    """``ModelSpec.supports_vision`` default → ``BuiltAgent.supports_vision`` False."""
    async with make_checkpointer("memory") as cp:
        built = await _build(_spec(), secret_store=_secret_store(), checkpointer=cp)
    assert built.supports_vision is False


@pytest.mark.asyncio
async def test_build_agent_supports_vision_propagates_from_manifest() -> None:
    """Stream J.6 — the manifest's ``model.supports_vision`` reaches BuiltAgent."""
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["model"]["supports_vision"] = True
    spec = AgentSpec.model_validate(doc)
    async with make_checkpointer("memory") as cp:
        built = await _build(spec, secret_store=_secret_store(), checkpointer=cp)
    assert built.supports_vision is True


@pytest.mark.asyncio
async def test_build_agent_ignores_vision_block_on_visual_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stream J.6 — Path A (native content blocks) and Path B (ask_image) are
    mutually exclusive. A ``vision:`` block on a natively-multimodal manifest is
    IGNORED (Path A wins) with a warning, rather than failing the build: the
    block is commonly a leftover after the editor flips supports_vision on when a
    vision-capable model is selected. The qwen VL model in the ignored block is
    never routed (so ``_secret_store()`` needs no qwen credential)."""
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["model"]["supports_vision"] = True
    doc["spec"]["vision"] = {"model": {"provider": "qwen", "name": "qwen-vl-max"}}
    spec = AgentSpec.model_validate(doc)
    async with make_checkpointer("memory") as cp:
        with caplog.at_level(logging.WARNING, logger="expert_work.orchestrator.agent_factory"):
            built = await _build(spec, secret_store=_secret_store(), checkpointer=cp)
    # Build succeeds and Path A stays active (no raise on the leftover Path B).
    assert built.supports_vision is True
    # The dropped VL config is warned, not silently discarded.
    assert "vision_block_ignored" in caplog.text


class _StubChildBuilder:
    """Conforms to ``ChildAgentBuilder``; never invoked by ``build_agent``
    (assembly only *registers* SubAgentTools)."""

    async def __call__(
        self,
        *,
        tenant_id: Any,
        name: str,
        version: str,
        depth: int,
        oauth_user_id: str | None = None,
    ) -> Any:
        raise AssertionError("child builder must not be called during build_agent")


def _subagent_spec() -> AgentSpec:
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["subagents"] = [
        {"name": "researcher", "agent_ref": "deep-researcher@1.0.0", "description": "research"}
    ]
    return AgentSpec.model_validate(doc)


@pytest.mark.asyncio
async def test_build_agent_with_subagents_succeeds() -> None:
    env = ToolEnv(child_agent_builder=_StubChildBuilder())
    async with make_checkpointer("memory") as cp:
        built = await _build(
            _subagent_spec(), secret_store=_secret_store(), checkpointer=cp, tool_env=env
        )
    assert isinstance(built, BuiltAgent)


@pytest.mark.asyncio
async def test_build_agent_subagents_without_builder_raises() -> None:
    async with make_checkpointer("memory") as cp:
        with pytest.raises(AgentFactoryError, match="sub-agent builder"):
            await _build(_subagent_spec(), secret_store=_secret_store(), checkpointer=cp)


def _knowledge_spec() -> AgentSpec:
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["knowledge"] = {"knowledge_base_refs": ["hr-policies"]}
    return AgentSpec.model_validate(doc)


@pytest.mark.asyncio
async def test_build_agent_with_knowledge_succeeds() -> None:
    env = ToolEnv(
        knowledge_retriever=KnowledgeRetriever(
            store=InMemoryKnowledgeStore(), embedder=FakeEmbedder()
        )
    )
    async with make_checkpointer("memory") as cp:
        built = await _build(
            _knowledge_spec(), secret_store=_secret_store(), checkpointer=cp, tool_env=env
        )
    assert isinstance(built, BuiltAgent)


@pytest.mark.asyncio
async def test_build_agent_knowledge_without_retriever_raises() -> None:
    async with make_checkpointer("memory") as cp:
        with pytest.raises(AgentFactoryError, match="knowledge retriever"):
            await _build(_knowledge_spec(), secret_store=_secret_store(), checkpointer=cp)


@pytest.mark.asyncio
async def test_build_agent_react_has_no_planner_node() -> None:
    """The default ``react`` workflow builds a graph without a planner."""
    async with make_checkpointer("memory") as cp:
        built = await _build(_spec(), secret_store=_secret_store(), checkpointer=cp)
    assert "planner" not in built.graph.nodes


@pytest.mark.asyncio
async def test_build_agent_plan_execute_adds_planner_node() -> None:
    """A ``plan_execute`` manifest front-loads a ``planner`` node (J.1)."""
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["workflow"] = {"type": "plan_execute"}
    spec = AgentSpec.model_validate(doc)
    async with make_checkpointer("memory") as cp:
        built = await _build(spec, secret_store=_secret_store(), checkpointer=cp)
    assert "planner" in built.graph.nodes
    assert "agent" in built.graph.nodes


def _spy_on_register(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch ``ToolRegistry.register`` to record registered tool names."""
    registered: list[str] = []
    original = ToolRegistry.register

    def _spy(self: ToolRegistry, tool: Any, *args: Any, **kwargs: Any) -> None:
        registered.append(tool.spec.name)
        return original(self, tool, *args, **kwargs)

    monkeypatch.setattr(ToolRegistry, "register", _spy)
    return registered


@pytest.mark.asyncio
async def test_build_agent_react_registers_update_plan_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P3 — update_plan is default-registered for every workflow, so a
    react agent can self-plan (previously gated to plan_execute)."""
    registered = _spy_on_register(monkeypatch)
    async with make_checkpointer("memory") as cp:
        await _build(_spec(), secret_store=_secret_store(), checkpointer=cp)
    assert "update_plan" in registered


@pytest.mark.asyncio
async def test_build_agent_plan_execute_still_registers_update_plan_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: plan_execute keeps update_plan (now via the same
    unconditional registration path)."""
    registered = _spy_on_register(monkeypatch)
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["workflow"] = {"type": "plan_execute"}
    spec = AgentSpec.model_validate(doc)
    async with make_checkpointer("memory") as cp:
        await _build(spec, secret_store=_secret_store(), checkpointer=cp)
    assert "update_plan" in registered


# ---------------------------------------------------------------------------
# build_agent — manage_task builtin mount (Spec 1 PR2 Task 4)
# ---------------------------------------------------------------------------


def _spec_with_manage_task() -> AgentSpec:
    """A minimal spec whose manifest declares the manage_task builtin."""
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["tools"] = [BuiltinToolSpec(name="manage_task")]
    return AgentSpec.model_validate(doc)


class _FakeTriggerStore:
    async def create(self, record: Any) -> Any:  # pragma: no cover - unused in build test
        return record


@pytest.mark.asyncio
async def test_build_agent_registers_manage_task_when_declared_and_wired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered = _spy_on_register(monkeypatch)
    async with make_checkpointer("memory") as cp:
        await _build(
            _spec_with_manage_task(),
            secret_store=_secret_store(),
            checkpointer=cp,
            trigger_store=_FakeTriggerStore(),
        )
    assert "manage_task" in registered


@pytest.mark.asyncio
async def test_build_agent_manage_task_declared_without_store_raises() -> None:
    async with make_checkpointer("memory") as cp:
        with pytest.raises(AgentFactoryError, match="manage_task"):
            await _build(
                _spec_with_manage_task(),
                secret_store=_secret_store(),
                checkpointer=cp,
                trigger_store=None,
            )


@pytest.mark.asyncio
async def test_build_agent_no_manage_task_when_not_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registered = _spy_on_register(monkeypatch)
    async with make_checkpointer("memory") as cp:
        await _build(_spec(), secret_store=_secret_store(), checkpointer=cp)
    assert "manage_task" not in registered


@pytest.mark.asyncio
async def test_build_agent_no_reflection_has_no_reflect_node() -> None:
    """Without a ``reflection:`` block the graph has no reflect node."""
    async with make_checkpointer("memory") as cp:
        built = await _build(_spec(), secret_store=_secret_store(), checkpointer=cp)
    assert "reflect" not in built.graph.nodes


@pytest.mark.asyncio
async def test_build_agent_reflection_block_adds_reflect_node() -> None:
    """A ``reflection:`` block inserts a self-critique reflect node (J.2)."""
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["reflection"] = {"budget": 3}
    spec = AgentSpec.model_validate(doc)
    async with make_checkpointer("memory") as cp:
        built = await _build(spec, secret_store=_secret_store(), checkpointer=cp)
    assert "reflect" in built.graph.nodes


# ---------------------------------------------------------------------------
# build_step_routers — Stream J.11 model routing
# ---------------------------------------------------------------------------


def _routing_spec(when: str, *, provider: str, name: str, key_name: str) -> AgentSpec:
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["routing"] = {
        "rules": [
            {
                "when": when,
                "model": {
                    "provider": provider,
                    "name": name,
                    "api_key_ref": f"secret://{key_name}",
                },
            }
        ]
    }
    return AgentSpec.model_validate(doc)


@pytest.mark.asyncio
async def test_build_step_routers_default_when_no_routing() -> None:
    """Without a routing block every step class reuses the default router."""
    routers = await build_step_routers(_spec(), secret_store=_secret_store())
    assert routers.planning is routers.default
    assert routers.reflection is routers.default


@pytest.mark.asyncio
async def test_build_step_routers_routes_planning_to_its_model() -> None:
    spec = _routing_spec("planning", provider="openai", name="gpt-4o", key_name=_OPENAI_KEY_NAME)
    routers = await build_step_routers(spec, secret_store=_secret_store())
    # planning routes to its own model; default + reflection stay on the
    # agent's top-level model.
    assert routers.planning.providers[0].key == "openai:gpt-4o"
    assert routers.default.providers[0].key == "anthropic:claude-sonnet-4-6"
    assert routers.reflection is routers.default


@pytest.mark.asyncio
async def test_build_step_routers_routes_reflection_independently() -> None:
    spec = _routing_spec(
        "reflection", provider="openai", name="gpt-4o-mini", key_name=_OPENAI_KEY_NAME
    )
    routers = await build_step_routers(spec, secret_store=_secret_store())
    assert routers.reflection.providers[0].key == "openai:gpt-4o-mini"
    assert routers.planning is routers.default


@pytest.mark.asyncio
async def test_build_agent_with_routing_block_builds() -> None:
    """build_agent assembles cleanly when a routing block is present."""
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["workflow"] = {"type": "plan_execute"}
    doc["spec"]["routing"] = {
        "rules": [
            {
                "when": "planning",
                "model": {
                    "provider": "openai",
                    "name": "gpt-4o",
                    "api_key_ref": f"secret://{_OPENAI_KEY_NAME}",
                },
            }
        ]
    }
    spec = AgentSpec.model_validate(doc)
    async with make_checkpointer("memory") as cp:
        built = await _build(spec, secret_store=_secret_store(), checkpointer=cp)
    assert "planner" in built.graph.nodes


# ---------------------------------------------------------------------------
# build_agent — long-term memory (Stream J.3)
# ---------------------------------------------------------------------------


def _memory_env() -> MemoryEnv:
    return MemoryEnv(store=InMemoryMemoryStore(), embedder=FakeEmbedder(dim=16))


@pytest.mark.asyncio
async def test_build_agent_no_memory_has_no_memory_nodes() -> None:
    async with make_checkpointer("memory") as cp:
        built = await _build(_spec(), secret_store=_secret_store(), checkpointer=cp)
    assert "memory_recall" not in built.graph.nodes
    assert "memory_writeback" not in built.graph.nodes


@pytest.mark.asyncio
async def test_build_agent_long_term_memory_adds_both_nodes() -> None:
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["memory"] = {"long_term": {}}
    spec = AgentSpec.model_validate(doc)
    async with make_checkpointer("memory") as cp:
        built = await _build(
            spec, secret_store=_secret_store(), checkpointer=cp, memory_env=_memory_env()
        )
    assert "memory_recall" in built.graph.nodes
    assert "memory_writeback" in built.graph.nodes


@pytest.mark.asyncio
async def test_build_agent_floors_head_keep_when_per_session_memory(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RT-2 PR-4 (§8.4) — per_session recall + ``head_keep=0`` → the factory
    floors ``head_keep`` to 1 (with a warning) so the ``messages[1]``
    cache-anchor block survives compression."""
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["memory"] = {"long_term": {}}  # recall_mode defaults per_session
    doc["spec"]["policies"] = {"context_compression": {"head_keep": 0}}
    spec = AgentSpec.model_validate(doc)
    with caplog.at_level(logging.WARNING, logger="expert_work.orchestrator.agent_factory"):
        async with make_checkpointer("memory") as cp:
            await _build(
                spec, secret_store=_secret_store(), checkpointer=cp, memory_env=_memory_env()
            )
    assert "head_keep_floored" in caplog.text
    assert "floored=1" in caplog.text


@pytest.mark.asyncio
async def test_build_agent_keeps_head_keep_zero_without_per_session_memory(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``head_keep=0`` with no per_session recall is a valid config — not
    floored, no warning (no anchor present to protect)."""
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["policies"] = {"context_compression": {"head_keep": 0}}
    spec = AgentSpec.model_validate(doc)
    with caplog.at_level(logging.WARNING, logger="expert_work.orchestrator.agent_factory"):
        async with make_checkpointer("memory") as cp:
            await _build(spec, secret_store=_secret_store(), checkpointer=cp)
    assert "head_keep_floored" not in caplog.text


@pytest.mark.asyncio
async def test_build_agent_long_term_memory_write_back_off() -> None:
    """``write_back: false`` → recall node only, no write-back node."""
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["memory"] = {"long_term": {"write_back": False}}
    spec = AgentSpec.model_validate(doc)
    async with make_checkpointer("memory") as cp:
        built = await _build(
            spec, secret_store=_secret_store(), checkpointer=cp, memory_env=_memory_env()
        )
    assert "memory_recall" in built.graph.nodes
    assert "memory_writeback" not in built.graph.nodes


@pytest.mark.asyncio
async def test_build_agent_long_term_memory_without_env_raises() -> None:
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["memory"] = {"long_term": {}}
    spec = AgentSpec.model_validate(doc)
    async with make_checkpointer("memory") as cp:
        with pytest.raises(AgentFactoryError, match=r"memory\.long_term"):
            await _build(spec, secret_store=_secret_store(), checkpointer=cp)


@pytest.mark.asyncio
async def test_build_memory_nodes_passes_reranker_to_recall() -> None:
    """Stream CM-4 — ``MemoryEnv.reranker`` reaches the recall node, so a
    wired reranker actually reorders long-term memory recall."""
    from collections.abc import Sequence
    from uuid import UUID, uuid4

    from langchain_core.messages import HumanMessage

    from expert_work.protocol import MemoryItem
    from orchestrator.agent_factory import _build_memory_nodes

    tenant, user = uuid4(), uuid4()
    store = InMemoryMemoryStore()
    embedder = FakeEmbedder(dim=16)
    [vec] = await embedder.embed(["seed"], tenant_id=tenant)
    await store.write(
        [
            MemoryItem(
                id=uuid4(),
                tenant_id=tenant,
                user_id=user,
                kind="fact",
                content="seeded",
                embedding=vec,
            )
        ]
    )

    class _SpyReranker:
        def __init__(self) -> None:
            self.calls = 0

        async def rerank(
            self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
        ) -> list[int]:
            del query, tenant_id
            self.calls += 1
            return list(range(len(documents)))[:top_k]

    async def _dummy_llm(*, messages: object, tools: object) -> object:  # never called by recall
        raise AssertionError("recall must not call the LLM")

    spy = _SpyReranker()
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["memory"] = {"long_term": {}}
    spec = AgentSpec.model_validate(doc)

    recall, _writeback, _flush = _build_memory_nodes(
        spec,
        memory_env=MemoryEnv(store=store, embedder=embedder, reranker=spy),  # type: ignore[arg-type]
        llm_caller=_dummy_llm,  # type: ignore[arg-type]
    )
    assert recall is not None
    await recall(
        {"messages": [HumanMessage(content="q")], "step_count": 0, "max_steps": 5},  # type: ignore[arg-type]
        {"configurable": {"tenant_id": str(tenant), "user_id": str(user)}},
    )
    assert spy.calls == 1  # the reranker was invoked → passthrough wired


@pytest.mark.asyncio
async def test_build_memory_nodes_passes_rewrite_reads_to_recall() -> None:
    """P5a assembly — ``long_term.rewrite_reads`` + the agent's chat model reach
    the recall node as ``rewrite_query`` + ``rewriter``, so an enabled manifest
    actually rewrites the query before recall."""
    from collections.abc import Sequence
    from uuid import UUID, uuid4

    from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

    from expert_work.protocol import MemoryItem
    from orchestrator.agent_factory import _build_memory_nodes

    tenant, user = uuid4(), uuid4()

    class _FixedEmbedder:
        async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
            del tenant_id
            return [(1.0, 0.0, 0.0) for _ in texts]

    class _NoHitStore:
        def __init__(self) -> None:
            self.query_texts: list[object] = []

        async def retrieve(self, **kwargs: object) -> list[MemoryItem]:
            self.query_texts.append(kwargs.get("query_text"))
            return []

        async def bump_access(self, **kwargs: object) -> None:
            return None

    class _SpyLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(
            self, *, messages: Sequence[BaseMessage], tools: Sequence[object]
        ) -> AIMessage:
            del messages, tools
            self.calls += 1
            return AIMessage(content="rewritten query")

    store = _NoHitStore()
    llm = _SpyLLM()
    doc = deepcopy(_MINIMAL_SPEC)
    # verify_reads off so the only LLM call under test is the rewriter.
    doc["spec"]["memory"] = {"long_term": {"rewrite_reads": True, "verify_reads": False}}
    spec = AgentSpec.model_validate(doc)

    recall, _writeback, _flush = _build_memory_nodes(
        spec,
        memory_env=MemoryEnv(store=store, embedder=_FixedEmbedder()),  # type: ignore[arg-type]
        llm_caller=llm,  # type: ignore[arg-type]
    )
    assert recall is not None
    await recall(
        {"messages": [HumanMessage(content="q")], "step_count": 0, "max_steps": 5},  # type: ignore[arg-type]
        {"configurable": {"tenant_id": str(tenant), "user_id": str(user)}},
    )
    assert llm.calls == 1  # rewriter invoked → rewrite_reads + rewriter wired from spec
    # Default recall mode is hybrid → the rewritten text reaches query_text too.
    assert store.query_texts == ["rewritten query"]


@pytest.mark.asyncio
async def test_build_memory_nodes_passes_abstain_threshold_to_recall() -> None:
    """P5a assembly — ``long_term.abstain_threshold`` reaches the recall node, so
    a manifest that sets it actually abstains on a weak match."""
    from collections.abc import Sequence
    from uuid import UUID, uuid4

    from langchain_core.messages import HumanMessage

    from expert_work.protocol import MemoryItem
    from orchestrator.agent_factory import _build_memory_nodes

    tenant, user = uuid4(), uuid4()

    class _FixedEmbedder:
        async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
            del tenant_id
            return [(1.0, 0.0, 0.0) for _ in texts]

    # Orthogonal to the query embedding → cosine 0.0, below the 0.5 threshold.
    far = MemoryItem(
        id=uuid4(),
        tenant_id=tenant,
        user_id=user,
        kind="fact",
        content="far",
        embedding=(0.0, 1.0, 0.0),
    )

    class _OneHitStore:
        def __init__(self) -> None:
            self.retrieve_calls = 0

        async def retrieve(self, **kwargs: object) -> list[MemoryItem]:
            del kwargs
            self.retrieve_calls += 1
            return [far]

        async def bump_access(self, **kwargs: object) -> None:
            return None

    async def _no_llm(*, messages: object, tools: object) -> object:
        raise AssertionError("no rewrite / verify enabled — LLM must not be called")

    store = _OneHitStore()
    doc = deepcopy(_MINIMAL_SPEC)
    # verify_reads off so an empty result can only come from the abstain gate,
    # never from the fail-open verify path swallowing an LLM error.
    doc["spec"]["memory"] = {"long_term": {"abstain_threshold": 0.5, "verify_reads": False}}
    spec = AgentSpec.model_validate(doc)

    recall, _writeback, _flush = _build_memory_nodes(
        spec,
        memory_env=MemoryEnv(store=store, embedder=_FixedEmbedder()),  # type: ignore[arg-type]
        llm_caller=_no_llm,  # type: ignore[arg-type]
    )
    assert recall is not None
    out = await recall(
        {"messages": [HumanMessage(content="q")], "step_count": 0, "max_steps": 5},  # type: ignore[arg-type]
        {"configurable": {"tenant_id": str(tenant), "user_id": str(user)}},
    )
    assert store.retrieve_calls == 1  # retrieve ran and returned the weak match…
    assert out == {}  # …and it was abstained → abstain_threshold wired from spec


@pytest.mark.asyncio
async def test_build_agent_missing_key_raises() -> None:
    """Stream Y-2 — agent builds require a platform credential. With no
    ``provider_key_resolver`` (and the manifest ref ignored), the build fails."""
    doc = deepcopy(_MINIMAL_SPEC)
    del doc["spec"]["model"]["api_key_ref"]
    spec = AgentSpec.model_validate(doc)
    async with make_checkpointer("memory") as cp:
        with pytest.raises(AgentFactoryError, match="no platform credential"):
            await build_agent(spec, secret_store=_secret_store(), checkpointer=cp)


def _spec_with_web_search() -> AgentSpec:
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["tools"] = [{"type": "builtin", "name": "web_search"}]
    return AgentSpec.model_validate(doc)


@pytest.mark.asyncio
async def test_build_agent_tool_declared_without_env_raises() -> None:
    """A manifest tool whose ToolEnv dep is absent fails at build time."""
    spec = _spec_with_web_search()
    async with make_checkpointer("memory") as cp:
        with pytest.raises(AgentFactoryError, match="Tavily client"):
            await _build(spec, secret_store=_secret_store(), checkpointer=cp)


@pytest.mark.asyncio
async def test_build_agent_with_tool_env_succeeds() -> None:
    """``tool_env`` is threaded to the assembler — a satisfied dep builds."""
    spec = _spec_with_web_search()
    env = ToolEnv(web_search_client=RecordingTavilyClient())
    async with make_checkpointer("memory") as cp:
        built = await _build(spec, secret_store=_secret_store(), checkpointer=cp, tool_env=env)
    assert isinstance(built, BuiltAgent)


@pytest.mark.asyncio
async def test_build_agent_with_middleware_env_succeeds() -> None:
    """``middleware_env`` is threaded to build_middleware_chains."""
    env = MiddlewareEnv(langfuse_client=RecordingLangfuseClient())
    async with make_checkpointer("memory") as cp:
        built = await _build(
            _spec(), secret_store=_secret_store(), checkpointer=cp, middleware_env=env
        )
    assert isinstance(built, BuiltAgent)


# ---------------------------------------------------------------------------
# Stream Y-2 — agent builds ignore manifest api_key_ref (platform-metered keys)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_agent_ignores_manifest_api_key_ref_for_resolver(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A manifest model that pins ``api_key_ref`` is built via the platform
    resolver instead; the manifest ref is ignored + a deprecation warning logs."""
    # The manifest pins the anthropic dev ref. If that ref had won,
    # build_llm_router would short-circuit on api_key_ref and NEVER consult the
    # resolver — so the resolver being invoked at all (``seen`` non-empty) is
    # proof the manifest ref was ignored and the platform path was taken.
    seen: list[str] = []

    async def resolver(provider: str) -> list[str]:
        seen.append(provider)
        return [f"secret://{_OPENAI_KEY_NAME}"]

    with caplog.at_level("WARNING", logger="expert_work.orchestrator.agent_factory"):
        async with make_checkpointer("memory") as cp:
            built = await build_agent(
                _spec(),
                secret_store=_secret_store(),
                checkpointer=cp,
                provider_key_resolver=resolver,
            )
    assert isinstance(built, BuiltAgent)
    # The resolver was consulted for the manifest provider despite the pinned ref.
    assert seen == ["anthropic"]
    assert any("ignored (Stream Y-2)" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_build_agent_no_resolver_raises_no_platform_credential() -> None:
    """A manifest with a pinned ``api_key_ref`` but no ``provider_key_resolver``
    fails the build — the manifest ref is ignored, leaving no platform key."""
    async with make_checkpointer("memory") as cp:
        with pytest.raises(AgentFactoryError, match="no platform credential"):
            await build_agent(_spec(), secret_store=_secret_store(), checkpointer=cp)


@pytest.mark.asyncio
async def test_build_llm_router_default_honors_api_key_ref_internal_plumbing() -> None:
    """The internal-plumbing default (``ignore_api_key_ref=False``) still honors a
    pinned ``api_key_ref`` with no resolver — control-plane aux callers rely on it."""
    router = await build_llm_router(_spec().spec.model, secret_store=_secret_store())
    inner = router.providers[0].provider.inner  # type: ignore[union-attr]
    assert isinstance(inner, AnthropicProvider)
    # The manifest-pinned anthropic ref resolved through the store, unchanged.
    assert inner.client.api_key == "sk-ant-test"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Stream CM-9 — compute-control capability gates in _build_provider
# ---------------------------------------------------------------------------


def _anthropic_model(**overrides: Any) -> ModelSpec:
    return ModelSpec.model_validate(
        {"provider": "anthropic", "name": "claude-sonnet-4-6", **overrides}
    )


def test_effort_on_unsupported_model_fails_fast() -> None:
    # haiku-4-5 rejects output_config.effort with a 400 — the factory
    # refuses at build instead (Mini-ADR CM-J3).
    model = _anthropic_model(name="claude-haiku-4-5", effort="high")
    with pytest.raises(AgentFactoryError, match=r"does not support output_config\.effort"):
        _build_provider(model, "k")


def test_opus_4_8_drops_temperature() -> None:
    # Opus 4.7+ removed sampling params — sending temperature is a 400.
    provider = _build_provider(_anthropic_model(name="claude-opus-4-8"), "k")
    assert isinstance(provider, AnthropicProvider)
    assert provider.temperature is None


def test_supported_model_passes_effort_and_thinking_through() -> None:
    provider = _build_provider(_anthropic_model(effort="medium", adaptive_thinking=True), "k")
    assert isinstance(provider, AnthropicProvider)
    assert provider.effort == "medium"
    assert provider.adaptive_thinking is True
    assert provider.temperature is not None  # sonnet-4-6 keeps sampling


def test_off_catalog_model_is_not_gated() -> None:
    provider = _build_provider(_anthropic_model(name="claude-custom-gw", effort="max"), "k")
    assert isinstance(provider, AnthropicProvider)
    assert provider.effort == "max"


def test_build_provider_threads_timeout_to_client() -> None:
    # No timeout_s → shared 60s default; an explicit value (VL path) overrides
    # the provider client's httpx wall-clock timeout (Stream L.L3).
    default = _build_provider(_anthropic_model(name="claude-sonnet-4-6"), "k")
    assert isinstance(default, AnthropicProvider)
    assert default.client.timeout_s == 60.0  # type: ignore[union-attr]
    raised = _build_provider(_anthropic_model(name="claude-sonnet-4-6"), "k", timeout_s=180.0)
    assert isinstance(raised, AnthropicProvider)
    assert raised.client.timeout_s == 180.0  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Stream CM-10 — vendor thinking translation (_thinking_payload)
# ---------------------------------------------------------------------------


def _vendor_model(provider: str, name: str, **overrides: Any) -> ModelSpec:
    return ModelSpec.model_validate({"provider": provider, "name": name, **overrides})


def test_thinking_payload_none_for_anthropic_and_untouched() -> None:
    from orchestrator.agent_factory import _thinking_payload

    # anthropic uses CM-9's native channel, never this layer.
    assert _thinking_payload(_anthropic_model(effort="high")) is None
    # Untouched manifests send nothing on any vendor.
    assert _thinking_payload(_vendor_model("qwen", "qwen3.7-max")) is None
    assert _thinking_payload(_vendor_model("glm", "glm-5.1")) is None


def test_thinking_payload_effort_vendors() -> None:
    from orchestrator.agent_factory import _thinking_payload

    assert _thinking_payload(_vendor_model("openai", "gpt-5.5", effort="high")) == {
        "reasoning_effort": "high"
    }
    assert _thinking_payload(_vendor_model("deepseek", "deepseek-v4-pro", effort="max")) == {
        "reasoning_effort": "max"
    }
    # adaptive-only on an effort vendor: omit — the vendor default is
    # already dynamic (Mini-ADR CM-L7).
    assert _thinking_payload(_vendor_model("openai", "gpt-5.5", adaptive_thinking=True)) is None


def test_thinking_payload_budget_vendors() -> None:
    from orchestrator.agent_factory import _thinking_payload

    qwen = _thinking_payload(_vendor_model("qwen", "qwen3.7-max", effort="high", max_tokens=10_000))
    assert qwen == {"enable_thinking": True, "thinking_budget": 8_000}
    doubao = _thinking_payload(
        _vendor_model("doubao", "doubao-seed-2.0-pro", effort="low", max_tokens=4_096)
    )
    # 4096 x 0.2 = 819 → clamped up to the 1024 floor.
    assert doubao == {"thinking": {"type": "enabled", "budget_tokens": 1024}}
    # adaptive-only: qwen opens thinking without a budget; doubao uses auto.
    assert _thinking_payload(_vendor_model("qwen", "qwen3.7-max", adaptive_thinking=True)) == {
        "enable_thinking": True
    }
    assert _thinking_payload(
        _vendor_model("doubao", "doubao-seed-2.0-pro", adaptive_thinking=True)
    ) == {"thinking": {"type": "auto"}}


def test_thinking_payload_budget_clamps_at_ceiling() -> None:
    from orchestrator.agent_factory import _thinking_payload

    payload = _thinking_payload(
        _vendor_model("qwen", "qwen3.7-max", effort="max", max_tokens=200_000)
    )
    assert payload is not None and payload["thinking_budget"] == 81_920


def test_thinking_payload_toggle_vendors_ignore_level() -> None:
    from orchestrator.agent_factory import _thinking_payload

    on = {"thinking": {"type": "enabled"}}
    assert _thinking_payload(_vendor_model("glm", "glm-5.1", effort="low")) == on
    assert _thinking_payload(_vendor_model("glm", "glm-5.1", effort="max")) == on
    assert _thinking_payload(_vendor_model("kimi", "kimi-k2.6", adaptive_thinking=True)) == on


def test_thinking_payload_glm_52_plus_effort_levels() -> None:
    # GLM 5.2+ supports ``reasoning_effort`` (vendor docs 2026-08) on a
    # max/high/low scale — no "medium", so the manifest's medium rounds up
    # to high. The thinking object stays the on/off channel alongside it.
    from orchestrator.agent_factory import _thinking_payload

    assert _thinking_payload(_vendor_model("glm", "glm-5.3", effort="low")) == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
    }
    assert _thinking_payload(_vendor_model("glm", "glm-5.3", effort="medium")) == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
    }
    assert _thinking_payload(_vendor_model("glm", "glm-5.2", effort="max")) == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }


def test_thinking_payload_glm_52_plus_toggle_semantics_kept() -> None:
    from orchestrator.agent_factory import _thinking_payload

    # Force-on with no effort keeps the bare toggle; force-off is a REAL
    # off — GLM has no "minimal", it must not degrade like other effort
    # vendors. Untouched manifests still send nothing (vendor default).
    assert _thinking_payload(_vendor_model("glm", "glm-5.3", thinking_enabled=True)) == {
        "thinking": {"type": "enabled"}
    }
    assert _thinking_payload(_vendor_model("glm", "glm-5.3", thinking_enabled=False)) == {
        "thinking": {"type": "disabled"}
    }
    assert _thinking_payload(_vendor_model("glm", "glm-5.3")) is None


def test_thinking_payload_off_catalog_compat_sends_nothing() -> None:
    # CM-L5 — thinking wire formats differ per vendor, so off-catalog
    # OpenAI-compatible models never get a blind payload.
    from orchestrator.agent_factory import _thinking_payload

    assert _thinking_payload(_vendor_model("qwen", "custom-gateway-model", effort="max")) is None
    assert _thinking_payload(_vendor_model("deepseek", "deepseek-reasoner", effort="high")) is None


def test_thinking_payload_kimi_k3_always_thinking_sends_nothing() -> None:
    # kimi-k3 is on-catalog with thinking=None (always thinking; only accepts
    # reasoning_effort=max today). No thinking field is sent — in particular it
    # must NOT emit the K2.x ``thinking.type`` toggle that the other kimi models
    # use (the K3 docs forbid it).
    from orchestrator.agent_factory import _thinking_payload

    assert _thinking_payload(_vendor_model("kimi", "kimi-k3")) is None


# ---------------------------------------------------------------------------
# Stream CM-10 PR3 — gate + thinking payload wiring for compat vendors
# ---------------------------------------------------------------------------


def test_compat_effort_on_unsupported_model_fails_fast() -> None:
    # qwen3-vl-plus is in-catalog with no thinking control.
    model = _vendor_model("qwen", "qwen3-vl-plus", effort="high")
    with pytest.raises(AgentFactoryError, match="thinking-depth control"):
        _build_provider(model, "k")


def test_compat_provider_carries_translated_payload() -> None:
    provider = _build_provider(
        _vendor_model("qwen", "qwen3.7-max", effort="high", max_tokens=10_000), "k"
    )
    assert isinstance(provider, OpenAIProvider)
    assert provider.thinking_payload == {"enable_thinking": True, "thinking_budget": 8_000}
    glm = _build_provider(_vendor_model("glm", "glm-5.1", effort="low"), "k")
    assert isinstance(glm, OpenAIProvider)
    assert glm.thinking_payload == {"thinking": {"type": "enabled"}}
    openai = _build_provider(_vendor_model("openai", "gpt-5.5", effort="max"), "k")
    assert isinstance(openai, OpenAIProvider)
    assert openai.thinking_payload == {"reasoning_effort": "max"}


def test_compat_off_catalog_not_gated_and_sends_nothing() -> None:
    provider = _build_provider(_vendor_model("qwen", "custom-gateway-model", effort="max"), "k")
    assert isinstance(provider, OpenAIProvider)
    assert provider.thinking_payload is None


def test_glm_gets_tool_stream_stream_extra_body() -> None:
    # glm batches tool-call args by default; tool_stream=True makes it stream
    # them incrementally so the router's idle timer resets per fragment.
    glm = _build_provider(_vendor_model("glm", "glm-5.2"), "k")
    assert isinstance(glm, OpenAICompatibleProvider)
    assert glm.stream_extra_body == {"tool_stream": True}


def test_non_glm_compat_has_no_stream_extra_body() -> None:
    # deepseek/qwen/kimi/doubao stream tool-args incrementally by default.
    ds = _build_provider(_vendor_model("deepseek", "deepseek-v4-pro"), "k")
    assert isinstance(ds, OpenAICompatibleProvider)
    assert ds.stream_extra_body is None


# ---------------------------------------------------------------------------
# Thinking-Toggle — tri-state thinking_enabled (force on / force off / gate)
# ---------------------------------------------------------------------------


def test_thinking_payload_force_off_per_vendor() -> None:
    from orchestrator.agent_factory import _thinking_payload

    # effort vendors (non-anthropic) have no off level -> degrade to "minimal".
    assert _thinking_payload(_vendor_model("openai", "gpt-5.5", thinking_enabled=False)) == {
        "reasoning_effort": "minimal"
    }
    assert _thinking_payload(
        _vendor_model("deepseek", "deepseek-v4-pro", thinking_enabled=False)
    ) == {"reasoning_effort": "minimal"}
    # qwen budget -> enable_thinking false.
    assert _thinking_payload(_vendor_model("qwen", "qwen3.7-max", thinking_enabled=False)) == {
        "enable_thinking": False
    }
    # doubao budget + glm/kimi toggle share the disabled shape.
    disabled = {"thinking": {"type": "disabled"}}
    assert (
        _thinking_payload(_vendor_model("doubao", "doubao-seed-2.0-pro", thinking_enabled=False))
        == disabled
    )
    assert _thinking_payload(_vendor_model("glm", "glm-5.1", thinking_enabled=False)) == disabled


def test_thinking_payload_force_on_per_vendor() -> None:
    from orchestrator.agent_factory import _thinking_payload

    # budget/toggle enable even with no effort set.
    assert _thinking_payload(_vendor_model("qwen", "qwen3.7-max", thinking_enabled=True)) == {
        "enable_thinking": True
    }
    assert _thinking_payload(_vendor_model("glm", "glm-5.1", thinking_enabled=True)) == {
        "thinking": {"type": "enabled"}
    }
    # effort vendor on with no effort -> vendor default (nothing sent).
    assert _thinking_payload(_vendor_model("openai", "gpt-5.5", thinking_enabled=True)) is None
    # on + explicit effort -> depth honoured.
    assert _thinking_payload(
        _vendor_model("openai", "gpt-5.5", thinking_enabled=True, effort="high")
    ) == {"reasoning_effort": "high"}


def test_thinking_toggle_gate_on_no_knob_model() -> None:
    # in-catalog model with no thinking knob rejects thinking_enabled (compat).
    with pytest.raises(AgentFactoryError, match="no thinking toggle"):
        _build_provider(_vendor_model("qwen", "qwen3-vl-plus", thinking_enabled=False), "k")
    # anthropic path too (haiku has no knob).
    with pytest.raises(AgentFactoryError, match="no thinking toggle"):
        _build_provider(_anthropic_model(name="claude-haiku-4-5", thinking_enabled=True), "k")
    # off-catalog is not gated.
    ok = _build_provider(_vendor_model("qwen", "custom-gw", thinking_enabled=False), "k")
    assert isinstance(ok, OpenAIProvider) and ok.thinking_payload is None


def test_anthropic_provider_carries_thinking_enabled() -> None:
    off = _build_provider(_anthropic_model(thinking_enabled=False), "k")
    assert isinstance(off, AnthropicProvider) and off.thinking_enabled is False


def test_compat_untouched_manifest_has_no_payload() -> None:
    provider = _build_provider(_vendor_model("qwen", "qwen3.7-max"), "k")
    assert isinstance(provider, OpenAIProvider)
    assert provider.thinking_payload is None


# ---------------------------------------------------------------------------
# Stream HX-1 — the factory resolves one shared token estimator per build
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_agent_resolves_shared_token_estimator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The build path asks :func:`default_estimator` for the shared
    tiktoken-backed estimator (threaded into the context gates + the
    drift counter); the patch proves the seam is exercised without
    loading a real BPE vocabulary in unit tests."""
    from expert_work.runtime.tokens import CharTokenEstimator

    calls: list[int] = []
    fake = CharTokenEstimator()

    def _fake_default() -> CharTokenEstimator:
        calls.append(1)
        return fake

    monkeypatch.setattr("orchestrator.agent_factory.default_estimator", _fake_default)
    async with make_checkpointer("memory") as cp:
        built = await _build(_spec(), secret_store=_secret_store(), checkpointer=cp)
    assert isinstance(built, BuiltAgent)
    assert calls == [1]


# ---------------------------------------------------------------------------
# Stream HX-1 PR2 — context_window catalog resolution (Mini-ADR HX-A4)
# ---------------------------------------------------------------------------


def test_resolved_context_window_explicit_value_wins() -> None:
    from orchestrator.agent_factory import _resolved_context_window

    model = ModelSpec(provider="qwen", name="qwen3.7-max", context_window=42_000)
    assert _resolved_context_window(model) == 42_000


def test_resolved_context_window_from_catalog() -> None:
    from orchestrator.agent_factory import _resolved_context_window

    model = ModelSpec(provider="qwen", name="qwen3.7-max")
    assert _resolved_context_window(model) == 1_000_000


def test_resolved_context_window_catalog_entry_without_window() -> None:
    from orchestrator.agent_factory import _resolved_context_window

    # qwen3-vl-plus is in the catalog but publishes no context_window.
    model = ModelSpec(provider="qwen", name="qwen3-vl-plus")
    assert _resolved_context_window(model) == 200_000


def test_resolved_context_window_off_catalog_fallback() -> None:
    from orchestrator.agent_factory import _resolved_context_window

    model = ModelSpec(provider="self-hosted", name="my-private-model")
    assert _resolved_context_window(model) == 200_000


# ---------------------------------------------------------------------------
# Tool-call-rate uplift — tool-use enforcement block reaches the built system
# prompt under the ``auto`` denylist (weak model enforced, Claude/GPT exempt).
# Proves the build-time wiring incl. the "agent has tools" gate.
# ---------------------------------------------------------------------------


async def test_enforcement_block_present_for_weak_model_build() -> None:
    # A weak-family model (qwen) WITH a tool → enforcement is injected. The
    # block is gated on the agent actually having tools, so the spec declares
    # web_search (satisfied by a recording client).
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["model"].update({"provider": "qwen", "name": "qwen3-max"})
    doc["spec"]["tools"] = [{"type": "builtin", "name": "web_search"}]
    spec = AgentSpec.model_validate(doc)
    env = ToolEnv(web_search_client=RecordingTavilyClient())
    async with make_checkpointer("memory") as cp:
        built = await _build(spec, secret_store=_secret_store(), checkpointer=cp, tool_env=env)
    assert "# Tool-use enforcement" in built.system_prompt


async def test_enforcement_block_absent_for_claude_build() -> None:
    # Default minimal spec is anthropic/claude → ``auto`` exempts it.
    async with make_checkpointer("memory") as cp:
        built = await _build(_spec(), secret_store=_secret_store(), checkpointer=cp)
    assert "# Tool-use enforcement" not in built.system_prompt


# ---------------------------------------------------------------------------
# Phase 3 — tool-output-budget master switch resolution (effective = platform AND
# agent). A spy on ``build_react_graph`` captures the resolved bool the factory
# threads into externalization; the platform value also gates the prune build.
# ---------------------------------------------------------------------------


def _spy_budget(monkeypatch: Any, captured: dict[str, Any]) -> None:
    from orchestrator.agent_factory import build_react_graph as real

    def _spy(**kwargs: Any) -> Any:
        captured["enabled"] = kwargs.get("tool_output_budget_enabled")
        return real(**kwargs)

    monkeypatch.setattr("orchestrator.agent_factory.build_react_graph", _spy)


async def test_tool_budget_effective_is_platform_and_agent(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _spy_budget(monkeypatch, captured)

    async with make_checkpointer("memory") as cp:
        await _build(
            _spec(),
            secret_store=_secret_store(),
            checkpointer=cp,
            platform_tool_budget_enabled=True,
        )
        assert captured["enabled"] is True  # platform ∧ agent both on

        await _build(
            _spec(),
            secret_store=_secret_store(),
            checkpointer=cp,
            platform_tool_budget_enabled=False,
        )
        assert captured["enabled"] is False  # platform off = master kill

        spec = _spec()
        spec.spec.policies.tool_output_budget.enabled = False
        await _build(
            spec, secret_store=_secret_store(), checkpointer=cp, platform_tool_budget_enabled=True
        )
        assert captured["enabled"] is False  # agent off


async def test_tool_budget_platform_none_falls_back_to_env(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _spy_budget(monkeypatch, captured)

    async with make_checkpointer("memory") as cp:
        monkeypatch.delenv("EXPERT_WORK_TOOL_OUTPUT_BUDGET", raising=False)
        await _build(_spec(), secret_store=_secret_store(), checkpointer=cp)  # → env default (on)
        assert captured["enabled"] is True

        monkeypatch.setenv("EXPERT_WORK_TOOL_OUTPUT_BUDGET", "0")
        await _build(_spec(), secret_store=_secret_store(), checkpointer=cp)
        assert captured["enabled"] is False


# ---------------------------------------------------------------------------
# Stream RT-1 PR-3 (RT-ADR-4) — Tier3 output_schema wiring
# ---------------------------------------------------------------------------


def _output_schema_spec(json_schema: dict[str, Any]) -> AgentSpec:
    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["output_schema"] = {"name": "final_report", "json_schema": json_schema}
    return AgentSpec.model_validate(doc)


async def test_output_schema_threads_into_graph_with_fence_nonce(monkeypatch: Any) -> None:
    """The manifest block becomes a StructuredOutputSpec handed to
    build_react_graph — tenant-origin, so fence_nonce is always set
    (design § 7.5), reusing the build's spotlight nonce when present."""
    captured: dict[str, Any] = {}
    from orchestrator.agent_factory import build_react_graph as real

    def _spy(**kwargs: Any) -> Any:
        captured["output_schema"] = kwargs.get("output_schema")
        captured["spotlight_nonce"] = kwargs.get("spotlight_nonce")
        return real(**kwargs)

    monkeypatch.setattr("orchestrator.agent_factory.build_react_graph", _spy)

    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    async with make_checkpointer("memory") as cp:
        built = await _build(
            _output_schema_spec(schema), secret_store=_secret_store(), checkpointer=cp
        )
    assert isinstance(built, BuiltAgent)
    spec = captured["output_schema"]
    assert isinstance(spec, StructuredOutputSpec)
    assert spec.schema == schema
    assert spec.name == "final_report"
    assert spec.strict is True
    # Spotlighting is on by default → the build nonce is reused.
    assert spec.fence_nonce == captured["spotlight_nonce"]
    assert spec.fence_nonce


async def test_output_schema_fence_nonce_minted_when_spotlight_off(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    from orchestrator.agent_factory import build_react_graph as real

    def _spy(**kwargs: Any) -> Any:
        captured["output_schema"] = kwargs.get("output_schema")
        return real(**kwargs)

    monkeypatch.setattr("orchestrator.agent_factory.build_react_graph", _spy)

    doc = deepcopy(_MINIMAL_SPEC)
    doc["spec"]["output_schema"] = {"json_schema": {"type": "object"}}
    doc["spec"]["defenses"] = {"prompt_injection": "off"}
    async with make_checkpointer("memory") as cp:
        await _build(AgentSpec.model_validate(doc), secret_store=_secret_store(), checkpointer=cp)
    spec = captured["output_schema"]
    assert isinstance(spec, StructuredOutputSpec)
    assert spec.fence_nonce  # minted even without the spotlight defense


async def test_output_schema_absent_threads_none(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    from orchestrator.agent_factory import build_react_graph as real

    def _spy(**kwargs: Any) -> Any:
        captured["output_schema"] = kwargs.get("output_schema")
        return real(**kwargs)

    monkeypatch.setattr("orchestrator.agent_factory.build_react_graph", _spy)

    async with make_checkpointer("memory") as cp:
        await _build(_spec(), secret_store=_secret_store(), checkpointer=cp)
    assert captured["output_schema"] is None


async def test_output_schema_deep_invalid_schema_fails_the_build() -> None:
    """The protocol model only shape-checks; a deeply-invalid JSON Schema
    (bad keyword value) must fail at BUILD time with a clear error."""
    bad = _output_schema_spec({"type": "object", "properties": {"x": {"type": 123}}})
    async with make_checkpointer("memory") as cp:
        with pytest.raises(AgentFactoryError, match="not a valid JSON Schema"):
            await _build(bad, secret_store=_secret_store(), checkpointer=cp)
