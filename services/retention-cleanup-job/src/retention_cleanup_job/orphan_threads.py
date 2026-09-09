"""留存链 B-27 —— 孤儿 ``threads/<thread_id>/`` 目录的每日补扫(拆自 job.py)。

文件系统为发现源、``thread_meta`` 行为判据、24h 宽限为兜底。碰文件的入口仍然只有
:mod:`retention_cleanup_job.workspace_files`(这里只调用 ``remove_thread_dir``)。
"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import UUID

from expert_work.persistence.thread_meta import ThreadMetaStore
from retention_cleanup_job.workspace_files import (
    ThreadDir,
    UnsafeWorkspacePathError,
    iter_thread_dirs,
    remove_thread_dir,
)

logger = logging.getLogger(__name__)

#: B-27 孤儿 ``threads/<id>/`` 的宽限:目录(含直接子项)最新 mtime 距今不足
#: 这个数就不删,哪怕 thread 行已经不在。
#:
#: 顺序核实(审查项 2):``thread_meta`` 行在 ``POST /v1/sessions``
#: (api/sessions.py ``threads.create`` → SQL store 自己 ``commit``)/ 触发器
#: (trigger_firing.py)里先提交;``threads/<id>/`` 只在 run 的 turn 末投影时
#: 才写(graph_builder/builder.py ``_project_workspace_state`` → 沙箱 write_file),
#: 而起 run 的端点先 ``threads.get`` 404 —— 所以「行提交」严格早于「首次写目录」,
#: 但**不是同一事务**(两个 session、两个 HTTP 请求)。宽限兜的是另一头:purge
#: 钩子删了行,一个正在收尾的 run 紧接着又把投影写进去(窗口毫秒级),或者
#: NFS 属性缓存让扫描一侧看到的目录比真实状态旧。24h 远大于任何 run 的墙钟上限
#: (deadline 分钟级)与 NFS 缓存,又远小于 90 天的留存,取这个数没有更细的理由。
_ORPHAN_THREAD_DIR_GRACE_S = 24 * 3600.0


async def sweep_orphan_thread_dirs(root: str, thread_store: ThreadMetaStore) -> int:
    """留存链 B-27 —— 孤儿 ``threads/<thread_id>/`` 目录。

    以文件系统为发现源(``{root}/<tenant>/<user>/threads/<uuid>``,三层都要
    是 UUID 名),按 ``(tenant, user)`` 一批查 ``thread_meta``:行不存在**且**
    目录(含直接子项)最新 mtime 超过 :data:`_ORPHAN_THREAD_DIR_GRACE_S` 的
    删掉。**行还在的目录一律不动,不管多老** —— 会话被删除/purge 时的同步
    清理是 control-plane 的 purge 钩子,这里只兜那次钩子失败或早于钩子存在的
    存量。已软删的用户整棵树跳过(归 janitor 归档)。

    Returns the number of directories removed.
    """
    entries = await asyncio.to_thread(lambda: list(iter_thread_dirs(root)))
    grouped: dict[tuple[UUID, UUID], list[ThreadDir]] = {}
    for entry in entries:
        grouped.setdefault((entry.tenant_id, entry.user_id), []).append(entry)
    removed = 0
    grace_cutoff = time.time() - _ORPHAN_THREAD_DIR_GRACE_S
    for (tenant_id, user_id), dirs in grouped.items():
        existing = await thread_store.get_many([d.thread_id for d in dirs], tenant_id=tenant_id)
        for entry in dirs:
            if entry.thread_id in existing:
                continue
            if entry.newest_mtime > grace_cutoff:
                continue  # 宽限期内的孤儿:明天再看
            try:
                if await asyncio.to_thread(
                    remove_thread_dir, root, tenant_id, user_id, entry.thread_id
                ):
                    removed += 1
            except (OSError, UnsafeWorkspacePathError):
                logger.exception("retention.thread_dir_remove_failed thread_id=%s", entry.thread_id)
    return removed


__all__ = ["sweep_orphan_thread_dirs"]
