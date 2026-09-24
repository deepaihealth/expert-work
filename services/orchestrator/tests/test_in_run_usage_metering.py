"""B-104 —— run 内除主循环外的模型调用也要记账(落 ``token_usage`` + 扣全树 token 池)。

此前只有 agent 节点(``after_llm_call`` 链上的 ``TokenUsageMiddleware``)与看图记账;
任务规划、反思评判、对话压缩摘要、记忆读时校验 / 查询改写 / 写回抽取 / 写回归并在
``token_usage`` 里一行都没有,token 熔断也看不见它们。这里用 ``build_agent`` 建出的真 agent
真跑一轮,按「调用一次 → 一行、kind 对、trace 对、预算增加」逐个调用点核对,并钉住主循环
行数不变(不重复计)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import var_child_runnable_config
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from expert_work.common.observability import (
    ExpertWorkComponent,
    current_trace_id_hex,
    expert_work_span,
    init_tracing,
)
from expert_work.persistence import InMemoryKnowledgeStore, InMemoryMemoryStore
from expert_work.persistence.token_usage_store import (
    PLATFORM_OVERHEAD_USAGE_KIND,
    InMemoryTokenUsageStore,
    TokenUsageRecord,
)
from expert_work.protocol import (
    AgentSpec,
    KnowledgeChunk,
    MemoryItem,
    ModelSpec,
    StructuredOutputSpec,
)
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import MemoryEnv, MiddlewareEnv, ToolEnv, build_agent
from orchestrator.llm import FakeEmbedder
from orchestrator.llm.providers._streaming import LLMDelta
from orchestrator.tools import KnowledgeRetriever
from orchestrator.tools._guards import TOKEN_BUDGET_KEY, TokenBudget
from orchestrator.tools.registry import ToolSpec
from orchestrator.usage_metering import (
    MeteredLLMCaller,
    UsageIdentity,
    UsageMeter,
    current_usage_identity,
)

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_USER = UUID("33333333-3333-3333-3333-333333333333")
_KEY = "expert-work/dev/llm/any"
_MAIN = ("anthropic", "claude-sonnet-4-6")

#: 每类调用各用一个独特的 input_tokens,落下来的行按它认出是哪个调用点。
_PURPOSE_INPUT = {
    "main": 100,
    "planner": 11,
    "reflect": 12,
    "compress": 13,
    "verify": 14,
    "rewrite": 15,
    "extract": 16,
    "reconcile": 17,
}
#: 系统提示里的特征句 → 调用点。
_SYSTEM_MARKERS = (
    ("You are a planning module", "planner"),
    ("You are a reflection module", "reflect"),
    ("You are a context compressor", "compress"),
    ("You maintain a running background summary", "compress"),
    ("You are a memory relevance filter", "verify"),
    ("You rewrite the user's latest message", "rewrite"),
    ("You are a memory extraction module", "extract"),
    ("You reconcile newly extracted memories", "reconcile"),
)
_REPLIES = {
    "planner": '{"goal": "g", "steps": ["s1"]}',
    "reflect": '{"verdict": "accept", "critique": "ok"}',
    "compress": "summary",
    "verify": "[0]",
    "rewrite": "tea preference",
    "extract": '{"memories": [{"kind": "fact", "content": "user likes tea"}]}',
    "reconcile": '{"operations": []}',
}


def _usage(purpose: str) -> dict[str, int]:
    n = _PURPOSE_INPUT[purpose]
    return {"input_tokens": n, "output_tokens": 1, "total_tokens": n + 1}


def _purpose_of(messages: Sequence[BaseMessage]) -> str:
    first = messages[0] if messages else None
    if isinstance(first, SystemMessage) and isinstance(first.content, str):
        for marker, purpose in _SYSTEM_MARKERS:
            if first.content.startswith(marker):
                return purpose
    return "main"


@dataclass
class _Brain:
    """按系统提示认出调用点、回对应答案的 provider 替身;主循环调用可以脚本化。"""

    main_replies: list[AIMessage] = field(default_factory=list)
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
        if purpose == "main":
            if self.main_replies:
                return self.main_replies.pop(0)
            return AIMessage(content="done", usage_metadata=_usage("main"))
        return AIMessage(content=_REPLIES[purpose], usage_metadata=_usage(purpose))


@pytest.fixture
def tracing() -> Iterator[None]:
    provider = init_tracing(
        service_name="in-run-usage-test",
        env="test",
        span_processor=SimpleSpanProcessor(InMemorySpanExporter()),
    )
    try:
        yield
    finally:
        provider.shutdown()


def _spec(extra: dict[str, Any]) -> AgentSpec:
    doc: dict[str, Any] = {
        "apiVersion": "expert_work.io/v1",
        "kind": "Agent",
        "metadata": {"name": "ai-health-plan", "version": "1.2.0", "tenant": "t"},
        "spec": {
            "tenant_config": {},
            "model": {"provider": _MAIN[0], "name": _MAIN[1]},
            "system_prompt": {"template": "t"},
            "sandbox": {
                "resources": {"cpu": "1.0", "memory": "1Gi"},
                "network": {"egress": "proxy", "allowlist": []},
                "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
            },
        },
    }
    doc["spec"].update(extra)
    return AgentSpec.model_validate(doc)


async def _any_key(provider: str) -> list[str]:
    del provider
    return [f"secret://{_KEY}"]


def _patch_providers(monkeypatch: pytest.MonkeyPatch, brain: _Brain) -> None:
    def _fake_build_provider(entry: ModelSpec, api_key: str, **kwargs: Any) -> Any:
        del entry, api_key, kwargs
        return brain

    monkeypatch.setattr("orchestrator.agent_factory._build_provider", _fake_build_provider)


def _config(budget: TokenBudget) -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": str(uuid4()),
            "tenant_id": str(_TENANT),
            "user_id": str(_USER),
            "run_id": str(uuid4()),
            TOKEN_BUDGET_KEY: budget,
        }
    }


@dataclass
class _Run:
    rows: list[TokenUsageRecord]
    trace_id: str
    budget: TokenBudget
    brain: _Brain

    def by_purpose(self, purpose: str) -> list[TokenUsageRecord]:
        return [r for r in self.rows if r.input_tokens == _PURPOSE_INPUT[purpose]]


async def _run(
    spec: AgentSpec,
    brain: _Brain,
    *,
    messages: list[BaseMessage] | None = None,
    memory_env: MemoryEnv | None = None,
    tool_env: ToolEnv | None = None,
) -> _Run:
    store = InMemoryTokenUsageStore()
    budget = TokenBudget(limit=10_000_000)
    async with make_checkpointer("memory") as cp:
        built = await build_agent(
            spec,
            secret_store=LocalDevSecretStore.from_mapping({_KEY: "sk-test"}),
            checkpointer=cp,
            provider_key_resolver=_any_key,
            middleware_env=MiddlewareEnv(token_usage_store=store),
            memory_env=memory_env,
            tool_env=tool_env,
        )
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "run"):
            trace_id = current_trace_id_hex()
            await built.graph.ainvoke(
                {
                    "messages": messages or [HumanMessage(content="plan my week")],
                    "step_count": 0,
                    "max_steps": 5,
                },
                config=_config(budget),
            )
    assert trace_id is not None
    rows = sorted(await store.list_for_tenant(tenant_id=_TENANT), key=lambda r: r.id or 0)
    return _Run(rows=rows, trace_id=trace_id, budget=budget, brain=brain)


def _assert_row(
    run: _Run, purpose: str, *, model: tuple[str, str], kind: str = "conversation"
) -> None:
    rows = run.by_purpose(purpose)
    assert len(rows) == 1, (purpose, [(r.model, r.input_tokens) for r in run.rows])
    row = rows[0]
    assert (row.provider, row.model) == model
    assert row.usage_kind == kind
    assert (row.agent_name, row.agent_version) == ("ai-health-plan", "1.2.0")
    assert (row.tenant_id, row.user_id) == (_TENANT, _USER)
    assert row.trace_id == run.trace_id


def _spent(purposes: Sequence[str]) -> int:
    return sum(_PURPOSE_INPUT[p] + 1 for p in purposes)


# ---------------------------------------------------------------------------
# #1 任务规划 / #2 反思评判 —— 各自记在路由选中的模型名下
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_planner_and_reflection_calls_are_metered_under_their_routed_models(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = _Brain()
    _patch_providers(monkeypatch, brain)
    spec = _spec(
        {
            "workflow": {"type": "plan_execute"},
            "reflection": {"budget": 1},
            "routing": {
                "rules": [
                    {"when": "planning", "model": {"provider": "openai", "name": "gpt-4o"}},
                    {"when": "reflection", "model": {"provider": "qwen", "name": "qwen-max"}},
                ]
            },
        }
    )

    run = await _run(spec, brain)

    assert sorted(brain.purposes) == ["main", "planner", "reflect"]
    _assert_row(run, "planner", model=("openai", "gpt-4o"))
    _assert_row(run, "reflect", model=("qwen", "qwen-max"))
    # 主循环不重复计:一次调用一行。
    _assert_row(run, "main", model=_MAIN)
    assert len(run.rows) == 3
    assert run.budget.spent == _spent(brain.purposes)


# ---------------------------------------------------------------------------
# #3 对话压缩摘要
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compression_summary_call_is_metered(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = _Brain()
    _patch_providers(monkeypatch, brain)
    spec = _spec(
        {
            "model": {"provider": _MAIN[0], "name": _MAIN[1], "context_window": 4000},
            "policies": {
                "context_compression": {
                    "enabled": True,
                    "threshold_pct": 0.5,
                    "head_keep": 1,
                    "tail_keep": 1,
                }
            },
        }
    )
    filler = "lorem ipsum dolor sit amet " * 120
    history: list[BaseMessage] = []
    for i in range(6):
        history.append(HumanMessage(content=f"turn {i}: {filler}"))
        history.append(AIMessage(content=f"answer {i}: {filler}"))
    history.append(HumanMessage(content="now summarise where we are"))

    run = await _run(spec, brain, messages=history)

    assert "compress" in brain.purposes
    _assert_row(run, "compress", model=_MAIN)
    _assert_row(run, "main", model=_MAIN)
    assert len(run.rows) == len(brain.purposes)
    assert run.budget.spent == _spent(brain.purposes)


# ---------------------------------------------------------------------------
# #4-#7 记忆:读时校验 / 查询改写 / 写回抽取 / 写回归并;#10 记忆重排序带平台身份
# ---------------------------------------------------------------------------


@dataclass
class _SpyReranker:
    identities: list[UsageIdentity | None] = field(default_factory=list)

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        del query, tenant_id
        self.identities.append(current_usage_identity())
        return list(range(len(documents)))[:top_k]


async def _seeded_memory() -> InMemoryMemoryStore:
    store = InMemoryMemoryStore()
    [vec] = await FakeEmbedder(dim=16).embed(["user likes tea"], tenant_id=_TENANT)
    await store.write(
        [
            MemoryItem(
                id=uuid4(),
                tenant_id=_TENANT,
                user_id=_USER,
                kind="fact",
                content="user likes tea",
                embedding=vec,
            )
        ]
    )
    return store


def _assert_platform_identity(identity: UsageIdentity | None) -> None:
    assert identity is not None
    assert identity.usage_kind == PLATFORM_OVERHEAD_USAGE_KIND
    assert (identity.agent_name, identity.agent_version) == ("ai-health-plan", "1.2.0")
    assert identity.store is not None


@pytest.mark.asyncio
async def test_memory_calls_are_metered_and_rerank_carries_the_platform_identity(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = _Brain()
    _patch_providers(monkeypatch, brain)
    spec = _spec(
        {
            "memory": {
                "long_term": {
                    "verify_reads": True,
                    "rewrite_reads": True,
                    "write_back": True,
                    "reconcile_writes": True,
                }
            }
        }
    )
    reranker = _SpyReranker()
    memory_env = MemoryEnv(
        store=await _seeded_memory(), embedder=FakeEmbedder(dim=16), reranker=reranker
    )

    run = await _run(spec, brain, memory_env=memory_env)

    assert sorted(brain.purposes) == sorted(["rewrite", "verify", "main", "extract", "reconcile"])
    for purpose in ("rewrite", "verify", "extract", "reconcile"):
        _assert_row(run, purpose, model=_MAIN)
    _assert_row(run, "main", model=_MAIN)
    assert len(run.rows) == 5
    assert run.budget.spent == _spent(brain.purposes)
    assert len(reranker.identities) == 1
    _assert_platform_identity(reranker.identities[0])


# ---------------------------------------------------------------------------
# #10 知识库重排序带平台身份(真正落行由控制面重排序器的 LLM 分支做)
# ---------------------------------------------------------------------------


class _FixedEmbedder:
    async def embed(self, texts: Sequence[str], *, tenant_id: UUID) -> list[tuple[float, ...]]:
        del tenant_id
        return [(1.0, 0.0) for _ in texts]


async def _knowledge_env(reranker: _SpyReranker) -> ToolEnv:
    store = InMemoryKnowledgeStore()
    base = await store.create_base(tenant_id=_TENANT, name="kb")
    document = await store.upsert_document(tenant_id=_TENANT, kb_id=base.id, filename="doc.pdf")
    await store.replace_chunks(
        tenant_id=_TENANT,
        document_id=document.id,
        chunks=[
            KnowledgeChunk(
                id=uuid4(),
                tenant_id=_TENANT,
                kb_id=base.id,
                document_id=document.id,
                chunk_index=0,
                content="the deductible is 500 dollars",
                embedding=(1.0, 0.0),
            )
        ],
    )
    return ToolEnv(
        knowledge_retriever=KnowledgeRetriever(
            store=store, embedder=_FixedEmbedder(), reranker=reranker
        )
    )


@pytest.mark.asyncio
async def test_knowledge_rerank_carries_the_platform_identity(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    search = {
        "name": "knowledge_search",
        "args": {"query": "deductible"},
        "id": "call-1",
        "type": "tool_call",
    }
    brain = _Brain(
        main_replies=[AIMessage(content="", tool_calls=[search], usage_metadata=_usage("main"))]
    )
    _patch_providers(monkeypatch, brain)
    reranker = _SpyReranker()
    spec = _spec({"knowledge": {"knowledge_base_refs": ["kb"]}})

    run = await _run(spec, brain, tool_env=await _knowledge_env(reranker))

    assert len(reranker.identities) == 1
    _assert_platform_identity(reranker.identities[0])
    # 主循环两轮两行,重排序替身不调模型,不多出行。
    assert [r.input_tokens for r in run.rows] == [100, 100]


# ---------------------------------------------------------------------------
# 组件本身:成功才记;run 外不记;没有存储只扣池
# ---------------------------------------------------------------------------


@dataclass
class _Caller:
    response: AIMessage = field(
        default_factory=lambda: AIMessage(content="x", usage_metadata=_usage("planner"))
    )
    error: Exception | None = None
    kwargs: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
        on_delta: Callable[[LLMDelta], Awaitable[None]] | None = None,
    ) -> AIMessage:
        del messages, tools
        self.kwargs.append({"output_schema": output_schema, "on_delta": on_delta})
        if self.error is not None:
            raise self.error
        return self.response


def _meter(store: InMemoryTokenUsageStore | None) -> UsageMeter:
    return UsageIdentity(
        store=store, agent_name="a", agent_version="1", usage_kind="conversation"
    ).meter(default=("openai", "gpt-4o"))


@pytest.mark.asyncio
async def test_metered_caller_charges_budget_and_writes_a_row_inside_a_run() -> None:
    store = InMemoryTokenUsageStore()
    budget = TokenBudget(limit=1_000, spent=3)
    caller = MeteredLLMCaller(inner=_Caller(), meter=_meter(store))
    token = var_child_runnable_config.set(_config(budget))
    try:
        await caller(messages=[HumanMessage(content="q")], tools=[])
    finally:
        var_child_runnable_config.reset(token)

    assert budget.spent == 3 + 12
    rows = await store.list_for_tenant(tenant_id=_TENANT)
    assert [(r.provider, r.model, r.user_id) for r in rows] == [("openai", "gpt-4o", _USER)]


@pytest.mark.asyncio
async def test_metered_caller_records_nothing_when_the_call_fails() -> None:
    store = InMemoryTokenUsageStore()
    budget = TokenBudget(limit=1_000)
    caller = MeteredLLMCaller(inner=_Caller(error=RuntimeError("down")), meter=_meter(store))
    token = var_child_runnable_config.set(_config(budget))
    try:
        with pytest.raises(RuntimeError, match="down"):
            await caller(messages=[HumanMessage(content="q")], tools=[])
    finally:
        var_child_runnable_config.reset(token)

    assert budget.spent == 0
    assert await store.list_for_tenant(tenant_id=_TENANT) == []


@pytest.mark.asyncio
async def test_metered_caller_outside_a_run_passes_through_and_records_nothing() -> None:
    store = InMemoryTokenUsageStore()
    inner = _Caller()
    caller = MeteredLLMCaller(inner=inner, meter=_meter(store))

    response = await caller(messages=[HumanMessage(content="q")], tools=[])

    assert response is inner.response
    assert await store.list_for_tenant(tenant_id=_TENANT) == []


@pytest.mark.asyncio
async def test_metered_caller_without_a_store_still_charges_the_budget() -> None:
    budget = TokenBudget(limit=1_000)
    caller = MeteredLLMCaller(inner=_Caller(), meter=_meter(None))
    token = var_child_runnable_config.set(_config(budget))
    try:
        await caller(messages=[HumanMessage(content="q")], tools=[])
    finally:
        var_child_runnable_config.reset(token)

    assert budget.spent == 12
