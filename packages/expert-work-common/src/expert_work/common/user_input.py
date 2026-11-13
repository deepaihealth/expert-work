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

from expert_work.common.conversation_channel import message_field, message_text

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


__all__ = ["USER_INPUT_KEY", "UserInput", "read_user_input", "user_input_kwargs"]
