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
重跑。多出来的一条:只要有一步失败(或对话数到顶),``agent_spec`` 行就**不删** —— 名字还
在「已删除」列表里,管理员可以再点一次把剩下的清完;全部成功才删版本行,名字从此可以重建。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID

from control_plane.purge.agent_purge_nas import archived_users, scan_agent_dirs
from control_plane.runtime import AgentRuntime
from expert_work.persistence import (
    WORKSPACE_AGENTS_DIR,
    ApprovalStore,
    ArtifactStore,
    TriggerRunStore,
    TriggerStore,
    WebhookDeliveryStore,
    WebhookEndpointStore,
)
from expert_work.persistence.agent_disable import AgentDisableStore
from expert_work.persistence.agent_instance import AgentInstanceStore
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.persistence.curation import CurationCandidateStore, EvalDatasetStore
from expert_work.persistence.feedback_store import FeedbackStore
from expert_work.persistence.image_upload import ImageUploadStore
from expert_work.persistence.knowledge import KnowledgeRetrievalLogStore
from expert_work.persistence.memory import MemoryStore, MemoryWritebackDLQ, MemoryWritebackJobStore
from expert_work.persistence.quality_drift_alert import QualityDriftAlertStore
from expert_work.persistence.quality_score import QualityScoreStore
from expert_work.persistence.secret_allowlist import AgentSecretAllowlistStore
from expert_work.persistence.tenant_config import TenantConfigStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.persistence.user_upload import UserUploadStore
from expert_work.protocol import AgentSpecStatus, parse_agent_ref
from expert_work.protocol.agent_key import require_safe_key, sanitize_agent_key
from expert_work.runtime.runs import RunStore
from expert_work.runtime.runs.schemas import ThreadRunAggregate
from expert_work.runtime.storage import ObjectStore
from orchestrator.context.workspace_projection import thread_projection_prefix
from orchestrator.tools import WorkspaceStore
from orchestrator.tools.agent_file_versions import AgentFileVersionStore
from orchestrator.tools.artifact_snapshots import ArtifactSnapshotStore
from orchestrator.tools.overflow import OVERFLOW_DIR

logger = logging.getLogger("expert_work.control_plane.purge.agent")

#: 与 ``purge_user`` 同一个上限:超过的部分这次不删,``agent_spec`` 行留着,重跑接着删。
_THREAD_PAGE = 500
_MAX_THREADS = 20_000
_SPEC_PAGE = 500
_INSTANCE_PAGE = 500
_LOG_STEP = "knowledge_retrieval_log"

#: 保留的表(对账 / 审计 / 租户资产),预览里原样列给管理员看。
KEPT_TABLES: tuple[str, ...] = (
    "token_usage",
    "tenant_billing_ledger",
    "token_reservation",
    "audit_log",
    "credential_proxy_audit",
    "sandbox_egress_audit",
    "skill",
    "skill_run_usage",
    "skill_promote_request",
)


@dataclass(frozen=True)
class AgentPurgeDeps:
    """``purge_agent`` 要用的 store(从 ``app.state`` 装配,见 ``api/agent_lifecycle.py``)。"""

    agent_specs: AgentSpecStore
    agent_disable: AgentDisableStore
    threads: ThreadMetaStore
    #: ``durable_checkpointer``(可选)+ ``run_manager.delete_by_thread``。
    runtime: AgentRuntime
    runs: RunStore
    approvals: ApprovalStore
    feedback: FeedbackStore
    retrieval_logs: KnowledgeRetrievalLogStore
    memory: MemoryStore
    memory_dlq: MemoryWritebackDLQ
    memory_writeback_jobs: MemoryWritebackJobStore
    artifacts: ArtifactStore
    agent_instances: AgentInstanceStore
    triggers: TriggerStore
    trigger_runs: TriggerRunStore
    webhook_endpoints: WebhookEndpointStore
    webhook_deliveries: WebhookDeliveryStore
    image_uploads: ImageUploadStore
    user_uploads: UserUploadStore
    quality_scores: QualityScoreStore
    drift_alerts: QualityDriftAlertStore
    eval_datasets: EvalDatasetStore
    curation_candidates: CurationCandidateStore
    secret_allowlist: AgentSecretAllowlistStore
    tenant_config: TenantConfigStore
    #: ``None`` = 没接对象存储;图片行照删,对象键留给留存作业。
    object_store: ObjectStore | None = None
    #: ``None`` = 这个部署没有工作区。
    workspace_store: WorkspaceStore | None = None
    #: NAS 根(只有 NAS 后端有);有它才扫盘找「只剩文件、没有数据库行」的用户。
    workspace_root: str | None = None
    artifact_snapshots: ArtifactSnapshotStore | None = None
    agent_file_versions: AgentFileVersionStore | None = None


