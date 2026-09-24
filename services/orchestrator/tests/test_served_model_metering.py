"""B-102 / B-103 —— ``token_usage`` 按**实际应答**的模型记账,worker 帧同口径。

B-102:主循环原来一律记配置的主模型名,备用模型接管时也按主模型计价(测试环境 30 天
7.7% 的主 Agent 回复由非主模型回答),而看图(B-64 Task 9)记的是实际应答者 —— 同一张
表两种含义。现在全部按「路由里被选中的那个模型条目的**配置**名」记,不用厂商回显的
``response_metadata.model_name``(会带别名)。

B-103:worker 事件帧的 ``usage_by_model`` 原来按 worker 主模型单桶汇总、不含看图;现在
与 run 级同口径按实际模型分桶。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from expert_work.common.observability import (
    ExpertWorkComponent,
    current_trace_id_hex,
    expert_work_span,
    init_tracing,
)
from expert_work.persistence.token_usage_store import (
    InMemoryTokenUsageStore,
    TokenUsageRecord,
)
from expert_work.protocol import AgentSpec, ModelSpec, StructuredOutputSpec
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.middleware import (
    LLMStreamStaleError,
    MiddlewareContext,
    TokenUsageMiddleware,
)
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import MiddlewareEnv, build_agent
from orchestrator.sse import _to_jsonable
from orchestrator.tools._guards import TokenBudget
from orchestrator.tools.registry import ToolSpec
from orchestrator.usage_metering import SERVED_BY_KEY, ServedModelResolver

from .test_in_run_usage_metering import (
    _KEY,
    _MAIN,
    _PURPOSE_INPUT,
    _TENANT,
    _any_key,
    _Brain,
    _config,
    _spec,
)


@pytest.fixture
def tracing() -> Iterator[None]:
    provider = init_tracing(
        service_name="served-model-usage-test",
        env="test",
        span_processor=SimpleSpanProcessor(InMemorySpanExporter()),
    )
    try:
        yield
    finally:
        provider.shutdown()


@dataclass
class _Hung:
    """卡死的模型:每次调用记下它收到的思考档位,然后抛可换备用的错。"""

    entry: ModelSpec
    efforts: list[str | None] = field(default_factory=list)

    async def complete(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AIMessage:
        del messages, tools, output_schema
        self.efforts.append(self.entry.effort)
        raise LLMStreamStaleError("hung")


@dataclass
class _Echoing:
    """正常应答,但厂商回显的 ``model_name`` 是个别名(与配置名不同)。"""

    brain: _Brain
    alias: str

    async def complete(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AIMessage:
        response = await self.brain.complete(
            messages=messages, tools=tools, output_schema=output_schema
        )
        return response.model_copy(update={"response_metadata": {"model_name": self.alias}})


def _patch(monkeypatch: pytest.MonkeyPatch, brain: _Brain, *, hung: set[str]) -> list[_Hung]:
    """``hung`` 里的模型卡死;其余模型由 ``brain`` 应答,厂商回显 ``<配置名>-latest``。"""
    hung_providers: list[_Hung] = []

    def _fake_build_provider(entry: ModelSpec, api_key: str, **kwargs: Any) -> Any:
        del api_key, kwargs
        if entry.name in hung:
            provider = _Hung(entry=entry)
            hung_providers.append(provider)
            return provider
        return _Echoing(brain=brain, alias=f"{entry.name}-latest")

    monkeypatch.setattr("orchestrator.agent_factory._build_provider", _fake_build_provider)
    return hung_providers


@dataclass
class _Run:
    rows: list[TokenUsageRecord]
    by_model: dict[tuple[str | None, str], int]

    def model_of(self, purpose: str) -> tuple[str | None, str]:
        rows = [r for r in self.rows if r.input_tokens == _PURPOSE_INPUT[purpose]]
        assert len(rows) == 1, (purpose, [(r.model, r.input_tokens) for r in self.rows])
        return rows[0].provider, rows[0].model


async def _run(spec: AgentSpec, *, step_count: int = 0, max_steps: int = 5) -> _Run:
    store = InMemoryTokenUsageStore()
    async with make_checkpointer("memory") as cp:
        built = await build_agent(
            spec,
            secret_store=LocalDevSecretStore.from_mapping({_KEY: "sk-test"}),
            checkpointer=cp,
            provider_key_resolver=_any_key,
            middleware_env=MiddlewareEnv(token_usage_store=store),
        )
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "run"):
            trace_id = current_trace_id_hex()
            await built.graph.ainvoke(
                {
                    "messages": [HumanMessage(content="plan my week")],
                    "step_count": step_count,
                    "max_steps": max_steps,
                },
                config=_config(TokenBudget(limit=10_000_000)),
            )
    assert trace_id is not None
    rows = sorted(await store.list_for_tenant(tenant_id=_TENANT), key=lambda r: r.id or 0)
    # ``end`` 帧 / runs usage 接口的数据源:按 trace 汇总、只取 conversation。
    totals = await store.totals_by_trace_ids([trace_id], usage_kinds=("conversation",))
    by_model = {(b.provider, b.model): b.input_tokens for b in totals[trace_id].by_model}
    return _Run(rows=rows, by_model=by_model)


_QWEN = ("qwen", "qwen-max")


# ---------------------------------------------------------------------------
# B-102 主循环
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_answer_is_recorded_under_the_fallback_config_name(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主模型卡死 → 备用接管:行记备用的**配置名**(不是厂商回显的别名),端帧的数据源
    里出现备用桶、主模型桶不含这次调用。"""
    brain = _Brain()
    hung = _patch(monkeypatch, brain, hung={_MAIN[1]})
    spec = _spec(
        {
            "model": {
                "provider": _MAIN[0],
                "name": _MAIN[1],
                "fallback": [{"provider": _QWEN[0], "name": _QWEN[1]}],
            }
        }
    )

    run = await _run(spec)

    assert hung and hung[0].efforts, "主模型应当先被试过"
    assert brain.purposes == ["main"]
    # 回归:主循环仍是一次调用一行。
    assert len(run.rows) == 1
    assert run.model_of("main") == _QWEN
    assert run.by_model == {_QWEN: _PURPOSE_INPUT["main"]}


