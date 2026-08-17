"""Phase 3a — ``purge_user`` orchestration + the ``POST /v1/users/{id}:purge`` route.

The orchestration test seeds two users in one tenant plus a user in a second
tenant, purges user A, and asserts: A's high-PII rows are gone, A's billing
rows are anonymized (present, ``user_id`` null), the workspace mark was sent,
the ``tenant_user`` row is deactivated, ``USER_PURGE`` is audited, user B and
the other tenant are untouched, and a re-run is a safe no-op.

The endpoint test asserts the admin/viewer authz split.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.purge import PurgeUserDeps, purge_user
from control_plane.settings import Settings
from expert_work.persistence import (
    InMemoryApprovalStore,
    InMemoryArtifactStore,
    InMemoryCurationCandidateStore,
    InMemoryEvalDatasetStore,
    InMemoryMcpOAuthConnectionStore,
    InMemoryMemoryStore,
    InMemoryTenantUserStore,
    InMemoryThreadMetaStore,
    InMemoryTriggerRunStore,
    InMemoryTriggerStore,
    InMemoryWebhookDeliveryStore,
    InMemoryWebhookEndpointStore,
)
from expert_work.persistence.agent_instance.memory import InMemoryAgentInstanceStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.feedback_store import FeedbackRecord, InMemoryFeedbackStore
from expert_work.persistence.image_upload import InMemoryImageUploadStore
from expert_work.persistence.memory.dlq import InMemoryMemoryWritebackDLQ
from expert_work.persistence.skill import InMemorySkillStore
from expert_work.persistence.token_usage_store import InMemoryTokenUsageStore, TokenUsageRecord
from expert_work.persistence.user_upload import InMemoryUserUploadStore
from expert_work.persistence.workspace.dlq import InMemoryVolumeBackupDLQ
from expert_work.protocol import ApprovalRecord, AuditAction, AuditQuery, MemoryItem
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunStore,
    RunInfo,
    RunManager,
    RunStatus,
)
from expert_work.runtime.storage import InMemoryObjectStore, ObjectNotFoundError
from orchestrator.tools.workspace_store import RecordingWorkspaceStore
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)


def _mem(*, tenant: UUID, user: UUID, content: str) -> MemoryItem:
    return MemoryItem(
        id=uuid4(),
        tenant_id=tenant,
        user_id=user,
        kind="fact",  # type: ignore[arg-type]
        content=content,
        embedding=(1.0, 0.0),
    )


def _tok(*, tenant: UUID, user: UUID) -> TokenUsageRecord:
    return TokenUsageRecord(
        tenant_id=tenant,
        agent_name="alpha",
        agent_version="1.0.0",
        model="m1",
        user_id=user,
        input_tokens=10,
        output_tokens=5,
    )


_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def _run(*, tenant: UUID, user: UUID, thread: UUID) -> RunInfo:
    return RunInfo(
        run_id=uuid4(),
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


def _approval(*, tenant: UUID, thread: UUID) -> ApprovalRecord:
    """A NULL-``user_id`` approval — the shape every real row has (the only
    writer, orchestrator sse.py's pause flow, stamps ``user_id=None``)."""
    return ApprovalRecord(
        id=uuid4(),
        tenant_id=tenant,
        user_id=None,
        run_id=uuid4(),
        thread_id=thread,
        request_id="approval:purge-seed",
        node="tools",
        reason_kind="policy_gate",
        action_summary="approval-gated tool 'send_email'",
        requested_at=_NOW,
        timeout_at=_NOW + timedelta(hours=24),
    )


@pytest.mark.asyncio
async def test_purge_user_cascade_isolates_other_user_and_tenant_and_is_idempotent() -> None:
    t1, t2 = uuid4(), uuid4()
    threads = InMemoryThreadMetaStore()
    memory = InMemoryMemoryStore()
    memory_dlq = InMemoryMemoryWritebackDLQ()
    artifacts = InMemoryArtifactStore()
    mcp_oauth = InMemoryMcpOAuthConnectionStore()
    agent_instances = InMemoryAgentInstanceStore()
    approvals = InMemoryApprovalStore()
    triggers = InMemoryTriggerStore()
    trigger_runs = InMemoryTriggerRunStore()
    webhook_endpoints = InMemoryWebhookEndpointStore()
    webhook_deliveries = InMemoryWebhookDeliveryStore()
    image_uploads = InMemoryImageUploadStore()
    user_uploads = InMemoryUserUploadStore()
    feedback = InMemoryFeedbackStore()
    object_store = InMemoryObjectStore()
    volume_backup_dlq = InMemoryVolumeBackupDLQ()
    token_usage = InMemoryTokenUsageStore()
    runs = InMemoryRunStore()
    skills = InMemorySkillStore()
    eval_datasets = InMemoryEvalDatasetStore()
    curation = InMemoryCurationCandidateStore()
    users = InMemoryTenantUserStore()
    audit_store = InMemoryAuditLogStore()
    workspace_store = RecordingWorkspaceStore()

    a = await users.resolve(tenant_id=t1, subject_type="user", subject_id="subj-a")
    b = await users.resolve(tenant_id=t1, subject_type="user", subject_id="subj-b")
    c = await users.resolve(tenant_id=t2, subject_type="user", subject_id="subj-c")

    # --- User A (the purge target) — one of every relevant kind. ---
    t_a = uuid4()
    await threads.create(
        thread_id=t_a,
        tenant_id=t1,
        created_by="seed",
        user_id=a.id,
        agent_name="alpha",
        agent_version="1.0.0",
    )
    await runs.create(
        _run(tenant=t1, user=a.id, thread=t_a)
    )  # on A's thread → deleted by thread purge
    orphan_thread = uuid4()  # no thread_meta row → survives thread purge → anonymized
    await runs.create(_run(tenant=t1, user=a.id, thread=orphan_thread))
    await memory.write([_mem(tenant=t1, user=a.id, content="a-secret")])
    await token_usage.insert(_tok(tenant=t1, user=a.id))
    await artifacts.save_version(
        tenant_id=t1,
        user_id=a.id,
        name="doc",
        kind="document",
        path_in_workspace="/w",
        created_in_thread="t",
    )
    await agent_instances.touch(tenant_id=t1, agent_code="alpha", user_id=a.id)
    await mcp_oauth.create(
        tenant_id=t1, user_id="subj-a", catalog_id=uuid4(), name="c", resolved_url="https://x"
    )
    await skills.create_skill(skill_id=uuid4(), tenant_id=t1, name="s-a", created_by_user_id=a.id)
    image_id = uuid4()
    image_key = f"tenants/{t1}/uploads/{image_id}.png"
    await image_uploads.insert(
        image_id=image_id,
        tenant_id=t1,
        thread_id=t_a,
        user_id=a.id,
        object_key=image_key,
        size_bytes=10,
        mime_type="image/png",
        sha256="deadbeef" * 8,
    )
    await object_store.put(image_key, b"bytes")
    await feedback.insert(
        FeedbackRecord(tenant_id=t1, thread_id=t_a, rating="up", actor_id="subj-a")
    )

    # --- User B (same tenant) + User C (other tenant) — must survive intact. ---
    await memory.write([_mem(tenant=t1, user=b.id, content="b-keep")])
    await token_usage.insert(_tok(tenant=t1, user=b.id))
    await memory.write([_mem(tenant=t2, user=c.id, content="c-keep")])
    await token_usage.insert(_tok(tenant=t2, user=c.id))
    b_thread = uuid4()
    await threads.create(
        thread_id=b_thread,
        tenant_id=t1,
        created_by="seed",
        user_id=b.id,
        agent_name="alpha",
        agent_version="1.0.0",
    )
    await feedback.insert(
        FeedbackRecord(tenant_id=t1, thread_id=b_thread, rating="down", actor_id="subj-b")
    )

    deps = PurgeUserDeps(
        threads=threads,
        runtime=SimpleNamespace(durable_checkpointer=None, run_manager=RunManager(store=runs)),  # type: ignore[arg-type]
        memory=memory,
        memory_dlq=memory_dlq,
        artifacts=artifacts,
        mcp_oauth=mcp_oauth,
        agent_instances=agent_instances,
        approvals=approvals,
        triggers=triggers,
        trigger_runs=trigger_runs,
        webhook_endpoints=webhook_endpoints,
        webhook_deliveries=webhook_deliveries,
        image_uploads=image_uploads,
        user_uploads=user_uploads,
        feedback=feedback,
        object_store=object_store,
        volume_backup_dlq=volume_backup_dlq,
        token_usage=token_usage,
        runs=runs,
        skills=skills,
        eval_datasets=eval_datasets,
        curation_candidates=curation,
        tenant_users=users,
        audit=build_default_audit_logger(audit_store),
        workspace_store=workspace_store,
    )

    summary = await purge_user(
        tenant_id=t1, user_id=a.id, subject_id="subj-a", deps=deps, actor_id="admin"
    )

    # --- A's high-PII rows are gone. ---
    assert summary.threads_purged == 1
    assert await threads.get(t_a, tenant_id=t1) is None
    assert await memory.list_for_user(tenant_id=t1, user_id=a.id) == []
    assert await artifacts.list_for_user(tenant_id=t1, user_id=a.id, include_deleted=True) == []
    assert await agent_instances.list_by_user(tenant_id=t1, user_id=a.id) == []
    assert await mcp_oauth.list_for_user(tenant_id=t1, user_id="subj-a") == []

    # --- A's image blob is deleted from the object store + the row is gone. ---
    assert summary.deleted["image_upload"] == 1
    assert summary.image_blobs_removed == 1
    assert summary.image_blobs_failed == 0
    assert summary.image_blobs_skipped == 0
    assert await image_uploads.list_for_user(tenant_id=t1, user_id=a.id) == []
    with pytest.raises(ObjectNotFoundError):
        await object_store.get(image_key)

    # --- A's thread feedback is gone; B's (a different thread) survives. ---
    assert summary.deleted["feedback"] == 1
    assert await feedback.list_for_thread(thread_id=t_a) == []
    assert len(await feedback.list_for_thread(thread_id=b_thread)) == 1

    # --- A's billing / tenant-asset rows are KEPT + anonymized. ---
    a_tokens = [
        r for r in await token_usage.list_for_tenant(tenant_id=t1, limit=100) if r.user_id == a.id
    ]
    assert a_tokens == []  # no token row still points at A
    all_t1_tokens = await token_usage.list_for_tenant(tenant_id=t1, limit=100)
    assert len(all_t1_tokens) == 2  # A's row KEPT (nulled) + B's row
    # The surviving orphan agent_run was anonymized (kept, user link nulled).
    all_runs = await runs.list_for_tenant(tenant_id=t1, limit=100)
    assert [r.thread_id for r in all_runs] == [orphan_thread]
    assert all_runs[0].user_id is None

    # --- Workspace mark sent; tenant_user deactivated; USER_PURGE audited. ---
    assert (t1, a.id) in workspace_store.workspace_deletions
    assert summary.workspace_marked_deleted is True
    assert summary.deactivated is True
    assert {u.id for u in await users.list_by_tenant(t1, subject_type="user")} == {b.id}
    page = await audit_store.query(AuditQuery(tenant_id=t1, action=AuditAction.USER_PURGE))
    assert len(page.entries) == 1
    assert page.entries[0].resource_id == str(a.id)

    # --- User B + the other tenant are UNTOUCHED. ---
    assert len(await memory.list_for_user(tenant_id=t1, user_id=b.id)) == 1
    b_tokens = [r for r in all_t1_tokens if r.user_id == b.id]
    assert len(b_tokens) == 1
    assert len(await memory.list_for_user(tenant_id=t2, user_id=c.id)) == 1
    assert {u.id for u in await users.list_by_tenant(t2, subject_type="user")} == {c.id}

    # --- Re-running is a safe no-op (idempotent). ---
    summary2 = await purge_user(
        tenant_id=t1, user_id=a.id, subject_id="subj-a", deps=deps, actor_id="admin"
    )
    assert not summary2.failures
    assert summary2.deleted["memory_item"] == 0
    assert summary2.anonymized["token_usage"] == 0
    assert summary2.deactivated is True  # idempotent-True
    assert summary2.deleted["feedback"] == 0
    assert summary2.deleted["image_upload"] == 0
    assert summary2.image_blobs_removed == 0
    assert summary2.image_blobs_failed == 0
    assert summary2.image_blobs_skipped == 0


@pytest.mark.asyncio
async def test_purge_user_image_blobs_skipped_without_object_store() -> None:
    """No object store wired: rows still hard-delete; blobs are counted as
    'skipped' (not 'removed', not a failure) — the retention sweep reaps the
    now-orphaned object-store key on its next pass."""
    t1 = uuid4()
    threads = InMemoryThreadMetaStore()
    memory = InMemoryMemoryStore()
    memory_dlq = InMemoryMemoryWritebackDLQ()
    artifacts = InMemoryArtifactStore()
    mcp_oauth = InMemoryMcpOAuthConnectionStore()
    agent_instances = InMemoryAgentInstanceStore()
    approvals = InMemoryApprovalStore()
    triggers = InMemoryTriggerStore()
    trigger_runs = InMemoryTriggerRunStore()
    webhook_endpoints = InMemoryWebhookEndpointStore()
    webhook_deliveries = InMemoryWebhookDeliveryStore()
    image_uploads = InMemoryImageUploadStore()
    user_uploads = InMemoryUserUploadStore()
    feedback = InMemoryFeedbackStore()
    volume_backup_dlq = InMemoryVolumeBackupDLQ()
    token_usage = InMemoryTokenUsageStore()
    runs = InMemoryRunStore()
    skills = InMemorySkillStore()
    eval_datasets = InMemoryEvalDatasetStore()
    curation = InMemoryCurationCandidateStore()
    users = InMemoryTenantUserStore()
    audit_store = InMemoryAuditLogStore()
    workspace_store = RecordingWorkspaceStore()

    a = await users.resolve(tenant_id=t1, subject_type="user", subject_id="subj-a")
    image_id = uuid4()
    await image_uploads.insert(
        image_id=image_id,
        tenant_id=t1,
        thread_id=uuid4(),
        user_id=a.id,
        object_key=f"tenants/{t1}/uploads/{image_id}.png",
        size_bytes=1,
        mime_type="image/png",
        sha256="x",
    )

    deps = PurgeUserDeps(
        threads=threads,
        runtime=SimpleNamespace(durable_checkpointer=None, run_manager=RunManager(store=runs)),  # type: ignore[arg-type]
        memory=memory,
        memory_dlq=memory_dlq,
        artifacts=artifacts,
        mcp_oauth=mcp_oauth,
        agent_instances=agent_instances,
        approvals=approvals,
        triggers=triggers,
        trigger_runs=trigger_runs,
        webhook_endpoints=webhook_endpoints,
        webhook_deliveries=webhook_deliveries,
        image_uploads=image_uploads,
        user_uploads=user_uploads,
        feedback=feedback,
        object_store=None,
        volume_backup_dlq=volume_backup_dlq,
        token_usage=token_usage,
        runs=runs,
        skills=skills,
        eval_datasets=eval_datasets,
        curation_candidates=curation,
        tenant_users=users,
        audit=build_default_audit_logger(audit_store),
        workspace_store=workspace_store,
    )

    summary = await purge_user(
        tenant_id=t1, user_id=a.id, subject_id="subj-a", deps=deps, actor_id="admin"
    )

    assert "image_upload" not in summary.failures
    assert summary.deleted["image_upload"] == 1
    assert summary.image_blobs_skipped == 1
    assert summary.image_blobs_removed == 0
    assert summary.image_blobs_failed == 0
    assert await image_uploads.list_for_user(tenant_id=t1, user_id=a.id) == []


@pytest.mark.asyncio
async def test_purge_user_deletes_user_upload_rows() -> None:
    """对外附件模型统一(spec 2026-08-17) —— purge hard-deletes every
    ``user_upload`` row (document + image alike) for the target user, and
    leaves other users'/tenants' rows untouched."""
    t1 = uuid4()
    threads = InMemoryThreadMetaStore()
    memory = InMemoryMemoryStore()
    memory_dlq = InMemoryMemoryWritebackDLQ()
    artifacts = InMemoryArtifactStore()
    mcp_oauth = InMemoryMcpOAuthConnectionStore()
    agent_instances = InMemoryAgentInstanceStore()
    approvals = InMemoryApprovalStore()
    triggers = InMemoryTriggerStore()
    trigger_runs = InMemoryTriggerRunStore()
    webhook_endpoints = InMemoryWebhookEndpointStore()
    webhook_deliveries = InMemoryWebhookDeliveryStore()
    image_uploads = InMemoryImageUploadStore()
    user_uploads = InMemoryUserUploadStore()
    feedback = InMemoryFeedbackStore()
    volume_backup_dlq = InMemoryVolumeBackupDLQ()
    token_usage = InMemoryTokenUsageStore()
    runs = InMemoryRunStore()
    skills = InMemorySkillStore()
    eval_datasets = InMemoryEvalDatasetStore()
    curation = InMemoryCurationCandidateStore()
    users = InMemoryTenantUserStore()
    audit_store = InMemoryAuditLogStore()
    workspace_store = RecordingWorkspaceStore()

    a = await users.resolve(tenant_id=t1, subject_type="user", subject_id="subj-a")
    b = await users.resolve(tenant_id=t1, subject_type="user", subject_id="subj-b")
    doc_id = uuid4()
    image_id = uuid4()
    await user_uploads.insert(
        upload_id=doc_id,
        tenant_id=t1,
        user_id=a.id,
        thread_id=uuid4(),
        kind="document",
        ref="uploads/report.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        filename="report.pdf",
    )
    await user_uploads.insert(
        upload_id=image_id,
        tenant_id=t1,
        user_id=a.id,
        thread_id=uuid4(),
        kind="image",
        ref="expert_work://image/x/y/z.png",
        mime_type="image/png",
        size_bytes=1,
        filename="z.png",
    )
    # Another user's row in the same tenant — must survive.
    other_upload_id = uuid4()
    await user_uploads.insert(
        upload_id=other_upload_id,
        tenant_id=t1,
        user_id=b.id,
        thread_id=uuid4(),
        kind="document",
        ref="uploads/other.pdf",
        mime_type="application/pdf",
        size_bytes=1,
        filename="other.pdf",
    )

    deps = PurgeUserDeps(
        threads=threads,
        runtime=SimpleNamespace(durable_checkpointer=None, run_manager=RunManager(store=runs)),  # type: ignore[arg-type]
        memory=memory,
        memory_dlq=memory_dlq,
        artifacts=artifacts,
        mcp_oauth=mcp_oauth,
        agent_instances=agent_instances,
        approvals=approvals,
        triggers=triggers,
        trigger_runs=trigger_runs,
        webhook_endpoints=webhook_endpoints,
        webhook_deliveries=webhook_deliveries,
        image_uploads=image_uploads,
        user_uploads=user_uploads,
        feedback=feedback,
        object_store=None,
        volume_backup_dlq=volume_backup_dlq,
        token_usage=token_usage,
        runs=runs,
        skills=skills,
        eval_datasets=eval_datasets,
        curation_candidates=curation,
        tenant_users=users,
        audit=build_default_audit_logger(audit_store),
        workspace_store=workspace_store,
    )

    summary = await purge_user(
        tenant_id=t1, user_id=a.id, subject_id="subj-a", deps=deps, actor_id="admin"
    )

    assert "user_upload" not in summary.failures
    assert summary.deleted["user_upload"] == 2
    assert await user_uploads.get(upload_id=doc_id, tenant_id=t1) is None
    assert await user_uploads.get(upload_id=image_id, tenant_id=t1) is None
    # User B's row is untouched.
    assert await user_uploads.get(upload_id=other_upload_id, tenant_id=t1) is not None


class _FailingObjectStore(InMemoryObjectStore):
    """``delete`` always raises; every other op behaves like the real in-memory store.

    Used to drive the ``_purge_images`` best-effort branch: a blob-delete
    failure must be counted in ``image_blobs_failed`` and must NOT block the
    row deletion that follows (``delete_all_for_user``) or fail the
    ``image_upload`` purge step.
    """

    async def delete(self, key: str) -> None:
        raise RuntimeError("blob store unavailable")


@pytest.mark.asyncio
async def test_purge_user_image_blob_delete_failure_still_deletes_rows() -> None:
    """Blob delete raising for every image: rows are still hard-deleted.

    Deletion-hygiene purge (Task 8) — ``_purge_images`` treats a blob-store
    failure as best-effort: it's counted in ``image_blobs_failed`` but must
    not prevent ``delete_all_for_user`` from running, and must not count as a
    step failure (an orphaned object-store key is recoverable by the
    retention sweep; a stuck ``image_upload`` row is not).
    """
    t1 = uuid4()
    threads = InMemoryThreadMetaStore()
    memory = InMemoryMemoryStore()
    memory_dlq = InMemoryMemoryWritebackDLQ()
    artifacts = InMemoryArtifactStore()
    mcp_oauth = InMemoryMcpOAuthConnectionStore()
    agent_instances = InMemoryAgentInstanceStore()
    approvals = InMemoryApprovalStore()
    triggers = InMemoryTriggerStore()
    trigger_runs = InMemoryTriggerRunStore()
    webhook_endpoints = InMemoryWebhookEndpointStore()
    webhook_deliveries = InMemoryWebhookDeliveryStore()
    image_uploads = InMemoryImageUploadStore()
    user_uploads = InMemoryUserUploadStore()
    feedback = InMemoryFeedbackStore()
    object_store = _FailingObjectStore()
    volume_backup_dlq = InMemoryVolumeBackupDLQ()
    token_usage = InMemoryTokenUsageStore()
    runs = InMemoryRunStore()
    skills = InMemorySkillStore()
    eval_datasets = InMemoryEvalDatasetStore()
    curation = InMemoryCurationCandidateStore()
    users = InMemoryTenantUserStore()
    audit_store = InMemoryAuditLogStore()
    workspace_store = RecordingWorkspaceStore()

    a = await users.resolve(tenant_id=t1, subject_type="user", subject_id="subj-a")
    image_ids = [uuid4(), uuid4()]
    for image_id in image_ids:
        object_key = f"tenants/{t1}/uploads/{image_id}.png"
        await image_uploads.insert(
            image_id=image_id,
            tenant_id=t1,
            thread_id=uuid4(),
            user_id=a.id,
            object_key=object_key,
            size_bytes=1,
            mime_type="image/png",
            sha256="x",
        )
        # No ``put`` needed — the store is never actually read; ``delete``
        # always raises regardless of whether the key exists.

    deps = PurgeUserDeps(
        threads=threads,
        runtime=SimpleNamespace(durable_checkpointer=None, run_manager=RunManager(store=runs)),  # type: ignore[arg-type]
        memory=memory,
        memory_dlq=memory_dlq,
        artifacts=artifacts,
        mcp_oauth=mcp_oauth,
        agent_instances=agent_instances,
        approvals=approvals,
        triggers=triggers,
        trigger_runs=trigger_runs,
        webhook_endpoints=webhook_endpoints,
        webhook_deliveries=webhook_deliveries,
        image_uploads=image_uploads,
        user_uploads=user_uploads,
        feedback=feedback,
        object_store=object_store,
        volume_backup_dlq=volume_backup_dlq,
        token_usage=token_usage,
        runs=runs,
        skills=skills,
        eval_datasets=eval_datasets,
        curation_candidates=curation,
        tenant_users=users,
        audit=build_default_audit_logger(audit_store),
        workspace_store=workspace_store,
    )

    summary = await purge_user(
        tenant_id=t1, user_id=a.id, subject_id="subj-a", deps=deps, actor_id="admin"
    )

    assert summary.image_blobs_failed == len(image_ids)
    assert summary.image_blobs_removed == 0
    assert summary.image_blobs_skipped == 0
    assert summary.deleted["image_upload"] == len(image_ids)
    assert "image_upload" not in summary.failures
    assert await image_uploads.list_for_user(tenant_id=t1, user_id=a.id) == []


@pytest.mark.asyncio
async def test_purge_user_deletes_null_user_approvals_on_user_threads() -> None:
    """Approvals are wiped by *thread*, not user — regression sentinel.

    Every real ``agent_approval`` row has ``user_id=None`` (the only writer,
    orchestrator sse.py's pause flow, hardcodes it), so the per-user
    ``delete_all_for_user`` backstop matches nothing today — the thread-scope
    pass in ``_purge_threads`` must be the one that removes the rows.
    A NULL-``user_id`` approval on ANOTHER user's thread must survive
    (thread-predicate sentinel: the delete is thread-scoped, not tenant-wide).
    """
    t1 = uuid4()
    threads = InMemoryThreadMetaStore()
    memory = InMemoryMemoryStore()
    memory_dlq = InMemoryMemoryWritebackDLQ()
    artifacts = InMemoryArtifactStore()
    mcp_oauth = InMemoryMcpOAuthConnectionStore()
    agent_instances = InMemoryAgentInstanceStore()
    approvals = InMemoryApprovalStore()
    triggers = InMemoryTriggerStore()
    trigger_runs = InMemoryTriggerRunStore()
    webhook_endpoints = InMemoryWebhookEndpointStore()
    webhook_deliveries = InMemoryWebhookDeliveryStore()
    image_uploads = InMemoryImageUploadStore()
    user_uploads = InMemoryUserUploadStore()
    feedback = InMemoryFeedbackStore()
    volume_backup_dlq = InMemoryVolumeBackupDLQ()
    token_usage = InMemoryTokenUsageStore()
    runs = InMemoryRunStore()
    skills = InMemorySkillStore()
    eval_datasets = InMemoryEvalDatasetStore()
    curation = InMemoryCurationCandidateStore()
    users = InMemoryTenantUserStore()
    audit_store = InMemoryAuditLogStore()
    workspace_store = RecordingWorkspaceStore()

    a = await users.resolve(tenant_id=t1, subject_type="user", subject_id="subj-a")
    b = await users.resolve(tenant_id=t1, subject_type="user", subject_id="subj-b")
    t_a, t_b = uuid4(), uuid4()
    await threads.create(
        thread_id=t_a,
        tenant_id=t1,
        created_by="seed",
        user_id=a.id,
        agent_name="alpha",
        agent_version="1.0.0",
    )
    await threads.create(
        thread_id=t_b,
        tenant_id=t1,
        created_by="seed",
        user_id=b.id,
        agent_name="alpha",
        agent_version="1.0.0",
    )
    approval_a = _approval(tenant=t1, thread=t_a)  # on A's thread → must be deleted
    approval_b = _approval(tenant=t1, thread=t_b)  # on B's thread → must survive
    await approvals.create(approval_a)
    await approvals.create(approval_b)

    deps = PurgeUserDeps(
        threads=threads,
        runtime=SimpleNamespace(durable_checkpointer=None, run_manager=RunManager(store=runs)),  # type: ignore[arg-type]
        memory=memory,
        memory_dlq=memory_dlq,
        artifacts=artifacts,
        mcp_oauth=mcp_oauth,
        agent_instances=agent_instances,
        approvals=approvals,
        triggers=triggers,
        trigger_runs=trigger_runs,
        webhook_endpoints=webhook_endpoints,
        webhook_deliveries=webhook_deliveries,
        image_uploads=image_uploads,
        user_uploads=user_uploads,
        feedback=feedback,
        object_store=None,
        volume_backup_dlq=volume_backup_dlq,
        token_usage=token_usage,
        runs=runs,
        skills=skills,
        eval_datasets=eval_datasets,
        curation_candidates=curation,
        tenant_users=users,
        audit=build_default_audit_logger(audit_store),
        workspace_store=workspace_store,
    )

    summary = await purge_user(
        tenant_id=t1, user_id=a.id, subject_id="subj-a", deps=deps, actor_id="admin"
    )

    # A's thread's NULL-user approval is gone, counted under agent_approval —
    # and the count survives the later per-user backstop step (no overwrite).
    assert summary.deleted["agent_approval"] == 1
    assert "agent_approval" not in summary.failures
    assert await approvals.get_by_run(run_id=approval_a.run_id, tenant_id=t1) is None
    # B's thread's NULL approval is untouched (thread predicate, not tenant-wide).
    assert await approvals.get_by_run(run_id=approval_b.run_id, tenant_id=t1) is not None


class _FailingApprovalStore(InMemoryApprovalStore):
    """``delete_for_threads`` always raises; every other op behaves normally.

    Drives the ``_purge_threads`` approval-cleanup best-effort branch (its own
    inner ``try``/``except``, distinct from the outer per-step ``_step``
    wrapper): a failure there must be recorded and must NOT abort the rest of
    ``_purge_threads`` — the checkpoint/run/thread-row deletion loop that
    follows in the same function must still run.
    """

    #: Shaped like a real driver error — the realistic leak is a connect
    #: failure whose message carries the DSN, password included. Asserted
    #: absent from the response-visible summary below.
    SECRET_IN_MESSAGE = "connect failed: postgresql://purge:s3cr3t@rds-internal:5432/ew"

    async def delete_for_threads(self, *, thread_ids: Sequence[UUID], tenant_id: UUID) -> int:
        raise RuntimeError(self.SECRET_IN_MESSAGE)


class _FailingFeedbackStore(InMemoryFeedbackStore):
    """``delete_for_threads`` always raises; drives the feedback best-effort branch.

    Third of the three sites that write ``summary.failures`` (``_step`` plus
    the two hand-rolled ``except`` blocks in ``_purge_threads``). Each needs
    its own guard: they are separate literals, so one regressing does not make
    the others' assertions fail.
    """

    SECRET_IN_MESSAGE = "open /mnt/workspaces/1a2b/feedback.db failed: uid=10000 mode=0600"

    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        raise RuntimeError(self.SECRET_IN_MESSAGE)


@pytest.mark.asyncio
async def test_purge_user_approval_cleanup_failure_recorded_and_does_not_abort() -> None:
    """``approvals.delete_for_threads`` raising —— failure is recorded under
    ``agent_approval`` and the rest of ``_purge_threads`` (thread-row
    deletion, which runs later in the SAME function) still completes.

    Guards the inner try/except around the approval cleanup in
    ``_purge_threads`` (distinct from ``purge_user``'s outer per-step
    ``_step`` wrapper around the whole ``_purge_threads`` coroutine): if that
    inner guard were removed, the exception would propagate out of
    ``_purge_threads`` entirely, get caught by the OUTER ``_step`` instead
    (recorded as ``failures["threads"]``, not ``failures["agent_approval"]``),
    and abort the function before the per-thread deletion loop below it ever
    runs — leaving ``threads_purged == 0``.

    Both hand-rolled ``except`` blocks in ``_purge_threads`` (feedback and
    approvals) are driven here rather than in two near-identical tests — the
    deps fixture below is ~25 stores long. ``_step``, the third writer of
    ``summary.failures``, is guarded at the real response boundary instead
    (``test_members_api.py``).

    **The two branches are not symmetric, so assertion order matters.**
    Feedback cleanup runs first; if *its* guard is removed the exception
    escapes ``_purge_threads`` entirely, the outer ``_step`` catches it as
    ``failures["threads"]``, and the approvals block never executes — so a
    feedback regression surfaces as the *approvals* assertion failing, with a
    misleading message. Feedback is therefore asserted first, before anything
    approvals-specific. (The reverse does not happen: removing the approvals
    guard leaves feedback untouched.)
    """
    t1 = uuid4()
    threads = InMemoryThreadMetaStore()
    memory = InMemoryMemoryStore()
    memory_dlq = InMemoryMemoryWritebackDLQ()
    artifacts = InMemoryArtifactStore()
    mcp_oauth = InMemoryMcpOAuthConnectionStore()
    agent_instances = InMemoryAgentInstanceStore()
    approvals = _FailingApprovalStore()
    triggers = InMemoryTriggerStore()
    trigger_runs = InMemoryTriggerRunStore()
    webhook_endpoints = InMemoryWebhookEndpointStore()
    webhook_deliveries = InMemoryWebhookDeliveryStore()
    image_uploads = InMemoryImageUploadStore()
    user_uploads = InMemoryUserUploadStore()
    feedback = _FailingFeedbackStore()
    volume_backup_dlq = InMemoryVolumeBackupDLQ()
    token_usage = InMemoryTokenUsageStore()
    runs = InMemoryRunStore()
    skills = InMemorySkillStore()
    eval_datasets = InMemoryEvalDatasetStore()
    curation = InMemoryCurationCandidateStore()
    users = InMemoryTenantUserStore()
    audit_store = InMemoryAuditLogStore()
    workspace_store = RecordingWorkspaceStore()

    a = await users.resolve(tenant_id=t1, subject_type="user", subject_id="subj-a")
    t_a = uuid4()
    await threads.create(
        thread_id=t_a,
        tenant_id=t1,
        created_by="seed",
        user_id=a.id,
        agent_name="alpha",
        agent_version="1.0.0",
    )
    await approvals.create(_approval(tenant=t1, thread=t_a))

    deps = PurgeUserDeps(
        threads=threads,
        runtime=SimpleNamespace(durable_checkpointer=None, run_manager=RunManager(store=runs)),  # type: ignore[arg-type]
        memory=memory,
        memory_dlq=memory_dlq,
        artifacts=artifacts,
        mcp_oauth=mcp_oauth,
        agent_instances=agent_instances,
        approvals=approvals,
        triggers=triggers,
        trigger_runs=trigger_runs,
        webhook_endpoints=webhook_endpoints,
        webhook_deliveries=webhook_deliveries,
        image_uploads=image_uploads,
        user_uploads=user_uploads,
        feedback=feedback,
        object_store=None,
        volume_backup_dlq=volume_backup_dlq,
        token_usage=token_usage,
        runs=runs,
        skills=skills,
        eval_datasets=eval_datasets,
        curation_candidates=curation,
        tenant_users=users,
        audit=build_default_audit_logger(audit_store),
        workspace_store=workspace_store,
    )

    summary = await purge_user(
        tenant_id=t1, user_id=a.id, subject_id="subj-a", deps=deps, actor_id="admin"
    )

    # Feedback first — see the docstring: its guard failing would also break
    # every approvals assertion below, and the message would point at the
    # wrong branch.
    assert summary.failures["feedback"] == "RuntimeError"
    # Failure recorded under the specific step name, not just "threads" as a
    # whole — proves the inner try/except (not the outer _step) caught it.
    assert "agent_approval" in summary.failures
    # Exception TYPE only — the message never reaches the response body. The
    # summary is serialized straight into the purge response, and an exception
    # message is arbitrary text from whichever layer raised it (here: a DSN
    # with its password). Equality, not a substring check: "does not contain
    # the secret" would still pass if the value grew some *other* leaked field.
    assert summary.failures["agent_approval"] == "RuntimeError"
    rendered = str(summary.as_dict())
    assert _FailingApprovalStore.SECRET_IN_MESSAGE not in rendered
    assert _FailingFeedbackStore.SECRET_IN_MESSAGE not in rendered
    # The rest of _purge_threads — which runs AFTER the approval-cleanup
    # block, in the same function — still executed: the thread row itself
    # was purged despite the approval-cleanup exception.
    assert summary.threads_purged == 1
    # purge_user's outer per-step wrapper never saw an exception either — the
    # "threads" step (the whole _purge_threads coroutine) is not itself
    # recorded as failed.
    assert "threads" not in summary.failures


# --------------------------------------------------------------------------- #
# Endpoint authz — admin 200, viewer 403
# --------------------------------------------------------------------------- #
_ENDPOINT_TENANT = UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
async def app_client() -> AsyncIterator[tuple[AsyncClient, UUID]]:
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    user = await app.state.tenant_user_repo.resolve(
        tenant_id=_ENDPOINT_TENANT, subject_type="user", subject_id="victim", display_name="V"
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, user.id


@pytest.mark.asyncio
async def test_purge_endpoint_admin_gets_summary(app_client: tuple[AsyncClient, UUID]) -> None:
    client, user_id = app_client
    jwt = make_test_jwt(tenant_id=_ENDPOINT_TENANT, subject=str(uuid4()), roles=("admin",))
    resp = await client.post(
        f"/v1/users/{user_id}:purge", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["user_id"] == str(user_id)
    assert data["deactivated"] is True
    # The purged user drops out of the roster.
    listed = await client.get("/v1/users", headers={"Authorization": f"Bearer {jwt}"})
    ids = {row["user_id"] for row in listed.json()["data"]["items"]}
    assert str(user_id) not in ids


@pytest.mark.asyncio
async def test_purge_endpoint_viewer_forbidden(app_client: tuple[AsyncClient, UUID]) -> None:
    client, user_id = app_client
    jwt = make_test_jwt(tenant_id=_ENDPOINT_TENANT, subject=str(uuid4()), roles=("viewer",))
    resp = await client.post(
        f"/v1/users/{user_id}:purge", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_purge_endpoint_unknown_user_404(app_client: tuple[AsyncClient, UUID]) -> None:
    client, _ = app_client
    jwt = make_test_jwt(tenant_id=_ENDPOINT_TENANT, subject=str(uuid4()), roles=("admin",))
    resp = await client.post(
        f"/v1/users/{uuid4()}:purge", headers={"Authorization": f"Bearer {jwt}"}
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_purge_endpoint_allows_employee_member() -> None:
    """Purge is decoupled from account deletion — an employee's *data* is purgeable here too.

    Just like any other user, this data-only endpoint clears
    conversations/memory/workspace and soft-deactivates the ``tenant_user``
    row, but it never touches ``tenant_member`` (role, status, Keycloak
    account) — deleting the employee's *account* is still only done from the
    members page (revoke), unaffected by this endpoint.
    """
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    user = await app.state.tenant_user_repo.resolve(
        tenant_id=_ENDPOINT_TENANT, subject_type="user", subject_id="employee", display_name="E"
    )
    members = app.state.tenant_member_repo
    member = await members.create(
        tenant_id=_ENDPOINT_TENANT,
        email="e@corp.test",
        role="operator",
        invited_by="admin",
        keycloak_user_id="kc-e",  # active-consistency CHECK needs this non-NULL
    )
    # Back-fill subject_id == the employee's tenant_user.id (W3 first-login link).
    assert await members.transition(
        member_id=member.id,
        tenant_id=_ENDPOINT_TENANT,
        to="active",
        now=datetime.now(UTC),
        subject_id=user.id,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        jwt = make_test_jwt(tenant_id=_ENDPOINT_TENANT, subject=str(uuid4()), roles=("admin",))
        resp = await client.post(
            f"/v1/users/{user.id}:purge", headers={"Authorization": f"Bearer {jwt}"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["deactivated"] is True
        # The purged employee drops out of the roster (data gone)...
        listed = await client.get("/v1/users", headers={"Authorization": f"Bearer {jwt}"})
        ids = {row["user_id"] for row in listed.json()["data"]["items"]}
        assert str(user.id) not in ids
    # ...the tenant_user row is soft-deactivated (data-only)...
    still = await app.state.tenant_user_repo.get(user.id, tenant_id=_ENDPOINT_TENANT)
    assert still is not None and still.deleted_at is not None
    # ...but the console membership, role and Keycloak account are untouched.
    member_after = await members.get(tenant_id=_ENDPOINT_TENANT, member_id=member.id)
    assert member_after is not None
    assert member_after.status == "active"
    assert member_after.role == "operator"
    assert member_after.keycloak_user_id == "kc-e"


@pytest.mark.asyncio
async def test_purge_endpoint_allows_self() -> None:
    """The caller may purge their own data — no backend self-block.

    Data-only: purging yourself clears your conversations/memory/workspace
    like purging anyone else; it never signs you out or touches your account
    (no Keycloak / role changes). The UI carries the extra confirmation
    weight for a self-purge; the backend treats it the same as any target.
    """
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    caller_sub = str(uuid4())
    user = await app.state.tenant_user_repo.resolve(
        tenant_id=_ENDPOINT_TENANT, subject_type="user", subject_id=caller_sub, display_name="Self"
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # The caller's own JWT sub matches the purge target's subject_id.
        jwt = make_test_jwt(tenant_id=_ENDPOINT_TENANT, subject=caller_sub, roles=("admin",))
        resp = await client.post(
            f"/v1/users/{user.id}:purge", headers={"Authorization": f"Bearer {jwt}"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["deactivated"] is True
