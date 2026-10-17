"""B-191 —— 模型正常结束了这一轮,却既没有回答也没有工具调用。

实测形态:思考模型把要发的工具调用(连同一大段参数)写进了思考内容,结束原因是正常的
``stop``,正文为空、``tool_calls`` 为空 —— 调用根本没发出去,run 却记成「成功、空回复」。
判定只看「正常结束 + 空」,不认任何厂商的标记格式;截断(B-105,``length``)与停顿
(B-186,路由盖章)各有各的判定和处理,不归这里。主循环先丢弃重来一次(同 B-158),
重试那一轮还这样就让 run 可见地失败。厂商内容审核拦下的空回复不重试(重来也是被拦),
直接以 :class:`ContentFilteredError` 失败。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from expert_work.common.observability.metrics import expert_work_counter
from orchestrator.llm.stall import STALLED_AFTER_S
from orchestrator.llm.truncation import is_truncated

#: 空回复次数。``outcome`` = 丢弃后重试("retried")、重试后仍空 run 失败("failed"),
#: 还是被厂商内容审核拦下 run 失败("filtered")。
empty_reply_total = expert_work_counter(
    "expert_work_llm_empty_reply_total",
    "Main-loop replies that ended with no answer and no tool call, by outcome.",
    ("provider", "model", "outcome"),
)

#: 不算回答内容的块:思考(Anthropic ``thinking`` / ``redacted_thinking``,LangChain 标准块
#: ``reasoning``)。其余任何块 —— 图片、文件,以及不认识的类型 —— 都算内容。
_THINKING_BLOCKS = frozenset({"thinking", "redacted_thinking", "reasoning"})

#: 厂商内容审核拦截的结束原因(``response_metadata`` 原样透传厂商的值):
#:   * OpenAI 兼容 ``finish_reason = content_filter`` —— 仓内已记的取值
#:     (``docs/superpowers/specs/2026-08-17-external-docs-readability-w3-audit.md``);
#:   * 智谱 ``finish_reason = sensitive`` —— 按其接口文档,未实调;
#:   * Anthropic ``stop_reason = refusal`` —— 按文档,未实调。
_FILTERED = {
    ("finish_reason", "content_filter"),
    ("finish_reason", "sensitive"),
    ("stop_reason", "refusal"),
}


def _has_content(message: AIMessage) -> bool:
    """去掉思考块与只有空白的文字,还剩任何东西就算有内容。"""
    content = message.content
    if isinstance(content, str):
        return bool(content.strip())
    for block in content:
        if isinstance(block, str):
            if block.strip():
                return True
            continue
        kind = block.get("type")
        if kind in _THINKING_BLOCKS:
            continue
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                return True
            continue
        return True
    return False


def is_empty_reply(message: AIMessage) -> bool:
    """正常结束的空回复:没有内容,也没有可执行的工具调用。

    ``invalid_tool_calls``(参数解析失败的调用)不算:图只派发 ``tool_calls``,只带它们的
    回复会被当成说完了收尾 —— 同样是「空着的成功」。截断与停顿不算(各有各的判定)。
    """
    if message.tool_calls:
        return False
    if is_truncated(message) or STALLED_AFTER_S in (message.response_metadata or {}):
        return False
    return not _has_content(message)


def is_filtered(message: AIMessage) -> bool:
    """结束原因是厂商内容审核拦截。"""
    meta = message.response_metadata or {}
    return any(meta.get(key) == value for key, value in _FILTERED)


class EmptyReplyError(RuntimeError):
    """重试过 3 次(B-194,次数同 ``builder.MAX_REPLY_RETRIES``),模型还是正常结束却既没有
    回答也没有工具调用。在 agent 节点、路由之后抛出,run 以可见错误结束。"""

    def __init__(self) -> None:
        super().__init__(
            "模型结束了这一轮,却既没有给出回答,也没有调用工具(已自动重试过 3 次)。"
            "可以重试;经常出现的话,可以换用别的模型。"
        )


class ContentFilteredError(RuntimeError):
    """回复被厂商内容审核拦下、一个字都没返回。不重试(同样的请求还会被拦),run 以可见错误
    结束。"""

    def __init__(self) -> None:
        super().__init__("这次回答被模型服务方的内容审核拦截了(没有内容返回)。可以换个说法重试。")