@pytest.mark.asyncio
async def test_vendor_echoed_alias_is_not_what_gets_recorded(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = _Brain()
    _patch(monkeypatch, brain, hung=set())

    run = await _run(_spec({}))

    assert run.model_of("main") == _MAIN
    assert run.by_model == {_MAIN: _PURPOSE_INPUT["main"]}


@pytest.mark.asyncio
async def test_escalated_turn_answered_by_the_fallback_is_recorded_under_the_fallback(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CM-9 思考升档:升档路由上主模型卡死、备用接管 → 记备用。"""
    brain = _Brain()
    hung = _patch(monkeypatch, brain, hung={_MAIN[1]})
    spec = _spec(
        {
            "model": {
                "provider": _MAIN[0],
                "name": _MAIN[1],
                "effort": "low",
                "fallback": [{"provider": _QWEN[0], "name": _QWEN[1]}],
            }
        }
    )

    # step 3/4 ≥ 75% → 预算信号,这一轮走升档路由。
    run = await _run(spec, step_count=3, max_steps=4)

    tried = [effort for provider in hung for effort in provider.efforts]
    assert tried == ["medium"], "这一轮应当由升档路由(effort low → medium)先试主模型"
    assert run.model_of("main") == _QWEN
    assert run.by_model == {_QWEN: _PURPOSE_INPUT["main"]}


@pytest.mark.asyncio
async def test_routed_planning_and_reflection_are_recorded_under_who_answered(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1 规划 / #2 反思走 routing 规则选中的模型;它们卡死时各自的备用接管 → 各记备用。"""
    brain = _Brain()
    _patch(monkeypatch, brain, hung={"gpt-4o", "qwen-max"})
    spec = _spec(
        {
            "workflow": {"type": "plan_execute"},
            "reflection": {"budget": 1},
            "routing": {
                "rules": [
                    {
                        "when": "planning",
                        "model": {
                            "provider": "openai",
                            "name": "gpt-4o",
                            "fallback": [{"provider": "deepseek", "name": "deepseek-v4-flash"}],
                        },
                    },
                    {
                        "when": "reflection",
                        "model": {
                            "provider": "qwen",
                            "name": "qwen-max",
                            "fallback": [{"provider": "glm", "name": "glm-5.3"}],
                        },
                    },
                ]
            },
        }
    )

    run = await _run(spec)

    assert sorted(brain.purposes) == ["main", "planner", "reflect"]
    assert run.model_of("planner") == ("deepseek", "deepseek-v4-flash")
    assert run.model_of("reflect") == ("glm", "glm-5.3")
    assert run.model_of("main") == _MAIN
    assert len(run.rows) == 3


# ---------------------------------------------------------------------------
# B-102 记账中间件:解析器 / 缓存命中 / tap
# ---------------------------------------------------------------------------


def _middleware(store: InMemoryTokenUsageStore, **kwargs: Any) -> TokenUsageMiddleware:
    return TokenUsageMiddleware(
        store=store,
        agent_name="a",
        agent_version="1",
        model=_MAIN[1],
        provider=_MAIN[0],
        served_by=ServedModelResolver(
            default=_MAIN, models={f"{_MAIN[0]}:{_MAIN[1]}": _MAIN, "qwen:qwen-max": _QWEN}
        ),
        **kwargs,
    )


def _stamped(key: str | None, usage: dict[str, Any] | None) -> AIMessage:
    meta: dict[str, Any] = {"model_name": "vendor-alias"}
    if key is not None:
        meta[SERVED_BY_KEY] = key
    return AIMessage(content="x", usage_metadata=usage, response_metadata=meta)


async def _record(
    middleware: TokenUsageMiddleware, response: AIMessage, *, cache_hit: bool = False
) -> None:
    async def _noop(_ctx: MiddlewareContext) -> None:
        return None

    payload: dict[str, Any] = {"tenant_id": _TENANT, "response": response, "cache_hit": cache_hit}
    await middleware(MiddlewareContext(payload=payload), _noop)


_U = {"input_tokens": 7, "output_tokens": 1, "total_tokens": 8}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "expected"),
    [("qwen:qwen-max#1", _QWEN), (None, _MAIN), ("unknown:model", _MAIN)],
)
async def test_middleware_records_the_stamped_model(
    key: str | None, expected: tuple[str, str]
) -> None:
    store = InMemoryTokenUsageStore()
    await _record(_middleware(store), _stamped(key, _U))
    rows = await store.list_for_tenant(tenant_id=_TENANT)
    assert [(r.provider, r.model) for r in rows] == [expected]


