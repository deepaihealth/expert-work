"""B-278 —— ``purge_agent`` 的各个删除步骤。

一条规矩贯穿全部:**先删子、子删成功才删父**。父行(对话、定时任务、webhook、产物登记、
用户锚点)是重跑时找到子数据的唯一线索——子删失败还先删了父,重跑就找不到剩下的子数据,
却会报「全部完成」。所以:

* 对话:挂在对话上的批量子表(反馈 / 审批 / 写回失败队列 / 上传 / 图片)任一失败 → 这一批
  对话一条都不删;单条对话的检索记录 / checkpoint / run / 老布局目录任一失败 → 这条对话不删。
* 定时任务先删运行记录、webhook 先删投递记录、产物先删字节副本、评测集先删候选样本,子失败
  就不删父。
* NAS 上某个用户的 ``agents/<key>`` 删失败 → 这次不删能找回这个用户的数据库行(对话、产物、
  agent_instance),重跑时还找得到他。

失败只记异常**类型**(消息里可能带 DSN / 路径,理由见 ``user_purge._step``)。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from uuid import UUID

from control_plane.purge.agent_purge_nas import has_symlink
from expert_work.persistence import WORKSPACE_AGENTS_DIR
from orchestrator.context.workspace_projection import thread_projection_prefix
from orchestrator.tools.overflow import OVERFLOW_DIR

if TYPE_CHECKING:
    from control_plane.purge.agent_purge import AgentPurgeDeps, AgentPurgeSummary, Footprint

logger = logging.getLogger("expert_work.control_plane.purge.agent")

LOG_STEP = "knowledge_retrieval_log"


def fail(summary: AgentPurgeSummary, name: str, exc: Exception) -> None:
    logger.warning("purge_agent.step_failed step=%s error=%s", name, type(exc).__name__)
    summary.failures.setdefault(name, type(exc).__name__)


def add(summary: AgentPurgeSummary, table: str, n: int) -> None:
    summary.deleted[table] = summary.deleted.get(table, 0) + n


async def run(summary: AgentPurgeSummary, table: str, coro: Awaitable[int]) -> bool:
    """Run one counted delete; record the count or the failure type. ``True`` = it worked."""
    try:
        add(summary, table, await coro)
    except Exception as exc:  # best-effort: never abort the purge
        fail(summary, table, exc)
        return False
    return True


def skip(summary: AgentPurgeSummary, what: str) -> None:
    """``what`` was not attempted because something it depends on failed."""
    if what not in summary.skipped:
        summary.skipped.append(what)


async def _delete_tree(
    deps: AgentPurgeDeps,
    summary: AgentPurgeSummary,
    *,
    tenant_id: UUID,
    user_id: UUID,
    path: str,
    step: str,
) -> bool:
    """``rm -rf`` one path under a user root. A symlink anywhere on the path (the
    model can make one in its own workspace) is skipped and counted, not a
    failure — the store would refuse it forever (``O_NOFOLLOW``) and the name would
    stay stuck in ``purging``. ``True`` = gone or skipped, ``False`` = failed."""
    store = deps.workspace_store
    if store is None:
        return True
    root = deps.workspace_root
    if root is not None and await has_symlink(root, tenant_id, user_id, path):
        summary.nas_symlinks_skipped += 1
        skip(summary, "nas_symlink")
        return True
    try:
        await store.delete_tree(tenant_id=tenant_id, user_id=user_id, path=path)
    except Exception as exc:
        fail(summary, step, exc)
        return False
    return True


async def purge_nas(
    deps: AgentPurgeDeps, summary: AgentPurgeSummary, fp: Footprint, *, tenant_id: UUID, key: str
) -> bool:
    """每个用户的 ``agents/<key>/`` 与 ``/agent`` 文件历史。``True`` = 全部删掉了。"""
    summary.nas_users = len(fp.nas_users)
    if fp.archived_users:
        skip(summary, "nas_archived_users")
    ok = True
    for user_id in sorted(fp.nas_users):
        if deps.workspace_store is not None:
            ok &= await _delete_tree(
                deps,
                summary,
                tenant_id=tenant_id,
                user_id=user_id,
                path=f"{WORKSPACE_AGENTS_DIR}/{key}",
                step="agent_workspace",
            )
        if deps.agent_file_versions is not None:
            ok &= await run(
                summary,
                "agent_file_version",
                deps.agent_file_versions.delete_for_agent(
                    tenant_id=tenant_id, user_id=user_id, agent_key=key
                ),
            )
    return ok


async def _purge_images(
    deps: AgentPurgeDeps, summary: AgentPurgeSummary, tenant_id: UUID, threads: list[UUID]
) -> int:
    """先删对象存储里的图、再删行(同 ``purge_user._purge_images``:
    孤儿键有留存作业兜,卡住的行没人兜)。"""
    rows = await deps.image_uploads.list_for_threads(tenant_id=tenant_id, thread_ids=threads)
    if deps.object_store is None:
        summary.image_blobs_skipped += len(rows)
    else:
        for row in rows:
            try:
                await deps.object_store.delete(row.object_key)
                summary.image_blobs_removed += 1
            except Exception:
                summary.image_blobs_failed += 1
                logger.warning("purge_agent.image_blob_failed", exc_info=True)
    return await deps.image_uploads.delete_for_threads(tenant_id=tenant_id, thread_ids=threads)


async def _purge_one_thread(
    deps: AgentPurgeDeps,
    summary: AgentPurgeSummary,
    *,
    tenant_id: UUID,
    thread_id: UUID,
    user_id: UUID | None,
) -> None:
    """``purge_session`` 的同款步骤 + 老布局的工作区目录(新布局在 ``agents/<key>/`` 下,
    随整棵子树删)。任一步失败,这条对话的 ``thread_meta`` 就留着,等重跑。"""
    if not await run(
        summary,
        LOG_STEP,
        deps.retrieval_logs.delete_for_threads(tenant_id=tenant_id, thread_ids=[thread_id]),
    ):
        return
    adelete = getattr(deps.runtime.durable_checkpointer, "adelete_thread", None)
    if adelete is not None:
        try:
            await adelete(str(thread_id))
        except Exception as exc:
            fail(summary, "checkpoint", exc)
            return
    try:
        runs = await deps.runs.list_by_thread(thread_id=thread_id, tenant_id=tenant_id)
        add(
            summary,
            "agent_run",
            await deps.runtime.run_manager.delete_by_thread(thread_id, tenant_id=tenant_id),
        )
    except Exception as exc:
        fail(summary, "agent_run", exc)
        return
    if user_id is not None and deps.workspace_store is not None:
        legacy = [thread_projection_prefix(thread_id).rstrip("/")]
        legacy += [f"{OVERFLOW_DIR}/{r.run_id}" for r in runs]
        dirs_ok = True
        for path in legacy:
            dirs_ok &= await _delete_tree(
                deps, summary, tenant_id=tenant_id, user_id=user_id, path=path, step="threads_dir"
            )
        if not dirs_ok:
            return
    try:
        if await deps.threads.delete(thread_id, tenant_id=tenant_id):
            add(summary, "thread_meta", 1)
    except Exception as exc:
        fail(summary, "thread_meta", exc)
        return
    # 同 ``purge_session``:还在跑的 run 可能在两遍之间又记了一条检索;再删一遍,失败只记日志
    # (这条之后由留存作业兜)。
    try:
        add(
            summary,
            LOG_STEP,
            await deps.retrieval_logs.delete_for_threads(
                tenant_id=tenant_id, thread_ids=[thread_id]
            ),
        )
    except Exception as exc:
        logger.warning("purge_agent.retrieval_logs_late_failed error=%s", type(exc).__name__)


async def purge_conversations(
    deps: AgentPurgeDeps, summary: AgentPurgeSummary, fp: Footprint, *, tenant_id: UUID
) -> None:
    threads = fp.thread_ids
    batch_ok = True
    for table, coro in (
        ("feedback", deps.feedback.delete_for_threads(tenant_id=tenant_id, thread_ids=threads)),
        (
            "agent_approval",
            deps.approvals.delete_for_threads(thread_ids=threads, tenant_id=tenant_id),
        ),
        (
            "memory_writeback_dlq",
            deps.memory_dlq.delete_for_threads(tenant_id=tenant_id, thread_ids=threads),
        ),
        (
            "user_upload",
            deps.user_uploads.delete_for_threads(tenant_id=tenant_id, thread_ids=threads),
        ),
        ("image_upload", _purge_images(deps, summary, tenant_id, threads)),
    ):
        batch_ok &= await run(summary, table, coro)
    if not batch_ok:
        # 子表没删干净:这一批对话一条都不删,重跑时按对话还能找到它们。
        skip(summary, "thread_meta")
        return
    for thread_id, user_id in fp.thread_users.items():
        await _purge_one_thread(
            deps, summary, tenant_id=tenant_id, thread_id=thread_id, user_id=user_id
        )


async def purge_artifacts(
    deps: AgentPurgeDeps, summary: AgentPurgeSummary, fp: Footprint, *, tenant_id: UUID, key: str
) -> None:
    """B-235 —— 先删各版本的字节副本;有一份没删掉,登记行就留着(重跑时靠它找到副本)。"""
    if deps.artifact_snapshots is not None:
        removed, copies_ok = 0, True
        for user_id, snapshot_key in fp.snapshots:
            try:
                if await deps.artifact_snapshots.delete(
                    tenant_id=tenant_id, user_id=user_id, snapshot_key=snapshot_key
                ):
                    removed += 1
            except Exception as exc:
                fail(summary, "artifact_snapshot", exc)
                copies_ok = False
        add(summary, "artifact_snapshot", removed)
        if not copies_ok:
            skip(summary, "artifact")
            return
    await run(
        summary, "artifact", deps.artifacts.delete_all_for_agent(tenant_id=tenant_id, agent_key=key)
    )


async def _delete_each(
    summary: AgentPurgeSummary,
    table: str,
    ids: list[UUID],
    delete_one: Callable[[UUID], Awaitable[bool]],
) -> None:
    """逐行删(这几类每个智能体只有几条到几十条,复用 store 现成的按 id 删)。"""
    removed = 0
    for row_id in ids:
        try:
            if await delete_one(row_id):
                removed += 1
        except Exception as exc:
            fail(summary, table, exc)
    add(summary, table, removed)


async def purge_triggers_and_webhooks(
    deps: AgentPurgeDeps, summary: AgentPurgeSummary, fp: Footprint, *, tenant_id: UUID
) -> None:
    async def _trigger(trigger_id: UUID) -> bool:
        return await deps.triggers.delete(trigger_id=trigger_id, tenant_id=tenant_id)

    async def _endpoint(endpoint_id: UUID) -> bool:
        return await deps.webhook_endpoints.delete(endpoint_id=endpoint_id, tenant_id=tenant_id)

    if await run(
        summary,
        "trigger_run",
        deps.trigger_runs.delete_for_triggers(trigger_ids=fp.trigger_ids, tenant_id=tenant_id),
    ):
        await _delete_each(summary, "agent_trigger", fp.trigger_ids, _trigger)
    else:
        skip(summary, "agent_trigger")
    if await run(
        summary,
        "webhook_delivery",
        deps.webhook_deliveries.delete_for_endpoints(
            endpoint_ids=fp.endpoint_ids, tenant_id=tenant_id
        ),
    ):
        await _delete_each(summary, "webhook_endpoint", fp.endpoint_ids, _endpoint)
    else:
        skip(summary, "webhook_endpoint")


async def purge_eval_data(
    deps: AgentPurgeDeps, summary: AgentPurgeSummary, fp: Footprint, *, tenant_id: UUID, name: str
) -> None:
    """候选样本先于评测集:promoted 的候选经 ``RESTRICT`` 外键指着评测集。"""

    async def _dataset(dataset_id: UUID) -> bool:
        return await deps.eval_datasets.delete(dataset_id=dataset_id, tenant_id=tenant_id)

    if await run(
        summary,
        "curation_candidate",
        deps.curation_candidates.delete_for_agent(tenant_id=tenant_id, agent_name=name),
    ):
        await _delete_each(summary, "eval_dataset", fp.eval_dataset_ids, _dataset)
    else:
        skip(summary, "eval_dataset")
