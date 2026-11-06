"""B-278 —— ``purge_agent`` / ``preview_agent_purge`` orchestration (in-memory stores).

One world holds the doomed agent (two soft-deleted versions, two users, one of
everything), a live sibling agent in the same tenant, a shared fact memory
extracted from the doomed agent's conversation, and the same agent name in a
second tenant. The tests assert what goes, what stays, the order that matters,
that the preview counts are exactly what the purge then removes, and that a
failed step keeps the ``agent_spec`` rows so a re-run can finish.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from control_plane.purge.agent_purge import (
    AgentPurgeDeps,
    agent_key_for,
    find_blockers,
    preview_agent_purge,
    purge_agent,
)
from expert_work.persistence import (
    InMemoryApprovalStore,
    InMemoryArtifactStore,
    InMemoryCurationCandidateStore,
    InMemoryEvalDatasetStore,
    InMemoryMemoryStore,
    InMemoryThreadMetaStore,
    InMemoryTriggerRunStore,
    InMemoryTriggerStore,
    InMemoryWebhookDeliveryStore,
    InMemoryWebhookEndpointStore,
)
from expert_work.persistence.agent_disable import InMemoryAgentDisableStore
from expert_work.persistence.agent_instance.memory import InMemoryAgentInstanceStore
from expert_work.persistence.agent_spec import InMemoryAgentSpecStore
from expert_work.persistence.feedback_store import FeedbackRecord, InMemoryFeedbackStore
from expert_work.persistence.image_upload import InMemoryImageUploadStore
from expert_work.persistence.knowledge import (
    InMemoryKnowledgeRetrievalLogStore,
    RetrievalLogEntry,
)
from expert_work.persistence.memory import InMemoryMemoryWritebackJobStore
from expert_work.persistence.memory.dlq import InMemoryMemoryWritebackDLQ
from expert_work.persistence.quality_drift_alert import InMemoryQualityDriftAlertStore
from expert_work.persistence.quality_score import InMemoryQualityScoreStore
from expert_work.persistence.secret_allowlist import InMemoryAgentSecretAllowlistStore
from expert_work.persistence.tenant_config import InMemoryTenantConfigStore
from expert_work.persistence.user_upload import InMemoryUserUploadStore
from expert_work.protocol import (
    AgentSpec,
    AgentSpecStatus,
    ApprovalRecord,
    CandidateStatus,
    CurationCandidateRecord,
    EvalDatasetRecord,
    MemoryItem,
    QualityDriftAlertRecord,
    QualityScoreRecord,
    TenantConfigPatch,
    TriggerRecord,
    TriggerRunRecord,
    WebhookDeliveryRecord,
    WebhookEndpointRecord,
)
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunStore,
    RunInfo,
    RunManager,
    RunStatus,
)
from expert_work.runtime.storage import InMemoryObjectStore
from orchestrator.tools.artifact_snapshots import InMemoryArtifactSnapshotStore
from orchestrator.tools.workspace_store import RecordingWorkspaceStore

_X = "doomed-agent"
_Y = "live-agent"
_NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)

#: preview ``counts`` key → ``summary.deleted`` key (the same rows, two names).
_PREVIEW_TO_DELETED = {
    "conversations": "thread_meta",
    "runs": "agent_run",
    "approvals": "agent_approval",
    "feedback": "feedback",
    "retrieval_logs": "knowledge_retrieval_log",
    "memories": "memory_item",
    "memory_jobs": "memory_writeback_job",
    "memory_dlq": "memory_writeback_dlq",
    "artifacts": "artifact",
    "artifact_versions": "artifact_snapshot",  # every seeded version has a byte copy
    "triggers": "agent_trigger",
    "trigger_runs": "trigger_run",
    "webhooks": "webhook_endpoint",
    "webhook_deliveries": "webhook_delivery",
    "agent_instances": "agent_instance",
    "quality_scores": "quality_score",
    "drift_alerts": "quality_drift_alert",
    "eval_datasets": "eval_dataset",
    "curation_candidates": "curation_candidate",
    "uploads": "user_upload",
    "images": "image_upload",
    "secret_allowlist": "secret_allowlist",
    "revisions": "agent_spec_revision",
}


def _spec(name: str, version: str = "1.0.0", subagent_of: str | None = None) -> AgentSpec:
    doc: dict[str, object] = {
        "apiVersion": "expert_work.io/v1",
        "kind": "Agent",
        "metadata": {"name": name, "version": version, "tenant": "t"},
        "spec": {
            "tenant_config": {},
            "model": {"provider": "anthropic", "name": "claude-sonnet-4-6"},
            "system_prompt": {"template": "you help"},
            "sandbox": {
                "resources": {"cpu": "1.0", "memory": "1Gi"},
                "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
                "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
            },
        },
    }
    if subagent_of is not None:
        doc["spec"]["subagents"] = [  # type: ignore[index]
            {"name": "helper", "agent_ref": subagent_of, "description": "delegate"}
        ]
    return AgentSpec.model_validate(doc)


def _workspace() -> RecordingWorkspaceStore:
    return RecordingWorkspaceStore(workspace_file=b"bytes")


class _JobsFirstMemory(InMemoryMemoryStore):
    """Records how many writeback jobs of the agent were left when memories went."""

    def __init__(self, jobs: InMemoryMemoryWritebackJobStore) -> None:
        super().__init__()
        self.jobs = jobs
        self.jobs_left: list[int] = []

    async def delete_for_agent(self, *, tenant_id: UUID, agent_name: str) -> int:
        self.jobs_left.append(
            await self.jobs.count_for_agent(tenant_id=tenant_id, agent_name=agent_name)
        )
        return await super().delete_for_agent(tenant_id=tenant_id, agent_name=agent_name)


@dataclass
class _World:
    specs: InMemoryAgentSpecStore = field(default_factory=InMemoryAgentSpecStore)
    disable: InMemoryAgentDisableStore = field(default_factory=InMemoryAgentDisableStore)
    threads: InMemoryThreadMetaStore = field(default_factory=InMemoryThreadMetaStore)
    runs: InMemoryRunStore = field(default_factory=InMemoryRunStore)
    approvals: InMemoryApprovalStore = field(default_factory=InMemoryApprovalStore)
    feedback: InMemoryFeedbackStore = field(default_factory=InMemoryFeedbackStore)
    logs: InMemoryKnowledgeRetrievalLogStore = field(
        default_factory=InMemoryKnowledgeRetrievalLogStore
    )
    jobs: InMemoryMemoryWritebackJobStore = field(default_factory=InMemoryMemoryWritebackJobStore)
    dlq: InMemoryMemoryWritebackDLQ = field(default_factory=InMemoryMemoryWritebackDLQ)
    artifacts: InMemoryArtifactStore = field(default_factory=InMemoryArtifactStore)
    instances: InMemoryAgentInstanceStore = field(default_factory=InMemoryAgentInstanceStore)
    triggers: InMemoryTriggerStore = field(default_factory=InMemoryTriggerStore)
    trigger_runs: InMemoryTriggerRunStore = field(default_factory=InMemoryTriggerRunStore)
    endpoints: InMemoryWebhookEndpointStore = field(default_factory=InMemoryWebhookEndpointStore)
    deliveries: InMemoryWebhookDeliveryStore = field(default_factory=InMemoryWebhookDeliveryStore)
    images: InMemoryImageUploadStore = field(default_factory=InMemoryImageUploadStore)
    uploads: InMemoryUserUploadStore = field(default_factory=InMemoryUserUploadStore)
    scores: InMemoryQualityScoreStore = field(default_factory=InMemoryQualityScoreStore)
    alerts: InMemoryQualityDriftAlertStore = field(default_factory=InMemoryQualityDriftAlertStore)
    datasets: InMemoryEvalDatasetStore = field(default_factory=InMemoryEvalDatasetStore)
    candidates: InMemoryCurationCandidateStore = field(
        default_factory=InMemoryCurationCandidateStore
    )
    allowlist: InMemoryAgentSecretAllowlistStore = field(
        default_factory=InMemoryAgentSecretAllowlistStore
    )
    config: InMemoryTenantConfigStore = field(default_factory=InMemoryTenantConfigStore)
    objects: InMemoryObjectStore = field(default_factory=InMemoryObjectStore)
    workspace: RecordingWorkspaceStore = field(default_factory=_workspace)
    t1: UUID = field(default_factory=uuid4)
    t2: UUID = field(default_factory=uuid4)
    memory: _JobsFirstMemory = field(init=False)
    snapshots: InMemoryArtifactSnapshotStore = field(init=False)

    def __post_init__(self) -> None:
        self.memory = _JobsFirstMemory(self.jobs)
        self.snapshots = InMemoryArtifactSnapshotStore(workspace=self.workspace)

    def deps(self) -> AgentPurgeDeps:
        return AgentPurgeDeps(
            agent_specs=self.specs,
            agent_disable=self.disable,
            threads=self.threads,
            runtime=SimpleNamespace(  # type: ignore[arg-type]
                durable_checkpointer=None, run_manager=RunManager(store=self.runs)
            ),
            runs=self.runs,
            approvals=self.approvals,
            feedback=self.feedback,
            retrieval_logs=self.logs,
            memory=self.memory,
            memory_dlq=self.dlq,
            memory_writeback_jobs=self.jobs,
            artifacts=self.artifacts,
            agent_instances=self.instances,
            triggers=self.triggers,
            trigger_runs=self.trigger_runs,
            webhook_endpoints=self.endpoints,
            webhook_deliveries=self.deliveries,
            image_uploads=self.images,
            user_uploads=self.uploads,
            quality_scores=self.scores,
            drift_alerts=self.alerts,
            eval_datasets=self.datasets,
            curation_candidates=self.candidates,
            secret_allowlist=self.allowlist,
            tenant_config=self.config,
            object_store=self.objects,
            workspace_store=self.workspace,
            artifact_snapshots=self.snapshots,
        )

    async def seed_agent(self, tenant: UUID, name: str, *, deleted: bool) -> list[UUID]:
        """Two versions, two users, one of everything; returns the thread ids."""
        for version in ("1.0.0", "2.0.0"):
            await self.specs.create(
                tenant_id=tenant, spec=_spec(name, version), spec_sha256="a" * 64, created_by="u"
            )
            if deleted:
                await self.specs.update_status(
                    tenant_id=tenant, name=name, version=version, status=AgentSpecStatus.DELETED
                )
        await self.disable.set_disabled(
            tenant_id=tenant, agent_name=name, disabled=True, reason="r", disabled_by="u"
        )
        key = agent_key_for(name)
        thread_ids: list[UUID] = []
        for user in (uuid4(), uuid4()):
            thread = uuid4()
            thread_ids.append(thread)
            await self.threads.create(
                thread_id=thread,
                tenant_id=tenant,
                created_by="seed",
                user_id=user,
                agent_name=name,
                agent_version="1.0.0",
            )
            await self._seed_thread(tenant, thread, user)
            await self.memory.write([_mem(tenant, user, name, f"{name} remembers")])
            await self.jobs.enqueue(
                tenant_id=tenant,
                user_id=user,
                agent_name=name,
                agent_version="1.0.0",
                thread_id=thread,
                run_id=uuid4(),
                trace_id=None,
                checkpoint_id="ckpt",
                now=_NOW,
            )
            await self.artifacts.save_version(
                tenant_id=tenant,
                user_id=user,
                agent_key=key,
                name="report.md",
                kind="document",
                path_in_workspace="report.md",
                created_in_thread=str(thread),
                snapshot_key=await self._copy(tenant, user),
            )
            await self.instances.touch(tenant_id=tenant, agent_code=name, user_id=user)
        trigger = TriggerRecord(
            id=uuid4(),
            tenant_id=tenant,
            agent_name=name,
            agent_version="1.0.0",
            name="nightly",
            kind="cron",
            config={"expr": "0 9 * * *"},
            enabled=False,
            source="api",
            created_at=_NOW,
            updated_at=_NOW,
        )
        await self.triggers.create(trigger)
        await self.trigger_runs.create(
            TriggerRunRecord(id=uuid4(), tenant_id=tenant, trigger_id=trigger.id, triggered_at=_NOW)
        )
        endpoint = WebhookEndpointRecord(
            id=uuid4(),
            tenant_id=tenant,
            name=f"hook-{name}",
            url="https://example.invalid/hook",
            event_types=("run.completed",),
            agent_name=name,
            created_at=_NOW,
            updated_at=_NOW,
        )
        await self.endpoints.create(endpoint)
        await self.deliveries.create(
            WebhookDeliveryRecord(
                id=uuid4(),
                tenant_id=tenant,
                endpoint_id=endpoint.id,
                event_id=uuid4().hex,
                event_type="run.completed",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        await self.scores.insert(
            QualityScoreRecord(
                tenant_id=tenant,
                agent_name=name,
                agent_version="1.0.0",
                run_id=uuid4(),
                thread_id=thread_ids[0],
                overall=4,
                dimensions={},
                rationale="r",
                judge_model="m",
            )
        )
        await self.alerts.insert(
            QualityDriftAlertRecord(
                tenant_id=tenant,
                agent_name=name,
                recent_mean=3.0,
                baseline_mean=4.0,
                drift_pct=0.25,
                recent_count=5,
                baseline_count=5,
            )
        )
        dataset = await self.datasets.create(
            EvalDatasetRecord(
                id=uuid4(),
                tenant_id=tenant,
                agent_name=name,
                name="regressions",
                source="trajectory",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        await self.candidates.upsert(
            CurationCandidateRecord(
                id=uuid4(),
                tenant_id=tenant,
                agent_name=name,
                thread_id=thread_ids[0],
                trajectory_key=f"k-{uuid4().hex}",
                outcome="failed",
                signal="failed_outcome",
                status=CandidateStatus.PROMOTED,
                eval_dataset_id=dataset.id,
                detected_at=_NOW,
                reviewed_at=_NOW,
            )
        )
        self.allowlist.add(
            tenant_id=tenant, agent_name=name, agent_version="1.0.0", secret_ref="secret://k"
        )
        return thread_ids

    async def _copy(self, tenant: UUID, user: UUID) -> str:
        key = str(uuid4())
        await self.snapshots.capture(
            tenant_id=tenant, user_id=user, path="report.md", snapshot_key=key
        )
        return key

    async def _seed_thread(self, tenant: UUID, thread: UUID, user: UUID) -> None:
        run_id = uuid4()
        await self.runs.create(
            RunInfo(
                run_id=run_id,
                tenant_id=tenant,
                thread_id=thread,
                user_id=user,
                status=RunStatus.SUCCESS,
                on_disconnect=DisconnectMode.CANCEL,
                is_resume=False,
                error=None,
                created_at=_NOW,
                updated_at=_NOW,
                finished_at=_NOW,
                trace_id=None,
            )
        )
        await self.approvals.create(
            ApprovalRecord(
                id=uuid4(),
                tenant_id=tenant,
                user_id=None,
                run_id=run_id,
                thread_id=thread,
                request_id="approval:seed",
                node="tools",
                reason_kind="policy_gate",
                action_summary="s",
                requested_at=_NOW,
                timeout_at=_NOW + timedelta(hours=1),
            )
        )
        await self.feedback.insert(
            FeedbackRecord(tenant_id=tenant, thread_id=thread, rating="up", actor_id="a")
        )
        await self.logs.add(
            RetrievalLogEntry(
                tenant_id=tenant,
                kb_ids=(uuid4(),),
                source="agent",
                query="q",
                status="ok",
                latency_ms=1,
                run_id=run_id,
                thread_id=thread,
            )
        )
        await self.dlq.enqueue(
            tenant_id=tenant,
            user_id=user,
            source_thread_id=str(thread),
            extracted=[("fact", "c")],
            error="e",
        )
        await self.uploads.insert(
            upload_id=uuid4(),
            tenant_id=tenant,
            user_id=user,
            thread_id=thread,
            kind="document",
            ref=f"uploads/{uuid4().hex}.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            filename="f.pdf",
        )
        image_id = uuid4()
        object_key = f"tenants/{tenant}/uploads/{image_id}.png"
        await self.images.insert(
            image_id=image_id,
            tenant_id=tenant,
            thread_id=thread,
            user_id=user,
            object_key=object_key,
            size_bytes=1,
            mime_type="image/png",
            sha256="x",
        )
        await self.objects.put(object_key, b"png")


def _mem(tenant: UUID, user: UUID, agent: str | None, content: str) -> MemoryItem:
    return MemoryItem(
        id=uuid4(),
        tenant_id=tenant,
        user_id=user,
        kind="fact",
        content=content,
        embedding=(1.0, 0.0),
        agent_name=agent,
    )


async def _world() -> tuple[_World, list[UUID]]:
    world = _World()
    doomed_threads = await world.seed_agent(world.t1, _X, deleted=True)
    await world.seed_agent(world.t1, _Y, deleted=False)  # a live sibling
    await world.seed_agent(world.t2, _X, deleted=True)  # same name, other tenant
    # A shared fact (no agent_name) extracted from the doomed agent's conversation.
    shared = _mem(world.t1, uuid4(), None, "user likes tea")
    await world.memory.write(
        [shared.model_copy(update={"source_thread_id": str(doomed_threads[0])})]
    )
    await world.config.create(tenant_id=world.t1, display_name="t1", actor_id="u")
    await world.config.upsert(
        tenant_id=world.t1, patch=TenantConfigPatch(default_agent_name=_X), actor_id="u"
    )
    return world, doomed_threads


async def _counts(world: _World, tenant: UUID, name: str) -> dict[str, int]:
    preview = await preview_agent_purge(world.deps(), tenant_id=tenant, name=name)
    counts = preview["counts"]
    assert isinstance(counts, dict)
    return counts


@pytest.mark.asyncio
async def test_preview_counts_are_exactly_what_the_purge_deletes() -> None:
    world, _ = await _world()
    before = await _counts(world, world.t1, _X)
    assert all(before[k] > 0 for k in _PREVIEW_TO_DELETED), before

    summary = await purge_agent(world.deps(), tenant_id=world.t1, name=_X)

    assert summary.failures == {}
    assert summary.ok
    for preview_key, table in _PREVIEW_TO_DELETED.items():
        assert summary.deleted.get(table) == before[preview_key], (preview_key, table)
    assert summary.deleted["agent_spec"] == 2
    assert summary.nas_users == before["nas_users"] == 2
    after = await _counts(world, world.t1, _X)
    assert all(v == 0 for v in after.values()), after


@pytest.mark.asyncio
async def test_purge_leaves_the_sibling_agent_and_the_other_tenant_intact() -> None:
    world, _ = await _world()
    sibling_before = await _counts(world, world.t1, _Y)
    other_tenant_before = await _counts(world, world.t2, _X)

    await purge_agent(world.deps(), tenant_id=world.t1, name=_X)

    got_1 = await _counts(world, world.t1, _Y)
    assert got_1 == sibling_before
    got_2 = await _counts(world, world.t2, _X)
    assert got_2 == other_tenant_before
    got_3 = await world.disable.get(tenant_id=world.t2, agent_name=_X)
    assert got_3 is not None
    got_4 = await world.disable.get(tenant_id=world.t1, agent_name=_Y)
    assert got_4 is not None
    got_5 = await world.specs.list_by_tenant(tenant_id=world.t2, name=_X)
    assert len(got_5) == 2


@pytest.mark.asyncio
async def test_purge_clears_the_kill_switch_default_agent_and_the_versions() -> None:
    world, _ = await _world()

    summary = await purge_agent(world.deps(), tenant_id=world.t1, name=_X)

    assert summary.agent_spec_deleted
    got_6 = await world.specs.list_by_tenant(tenant_id=world.t1, name=_X)
    assert got_6 == []
    got_7 = await world.specs.count_revisions_for_name(tenant_id=world.t1, name=_X)
    assert got_7 == 0
    # A new agent created under the same name must not start out disabled.
    got_8 = await world.disable.get(tenant_id=world.t1, agent_name=_X)
    assert got_8 is None
    assert summary.default_agent_cleared is True
    config = await world.config.get(tenant_id=world.t1)
    assert config is not None and config.default_agent_name is None


@pytest.mark.asyncio
async def test_shared_fact_memories_from_its_conversations_are_kept() -> None:
    world, doomed_threads = await _world()

    await purge_agent(world.deps(), tenant_id=world.t1, name=_X)

    survivors = [r for r in world.memory._rows if r.tenant_id == world.t1]
    assert {r.agent_name for r in survivors} == {None, _Y}
    shared = [r for r in survivors if r.agent_name is None]
    assert [r.source_thread_id for r in shared] == [str(doomed_threads[0])]


@pytest.mark.asyncio
async def test_writeback_jobs_go_before_the_memories() -> None:
    world, _ = await _world()

    await purge_agent(world.deps(), tenant_id=world.t1, name=_X)

    assert world.memory.jobs_left == [0]


@pytest.mark.asyncio
async def test_blobs_and_byte_copies_go_with_the_rows() -> None:
    world, _ = await _world()

    summary = await purge_agent(world.deps(), tenant_id=world.t1, name=_X)

    assert summary.image_blobs_removed == 2
    assert summary.deleted["artifact_snapshot"] == 2
    remaining = {k[0] for k in world.snapshots._blobs}
    assert world.t1 in remaining  # the sibling's copies stay
    assert len([k for k in world.snapshots._blobs if k[0] == world.t1]) == 2
    # Every user's agents/<key> subtree, and the legacy root-level thread dirs.
    key = agent_key_for(_X)
    trees = {(tenant, path) for tenant, _user, path in world.workspace.workspace_tree_deletes}
    assert {tenant for tenant, _ in trees} == {world.t1}
    trees_paths = {path for _, path in trees}
    assert f"agents/{key}" in trees_paths
    assert any(p.startswith("threads/") for p in trees_paths)
    assert not any(agent_key_for(_Y) in p for p in trees_paths)


@pytest.mark.asyncio
async def test_a_failed_step_keeps_the_versions_so_a_rerun_finishes() -> None:
    world, _ = await _world()

    class _Broken(InMemoryAgentInstanceStore):
        async def delete_for_agent(
            self, *, tenant_id: UUID, agent_code: str, keep_users: Collection[UUID] = ()
        ) -> int:
            raise OSError(5, "connect failed: postgresql://u:s3cret@db/x")

    broken = _Broken()
    await broken.touch(tenant_id=world.t1, agent_code=_X, user_id=uuid4())
    summary = await purge_agent(
        replace(world.deps(), agent_instances=broken), tenant_id=world.t1, name=_X
    )

    assert summary.failures == {"agent_instance": "OSError"}  # type only, never the message
    assert "s3cret" not in repr(summary.as_dict())
    assert not summary.ok and "agent_spec" in summary.skipped
    got_9 = await world.specs.list_by_tenant(tenant_id=world.t1, name=_X)
    assert len(got_9) == 2

    rerun = await purge_agent(world.deps(), tenant_id=world.t1, name=_X)
    assert rerun.ok
    got_10 = await world.specs.list_by_tenant(tenant_id=world.t1, name=_X)
    assert got_10 == []


@pytest.mark.asyncio
async def test_blockers_list_live_versions_and_live_referrers() -> None:
    world, _ = await _world()
    await world.specs.create(
        tenant_id=world.t1,
        spec=_spec("parent", subagent_of=f"{_X}@1.0.0"),
        spec_sha256="a" * 64,
        created_by="u",
    )
    await world.specs.create(
        tenant_id=world.t1,
        spec=_spec("gone-parent", subagent_of=f"{_X}@1.0.0"),
        spec_sha256="a" * 64,
        created_by="u",
    )
    await world.specs.update_status(
        tenant_id=world.t1, name="gone-parent", version="1.0.0", status=AgentSpecStatus.DELETED
    )
    await world.specs.restore(tenant_id=world.t1, name=_X, version="2.0.0")

    blockers = await find_blockers(world.specs, tenant_id=world.t1, name=_X)

    assert blockers.versions == ["1.0.0", "2.0.0"]
    assert blockers.active_versions == ["2.0.0"]
    assert blockers.referenced_by == ["parent@1.0.0"]
    assert blockers.blocked
    # The other tenant's same name is not blocked by this tenant's parent.
    got_11 = await find_blockers(world.specs, tenant_id=world.t2, name=_X)
    assert not got_11.blocked


@pytest.mark.asyncio
async def test_nas_agent_trees_and_file_versions_for_every_user(tmp_path: Path) -> None:
    from orchestrator.tools.agent_file_versions import NasAgentFileVersionStore
    from orchestrator.tools.nas_workspace_store import NasWorkspaceStore

    world, _ = await _world()
    nas = NasWorkspaceStore(root=str(tmp_path))
    versions = NasAgentFileVersionStore(workspace=nas)
    key, other_key = agent_key_for(_X), agent_key_for(_Y)
    db_user = (await world.threads.list_by_tenant(world.t1, agent_name=_X))[0].user_id
    assert db_user is not None
    files_only_user = uuid4()  # no database row left, only files on the NAS
    archived_user = uuid4()  # already purged as a user: its volume is being archived
    for user in (db_user, files_only_user, archived_user):
        notes = tmp_path / str(world.t1) / str(user) / "agents" / key / "AGENT_NOTES.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("- a\n")
        await versions.observe(tenant_id=world.t1, user_id=user, agent_key=key)
    sibling = tmp_path / str(world.t1) / str(db_user) / "agents" / other_key / "keep.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("keep")
    await versions.observe(tenant_id=world.t1, user_id=db_user, agent_key=other_key)
    other_tenant = tmp_path / str(world.t2) / str(db_user) / "agents" / key / "keep.md"
    other_tenant.parent.mkdir(parents=True)
    other_tenant.write_text("keep")
    marker = tmp_path / str(world.t1) / ".deleted" / str(archived_user)
    marker.parent.mkdir(parents=True)
    marker.write_text("")

    summary = await purge_agent(
        replace(
            world.deps(),
            workspace_store=nas,
            workspace_root=str(tmp_path),
            agent_file_versions=versions,
            artifact_snapshots=None,
        ),
        tenant_id=world.t1,
        name=_X,
    )

    assert summary.failures == {}
    history = tmp_path / str(world.t1) / ".agent-file-versions"
    for user in (db_user, files_only_user):
        assert not (tmp_path / str(world.t1) / str(user) / "agents" / key).exists()
        assert not (history / str(user) / key).exists()
    assert summary.deleted["agent_file_version"] == 2
    # Skipped: the archived user's tree is left to the user-purge archive path.
    assert (tmp_path / str(world.t1) / str(archived_user) / "agents" / key).exists()
    assert "nas_archived_users" in summary.skipped
    # Untouched: another agent's subtree + its history, and the other tenant.
    assert sibling.exists()
    assert (history / str(db_user) / other_key).exists()
    assert other_tenant.exists()


@pytest.mark.asyncio
async def test_store_restore_only_flips_a_deleted_version() -> None:
    """The API checks the status first; the store's own condition is what holds
    when a restore races a second restore or a purge."""
    world, _ = await _world()

    got_12 = await world.specs.restore(tenant_id=world.t1, name=_Y, version="1.0.0")
    assert got_12 is None
    restored = await world.specs.restore(tenant_id=world.t1, name=_X, version="1.0.0")
    assert restored is not None and restored.status is AgentSpecStatus.ACTIVE
    got_13 = await world.specs.restore(tenant_id=world.t1, name=_X, version="1.0.0")
    assert got_13 is None