@pytest.mark.asyncio
async def test_cache_hit_row_stays_under_the_configured_model() -> None:
    """缓存命中规则不变:没有模型应答,全 0 行照旧记配置的主模型。"""
    store = InMemoryTokenUsageStore()
    await _record(_middleware(store), _stamped("qwen:qwen-max", None), cache_hit=True)
    rows = await store.list_for_tenant(tenant_id=_TENANT)
    assert [(r.provider, r.model, r.input_tokens) for r in rows] == [(*_MAIN, 0)]


@pytest.mark.asyncio
async def test_second_level_fallback_is_recorded_under_its_own_name(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """备用的备用(嵌套备用树)接管 → 记第二层备用的配置名。"""
    brain = _Brain()
    _patch(monkeypatch, brain, hung={_MAIN[1], _QWEN[1]})
    spec = _spec(
        {
            "model": {
                "provider": _MAIN[0],
                "name": _MAIN[1],
                "fallback": [
                    {
                        "provider": _QWEN[0],
                        "name": _QWEN[1],
                        "fallback": [{"provider": "kimi", "name": "kimi-k3"}],
                    }
                ],
            }
        }
    )

    run = await _run(spec)

    assert run.model_of("main") == ("kimi", "kimi-k3")
    assert run.by_model == {("kimi", "kimi-k3"): _PURPOSE_INPUT["main"]}


# ---------------------------------------------------------------------------
# 盖章只给记账:不进 state / checkpoint / updates 帧(对外 legacy 流与 run_event 的来源)
# ---------------------------------------------------------------------------


async def _stream_updates(
    spec: AgentSpec, store: InMemoryTokenUsageStore
) -> tuple[list[str], list[BaseMessage], list[BaseMessage]]:
    """跑一轮,返回 ``(updates 帧 JSON, 流结束时的 state 消息, checkpoint 里的消息)``。

    ``updates`` 帧按 ``sse._to_jsonable`` 序列化 —— run_event 落库与对外 legacy 流
    转发的就是这份。
    """
    frames: list[str] = []
    async with make_checkpointer("memory") as cp:
        built = await build_agent(
            spec,
            secret_store=LocalDevSecretStore.from_mapping({_KEY: "sk-test"}),
            checkpointer=cp,
            provider_key_resolver=_any_key,
            middleware_env=MiddlewareEnv(token_usage_store=store),
        )
        config = _config(TokenBudget(limit=10_000_000))
        final: dict[str, Any] = {}
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "run"):
            async for mode, chunk in built.graph.astream(
                {
                    "messages": [HumanMessage(content="plan my week")],
                    "step_count": 0,
                    "max_steps": 5,
                },
                config=config,
                stream_mode=["updates", "values"],
            ):
                if mode == "updates":
                    frames.append(json.dumps(_to_jsonable(chunk)))
                elif isinstance(chunk, dict):
                    final = chunk
        snapshot = await built.graph.aget_state(config)
    return frames, list(final["messages"]), list(snapshot.values["messages"])


