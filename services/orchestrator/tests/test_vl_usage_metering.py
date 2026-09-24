"""B-64 Task 9 —— ``ask_image`` 的看图模型(VL)调用要记账。

测试环境实测:VL 调了十几次,``token_usage`` 里一行都没有,``end`` 帧的
``usage_by_model`` 与 runs usage 接口也没有 VL。主模型的用量由 ``after_llm_call``
链上的 ``TokenUsageMiddleware`` 落行,那条链只在 agent 节点里跑;``ask_image`` 在
工具里直接调 VL 路由,从没经过它。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from expert_work.common.observability import (
    ExpertWorkComponent,
    current_trace_id_hex,
    expert_work_span,
    init_tracing,
)
from expert_work.persistence.token_usage_store import InMemoryTokenUsageStore, TokenUsageRecord
from expert_work.protocol import AgentSpec, ModelSpec, StructuredOutputSpec
from expert_work.protocol.multimodal import ImageRef
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.middleware import LLMStreamStaleError
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import MiddlewareEnv, ToolEnv, build_agent
from orchestrator.llm import LLMRouter, ProviderHandle
from orchestrator.llm.providers._streaming import LLMDelta
from orchestrator.multimodal import InMemoryImageResolver, ResolvedImage
from orchestrator.tools._guards import TOKEN_BUDGET_KEY, TokenBudget
from orchestrator.tools.registry import ToolContext, ToolSpec
from orchestrator.tools.vision import AskImageTool
from orchestrator.vl_metering import SERVED_BY_KEY, VLUsageRecorder, with_served_by

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_USER = UUID("33333333-3333-3333-3333-333333333333")
_THREAD = UUID("22222222-2222-2222-2222-222222222222")
_VL_USAGE = {"input_tokens": 800, "output_tokens": 40, "total_tokens": 840}


@pytest.fixture
def tracing() -> Iterator[None]:
    provider = init_tracing(
        service_name="vl-usage-test",
        env="test",
        span_processor=SimpleSpanProcessor(InMemorySpanExporter()),
    )
    try:
        yield
    finally:
        provider.shutdown()


def _ref(tenant: UUID = _TENANT) -> str:
    return ImageRef(tenant_id=tenant, thread_id=_THREAD, image_id=uuid4(), ext=".png").to_uri()


def _resolver() -> InMemoryImageResolver:
    return InMemoryImageResolver(images={"any": ResolvedImage(media_type="image/png", data=b"PNG")})


@dataclass
class _VL:
    """VL 调用替身:回一条带用量的回答,或抛错。"""

    response: AIMessage = field(
        default_factory=lambda: AIMessage(content="a chart", usage_metadata=_VL_USAGE)
    )
    error: Exception | None = None

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
        on_delta: Callable[[LLMDelta], Awaitable[None]] | None = None,
    ) -> AIMessage:
        del messages, tools, output_schema, on_delta
        if self.error is not None:
            raise self.error
        return self.response


@dataclass
class _Meter:
    calls: list[tuple[AIMessage, UUID, UUID | None]] = field(default_factory=list)

    async def __call__(self, response: AIMessage, *, tenant_id: UUID, user_id: UUID | None) -> None:
        self.calls.append((response, tenant_id, user_id))


async def _rows(store: InMemoryTokenUsageStore) -> list[TokenUsageRecord]:
    return sorted(await store.list_for_tenant(tenant_id=_TENANT), key=lambda r: r.id or 0)


def _recorder(store: InMemoryTokenUsageStore) -> VLUsageRecorder:
    return VLUsageRecorder(
        store=store,
        agent_name="ai-health-plan",
        agent_version="1.2.0",
        usage_kind="conversation",
        models={
            "doubao:doubao-seed-2-1-pro": ("doubao", "doubao-seed-2-1-pro"),
            "qwen:qwen-vl-max": ("qwen", "qwen-vl-max"),
        },
        default=("doubao", "doubao-seed-2-1-pro"),
    )


# ---------------------------------------------------------------------------
# 工具:每次 VL 调用都交给记账回调 + 扣全树 token 池
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_image_hands_each_vl_response_to_the_meter_with_run_identity() -> None:
    vl = _VL()
    meter = _Meter()
    tool = AskImageTool(vl_caller=vl, image_resolver=_resolver(), usage_meter=meter)

    await tool.call(
        {"image_ref": _ref(), "question": "q"},
        ctx=ToolContext(tenant_id=_TENANT, user_id=_USER),
    )

    assert len(meter.calls) == 1
    response, tenant_id, user_id = meter.calls[0]
    assert response is vl.response
    assert (tenant_id, user_id) == (_TENANT, _USER)


@pytest.mark.asyncio
async def test_ask_image_charges_vl_usage_to_the_shared_token_budget() -> None:
    budget = TokenBudget(limit=10_000, spent=5)
    tool = AskImageTool(vl_caller=_VL(), image_resolver=_resolver())

    await tool.call(
        {"image_ref": _ref(), "question": "q"},
        ctx=ToolContext(tenant_id=_TENANT, token_budget=budget),
    )

    assert budget.spent == 5 + 800 + 40


@pytest.mark.asyncio
async def test_failed_vl_call_charges_and_meters_nothing() -> None:
    # 与主模型一致:调用抛错(含取消)时既不落行也不扣池。
    budget = TokenBudget(limit=10_000)
    meter = _Meter()
    tool = AskImageTool(
        vl_caller=_VL(error=RuntimeError("vl down")), image_resolver=_resolver(), usage_meter=meter
    )

    with pytest.raises(RuntimeError, match="vl down"):
        await tool.call(
            {"image_ref": _ref(), "question": "q"},
            ctx=ToolContext(tenant_id=_TENANT, token_budget=budget),
        )

    assert meter.calls == []
    assert budget.spent == 0


# ---------------------------------------------------------------------------
# 记账:落在实际应答的模型名下
# ---------------------------------------------------------------------------


def _served(key: str | None) -> AIMessage:
    meta = {SERVED_BY_KEY: key} if key is not None else {}
    return AIMessage(content="x", usage_metadata=_VL_USAGE, response_metadata=meta)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("qwen:qwen-vl-max", ("qwen", "qwen-vl-max")),
        # 多 key 句柄带 ``#序号``,记账按 group 归到同一个模型。
        ("qwen:qwen-vl-max#1", ("qwen", "qwen-vl-max")),
        # 没有盖章(不经过路由的替身)→ VL 主模型。
        (None, ("doubao", "doubao-seed-2-1-pro")),
    ],
)
async def test_recorder_writes_one_row_under_the_model_that_answered(
    tracing: None, key: str | None, expected: tuple[str, str]
) -> None:
    store = InMemoryTokenUsageStore()
    with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "run"):
        trace_id = current_trace_id_hex()
        await _recorder(store)(_served(key), tenant_id=_TENANT, user_id=_USER)

    rows = await _rows(store)
    assert len(rows) == 1
    row = rows[0]
    assert (row.provider, row.model) == expected
    assert row.usage_kind == "conversation"
    assert (row.agent_name, row.agent_version) == ("ai-health-plan", "1.2.0")
    assert (row.tenant_id, row.user_id) == (_TENANT, _USER)
    assert (row.input_tokens, row.output_tokens) == (800, 40)
    assert trace_id is not None and row.trace_id == trace_id


@pytest.mark.asyncio
async def test_recorder_writes_nothing_when_the_provider_reported_no_usage() -> None:
    # 与主模型未命中缓存时同一条规则:上游没报用量就不落行。
    store = InMemoryTokenUsageStore()
    await _recorder(store)(AIMessage(content="x"), tenant_id=_TENANT, user_id=None)
    assert await _rows(store) == []


# ---------------------------------------------------------------------------
# 盖章:路由上实际应答的句柄
# ---------------------------------------------------------------------------


@dataclass
class _Provider:
    response: AIMessage | None = None
    error: Exception | None = None

    async def complete(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AIMessage:
        del messages, tools, output_schema
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


@pytest.mark.asyncio
async def test_router_response_carries_the_fallback_handle_that_answered() -> None:
    router = LLMRouter(
        providers=[
            ProviderHandle(
                provider=_Provider(error=LLMStreamStaleError("hung")),
                key="doubao:doubao-seed-2-1-pro",
                group="doubao:doubao-seed-2-1-pro",
            ),
            ProviderHandle(
                provider=_Provider(response=AIMessage(content="ok")),
                key="qwen:qwen-vl-max#1",
                group="qwen:qwen-vl-max",
            ),
        ],
        around_llm_chain=with_served_by(None),
    )

    response = await router(messages=[HumanMessage(content="q")], tools=[])

    assert response.response_metadata[SERVED_BY_KEY] == "qwen:qwen-vl-max#1"


# ---------------------------------------------------------------------------
# 端到端:build_agent 建出来的 agent 真跑一轮 ask_image
# ---------------------------------------------------------------------------

_KEY = "expert-work/dev/llm/any"
_MAIN_USAGE = [
    {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
    {"input_tokens": 200, "output_tokens": 20, "total_tokens": 220},
]


def _vision_spec(name: str = "ai-health-plan") -> AgentSpec:
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": name, "version": "1.2.0", "tenant": "t"},
            "spec": {
                "tenant_config": {},
                "model": {"provider": "anthropic", "name": "claude-sonnet-4-6"},
                "vision": {
                    "model": {"provider": "openai", "name": "gpt-4o"},
                    "fallbacks": [{"provider": "qwen", "name": "qwen-vl-max"}],
                },
                "system_prompt": {"template": "t"},
                "sandbox": {
                    "resources": {"cpu": "1.0", "memory": "1Gi"},
                    "network": {"egress": "proxy", "allowlist": []},
                    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
                },
            },
        }
    )


async def _any_key(provider: str) -> list[str]:
    del provider
    return [f"secret://{_KEY}"]


@dataclass
class _Scripted:
    responses: list[AIMessage]
    calls: int = 0

    async def complete(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AIMessage:
        del messages, tools, output_schema
        self.calls += 1
        return self.responses[self.calls - 1]


def _fake_providers(image_ref: str) -> dict[str, _Scripted | _Provider]:
    ask = {
        "name": "ask_image",
        "args": {"image_ref": image_ref, "question": "what trend?"},
        "id": "call-1",
        "type": "tool_call",
    }
    return {
        "claude-sonnet-4-6": _Scripted(
            responses=[
                AIMessage(content="", tool_calls=[ask], usage_metadata=_MAIN_USAGE[0]),
                AIMessage(content="done", usage_metadata=_MAIN_USAGE[1]),
            ]
        ),
        # VL 主模型卡死 → 备用 qwen 接管;账要记在 qwen 名下。
        "gpt-4o": _Provider(error=LLMStreamStaleError("hung")),
        "qwen-vl-max": _Provider(response=AIMessage(content="rising", usage_metadata=_VL_USAGE)),
    }


@pytest.mark.asyncio
async def test_built_agent_meters_ask_image_under_the_run_trace(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_ref = _ref()
    fakes = _fake_providers(image_ref)

    def _fake_build_provider(entry: ModelSpec, api_key: str, **kwargs: Any) -> Any:
        del api_key, kwargs
        return fakes[entry.name]

    monkeypatch.setattr("orchestrator.agent_factory._build_provider", _fake_build_provider)
    store = InMemoryTokenUsageStore()
    budget = TokenBudget(limit=1_000_000)

    async with make_checkpointer("memory") as cp:
        built = await build_agent(
            _vision_spec(),
            secret_store=LocalDevSecretStore.from_mapping({_KEY: "sk-test"}),
            checkpointer=cp,
            provider_key_resolver=_any_key,
            tool_env=ToolEnv(image_resolver=_resolver()),
            middleware_env=MiddlewareEnv(token_usage_store=store),
        )
        cfg: RunnableConfig = {
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
            state = await built.graph.ainvoke(
                {"messages": [HumanMessage(content="look")], "step_count": 0, "max_steps": 5},
                config=cfg,
            )

    tool_msgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    # 工具输出会被 spotlight 围栏包起来,只看 VL 的回答确实回到了主模型。
    assert len(tool_msgs) == 1 and "rising" in str(tool_msgs[0].content)
    rows = await _rows(store)
    main_rows = [r for r in rows if r.model == "claude-sonnet-4-6"]
    vl_rows = [r for r in rows if r.model != "claude-sonnet-4-6"]
    # 回归:主模型仍是一次调用一行,不因 VL 记账多出或少掉。
    assert len(main_rows) == 2
    assert len(vl_rows) == 1
    vl = vl_rows[0]
    assert (vl.provider, vl.model) == ("qwen", "qwen-vl-max")
    assert vl.usage_kind == "conversation"
    assert (vl.agent_name, vl.agent_version) == ("ai-health-plan", "1.2.0")
    assert (vl.tenant_id, vl.user_id) == (_TENANT, _USER)
    assert trace_id is not None
    assert {r.trace_id for r in rows} == {trace_id}
    # ``end`` 帧 / runs usage 接口的数据源:按 trace 汇总、只取 conversation。
    totals = await store.totals_by_trace_ids([trace_id], usage_kinds=("conversation",))
    by_model = {(b.provider, b.model): b for b in totals[trace_id].by_model}
    assert set(by_model) == {("anthropic", "claude-sonnet-4-6"), ("qwen", "qwen-vl-max")}
    assert (by_model[("qwen", "qwen-vl-max")].input_tokens,) == (800,)
    # B3 全树 token 池:主模型两次 + VL 一次。
    assert budget.spent == 110 + 220 + 840
