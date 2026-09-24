"""B-103 —— worker 事件帧的 ``usage_by_model`` 与 run 级同口径:按实际应答的模型分桶。

原来 ``_child_run._usage_by_model_of`` 按 worker 主模型单桶汇总(备用接管也记主模型名),
不含 worker 内的看图与 B-104 接上的 in-run 调用。现在 worker 这一段经
``usage_tap`` 收下 ``TokenUsageMiddleware`` 落行时的同一份记账,按 ``(provider, model)``
分桶 —— 与 ``token_usage`` 行、端帧 ``usage_by_model`` 同一笔账。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from expert_work.common.observability import ExpertWorkComponent, expert_work_span, init_tracing
from expert_work.persistence.token_usage_store import (
    PLATFORM_OVERHEAD_USAGE_KIND,
    InMemoryTokenUsageStore,
)
from expert_work.protocol import ModelSpec
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.middleware import MeteredCall, TokenUsageMiddleware, usage_tap
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import MiddlewareEnv, ToolEnv, build_agent
from orchestrator.agent_factory import BuiltAgent
from orchestrator.tools._child_run import run_child_to_result
from orchestrator.tools._guards import TokenBudget
from orchestrator.tools.registry import ToolContext

from .test_in_run_usage_metering import _KEY, _MAIN, _TENANT, _USER, _any_key
from .test_served_model_metering import _QWEN, _U, _middleware, _record, _stamped
from .test_vl_usage_metering import _fake_providers, _ref, _resolver, _vision_spec


@pytest.fixture
def tracing() -> Iterator[None]:
    provider = init_tracing(
        service_name="worker-usage-test",
        env="test",
        span_processor=SimpleSpanProcessor(InMemorySpanExporter()),
    )
    try:
        yield
    finally:
        provider.shutdown()


@pytest.mark.asyncio
async def test_usage_tap_sees_what_the_row_records_and_nests_by_replacement() -> None:
    store = InMemoryTokenUsageStore()
    middleware = _middleware(store)
    outer: list[MeteredCall] = []
    inner: list[MeteredCall] = []
    with usage_tap(outer.append):
        await _record(middleware, _stamped(None, _U))
        with usage_tap(inner.append):
            await _record(middleware, _stamped("qwen:qwen-max", _U))
        # 没报用量的调用不落行,也不交给 tap。
        await _record(middleware, _stamped(None, None))
    await _record(middleware, _stamped(None, _U))

    assert [(c.provider, c.model) for c in outer] == [_MAIN]
    assert [(c.provider, c.model, c.usage_kind) for c in inner] == [(*_QWEN, "conversation")]
    assert inner[0].usage_metadata == _U
    assert len(await store.list_for_tenant(tenant_id=_TENANT)) == 3


# ---------------------------------------------------------------------------
# B-103 worker 帧
# ---------------------------------------------------------------------------


def _worker_ctx(frames: list[dict[str, Any]]) -> ToolContext:
    async def _sink(frame: dict[str, Any]) -> None:
        frames.append(frame)

    return ToolContext(
        tenant_id=_TENANT,
        user_id=_USER,
        run_id=uuid4(),
        worker_event_sink=_sink,
        tool_call_id="call-1",
        token_budget=TokenBudget(limit=10_000_000),
    )


@pytest.mark.asyncio
async def test_worker_frame_buckets_the_main_model_and_the_vision_model_apart(
    tracing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker 内一次主模型回答 + 一次看图(VL 主模型卡死、备用 qwen 接管)→ 两个桶,
    与这个 worker 落的 ``token_usage`` 行逐桶对得上。"""
    image_ref = _ref()
    fakes = _fake_providers(image_ref)

    def _fake_build_provider(entry: ModelSpec, api_key: str, **kwargs: Any) -> Any:
        del api_key, kwargs
        return fakes[entry.name]

    monkeypatch.setattr("orchestrator.agent_factory._build_provider", _fake_build_provider)
    store = InMemoryTokenUsageStore()
    frames: list[dict[str, Any]] = []
    async with make_checkpointer("memory") as cp:
        child: BuiltAgent = await build_agent(
            _vision_spec("ai-health-plan-worker"),
            secret_store=LocalDevSecretStore.from_mapping({_KEY: "sk-test"}),
            checkpointer=cp,
            provider_key_resolver=_any_key,
            tool_env=ToolEnv(image_resolver=_resolver()),
            middleware_env=MiddlewareEnv(token_usage_store=store),
        )
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "run"):
            await run_child_to_result(
                child=child,
                task="look at the chart",
                ctx=_worker_ctx(frames),
                child_depth=1,
                label="spawn_worker",
                agent_ref="dynamic:general",
                trajectory_recorder=None,
                trajectory_metadata={},
            )

    end = frames[-1]
    assert end["kind"] == "end"
    usage = end["data"]["usage"]
    buckets = {(b["provider"], b["model"]): b for b in end["data"]["usage_by_model"]}
    assert set(buckets) == {("anthropic", "claude-sonnet-4-6"), ("qwen", "qwen-vl-max")}
    assert buckets[("anthropic", "claude-sonnet-4-6")]["input_tokens"] == 100 + 200
    assert buckets[("qwen", "qwen-vl-max")]["input_tokens"] == 800
    # 桶的和 == 总量,逐字段;且与 token_usage 行同一笔账。
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        assert sum(b[key] for b in buckets.values()) == usage[key], key
    rows = await store.list_for_tenant(tenant_id=_TENANT)
    row_totals: dict[tuple[str | None, str], int] = {}
    for row in rows:
        row_totals[(row.provider, row.model)] = (
            row_totals.get((row.provider, row.model), 0) + row.input_tokens
        )
    assert row_totals == {k: b["input_tokens"] for k, b in buckets.items()}


