"""对话可见轮次 + ``channel`` 判定 —— 全平台**唯一**一份实现。

判定规则来自 ``docs/superpowers/specs/2026-07-30-conversation-output-channels-design.md``:
助手轮是 ``final`` 当且仅当它是**所在段落里最后一条可见轮次**且**不带
``tool_calls``**;其余一律 ``commentary``。段落由用户消息分隔。

为什么抽到 common:这条规则原本只写在
``control_plane.transcript.extract_turns`` 里,而对话条目
(:mod:`expert_work.common.conversation_derive`)要给出同一个 ``channel``。
两处各写一份,下次给规则加约束时必然只落到一处 —— 本仓库为这个模式付过
学费。orchestrator 不能 import control-plane,所以共享点只能在这里。

调用方各自决定要不要过滤隐藏消息(``include_hidden``),判定本身不变。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: 编排层自己写进 checkpoint 的脚手架标记(CM-1 ``<recovery-advisory>`` /
#: reflect 反馈 / 循环检测提醒)。带这个标记的消息进模型、进审计,但不进
#: 任何面向用户或第三方的视图。
HIDE_FROM_UI = "expert_work_hide_from_ui"

#: 定时任务投递的助手消息标记(``trigger_delivery.inject_delivery``)。它
#: **自己开一个段落** —— 否则它会被接到用户上一个真实提问的段尾,把那一段
#: 的 ``final`` 抢走。
SCHEDULED_DELIVERY = "expert_work_scheduled_delivery"

#: ``channel`` 的两个取值。与 ``conversation_items.CHANNELS`` 的一致性由
#: 契约测试钉住(那份是对外词表,这份是判定实现)。
CHANNEL_FINAL = "final"
CHANNEL_COMMENTARY = "commentary"


def message_field(msg: Any, name: str, default: Any = None) -> Any:
    """读一条消息的某个字段,**对象形态与 dict 形态都吃**。

    这是全部消息字段读取的唯一入口,别在别处写 ``getattr(msg, ...)``。

    为什么必须两种都吃:三条产出路径喂进来的形态不同。会话历史读 checkpoint,
    拿到的是 ``BaseMessage`` 对象;而实时 SSE 与单 run 回放喂的是 ``updates``
    帧 —— ``orchestrator/sse.py`` 在 publish 之前就把 chunk 过了
    ``_to_jsonable``,``BaseMessage`` 在那里变成 ``model_dump()`` 的 dict
    (实时帧与落库行是同一个对象,所以两条路径都是 dict)。

    只用 ``getattr`` 的话 dict 形态取不到任何字段,推导会**静默返回空列表**
    而不是报错 —— spec §十一 点名的那种失败方式。
    """
    if isinstance(msg, Mapping):
        value = msg.get(name, default)
    else:
        value = getattr(msg, name, default)
    # ``model_dump()`` 会把没设的字段落成显式 ``None``(如 ``name``);调用方
    # 要的是「缺席」语义,与对象形态对齐。
    return default if value is None else value


def message_text(content: Any) -> str:
    """把 LangChain 消息的 ``content``(字符串或内容块列表)拍平成文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    return ""


def _kwargs(msg: Any) -> dict[str, Any]:
    ak: dict[str, Any] = message_field(msg, "additional_kwargs") or {}
    return ak


def is_hidden(msg: Any) -> bool:
    """这条消息是否是不该出现在对外视图里的编排层脚手架。"""
    return bool(_kwargs(msg).get(HIDE_FROM_UI))


def has_tool_calls(msg: Any) -> bool:
    """助手消息是否带工具调用(带 = 它还没说完,必然是 ``commentary``)。"""
    return message_field(msg, "type") == "ai" and bool(message_field(msg, "tool_calls"))


def opens_segment(msg: Any) -> bool:
    """这条消息是否**自己**开启一个新段落。

    只看这条消息自己的类型与 kwargs,绝不看它在列表里的位置 —— 位置无关
    是段落边界能跨 ``include_hidden`` 稳定的前提。
    """
    mtype = message_field(msg, "type")
    if mtype == "human":
        # 隐藏的 human 是脚手架,不是用户开的新一段。
        return not is_hidden(msg)
    if mtype == "ai":
        return bool(_kwargs(msg).get(SCHEDULED_DELIVERY))
    return False


@dataclass(frozen=True, slots=True)
class VisibleTurn:
    """一条带文本的可见用户/助手轮次。

    ``seq`` 是它在输入列表里的下标 —— 调用方拿它回查原消息(时间戳、
    run 归属、附件都在原消息上,本模块不碰)。
    """

    seq: int
    #: ``"user"`` / ``"assistant"``。
    role: str
    text: str
    #: 助手轮的 ``final`` / ``commentary``;用户轮恒为 ``None``。
    channel: str | None


def visible_turns(raw_messages: Sequence[Any], *, include_hidden: bool = True) -> list[VisibleTurn]:
    """抽出带文本的用户/助手轮次,并给助手轮定 ``channel``。

    工具/系统消息不在此列(它们不是「轮次」);正文空白的 human/ai 也丢弃
    —— 空气泡不该渲染,也不该充当段落里的「最后一条」。

    ``include_hidden=True``(默认)= 忠实视图,脚手架也算一轮;``False`` =
    对外视图。隐藏消息从不开启段落,所以过滤它们不会移动段落边界;但它若
    正好排在某条助手轮之后,两种视图给那条助手轮的 ``channel`` 仍可能不同
    (忠实视图里它「后面还有一行」)。
    """
    collected: list[tuple[int, str, str, bool, bool]] = []
    for seq, msg in enumerate(raw_messages):
        mtype = message_field(msg, "type")
        if mtype not in ("human", "ai"):
            continue
        if not include_hidden and is_hidden(msg):
            continue
        text = message_text(message_field(msg, "content", ""))
        if not text.strip():
            continue
        collected.append((seq, mtype, text, has_tool_calls(msg), opens_segment(msg)))

    out: list[VisibleTurn] = []
    for i, (seq, mtype, text, tool_calls, _opens) in enumerate(collected):
        if mtype == "human":
            out.append(VisibleTurn(seq=seq, role="user", text=text, channel=None))
            continue
        nxt = collected[i + 1] if i + 1 < len(collected) else None
        last_in_segment = nxt is None or nxt[4]
        channel = CHANNEL_FINAL if last_in_segment and not tool_calls else CHANNEL_COMMENTARY
        out.append(VisibleTurn(seq=seq, role="assistant", text=text, channel=channel))
    return out


__all__ = [
    "CHANNEL_COMMENTARY",
    "CHANNEL_FINAL",
    "HIDE_FROM_UI",
    "SCHEDULED_DELIVERY",
    "VisibleTurn",
    "has_tool_calls",
    "is_hidden",
    "message_field",
    "message_text",
    "opens_segment",
    "visible_turns",
]
