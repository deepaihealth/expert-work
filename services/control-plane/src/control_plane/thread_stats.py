"""run 终局重算会话的对外可见消息条数(第三方对接 API v1 P2 块 2)。

**重算而非累加**:run 会重试、会被打断、会在审批处暂停后续跑,累加型计数器
在这些路径上必然漂。重算天然自愈 —— 任何一次成功的 run 终局都会把计数纠回来。

口径与对外消息端点严格一致(``include_hidden=False`` + 同一个
:func:`~control_plane.transcript.extract_turns`)。两处若各写各的就会漂 ——
那正是 ``thread_message`` 镜像表已经犯过的错。

住在 control-plane 而不是 orchestrator,是因为只有这边能 import
``transcript.extract_turns``;orchestrator 那侧只有
:class:`~orchestrator.sse.ThreadStatsRecorder` 这个 Protocol,由 ``app.py``
组装时把本实现注入进去,依赖方向不反。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from langchain_core.messages import BaseMessage

from control_plane.transcript import extract_turns

logger = logging.getLogger(__name__)

__all__ = ["ThreadStatsRecorderImpl"]


class ThreadStatsRecorderImpl:
    """把重算出的条数写回 ``thread_meta.message_count``。Best-effort。"""

    def __init__(self, *, threads: Any) -> None:
        """``threads`` 是一个 ``ThreadMetaStore``。类型放宽成 ``Any`` 只为让
        测试能塞进最小替身;真实注入点(``app.py``)传的是真 store。"""
        self._threads = threads

    async def record(
        self, *, thread_id: UUID, tenant_id: UUID, messages: Sequence[BaseMessage]
    ) -> None:
        try:
            count = len(extract_turns(list(messages), include_hidden=False))
            updated = await self._threads.update_message_count(
                thread_id, count, tenant_id=tenant_id
            )
            if not updated:
                # 会话没有 thread_meta 行(或租户不匹配)。不是错误:并非每条
                # 会话都有元数据行,漏掉的下次 run 会再算一次。
                logger.debug(
                    "thread_stats.row_missing thread_id=%s tenant_id=%s", thread_id, tenant_id
                )
        except Exception:
            # 计数是展示增强,绝不能影响 run 的终局。漏了的会话在下次 run 自愈。
            logger.warning("thread_stats.record_failed thread_id=%s", thread_id, exc_info=True)
