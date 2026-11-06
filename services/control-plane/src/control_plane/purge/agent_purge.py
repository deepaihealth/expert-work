"""B-278 —— 彻底删除智能体(连数据一起清),以及删除前的条数预览。

粒度是**整个名字**:一个名字的所有版本都已软删(``status = deleted``)、且没有别的
智能体把它当子智能体引用时才放行(:func:`find_blockers`)。删掉的东西:

* 按名字 / ``agent_key`` 直接关联的:``agent_spec`` + ``agent_spec_revision``(最后、同一个
  事务)、``agent_disable``、``agent_instance``、``agent_trigger`` + ``trigger_run``、
  ``thread_meta``(``thread_message`` 级联)、``artifact`` + ``artifact_version`` + 各版本的
  字节副本、``memory_item``(``agent_name`` 恰好是它的;共享事实记忆 ``agent_name IS NULL``
  保留)、``memory_writeback_job``(先于记忆)、``quality_score``、``quality_drift_alert``、
  ``curation_candidate``(先于评测集)、``eval_dataset``、只订阅它的 ``webhook_endpoint``
  + ``webhook_delivery``、``secret_allowlist``;``tenant_config.default_agent_name`` 置空。
* 按它的对话集合关联的:``knowledge_retrieval_log``(最先,删不掉就整条对话不动)、
  ``agent_run`` + ``run_event``、``agent_approval``、``feedback``、``memory_writeback_dlq``、
  ``image_upload``(先删对象存储里的图)、``user_upload``、LangGraph checkpoint。
* NAS:每个用户的 ``agents/<key>/`` 整棵、老布局的 ``threads/<id>/`` 与
  ``.tool_results/<run>/``、``.agent-file-versions/<user>/<key>/``。

保留(对账):``token_usage``、``tenant_billing_ledger``、``token_reservation``、
``audit_log``、``credential_proxy_audit``、``sandbox_egress_audit``、``skill`` 系列。
OSS 上的轨迹 / 事件归档走既有生命周期,不在这里删。

和 ``purge_user`` 同一套规矩:逐步执行、每步失败只记**异常类型**(不记 ``str(exc)``)、可以
重跑。另外三条:

* 开始时把整个名字的所有版本翻成 ``purging``(:meth:`AgentSpecStore.begin_purge`,持久的
  「正在彻底删除」标记):这期间不能恢复、不能新建同名版本,删到一半失败也停在这个状态,
  不会出现「版本恢复了、数据却没了」。
* 先删子、子删成功才删父(见 ``agent_purge_steps``),重跑永远找得到剩下的东西。
* 只要有一步失败(或对话数到顶),``agent_spec`` 行就**不删**:名字留在「已删除」里(状态
  ``purging``),管理员再点一次把剩下的清完;全部成功才在同一个事务里再查一遍「没有在用的
  版本、没有在用的智能体引用它」,然后删版本行,名字从此可以重建。
"""

from __future__ import annotations

import logging
from uuid import UUID

from control_plane.purge.agent_purge_nas import archived_users, scan_agent_dirs
from control_plane.purge.agent_purge_steps import (
    purge_artifacts,
    purge_conversations,
    purge_eval_data,
    purge_nas,
    purge_triggers_and_webhooks,
    run,
    skip,
)
from control_plane.purge.agent_purge_types import (
    KEPT_TABLES,
    AgentPurgeBlockers,
    AgentPurgeDeps,
    AgentPurgeSummary,
    Footprint,
)
from expert_work.persistence.agent_spec import AgentSpecPurgeBlockedError, AgentSpecStore
from expert_work.protocol import parse_agent_ref
from expert_work.protocol.agent_key import require_safe_key, sanitize_agent_key
from expert_work.runtime.runs.schemas import ThreadRunAggregate

__all__ = [
    "KEPT_TABLES",
    "AgentPurgeBlockers",
    "AgentPurgeDeps",
    "AgentPurgeSummary",
    "Footprint",
    "agent_key_for",
    "find_blockers",
    "preview_agent_purge",
    "purge_agent",
]

