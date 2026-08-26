"""Shared helpers: derive a human session title from message text.

Used by the run trigger (auto-title the thread from its first user message)
and the session-history list (lazy backfill of pre-existing threads that were
created before auto-titling existed — read their durable checkpoint's first
user message and persist it).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

# 拍平消息正文的定义住在 common —— 会话标题、对外消息列表、对话条目三处读的
# 必须是同一段文本,否则「标题里有的字」和「气泡里有的字」会对不上。这里保留
# 转出名,老调用点不受影响。
from expert_work.common.conversation_channel import message_text as message_text

logger = logging.getLogger("expert_work.control_plane.session_title")


def title_from_text(text: str, *, limit: int = 80) -> str:
    """A compact single-line title from message text.

    Collapses all runs of whitespace (incl. newlines) to single spaces, strips,
    truncates to ``limit`` chars.
    """
    single = " ".join(text.split())
    return single[:limit]


async def first_message_title(
    checkpointer: BaseCheckpointSaver[Any],
    thread_id: UUID,
    *,
    limit: int = 80,
) -> str | None:
    """The thread's first user message as a title, or ``None``.

    Reads the durable checkpoint directly (keyed by ``thread_id``) and returns
    the first ``human`` message's text, truncated. Best-effort: any failure
    (no checkpoint, read error, no human turn) degrades to ``None``.
    """
    config: RunnableConfig = {"configurable": {"thread_id": str(thread_id), "checkpoint_ns": ""}}
    try:
        tup = await checkpointer.aget_tuple(config)
    except Exception:
        logger.warning("session_title.read_failed", exc_info=True)
        return None
    if tup is None:
        return None
    raw = (tup.checkpoint.get("channel_values") or {}).get("messages", [])
    for m in raw:
        if getattr(m, "type", None) != "human":
            continue
        title = title_from_text(message_text(getattr(m, "content", "")), limit=limit)
        if title:
            return title
    return None


async def backfill_titles(
    items: list[Any],
    *,
    threads: Any,
    checkpointer: BaseCheckpointSaver[Any] | None,
) -> list[Any]:
    """Fill in a title for any listed thread that has none.

    NULL-title threads(auto-title 之前建的、以及对外平面早期建的)在列表里
    显示成 thread_id 哈希 / 「未命名对话」。从 checkpoint 首条用户消息推导并
    落库,一线程一次;只处理列出的这一页,有界。best-effort:缺 checkpoint /
    读失败就保留原样。调用方须在租户 scope 内跑,落库才走 RLS。原为
    ``sessions.py`` 私有,对话页(``/v1/conversations``)接同一兜底后搬到
    这里共享(2026-08-26「全是未命名对话」反馈)。

    ``items`` 是 ``ThreadMeta`` 列表 —— 参数typed ``Any`` 以免本模块反向依赖
    persistence 的模型(与本文件其余部分同一纪律)。
    """
    if checkpointer is None:
        return items
    out: list[Any] = []
    for m in items:
        if m.title is None:
            title = await first_message_title(checkpointer, m.thread_id)
            if title:
                await threads.update_title(m.thread_id, title, tenant_id=m.tenant_id)
                m = m.model_copy(update={"title": title})
        out.append(m)
    return out
