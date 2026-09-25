"""B-105 —— 看图模型被输出上限截断成空回答时,``ask_image`` 给出说清原因的工具错误。

以前这种回答落成 ``[VL model returned no text]``,模型只会以为图里没东西、原样再问。
现在:照常扣池 / 记账(这次调用照样计费),计截断数(``usable="false"``,标签取实际应答
的看图模型),然后抛 :class:`GuidedToolError`,错误里带上限、建议里不许原样重试。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from prometheus_client import REGISTRY

from expert_work.protocol import ModelSpec
from expert_work.protocol.multimodal import ImageRef
from orchestrator.llm.truncation import served_output_cap
from orchestrator.multimodal import InMemoryImageResolver, ResolvedImage
from orchestrator.tools._guards import TokenBudget
from orchestrator.tools.error_classifier import GuidedToolError, classify_tool_error
from orchestrator.tools.registry import ToolContext
from orchestrator.tools.vision import AskImageTool
from orchestrator.usage_metering import SERVED_BY_KEY

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_USER = UUID("33333333-3333-3333-3333-333333333333")
_USAGE = {"input_tokens": 800, "output_tokens": 3000, "total_tokens": 3800}


def _ref() -> str:
    return ImageRef(tenant_id=_TENANT, thread_id=uuid4(), image_id=uuid4(), ext=".png").to_uri()


def _resolver() -> InMemoryImageResolver:
    return InMemoryImageResolver(images={"any": ResolvedImage(media_type="image/png", data=b"PNG")})


@dataclass
class _VL:
    response: AIMessage

    async def __call__(self, *, messages: Sequence[BaseMessage], tools: Sequence[Any]) -> AIMessage:
        del messages, tools
        return self.response


@dataclass
class _Meter:
    calls: list[AIMessage] = field(default_factory=list)

    async def __call__(self, response: AIMessage, *, tenant_id: UUID, user_id: UUID | None) -> None:
        del tenant_id, user_id
        self.calls.append(response)


def _truncated(content: str = "", **meta: Any) -> AIMessage:
    return AIMessage(
        content=content,
        usage_metadata=_USAGE,
        response_metadata={"finish_reason": "length", **meta},
    )


def _count(provider: str, model: str) -> float:
    value = REGISTRY.get_sample_value(
        "expert_work_llm_output_truncated_total",
        {"provider": provider, "model": model, "usable": "false"},
    )
    return value or 0.0


def _vl_model(**overrides: Any) -> ModelSpec:
    return ModelSpec.model_validate(
        {
            "provider": "qwen",
            "name": "qwen3-vl-flash",
            "max_tokens": 3000,
            "fallback": [{"provider": "glm", "name": "glm-4.6v", "max_tokens": 5000}],
            **overrides,
        }
    )


def _tool(response: AIMessage, model: ModelSpec, meter: _Meter) -> AskImageTool:
    return AskImageTool(
        vl_caller=_VL(response),
        image_resolver=_resolver(),
        usage_meter=meter,
        vl_model_name=model.name,
        output_cap_resolver=served_output_cap(model),
    )


def _ctx(budget: TokenBudget | None = None) -> ToolContext:
    return ToolContext(tenant_id=_TENANT, user_id=_USER, token_budget=budget)


@pytest.mark.asyncio
async def test_truncated_empty_reply_is_metered_counted_and_a_guided_error() -> None:
    meter = _Meter()
    budget = TokenBudget(limit=1_000_000)
    response = _truncated()
    before = _count("qwen", "qwen3-vl-flash")

    with pytest.raises(GuidedToolError) as info:
        await _tool(response, _vl_model(), meter).call(
            {"image_ref": _ref(), "question": "q"}, ctx=_ctx(budget)
        )

    assert str(info.value) == (
        "看图模型输出被截断(已用满输出上限 3000,含思考)。"
        "请在看图模型配置里调大『输出上限』,或者改用默认的快看。"
    )
    # 照常记账、扣池:这次调用厂商照样计费。
    assert meter.calls == [response]
    assert budget.spent == 3800
    assert _count("qwen", "qwen3-vl-flash") == before + 1
    # 分类:真失败(进欠账),不许原样重试,建议是工具自己的。
    classified = classify_tool_error(tool_name="ask_image", error=info.value)
    assert classified.retryable is False
    assert classified.error_class != "transient"
    assert "Do not repeat the identical call" in classified.advice


@pytest.mark.asyncio
async def test_label_and_cap_follow_the_served_fallback_model() -> None:
    response = _truncated(**{SERVED_BY_KEY: "glm:glm-4.6v#0"})
    before = _count("glm", "glm-4.6v")

    with pytest.raises(GuidedToolError, match="已用满输出上限 5000"):
        await _tool(response, _vl_model(), _Meter()).call(
            {"image_ref": _ref(), "question": "q"}, ctx=_ctx()
        )

    assert _count("glm", "glm-4.6v") == before + 1


@pytest.mark.asyncio
async def test_empty_cap_reads_vendor_default_and_anthropic_reads_4096() -> None:
    with pytest.raises(GuidedToolError, match="已用满输出上限 厂商默认值"):
        await _tool(_truncated(), _vl_model(max_tokens=None, fallback=[]), _Meter()).call(
            {"image_ref": _ref(), "question": "q"}, ctx=_ctx()
        )
    anthropic = ModelSpec(provider="anthropic", name="claude-sonnet-4-6")
    response = AIMessage(content="", response_metadata={"stop_reason": "max_tokens"})
    with pytest.raises(GuidedToolError, match="已用满输出上限 4096"):
        await _tool(response, anthropic, _Meter()).call(
            {"image_ref": _ref(), "question": "q"}, ctx=_ctx()
        )


@pytest.mark.asyncio
async def test_normal_and_usable_truncated_replies_are_returned_unchanged() -> None:
    for response in (
        AIMessage(content="a chart", usage_metadata=_USAGE),
        _truncated("a chart, partly"),
    ):
        meter = _Meter()
        result = await _tool(response, _vl_model(), meter).call(
            {"image_ref": _ref(), "question": "q"}, ctx=_ctx()
        )
        assert result.content == response.content
        assert meter.calls == [response]