@dataclass(frozen=True)
class AgentPurgeBlockers:
    versions: list[str]
    active_versions: list[str]
    #: ``"name@version"`` of every live agent that lists this one as a subagent.
    referenced_by: list[str]

    @property
    def blocked(self) -> bool:
        return bool(self.active_versions or self.referenced_by)


@dataclass
class _Footprint:
    """删之前先把要删的东西点一遍(删的过程中会改变列表,不能边删边翻页)。"""

    thread_users: dict[UUID, UUID | None] = field(default_factory=dict)
    threads_capped: bool = False
    trigger_ids: list[UUID] = field(default_factory=list)
    endpoint_ids: list[UUID] = field(default_factory=list)
    eval_dataset_ids: list[UUID] = field(default_factory=list)
    #: ``(user_id, snapshot_key)`` of every artifact version that has a byte copy.
    snapshots: list[tuple[UUID, str]] = field(default_factory=list)
    artifact_versions: int = 0
    nas_users: set[UUID] = field(default_factory=set)
    archived_users: set[UUID] = field(default_factory=set)

    @property
    def thread_ids(self) -> list[UUID]:
        return list(self.thread_users)


@dataclass
class AgentPurgeSummary:
    """``purge_agent`` 做了什么 —— 每张表删了几行、哪步失败(只记异常类型)。"""

    tenant_id: UUID
    name: str
    agent_key: str
    versions: list[str] = field(default_factory=list)
    deleted: dict[str, int] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    threads_capped: bool = False
    default_agent_cleared: bool = False
    nas_users: int = 0
    image_blobs_removed: int = 0
    image_blobs_failed: int = 0
    image_blobs_skipped: int = 0
    #: 版本行真的删掉了 = 名字可以重建。
    agent_spec_deleted: bool = False

    @property
    def ok(self) -> bool:
        return not self.failures and self.agent_spec_deleted

    def as_dict(self) -> dict[str, object]:
        return {
            "tenant_id": str(self.tenant_id),
            "name": self.name,
            "agent_key": self.agent_key,
            "versions": list(self.versions),
            "deleted": dict(self.deleted),
            "failures": dict(self.failures),
            "skipped": list(self.skipped),
            "threads_capped": self.threads_capped,
            "default_agent_cleared": self.default_agent_cleared,
            "nas_users": self.nas_users,
            "image_blobs_removed": self.image_blobs_removed,
            "image_blobs_failed": self.image_blobs_failed,
            "image_blobs_skipped": self.image_blobs_skipped,
            "agent_spec_deleted": self.agent_spec_deleted,
            "kept": list(KEPT_TABLES),
            "ok": self.ok,
        }


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
                if rec.status is not AgentSpecStatus.DELETED:
                    active.append(rec.version)
                continue
            if rec.status is AgentSpecStatus.DELETED:
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


async def _gather(deps: AgentPurgeDeps, *, tenant_id: UUID, name: str, key: str) -> _Footprint:
    fp = _Footprint()
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


async def _step[T](summary: AgentPurgeSummary, name: str, coro: Awaitable[T], *, default: T) -> T:
    """一步失败只记异常**类型**(理由见 ``user_purge._step``:消息里可能带 DSN / 路径)。"""
    try:
        return await coro
    except Exception as exc:  # best-effort: never abort the purge
        logger.warning("purge_agent.step_failed step=%s", name, exc_info=True)
        summary.failures.setdefault(name, type(exc).__name__)
        return default