logger = logging.getLogger("expert_work.control_plane.purge.agent")

#: 与 ``purge_user`` 同一个上限:超过的部分这次不删,``agent_spec`` 行留着,重跑接着删。
_THREAD_PAGE = 500
_MAX_THREADS = 20_000
_SPEC_PAGE = 500
_INSTANCE_PAGE = 500


def agent_key_for(name: str) -> str:
    key = sanitize_agent_key(name)
    require_safe_key(key)
    return key


async def find_blockers(specs: AgentSpecStore, *, tenant_id: UUID, name: str) -> AgentPurgeBlockers:
    """这个名字的版本、还活着的版本,以及把它当子智能体引用的活着的智能体。

    引用方只看没删除的智能体(线上配置与未发布草稿都看):已删除的引用方恢复之后,
    构建时会明确报「子智能体不存在」,不会悄悄跑偏。"""
    versions: list[str] = []
    active: list[str] = []
    referrers: set[str] = set()
    offset = 0
    while True:
        page = await specs.list_by_tenant(tenant_id=tenant_id, limit=_SPEC_PAGE, offset=offset)
        for rec in page:
            if rec.name == name:
                versions.append(rec.version)
                if not rec.status.gone:
                    active.append(rec.version)
                continue
            if rec.status.gone:
                continue
            specs_to_check = [rec.spec] + ([rec.draft.spec] if rec.draft is not None else [])
            for spec in specs_to_check:
                for sub in spec.spec.subagents:
                    if parse_agent_ref(sub.agent_ref)[0] == name:
                        referrers.add(f"{rec.name}@{rec.version}")
        if len(page) < _SPEC_PAGE:
            break
        offset += _SPEC_PAGE
    return AgentPurgeBlockers(
        versions=sorted(versions), active_versions=sorted(active), referenced_by=sorted(referrers)
    )


async def _gather(deps: AgentPurgeDeps, *, tenant_id: UUID, name: str, key: str) -> Footprint:
    fp = Footprint()
    offset = 0
    while len(fp.thread_users) < _MAX_THREADS:
        page = await deps.threads.list_by_tenant(
            tenant_id, agent_name=name, include_archived=True, limit=_THREAD_PAGE, offset=offset
        )
        for meta in page:
            fp.thread_users[meta.thread_id] = meta.user_id
        if len(page) < _THREAD_PAGE:
            break
        offset += _THREAD_PAGE
    if len(fp.thread_users) >= _MAX_THREADS:
        fp.threads_capped = True
        logger.warning("purge_agent.threads_capped hit the %d-thread cap", _MAX_THREADS)

    fp.trigger_ids = [
        t.id for t in await deps.triggers.list_by_agent(tenant_id=tenant_id, agent_name=name)
    ]
    fp.endpoint_ids = [
        e.id
        for e in await deps.webhook_endpoints.list_by_tenant(tenant_id=tenant_id, agent_name=name)
        if e.agent_name == name
    ]
    fp.eval_dataset_ids = [
        d.id for d in await deps.eval_datasets.list_by_agent(tenant_id=tenant_id, agent_name=name)
    ]
    versions = await deps.artifacts.list_versions_for_agent(tenant_id=tenant_id, agent_key=key)
    fp.artifact_versions = len(versions)
    fp.snapshots = [(v.user_id, v.snapshot_key) for v in versions if v.snapshot_key]

    users: set[UUID] = {u for u in fp.thread_users.values() if u is not None}
    users |= {v.user_id for v in versions}
    offset = 0
    while True:
        bound = await deps.agent_instances.list_by_agent(
            tenant_id=tenant_id, agent_code=name, limit=_INSTANCE_PAGE, offset=offset
        )
        users |= {b.user_id for b in bound}
        if len(bound) < _INSTANCE_PAGE:
            break
        offset += _INSTANCE_PAGE
    if deps.workspace_root is not None:
        users |= await scan_agent_dirs(deps.workspace_root, tenant_id, key)
        fp.archived_users = await archived_users(deps.workspace_root, tenant_id, users)
    fp.nas_users = users - fp.archived_users
    return fp


