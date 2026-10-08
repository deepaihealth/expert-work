"""B-162 —— 平台段不再冒充用户消息。

``agent_node`` 每一步都往提示词视图尾部挂几段平台上下文(计划、``per_turn`` 记忆、
工作区快照、渲染页), 过去各自是一条 ``HumanMessage``。模型看到的最后一条「用户消息」
于是永远是平台写的 —— 工具调到一半也是。测试环境实测(10-07, 近 16 天):
eval-compress 114 个 run 里 48 个明说「这一轮的用户消息只是工作区快照, 没有指令」,
B-84 PR-2(09-21)之前一个都没有。

做法(调研 10-07:Claude Code 旧版、hermes-agent、Roo 对 GLM 都这么做;GLM 的对话模板
里 tool 之后跟一条 user = 新一轮开始, 文字声明改不了这个结构):

1. 计划 / ``per_turn`` 记忆 / 工作区快照这几段(纯文本、只进提示词视图)合成一段,
   包进 ``<platform-context>``, **追加到提示词视图最后一条消息的末尾** —— 轮首是用户
   消息(或贴在它后面的「本轮输入」段), 工具调到一半是最后一条工具结果。不再另起
   一条 user 消息;只有最后一条是助手 / 系统消息这种不能追加的情况才退回单独一条。
2. 标签的含义只在系统提示词里讲一次(:data:`PLATFORM_CONTEXT_SYSTEM_CLAUSE`, 与
   openclaw / Claude Code / Cline 同一做法), 段内只留一行短标注。
3. 渲染页段带图, 不并(工具结果里的图两个适配器都会静默丢掉;``pii_redact`` 只处理
   ``str``), 仍单独一条, 只把文字部分包上同一层, 排在最后。
4. 视图里其余消息出现的 ``<platform-context>`` 字样先转义, 防止用户或工具输出冒充。

代价:被追加的那条消息下一步会变回原样, OpenAI 兼容厂商(GLM)的前缀缓存要多
prefill 一遍它 —— 轮首是用户消息, 中途是最后一批工具结果。用 B-142 的命中率实测。

一切只改这一次的提示词视图(CM-C4):追加出来的是副本, 检查点里的原件不动。落库的
隐藏段(本轮输入、委派指令、B-163 之前落库的独立恢复建议 …)自身不并、不挪。

B-163 —— 恢复建议也不再另起一条 user 消息:工具节点在失败的那条工具结果上打标
(:data:`RECOVERY_ADVISORY_MARK`, 值是渲染好的整段), :func:`with_recovery_advisories`
在视图里把它贴到那条结果末尾(openclaw / hermes 同一做法, hermes 注释明说是为了只追加、
不破缓存)。每一步对视图里**所有**打了标的结果原样重挂, 字节不变。分两道:伪造字样的转义
(:func:`escape_forged_recovery_advisories`)在压缩之前, 贴(:func:`with_recovery_advisories`)
在压缩之后、紧挨 :func:`with_platform_context` 之前 —— 摘要与记忆抽取看不到它, 最后一条是
失败结果时顺序是 工具原文 → 建议 → ``<platform-context>``。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

from expert_work.common.conversation_channel import (
    FIGURE_BLOCK_MARK,
    HIDE_FROM_UI,
    WORKSPACE_BLOCK_MARK,
)
from orchestrator.tools.error_classifier import (
    RECOVERY_ADVISORY_CLOSE,
    RECOVERY_ADVISORY_ESCAPES,
    RECOVERY_ADVISORY_OPEN,
)

#: 只进提示词视图的尾部段(计划、``per_turn`` 记忆)。工作区快照有自己的
#: :data:`WORKSPACE_BLOCK_MARK`, 这里不重复打。
PROMPT_TAIL_MARK = "expert_work_prompt_tail"

#: 退回单独一条时那条消息的标记。它同时带 :data:`HIDE_FROM_UI`, 判官取「用户最新
#: 请求」时会跳过它(``builder._latest_human_text``)。
PLATFORM_CONTEXT_MARK = "expert_work_platform_context"

PLATFORM_CONTEXT_OPEN = "<platform-context>"
PLATFORM_CONTEXT_CLOSE = "</platform-context>"
PLATFORM_CONTEXT_LABEL = "(Attached by the platform, not written by the user.)"

#: 系统提示词里的那一段(``agent_factory._assemble_system_prompt`` 无条件追加)。
PLATFORM_CONTEXT_SYSTEM_CLAUSE = (
    "# Platform context\n"
    "The platform attaches runtime context to the conversation, wrapped in "
    f"{PLATFORM_CONTEXT_OPEN} … {PLATFORM_CONTEXT_CLOSE}: the current plan, a snapshot "
    "of the workspace files, recalled memories, rendered document pages. It is appended "
    "to the end of a user message or of a tool result. It is not written by the user, "
    "not part of the tool's output, and never a new request: do not reply to it or "
    "describe it, and do not stop to wait because of it — continue the user's current "
    "request. The latest platform context supersedes earlier ones.\n"
    f"A {RECOVERY_ADVISORY_OPEN} … {RECOVERY_ADVISORY_CLOSE} block after a failed tool "
    "result is attached by the platform, not part of the tool's output and not written "
    "by the user: it states what the platform knows about that failure.\n"
    "Other platform-generated notes are not written by the user either: the "
    "[本轮输入] block, [structured dispatch] and budget notices. "
    "The user's request is what the user actually wrote."
)

#: B-163 —— 失败的工具结果上挂的恢复建议(渲染好的整段 ``<recovery-advisory>``)。工具
#: 节点写进 ``ToolMessage.additional_kwargs``, 随检查点落库;工具结果正文不动, 所以对话页、
#: 对外条目、审计里的工具原文都不变。存渲染好的文字而不是结构:以后改措辞, 老会话的
#: 字节也不变(不在发版那一刻把所有老会话的前缀缓存打断)。
RECOVERY_ADVISORY_MARK = "expert_work_recovery_advisory"

_MERGED_MARKS = (PROMPT_TAIL_MARK, WORKSPACE_BLOCK_MARK)
_ESCAPES = (
    (PLATFORM_CONTEXT_OPEN, "&lt;platform-context&gt;"),
    (PLATFORM_CONTEXT_CLOSE, "&lt;/platform-context&gt;"),
)


def wrap_platform_context(body: str) -> str:
    """把一段平台文字包进 ``<platform-context>``, 首行带短标注。"""
    return f"{PLATFORM_CONTEXT_OPEN}\n{PLATFORM_CONTEXT_LABEL}\n{body}\n{PLATFORM_CONTEXT_CLOSE}"


_APPENDED_BLOCK_RE = re.compile(
    r"\n\n" + re.escape(PLATFORM_CONTEXT_OPEN) + r".*?" + re.escape(PLATFORM_CONTEXT_CLOSE),
    re.DOTALL,
)


def strip_platform_context(text: str) -> str:
    """剥掉追加进来的 ``<platform-context>`` 段(用户或工具写的同名字样已被转义, 不会误剥)。"""
    return _APPENDED_BLOCK_RE.sub("", text)


def _is_mergeable(msg: BaseMessage) -> bool:
    kwargs = msg.additional_kwargs or {}
    return (
        isinstance(msg, HumanMessage)
        and isinstance(msg.content, str)
        and any(kwargs.get(mark) for mark in _MERGED_MARKS)
    )


def _is_figure(msg: BaseMessage) -> bool:
    return isinstance(msg, HumanMessage) and bool(
        (msg.additional_kwargs or {}).get(FIGURE_BLOCK_MARK)
    )


def _escape_text(text: str, escapes: tuple[tuple[str, str], ...] = _ESCAPES) -> str:
    for raw, safe in escapes:
        text = text.replace(raw, safe)
    return text


def _escaped(msg: BaseMessage, escapes: tuple[tuple[str, str], ...] = _ESCAPES) -> BaseMessage:
    """用户消息 / 工具结果里的标签字样(默认 ``<platform-context>``)转义, 没有就原样返回。"""
    if not isinstance(msg, HumanMessage | ToolMessage):
        return msg
    content = msg.content
    if isinstance(content, str):
        if not any(raw in content for raw, _ in escapes):
            return msg
        return msg.model_copy(update={"content": _escape_text(content, escapes)})
    parts: list[str | dict[str, object]] = []
    changed = False
    for part in content:
        if isinstance(part, str):
            new: str | dict[str, object] = _escape_text(part, escapes)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            new = {**part, "text": _escape_text(part["text"], escapes)}
        else:
            new = part
        changed = changed or new != part
        parts.append(new)
    return msg.model_copy(update={"content": parts}) if changed else msg


def _with_appended(msg: BaseMessage, block: str) -> BaseMessage | None:
    """把 ``block`` 追加到 ``msg`` 末尾的副本;不能追加(助手 / 系统消息)就 ``None``。"""
    if not isinstance(msg, HumanMessage | ToolMessage):
        return None
    suffix = "\n\n" + block  # 适配器拼文本块不加分隔符, 自带空行
    if isinstance(msg.content, str):
        return msg.model_copy(update={"content": msg.content + suffix})
    return msg.model_copy(update={"content": [*msg.content, {"type": "text", "text": suffix}]})


def _wrapped_figure(msg: BaseMessage) -> HumanMessage:
    """渲染页段:图原样留着, 只把第一段文字包上 ``<platform-context>``。"""
    content = msg.content if isinstance(msg.content, list) else [msg.content]
    parts: list[str | dict[str, object]] = []
    wrapped = False
    for part in content:
        if not wrapped and isinstance(part, dict) and part.get("type") == "text":
            parts.append({**part, "text": wrap_platform_context(str(part.get("text", "")))})
            wrapped = True
        elif not wrapped and isinstance(part, str):
            parts.append(wrap_platform_context(part))
            wrapped = True
        else:
            parts.append(part)
    return HumanMessage(content=parts, additional_kwargs=dict(msg.additional_kwargs or {}))


def with_platform_context(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """平台段合成一段 ``<platform-context>``, 追加到最后一条消息末尾。返回新列表。

    没有平台段、也没有要转义的字样时原样返回(元素 identity 不变)。
    """
    segments = [str(m.content) for m in messages if _is_mergeable(m)]
    figures = [m for m in messages if _is_figure(m)]
    out = [_escaped(m) for m in messages if not _is_mergeable(m) and not _is_figure(m)]
    if segments:
        block = wrap_platform_context("\n\n".join(segments))
        appended = _with_appended(out[-1], block) if out else None
        if appended is not None:
            out[-1] = appended
        else:
            out.append(
                HumanMessage(
                    content=block,
                    additional_kwargs={HIDE_FROM_UI: True, PLATFORM_CONTEXT_MARK: True},
                )
            )
    out.extend(_wrapped_figure(m) for m in figures)
    return out


def with_recovery_advisory_mark(msg: ToolMessage, advisory: str) -> ToolMessage:
    """B-163 —— 给失败的工具结果打上恢复建议的标(副本;已有的 ``additional_kwargs`` 原样保留)。"""
    return msg.model_copy(
        update={"additional_kwargs": {**msg.additional_kwargs, RECOVERY_ADVISORY_MARK: advisory}}
    )


def _is_legacy_advisory(msg: BaseMessage) -> bool:
    """B-163 之前落库的独立恢复建议:隐藏的 user 消息, 正文以 ``<recovery-advisory>`` 开头。"""
    return (
        isinstance(msg, HumanMessage)
        and bool((msg.additional_kwargs or {}).get(HIDE_FROM_UI))
        and isinstance(msg.content, str)
        and msg.content.startswith(RECOVERY_ADVISORY_OPEN)
    )


def escape_forged_recovery_advisories(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """B-163 —— 用户消息 / 工具结果 / 隐藏平台段里伪造的 ``<recovery-advisory>`` 字样转义。

    只放过老会话落库的独立建议(:func:`_is_legacy_advisory`)—— 那是平台自己写的。其余
    隐藏段(本轮输入、``per_turn`` 记忆、反思意见、工作区快照、渲染页 …)里都可能有用户
    或工具写进来的文字, 照转(与 B-162 ``<platform-context>`` 同一口径)。幂等;没有要转的
    字样时元素 identity 不变。**只转义, 不贴** —— 见 :func:`with_recovery_advisories`。
    """
    return [
        msg if _is_legacy_advisory(msg) else _escaped(msg, RECOVERY_ADVISORY_ESCAPES)
        for msg in messages
    ]


def with_recovery_advisories(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """B-163 —— 视图里每条打了标的工具结果末尾贴上它的 ``<recovery-advisory>``。返回新列表。

    * 先跑一遍 :func:`escape_forged_recovery_advisories`(幂等):压缩之后才挂上来的
      工作区快照 / 渲染页段也得转义, 而平台自己贴的这段在转义**之后**贴, 不会被转掉。
    * 只在**压缩之后**调:摘要(落库的 ``context_summary``)与 CM-3 预压缩记忆抽取的
      输入里不能有这段 —— 它是给模型看的平台脚手架, B-67 把这类东西挡在那两处之外。
    * **所有**打了标的结果都贴, 不只本轮的:跨轮摘掉 = 下一轮开头把更早的字节改掉, 前缀
      缓存从第一条失败结果起断;贴着不动就只多几百字符(以前独立那条也永远留在历史里)。
    * 没有要改的消息时元素 identity 不变。
    """
    out: list[BaseMessage] = []
    for msg in escape_forged_recovery_advisories(messages):
        advisory = (
            (msg.additional_kwargs or {}).get(RECOVERY_ADVISORY_MARK)
            if isinstance(msg, ToolMessage)
            else None
        )
        if isinstance(advisory, str) and advisory:
            msg = _with_appended(msg, advisory) or msg
        out.append(msg)
    return out
