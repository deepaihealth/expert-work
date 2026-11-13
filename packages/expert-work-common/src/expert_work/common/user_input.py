"""B-300 ① —— 白名单:哪一段是「用户在对话里输入的文字」。

用户在对话里的输入只有一个构造点(``control_plane.api.runs._build_human_message``,控制台与
排队输入两个入口都经它)。它在消息的 ``additional_kwargs`` 里记下 :data:`USER_INPUT_KEY`:

* ``text_chars`` —— 用户输入的文字在正文开头占几个字符(正文 = 字符串,或各文字块按顺序拼起来;
  输入文字永远排在附件行、外部内容之前);
* ``images`` / ``files`` / ``external_blocks`` —— 这条消息还带了几张图、几个文件、几段对接方的外部
  内容(只有计数,长期记忆的第二次提炼只给计数,不给内容)。

**只有带这个标记的消息,其输入文字那一段,才进长期记忆的第一次提炼**(只看用户自己说的那一次)。
没有标记的用户角色消息 —— 定时任务的种子、子智能体的任务说明、发版前的旧消息 —— 一律不算。
判不出的从不默认成用户。仓库里只许 ``_build_human_message`` 写它(control-plane 有测试遍历全部源码)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage

from expert_work.common.conversation_channel import message_field, message_text
from expert_work.common.spotlight import untrusted_fence_start

#: 关掉 spotlight 时外部内容段的开头(``control_plane.api.runs._fence_untrusted``)。
UNTRUSTED_PLAIN_MARKER = "[untrusted content]\n"

#: ``additional_kwargs`` 里的键。
USER_INPUT_KEY = "expert_work_user_input"


@dataclass(frozen=True)
class UserInput:
    """一条用户消息里平台确知是用户输入的部分。"""

    text: str
    images: int = 0
    files: int = 0
    external_blocks: int = 0


def user_input_kwargs(
    *, text: str, images: int, files: int, external_blocks: int
) -> dict[str, Any]:
    """``_build_human_message`` 给消息附加的字段。只有那一个构造点调用它。"""
    return {
        USER_INPUT_KEY: {
            "text_chars": len(text),
            "images": images,
            "files": files,
            "external_blocks": external_blocks,
        }
    }


def _count(value: object) -> int | None:
    # bool 是 int 的子类,不算。
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def read_user_input(message: Any) -> UserInput | None:
    """带白名单标记的用户消息 → 它的输入文字与附件计数;没有标记、或标记与正文对不上 → ``None``。

    对象形态与 dict 形态都吃(同 :func:`~expert_work.common.conversation_channel.message_field`)。
    """
    if message_field(message, "type") != "human":
        return None
    kwargs = message_field(message, "additional_kwargs") or {}
    mark = kwargs.get(USER_INPUT_KEY) if isinstance(kwargs, dict) else None
    if not isinstance(mark, dict):
        return None
    chars = _count(mark.get("text_chars"))
    body = message_text(message_field(message, "content", ""))
    if chars is None or chars > len(body):
        return None
    return UserInput(
        text=body[:chars],
        images=_count(mark.get("images")) or 0,
        files=_count(mark.get("files")) or 0,
        external_blocks=_count(mark.get("external_blocks")) or 0,
    )


#: B-300 review M1 —— 草稿试跑(``use_draft``)那一轮的用户消息上的服务端标记。长期记忆每次写回
#: 都重抽整段对话,所以「这一轮不写」光靠这次 run 的配置不够:审批续跑(按已发布版本重建)、孤儿接管、
#: 同一对话里后面的线上轮都会把它再抽一遍。带这个标记的那一整轮,两次提炼都跳过。只由
#: ``control_plane.api.runs._start_run`` 盖;请求体没有任何字段能写到消息的附加字段上,伪造它也只会
#: 让那一轮被跳过。
DRAFT_TURN_KEY = "expert_work_draft_turn"


def text_before_external_content(text: str) -> str:
    """B-300 review L1 —— 用户消息里对接方外部内容之前的那段文字。

    没有白名单标记的用户角色消息(发版前的旧消息等)也可能带着 ``_build_human_message`` 拼进去的
    外部内容:spotlight 围栏,或关掉 spotlight 时的 ``[untrusted content]`` 段。它们一律排在用户文字
    之后,所以从第一个围栏处切掉。"""
    plain = text.find(UNTRUSTED_PLAIN_MARKER)
    cuts = [
        i for i in (untrusted_fence_start(text), plain if plain >= 0 else None) if i is not None
    ]
    return text[: min(cuts)] if cuts else text


def mark_draft_turn(message: BaseMessage) -> BaseMessage:
    """返回盖了草稿标记的**新**消息(已有的附加字段原样保留)。"""
    return message.model_copy(
        update={"additional_kwargs": {**message.additional_kwargs, DRAFT_TURN_KEY: True}}
    )


def is_draft_turn(message: Any) -> bool:
    """这条消息是否是草稿试跑那一轮的用户消息(对象形态与 dict 形态都吃)。"""
    kwargs = message_field(message, "additional_kwargs") or {}
    return isinstance(kwargs, dict) and kwargs.get(DRAFT_TURN_KEY) is True


__all__ = [
    "DRAFT_TURN_KEY",
    "UNTRUSTED_PLAIN_MARKER",
    "USER_INPUT_KEY",
    "UserInput",
    "is_draft_turn",
    "mark_draft_turn",
    "read_user_input",
    "text_before_external_content",
    "user_input_kwargs",
]
