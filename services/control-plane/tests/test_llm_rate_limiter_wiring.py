"""波 2 线 A — control-plane wiring of the global provider RPM bucket.

The Redis-backed limiter lives in the orchestrator; the control-plane picks
it when ``quota_redis_url`` is set and threads it into every ``build_agent``
path (main / delegated child / spawned worker) and the judge caller, the
same way ``http_client`` travels. Without Redis every path passes ``None``
and the orchestrator keeps its per-process bucket.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from control_plane.app import create_app
from control_plane.runtime import make_agent_builder, resolve_defenses
from control_plane.settings import Settings
from control_plane.subagent_runtime import make_child_agent_builder, make_worker_build_fn
from expert_work.common.credentials import CredentialsResolver
from expert_work.persistence.agent_spec import InMemoryAgentSpecStore
from expert_work.protocol import AgentSpec, TenantConfigRecord, TenantPlan
from expert_work.runtime.secret_store import LocalDevSecretStore
from expert_work.testing import InMemorySecretStore
from orchestrator import BuiltAgent, LLMOutputJudge, ToolEnv
from orchestrator.llm.rate_limit import AdmissionLimiter
from orchestrator.llm.rate_limit_redis import RedisRpmLimiter
from tests.auth_fixtures import build_test_jwt_verifier

_SHA = "a" * 64
_ANTHROPIC_KEY_NAME = "anthropic-test"


def _spec(name: str = "x", *, defenses: bool = False) -> AgentSpec:
    doc: dict[str, Any] = {
        "apiVersion": "expert_work.io/v1",
        "kind": "Agent",
        "metadata": {"name": name, "version": "1.0.0", "tenant": "t"},
        "spec": {
            "tenant_config": {},
            "model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
            "system_prompt": {"template": "x"},
            "sandbox": {
                "resources": {"cpu": "1", "memory": "1Gi"},
                "network": {"egress": "proxy", "allowlist": ["a.com"]},
                "filesystem": {},
            },
        },
    }
    if defenses:
        doc["spec"]["defenses"] = {"output_judge": "block", "action_screen": "off"}
    return AgentSpec.model_validate(doc)


class _StubTenantConfig:
    async def get(self, *, tenant_id: UUID, actor_id: str | None = None) -> TenantConfigRecord:
        now = datetime.now(UTC)
        return TenantConfigRecord(
            tenant_id=tenant_id,
            display_name="t",
            plan=TenantPlan.FREE,
            created_at=now,
            updated_at=now,
            updated_by="test",
        )


def _credentials_resolver() -> CredentialsResolver:
    return CredentialsResolver(
        platform_provider_credentials={"anthropic": f"secret://{_ANTHROPIC_KEY_NAME}"},  # type: ignore[arg-type]
        platform_tool_credentials={},  # type: ignore[arg-type]
        tenant_config_getter=_StubTenantConfig(),  # type: ignore[arg-type]
    )


class _Spy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *, key: str, secret_ref: str, rate_limit_rpm: int) -> AdmissionLimiter:
        from aiolimiter import AsyncLimiter

        self.calls.append({"key": key, "secret_ref": secret_ref, "rate_limit_rpm": rate_limit_rpm})
        return AsyncLimiter(max_rate=1000, time_period=60)


@pytest.fixture
def runtime_build_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _fake_build_agent(spec: AgentSpec, **kwargs: Any) -> BuiltAgent:
        calls.append({"spec": spec, **kwargs})
        return BuiltAgent(graph=object(), system_prompt="", max_steps=1)  # type: ignore[arg-type]

    monkeypatch.setattr("control_plane.runtime.build_agent", _fake_build_agent)
    return calls


@pytest.fixture
def subagent_build_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _fake_build_agent(spec: AgentSpec, **kwargs: Any) -> BuiltAgent:
        calls.append({"spec": spec, **kwargs})
        return BuiltAgent(graph=object(), system_prompt="", max_steps=1)  # type: ignore[arg-type]

    monkeypatch.setattr("control_plane.subagent_runtime.build_agent", _fake_build_agent)
    return calls


# ------------------------------------------------------------ builders forward


@pytest.mark.asyncio
async def test_main_builder_forwards_rate_limiter_factory(
    runtime_build_calls: list[dict[str, Any]],
) -> None:
    spy = _Spy()
    builder = make_agent_builder(
        InMemorySecretStore(), InMemorySaver(), tool_env=ToolEnv(), rate_limiter_factory=spy
    )

    await builder(_spec(), tenant_id=uuid4())

    assert runtime_build_calls[0]["rate_limiter_factory"] is spy


@pytest.mark.asyncio
async def test_main_builder_default_is_none(runtime_build_calls: list[dict[str, Any]]) -> None:
    builder = make_agent_builder(InMemorySecretStore(), InMemorySaver(), tool_env=ToolEnv())

    await builder(_spec(), tenant_id=uuid4())

    assert runtime_build_calls[0]["rate_limiter_factory"] is None


@pytest.mark.asyncio
async def test_child_builder_forwards_rate_limiter_factory(
    subagent_build_calls: list[dict[str, Any]],
) -> None:
    spy = _Spy()
    tenant = uuid4()
    store = InMemoryAgentSpecStore()
    await store.create(tenant_id=tenant, spec=_spec("child"), spec_sha256=_SHA, created_by="t")
    builder = make_child_agent_builder(
        spec_store=store,
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        rate_limiter_factory=spy,
    )

    await builder(tenant_id=tenant, name="child", version="1.0.0", depth=1)

    assert subagent_build_calls[0]["rate_limiter_factory"] is spy


@pytest.mark.asyncio
async def test_worker_build_fn_forwards_rate_limiter_factory(
    subagent_build_calls: list[dict[str, Any]],
) -> None:
    spy = _Spy()
    build_fn = make_worker_build_fn(
        secret_store=InMemorySecretStore(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        max_iterations=8,
        allowed_toolsets=[],
        rate_limiter_factory=spy,
    )

    await build_fn(_spec("parent"), tenant_id=uuid4(), role="probe", depth=1)

    assert subagent_build_calls[0]["rate_limiter_factory"] is spy


# ------------------------------------------------------------ judge caller


@pytest.mark.asyncio
async def test_judge_caller_uses_the_factory() -> None:
    """The judge's router is built through ``build_llm_router`` too — its
    provider handle must draw from the same global bucket."""
    spy = _Spy()

    defenses = await resolve_defenses(
        _spec(defenses=True),
        tenant_id=uuid4(),
        credentials_resolver=_credentials_resolver(),
        secret_store=LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"}),
        rate_limiter_factory=spy,
    )

    assert isinstance(defenses.output_judge, LLMOutputJudge)
    assert [c["key"] for c in spy.calls] == ["anthropic:claude-haiku-4-5"]
    assert spy.calls[0]["secret_ref"] == f"secret://{_ANTHROPIC_KEY_NAME}"


# ------------------------------------------------------------ app lifespan


def test_quota_redis_url_selects_the_redis_backend() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        single_instance=False,
        quota_redis_url="redis://localhost:6379/0",
        apikey_rate_limit_hmac_salt="test-hmac-salt",
    )
    app = create_app(settings=settings, jwt_verifier=build_test_jwt_verifier())
    with TestClient(app):
        factory = app.state.llm_rate_limiter_factory
        assert factory is not None
        limiter = factory(key="anthropic:claude", secret_ref="secret://x", rate_limit_rpm=60)
        assert isinstance(limiter, RedisRpmLimiter)


def test_without_quota_redis_url_no_factory_is_wired() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    app = create_app(settings=settings, jwt_verifier=build_test_jwt_verifier())
    with TestClient(app):
        assert app.state.llm_rate_limiter_factory is None
