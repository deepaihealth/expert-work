"""B-278 —— 彻底删除智能体时隔离起来的目录:满 7 天删掉。

control-plane 彻底删除一个智能体时,删不掉的目录(别的 uid 的文件、补了权限也删不动)被挪到
用户根旁边的隔离区 ``{root}/{tenant}/.purged-agents/{user}/{原名}-{unix 秒}-{随机}``。这里按名字
里的时间满 7 天删掉,用的是同一个安全删除
(:func:`expert_work.persistence.workspace.safe_remove.sweep_quarantine`:补属主位不经链接、
不递归、删完 ``lstat`` 核实)。删不掉的计数进报告,不中断其它 pass。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from expert_work.persistence.workspace.safe_remove import sweep_quarantine

#: 隔离区条目保留多久。给运维留出看一眼的时间,又不至于无限占 NAS。
PURGED_AGENT_RETENTION_S = 7 * 24 * 3600


@dataclass(frozen=True)
class PurgedAgentsSweep:
    removed: int = 0
    failed: int = 0


async def sweep_purged_agents(
    root: str, *, min_age_s: float = PURGED_AGENT_RETENTION_S
) -> PurgedAgentsSweep:
    """删掉挪进隔离区满 ``min_age_s`` 的条目;返回删掉 / 删不掉的个数。"""
    removed, failed = await asyncio.to_thread(
        sweep_quarantine, root, older_than=time.time() - min_age_s
    )
    return PurgedAgentsSweep(removed=removed, failed=failed)


__all__ = ["PURGED_AGENT_RETENTION_S", "PurgedAgentsSweep", "sweep_purged_agents"]
