"""B-320 —— Claude 5 四个型号收到的请求体,逐个字段断言。

四个型号不收的东西各不相同(Anthropic 官方接口文档,2026-11-03 版):

* Fable 5.2 / Opus 5.5:思考关不掉 —— ``thinking: {type: disabled}`` 与 ``budget_tokens`` 都 400,
  关思考只能不发 ``thinking`` 并把 ``output_config.effort`` 压到最低档;强制 ``tool_choice``
  (``any`` / ``tool``)400。
* Sonnet 5.5:``disabled`` 400,关思考发 ``{type: between_tools}``(不带别的字段,effort 不高于
  high);强制 ``tool_choice`` 400。
* Haiku 5.5:``disabled`` 照收(effort 不高于 high);强制 ``tool_choice`` 照收。
* 四个都不收采样参数(Sonnet / Haiku 只收默认值,平台一律不发)。

用真工厂(``_build_provider``,目录闸门全在)+ 假 HTTP 传输抓下真正发出去的请求体。没配输出上限
时 Claude 5 四个发 16000(目录 ``default_max_tokens``:思考默认开,4096 会被思考吃满),旧模型
与目录外模型仍发 4096。``effort`` 不高于 high 这一条:关思考时平台不发 effort,用的是厂商
默认档(Sonnet 5.5 high、Haiku 5.5 medium),都在可收范围内。

结构化输出:能强制调工具的(Haiku 5.5、旧模型)照旧强制 + 关思考;不能强制的三个发 ``auto``,
思考 / 档位与这个智能体的普通调用相同。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from langchain_core.messages import HumanMessage

from expert_work.protocol import ModelSpec, StructuredOutputSpec
from orchestrator.agent_factory import _build_provider, _escalated_model
from orchestrator.llm import AnthropicProvider
from orchestrator.llm.truncation import effective_output_cap

_VERDICT = StructuredOutputSpec(
    name="verdict",
    schema={
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    },
)

#: 请求体里与本单无关的结构字段;其余每个字段都要写进期望里(多发一个就红)。
_STRUCTURAL = frozenset({"model", "messages", "system", "tools", "stream"})

_FABLE = "claude-fable-5-2"
_OPUS55 = "claude-opus-5-5"
_SONNET55 = "claude-sonnet-5-5"
_HAIKU55 = "claude-haiku-5-5"
_OPUS48 = "claude-opus-4-8"

_OFF_CATALOG = "claude-custom-gw"

#: 没配输出上限时发的值:Claude 5 四个按目录 ``default_max_tokens``(思考默认开,4096 会被
#: 思考吃满);旧模型与目录外模型仍是 4096。
_DEFAULT_CAP = {"max_tokens": 16_000}
_OLD_CAP = {"max_tokens": 4096}
_LOW = {**_DEFAULT_CAP, "output_config": {"effort": "low"}}
_BETWEEN_TOOLS = {**_DEFAULT_CAP, "thinking": {"type": "between_tools"}}
_DISABLED = {**_DEFAULT_CAP, "thinking": {"type": "disabled"}}
_OLD_DISABLED = {**_OLD_CAP, "thinking": {"type": "disabled"}}
_AUTO = {"tool_choice": {"type": "auto"}}
_FORCED = {"tool_choice": {"type": "tool", "name": "verdict"}}

#: 关思考(manifest 还写着 effort=max:关了就不该把它发出去)。
_OFF = {"thinking_enabled": False, "effort": "max"}


def _sse(*events: dict[str, Any]) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


class _Wire:
    """假上游:记下每个请求体,按调用形态回一个合法应答。"""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
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
        tools = body.get("tools") or []
        if tools:
            # 结构化调用:模型调带 schema 的那个工具交答案(强制或自选都一样)。
            content: list[dict[str, Any]] = [
                {"type": "tool_use", "id": "t1", "name": tools[0]["name"], "input": {"ok": True}}
            ]
        else:
            content = [{"type": "text", "text": "ok"}]
        return httpx.Response(
            200,
            json={
                "content": content,
                "stop_reason": "tool_use" if tools else "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 1},
            },
        )


async def _send(
    name: str,
    overrides: dict[str, Any],
    *,
    structured: bool = False,
    stream: bool = False,
) -> tuple[dict[str, Any], Any]:
    wire = _Wire()
    model = ModelSpec.model_validate({"provider": "anthropic", "name": name, **overrides})
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as http:
        provider = _build_provider(model, "k", http_client=http)
        assert isinstance(provider, AnthropicProvider)
        messages = [HumanMessage(content="hi")]
        if stream:
            reply: Any = [d async for d in provider.stream(messages=messages, tools=[])]
        else:
            reply = await provider.complete(
                messages=messages,
                tools=[],
                output_schema=_VERDICT if structured else None,
            )
    [body] = wire.bodies
    return body, reply


def _controls(body: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in body.items() if k not in _STRUCTURAL}


@pytest.mark.parametrize(
    ("name", "overrides", "expected"),
    [
        # —— 什么都没配:不发思考 / effort,走厂商默认(四个都默认开思考)。
        (_FABLE, {}, _DEFAULT_CAP),
        (_OPUS55, {}, _DEFAULT_CAP),
        (_SONNET55, {}, _DEFAULT_CAP),
        (_HAIKU55, {}, _DEFAULT_CAP),
        # —— 配了温度:四个都不发采样参数。
        (_FABLE, {"temperature": 0.7}, _DEFAULT_CAP),
        (_OPUS55, {"temperature": 0.7}, _DEFAULT_CAP),
        (_SONNET55, {"temperature": 0.7}, _DEFAULT_CAP),
        (_HAIKU55, {"temperature": 0.7}, _DEFAULT_CAP),
        # —— 关思考。
        (_FABLE, _OFF, _LOW),
        (_OPUS55, _OFF, _LOW),
        (_SONNET55, _OFF, _BETWEEN_TOOLS),
        (_HAIKU55, _OFF, _DISABLED),
        # —— 开思考 + 档位:档位原样发,thinking 不发(默认就是 adaptive)。
        (
            _FABLE,
            {"thinking_enabled": True, "effort": "high"},
            {**_DEFAULT_CAP, "output_config": {"effort": "high"}},
        ),
        (
            _OPUS55,
            {"thinking_enabled": True, "effort": "high"},
            {**_DEFAULT_CAP, "output_config": {"effort": "high"}},
        ),
        (
            _SONNET55,
            {"thinking_enabled": True, "effort": "max"},
            {**_DEFAULT_CAP, "output_config": {"effort": "max"}},
        ),
        (
            _HAIKU55,
            {"thinking_enabled": True, "effort": "low"},
            {**_DEFAULT_CAP, "output_config": {"effort": "low"}},
        ),
        # —— 显式 adaptive:四个都收。
        (
            _FABLE,
            {"adaptive_thinking": True, "effort": "max"},
            {**_DEFAULT_CAP, "thinking": {"type": "adaptive"}, "output_config": {"effort": "max"}},
        ),
        (
            _SONNET55,
            {"adaptive_thinking": True, "max_tokens": 128_000},
            {"max_tokens": 128_000, "thinking": {"type": "adaptive"}},
        ),
        # —— 旧模型不变:Opus 4.8 关思考仍是 disabled、上限 4096。
        (_OPUS48, _OFF, _OLD_DISABLED),
        (_OPUS48, {}, _OLD_CAP),
        # —— 目录外模型(自建网关)不变:disabled、4096、照发温度。
        (_OFF_CATALOG, _OFF, {**_OLD_DISABLED, "temperature": 0.2}),
        (_OFF_CATALOG, {}, {**_OLD_CAP, "temperature": 0.2}),
    ],
)
@pytest.mark.asyncio
async def test_chat_call_request_body(
    name: str, overrides: dict[str, Any], expected: dict[str, Any]
) -> None:
    body, _ = await _send(name, overrides)
    assert body["model"] == name
    assert _controls(body) == expected


@pytest.mark.parametrize(
    ("name", "overrides", "expected"),
    [
        # 不收强制调工具的三个:tool_choice=auto,思考 / 档位与这个智能体的普通调用**完全
        # 相同**(不为结构化调用单独关思考:Sonnet 5.5 半路换 between_tools 会改档位,
        # Fable / Opus 5.5 会悄悄降到 low)。
        (_FABLE, {}, {**_DEFAULT_CAP, **_AUTO}),
        (_FABLE, _OFF, {**_LOW, **_AUTO}),
        (
            _OPUS55,
            {"effort": "high"},
            {**_DEFAULT_CAP, "output_config": {"effort": "high"}, **_AUTO},
        ),
        (_SONNET55, {}, {**_DEFAULT_CAP, **_AUTO}),
        (
            _SONNET55,
            {"thinking_enabled": True, "effort": "max"},
            {**_DEFAULT_CAP, "output_config": {"effort": "max"}, **_AUTO},
        ),
        (_SONNET55, _OFF, {**_BETWEEN_TOOLS, **_AUTO}),
        # Haiku 5.5 收强制调工具 + disabled:与旧模型同一条线(强制调工具时思考必须关)。
        (_HAIKU55, {"effort": "max"}, {**_DISABLED, **_FORCED}),
        (_OPUS48, {}, {**_OLD_DISABLED, **_FORCED}),
        (_OFF_CATALOG, {}, {**_OLD_DISABLED, "temperature": 0.2, **_FORCED}),
    ],
)
@pytest.mark.asyncio
async def test_structured_call_request_body(
    name: str, overrides: dict[str, Any], expected: dict[str, Any]
) -> None:
    body, reply = await _send(name, overrides, structured=True)
    assert _controls(body) == expected
    [tool] = body["tools"]
    assert tool["name"] == "verdict"
    assert tool["input_schema"] == _VERDICT.schema
    # 不管强制还是自选,调了那个工具就解成 JSON 正文、不留 tool_calls。
    assert json.loads(reply.content) == {"ok": True}
    assert reply.tool_calls == []


@pytest.mark.parametrize("name", [_FABLE, _OPUS55, _SONNET55])
@pytest.mark.asyncio
async def test_unforced_schema_tool_tells_the_model_to_call_it(name: str) -> None:
    """不能强制调工具时,靠工具说明把模型引到它上面;能强制的模型说明不变(缓存前缀不动)。"""
    body, _ = await _send(name, {}, structured=True)
    assert "call this tool" in body["tools"][0]["description"]
    forced, _ = await _send(_HAIKU55, {}, structured=True)
    assert forced["tools"][0]["description"] == (
        "Return the final structured output for this request."
    )


@pytest.mark.asyncio
async def test_streaming_call_sends_the_same_controls() -> None:
    """流式与非流式共用同一份请求组装:关思考的 Sonnet 5.5 流式也发 between_tools。"""
    body, _ = await _send(_SONNET55, _OFF, stream=True)
    assert body["stream"] is True
    assert _controls(body) == _BETWEEN_TOOLS
    body, _ = await _send(_OPUS55, _OFF, stream=True)
    assert _controls(body) == _LOW


@pytest.mark.parametrize("name", [_FABLE, _OPUS55, _SONNET55, _HAIKU55])
@pytest.mark.asyncio
async def test_recorded_request_params_match_the_wire(name: str) -> None:
    """Langfuse 记的参数(``request_params``)与真正发出去的一致(B-187 同一条线)。"""
    for overrides in ({}, _OFF, {"thinking_enabled": True, "effort": "high"}):
        for structured in (False, True):
            model = ModelSpec.model_validate({"provider": "anthropic", "name": name, **overrides})
            provider = _build_provider(model, "k")
            assert isinstance(provider, AnthropicProvider)
            body, _ = await _send(name, overrides, structured=structured)
            sent = {k: v for k, v in _controls(body).items() if k != "tool_choice"}
            assert provider.request_params(structured=structured, stream=False) == sent


# --- 思考升档(``_escalated_model``)-------------------------------------------------


@pytest.mark.parametrize(
    ("name", "escalates"),
    [
        # Sonnet 5.5 关思考是 between_tools:升档 = 这一轮开思考 + medium(比它默认的 high
        # 还低),下一轮又回到 between_tools,档位在对话中途来回变 —— 不升。
        (_SONNET55, False),
        # 其余照旧:关思考的升一档就是开思考 + medium。
        (_FABLE, True),
        (_OPUS55, True),
        (_HAIKU55, True),
        (_OPUS48, True),
    ],
)
def test_thinking_off_escalation(name: str, escalates: bool) -> None:
    model = ModelSpec.model_validate(
        {"provider": "anthropic", "name": name, "thinking_enabled": False}
    )
    escalated = _escalated_model(model)
    if escalates:
        assert escalated is not None
        assert (escalated.thinking_enabled, escalated.effort) == (True, "medium")
    else:
        assert escalated is None


def test_sonnet_55_with_thinking_on_still_escalates() -> None:
    """只有「关思考」的 Sonnet 5.5 不升;开着思考的照常按档位升。"""
    model = ModelSpec.model_validate(
        {"provider": "anthropic", "name": _SONNET55, "thinking_enabled": True, "effort": "medium"}
    )
    escalated = _escalated_model(model)
    assert escalated is not None and escalated.effort == "high"


# --- 没配输出上限时的默认值 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "max_tokens", "expected"),
    [
        # Claude 5 四个:目录 default_max_tokens(16000)。
        (_FABLE, None, 16_000),
        (_OPUS55, None, 16_000),
        (_SONNET55, None, 16_000),
        (_HAIKU55, None, 16_000),
        # 旧模型与目录外模型一个字节不变:4096。
        (_OPUS48, None, 4096),
        ("claude-sonnet-4-6", None, 4096),
        ("claude-haiku-4-5", None, 4096),
        (_OFF_CATALOG, None, 4096),
        # 清单写了就用清单的。
        (_FABLE, 2048, 2048),
        (_OPUS48, 8000, 8000),
    ],
)
def test_default_output_cap(name: str, max_tokens: int | None, expected: int) -> None:
    """发出去的上限与截断报错里说的上限是同一个数(``effective_output_cap``)。"""
    model = ModelSpec.model_validate(
        {"provider": "anthropic", "name": name, "max_tokens": max_tokens}
    )
    provider = _build_provider(model, "k")
    assert isinstance(provider, AnthropicProvider)
    assert provider.max_tokens == expected
    assert effective_output_cap(model) == expected