async def preview_agent_purge(
    deps: AgentPurgeDeps, *, tenant_id: UUID, name: str
) -> dict[str, object]:
    """删之前各类会删几条 —— 与 :func:`purge_agent` 用同一份清单、同一组谓词。"""
    key = agent_key_for(name)
    blockers = await find_blockers(deps.agent_specs, tenant_id=tenant_id, name=name)
    fp = await _gather(deps, tenant_id=tenant_id, name=name, key=key)
    threads = fp.thread_ids
    aggregates: dict[UUID, ThreadRunAggregate] = {}
    for i in range(0, len(threads), _THREAD_PAGE):
        aggregates.update(
            await deps.runs.aggregate_by_threads(
                thread_ids=threads[i : i + _THREAD_PAGE], tenant_id=tenant_id
            )
        )
    trigger_runs = 0
    for trigger_id in fp.trigger_ids:
        trigger_runs += len(
            await deps.trigger_runs.list_by_trigger(trigger_id=trigger_id, tenant_id=tenant_id)
        )
    config = await deps.tenant_config.get(tenant_id=tenant_id)
    counts = {
        "conversations": len(threads),
        "runs": sum(a.run_count for a in aggregates.values()),
        "approvals": await deps.approvals.count_for_threads(
            tenant_id=tenant_id, thread_ids=threads
        ),
        "feedback": await deps.feedback.count_for_threads(tenant_id=tenant_id, thread_ids=threads),
        "retrieval_logs": await deps.retrieval_logs.count_for_threads(
            tenant_id=tenant_id, thread_ids=threads
        ),
        "memories": await deps.memory.count_for_agent(tenant_id=tenant_id, agent_name=name),
        "memory_jobs": await deps.memory_writeback_jobs.count_for_agent(
            tenant_id=tenant_id, agent_name=name
        ),
        "memory_dlq": await deps.memory_dlq.count_for_threads(
            tenant_id=tenant_id, thread_ids=threads
        ),
        "artifacts": await deps.artifacts.count_for_agent(tenant_id=tenant_id, agent_key=key),
        "artifact_versions": fp.artifact_versions,
        "triggers": len(fp.trigger_ids),
        "trigger_runs": trigger_runs,
        "webhooks": len(fp.endpoint_ids),
        "webhook_deliveries": await deps.webhook_deliveries.count_for_endpoints(
            endpoint_ids=fp.endpoint_ids, tenant_id=tenant_id
        ),
        "agent_instances": await deps.agent_instances.count_for_agent(
            tenant_id=tenant_id, agent_code=name
        ),
        "quality_scores": await deps.quality_scores.count_for_agent(
            tenant_id=tenant_id, agent_name=name
        ),
        "drift_alerts": await deps.drift_alerts.count_for_agent(
            tenant_id=tenant_id, agent_name=name
        ),
        "eval_datasets": len(fp.eval_dataset_ids),
        "curation_candidates": await deps.curation_candidates.count_for_agent(
            tenant_id=tenant_id, agent_name=name
        ),
        "uploads": await deps.user_uploads.count_for_threads(
            tenant_id=tenant_id, thread_ids=threads
        ),
        "images": len(
            await deps.image_uploads.list_for_threads(tenant_id=tenant_id, thread_ids=threads)
        ),
        "secret_allowlist": await deps.secret_allowlist.count_for_agent(
            tenant_id=tenant_id, agent_name=name
        ),
        "revisions": await deps.agent_specs.count_revisions_for_name(
            tenant_id=tenant_id, name=name
        ),
        "nas_users": len(fp.nas_users),
    }
    return {
        "name": name,
        "agent_key": key,
        "versions": blockers.versions,
        "blocking": {
            "active_versions": blockers.active_versions,
            "referenced_by": blockers.referenced_by,
        },
        "default_agent_cleared": config is not None and config.default_agent_name == name,
        "threads_capped": fp.threads_capped,
        "counts": counts,
        "kept": list(KEPT_TABLES),
    }


