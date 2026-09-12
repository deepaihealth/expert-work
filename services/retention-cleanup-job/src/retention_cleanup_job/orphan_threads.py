"""孤儿目录的每日补扫 —— ``threads/<thread_id>/`` 与 ``.tool_results/<run_id>/``。

文件系统为发现源、库里的行为判据、24h 宽限为兜底。碰文件的入口仍然只有
:mod:`retention_cleanup_job.workspace_files`。

两条扫描是同一个形状(B-50 spec §4.2 把 ``.tool_results/`` 纳入与 ``threads/``
同一套生命周期),只是判据表不同:一个查 ``thread_meta``,一个查 ``agent_run``。

> **``.tool_results/`` 此前从没被清过。** ``overflow.py`` 的注释声称它的生命周期
> 由既有留存机制负责,而留存 job 的不变式恰恰是「根目录其它文件永不触碰」,
> 且它没有任何登记行 —— 那条注释是假的(实测单用户堆了 40 个 run 目录,
> 一次没被清过)。注释已在同一个 PR 里改掉。
"""

from __future__ import annotations

import asyncio
import logging
import time
from uuid import UUID

from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.runtime.runs.store import RunStore
from retention_cleanup_job.workspace_files import (
    ThreadDir,
    ToolResultDir,
    UnsafeWorkspacePathError,
    iter_thread_dirs,
    iter_tool_result_dirs,
    remove_thread_dir,
    remove_tool_result_dir,
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
                # ``agent_key`` 必须原样带回去:搬迁期同一个 thread_id 的目录
                # 可能在用户根(空串)也可能在 ``agents/<key>/`` 下,拼错一边就是
                # 「删不掉」——孤儿永远留着,而且什么都不会报错。
                if await asyncio.to_thread(
                    remove_thread_dir,
                    root,
                    tenant_id,
                    user_id,
                    entry.thread_id,
                    agent_key=entry.agent_key,
                ):
                    removed += 1
            except (OSError, UnsafeWorkspacePathError):
                logger.exception("retention.thread_dir_remove_failed thread_id=%s", entry.thread_id)
    return removed


async def sweep_orphan_tool_results(root: str, run_store: RunStore) -> int:
    """孤儿 ``.tool_results/<run_id>/`` 目录(B-50 spec §4.2)。

    与 :func:`sweep_orphan_thread_dirs` 同一套规矩:以文件系统为发现源,
    ``agent_run`` 行不存在**且**目录最新 mtime 超过宽限期的删掉;**行还在的
    一律不动,不管多老**。

    宽限期在这里比在 threads 那边更要紧:``.tool_results/<run_id>/`` 是 run
    **执行中**写的(工具结果超长时外置),而 ``agent_run`` 行在排队阶段就已存在
    —— 正常情况下行永远早于目录。真正会撞上的是反过来的窗口:留存把老 run 行
    清掉的同一时刻,一个引用它的 run 还在收尾。宽限兜的是那个。

    Returns the number of directories removed.
    """
    entries = await asyncio.to_thread(lambda: list(iter_tool_result_dirs(root)))
    grouped: dict[tuple[UUID, UUID], list[ToolResultDir]] = {}
    for entry in entries:
        grouped.setdefault((entry.tenant_id, entry.user_id), []).append(entry)
    removed = 0
    grace_cutoff = time.time() - _ORPHAN_THREAD_DIR_GRACE_S
    for (tenant_id, user_id), dirs in grouped.items():
        existing = await run_store.existing_ids([d.run_id for d in dirs], tenant_id=tenant_id)
        for entry in dirs:
            if entry.run_id in existing:
                continue
            if entry.newest_mtime > grace_cutoff:
                continue  # 宽限期内的孤儿:明天再看
            try:
                if await asyncio.to_thread(
                    remove_tool_result_dir,
                    root,
                    tenant_id,
                    user_id,
                    entry.run_id,
                    agent_key=entry.agent_key,
                ):
                    removed += 1
            except (OSError, UnsafeWorkspacePathError):
                logger.exception("retention.tool_results_remove_failed run_id=%s", entry.run_id)
    return removed


__all__ = ["sweep_orphan_thread_dirs", "sweep_orphan_tool_results"]
