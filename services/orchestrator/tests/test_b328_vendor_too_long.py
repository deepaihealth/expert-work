"""B-328 PR R —— 厂商说「输入太长」时狠缩一次、重试一次(spec §4.2 / §6.2)。

正例只用核过出处的厂商原话,出处写在每条旁边;DeepSeek 自家的「太长」原话没核到,没有放进来。
反例里有几条是审查给的形状(标明了「原文没核到」):它们是旧的散词规则会误中的样子,照收。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.middleware import (
    CallNext,
    LLMClientError,
    LLMContextTooLongError,
    LLMRateLimitError,
    LLMServerError,
    MiddlewareChain,
    MiddlewareContext,
)
from orchestrator import GraphRunner, ToolRegistry, ToolSpec, build_react_graph
from orchestrator.context import ContextOverflowError
from orchestrator.context.result_budget import ContextBudget
from orchestrator.llm import LLMRouter, ProviderHandle
from orchestrator.llm.providers._errors import classify_http_error
from orchestrator.llm.providers.anthropic import _classify_anthropic_stream_error
from orchestrator.llm.providers.openai import _classify_stream_error
from orchestrator.tools.overflow import TOOL_RESULT_PATH_ARTIFACT_KEY

# ------------------------------------------------------------------ 厂商原话

ANTHROPIC_SPLIT = (
    "input length and `max_tokens` exceed context limit: {n} + {m} > {w}, "
    "decrease input length or `max_tokens` and try again"
)

#: 正例:说的是「输入太长」。
TOO_LONG = [
    # OpenAI —— 真实 API 回包(400 invalid_request_error, code context_length_exceeded),
    # 引自 github.com/langchain-ai/langchain/issues/16781。
    (
        "openai",
        "This model's maximum context length is 16385 tokens. However, your messages "
        "resulted in 16648 tokens. Please reduce the length of the messages.",
    ),
    # Anthropic —— 官方 Context windows 文档:输入本身超窗口回 400 invalid_request_error
    # 「prompt is too long」;完整写法引自 github.com/anthropics/claude-code/issues/59696。
    ("anthropic", "prompt is too long: 203284 tokens > 200000 maximum"),
    # 通义(DashScope / 百炼)—— 官方错误码文档(alibabacloud.com/help/en/model-studio/error-code)
    # 「Range of input length should be [1, xxx]」;数字取本仓库验收手册 §38.2 实测回包。
    ("dashscope", "Range of input length should be [1, 16000]"),
    # 智谱 / Z.ai —— 官方错误码表(docs.z.ai/api-reference/api-code):1261 / 400 / Prompt too long。
    ("zhipu", '{"error":{"code":"1261","message":"Prompt too long"}}'),
    # Moonshot / Kimi —— 官方 Kimi Code 错误参考(kimi.com/code/docs/.../error-reference):
    # 「Your request exceeded model token limit」(请求 tokens + max_tokens 超过模型规格)。
    ("kimi", "Your request exceeded model token limit: 262144"),
    # Anthropic —— 输入 + max_tokens 超窗口(老模型的预检;新模型改为接受后按
    # model_context_window_exceeded 停)。写法引自 anthropics/claude-code #52 / #408 的报错。
    # 是真的超窗口:按「输入 / 输出谁占大头」决定缩不缩(见下面 agent_node 用例)。
    ("anthropic", ANTHROPIC_SPLIT.format(n=188240, m=21333, w=200000)),
    # Gemini —— Google AI 开发者论坛多帖的真实回包(400 INVALID_ARGUMENT);Vertex AI 错误码
    # 文档把它归为「超过模型输入 token 上限」。
    (
        "gemini",
        "The input token count (35868) exceeds the maximum number of tokens allowed (32768).",
    ),
    # 审查 N2 给的形状:真的「太长」,排除词(image / quota)在正文远处,不该一票否决。
    (
        "openai",
        "This model's maximum context length is 16385 tokens. However, your messages "
        "resulted in 16648 tokens. Please reduce the length of the messages. "
        "(note: token counts include image inputs; request id req_quota_0001)",
    ),
]

#: 反例:同样是 4xx,但不是「输入太长」—— 缩提示词救不了,不该重试。
NOT_TOO_LONG = [
    # OpenAI —— max_tokens 太大(只说输出)。引自 community.zapier.com 与 OpenAI 社区的真实回包。
    (
        "openai",
        "max_tokens is too large: 32768. This model supports at most 4096 completion "
        "tokens, whereas you provided 32768.",
    ),
    # Anthropic —— max_tokens 超过模型输出上限。引自 anthropics/claude-code 的问题报告。
    (
        "anthropic",
        "max_tokens: 128001 > 128000, which is the maximum allowed number of output tokens "
        "for claude-opus-4-6",
    ),
    # Anthropic —— 图片太大,正文以 ``messages.…`` 路径开头(审查 X2 点名的那种)。
    # 引自 anthropics/claude-code 的问题报告。
    (
        "anthropic",
        "messages.0.content.1.image.source.base64: image exceeds 5 MB maximum: "
        "5607404 bytes > 5242880 bytes",
    ),
    # 通义 —— 官方错误码文档:「Range of max_tokens should be [1, xxx]」(只说输出)。
    ("dashscope", "Range of max_tokens should be [1, 8192]"),
    # OpenAI —— 说的是 prompt,但不是长度(400 invalid_prompt)。引自 OpenAI 社区多帖的真实回包。
    (
        "openai",
        "Invalid prompt: your prompt was flagged as potentially violating our usage policy. "
        "Please try again with a different prompt.",
    ),
    # 审查 N2 给的形状:短语旁边就说的是限流 —— 不认。原文没核到。
    ("gateway", "Rate limit exceeded: input length of 120000 tokens per minute is allowed."),
    # 以下四条是审查(R1)给的形状:按「输入词 + 长度词」规则会误中。原文没核到,形状照收。
    ("anthropic", "thinking.enabled.budget_tokens: Input should be greater than or equal to 1024"),
    ("openai", "In context=('properties', 'name'), 'maxLength' is not permitted."),
    ("openai", "In context=('properties', 'age'), 'maximum' is not permitted."),
    ("openai", "The input image exceeds the maximum size allowed."),
    # OpenAI —— 参数类型错误。引自 portkey.ai 错误库收录的 OpenAI 回包。
    ("openai", "Invalid type for 'user': expected a string, but got an integer instead."),
]

#: 429 每分钟 token 限额 —— 引自 OpenAI 社区帖子的真实回包。
TPM_429 = (
    "Rate limit reached for gpt-4-turbo-preview in organization org-x on tokens per min "
    "(TPM): Limit 30000, Used 4811, Requested 28049. Please try again in 5.716s."
)


@pytest.mark.parametrize(("vendor", "body"), TOO_LONG)
def test_vendor_too_long_wording_is_recognised(vendor: str, body: str) -> None:
    err = classify_http_error(vendor, 400, body)
    assert isinstance(err, LLMContextTooLongError)
    assert isinstance(err, LLMClientError)  # 仍是 4xx:不重试、不换厂商
    assert err.status == 400


@pytest.mark.parametrize(("vendor", "body"), NOT_TOO_LONG)
def test_other_client_errors_are_not_too_long(vendor: str, body: str) -> None:
    err = classify_http_error(vendor, 400, body)
    assert type(err) is LLMClientError


def test_tpm_rate_limit_is_still_a_rate_limit() -> None:
    assert isinstance(classify_http_error("openai", 429, TPM_429), LLMRateLimitError)


def test_openai_stream_error_saying_too_long_is_not_a_server_error() -> None:
    err = _classify_stream_error(
        {"message": TOO_LONG[0][1], "type": "invalid_request_error", "code": None}
    )
    assert isinstance(err, LLMContextTooLongError)


def test_openai_stream_rate_limit_text_is_not_too_long() -> None:
    assert type(_classify_stream_error({"message": TPM_429})) is LLMServerError


def test_anthropic_stream_error_saying_too_long_is_not_a_server_error() -> None:
    event = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": TOO_LONG[1][1]},
    }
    assert isinstance(_classify_anthropic_stream_error(event), LLMContextTooLongError)


def test_anthropic_stream_overloaded_stays_a_server_error() -> None:
    # 官方 Errors 文档:529 overloaded_error。
    event = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    assert type(_classify_anthropic_stream_error(event)) is LLMServerError


# ------------------------------------------------------------------ agent_node


def _history() -> list[BaseMessage]:
    """一条更早的大结果(有工作区副本,狠缩后收成一行引用)+ 本步刚返回的小结果。"""
    return [
        HumanMessage(content="go"),
        AIMessage(content="", tool_calls=[{"name": "web_search", "args": {}, "id": "c-old"}]),
        ToolMessage(
            content="w" * 8_000,
            tool_call_id="c-old",
            name="web_search",
            artifact={TOOL_RESULT_PATH_ARTIFACT_KEY: ".tool_results/r/c-old-web_search.txt"},
        ),
        AIMessage(content="", tool_calls=[{"name": "bash", "args": {}, "id": "c-now"}]),
        ToolMessage(content="ok", tool_call_id="c-now", name="bash"),
    ]


def _web(prompt: Sequence[BaseMessage]) -> str:
    (found,) = [m for m in prompt if isinstance(m, ToolMessage) and m.name == "web_search"]
    return str(found.content)


def _too_long() -> LLMContextTooLongError:
    return LLMContextTooLongError("anthropic 400: " + TOO_LONG[1][1])


@dataclass
class _Scripted:
    """按脚本:异常就抛,回答就回。记下每次的提示词。"""

    script: list[BaseException | AIMessage]
    prompts: list[list[BaseMessage]] = field(default_factory=list)

    async def __call__(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec], **_: Any
    ) -> AIMessage:
        self.prompts.append(list(messages))
        item = self.script[min(len(self.prompts) - 1, len(self.script) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


@dataclass
class _Spy:
    anchor: str
    key: str
    log: list[list[BaseMessage]]
    name: str = "b328_r_spy"
    after: tuple[str, ...] = ()
    before: tuple[str, ...] = ()

    async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> None:
        self.log.append(list(ctx.payload[self.key]))
        await call_next(ctx)


async def _run(llm: Any, **graph_kwargs: Any) -> dict[str, list[list[BaseMessage]]]:
    looked: list[list[BaseMessage]] = []
    stored: list[list[BaseMessage]] = []
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=ToolRegistry(),
                before_llm_chain=MiddlewareChain(
                    "before_llm_call", (_Spy("before_llm_call", "messages", looked),)
                ),
                after_llm_chain=MiddlewareChain(
                    "after_llm_call", (_Spy("after_llm_call", "prompt_messages", stored),)
                ),
                **graph_kwargs,
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        await compiled.ainvoke(
            {"messages": _history(), "step_count": 0, "max_steps": 5}, config=cfg
        )
    return {"looked": looked, "stored": stored}


@pytest.mark.asyncio
async def test_vendor_too_long_shrinks_once_and_retries_once() -> None:
    llm = _Scripted(script=[_too_long(), AIMessage(content="好")])
    seen = await _run(llm)
    assert len(llm.prompts) == 2
    assert _web(llm.prompts[0]) == "w" * 8_000
    assert _web(llm.prompts[1]).startswith("<tool-result-overflow>")  # 第二次发的是狠缩后的
    # 缓存按狠缩后的提示词重查;被拒的那次不计费、不写缓存(after chain 只跑一次)。
    assert len(seen["looked"]) == 2
    assert _web(seen["looked"][1]).startswith("<tool-result-overflow>")
    assert len(seen["stored"]) == 1
    assert _web(seen["stored"][0]).startswith("<tool-result-overflow>")


@pytest.mark.asyncio
async def test_vendor_too_long_twice_fails_cleanly_without_a_third_call() -> None:
    llm = _Scripted(script=[_too_long(), _too_long(), AIMessage(content="不该到这里")])
    with pytest.raises(ContextOverflowError) as exc_info:
        await _run(llm)
    assert exc_info.value.reason == "vendor_rejected"
    assert "prompt is too long: 203284 tokens > 200000 maximum" in str(exc_info.value)
    assert "platform estimate" in str(exc_info.value)
    assert len(llm.prompts) == 2


@dataclass
class _Provider:
    script: list[BaseException | AIMessage]
    prompts: list[list[BaseMessage]] = field(default_factory=list)

    async def complete(
        self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
    ) -> AIMessage:
        self.prompts.append(list(messages))
        item = self.script[min(len(self.prompts) - 1, len(self.script) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.mark.asyncio
async def test_only_the_primary_window_is_checked_and_a_fallback_too_long_is_retried() -> None:
    """主模型窗口够大,发送前不狠缩;主模型 5xx、路由换到备用,备用说太长 → 狠缩并重试一次
    (重试仍从主模型开始,主模型还宕着就再走到备用,这次发的是狠缩后的提示词)。"""
    primary = _Provider(script=[LLMServerError("primary 503")])
    fallback = _Provider(script=[_too_long(), AIMessage(content="好")])
    router = LLMRouter(
        providers=[
            ProviderHandle(provider=primary, key="p"),
            ProviderHandle(provider=fallback, key="f"),
        ]
    )
    await _run(router, context_budget=ContextBudget(window_tokens=1_000_000, result_tokens=4_000))
    assert len(primary.prompts) == 2
    assert len(fallback.prompts) == 2
    assert _web(fallback.prompts[0]) == "w" * 8_000  # 发送前没有狠缩
    assert _web(fallback.prompts[1]).startswith("<tool-result-overflow>")


# ------------------------------------------------------------ 审查第二轮(PR R)补充

#: 网关在流式中途发的限流事件(审查 R1 给的形状);Anthropic 429 的同类写法见第三方转引。
STREAM_RATE_LIMIT = (
    "This request would exceed the rate limit for your organization of 30,000 input "
    "tokens per minute."
)


def test_stream_rate_limit_event_mentioning_input_tokens_stays_retryable() -> None:
    assert type(_classify_stream_error({"message": STREAM_RATE_LIMIT})) is LLMServerError


#: OpenAI「输入 + 要求的输出」写法(引自 OpenAI 社区多帖的真实回包)。
def _split_error(messages: int, completion: int, window: int = 128_000) -> LLMContextTooLongError:
    return LLMContextTooLongError(
        f"openai 400: This model's maximum context length is {window} tokens. However, you "
        f"requested {messages + completion} tokens ({messages} in the messages, {completion} "
        "in the completion). Please reduce the length of the messages or completion."
    )


@pytest.mark.asyncio
async def test_output_side_rejection_is_not_retried() -> None:
    """输入只占窗口的一小部分、是要求的输出把它撑爆:缩输入救不了,不重试,报错写清楚。"""
    llm = _Scripted(script=[_split_error(4_000, 136_000), AIMessage(content="不该到这里")])
    with pytest.raises(ContextOverflowError) as exc_info:
        await _run(llm)
    assert len(llm.prompts) == 1
    assert exc_info.value.reason == "vendor_rejected"
    assert "input + requested output exceeds the window (4000 input + 136000 output" in str(
        exc_info.value
    )


@pytest.mark.asyncio
async def test_input_side_split_rejection_is_retried() -> None:
    llm = _Scripted(script=[_split_error(100_000, 30_000), AIMessage(content="好")])
    await _run(llm)
    assert len(llm.prompts) == 2


@dataclass
class _AlwaysHit:
    """缓存替身:没要求旁路就给一个缓存回答。"""

    name: str = "b328_r_cache"
    anchor: str = "before_llm_call"
    after: tuple[str, ...] = ()
    before: tuple[str, ...] = ()

    async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> None:
        if not ctx.payload.get("llm_cache_bypass"):
            ctx.payload["llm_cache_hit"] = AIMessage(
                content="", tool_calls=[{"name": "bash", "args": {}, "id": "cached"}]
            )
        await call_next(ctx)


@pytest.mark.asyncio
async def test_wrap_up_retry_does_not_take_a_cached_answer() -> None:
    """预算用尽的收尾那一步本来不吃缓存;被拒后的重试也不能吃。"""
    llm = _Scripted(script=[_too_long(), AIMessage(content="收尾")])
    async with make_checkpointer("memory") as cp:
        compiled = GraphRunner(checkpointer=cp).compile(
            build_react_graph(
                llm_caller=llm,
                tool_registry=ToolRegistry(),
                before_llm_chain=MiddlewareChain("before_llm_call", (_AlwaysHit(),)),
            )
        )
        cfg: RunnableConfig = {"configurable": {"thread_id": str(uuid4())}}
        final = await compiled.ainvoke(
            {"messages": _history(), "step_count": 5, "max_steps": 5}, config=cfg
        )
    assert len(llm.prompts) == 2
    assert final["messages"][-1].content == "收尾"


@pytest.mark.asyncio
async def test_rejection_log_carries_no_vendor_text(caplog: pytest.LogCaptureFixture) -> None:
    llm = _Scripted(script=[_too_long(), AIMessage(content="好")])
    with caplog.at_level("WARNING"):
        await _run(llm)
    # 这条路径上**所有**日志(重试模块、agent_node、路由……)都不许带厂商原话。
    assert any(r.name == "orchestrator.graph_builder.vendor_too_long" for r in caplog.records)
    assert all("203284" not in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_structured_resend_is_shrunk_and_retried_once() -> None:
    from expert_work.protocol import StructuredOutputSpec

    spec = StructuredOutputSpec(
        schema={
            "type": "object",
            "properties": {"score": {"type": "integer"}},
            "required": ["score"],
            "additionalProperties": False,
        },
        name="verdict",
    )
    llm = _Scripted(
        script=[AIMessage(content="not json"), _too_long(), AIMessage(content='{"score": 4}')]
    )
    await _run(llm, output_schema=spec)
    assert len(llm.prompts) == 3
    assert _web(llm.prompts[1]) == "w" * 8_000
    assert _web(llm.prompts[2]).startswith("<tool-result-overflow>")


def _anthropic_split(n: int, m: int, w: int = 200_000) -> LLMContextTooLongError:
    return LLMContextTooLongError("anthropic 400: " + ANTHROPIC_SPLIT.format(n=n, m=m, w=w))


@pytest.mark.asyncio
async def test_anthropic_input_heavy_split_is_shrunk_and_retried() -> None:
    llm = _Scripted(script=[_anthropic_split(188_240, 21_333), AIMessage(content="好")])
    await _run(llm)
    assert len(llm.prompts) == 2
    assert _web(llm.prompts[1]).startswith("<tool-result-overflow>")


@pytest.mark.asyncio
async def test_anthropic_output_heavy_split_is_not_retried() -> None:
    llm = _Scripted(script=[_anthropic_split(5_000, 199_000), AIMessage(content="不该到这里")])
    with pytest.raises(ContextOverflowError) as exc_info:
        await _run(llm)
    assert len(llm.prompts) == 1
    assert "(5000 input + 199000 output > 200000)" in str(exc_info.value)


class _Zero:
    def count(self, text: str) -> int:
        del text
        return 0


@pytest.mark.asyncio
async def test_structured_resend_failure_uses_the_main_estimator() -> None:
    from expert_work.protocol import StructuredOutputSpec

    spec = StructuredOutputSpec(
        schema={"type": "object", "properties": {"score": {"type": "integer"}}},
        name="verdict",
    )
    llm = _Scripted(script=[AIMessage(content="not json"), _too_long(), _too_long()])
    budget = ContextBudget(window_tokens=10**9, result_tokens=10**9, estimator=_Zero())
    with pytest.raises(ContextOverflowError) as exc_info:
        await _run(llm, output_schema=spec, context_budget=budget)
    assert "platform estimate 0 tokens" in str(exc_info.value)
