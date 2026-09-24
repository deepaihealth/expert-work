"""B-104 —— 控制面构建出的 agent 真跑一轮:评审(#8 / #9)在真图里记账,委派出去的子
Agent 里的调用记在子代名下、父 run 的 trace 下、扣同一个池。

评审测试不手工设 ``var_child_runnable_config``:评审的调用点要是挪到节点外,这里会红。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from control_plane.runtime import make_agent_builder
from control_plane.subagent_runtime import make_child_agent_builder
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
from expert_work.protocol import AgentSpec, ModelSpec, StructuredOutputSpec
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import BuiltAgent, MiddlewareEnv, ToolEnv
from orchestrator.tools._guards import TOKEN_BUDGET_KEY, TokenBudget
from orchestrator.tools.registry import ToolSpec

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_USER = UUID("33333333-3333-3333-3333-333333333333")
_KEY_NAME = "expert-work/dev/llm/anthropic"
_MODEL = ("anthropic", "claude-haiku-4-5")

#: 每类调用一个独特的 input_tokens,落下来的行按它认出是哪个调用点。
_INPUT = {
    "parent": 100,
    "child": 200,
    "planner": 11,
    "output_judge": 70,
    "action_judge": 71,
}
_MARKERS = (
    ("You are a planning module", "planner"),
    ("You are a security output judge", "output_judge"),
    ("You are a security action judge", "action_judge"),
)
_REPLIES = {
    "planner": '{"goal": "g", "steps": ["s1"]}',
    "output_judge": '{"aligned": true, "leak_suspected": false, "reason": "ok"}',
    "action_judge": '{"aligned": true, "reason": "ok"}',
    "child": "child done",
}


def _usage(purpose: str) -> dict[str, int]:
    n = _INPUT[purpose]
    return {"input_tokens": n, "output_tokens": 1, "total_tokens": n + 1}


def _purpose_of(messages: Sequence[BaseMessage]) -> str:
    first = messages[0] if messages else None
    text = first.content if isinstance(first, SystemMessage) else ""
    if isinstance(text, str):
        for marker, purpose in _MARKERS:
            if text.startswith(marker):
                return purpose
        if "CHILD-PROMPT" in text:
            return "child"
    return "parent"


@dataclass
class _Brain:
    """按系统提示认出调用点回答;父 agent 的主循环按脚本回答。"""

    parent_replies: list[AIMessage] = field(default_factory=list)
    purposes: list[str] = field(default_factory=list)

    async def complete(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AIMessage:
        del tools, output_schema
        purpose = _purpose_of(messages)
        self.purposes.append(purpose)
        if purpose == "parent" and self.parent_replies:
            return self.parent_replies.pop(0)
        content = _REPLIES.get(purpose, "done")
        return AIMessage(content=content, usage_metadata=_usage(purpose))


def _patch_providers(monkeypatch: pytest.MonkeyPatch, brain: _Brain) -> None:
    def _fake_build_provider(entry: ModelSpec, api_key: str, **kwargs: Any) -> Any:
        del entry, api_key, kwargs
        return brain

    monkeypatch.setattr("orchestrator.agent_factory._build_provider", _fake_build_provider)


@pytest.fixture
def tracing() -> Iterator[None]:
    provider = init_tracing(
        service_name="in-run-metering-graph-test",
        env="test",
        span_processor=SimpleSpanProcessor(InMemorySpanExporter()),
    )
    try:
        yield
    finally:
        provider.shutdown()


class _StubTenantConfig:
    async def get(self, tenant_id: UUID) -> None:
        del tenant_id


def _credentials() -> CredentialsResolver:
    return CredentialsResolver(
        platform_provider_credentials={"anthropic": f"secret://{_KEY_NAME}"},
        platform_tool_credentials={},
        tenant_config_getter=_StubTenantConfig(),  # type: ignore[arg-type]
    )


def _secret_store() -> LocalDevSecretStore:
    return LocalDevSecretStore.from_mapping({_KEY_NAME: "sk-test"})


def _spec(name: str, prompt: str, extra: dict[str, Any]) -> AgentSpec:
    doc: dict[str, Any] = {
        "apiVersion": "expert_work.io/v1",
        "kind": "Agent",
        "metadata": {"name": name, "version": "1.2.0", "tenant": "t"},
        "spec": {
            "tenant_config": {},
            "model": {"provider": _MODEL[0], "name": _MODEL[1]},
            "system_prompt": {"template": prompt},
            "sandbox": {
                "resources": {"cpu": "1.0", "memory": "1Gi"},
                "network": {"egress": "proxy", "allowlist": []},
                "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
            },
        },
    }
    doc["spec"].update(extra)
    return AgentSpec.model_validate(doc)


def _call(name: str, args: dict[str, Any]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": f"call-{name}", "type": "tool_call"}],
        usage_metadata=_usage("parent"),
    )


async def _invoke(built: BuiltAgent, budget: TokenBudget) -> str:
    config: RunnableConfig = {
        "configurable": {
            "thread_id": str(uuid4()),
            "tenant_id": str(_TENANT),
            "user_id": str(_USER),
            "run_id": str(uuid4()),
            TOKEN_BUDGET_KEY: budget,
        }
    }
    with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "run"):
        trace_id = current_trace_id_hex()
        await built.graph.ainvoke(
            {"messages": [HumanMessage(content="plan my week")], "step_count": 0, "max_steps": 6},
            config=config,
        )
    assert trace_id is not None
    return trace_id


async def _rows(store: InMemoryTokenUsageStore) -> list[TokenUsageRecord]:
    return sorted(await store.list_for_tenant(tenant_id=_TENANT), key=lambda r: r.id or 0)


def _by_input(rows: Sequence[TokenUsageRecord], purpose: str) -> TokenUsageRecord:
    [row] = [r for r in rows if r.input_tokens == _INPUT[purpose]]
    return row


# ---------------------------------------------------------------------------
# m1 —— 评审在真图里记账
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_judges_are_metered_inside_a_real_run(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = _Brain(parent_replies=[_call("update_plan", {"goal": "g", "steps": ["s1"]})])
    _patch_providers(monkeypatch, brain)
    usage = InMemoryTokenUsageStore()
    spec = _spec(
        "ai-health-plan",
        "PARENT-PROMPT",
        {"defenses": {"output_judge": "block", "action_screen": "block"}},
    )
    built = await make_agent_builder(
        _secret_store(),
        InMemorySaver(),
        credentials_resolver=_credentials(),
        middleware_env=MiddlewareEnv(token_usage_store=usage),
    )(spec, tenant_id=_TENANT)
    budget = TokenBudget(limit=10_000_000)

    trace_id = await _invoke(built, budget)

    assert {"action_judge", "output_judge"} <= set(brain.purposes)
    rows = await _rows(usage)
    for purpose in ("action_judge", "output_judge"):
        row = _by_input(rows, purpose)
        assert (row.provider, row.model) == _MODEL
        assert row.usage_kind == PLATFORM_OVERHEAD_USAGE_KIND
        assert (row.agent_name, row.tenant_id, row.user_id) == ("ai-health-plan", _TENANT, _USER)
        assert row.trace_id == trace_id
    # 主循环两轮两行照旧,不因评审记账多出或少掉。
    assert [r.input_tokens for r in rows].count(_INPUT["parent"]) == 2
    assert budget.spent == sum(r.input_tokens + r.output_tokens for r in rows)


# ---------------------------------------------------------------------------
# m2 —— 委派路径:子代名下、父 trace、同一个池
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegated_child_calls_are_metered_under_the_child_in_the_parent_trace(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = _Brain(parent_replies=[_call("researcher", {"task": "look it up"})])
    _patch_providers(monkeypatch, brain)
    usage = InMemoryTokenUsageStore()
    env = MiddlewareEnv(token_usage_store=usage)
    child = _spec(
        "researcher",
        "CHILD-PROMPT",
        {"workflow": {"type": "plan_execute"}, "defenses": {"output_judge": "block"}},
    )
    specs = InMemoryAgentSpecStore()
    await specs.create(tenant_id=_TENANT, spec=child, spec_sha256="0" * 64, created_by="test")
    child_builder = make_child_agent_builder(
        spec_store=specs,
        secret_store=_secret_store(),
        checkpointer=InMemorySaver(),
        base_tool_env=ToolEnv(),
        credentials_resolver=_credentials(),
        middleware_env=env,
    )
    parent = _spec(
        "ai-health-plan",
        "PARENT-PROMPT",
        {
            "subagents": [
                {"name": "researcher", "agent_ref": "researcher@1.2.0", "description": "d"}
            ]
        },
    )
    built = await make_agent_builder(
        _secret_store(),
        InMemorySaver(),
        credentials_resolver=_credentials(),
        middleware_env=env,
        tool_env=ToolEnv(child_agent_builder=child_builder),
    )(parent, tenant_id=_TENANT)
    budget = TokenBudget(limit=10_000_000)

    trace_id = await _invoke(built, budget)

    assert {"planner", "child", "output_judge"} <= set(brain.purposes)
    rows = await _rows(usage)
    # #1 规划(Agent 自身模型)与 #8 输出评审(平台开销)都记在子代名下。
    planner = _by_input(rows, "planner")
    judge = _by_input(rows, "output_judge")
    assert (planner.agent_name, planner.usage_kind) == ("researcher", "conversation")
    assert (judge.agent_name, judge.usage_kind) == ("researcher", PLATFORM_OVERHEAD_USAGE_KIND)
    assert _by_input(rows, "child").agent_name == "researcher"
    assert {r.agent_name for r in rows if r.input_tokens == _INPUT["parent"]} == {"ai-health-plan"}
    # 全树同一个 trace、同一个池。
    assert {r.trace_id for r in rows} == {trace_id}
    assert {r.user_id for r in rows} == {_USER}
    assert budget.spent == sum(r.input_tokens + r.output_tokens for r in rows)