def _fail(summary: AgentPurgeSummary, name: str, exc: Exception) -> None:
    logger.warning("purge_agent.step_failed step=%s error=%s", name, type(exc).__name__)
    summary.failures.setdefault(name, type(exc).__name__)


def _add(summary: AgentPurgeSummary, table: str, n: int) -> None:
    summary.deleted[table] = summary.deleted.get(table, 0) + n


async def _purge_one_thread(
    deps: AgentPurgeDeps,
    summary: AgentPurgeSummary,
    *,
    tenant_id: UUID,
    thread_id: UUID,
    user_id: UUID | None,
) -> None:
    """``purge_session`` 的同款步骤,外加老布局的工作区目录(新布局在 ``agents/<key>/`` 下,
    随整棵子树删)。检索记录删不掉,这条对话就整条不动,等重跑。"""
    try:
        _add(
            summary,
            _LOG_STEP,
            await deps.retrieval_logs.delete_for_threads(
                tenant_id=tenant_id, thread_ids=[thread_id]
            ),
        )
    except Exception as exc:
        _fail(summary, _LOG_STEP, exc)
        return
    adelete = getattr(deps.runtime.durable_checkpointer, "adelete_thread", None)
    if adelete is not None:
        try:
            await adelete(str(thread_id))
        except Exception as exc:
            _fail(summary, "checkpoint", exc)
    run_ids: list[UUID] = []
    try:
        runs = await deps.runs.list_by_thread(thread_id=thread_id, tenant_id=tenant_id)
        run_ids = [r.run_id for r in runs]
        _add(
            summary,
            "agent_run",
            await deps.runtime.run_manager.delete_by_thread(thread_id, tenant_id=tenant_id),
        )
    except Exception as exc:
        _fail(summary, "agent_run", exc)
    try:
        if await deps.threads.delete(thread_id, tenant_id=tenant_id):
            _add(summary, "thread_meta", 1)
    except Exception as exc:
        _fail(summary, "thread_meta", exc)
    if user_id is not None and deps.workspace_store is not None:
        legacy = [thread_projection_prefix(thread_id).rstrip("/")]
        legacy += [f"{OVERFLOW_DIR}/{run_id}" for run_id in run_ids]
        for path in legacy:
            try:
                await deps.workspace_store.delete_tree(
                    tenant_id=tenant_id, user_id=user_id, path=path
                )
            except Exception as exc:
                _fail(summary, "threads_dir", exc)
    # 同 ``purge_session``:还在跑的 run 可能在两遍之间又记了一条检索。
    try:
        _add(
            summary,
            _LOG_STEP,
            await deps.retrieval_logs.delete_for_threads(
                tenant_id=tenant_id, thread_ids=[thread_id]
            ),
        )
    except Exception as exc:
        logger.warning("purge_agent.retrieval_logs_late_failed error=%s", type(exc).__name__)


async def _purge_conversations(
    deps: AgentPurgeDeps, summary: AgentPurgeSummary, fp: _Footprint, *, tenant_id: UUID
) -> None:
    threads = fp.thread_ids
    # 挂在对话上的子表先删(对话删到一半失败时,不留「对话没了、子表还在」的半截)。
    _add(
        summary,
        "feedback",
        await _step(
            summary,
            "feedback",
            deps.feedback.delete_for_threads(tenant_id=tenant_id, thread_ids=threads),
            default=0,
        ),
    )
    _add(
        summary,
        "agent_approval",
        await _step(
            summary,
            "agent_approval",
            deps.approvals.delete_for_threads(thread_ids=threads, tenant_id=tenant_id),
            default=0,
        ),
    )
    _add(
        summary,
        "memory_writeback_dlq",
        await _step(
            summary,
            "memory_writeback_dlq",
            deps.memory_dlq.delete_for_threads(tenant_id=tenant_id, thread_ids=threads),
            default=0,
        ),
    )
    _add(
        summary,
        "user_upload",
        await _step(
            summary,
            "user_upload",
            deps.user_uploads.delete_for_threads(tenant_id=tenant_id, thread_ids=threads),
            default=0,
        ),
    )
    await _step(
        summary, "image_upload", _purge_images(deps, summary, tenant_id, threads), default=None
    )
    for thread_id, user_id in fp.thread_users.items():
        await _purge_one_thread(
            deps, summary, tenant_id=tenant_id, thread_id=thread_id, user_id=user_id
        )