@dataclass
class _MeteringGraph:
    """跑的时候按脚本落几次记账(模拟 worker 节点里的各种调用),再吐最终 values。"""

    middleware: TokenUsageMiddleware
    judge: TokenUsageMiddleware
    responses: list[AIMessage]
    final: dict[str, Any]

    async def astream(self, state: Any, config: Any = None, *, stream_mode: Any = None) -> Any:
        del state, config, stream_mode
        for response in self.responses:
            await _record(self.middleware, response)
        await _record(
            self.judge,
            _stamped(None, {"input_tokens": 9000, "output_tokens": 9, "total_tokens": 9009}),
        )
        yield ("values", self.final)


@pytest.mark.asyncio
async def test_worker_frame_excludes_platform_overhead_and_keeps_reasoning() -> None:
    store = InMemoryTokenUsageStore()
    reasoning_usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "total_tokens": 110,
        "input_token_details": {"cache_read": 40, "cache_creation": 0},
        "output_token_details": {"reasoning": 6},
    }
    graph = _MeteringGraph(
        middleware=_middleware(store),
        judge=TokenUsageMiddleware(
            store=store,
            agent_name="a",
            agent_version="1",
            model="judge",
            provider="openai",
            usage_kind=PLATFORM_OVERHEAD_USAGE_KIND,
        ),
        responses=[_stamped(None, reasoning_usage), _stamped("qwen:qwen-max", _U)],
        # 消息里的用量故意与记账不同:帧必须来自记账,不是按消息反推。
        final={"messages": [AIMessage(content="done")], "step_count": 1},
    )
    frames: list[dict[str, Any]] = []
    await run_child_to_result(
        child=BuiltAgent(graph=graph, system_prompt="p", max_steps=5),  # type: ignore[arg-type]
        task="t",
        ctx=_worker_ctx(frames),
        child_depth=1,
        label="spawn_worker",
        agent_ref="dynamic:general",
        trajectory_recorder=None,
        trajectory_metadata={},
    )

    data = frames[-1]["data"]
    buckets = {(b["provider"], b["model"]): b for b in data["usage_by_model"]}
    assert set(buckets) == {_MAIN, _QWEN}
    assert buckets[_MAIN]["output_token_details"] == {"reasoning": 6}
    assert buckets[_MAIN]["input_token_details"]["cache_read"] == 40
    assert data["usage"]["input_tokens"] == 100 + 7
    assert data["usage"]["output_token_details"] == {"reasoning": 6}
