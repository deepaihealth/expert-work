"""B-104 —— run 内用**平台模型**的调用记 ``platform_overhead``:输出 / 工具调用安全评审
(#8 / #9)、知识库 / 记忆重排序的 LLM 分支(#10)。

口径按用途分(用户 2026-09-24 拍板):评审退回 Agent 主模型时也记 ``platform_overhead``;
这些行计入 run 的 token 池,运营用量页按 kind 可见,不进对外对话用量与客户账单。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import var_child_runnable_config
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from control_plane.runtime import (
    DynamicResolvingReranker,
    ResolvingReranker,
    make_agent_builder,
    resolve_defenses,
)
from control_plane.subagent_runtime import make_child_agent_builder, make_worker_build_fn
from expert_work.common.credentials import CredentialsResolver
from expert_work.common.observability import (
    ExpertWorkComponent,
    current_trace_id_hex,
    expert_work_span,
    init_tracing,
)
from expert_work.persistence.agent_spec import InMemoryAgentSpecStore
from expert_work.persistence.token_usage_store import (
    PLATFORM_OVERHEAD_USAGE_KIND,
    InMemoryTokenUsageStore,
    TokenUsageRecord,
)
from expert_work.protocol import AgentSpec, StructuredOutputSpec
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import BuiltAgent, LLMActionJudge, LLMOutputJudge, MiddlewareEnv, ToolEnv
from orchestrator.llm.providers._streaming import LLMDelta
from orchestrator.tools._guards import TOKEN_BUDGET_KEY, TokenBudget
from orchestrator.tools.registry import ToolSpec
from orchestrator.usage_metering import MeteredLLMCaller, ScopedReranker, UsageIdentity

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_USER = UUID("33333333-3333-3333-3333-333333333333")
_KEY_NAME = "expert-work/dev/llm/anthropic"
_USAGE = {"input_tokens": 70, "output_tokens": 5, "total_tokens": 75}
_VERDICTS = {
    "output_judge_verdict": '{"aligned": true, "leak_suspected": false, "reason": "ok"}',
    "action_judge_verdict": '{"aligned": true, "reason": "ok"}',
}


@pytest.fixture
def tracing() -> Iterator[None]:
    provider = init_tracing(
        service_name="platform-overhead-test",
        env="test",
        span_processor=SimpleSpanProcessor(InMemorySpanExporter()),
    )
    try:
        yield
    finally:
        provider.shutdown()


@dataclass
class _Caller:
    """``build_llm_router`` 的替身:回一条带用量的回答,或抛错。

    按 ``output_schema`` 认出是哪个评审,回对应形状的 verdict;没有 schema(重排序)回
    ``content``。
    """

    content: str = ""
    error: Exception | None = None

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
        on_delta: Callable[[LLMDelta], Awaitable[None]] | None = None,
    ) -> AIMessage:
        del messages, tools, on_delta
        if self.error is not None:
            raise self.error
        content = self.content
        if output_schema is not None:
            content = _VERDICTS[output_schema.name]
        return AIMessage(content=content, usage_metadata=_USAGE)


@dataclass
class _RouterFactory:
    caller: _Caller
    specs: list[Any] = field(default_factory=list)

    async def __call__(self, model_spec: Any, **kwargs: Any) -> _Caller:
        del kwargs
        self.specs.append(model_spec)
        return self.caller


def _patch_router(monkeypatch: pytest.MonkeyPatch, caller: _Caller) -> _RouterFactory:
    factory = _RouterFactory(caller=caller)
    monkeypatch.setattr("control_plane.runtime.build_llm_router", factory)
    return factory


class _StubTenantConfig:
    async def get(self, tenant_id: UUID) -> None:
        del tenant_id


def _credentials() -> CredentialsResolver:
    return CredentialsResolver(
        platform_provider_credentials={
            "anthropic": f"secret://{_KEY_NAME}",
            "qwen": f"secret://{_KEY_NAME}",
        },
        platform_tool_credentials={},
        tenant_config_getter=_StubTenantConfig(),  # type: ignore[arg-type]
    )


def _secret_store() -> LocalDevSecretStore:
    return LocalDevSecretStore.from_mapping({_KEY_NAME: "sk-test"})


class _JudgeConfig:
    def __init__(self, pair: tuple[str, str] | None) -> None:
        self._pair = pair

    async def effective_judge_config(self) -> tuple[str, str] | None:
        return self._pair


def _spec(name: str = "ai-health-plan") -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": name, "version": "1.2.0", "tenant": "t"},
            "spec": {
                "tenant_config": {},
                "model": {"provider": "anthropic", "name": "claude-haiku-4-5"},
                "system_prompt": {"template": "t"},
                "defenses": {"output_judge": "block", "action_screen": "block"},
                "sandbox": {
                    "resources": {"cpu": "1.0", "memory": "1Gi"},
                    "network": {"egress": "proxy", "allowlist": []},
                    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
                },
            },
        }
    )


def _config(budget: TokenBudget) -> RunnableConfig:
    return {
        "configurable": {"tenant_id": str(_TENANT), "user_id": str(_USER), TOKEN_BUDGET_KEY: budget}
    }


async def _rows(store: InMemoryTokenUsageStore) -> list[TokenUsageRecord]:
    return sorted(await store.list_for_tenant(tenant_id=_TENANT), key=lambda r: r.id or 0)


def _assert_overhead_row(row: TokenUsageRecord, *, model: tuple[str, str], trace_id: str) -> None:
    assert (row.provider, row.model) == model
    assert row.usage_kind == PLATFORM_OVERHEAD_USAGE_KIND
    assert (row.agent_name, row.agent_version) == ("ai-health-plan", "1.2.0")
    assert (row.tenant_id, row.user_id) == (_TENANT, _USER)
    assert (row.input_tokens, row.output_tokens) == (70, 5)
    assert row.trace_id == trace_id


# ---------------------------------------------------------------------------
# #8 / #9 安全评审
# ---------------------------------------------------------------------------


async def _run_judges(
    monkeypatch: pytest.MonkeyPatch,
    *,
    judge_config: tuple[str, str] | None,
    caller: _Caller,
) -> tuple[InMemoryTokenUsageStore, TokenBudget, str]:
    _patch_router(monkeypatch, caller)
    store = InMemoryTokenUsageStore()
    defenses = await resolve_defenses(
        _spec(),
        tenant_id=_TENANT,
        credentials_resolver=_credentials(),
        secret_store=_secret_store(),
        platform_judge_config_service=_JudgeConfig(judge_config),  # type: ignore[arg-type]
        token_usage_store=store,
    )
    assert isinstance(defenses.output_judge, LLMOutputJudge)
    assert isinstance(defenses.action_judge, LLMActionJudge)
    budget = TokenBudget(limit=10_000)
    token = var_child_runnable_config.set(_config(budget))
    try:
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "run"):
            trace_id = current_trace_id_hex()
            try:
                await defenses.output_judge.judge(
                    user_request="summarise", response="summary", context_hint=None
                )
                await defenses.action_judge.judge_action(
                    user_request="summarise", tool_name="read_file", tool_args={"path": "a"}
                )
            except RuntimeError:
                pass
    finally:
        var_child_runnable_config.reset(token)
    assert trace_id is not None
    return store, budget, trace_id


@pytest.mark.asyncio
async def test_judge_calls_are_metered_as_platform_overhead_under_the_judge_model(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, budget, trace_id = await _run_judges(
        monkeypatch, judge_config=("qwen", "qwen-max"), caller=_Caller()
    )

    rows = await _rows(store)
    # 输出评审一次 + 工具调用评审一次。
    assert len(rows) == 2
    for row in rows:
        _assert_overhead_row(row, model=("qwen", "qwen-max"), trace_id=trace_id)
    assert budget.spent == 2 * 75


@pytest.mark.asyncio
async def test_judge_falling_back_to_the_agent_model_is_still_platform_overhead(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, budget, trace_id = await _run_judges(monkeypatch, judge_config=None, caller=_Caller())

    rows = await _rows(store)
    assert len(rows) == 2
    for row in rows:
        _assert_overhead_row(row, model=("anthropic", "claude-haiku-4-5"), trace_id=trace_id)
    assert budget.spent == 2 * 75


@pytest.mark.asyncio
async def test_failed_judge_call_records_nothing(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, budget, _trace_id = await _run_judges(
        monkeypatch, judge_config=None, caller=_Caller(error=RuntimeError("down"))
    )

    assert await _rows(store) == []
    assert budget.spent == 0


# ---------------------------------------------------------------------------
# 三条构建路径(主 / 静态子 Agent / worker)都把用量存储交给评审
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_build_path_hands_the_usage_store_to_the_judges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_router(monkeypatch, _Caller())
    calls: list[dict[str, Any]] = []

    async def _fake_build_agent(spec: AgentSpec, **kwargs: Any) -> BuiltAgent:
        calls.append({"spec": spec, **kwargs})
        return BuiltAgent(graph=object(), system_prompt="", max_steps=1)  # type: ignore[arg-type]

    monkeypatch.setattr("control_plane.runtime.build_agent", _fake_build_agent)
    monkeypatch.setattr("control_plane.subagent_runtime.build_agent", _fake_build_agent)
    usage = InMemoryTokenUsageStore()
    env = MiddlewareEnv(token_usage_store=usage)
    specs = InMemoryAgentSpecStore()
    await specs.create(
        tenant_id=_TENANT, spec=_spec("researcher"), spec_sha256="0" * 64, created_by="test"
    )

    await make_agent_builder(
        _secret_store(), InMemorySaver(), credentials_resolver=_credentials(), middleware_env=env
    )(_spec(), tenant_id=_TENANT)
    await make_child_agent_builder(
        spec_store=specs,
        secret_store=_secret_store(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        credentials_resolver=_credentials(),
        middleware_env=env,
    )(tenant_id=_TENANT, name="researcher", version="1.2.0", depth=1)
    await make_worker_build_fn(
        secret_store=_secret_store(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        max_iterations=8,
        allowed_toolsets=[],
        credentials_resolver=_credentials(),
        middleware_env=env,
    )(_spec(), tenant_id=_TENANT, role="probe", depth=1)

    assert len(calls) == 3
    for kw in calls:
        for judge in (kw["output_judge"], kw["action_judge"]):
            assert isinstance(judge.caller, MeteredLLMCaller)
            assert judge.caller.meter.store is usage
            assert judge.caller.meter.usage_kind == PLATFORM_OVERHEAD_USAGE_KIND


# ---------------------------------------------------------------------------
# #10 重排序的 LLM 分支
# ---------------------------------------------------------------------------


class _Resolver:
    async def resolve_provider(self, *, tenant_id: UUID, provider: str) -> str:
        del tenant_id
        return f"secret://{provider}"


class _SecretStore:
    async def get(self, name: str) -> str:
        del name
        return "fake-key"


class _RerankConfig:
    async def effective_rerank_config(self) -> tuple[str, str] | None:
        return ("qwen", "qwen-plus")


def _identity(store: InMemoryTokenUsageStore) -> UsageIdentity:
    return UsageIdentity(
        store=store,
        agent_name="ai-health-plan",
        agent_version="1.2.0",
        usage_kind=PLATFORM_OVERHEAD_USAGE_KIND,
    )


def _dynamic() -> DynamicResolvingReranker:
    return DynamicResolvingReranker(
        config_service=_RerankConfig(),  # type: ignore[arg-type]
        resolver=_Resolver(),  # type: ignore[arg-type]
        secret_store=_SecretStore(),  # type: ignore[arg-type]
    )


def _static() -> ResolvingReranker:
    return ResolvingReranker(
        resolver=_Resolver(),  # type: ignore[arg-type]
        secret_store=_SecretStore(),  # type: ignore[arg-type]
        provider="qwen",
        model="qwen-plus",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("make", [_dynamic, _static], ids=["dynamic", "static"])
async def test_llm_rerank_inside_a_run_is_metered_as_platform_overhead(
    tracing: None, monkeypatch: pytest.MonkeyPatch, make: Callable[[], Any]
) -> None:
    _patch_router(monkeypatch, _Caller(content="[2, 1]"))
    store = InMemoryTokenUsageStore()
    budget = TokenBudget(limit=10_000)
    reranker = ScopedReranker(inner=make(), identity=_identity(store))
    token = var_child_runnable_config.set(_config(budget))
    try:
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "run"):
            trace_id = current_trace_id_hex()
            order = await reranker.rerank(
                query="q", documents=["a", "b"], top_k=2, tenant_id=_TENANT
            )
    finally:
        var_child_runnable_config.reset(token)

    assert order == [1, 0]
    assert trace_id is not None
    [row] = await _rows(store)
    _assert_overhead_row(row, model=("qwen", "qwen-plus"), trace_id=trace_id)
    assert budget.spent == 75


@pytest.mark.asyncio
async def test_llm_rerank_outside_an_agent_build_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 检索测试接口等 run 外调用:没有 ScopedReranker 放身份,原样走,不记账。
    factory = _patch_router(monkeypatch, _Caller(content="[2, 1]"))
    budget = TokenBudget(limit=10_000)
    token = var_child_runnable_config.set(_config(budget))
    try:
        order = await _dynamic().rerank(query="q", documents=["a", "b"], top_k=2, tenant_id=_TENANT)
    finally:
        var_child_runnable_config.reset(token)

    assert order == [1, 0]
    assert len(factory.specs) == 1
    assert budget.spent == 0
