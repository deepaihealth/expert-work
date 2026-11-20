"""B-328 —— 一条大工具结果不再把对话卡死:单条预算(A②)与发送前的窗口检查(A③)。

设计:``docs/superpowers/specs/2026-11-20-b328-oversized-tool-result-stuck-design.md``。

三个数都由已有配置推出,不加新开关:

* **T** —— 压缩器的门槛 ``min(W x threshold_pct, absolute_cap_tokens)``。只管「从哪里开始
  省钱」(B-126);压不到它以下压缩器交回照发(A①,见 ``compressor``)。
* **W** —— **主模型**的窗口(与 T 同一个来源)。平台估算(消息 + 工具定义)超过整个窗口才
  「狠缩」一次,仍超才干净失败 —— 平台不再自己画一条比窗口小的失败线。
* **R** —— 单条预算 ``max(T // 5, 4000)``。只用于「换个方式能一小段一小段读回来」的结果。

两个函数都只改发给模型的那一份提示词(CM-C4):检查点、对话页、审计照旧是原文。同一份
输入每一步算出来逐字相同 —— 同一轮里不会「被截了又变回原文」,前缀缓存也稳。

**围栏(spotlight)**:工具结果在检查点里是「围栏包住的正文 + 围栏外的平台 notice /
footer」。截法是把正文开头一段重新包进**同一 nonce** 的开 / 关围栏,平台说明放在关围栏
之后;原来围栏外的 notice / footer 不搬原文,只用 ``tool_result_prune`` 同一套正则取出路径
/ 大小重新写。真正的关围栏是开围栏之后**第一个**同 nonce 的关标记:nonce 正文的作者猜不到,
而追加在后面的平台段(``<platform-context>`` 里的记忆块)可能带同 nonce 的围栏。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from expert_work.common.conversation_channel import is_hidden
from expert_work.common.observability import expert_work_counter
from expert_work.common.spotlight import DATAMARK_GLYPH
from expert_work.runtime.tokens import CharTokenEstimator, TokenEstimator, estimate_message
from orchestrator.context.compressor import ContextOverflowError
from orchestrator.context.tool_result_prune import (
    _PRUNE_TAG_CLOSE,
    _PRUNE_TAG_OPEN,
    _artifact_path,
    _collapsed_content,
    _footer_claimed_path,
    _footer_total_chars,
    _is_already_pruned,
)
from orchestrator.tools.error_classifier import RECOVERY_ADVISORY_OPEN
from orchestrator.tools.inputs_doc import CURRENT_INPUTS_REL
from orchestrator.tools.overflow import TOOL_RESULT_PATH_ARTIFACT_KEY, fit_prefix, make_preview
from orchestrator.tools.registry import ToolSpec

logger = logging.getLogger(__name__)

#: R 的下限(token)与 T 的比例(R = T / 5)。
RESULT_BUDGET_FLOOR_TOKENS = 4_000
_RESULT_BUDGET_DIVISOR = 5

#: ``ToolResult.meta`` 里工具当时解析出的真实路径(B-317 ``ResolvedPath.path``)。
SOURCE_PATH_META_KEY = "source_path"

#: 自带上限、重读还是同样大的结果:A② 不截(截了只会让模型反复重读、永远看不全)。
#: ``ask_image`` 是看图的结论,永不清理(``tool_result_prune._NEVER_PRUNE_TOOLS``)。
_NEVER_SHRINK_TOOLS = frozenset({"skill_view", "knowledge_read", "knowledge_search", "ask_image"})

_READ_DOCUMENT = "read_document"
_READ_FILE = "read_file"

_FENCE_OPEN_RE = re.compile(r"«UNTRUSTED nonce=([^»]+)»\n")

#: ``graph_builder.platform_context.PLATFORM_CONTEXT_OPEN`` —— 不能 import:graph_builder
#: 包会反过来 import 本模块(循环)。``test_b328_result_budget`` 钉两边相等。
PLATFORM_CONTEXT_OPEN = "<platform-context>"

#: 追加在工具结果末尾的平台段(恢复建议、``<platform-context>``)。A③ 在它们挂上之后跑,
#: 伪造的同名字样此时已被转义(``platform_context``),所以第一个出现处就是真的。
_APPENDED_TAILS = (f"\n\n{RECOVERY_ADVISORY_OPEN}", f"\n\n{PLATFORM_CONTEXT_OPEN}")

#: B-317 —— 跨轮会指向别的一轮的路径写法:旧消息的调用参数是这种形状时不写路径。
_UNSTABLE_PATH_MARKERS = (CURRENT_INPUTS_REL, "EXPERT_WORK_INPUTS_DIR")
#: B-317 —— 工具回执末尾平台补的那句真实路径(``builder._with_later_turns_path``)。只在关围栏
#: 之后找(那段是平台写的);路径只认 ``inputs/<run>/…`` 形状。
_LATER_TURNS_PATH_RE = re.compile(r"（之后的轮次请用 (inputs/[0-9a-f-]{36}/[^）\n]+)）")  # noqa: RUF001

_cm_result_shrunk_total = expert_work_counter(
    "expert_work_cm_result_shrunk_total",
    "Re-readable tool results cut to the per-result budget in the prompt view (B-328 A2).",
)
_cm_window_hard_shrink_total = expert_work_counter(
    "expert_work_cm_window_hard_shrink_total",
    "Pre-send window checks that hard-shrank the prompt (B-328 A3), by outcome.",
    ["outcome"],
)


def result_budget_tokens(threshold_tokens: int) -> int:
    """R = T / 5,下限 4,000 token。"""
    return max(RESULT_BUDGET_FLOOR_TOKENS, threshold_tokens // _RESULT_BUDGET_DIVISOR)


@dataclass(frozen=True)
class ContextBudget:
    """``agent_node`` 用的两个数:主模型窗口 W 与单条预算 R(工厂按 manifest 算好传入)。"""

    window_tokens: int
    result_tokens: int
    estimator: TokenEstimator | None = None


# ---------------------------------------------------------------- shared pieces


@dataclass(frozen=True)
class _Parts:
    """一条工具结果拆成:围栏 nonce(没开 spotlight 为 ``None``)、正文、正文之后的平台尾巴。"""

    nonce: str | None
    body: str
    tails: str


def _fenced(content: str) -> tuple[str, str, str] | None:
    """``(nonce, 围栏内正文, 关围栏之后的全部)``;不是以围栏开头(没开 spotlight)为 ``None``。"""
    match = _FENCE_OPEN_RE.match(content)
    if match is None:
        return None
    nonce = match.group(1)
    close_marker = f"\n«/UNTRUSTED nonce={nonce}»"
    close = content.find(close_marker, match.end())
    if close == -1:
        return None
    return nonce, content[match.end() : close], content[close + len(close_marker) :]


def _split(content: str, *, keep_tails: bool) -> _Parts:
    """拆开围栏。``keep_tails``:A③ 时把恢复建议 / ``<platform-context>`` 单独留下(原样
    挂回);围栏外的 notice / footer 一律丢掉、由调用方按路径重写。"""
    fenced = _fenced(content)
    if fenced is not None:
        nonce, body, outside = fenced
        return _Parts(nonce, body, _tails(outside, keep_tails))
    tails = _tails(content, keep_tails)
    return _Parts(None, content[: len(content) - len(tails)], tails)


def _tails(text: str, keep: bool) -> str:
    if not keep:
        return ""
    starts = [i for i in (text.find(tag) for tag in _APPENDED_TAILS) if i != -1]
    return text[min(starts) :] if starts else ""


def _fence(nonce: str | None, body: str) -> str:
    if nonce is None:
        return body
    return f"«UNTRUSTED nonce={nonce}»\n{body}\n«/UNTRUSTED nonce={nonce}»"


def _visible_len(text: str) -> int:
    """模型看到的字数:去掉数据标记字形(``datamark`` 只插不换,去掉就是原文)。"""
    return len(text) - text.count(DATAMARK_GLYPH)


def _call_args(messages: Sequence[BaseMessage]) -> dict[str, Mapping[str, Any]]:
    args: dict[str, Mapping[str, Any]] = {}
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls:
                if tc.get("id"):
                    args[str(tc["id"])] = tc.get("args") or {}
    return args


def _source_path(message: ToolMessage, args: Mapping[str, Any] | None) -> str | None:
    """工具当时解析出的真实路径;旧消息没有就退回调用参数 —— 但短路径 / 别名形状的参数到
    下一轮会指向别的一轮(B-317):那种只认关围栏之后平台自己写的「之后的轮次请用 …」那句
    (``builder._with_later_turns_path``),也没有就只说「原文件」不写路径。"""
    art = message.artifact
    if isinstance(art, dict):
        value = art.get(SOURCE_PATH_META_KEY)
        if isinstance(value, str) and value:
            return value
    raw = (args or {}).get("path")
    if not isinstance(raw, str) or not raw:
        return None
    if not any(m in raw for m in _UNSTABLE_PATH_MARKERS):
        return raw
    fenced = _fenced(message.content) if isinstance(message.content, str) else None
    later = _LATER_TURNS_PATH_RE.search(fenced[2]) if fenced is not None else None
    return later.group(1) if later is not None else None


def _copy_path(message: ToolMessage) -> str | None:
    """工作区里的全文副本(持久化路径优先,其次 footer 里取出的路径 —— 与清理闸同一套:
    只取正则认得的路径;围栏里伪造的 footer 被数据标记打断,认不出来)。"""
    path = _artifact_path(message)
    if path is None and isinstance(message.content, str):
        path = _footer_claimed_path(message.content)
    return path


def _recovery(message: ToolMessage, args: Mapping[str, Any] | None) -> str | None:
    """怎么接着读(spec A④)。``None`` = 没有无损的办法。"""
    copy = _copy_path(message)
    if copy is not None:
        return (
            f"The full text is saved at {copy} in your workspace; page through it with "
            "read_file (offset / limit)."
        )
    if message.name == _READ_DOCUMENT:
        source = _source_path(message, args)
        target = f"the original file {source}" if source else "the original file"
        return (
            f"To read further, call read_document again on {target} (it returns the text "
            "in pages and saves the full text to your workspace), or extract the part you "
            "need with exec_python or bash."
        )
    if message.name == _READ_FILE:
        source = _source_path(message, args)
        target = f"read_file on {source}" if source else "read_file on the same file"
        return f"Re-read it with {target} (offset / limit); the file may have changed since."
    return None


# ------------------------------------------------------------------- A② shrink


def _shrinkable(message: BaseMessage) -> bool:
    return (
        isinstance(message, ToolMessage)
        and isinstance(message.content, str)
        and message.name not in _NEVER_SHRINK_TOOLS
        and not _is_already_pruned(message.content)
        and (message.name == _READ_DOCUMENT or _copy_path(message) is not None)
    )


def _shrink_note(name: str, total: int, shown: int, recovery: str) -> str:
    return (
        f"\n\n[platform: this {name} result is {total:,} characters; only the first "
        f"{shown:,} are shown here to keep each result within this agent's per-result "
        f"budget. {recovery}]"
    )


def _shrink_one(
    message: ToolMessage,
    budget: int,
    count: Callable[[str], int],
    args: Mapping[str, Any] | None,
) -> ToolMessage:
    content = str(message.content)
    parts = _split(content, keep_tails=False)
    recovery = _recovery(message, args) or ""
    name = message.name or "tool"
    # 原来多大:footer 里记着全文大小就用它(上下文里那段本来就是截过的)。
    total = _footer_total_chars(content) or _visible_len(parts.body)

    def render(head: str) -> str:
        return _fence(parts.nonce, head) + _shrink_note(name, total, _visible_len(head), recovery)

    n = fit_prefix(parts.body, render, budget, count)
    update: dict[str, Any] = {"content": render(parts.body[:n])}
    copy = _copy_path(message)
    if copy is not None and _artifact_path(message) is None:
        # 旧消息只有 footer、没记路径:footer 被截掉了,把路径记进视图副本的 artifact
        # (不发给模型),后面的清理闸照样收成无损引用。
        art = message.artifact if isinstance(message.artifact, dict) else {}
        update["artifact"] = {**art, TOOL_RESULT_PATH_ARTIFACT_KEY: copy}
    return message.model_copy(update=update)


def shrink_oversized_results(
    messages: Sequence[BaseMessage], *, budget_tokens: int, estimator: TokenEstimator | None
) -> list[BaseMessage]:
    """A② —— 能分段重读的工具结果估算超过 R 的,截成「开头一段 + 怎么接着读」。

    只看大小、不看门槛和总量、不分新旧:符合条件且 > R 的一律截,≤ R 的一律不动。截的是
    ``read_document`` 的结果与带工作区副本的结果;技能、知识库、``ask_image`` 不截。
    返回新列表(``model_copy``:``artifact`` / ``id`` / 恢复建议标记原样带着,后面的闸照常
    认得它)。
    """
    est = estimator or CharTokenEstimator()
    args = _call_args(messages)
    out: list[BaseMessage] = []
    for m in messages:
        if (
            isinstance(m, ToolMessage)
            and _shrinkable(m)
            and estimate_message(m, est) > budget_tokens
        ):
            out.append(_shrink_one(m, budget_tokens, est.count, args.get(m.tool_call_id)))
            _cm_result_shrunk_total.inc()
        else:
            out.append(m)
    return out


# ------------------------------------------------------------- A③ window check


def tool_definition_tokens(tools: Sequence[ToolSpec], estimator: TokenEstimator) -> int:
    """工具定义的估算(名字 + 说明 + 参数 schema;与 HX-12 小池判断同一个口径)。"""
    return sum(
        estimator.count(
            json.dumps(
                {"name": t.name, "description": t.description, "parameters": dict(t.parameters)},
                ensure_ascii=False,
            )
        )
        for t in tools
    )


def _hard_note(name: str, total: int, recovery: str | None) -> str:
    tail = recovery or "The rest is not preserved in this prompt."
    return (
        f"\n\n[platform: this {name} result ({total:,} characters) was shortened to a "
        f"preview to fit the model's context window. {tail}]"
    )


def _preview_current(message: ToolMessage, args: Mapping[str, Any] | None) -> ToolMessage:
    """本步刚返回的结果缩到外置预览量(开头 2,000 + 结尾 1,000 字),包回同一 nonce 的围栏;
    恢复建议 / ``<platform-context>`` 原样挂回。"""
    parts = _split(str(message.content), keep_tails=True)
    preview = make_preview(parts.body)
    if preview == parts.body:
        return message
    note = _hard_note(message.name or "tool", _visible_len(parts.body), _recovery(message, args))
    content = _fence(parts.nonce, preview) + note + parts.tails
    return message.model_copy(update={"content": content})


def _collapse_prior(message: ToolMessage, args: Mapping[str, Any] | None) -> ToolMessage:
    """本步以前的结果收成一行引用(``tool_result_prune`` 的梯子;读文档 / 读文件用重读引用)。"""
    content = message.content
    if not isinstance(content, str) or _is_already_pruned(content):
        return message
    reread = (
        _recovery(message, args)
        if message.name in (_READ_DOCUMENT, _READ_FILE) and _copy_path(message) is None
        else None
    )
    if reread is not None:
        stub = (
            f"{_PRUNE_TAG_OPEN}\n[{message.name}] {_visible_len(content):,} chars elided "
            f"to fit the model's context window. {reread}\n{_PRUNE_TAG_CLOSE}"
        )
    else:
        stub = _collapsed_content(message, lossy_note="older context, not preserved") or content
    return message if stub == content else message.model_copy(update={"content": stub})


def hard_shrink(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """「狠缩」一次:本步以前的工具结果全收成一行引用,本步刚返回的缩到预览量。

    「本步刚返回的」= 最后一条助手消息之后的工具结果。图片 / 多模态内容与 ``ask_image``
    不动(与清理闸同一口径)。返回新列表。
    """
    last_ai = max((i for i, m in enumerate(messages) if isinstance(m, AIMessage)), default=-1)
    args = _call_args(messages)
    out: list[BaseMessage] = []
    for i, m in enumerate(messages):
        if not isinstance(m, ToolMessage) or not isinstance(m.content, str):
            out.append(m)
        elif m.name == "ask_image":
            out.append(m)
        elif i > last_ai:
            out.append(_preview_current(m, args.get(m.tool_call_id)))
        else:
            out.append(_collapse_prior(m, args.get(m.tool_call_id)))
    return out


def _breakdown(messages: Sequence[BaseMessage], est: TokenEstimator) -> tuple[int, int, int, int]:
    """(系统提示词, 平台段, 本轮用户输入, 历史) 的估算,给 ``over_window`` 报错用。"""
    system = sum(estimate_message(m, est) for m in messages if isinstance(m, SystemMessage))
    platform = 0
    if messages:
        last = messages[-1]
        text = last.content if isinstance(last.content, str) else ""
        at = text.find(f"\n\n{PLATFORM_CONTEXT_OPEN}")
        if at != -1:
            platform = est.count(text[at:])
    user = 0
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, HumanMessage) and not is_hidden(m):
            # 每轮第一步平台段就贴在这条用户消息末尾,已经算进 platform,不重复算。
            user = estimate_message(m, est) - (platform if i == len(messages) - 1 else 0)
            break
    total = sum(estimate_message(m, est) for m in messages)
    return system, platform, user, max(0, total - system - platform - user)


def fit_to_window(
    messages: Sequence[BaseMessage],
    tools: Sequence[ToolSpec],
    *,
    budget: ContextBudget,
) -> list[BaseMessage]:
    """A③ —— 发送前按「消息 + 工具定义」估一次,只防超过主模型的整个窗口 W。

    ≤ W 原样返回。> W 先 :func:`hard_shrink` 一次;缩完 ≤ W 返回缩后的(调用方用它替换本步
    的 ``messages``:缓存查 / 存、结构化重发、计费都用这一份);仍 > W 抛
    ``ContextOverflowError(reason="over_window")``,报错里写固定开销、本轮用户输入、历史各
    多少,以及 W。

    估算口径与三道闸相同(``estimate_message``):助手消息里工具调用的参数不计,图片按固定档
    计 —— 都可能比厂商实际少;估少了由厂商判,厂商说太长时缩一次重试是 PR R 的事。
    """
    est = budget.estimator or CharTokenEstimator()
    window = budget.window_tokens
    tool_tokens = tool_definition_tokens(tools, est)
    total = sum(estimate_message(m, est) for m in messages) + tool_tokens
    if total <= window:
        return list(messages)
    shrunk = hard_shrink(messages)
    after = sum(estimate_message(m, est) for m in shrunk) + tool_tokens
    if after <= window:
        _cm_window_hard_shrink_total.labels(outcome="fit").inc()
        logger.warning(
            "context.window_hard_shrink estimated_tokens=%d after=%d window=%d",
            total,
            after,
            window,
        )
        return shrunk
    _cm_window_hard_shrink_total.labels(outcome="over_window").inc()
    system, platform, user, history = _breakdown(shrunk, est)
    detail = (
        f"context overflow: estimated {after} tokens (messages + tool definitions, after "
        f"shrinking tool results) > model window {window}; fixed overhead "
        f"{system + tool_tokens + platform} (system prompt {system}, tool definitions "
        f"{tool_tokens}, platform context {platform}), this turn's user input {user}, "
        f"history {history}"
    )
    raise ContextOverflowError(
        estimated_tokens=after, threshold=window, reason="over_window", detail=detail
    )


def context_budget_for(
    *,
    window_tokens: int,
    threshold_pct: float,
    absolute_cap_tokens: int,
    estimator: TokenEstimator | None,
) -> ContextBudget:
    """工厂用:W = 主模型窗口,T 与压缩器同一个公式(``ContextCompressor.threshold_tokens``),
    R = :func:`result_budget_tokens`。压缩关着也按策略里的数算(R 只管单条结果的大小)。"""
    threshold = min(int(window_tokens * threshold_pct), absolute_cap_tokens)
    return ContextBudget(
        window_tokens=window_tokens,
        result_tokens=result_budget_tokens(threshold),
        estimator=estimator,
    )
