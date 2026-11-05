"""B-278 —— 彻底删除智能体用到的数据结构:依赖、足迹、汇总、保留表清单。

单独成模块,``agent_purge``(编排)和 ``agent_purge_steps``(各个删除步骤)都从这里取,
两者互不导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from control_plane.runtime import AgentRuntime
from expert_work.persistence import (
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
from expert_work.runtime.runs import RunStore
from expert_work.runtime.storage import ObjectStore
from orchestrator.tools import WorkspaceStore
from orchestrator.tools.agent_file_versions import AgentFileVersionStore
from orchestrator.tools.artifact_snapshots import ArtifactSnapshotStore

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
    # 事件流表:控制面没有接它的 store(``EventLogStore.delete_by_thread`` 注明只给测试 /
    # 开发用,生产账号没有 DELETE 权限),``purge_session`` / ``purge_user`` 也不删;到期由
    # 事件归档 + 留存作业按时间清。
    "event_log",
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
class Footprint:
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
    #: NAS 路径上有符号链接(模型在自己工作区里建的)而跳过的次数 —— 不算失败。
    nas_symlinks_skipped: int = 0
    #: 最后删版本行时又被拦下(有版本在用 / 有在用的智能体引用它):``{active_versions,
    #: referenced_by}``;API 据此回 409。
    blocked: dict[str, list[str]] | None = None
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
            "nas_symlinks_skipped": self.nas_symlinks_skipped,
            "blocked": dict(self.blocked) if self.blocked is not None else None,
            "agent_spec_deleted": self.agent_spec_deleted,
            "kept": list(KEPT_TABLES),
            "ok": self.ok,
        }
