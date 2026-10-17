"""B-187 —— Langfuse 的每个 LLM generation 记下**实际发出**的请求参数(model parameters)。

平台构建 provider 时会改写 manifest 的参数:目录固定温度覆盖配置值、不收采样参数的模型不发
温度、思考档位按厂商映射、输出上限换成厂商字段(通义 split 还要拆出思考预算)、Anthropic
没配上限时发默认 4096。以前这些只在日志里,「我改的配置生效没有」只能读代码回答。

这里用真工厂(``build_llm_router`` → ``_build_provider``,目录改写全在)+ 假 HTTP 传输
(``httpx.MockTransport``,抓下真正发出去的请求体)+ ``LangfuseMiddleware``,逐条断言
generation 上记的参数:是线上那份(与抓到的请求体逐项相等),也是改写后的值(写死的期望)。
备用模型接管、思考升档的路由各记各自发出的那份。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from langchain_core.messages import HumanMessage

from expert_work.protocol import ModelSpec, StructuredOutputSpec
from expert_work.runtime.middleware import (
    LangfuseMiddleware,
    MiddlewareChain,
    RecordedSpan,
    RecordingLangfuseClient,
)
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator.agent_factory import _escalated_model, build_llm_router

_KEY = "expert-work/dev/llm/any"
#: 平台凭据的哨兵值:它确实上了线(请求头),但绝不能出现在 trace 字段里。
_API_KEY = "sk-b187-sentinel-0123456789abcdef"

#: 请求体里算「模型参数」的字段 —— 与 generation 上记的那份逐项比对用。
_PARAM_FIELDS = (
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "reasoning_effort",
    "thinking",
    "enable_thinking",
    "thinking_budget",
    "output_config",
)
#: generation 上允许出现的键(拍平后)—— 只有采样 / 输出上限 / 思考字段。
_ALLOWED_KEYS = {
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "reasoning_effort",
    "thinking.type",
    "enable_thinking",
    "thinking_budget",
    "output_config.effort",
}

_VERDICT = StructuredOutputSpec(
    name="verdict",
    schema={
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    },
)


def _sse(*events: dict[str, Any]) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def _anthropic_reply(body: dict[str, Any]) -> httpx.Response:
    if body.get("stream"):
        return httpx.Response(
            200,
            content=_sse(
                {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "ok"},
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 1},
                },
                {"type": "message_stop"},
            ),
        )
    # 结构化调用走 complete():强制的那个工具带回答案。
    tool = body["tool_choice"]["name"]
    return httpx.Response(
        200,
        json={
            "content": [{"type": "tool_use", "id": "t1", "name": tool, "input": {"ok": True}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 3, "output_tokens": 1},
        },
    )


def _openai_reply() -> httpx.Response:
    return httpx.Response(
        200,
        content=_sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
        + b"data: [DONE]\n\n",
    )


@dataclass
class _Wire:
    """假上游:记下每个请求;``failing`` 里的模型回 503(可换备用的错)。"""

    failing: frozenset[str] = frozenset()
    requests: list[httpx.Request] = field(default_factory=list)
    bodies: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(request)
        self.bodies.append(body)
        if body["model"] in self.failing:
            return httpx.Response(503, text="upstream unavailable")
        if request.url.path.endswith("/v1/messages"):
            return _anthropic_reply(body)
        return _openai_reply()


async def _any_key(provider: str) -> list[str]:
    del provider
    return [f"secret://{_KEY}"]


async def _call(
    model: ModelSpec,
    wire: _Wire,
    *,
    output_schemas: tuple[StructuredOutputSpec | None, ...] = (None,),
) -> list[RecordedSpan]:
    """建一条真路由(只挂 Langfuse 中间件),按 ``output_schemas`` 各调一次,返回记下的 generation。"""
    client = RecordingLangfuseClient()
    chain = MiddlewareChain.from_middlewares("around_llm_call", [LangfuseMiddleware(client=client)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as http:
        router = await build_llm_router(
            model,
            secret_store=LocalDevSecretStore.from_mapping({_KEY: _API_KEY}),
            around_llm_chain=chain,
            provider_key_resolver=_any_key,
            ignore_api_key_ref=True,
            http_client=http,
        )
        for schema in output_schemas:
            await router(messages=[HumanMessage(content="hi")], tools=[], output_schema=schema)
    return client.spans


def _sent(body: dict[str, Any]) -> dict[str, Any]:
    """一份请求体里的参数字段,按 trace 的口径拍平(``thinking.type``)。"""
    flat: dict[str, Any] = {}
    for key in _PARAM_FIELDS:
        if key not in body:
            continue
        value = body[key]
        if isinstance(value, dict):
            flat.update({f"{key}.{sub}": v for sub, v in value.items()})
        else:
            flat[key] = value
    return flat


@pytest.mark.asyncio
async def test_catalog_fixed_temperature_records_the_forced_value() -> None:
    """kimi-k3 只收 temperature=1:manifest 写 0.9,发出去的是 1.0,记的也是 1.0。"""
    wire = _Wire()
    [span] = await _call(ModelSpec(provider="kimi", name="kimi-k3", temperature=0.9), wire)

    assert span.model_parameters == {"temperature": 1.0}
    assert span.model_parameters == _sent(wire.bodies[0])
    assert span.metadata["model"] == "kimi-k3"


@pytest.mark.asyncio
async def test_no_sampling_model_records_no_temperature() -> None:
    """Opus 4.7+ 不收采样参数:manifest 的 0.7 不发,记录里也没有 temperature;
    没配输出上限时发的是 Anthropic 默认 4096。"""
    wire = _Wire()
    [span] = await _call(
        ModelSpec(provider="anthropic", name="claude-opus-4-8", temperature=0.7), wire
    )

    assert span.model_parameters is not None
    assert "temperature" not in span.model_parameters
    assert "temperature" not in wire.bodies[0]
    assert span.model_parameters == {"max_tokens": 4096}
    assert span.model_parameters == _sent(wire.bodies[0])


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        # glm 档位映射 medium → high,思考开关走 thinking.type。
        (
            ModelSpec(provider="glm", name="glm-5.3", effort="medium", max_tokens=8000),
            {
                "temperature": 0.2,
                "thinking.type": "enabled",
                "reasoning_effort": "high",
                "max_tokens": 8000,
            },
        ),
        # 豆包没有 max 档,映射到 high;输出上限字段是 max_completion_tokens。
        (
            ModelSpec(provider="doubao", name="doubao-seed-2.0-pro", effort="max", max_tokens=8000),
            {"temperature": 0.2, "reasoning_effort": "high", "max_completion_tokens": 8000},
        ),
        # 通义 split:合计上限 10000 拆成思考预算 8000(high 档 0.8)+ 回答 2000。
        (
            ModelSpec(provider="qwen", name="qwen3-max", effort="high", max_tokens=10_000),
            {
                "temperature": 0.2,
                "enable_thinking": True,
                "thinking_budget": 8000,
                "max_tokens": 2000,
            },
        ),
    ],
    ids=["glm-effort-map", "doubao-effort-map", "qwen-split-cap"],
)
@pytest.mark.asyncio
async def test_vendor_mapped_thinking_records_the_wire_values(
    model: ModelSpec, expected: dict[str, Any]
) -> None:
    wire = _Wire()
    [span] = await _call(model, wire)

    assert span.model_parameters == expected
    assert span.model_parameters == _sent(wire.bodies[0])


@pytest.mark.asyncio
async def test_fallback_served_call_records_the_fallback_models_params() -> None:
    """主模型 503 换备用:失败那条记主模型的参数,应答那条记备用模型自己的。"""
    wire = _Wire(failing=frozenset({"glm-5.3"}))
    primary = ModelSpec(
        provider="glm",
        name="glm-5.3",
        effort="low",
        fallback=[ModelSpec(provider="kimi", name="kimi-k3", temperature=0.9)],
    )
    failed, served = await _call(primary, wire)

    assert failed.error is not None
    assert failed.metadata["model"] == "glm-5.3"
    assert failed.model_parameters == _sent(wire.bodies[0])
    assert served.error is None
    assert served.metadata["model"] == "kimi-k3"
    assert served.model_parameters == {"temperature": 1.0}
    assert served.model_parameters == _sent(wire.bodies[1])


@pytest.mark.asyncio
async def test_escalated_router_records_its_own_effort() -> None:
    """CM-9 升档路由与主路由同一个句柄 key(``glm:glm-5.3``),记的是各自发出的档位。"""
    primary = ModelSpec(provider="glm", name="glm-5.3", effort="low")
    escalated = _escalated_model(primary)
    assert escalated is not None
    primary_wire, escalated_wire = _Wire(), _Wire()

    [primary_span] = await _call(primary, primary_wire)
    [escalated_span] = await _call(escalated, escalated_wire)

    assert primary_span.model_parameters is not None
    assert escalated_span.model_parameters is not None
    assert primary_span.model_parameters["reasoning_effort"] == "low"
    # 升一档到 medium,glm 的 effort_map 把 medium 发成 high。
    assert escalated_span.model_parameters["reasoning_effort"] == "high"
    assert escalated_span.model_parameters == _sent(escalated_wire.bodies[0])


@pytest.mark.asyncio
async def test_anthropic_structured_call_records_thinking_pinned_off() -> None:
    """同一条路由:普通调用发 effort;结构化调用把思考钉成 disabled、不发 effort —— 各记各的。"""
    wire = _Wire()
    plain, structured = await _call(
        ModelSpec(provider="anthropic", name="claude-opus-4-8", effort="high"),
        wire,
        output_schemas=(None, _VERDICT),
    )

    assert plain.model_parameters == {"max_tokens": 4096, "output_config.effort": "high"}
    assert plain.model_parameters == _sent(wire.bodies[0])
    assert structured.model_parameters == {"max_tokens": 4096, "thinking.type": "disabled"}
    assert structured.model_parameters == _sent(wire.bodies[1])


@pytest.mark.parametrize(
    "model",
    [
        ModelSpec(
            provider="self-hosted",
            name="local-llm",
            base_url="https://llm.internal.example",
        ),
        ModelSpec(provider="anthropic", name="claude-sonnet-4-6", effort="high"),
    ],
    ids=["self-hosted", "anthropic"],
)
@pytest.mark.asyncio
async def test_recorded_params_carry_no_secrets(model: ModelSpec) -> None:
    """凭据确实上了线(请求头),但 trace 字段里只有模型名和参数:没有 key、没有上游地址。"""
    wire = _Wire()
    [span] = await _call(model, wire)

    sent_headers = " ".join(str(v) for r in wire.requests for v in r.headers.values())
    assert _API_KEY in sent_headers  # 哨兵确实拿得到,下面的「没有」才有意义
    assert span.model_parameters is not None
    assert set(span.model_parameters) <= _ALLOWED_KEYS
    recorded = json.dumps([span.metadata, span.model_parameters])
    for secret in (_API_KEY, "llm.internal.example", "api.anthropic.com"):
        assert secret not in recorded
