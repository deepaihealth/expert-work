"""B-64 Task 10 —— ``ask_image`` 单次限时 + 默认「快看」。

真栈:同一个看图模型(Agent 配置开了思考)同类问题一次 11s、另一次 234s(输出 11,753
token,绝大部分是思考),整轮拖到 333s。平台对单次 ``ask_image`` 没有硬上限,路由的空闲
超时对「一直在吐思考 token」的推理模型不触发。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.persistence.token_usage_store import InMemoryTokenUsageStore
from expert_work.protocol import AgentSpec, ModelSpec, StructuredOutputSpec
from expert_work.protocol.model_catalog import catalog_entry
from expert_work.protocol.multimodal import ImageRef
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.middleware import LLMStreamStaleError
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import MiddlewareEnv, ToolEnv, agent_factory, build_agent
from orchestrator.llm.providers._streaming import LLMDelta, OpenAIStreamAssembler
from orchestrator.multimodal import InMemoryImageResolver, ResolvedImage
from orchestrator.tools._guards import TokenBudget
from orchestrator.tools.error_classifier import classify_tool_error
from orchestrator.tools.registry import ToolContext, ToolSpec
from orchestrator.tools.vision import ASK_IMAGE_TIMEOUT_S, AskImageTool

_TENANT = UUID("11111111-1111-1111-1111-111111111111")
_USER = UUID("33333333-3333-3333-3333-333333333333")
_THREAD = UUID("22222222-2222-2222-2222-222222222222")
_VL_USAGE = {"input_tokens": 800, "output_tokens": 40, "total_tokens": 840}


def _ref() -> str:
    return ImageRef(tenant_id=_TENANT, thread_id=_THREAD, image_id=uuid4(), ext=".png").to_uri()


def _resolver() -> InMemoryImageResolver:
    return InMemoryImageResolver(images={"any": ResolvedImage(media_type="image/png", data=b"PNG")})


@dataclass
class _Hanging:
    """VL 替身:永远不返回,记下自己有没有被取消。"""

    started: bool = False
    cancelled: bool = False

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
        on_delta: Callable[[LLMDelta], Awaitable[None]] | None = None,
    ) -> AIMessage:
        del messages, tools, output_schema, on_delta
        self.started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


@dataclass
class _Answer:
    text: str = "a chart"
    calls: int = 0

    async def __call__(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
        on_delta: Callable[[LLMDelta], Awaitable[None]] | None = None,
    ) -> AIMessage:
        del messages, tools, output_schema, on_delta
        self.calls += 1
        return AIMessage(content=self.text, usage_metadata=_VL_USAGE)


@dataclass
class _Meter:
    calls: list[AIMessage] = field(default_factory=list)

    async def __call__(self, response: AIMessage, *, tenant_id: UUID, user_id: UUID | None) -> None:
        del tenant_id, user_id
        self.calls.append(response)


# ---------------------------------------------------------------------------
# 10.1 单次硬上限
# ---------------------------------------------------------------------------


def test_default_limit_is_hermes_vision_analyze_default() -> None:
    assert ASK_IMAGE_TIMEOUT_S == 120
    assert AskImageTool(vl_caller=_Answer(), image_resolver=_resolver()).timeout_s == 120


@pytest.mark.asyncio
async def test_hung_vl_call_times_out_is_cancelled_and_charges_nothing() -> None:
    vl = _Hanging()
    meter = _Meter()
    budget = TokenBudget(limit=10_000)
    tool = AskImageTool(vl_caller=vl, image_resolver=_resolver(), usage_meter=meter, timeout_s=0.05)

    with pytest.raises(TimeoutError) as info:
        await tool.call(
            {"image_ref": _ref(), "question": "q"},
            ctx=ToolContext(tenant_id=_TENANT, token_budget=budget),
        )

    message = str(info.value)
    assert "timed out" in message and "narrower question" in message
    assert "deep" not in message  # 默认快看的超时不提 deep
    # 底层调用被真正取消,不是只停止等待。
    assert vl.started and vl.cancelled
    assert meter.calls == []
    assert budget.spent == 0
    # tools 节点把它分到超时类(transient),ask_image 只读 → 可重试一次。
    classified = classify_tool_error(tool_name="ask_image", error=info.value, spec=tool.spec)
    assert classified.error_class == "transient"


@pytest.mark.asyncio
async def test_deep_timeout_suggests_the_quick_look() -> None:
    tool = AskImageTool(vl_caller=_Hanging(), image_resolver=_resolver(), timeout_s=0.05)

    with pytest.raises(TimeoutError, match=r"depth='deep'.*quick look"):
        await tool.call(
            {"image_ref": _ref(), "question": "q", "depth": "deep"},
            ctx=ToolContext(tenant_id=_TENANT),
        )


@pytest.mark.asyncio
async def test_timeout_inside_the_vl_call_is_not_rewritten() -> None:
    # 路由/适配器自己抛的 TimeoutError 不是本层限时,原样上抛。
    @dataclass
    class _Raises:
        async def __call__(self, **kwargs: Any) -> AIMessage:
            del kwargs
            raise TimeoutError("provider read timeout")

    tool = AskImageTool(vl_caller=_Raises(), image_resolver=_resolver())

    with pytest.raises(TimeoutError, match=r"^provider read timeout$"):
        await tool.call({"image_ref": _ref(), "question": "q"}, ctx=ToolContext(tenant_id=_TENANT))


@pytest.mark.asyncio
async def test_run_deadline_smaller_than_limit_wins() -> None:
    vl = _Hanging()
    tool = AskImageTool(vl_caller=vl, image_resolver=_resolver(), timeout_s=30)

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="the time this run had left"):
        await tool.call(
            {"image_ref": _ref(), "question": "q"},
            ctx=ToolContext(tenant_id=_TENANT, deadline_at=time.monotonic() + 0.05),
        )

    assert time.monotonic() - started < 5
    assert vl.cancelled


@pytest.mark.asyncio
async def test_expired_run_deadline_does_not_call_the_vl_model() -> None:
    vl = _Answer()
    tool = AskImageTool(vl_caller=vl, image_resolver=_resolver())

    with pytest.raises(TimeoutError, match="no time left"):
        await tool.call(
            {"image_ref": _ref(), "question": "q"},
            ctx=ToolContext(tenant_id=_TENANT, deadline_at=time.monotonic() - 1),
        )

    assert vl.calls == 0


# ---------------------------------------------------------------------------
# 10.2 depth 参数
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "expected"),
    [({}, "quick"), ({"depth": "quick"}, "quick"), ({"depth": "deep"}, "deep")],
)
async def test_depth_picks_the_router_and_is_recorded(args: dict[str, str], expected: str) -> None:
    deep, quick = _Answer("deep"), _Answer("quick")
    tool = AskImageTool(vl_caller=deep, image_resolver=_resolver(), quick_vl_caller=quick)

    result = await tool.call(
        {"image_ref": _ref(), "question": "q", **args}, ctx=ToolContext(tenant_id=_TENANT)
    )

    assert result.content == expected
    assert result.meta["depth"] == expected


@pytest.mark.asyncio
async def test_quick_without_a_separate_router_uses_the_configured_one() -> None:
    deep = _Answer()
    tool = AskImageTool(vl_caller=deep, image_resolver=_resolver())

    result = await tool.call(
        {"image_ref": _ref(), "question": "q"}, ctx=ToolContext(tenant_id=_TENANT)
    )

    assert deep.calls == 1
    assert result.meta["depth"] == "quick"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["fast", "", "QUICK", 1, True])
async def test_invalid_depth_is_an_argument_error(bad: object) -> None:
    vl = _Answer()
    tool = AskImageTool(vl_caller=vl, image_resolver=_resolver())

    with pytest.raises(ValueError, match="'depth' must be one of") as info:
        await tool.call(
            {"image_ref": _ref(), "question": "q", "depth": bad}, ctx=ToolContext(tenant_id=_TENANT)
        )

    assert vl.calls == 0
    classified = classify_tool_error(tool_name="ask_image", error=info.value, spec=tool.spec)
    assert classified.error_class == "invalid_arguments"


def test_depth_is_in_the_schema_with_both_values() -> None:
    tool = AskImageTool(vl_caller=_Answer(), image_resolver=_resolver())
    depth = tool.spec.parameters["properties"]["depth"]
    assert depth["enum"] == ["quick", "deep"]
    assert "depth" not in tool.spec.parameters["required"]
    assert "depth='deep'" in tool.spec.description


# ---------------------------------------------------------------------------
# 构建:quick 路由按厂商「关思考」发送,deep 与今天一致
# ---------------------------------------------------------------------------

_KEY = "expert-work/dev/llm/any"
_REAL_BUILD_PROVIDER = agent_factory._build_provider
_REAL_BUILD_LLM_ROUTER = agent_factory.build_llm_router


@dataclass
class _StreamingVL:
    """流式 VL 替身:``stall`` 时第一个 delta 之前就挂住;否则先吐几段思考再给正文。"""

    name: str
    log: list[tuple[str, Any]]
    stall: bool

    async def complete(self, **kwargs: Any) -> AIMessage:
        raise AssertionError("VL must go through the streaming path")

    async def stream(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AsyncIterator[LLMDelta]:
        del messages, tools, output_schema
        self.log.append((self.name, "stream"))
        if self.stall:
            await asyncio.Event().wait()
        for _ in range(5):
            # 每段间隔都短于首 token 超时,累计远超它:持续吐思考的推理模型不会被误杀。
            await asyncio.sleep(0.05)
            yield LLMDelta(reasoning="thinking…")
        yield LLMDelta(content=f"seen by {self.name}")

    def new_stream_assembler(self) -> OpenAIStreamAssembler:
        return OpenAIStreamAssembler()


@dataclass
class _VLProvider:
    """记下「谁、带着哪个思考 payload」应答了。"""

    name: str
    thinking_payload: Any
    log: list[tuple[str, Any]]
    error: Exception | None = None

    async def complete(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AIMessage:
        del messages, tools, output_schema
        self.log.append((self.name, self.thinking_payload))
        if self.error is not None:
            raise self.error
        return AIMessage(content=f"seen by {self.name}", usage_metadata=_VL_USAGE)


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


@dataclass
class _Harness:
    """build_agent 真建一次,主模型脚本化调一次 ask_image;VL provider 记录应答。"""

    built_vl: list[tuple[str, Any]] = field(default_factory=list)
    answered: list[tuple[str, Any]] = field(default_factory=list)
    hung: frozenset[str] = frozenset()
    #: 流式 VL:这些模型在第一个 delta 之前就卡住(真实的「主模型卡死」形态)。
    stalled: frozenset[str] = frozenset()
    #: 流式 VL:这些模型先连续吐一段思考增量,再给正文。
    reasoning_first: frozenset[str] = frozenset()
    stream_deadline_s: int | None = None
    main: _Scripted = field(default_factory=lambda: _Scripted(responses=[]))
    #: VL 路由的 (first_token_timeout_s, provider_timeout_s)。
    vl_router_timeouts: list[tuple[float | None, float | None]] = field(default_factory=list)

    def fake_build_provider(self, entry: ModelSpec, api_key: str, **kwargs: Any) -> Any:
        if entry.name == "claude-sonnet-4-6":
            return self.main
        real = _REAL_BUILD_PROVIDER(entry, api_key, **kwargs)
        payload = getattr(real, "thinking_payload", None)
        self.built_vl.append((entry.name, payload))
        if entry.name in self.stalled or entry.name in self.reasoning_first:
            return _StreamingVL(entry.name, self.answered, stall=entry.name in self.stalled)
        error = LLMStreamStaleError("hung") if entry.name in self.hung else None
        return _VLProvider(entry.name, payload, self.answered, error)

    async def spy_build_llm_router(self, model: ModelSpec, **kwargs: Any) -> Any:
        if model.name != "claude-sonnet-4-6":
            self.vl_router_timeouts.append(
                (kwargs.get("first_token_timeout_s"), kwargs.get("provider_timeout_s"))
            )
        return await _REAL_BUILD_LLM_ROUTER(model, **kwargs)

    async def run(
        self, monkeypatch: pytest.MonkeyPatch, vision: dict[str, Any], depth: str | None
    ) -> tuple[str, InMemoryTokenUsageStore]:
        args: dict[str, Any] = {"image_ref": _ref(), "question": "what number?"}
        if depth is not None:
            args["depth"] = depth
        call = {"name": "ask_image", "args": args, "id": "call-1", "type": "tool_call"}
        self.main = _Scripted(
            responses=[
                AIMessage(content="", tool_calls=[call]),
                AIMessage(content="done"),
            ]
        )
        monkeypatch.setattr("orchestrator.agent_factory._build_provider", self.fake_build_provider)
        monkeypatch.setattr(
            "orchestrator.agent_factory.build_llm_router", self.spy_build_llm_router
        )
        store = InMemoryTokenUsageStore()
        async with make_checkpointer("memory") as cp:
            built = await build_agent(
                _spec(vision, stream_deadline_s=self.stream_deadline_s),
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
                }
            }
            state = await built.graph.ainvoke(
                {"messages": [HumanMessage(content="look")], "step_count": 0, "max_steps": 5},
                config=cfg,
            )
        tool_msgs = [m for m in state["messages"] if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 1
        return str(tool_msgs[0].content), store


async def _any_key(provider: str) -> list[str]:
    del provider
    return [f"secret://{_KEY}"]


def _spec(vision: dict[str, Any], *, stream_deadline_s: int | None = None) -> AgentSpec:
    extra: dict[str, Any] = {}
    if stream_deadline_s is not None:
        extra["stream_deadline_s"] = stream_deadline_s
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "ai-health-plan", "version": "1.2.0", "tenant": "t"},
            "spec": {
                **extra,
                "tenant_config": {},
                "model": {"provider": "anthropic", "name": "claude-sonnet-4-6"},
                "vision": vision,
                "system_prompt": {"template": "t"},
                "sandbox": {
                    "resources": {"cpu": "1.0", "memory": "1Gi"},
                    "network": {"egress": "proxy", "allowlist": []},
                    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
                },
            },
        }
    )


def _disable_payload(provider: str, name: str) -> dict[str, Any]:
    entry = catalog_entry(provider, name)
    assert entry is not None and entry.thinking is not None
    model = ModelSpec(provider=provider, name=name)  # type: ignore[arg-type]
    return agent_factory._thinking_disable_payload(model, entry)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "name"),
    [
        ("glm", "glm-5v-turbo"),
        ("qwen", "qwen3.6-plus"),
        ("doubao", "doubao-seed-2-1-pro-260628"),
        # always_thinking:关不掉,「关」落到最低档。
        ("glm", "glm-5.3-flash"),
    ],
)
async def test_default_quick_sends_the_vendor_thinking_off_payload(
    monkeypatch: pytest.MonkeyPatch, provider: str, name: str
) -> None:
    harness = _Harness()
    vision = {"model": {"provider": provider, "name": name, "thinking_enabled": True}}

    await harness.run(monkeypatch, vision, depth=None)

    expected = _disable_payload(provider, name)
    assert harness.answered == [(name, expected)]


@pytest.mark.asyncio
async def test_deep_sends_exactly_the_configured_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _Harness()
    configured = {"provider": "doubao", "name": "doubao-seed-2-1-pro-260628", "effort": "high"}

    await harness.run(monkeypatch, {"model": configured}, depth="deep")

    today = agent_factory._thinking_payload(ModelSpec.model_validate(configured))
    assert today is not None and today["thinking"]["type"] == "enabled"
    assert harness.answered == [("doubao-seed-2-1-pro-260628", today)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vision_model",
    [
        # 目录里没有思考开关。
        {"provider": "qwen", "name": "qwen3-vl-plus"},
        # 目录外。
        {"provider": "doubao", "name": "doubao-seed-2-1-pro"},
        # 已配成关思考:quick 与 deep 本来就一样。
        {"provider": "glm", "name": "glm-5v-turbo", "thinking_enabled": False},
    ],
)
async def test_no_thinking_to_turn_off_builds_one_router(
    monkeypatch: pytest.MonkeyPatch, vision_model: dict[str, Any]
) -> None:
    harness = _Harness()

    await harness.run(monkeypatch, {"model": vision_model}, depth=None)

    assert [name for name, _ in harness.built_vl] == [vision_model["name"]]
    assert len(harness.answered) == 1


@pytest.mark.asyncio
async def test_quick_router_turns_thinking_off_per_fallback_and_is_metered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 主 VL 卡死 → 备用接管;quick 路由上每个模型各用自己厂商的「关思考」,账记在备用名下。
    harness = _Harness(hung=frozenset({"qwen3.6-plus"}))
    vision = {
        "model": {"provider": "qwen", "name": "qwen3.6-plus"},
        "fallbacks": [
            {"provider": "doubao", "name": "doubao-seed-2.0-pro"},
            {"provider": "qwen", "name": "qwen3-vl-plus"},
        ],
    }

    content, store = await harness.run(monkeypatch, vision, depth=None)

    assert "doubao-seed-2.0-pro" in content  # 输出被 spotlight 围栏改写,只比模型名
    assert harness.answered == [
        ("qwen3.6-plus", _disable_payload("qwen", "qwen3.6-plus")),
        ("doubao-seed-2.0-pro", _disable_payload("doubao", "doubao-seed-2.0-pro")),
    ]
    rows = await store.list_for_tenant(tenant_id=_TENANT)
    vl_rows = [r for r in rows if r.model != "claude-sonnet-4-6"]
    assert [(r.provider, r.model, r.input_tokens) for r in vl_rows] == [
        ("doubao", "doubao-seed-2.0-pro", 800)
    ]


# ---------------------------------------------------------------------------
# Fix round 1 —— I-1:VL 路由的首 token / httpx 超时低于 ask_image 上限,备用链轮得到
# ---------------------------------------------------------------------------


def test_vl_first_token_timeout_is_below_the_ask_image_cap() -> None:
    assert agent_factory.VL_FIRST_TOKEN_TIMEOUT_S == 60
    assert agent_factory.VL_FIRST_TOKEN_TIMEOUT_S < ASK_IMAGE_TIMEOUT_S


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_deadline_s", "expected"),
    [
        (None, 60.0),  # manifest 默认值大于 60 → 60
        (300, 60.0),
        (30, 30.0),  # manifest 更小 → 取更小
        (0, None),  # 0 = 关掉 deadline,照旧尊重
    ],
)
async def test_vl_routers_use_the_capped_first_token_and_http_timeout(
    monkeypatch: pytest.MonkeyPatch, stream_deadline_s: int | None, expected: float | None
) -> None:
    harness = _Harness(stream_deadline_s=stream_deadline_s)
    # 有思考开关 → deep + quick 两个路由,两个都要吃到同一组超时。
    vision = {"model": {"provider": "qwen", "name": "qwen3.6-plus"}}

    await harness.run(monkeypatch, vision, depth=None)

    assert harness.vl_router_timeouts == [(expected, expected), (expected, expected)]


@pytest.mark.asyncio
async def test_hung_primary_fails_over_to_the_fallback_inside_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 主 VL 在第一个 token 之前就卡死。首 token 超时(注入 0.1s)先于 ask_image 上限
    # (120s)触发 → 路由换备用,备用在窗口内作答。
    monkeypatch.setattr("orchestrator.agent_factory.VL_FIRST_TOKEN_TIMEOUT_S", 0.1)
    harness = _Harness(
        stalled=frozenset({"qwen3-vl-plus"}), reasoning_first=frozenset({"glm-4.6v"})
    )
    vision = {
        "model": {"provider": "qwen", "name": "qwen3-vl-plus"},
        "fallbacks": [{"provider": "glm", "name": "glm-4.6v"}],
    }

    started = time.monotonic()
    content, _ = await harness.run(monkeypatch, vision, depth=None)

    assert time.monotonic() - started < 10
    assert "glm-4.6v" in content and "timed out" not in content
    assert harness.answered == [("qwen3-vl-plus", "stream"), ("glm-4.6v", "stream")]


@pytest.mark.asyncio
async def test_a_model_streaming_thinking_is_not_cut_by_the_first_token_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 60s 之所以安全:首 token 计时每来一个 delta 就重置,思考增量也算。这里思考累计
    # 0.25s,远超注入的 0.1s 首 token 超时,主模型照样作答,不换备用。
    monkeypatch.setattr("orchestrator.agent_factory.VL_FIRST_TOKEN_TIMEOUT_S", 0.1)
    harness = _Harness(reasoning_first=frozenset({"qwen3-vl-plus", "glm-4.6v"}))
    vision = {
        "model": {"provider": "qwen", "name": "qwen3-vl-plus"},
        "fallbacks": [{"provider": "glm", "name": "glm-4.6v"}],
    }

    content, _ = await harness.run(monkeypatch, vision, depth=None)

    assert "qwen3-vl-plus" in content
    assert harness.answered == [("qwen3-vl-plus", "stream")]


# ---------------------------------------------------------------------------
# Fix round 1 —— M-2 / M-4:超时的恢复建议与剩余时间文字
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_advice_forbids_the_identical_retry() -> None:
    tool = AskImageTool(vl_caller=_Hanging(), image_resolver=_resolver(), timeout_s=0.05)

    with pytest.raises(TimeoutError) as info:
        await tool.call({"image_ref": _ref(), "question": "q"}, ctx=ToolContext(tenant_id=_TENANT))

    classified = classify_tool_error(tool_name="ask_image", error=info.value, spec=tool.spec)
    assert classified.error_class == "transient"
    assert classified.retryable is False
    assert "safe to retry" not in classified.advice
    assert "Do not repeat the identical call" in classified.advice
    assert "narrower" in classified.advice and "quick" in classified.advice


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining_s", [0.05, -1.0])
async def test_no_time_left_says_so_without_retry_advice(remaining_s: float) -> None:
    # 0.05:按 run 剩余时间限时后超时;-1:已经没时间,VL 不调。两种都不许建议重试。
    tool = AskImageTool(vl_caller=_Hanging(), image_resolver=_resolver())

    with pytest.raises(TimeoutError) as info:
        await tool.call(
            {"image_ref": _ref(), "question": "q"},
            ctx=ToolContext(tenant_id=_TENANT, deadline_at=time.monotonic() + remaining_s),
        )

    message = str(info.value)
    assert "no time left" in message
    assert "narrower" not in message and "try again" not in message
    classified = classify_tool_error(tool_name="ask_image", error=info.value, spec=tool.spec)
    assert classified.retryable is False
    assert (
        "no time left" in classified.advice and "Do not call ask_image again" in classified.advice
    )
    assert "retry" not in classified.advice.lower()


@pytest.mark.asyncio
async def test_sub_second_remaining_time_is_not_printed_as_zero_seconds() -> None:
    tool = AskImageTool(vl_caller=_Hanging(), image_resolver=_resolver())

    with pytest.raises(TimeoutError) as info:
        await tool.call(
            {"image_ref": _ref(), "question": "q"},
            ctx=ToolContext(tenant_id=_TENANT, deadline_at=time.monotonic() + 0.05),
        )

    assert "less than 1 second" in str(info.value)
    assert "0 seconds" not in str(info.value)


def test_plain_timeouts_keep_the_generic_transient_advice() -> None:
    # 回归:只有 ask_image 的受引导超时换建议,其余 TimeoutError 照旧。
    tool = AskImageTool(vl_caller=_Answer(), image_resolver=_resolver())
    classified = classify_tool_error(
        tool_name="ask_image", error=TimeoutError("read timeout"), spec=tool.spec
    )
    assert classified.retryable is True
    assert "safe to retry once" in classified.advice


# ---------------------------------------------------------------------------
# Fix round 1 —— M-1:_thinking_off 递归进 model.fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quick_turns_thinking_off_on_a_nested_model_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(hung=frozenset({"qwen3.6-plus"}))
    vision = {
        "model": {
            "provider": "qwen",
            "name": "qwen3.6-plus",
            "fallback": [{"provider": "doubao", "name": "doubao-seed-2.0-pro"}],
        },
    }

    content, _ = await harness.run(monkeypatch, vision, depth=None)

    assert "doubao-seed-2.0-pro" in content
    assert harness.answered == [
        ("qwen3.6-plus", _disable_payload("qwen", "qwen3.6-plus")),
        ("doubao-seed-2.0-pro", _disable_payload("doubao", "doubao-seed-2.0-pro")),
    ]
