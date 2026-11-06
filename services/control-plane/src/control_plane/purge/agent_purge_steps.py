"""B-278 —— ``purge_agent`` 的各个删除步骤。

一条规矩贯穿全部:**先删子、子删成功才删父**。父行(对话、定时任务、webhook、产物登记、
用户锚点)是重跑时找到子数据的唯一线索——子删失败还先删了父,重跑就找不到剩下的子数据,
却会报「全部完成」。所以:

* 对话:挂在对话上的批量子表(反馈 / 审批 / 写回失败队列 / 上传 / 图片 —— 图片有字节没删掉
  也算失败,那几行留着)任一失败 → 这一批对话一条都不删;单条对话的检索记录 / checkpoint /
  run / 老布局目录任一失败 → 这条对话不删。
* 定时任务先删运行记录、webhook 先删投递记录、产物先删字节副本、评测集先删候选样本,子失败
  就不删父。
* NAS 上某个用户的 ``agents/<key>`` 删不掉(先补回属主权限再删,还不行就改名挪开)→ 只留
  **这个用户**能找回他的数据库行(对话、产物、agent_instance),别的用户照删。

失败只记异常**类型**(消息里可能带 DSN / 路径,理由见 ``user_purge._step``)。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID

from control_plane.purge.agent_purge_nas import (
    QUARANTINED,
    SKIPPED_SYMLINK,
    UNLINKED,
    remove_nas_entry,
)
from control_plane.purge.agent_purge_types import AgentPurgeDeps, AgentPurgeSummary, Footprint
from control_plane.purge.image_blobs import ImageBytesNotDeletedError, ImageTally, delete_images
from control_plane.purge.trajectories import (
    TrajectoriesNotDeletedError,
    delete_conversation_trajectories,
    run_days,
)
from expert_work.persistence import AGENT_FILE_VERSIONS_DIR, WORKSPACE_AGENTS_DIR
from expert_work.runtime.runs import RunInfo
from orchestrator.context.workspace_projection import THREADS_DIR
from orchestrator.tools.overflow import OVERFLOW_DIR

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
    parent: str,
    name: str,
    step: str,
) -> bool:
    """``rm -rf {user root}/{parent}/{name}``. ``True`` = gone (removed, unlinked,
    skipped as a symlink, or parked in quarantine), ``False`` = failed (recorded).

    On NAS the purge does it itself (:func:`remove_nas_entry`: owner bits back on
    ``chmod 000`` directories, a symlink unlinked / skipped and never followed, an
    entry that still will not go renamed out of the way) — a folder the model
    broke must not keep the name in ``purging`` forever. Without a NAS root the
    workspace store does it."""
    root = deps.workspace_root
    try:
        if root is None:
            if deps.workspace_store is not None:
                await deps.workspace_store.delete_tree(
                    tenant_id=tenant_id, user_id=user_id, path=f"{parent}/{name}"
                )
            return True
        outcome = await remove_nas_entry(
            root,
            [str(tenant_id), str(user_id), *parent.split("/")],
            name,
            tenant_id=tenant_id,
            owner_id=user_id,
        )
    except Exception as exc:
        fail(summary, step, exc)
        return False
    _count(summary, outcome)
    return True


def _count(summary: AgentPurgeSummary, outcome: str) -> None:
    if outcome == SKIPPED_SYMLINK:
        summary.nas_symlinks_skipped += 1
        skip(summary, "nas_symlink")
    elif outcome == UNLINKED:
        summary.nas_symlinks_unlinked += 1
    elif outcome == QUARANTINED:
        summary.nas_quarantined += 1


async def _delete_file_versions(
    deps: AgentPurgeDeps, summary: AgentPurgeSummary, *, tenant_id: UUID, user_id: UUID, key: str
) -> bool:
    """``.agent-file-versions/{user}/{key}``: the store first (it counts the versions
    and holds the per-agent lock); if it refuses, the same NAS fallback as above."""
    store = deps.agent_file_versions
    if store is None:
        return True
    try:
        add(
            summary,
            "agent_file_version",
            await store.delete_for_agent(tenant_id=tenant_id, user_id=user_id, agent_key=key),
        )
        return True
    except Exception as exc:
        root = deps.workspace_root
        if root is None:
            fail(summary, "agent_file_version", exc)
            return False
    try:
        outcome = await remove_nas_entry(
            root,
            [str(tenant_id), AGENT_FILE_VERSIONS_DIR, str(user_id)],
            key,
            tenant_id=tenant_id,
            owner_id=user_id,
        )
    except Exception as exc:
        fail(summary, "agent_file_version", exc)
        return False
    _count(summary, outcome)
    return True


async def purge_nas(
    deps: AgentPurgeDeps, summary: AgentPurgeSummary, fp: Footprint, *, tenant_id: UUID, key: str
) -> set[UUID]:
    """每个用户的 ``agents/<key>/`` 与 ``/agent`` 文件历史。返回**没删掉**的用户:只有这些用户
    的数据库行(对话、产物、实例绑定)这次留着,别的用户照删。"""
    summary.nas_users = len(fp.nas_users)
    if fp.archived_users:
        skip(summary, "nas_archived_users")
    failed: set[UUID] = set()
    for user_id in sorted(fp.nas_users):
        tree_ok = True
        if deps.workspace_store is not None or deps.workspace_root is not None:
            tree_ok = await _delete_tree(
                deps,
                summary,
                tenant_id=tenant_id,
                user_id=user_id,
                parent=WORKSPACE_AGENTS_DIR,
                name=key,
                step="agent_workspace",
            )
        versions_ok = await _delete_file_versions(
            deps, summary, tenant_id=tenant_id, user_id=user_id, key=key
        )
        if not (tree_ok and versions_ok):
            failed.add(user_id)
    summary.nas_failed_users = len(failed)
    return failed


async def _purge_images(
    deps: AgentPurgeDeps, summary: AgentPurgeSummary, tenant_id: UUID, threads: list[UUID]
) -> int:
    """先删对象存储里的图、再删字节已经没了的行(:mod:`control_plane.purge.image_blobs`)。
    有图没删掉:那几行留着、这一步算失败(这一批对话不删,名字停在 ``purging``,重跑接着删)
    —— 没有作业按键扫孤儿图片,行先删了字节就永远留在桶里。"""
    rows = await deps.image_uploads.list_for_threads(tenant_id=tenant_id, thread_ids=threads)
    tally = ImageTally()
    try:
        await delete_images(deps.image_uploads, deps.object_store, rows, tally)
    except ImageBytesNotDeletedError:
        # ``run`` 只在成功时记条数;部分失败时已删的行在这里记。
        add(summary, "image_upload", tally.rows_deleted)
        raise
    finally:
        summary.image_blobs_removed += tally.blobs_removed
        summary.image_blobs_failed += tally.blobs_failed
        summary.image_blobs_skipped += tally.blobs_skipped
    return tally.rows_deleted


async def _purge_trajectories(
    deps: AgentPurgeDeps,
    summary: AgentPurgeSummary,
    *,
    tenant_id: UUID,
    thread_id: UUID,
    runs: list[RunInfo],
) -> int:
    try:
        return await delete_conversation_trajectories(
            deps.trajectories, tenant_id=tenant_id, thread_id=thread_id, days=run_days(runs)
        )
    except TrajectoriesNotDeletedError as exc:
        # ``run`` 只在成功时记条数;部分失败时已删的在这里记。
        add(summary, "trajectory", exc.removed)
        raise


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
    # B-285 —— 轨迹对象的日期只能从 run 推,所以在删 run 之前;删不掉 → 这条对话留着。
    try:
        runs = await deps.runs.list_by_thread(thread_id=thread_id, tenant_id=tenant_id)
    except Exception as exc:
        fail(summary, "agent_run", exc)
        return
    if not await run(
        summary,
        "trajectory",
        _purge_trajectories(deps, summary, tenant_id=tenant_id, thread_id=thread_id, runs=runs),
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
        add(
            summary,
            "agent_run",
            await deps.runtime.run_manager.delete_by_thread(thread_id, tenant_id=tenant_id),
        )
    except Exception as exc:
        fail(summary, "agent_run", exc)
        return
    if user_id is not None and (
        deps.workspace_store is not None or deps.workspace_root is not None
    ):
        legacy = [(THREADS_DIR, str(thread_id))]
        legacy += [(OVERFLOW_DIR, str(r.run_id)) for r in runs]
        dirs_ok = True
        for parent, name in legacy:
            dirs_ok &= await _delete_tree(
                deps,
                summary,
                tenant_id=tenant_id,
                user_id=user_id,
                parent=parent,
                name=name,
                step="threads_dir",
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
    deps: AgentPurgeDeps,
    summary: AgentPurgeSummary,
    fp: Footprint,
    *,
    tenant_id: UUID,
    keep_users: set[UUID],
) -> None:
    """``keep_users``: users whose NAS folder could not be removed — their
    conversations stay (with everything hanging off them) so a re-run finds them."""
    kept = {t: u for t, u in fp.thread_users.items() if u is not None and u in keep_users}
    if kept:
        skip(summary, "thread_meta")
    mine = {t: u for t, u in fp.thread_users.items() if t not in kept}
    threads = list(mine)
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
            # B-280 —— 文档行留成墓碑:别的智能体的对话再引用这个 upload_id 仍回
            # UPLOAD_SESSION_DELETED(对外已公布);图片行与到期行删掉。
            deps.user_uploads.tombstone_for_threads(
                tenant_id=tenant_id, thread_ids=threads, now=datetime.now(UTC)
            ),
        ),
        ("image_upload", _purge_images(deps, summary, tenant_id, threads)),
    ):
        batch_ok &= await run(summary, table, coro)
    if not batch_ok:
        # 子表没删干净:这一批对话一条都不删,重跑时按对话还能找到它们。
        skip(summary, "thread_meta")
        return
    for thread_id, user_id in mine.items():
        await _purge_one_thread(
            deps, summary, tenant_id=tenant_id, thread_id=thread_id, user_id=user_id
        )


async def purge_artifacts(
    deps: AgentPurgeDeps,
    summary: AgentPurgeSummary,
    fp: Footprint,
    *,
    tenant_id: UUID,
    key: str,
    keep_users: set[UUID],
) -> None:
    """B-235 —— 先删各版本的字节副本;有一份没删掉,登记行就留着(重跑时靠它找到副本)。
    ``keep_users`` 的产物这次不动(见 :func:`purge_conversations`)。"""
    if keep_users:
        skip(summary, "artifact")
    if deps.artifact_snapshots is not None:
        removed, copies_ok = 0, True
        for user_id, snapshot_key in fp.snapshots:
            if user_id in keep_users:
                continue
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
        summary,
        "artifact",
        deps.artifacts.delete_all_for_agent(
            tenant_id=tenant_id, agent_key=key, keep_users=keep_users
        ),
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
