"""B-257 —— ``ask_image`` 深入看更快:最多等 60 秒就改快看,没设思考深度时按中档发。

测试环境 30 天:quick p95 15 s、最长 28 s;deep 中位 43 s、p90 107 s,约 23 % 等满 120 s。
豆包 seed-2.1-pro 实测(化验单 + 趋势图各 3 次):不设档位(≈ high)95~123 s、一次超时;
medium 58~63 s;low 22~44 s;各档答案都对。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from expert_work.protocol import AgentSpec, ModelSpec
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.middleware.llm_error_handling import (
    LLMClientError,
    LLMUnauthorizedError,
)
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import ToolEnv, agent_factory, build_agent
from orchestrator.llm import AnthropicProvider
from orchestrator.llm.providers._errors import classify_http_error
from orchestrator.tools.error_classifier import GuidedTimeoutError, classify_tool_error
from orchestrator.tools.registry import ToolContext
from orchestrator.tools.vision import (
    ASK_IMAGE_DEEP_TIMEOUT_S,
    ASK_IMAGE_TIMEOUT_S,
    AskImageTool,
)

from .test_ask_image_depth_timeout import (
    _KEY,
    _TENANT,
    _USER,
    _Answer,
    _any_key,
    _disable_payload,
    _Hanging,
    _Harness,
    _ref,
    _Rejects,
    _resolver,
    _Scripted,
    _VLProvider,
)

_REAL_TIMEOUT = asyncio.timeout
_REAL_BUILD_PROVIDER = agent_factory._build_provider

# ---------------------------------------------------------------------------
# 工具层:deep 有快看兜底时只等 60 秒
# ---------------------------------------------------------------------------


class _ScaledAsyncio:
    """只替换 ``tools/vision.py`` 看到的 ``asyncio``:记下每次 VL 调用要的限时(秒),
    实际按 ``1 / divisor`` 等。其余属性原样转给真 ``asyncio``。"""

    def __init__(self, divisor: float) -> None:
        self.divisor = divisor
        self.asked: list[float] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    def timeout(self, delay: float | None) -> asyncio.Timeout:
        assert delay is not None
        self.asked.append(delay)
        return _REAL_TIMEOUT(delay / self.divisor)


def _scale_vision_timeouts(monkeypatch: pytest.MonkeyPatch, divisor: float) -> list[float]:
    scaled = _ScaledAsyncio(divisor)
    monkeypatch.setattr("orchestrator.tools.vision.asyncio", scaled)
    return scaled.asked


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """按千分之一等 —— 60 秒的窗口 60 毫秒就到。"""
    return _scale_vision_timeouts(monkeypatch, 1000)


def test_deep_wait_is_60s_and_the_overall_limit_stays_120s() -> None:
    assert ASK_IMAGE_DEEP_TIMEOUT_S == 60
    assert ASK_IMAGE_TIMEOUT_S == 120
    tool = AskImageTool(vl_caller=_Answer(), image_resolver=_resolver())
    assert (tool.deep_timeout_s, tool.timeout_s) == (60, 120)


async def test_deep_gives_up_after_60s_and_the_quick_look_answers(limits: list[float]) -> None:
    deep, quick = _Hanging(), _Answer("quick answer")
    tool = AskImageTool(vl_caller=deep, image_resolver=_resolver(), quick_vl_caller=quick)

    result = await tool.call(
        {"image_ref": _ref(), "question": "q", "depth": "deep"}, ctx=ToolContext(tenant_id=_TENANT)
    )

    assert limits == [60.0, 120.0]  # deep 60 秒,改用的快看照旧 120 秒
    assert deep.cancelled and quick.calls == 1
    assert str(result.content).startswith("(depth='deep' timed out after 60 seconds,")
    assert str(result.content).endswith("quick answer")
    assert result.meta["deep_timed_out"] is True
    assert result.meta["depth"] == "quick"


async def test_deep_without_a_quick_route_still_gets_120s(limits: list[float]) -> None:
    tool = AskImageTool(vl_caller=_Hanging(), image_resolver=_resolver())

    with pytest.raises(TimeoutError, match=r"timed out \(120 seconds\)"):
        await tool.call(
            {"image_ref": _ref(), "question": "q", "depth": "deep"},
            ctx=ToolContext(tenant_id=_TENANT),
        )

    assert limits == [120.0]


@pytest.mark.parametrize("quick_route", [True, False])
async def test_quick_keeps_its_120s(limits: list[float], quick_route: bool) -> None:
    hanging = _Hanging()
    tool = (
        AskImageTool(vl_caller=_Answer(), image_resolver=_resolver(), quick_vl_caller=hanging)
        if quick_route
        else AskImageTool(vl_caller=hanging, image_resolver=_resolver())
    )

    with pytest.raises(TimeoutError, match=r"timed out \(120 seconds\)"):
        await tool.call({"image_ref": _ref(), "question": "q"}, ctx=ToolContext(tenant_id=_TENANT))

    assert limits == [120.0]


# ---------------------------------------------------------------------------
# 构建:deep 路由没设思考深度时按平台默认档位「中档」发
# ---------------------------------------------------------------------------

_DOUBAO = "doubao-seed-2-1-pro-260628"


def _medium_budget(max_tokens: int | None = None) -> dict[str, Any]:
    return {
        "enable_thinking": True,
        "thinking_budget": agent_factory._thinking_budget("medium", max_tokens),
    }


@dataclass
class _WireHarness(_Harness):
    """Anthropic 不走 ``thinking_payload``,记它线上的 ``output_config``;``rejected`` 里的
    payload 被厂商按 400 拒(没实调过的厂商不认这一档)。"""

    rejected: tuple[Any, ...] = ()

    def fake_build_provider(self, entry: ModelSpec, api_key: str, **kwargs: Any) -> Any:
        provider = super().fake_build_provider(entry, api_key, **kwargs)
        if not isinstance(provider, _VLProvider):
            return provider
        if entry.provider == "anthropic":
            real = _REAL_BUILD_PROVIDER(entry, api_key, **kwargs)
            assert isinstance(real, AnthropicProvider)
            params = real.request_params(structured=False, stream=True)
            provider.thinking_payload = {"output_config": params.get("output_config")}
        if provider.thinking_payload in self.rejected:
            provider.error = LLMClientError("400 invalid parameter: reasoning_effort")
        return provider


@pytest.mark.parametrize(
    ("vision_model", "expected"),
    [
        # effort 厂商、没设:发它自己的 medium 档(以前什么都不发 = 厂商默认 ≈ high)。
        ({"provider": "doubao", "name": _DOUBAO}, {"reasoning_effort": "medium"}),
        (
            {"provider": "doubao", "name": _DOUBAO, "thinking_enabled": True},
            {"reasoning_effort": "medium"},
        ),
        # effort_map:平台 medium → 厂商 high。
        ({"provider": "kimi", "name": "kimi-k3"}, {"reasoning_effort": "high"}),
        # 显式设置一律优先。
        ({"provider": "doubao", "name": _DOUBAO, "effort": "low"}, {"reasoning_effort": "low"}),
        (
            {"provider": "doubao", "name": _DOUBAO, "thinking_enabled": False},
            {"thinking": {"type": "disabled"}},
        ),
        # budget 厂商:按档位换算的 medium 预算。
        ({"provider": "qwen", "name": "qwen3.6-plus"}, _medium_budget()),
        ({"provider": "qwen", "name": "qwen3-vl-plus", "thinking_enabled": True}, _medium_budget()),
        # 目录默认不思考、没显式开:补档位会把思考打开,原样。
        ({"provider": "qwen", "name": "qwen3-vl-plus"}, None),
        # budget 厂商输出上限不到 2048:最低预算 1024 会挤掉回答,不补。
        ({"provider": "qwen", "name": "qwen3.6-plus", "max_tokens": 2000}, None),
        ({"provider": "qwen", "name": "qwen3.6-plus", "max_tokens": 2048}, _medium_budget(2048)),
        # 只有开关的厂商、目录外模型:原样。
        ({"provider": "glm", "name": "glm-5v-turbo"}, None),
        ({"provider": "doubao", "name": "doubao-seed-2-1-pro"}, None),
        # OpenAI / Anthropic 也是 effort 形态,同样按 medium 发。
        ({"provider": "openai", "name": "gpt-5.4-mini"}, {"reasoning_effort": "medium"}),
        (
            {"provider": "anthropic", "name": "claude-opus-4-8"},
            {"output_config": {"effort": "medium"}},
        ),
    ],
)
async def test_deep_sends_the_platform_default_depth_only_when_untouched(
    monkeypatch: pytest.MonkeyPatch, vision_model: dict[str, Any], expected: Any
) -> None:
    harness = _WireHarness()

    await harness.run(monkeypatch, {"model": vision_model}, depth="deep")

    assert harness.answered == [(vision_model["name"], expected)]


async def test_deep_default_applies_per_node_down_the_fallback_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(hung=frozenset({"qwen3.6-plus", "doubao-seed-2.0-pro"}))
    vision = {
        "model": {
            "provider": "qwen",
            "name": "qwen3.6-plus",
            "fallback": [{"provider": "doubao", "name": "doubao-seed-2.0-pro"}],
        },
        "fallbacks": [{"provider": "doubao", "name": _DOUBAO, "effort": "low"}],
    }

    await harness.run(monkeypatch, vision, depth="deep")

    assert harness.answered == [
        ("qwen3.6-plus", _medium_budget()),
        ("doubao-seed-2.0-pro", {"reasoning_effort": "medium"}),
        (_DOUBAO, {"reasoning_effort": "low"}),
    ]


async def test_quick_route_is_unchanged_for_an_untouched_vision_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness()

    await harness.run(monkeypatch, {"model": {"provider": "doubao", "name": _DOUBAO}}, depth=None)

    assert harness.answered == [(_DOUBAO, _disable_payload("doubao", _DOUBAO))]


async def test_the_main_model_of_the_same_agent_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 主模型与看图模型都是没设档位的 effort 厂商:只有 deep 看图路由补中档。
    main_name = "doubao-seed-2.0-pro"
    built: list[tuple[str, str | None, Any]] = []
    answered: list[tuple[str, Any]] = []
    call = {
        "name": "ask_image",
        "args": {"image_ref": _ref(), "question": "q", "depth": "deep"},
        "id": "call-1",
        "type": "tool_call",
    }
    main = _Scripted(responses=[AIMessage(content="", tool_calls=[call]), AIMessage(content="ok")])

    def fake_build_provider(model: ModelSpec, api_key: str, **kwargs: Any) -> Any:
        real = _REAL_BUILD_PROVIDER(model, api_key, **kwargs)
        payload = getattr(real, "thinking_payload", None)
        built.append((model.name, model.effort, payload))
        if model.name == main_name:
            return main
        return _VLProvider(model.name, payload, answered)

    monkeypatch.setattr("orchestrator.agent_factory._build_provider", fake_build_provider)
    spec = AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "vision-agent", "version": "1.0.0", "tenant": "t"},
            "spec": {
                "tenant_config": {},
                "model": {"provider": "doubao", "name": main_name},
                "vision": {"model": {"provider": "doubao", "name": _DOUBAO}},
                "system_prompt": {"template": "t"},
                "sandbox": {
                    "resources": {"cpu": "1.0", "memory": "1Gi"},
                    "network": {"egress": "proxy", "allowlist": []},
                    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
                },
            },
        }
    )
    async with make_checkpointer("memory") as cp:
        agent = await build_agent(
            spec,
            secret_store=LocalDevSecretStore.from_mapping({_KEY: "sk-test"}),
            checkpointer=cp,
            provider_key_resolver=_any_key,
            tool_env=ToolEnv(image_resolver=_resolver()),
        )
        cfg: RunnableConfig = {
            "configurable": {
                "thread_id": str(uuid4()),
                "tenant_id": str(_TENANT),
                "user_id": str(_USER),
                "run_id": str(uuid4()),
            }
        }
        await agent.graph.ainvoke(
            {"messages": [HumanMessage(content="look")], "step_count": 0, "max_steps": 5},
            config=cfg,
        )

    assert answered == [(_DOUBAO, {"reasoning_effort": "medium"})]
    main_builds = [(effort, payload) for name, effort, payload in built if name == main_name]
    assert main_builds and all(effort is None for effort, _ in main_builds)
    assert all(payload is None for _, payload in main_builds)  # 厂商默认,与改动前一致


# ---------------------------------------------------------------------------
# 审查回修 1 —— deep 路由首 token 超时低于 deep 上限,主看图模型卡死时备用链轮得到
# ---------------------------------------------------------------------------


def test_deep_router_first_token_timeout_is_below_the_deep_limit() -> None:
    assert agent_factory.VL_DEEP_FIRST_TOKEN_TIMEOUT_S == 30
    assert agent_factory.VL_DEEP_FIRST_TOKEN_TIMEOUT_S < ASK_IMAGE_DEEP_TIMEOUT_S


async def test_hung_vision_model_on_deep_fails_over_to_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 工具限时按 1/100 等(deep 60s → 0.6s);deep 路由首 token 注入 0.05s(真值 30s,同样
    # 低于 deep 上限)。主看图模型开流后一个 delta 都没有 → 路由换备用,备用边思考边作答,
    # 在 deep 窗口内答完,不落到 B-252 的快看重试。
    asked = _scale_vision_timeouts(monkeypatch, 100)
    monkeypatch.setattr("orchestrator.agent_factory.VL_DEEP_FIRST_TOKEN_TIMEOUT_S", 0.05)
    harness = _Harness(
        stalled=frozenset({"qwen3.6-plus"}), reasoning_first=frozenset({"doubao-seed-2.0-pro"})
    )
    vision = {
        "model": {"provider": "qwen", "name": "qwen3.6-plus"},
        "fallbacks": [{"provider": "doubao", "name": "doubao-seed-2.0-pro"}],
    }

    started = time.monotonic()
    content, _ = await harness.run(monkeypatch, vision, depth="deep")

    assert time.monotonic() - started < 10
    assert asked == [60.0]  # 只有 deep 一次,没有快看重试
    assert "doubao-seed-2.0-pro" in content and "timed out" not in content
    assert harness.answered == [("qwen3.6-plus", "stream"), ("doubao-seed-2.0-pro", "stream")]


# ---------------------------------------------------------------------------
# 审查回修 2 —— 平台默认档位 / 快看被厂商拒:退回配置原样的路由
# ---------------------------------------------------------------------------


def _deep_default_fallback_count(model: str) -> float:
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value(
        "expert_work_vision_deep_default_fallback_total", {"model": model}
    )
    return value or 0.0


async def test_rejected_deep_default_retries_on_the_plain_router() -> None:
    before = _deep_default_fallback_count("vl-x")
    deep, plain = _Rejects(LLMClientError("400 bad param")), _Answer("plain answer")
    tool = AskImageTool(
        vl_caller=deep,
        image_resolver=_resolver(),
        quick_vl_caller=_Answer("quick"),
        plain_vl_caller=plain,
        vl_model_name="vl-x",
    )

    result = await tool.call(
        {"image_ref": _ref(), "question": "q", "depth": "deep"}, ctx=ToolContext(tenant_id=_TENANT)
    )

    assert result.content == "plain answer"
    assert (deep.calls, plain.calls) == (1, 1)
    assert _deep_default_fallback_count("vl-x") == before + 1


@pytest.mark.parametrize(
    "error",
    [LLMUnauthorizedError("401"), classify_http_error("qwen", 403, "forbidden")],
    ids=["unauthorized", "forbidden"],
)
async def test_deep_auth_errors_are_not_retried_on_the_plain_router(error: Exception) -> None:
    deep, plain = _Rejects(error), _Answer()
    tool = AskImageTool(vl_caller=deep, image_resolver=_resolver(), plain_vl_caller=plain)

    with pytest.raises(type(error)):
        await tool.call(
            {"image_ref": _ref(), "question": "q", "depth": "deep"},
            ctx=ToolContext(tenant_id=_TENANT),
        )

    assert plain.calls == 0


async def test_rejected_quick_falls_back_to_the_plain_router_not_the_defaulted_one() -> None:
    quick, deep, plain = _Rejects(LLMClientError("400")), _Answer("deep"), _Answer("plain")
    tool = AskImageTool(
        vl_caller=deep, image_resolver=_resolver(), quick_vl_caller=quick, plain_vl_caller=plain
    )

    result = await tool.call(
        {"image_ref": _ref(), "question": "q"}, ctx=ToolContext(tenant_id=_TENANT)
    )

    assert result.content == "plain"
    assert (quick.calls, deep.calls, plain.calls) == (1, 0, 1)


_PRO = "gpt-5.5-pro"


@pytest.mark.parametrize(
    ("depth", "first"),
    [(None, {"reasoning_effort": "minimal"}), ("deep", {"reasoning_effort": "medium"})],
)
async def test_vendor_rejecting_both_tiers_still_answers_as_configured(
    monkeypatch: pytest.MonkeyPatch, depth: str | None, first: dict[str, str]
) -> None:
    # 没实调过的厂商既不认 minimal(快看)也不认 medium(平台默认):两种深度都退回配置原样
    # (什么都不发)那条路由,看图不至于整个不可用。
    harness = _WireHarness(
        rejected=({"reasoning_effort": "minimal"}, {"reasoning_effort": "medium"})
    )

    content, _ = await harness.run(
        monkeypatch, {"model": {"provider": "openai", "name": _PRO}}, depth=depth
    )

    assert "tool error" not in content and _PRO in content
    assert harness.answered == [(_PRO, first), (_PRO, None)]


@pytest.mark.parametrize(
    ("vision_model", "routers"),
    [
        ({"provider": "doubao", "name": _DOUBAO}, 3),  # deep(中档)+ quick + 配置原样
        ({"provider": "doubao", "name": _DOUBAO, "effort": "low"}, 2),  # 默认没改什么
        ({"provider": "glm", "name": "glm-5v-turbo"}, 2),
    ],
)
async def test_plain_router_is_built_only_when_the_default_changed_something(
    monkeypatch: pytest.MonkeyPatch, vision_model: dict[str, Any], routers: int
) -> None:
    harness = _Harness()

    await harness.run(monkeypatch, {"model": vision_model}, depth=None)

    assert len(harness.vl_router_timeouts) == routers


# ---------------------------------------------------------------------------
# 审查回修 5 —— deep 超时、快看又被拒:报错不再建议「先换快看」
# ---------------------------------------------------------------------------


async def test_deep_timeout_then_rejected_quick_does_not_suggest_the_quick_look() -> None:
    tool = AskImageTool(
        vl_caller=_Hanging(),
        image_resolver=_resolver(),
        quick_vl_caller=_Rejects(LLMClientError("400")),
        deep_timeout_s=0.05,
    )

    with pytest.raises(GuidedTimeoutError) as info:
        await tool.call(
            {"image_ref": _ref(), "question": "q", "depth": "deep"},
            ctx=ToolContext(tenant_id=_TENANT),
        )

    message = str(info.value)
    assert "quick look was rejected" in message
    assert "try the default quick look" not in message
    classified = classify_tool_error(tool_name="ask_image", error=info.value, spec=tool.spec)
    assert "quick" not in classified.advice
    assert "narrower" in classified.advice