async def _purge_images(
    deps: AgentPurgeDeps, summary: AgentPurgeSummary, tenant_id: UUID, threads: list[UUID]
) -> None:
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
    _add(
        summary,
        "image_upload",
        await deps.image_uploads.delete_for_threads(tenant_id=tenant_id, thread_ids=threads),
    )


async def _purge_artifacts(
    deps: AgentPurgeDeps, summary: AgentPurgeSummary, fp: _Footprint, *, tenant_id: UUID, key: str
) -> None:
    # B-235 —— 先删副本再删登记行(同 ``purge_user``)。
    if deps.artifact_snapshots is not None:
        removed = 0
        for user_id, snapshot_key in fp.snapshots:
            try:
                if await deps.artifact_snapshots.delete(
                    tenant_id=tenant_id, user_id=user_id, snapshot_key=snapshot_key
                ):
                    removed += 1
            except Exception as exc:
                _fail(summary, "artifact_snapshot", exc)
        _add(summary, "artifact_snapshot", removed)
    _add(
        summary,
        "artifact",
        await _step(
            summary,
            "artifact",
            deps.artifacts.delete_all_for_agent(tenant_id=tenant_id, agent_key=key),
            default=0,
        ),
    )


async def _purge_by_ids(
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
            _fail(summary, table, exc)
    _add(summary, table, removed)


async def _purge_nas(
    deps: AgentPurgeDeps, summary: AgentPurgeSummary, fp: _Footprint, *, tenant_id: UUID, key: str
) -> None:
    summary.nas_users = len(fp.nas_users)
    if fp.archived_users:
        summary.skipped.append("nas_archived_users")
    for user_id in sorted(fp.nas_users):
        if deps.workspace_store is not None:
            try:
                await deps.workspace_store.delete_tree(
                    tenant_id=tenant_id, user_id=user_id, path=f"{WORKSPACE_AGENTS_DIR}/{key}"
                )
            except Exception as exc:
                _fail(summary, "agent_workspace", exc)
        if deps.agent_file_versions is not None:
            try:
                _add(
                    summary,
                    "agent_file_version",
                    await deps.agent_file_versions.delete_for_agent(
                        tenant_id=tenant_id, user_id=user_id, agent_key=key
                    ),
                )
            except Exception as exc:
                _fail(summary, "agent_file_version", exc)


async def purge_agent(deps: AgentPurgeDeps, *, tenant_id: UUID, name: str) -> AgentPurgeSummary:
    """彻底删除 ``name`` 的全部版本与数据。调用方先用 :func:`find_blockers` 拦住不能删的
    (这里在删版本行的同一个事务里还会再判一次「是不是都已删除」)。"""
    key = agent_key_for(name)
    summary = AgentPurgeSummary(tenant_id=tenant_id, name=name, agent_key=key)
    fp = await _gather(deps, tenant_id=tenant_id, name=name, key=key)
    summary.threads_capped = fp.threads_capped
    summary.versions = (
        await find_blockers(deps.agent_specs, tenant_id=tenant_id, name=name)
    ).versions

    # B-168 —— 后台写回任务先删(worker 写之前查任务行,行没了它就放弃),再删对话与记忆。
    _add(
        summary,
        "memory_writeback_job",
        await _step(
            summary,
            "memory_writeback_job",
            deps.memory_writeback_jobs.delete_for_agent(tenant_id=tenant_id, agent_name=name),
            default=0,
        ),
    )
    await _purge_conversations(deps, summary, fp, tenant_id=tenant_id)
    _add(
        summary,
        "memory_item",
        await _step(
            summary,
            "memory_item",
            deps.memory.delete_for_agent(tenant_id=tenant_id, agent_name=name),
            default=0,
        ),
    )
    await _purge_artifacts(deps, summary, fp, tenant_id=tenant_id, key=key)

    async def _delete_trigger(trigger_id: UUID) -> bool:
        return await deps.triggers.delete(trigger_id=trigger_id, tenant_id=tenant_id)

    async def _delete_endpoint(endpoint_id: UUID) -> bool:
        return await deps.webhook_endpoints.delete(endpoint_id=endpoint_id, tenant_id=tenant_id)

    async def _delete_dataset(dataset_id: UUID) -> bool:
        return await deps.eval_datasets.delete(dataset_id=dataset_id, tenant_id=tenant_id)

    await _purge_by_ids(summary, "agent_trigger", fp.trigger_ids, _delete_trigger)
    _add(
        summary,
        "trigger_run",
        await _step(
            summary,
            "trigger_run",
            deps.trigger_runs.delete_for_triggers(trigger_ids=fp.trigger_ids, tenant_id=tenant_id),
            default=0,
        ),
    )
    await _purge_by_ids(summary, "webhook_endpoint", fp.endpoint_ids, _delete_endpoint)
    _add(
        summary,
        "webhook_delivery",
        await _step(
            summary,
            "webhook_delivery",
            deps.webhook_deliveries.delete_for_endpoints(
                endpoint_ids=fp.endpoint_ids, tenant_id=tenant_id
            ),
            default=0,
        ),
    )
    for table, coro in (
        (
            "agent_instance",
            deps.agent_instances.delete_for_agent(tenant_id=tenant_id, agent_code=name),
        ),
        (
            "quality_score",
            deps.quality_scores.delete_for_agent(tenant_id=tenant_id, agent_name=name),
        ),
        (
            "quality_drift_alert",
            deps.drift_alerts.delete_for_agent(tenant_id=tenant_id, agent_name=name),
        ),
        # 候选先于评测集:promoted 的候选经 RESTRICT 外键指着评测集。
        (
            "curation_candidate",
            deps.curation_candidates.delete_for_agent(tenant_id=tenant_id, agent_name=name),
        ),
    ):
        _add(summary, table, await _step(summary, table, coro, default=0))
    await _purge_by_ids(summary, "eval_dataset", fp.eval_dataset_ids, _delete_dataset)
    _add(
        summary,
        "secret_allowlist",
        await _step(
            summary,
            "secret_allowlist",
            deps.secret_allowlist.delete_for_agent(tenant_id=tenant_id, agent_name=name),
            default=0,
        ),
    )
    summary.default_agent_cleared = await _step(
        summary,
        "tenant_config",
        deps.tenant_config.clear_default_agent_name(tenant_id=tenant_id, agent_name=name),
        default=False,
    )
    await _purge_nas(deps, summary, fp, tenant_id=tenant_id, key=key)
    if await _step(
        summary,
        "agent_disable",
        deps.agent_disable.delete(tenant_id=tenant_id, agent_name=name),
        default=False,
    ):
        _add(summary, "agent_disable", 1)

    if summary.failures or fp.threads_capped:
        # 有没删干净的,版本行留着:名字还在「已删除」里,重跑一次接着删。
        summary.skipped.append("agent_spec")
        return summary
    try:
        # 同一个事务里锁住版本行再判一次:预检之后有人恢复了某一版 → AgentSpecPurgeBlockedError。
        specs, revisions = await deps.agent_specs.purge_name(tenant_id=tenant_id, name=name)
    except Exception as exc:
        _fail(summary, "agent_spec", exc)
        return summary
    _add(summary, "agent_spec", specs)
    _add(summary, "agent_spec_revision", revisions)
    summary.agent_spec_deleted = True
    return summary
