"""Agent wall-clock timezone —— 一处定,两处用。

系统提示词里的「当前日期」行(``agent_factory._current_date_block``)告诉模型今天是
哪天、在哪个时区,并让它「精确时间调 exec_python 去算」;沙箱 exec 的 ``TZ``
(``tools.sandbox.platform_exec_envs``)决定 exec_python 里 ``datetime.now()`` /
``date`` 给出的钟点。两边若各读各的,提示词说 Asia/Shanghai、沙箱却是 UTC —— 模型照做
反而拿到慢 8 小时的时间(文件名时间戳、「今天」的计算,0~8 点之间连日期都差一天)。
所以两处都只调这里。
"""

from __future__ import annotations

import logging
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

#: zh-CN deployment → Asia/Shanghai,so "今天几号" answers in the user's local day,
#: not the server's UTC day. ``EXPERT_WORK_TIMEZONE`` overrides.
DEFAULT_AGENT_TIMEZONE = "Asia/Shanghai"


def resolve_agent_timezone() -> ZoneInfo:
    """Resolve the agent wall-clock timezone, falling back to UTC.

    Reads ``EXPERT_WORK_TIMEZONE`` (default ``Asia/Shanghai``). An invalid zone
    name degrades to UTC rather than failing the build — a stale tz label is
    recoverable, a crashed build is not.
    """
    name = os.environ.get("EXPERT_WORK_TIMEZONE", DEFAULT_AGENT_TIMEZONE)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("invalid EXPERT_WORK_TIMEZONE %r; falling back to UTC", name)
        return ZoneInfo("UTC")