async def purge_agent(deps: AgentPurgeDeps, *, tenant_id: UUID, name: str) -> AgentPurgeSummary:
    """彻底删除 ``name`` 的全部版本与数据。

    先把所有版本标成 ``purging``:有在用的版本就抛 :class:`AgentSpecPurgeBlockedError`,
    此时什么都还没删。调用方(API)事先用 :func:`find_blockers` 给出完整的 409 说明。"""
    key = agent_key_for(name)
    versions = await deps.agent_specs.begin_purge(tenant_id=tenant_id, name=name)
    summary = AgentPurgeSummary(tenant_id=tenant_id, name=name, agent_key=key, versions=versions)
    fp = await _gather(deps, tenant_id=tenant_id, name=name, key=key)
    summary.threads_capped = fp.threads_capped

    # NAS 先删:某个用户的目录实在删不掉(补权限、改名都不行),只留这个用户能找回他的
    # 数据库行(对话、产物、实例绑定),别的用户照删。
    keep_users = await purge_nas(deps, summary, fp, tenant_id=tenant_id, key=key)

    # B-168 —— 后台写回任务先删(worker 写记忆前查任务行,行没了它就放弃),删成功才删记忆。
    if await run(
        summary,
        "memory_writeback_job",
        deps.memory_writeback_jobs.delete_for_agent(tenant_id=tenant_id, agent_name=name),
    ):
        await run(
            summary,
            "memory_item",
            deps.memory.delete_for_agent(tenant_id=tenant_id, agent_name=name),
        )
    else:
        skip(summary, "memory_item")

    await purge_conversations(deps, summary, fp, tenant_id=tenant_id, keep_users=keep_users)
    await purge_artifacts(deps, summary, fp, tenant_id=tenant_id, key=key, keep_users=keep_users)
    if keep_users:
        skip(summary, "agent_instance")
    await run(
        summary,
        "agent_instance",
        deps.agent_instances.delete_for_agent(
            tenant_id=tenant_id, agent_code=name, keep_users=keep_users
        ),
    )
    await purge_triggers_and_webhooks(deps, summary, fp, tenant_id=tenant_id)
    await run(
        summary,
        "quality_score",
        deps.quality_scores.delete_for_agent(tenant_id=tenant_id, agent_name=name),
    )
    await run(
        summary,
        "quality_drift_alert",
        deps.drift_alerts.delete_for_agent(tenant_id=tenant_id, agent_name=name),
    )
    await purge_eval_data(deps, summary, fp, tenant_id=tenant_id, name=name)
    await run(
        summary,
        "secret_allowlist",
        deps.secret_allowlist.delete_for_agent(tenant_id=tenant_id, agent_name=name),
    )
    try:
        summary.default_agent_cleared = await deps.tenant_config.clear_default_agent_name(
            tenant_id=tenant_id, agent_name=name
        )
    except Exception as exc:
        summary.failures.setdefault("tenant_config", type(exc).__name__)
    try:
        if await deps.agent_disable.delete(tenant_id=tenant_id, agent_name=name):
            summary.deleted["agent_disable"] = 1
    except Exception as exc:
        summary.failures.setdefault("agent_disable", type(exc).__name__)

    if summary.failures or fp.threads_capped:
        # 有没删干净的:版本行留着(状态 ``purging``),重跑接着删。
        skip(summary, "agent_spec")
        return summary
    try:
        # 同一个事务里锁住版本行,再查一遍在用的版本与引用方,然后删版本行与修订历史。
        specs, revisions = await deps.agent_specs.purge_name(tenant_id=tenant_id, name=name)
    except AgentSpecPurgeBlockedError as exc:
        summary.blocked = {
            "active_versions": exc.active_versions,
            "referenced_by": exc.referenced_by,
        }
        skip(summary, "agent_spec")
        return summary
    except Exception as exc:
        summary.failures.setdefault("agent_spec", type(exc).__name__)
        return summary
    summary.deleted["agent_spec"] = specs
    summary.deleted["agent_spec_revision"] = revisions
    summary.agent_spec_deleted = True
    return summary
