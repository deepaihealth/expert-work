"""B-128 —— prompt 视图只保留最新一份系统提示词。

入口(``control_plane.api.runs`` / 触发器 / 委派子 run)每一轮都往检查点追加一条
``SystemMessage``,内容是当轮渲染的系统提示词。检查点里因此有 N 份,而
:func:`~orchestrator.llm.coalesce.coalesce_system_messages` 把所有 SystemMessage
按原顺序拼成一条 —— 第 N 轮的请求带 N 份几乎相同的提示词,每轮新追加的那份又
让前缀缓存在上一份末尾断开;提示词中途改过时模型还会同时看到新旧两版。

本模块只改 prompt 视图(CM-C4 同一契约),检查点历史不动:

* 留**最后一条**非摘要 SystemMessage(当轮配置渲染出来的那份),放到下标 0;
* 压缩器写的 ``<context-summary>`` 不算提示词,原位保留;
* 其余消息保持原相对顺序;输入列表与消息对象都不被修改。
"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import BaseMessage, SystemMessage

from orchestrator.context.compressor import is_summary_message

__all__ = ["keep_latest_system_prompt"]


def _is_prompt(message: BaseMessage) -> bool:
    return isinstance(message, SystemMessage) and not is_summary_message(message)


def keep_latest_system_prompt(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """返回只含最新一份系统提示词(位于下标 0)的新列表。"""
    prompt_indices = [i for i, m in enumerate(messages) if _is_prompt(m)]
    if not prompt_indices or prompt_indices == [0]:
        return list(messages)
    latest = messages[prompt_indices[-1]]
    return [latest, *(m for m in messages if not _is_prompt(m))]