def _assert_no_stamp(
    frames: list[str], state: list[BaseMessage], checkpoint: list[BaseMessage]
) -> None:
    assert frames
    assert all(SERVED_BY_KEY not in frame for frame in frames)
    for message in [*state, *checkpoint]:
        assert SERVED_BY_KEY not in message.response_metadata, message


@pytest.mark.asyncio
async def test_served_by_stamp_never_leaves_the_metering(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = _Brain()
    _patch(monkeypatch, brain, hung={_MAIN[1]})
    spec = _spec(
        {
            "model": {
                "provider": _MAIN[0],
                "name": _MAIN[1],
                "fallback": [{"provider": _QWEN[0], "name": _QWEN[1]}],
            }
        }
    )
    store = InMemoryTokenUsageStore()

    frames, state, checkpoint = await _stream_updates(spec, store)

    _assert_no_stamp(frames, state, checkpoint)
    # 厂商回显的别名这类非章元数据照旧保留。
    assert state[-1].response_metadata == {"model_name": "qwen-max-latest"}
    # 剥章在记账之后:行仍记实际应答者。
    rows = await store.list_for_tenant(tenant_id=_TENANT)
    assert [(r.provider, r.model) for r in rows] == [_QWEN]


@pytest.mark.asyncio
async def test_structured_resend_candidate_and_answer_carry_no_stamp(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """结构化收尾:候选不合 schema → 重发。两次调用都记在备用名下,state 里都没有章。"""
    usage = {"input_tokens": _PURPOSE_INPUT["main"], "output_tokens": 1, "total_tokens": 101}
    brain = _Brain(
        main_replies=[
            AIMessage(content="four out of five", usage_metadata=usage),
            AIMessage(content='{"score": 4}', usage_metadata=usage),
        ]
    )
    _patch(monkeypatch, brain, hung={_MAIN[1]})
    spec = _spec(
        {
            "model": {
                "provider": _MAIN[0],
                "name": _MAIN[1],
                "fallback": [{"provider": _QWEN[0], "name": _QWEN[1]}],
            },
            "output_schema": {
                "json_schema": {
                    "type": "object",
                    "properties": {"score": {"type": "integer"}},
                    "required": ["score"],
                    "additionalProperties": False,
                }
            },
        }
    )
    store = InMemoryTokenUsageStore()

    frames, state, checkpoint = await _stream_updates(spec, store)

    assert brain.purposes == ["main", "main"], "应当发生一次结构化重发"
    _assert_no_stamp(frames, state, checkpoint)
    rows = await store.list_for_tenant(tenant_id=_TENANT)
    assert [(r.provider, r.model) for r in rows] == [_QWEN, _QWEN]
