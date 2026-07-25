"""``purge_user`` — Phase 3a cascade purge of a user's data + assets.

Spec: ``docs/plans`` steady-forging-pony §Phase 3a. Given a ``tenant_user``
(``subject_type="user"``), remove every trace of the user's *personal* data
and sever the per-user link on the tenant-owned rows that must survive for
billing / analytics:

* **HARD-DELETE** (high PII) — the user's threads (checkpoint + runs + meta;
  ``thread_message`` cascades), long-term memory, memory-writeback DLQ,
  artifacts (+ versions), MCP OAuth connections, agent-instance bindings,
  approvals, triggers (+ their runs), webhook endpoints (+ their deliveries),
  image uploads, volume-backup DLQ, and — via the supervisor — the workspace
  volume (soft-deleted → reaper archives) + ``sandbox_instance`` rows.
* **ANONYMIZE** (KEEP the row, null the user link) — ``token_usage`` and
  ``agent_run`` (billing / analytics), ``skill`` actor columns (tenant IP),
  ``eval_dataset.source_user_id`` and ``curation_candidate.user_id``.
* **IDENTITY** — soft-deactivate the ``tenant_user`` row (``deleted_at``).

Modelled on ``api/tenants.py:_bulk_cancel_tenant_runs`` (enumerate-then-act,
paginated, capped with a non-silent truncation log). Every step is
**best-effort**: a failing step is logged + recorded in the summary and the
purge continues — one store's failure never aborts the whole cascade. The
whole thing is **idempotent**: re-running on an already-purged user is a safe
no-op (each store method skips already-gone / already-null rows).

NOTE on ``user_id`` shape: the ``tenant_user.id`` surrogate keys every owned
table EXCEPT ``mcp_oauth_connection``, which is keyed by the user's
``subject_id`` string (the OIDC sub / app-supplied id) — see
``api/mcp_oauth_api.py``. ``purge_user`` therefore takes both.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from control_plane.audit import emit
from control_plane.runtime import AgentRuntime
from expert_work.persistence.agent_instance.base import AgentInstanceStore
from expert_work.persistence.approval import ApprovalStore
from expert_work.persistence.artifact import ArtifactStore
from expert_work.persistence.curation import CurationCandidateStore, EvalDatasetStore
from expert_work.persistence.feedback_store import FeedbackStore
from expert_work.persistence.image_upload import ImageUploadStore
from expert_work.persistence.mcp_oauth_connection.base import McpOAuthConnectionStore
from expert_work.persistence.memory import MemoryStore, MemoryWritebackDLQ
from expert_work.persistence.skill import SkillStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.thread_meta import ThreadMetaStore
from expert_work.persistence.token_usage_store import TokenUsageStore
from expert_work.persistence.trigger import TriggerRunStore, TriggerStore
from expert_work.persistence.webhook import WebhookDeliveryStore, WebhookEndpointStore
from expert_work.persistence.workspace.dlq import VolumeBackupDLQ
from expert_work.protocol import AuditAction, AuditResult
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.runs import RunStore
from expert_work.runtime.storage import ObjectStore
from orchestrator.tools import SupervisorClient

logger = logging.getLogger("expert_work.control_plane.purge.user")

#: Thread enumeration page size + hard cap (mirrors ``_bulk_cancel_tenant_runs``).
#: A user with more than the cap of threads gets a partial thread purge, logged
#: (no silent truncation); the anonymize catch-all still nulls any surviving
#: ``agent_run`` user link, and a re-run finishes the rest.
_THREAD_PAGE = 500
_MAX_THREADS = 20_000


@dataclass(frozen=True)
class PurgeUserDeps:
    """The stores + services ``purge_user`` needs (wired from ``app.state``)."""

    threads: ThreadMetaStore
    runtime: AgentRuntime
    memory: MemoryStore
    memory_dlq: MemoryWritebackDLQ
    artifacts: ArtifactStore
    mcp_oauth: McpOAuthConnectionStore
    agent_instances: AgentInstanceStore
    approvals: ApprovalStore
    triggers: TriggerStore
    trigger_runs: TriggerRunStore
    webhook_endpoints: WebhookEndpointStore
    webhook_deliveries: WebhookDeliveryStore
    image_uploads: ImageUploadStore
    #: Deletion-hygiene purge (Task 8) — feedback rows on the user's threads.
    feedback: FeedbackStore
    #: ``None`` when no object store is wired (e.g. some dev/test deployments)
    #: — the image-blob purge step then hard-deletes rows only and counts them
    #: as skipped (the retention sweep reaps the orphaned keys later).
    object_store: ObjectStore | None
    #: ``None`` in control-plane deployments — the volume-backup DLQ is a
    #: supervisor-owned store (like ``sandbox_instance``); wire it if present.
    volume_backup_dlq: VolumeBackupDLQ | None
    token_usage: TokenUsageStore
    runs: RunStore
    skills: SkillStore
    eval_datasets: EvalDatasetStore
    curation_candidates: CurationCandidateStore
    tenant_users: TenantUserStore
    audit: AuditLogger
    #: ``None`` in deployments without a wired supervisor — the workspace step
    #: is then skipped + flagged (mirrors ``sessions.py`` supervisor optionality).
    supervisor: SupervisorClient | None


@dataclass
class PurgeSummary:
    """What ``purge_user`` did — per-store counts + any step that failed."""

    tenant_id: UUID
    user_id: UUID
    subject_id: str
    threads_purged: int = 0
    runs_deleted: int = 0
    threads_capped: bool = False
    #: store name → rows hard-deleted.
    deleted: dict[str, int] = field(default_factory=dict)
    #: store name → rows anonymized (kept, user link nulled).
    anonymized: dict[str, int] = field(default_factory=dict)
    workspace_marked_deleted: bool = False
    deactivated: bool = False
    #: step name → error string, for the steps that raised (best-effort).
    failures: dict[str, str] = field(default_factory=dict)
    #: Deletion-hygiene purge (Task 8) — object-store blob cleanup counts for
    #: the user's image uploads (row deletion itself is ``deleted["image_upload"]``).
    image_blobs_removed: int = 0
    image_blobs_failed: int = 0
    image_blobs_skipped: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "tenant_id": str(self.tenant_id),
            "user_id": str(self.user_id),
            "subject_id": self.subject_id,
            "threads_purged": self.threads_purged,
            "runs_deleted": self.runs_deleted,
            "threads_capped": self.threads_capped,
            "deleted": dict(self.deleted),
            "anonymized": dict(self.anonymized),
            "workspace_marked_deleted": self.workspace_marked_deleted,
            "deactivated": self.deactivated,
            "failures": dict(self.failures),
            "image_blobs_removed": self.image_blobs_removed,
            "image_blobs_failed": self.image_blobs_failed,
            "image_blobs_skipped": self.image_blobs_skipped,
            "ok": not self.failures,
        }


async def _step[T](summary: PurgeSummary, name: str, coro: Awaitable[T], *, default: T) -> T:
    """Run one best-effort step; record + swallow any failure, return ``default``.

    A single store's failure must not abort the cascade (a half-purged user is
    worse than a re-run). The failure is logged (no request-derived value in the
    message — CodeQL py/log-injection) and surfaced in the summary; the caller
    can re-run to retry.
    """
    try:
        return await coro
    except Exception as exc:  # best-effort: never abort the purge
        logger.warning("purge_user.step_failed step=%s", name, exc_info=True)
        summary.failures[name] = f"{type(exc).__name__}: {exc}"
        return default


async def _purge_threads(
    deps: PurgeUserDeps, summary: PurgeSummary, *, tenant_id: UUID, user_id: UUID
) -> None:
    """Enumerate the user's threads (paginated, capped) and hard-purge each.

    Replicates ``api/sessions.py`` ``purge_session`` per thread: delete the
    LangGraph checkpoint, delete the run rows, delete the ``thread_meta`` row
    (``thread_message`` cascades). Resilient per thread — one bad thread logs +
    continues. Threads are gathered first (delete shifts an offset scan)."""
    thread_ids: list[UUID] = []
    offset = 0
    while len(thread_ids) < _MAX_THREADS:
        page = await deps.threads.list_by_tenant(
            tenant_id,
            user_id=user_id,
            include_archived=True,
            limit=_THREAD_PAGE,
            offset=offset,
        )
        thread_ids.extend(m.thread_id for m in page)
        if len(page) < _THREAD_PAGE:
            break
        offset += _THREAD_PAGE
    if len(thread_ids) >= _MAX_THREADS:
        summary.threads_capped = True
        # No request-derived value in the message (CodeQL py/log-injection).
        logger.warning("purge_user.threads_capped hit the %d-thread cap", _MAX_THREADS)

    # Feedback on the user's threads, deleted BEFORE the per-thread purge loop
    # below: if a thread's delete fails partway through, we don't want to be
    # left with "the thread is gone but its feedback rows survive" — cascading
    # the comments first means a failed thread purge still leaves a consistent
    # (feedback-free) state to retry.
    try:
        summary.deleted["feedback"] = await deps.feedback.delete_for_threads(
            tenant_id=tenant_id, thread_ids=thread_ids
        )
    except Exception as exc:  # best-effort, same failure-recording shape as _step
        logger.warning("purge_user.feedback_failed", exc_info=True)
        summary.failures["feedback"] = f"{type(exc).__name__}: {exc}"

    # Approvals are keyed by thread, not user — the only writer (orchestrator
    # sse.py pause flow) stamps user_id=None on every row, so the per-user
    # delete below never matches. Thread-scope delete is the one that works.
    try:
        summary.deleted["agent_approval"] = await deps.approvals.delete_for_threads(
            thread_ids=thread_ids, tenant_id=tenant_id
        )
    except Exception as exc:
        logger.warning("purge_user.approvals_failed", exc_info=True)
        summary.failures["agent_approval"] = f"{type(exc).__name__}: {exc}"

    checkpointer = deps.runtime.durable_checkpointer
    adelete = getattr(checkpointer, "adelete_thread", None)
    for thread_id in thread_ids:
        if adelete is not None:
            try:
                await adelete(str(thread_id))
            except Exception:  # best-effort per thread
                logger.warning("purge_user.checkpoint_failed", exc_info=True)
        try:
            summary.runs_deleted += await deps.runtime.run_manager.delete_by_thread(
                thread_id, tenant_id=tenant_id
            )
        except Exception:
            # Best-effort — delete_by_thread empties the runs' run_event
            # children itself (deletion-hygiene PR3), so RESTRICT no longer
            # blocks it; anonymize only backstops a store failure here.
            logger.warning("purge_user.runs_failed", exc_info=True)
        try:
            if await deps.threads.delete(thread_id, tenant_id=tenant_id):
                summary.threads_purged += 1
        except Exception:  # best-effort per thread
            logger.warning("purge_user.thread_delete_failed", exc_info=True)


async def _purge_triggers(
    deps: PurgeUserDeps, summary: PurgeSummary, *, tenant_id: UUID, user_id: UUID
) -> None:
    """Delete the user's triggers, then their (FK-less) ``trigger_run`` children."""
    trigger_ids = await deps.triggers.delete_all_for_user(tenant_id=tenant_id, user_id=user_id)
    summary.deleted["agent_trigger"] = len(trigger_ids)
    summary.deleted["trigger_run"] = await deps.trigger_runs.delete_for_triggers(
        trigger_ids=trigger_ids, tenant_id=tenant_id
    )


async def _purge_webhooks(
    deps: PurgeUserDeps, summary: PurgeSummary, *, tenant_id: UUID, user_id: UUID
) -> None:
    """Delete the user's webhook endpoints, then their (FK-less) deliveries."""
    endpoint_ids = await deps.webhook_endpoints.delete_all_for_user(
        tenant_id=tenant_id, user_id=user_id
    )
    summary.deleted["webhook_endpoint"] = len(endpoint_ids)
    summary.deleted["webhook_delivery"] = await deps.webhook_deliveries.delete_for_endpoints(
        endpoint_ids=endpoint_ids, tenant_id=tenant_id
    )


async def _purge_images(
    deps: PurgeUserDeps, summary: PurgeSummary, *, tenant_id: UUID, user_id: UUID
) -> None:
    """Delete the user's image-upload object-store blobs, then their rows.

    Deletion-hygiene purge (Task 8) — ``list_for_user`` returns every row
    (active AND soft-deleted, per its docstring) so a soft-deleted image's
    blob is cleaned up too, not just the ones still visible in the UI.
    Row deletion always runs (even with no ``object_store`` wired) — an
    orphaned object-store key is recoverable by the retention sweep, a
    stuck ``image_upload`` row is not.
    """
    rows = await deps.image_uploads.list_for_user(tenant_id=tenant_id, user_id=user_id)
    if deps.object_store is None:
        summary.image_blobs_skipped = len(rows)
    else:
        for row in rows:
            try:
                await deps.object_store.delete(row.object_key)
                summary.image_blobs_removed += 1
            except Exception:  # best-effort: orphaned key beats a stuck purge
                summary.image_blobs_failed += 1
                logger.warning("purge_user.image_blob_failed", exc_info=True)
    summary.deleted["image_upload"] = await deps.image_uploads.delete_all_for_user(
        tenant_id=tenant_id, user_id=user_id
    )


async def purge_user(
    *,
    tenant_id: UUID,
    user_id: UUID,
    subject_id: str,
    deps: PurgeUserDeps,
    actor_id: str,
    trace_id: str | None = None,
) -> PurgeSummary:
    """Cascade-purge a user's data + assets (Phase 3a). Best-effort + idempotent.

    ``user_id`` is the surrogate ``tenant_user.id``; ``subject_id`` is the
    user's opaque app id (used only for ``mcp_oauth_connection``, which is keyed
    by string). ``actor_id`` is the admin performing the purge (for the audit
    row). Returns a :class:`PurgeSummary`; per-step failures are surfaced there,
    not raised — the endpoint returns 200 with the summary and the operator can
    re-run to retry the failed steps.
    """
    summary = PurgeSummary(tenant_id=tenant_id, user_id=user_id, subject_id=subject_id)

    # 1) Threads (checkpoint + runs + meta; thread_message cascades).
    await _step(
        summary,
        "threads",
        _purge_threads(deps, summary, tenant_id=tenant_id, user_id=user_id),
        default=None,
    )

    # 2) HARD-DELETE the high-PII per-user stores.
    summary.deleted["memory_item"] = await _step(
        summary,
        "memory_item",
        deps.memory.delete_all_for_user(tenant_id=tenant_id, user_id=user_id),
        default=0,
    )
    summary.deleted["memory_writeback_dlq"] = await _step(
        summary,
        "memory_writeback_dlq",
        deps.memory_dlq.delete_all_for_user(tenant_id=tenant_id, user_id=user_id),
        default=0,
    )
    summary.deleted["artifact"] = await _step(
        summary,
        "artifact",
        deps.artifacts.delete_all_for_user(tenant_id=tenant_id, user_id=user_id),
        default=0,
    )
    # mcp_oauth_connection is keyed by the string subject_id, NOT the surrogate.
    summary.deleted["mcp_oauth_connection"] = await _step(
        summary,
        "mcp_oauth_connection",
        deps.mcp_oauth.delete_all_for_user(tenant_id=tenant_id, user_id=subject_id),
        default=0,
    )
    summary.deleted["agent_instance"] = await _step(
        summary,
        "agent_instance",
        deps.agent_instances.delete_all_for_user(tenant_id=tenant_id, user_id=user_id),
        default=0,
    )
    # Backstop for future user-stamped approval rows; the thread-scope pass in
    # _purge_threads covers today's NULL-user_id rows (its count is
    # deleted["agent_approval"] — a separate key here so it isn't overwritten).
    summary.deleted["agent_approval_user_scope"] = await _step(
        summary,
        "agent_approval_user_scope",
        deps.approvals.delete_all_for_user(tenant_id=tenant_id, user_id=user_id),
        default=0,
    )
    await _step(
        summary,
        "triggers",
        _purge_triggers(deps, summary, tenant_id=tenant_id, user_id=user_id),
        default=None,
    )
    await _step(
        summary,
        "webhooks",
        _purge_webhooks(deps, summary, tenant_id=tenant_id, user_id=user_id),
        default=None,
    )
    await _step(
        summary,
        "image_upload",
        _purge_images(deps, summary, tenant_id=tenant_id, user_id=user_id),
        default=None,
    )
    if deps.volume_backup_dlq is not None:
        summary.deleted["volume_backup_dlq"] = await _step(
            summary,
            "volume_backup_dlq",
            deps.volume_backup_dlq.delete_all_for_user(tenant_id=tenant_id, user_id=user_id),
            default=0,
        )

    # 3) ANONYMIZE the billing / analytics / tenant-asset rows (KEEP the row).
    summary.anonymized["token_usage"] = await _step(
        summary,
        "token_usage",
        deps.token_usage.anonymize_all_for_user(tenant_id=tenant_id, user_id=user_id),
        default=0,
    )
    summary.anonymized["agent_run"] = await _step(
        summary,
        "agent_run",
        deps.runs.anonymize_all_for_user(tenant_id=tenant_id, user_id=user_id),
        default=0,
    )
    summary.anonymized["skill"] = await _step(
        summary,
        "skill",
        deps.skills.anonymize_all_for_user(tenant_id=tenant_id, user_id=user_id),
        default=0,
    )
    summary.anonymized["eval_dataset"] = await _step(
        summary,
        "eval_dataset",
        deps.eval_datasets.anonymize_all_for_user(tenant_id=tenant_id, user_id=user_id),
        default=0,
    )
    summary.anonymized["curation_candidate"] = await _step(
        summary,
        "curation_candidate",
        deps.curation_candidates.anonymize_all_for_user(tenant_id=tenant_id, user_id=user_id),
        default=0,
    )

    # 4) Workspace — supervisor soft-deletes the volume (reaper archives) +
    # drops the user's sandbox_instance rows. Skipped when no supervisor wired.
    if deps.supervisor is not None:
        await _step(
            summary,
            "workspace",
            deps.supervisor.mark_workspace_deleted(tenant_id=tenant_id, user_id=user_id),
            default=None,
        )
        summary.workspace_marked_deleted = "workspace" not in summary.failures
    else:
        summary.failures["workspace"] = "no supervisor client wired"

    # 5) Identity — soft-deactivate the tenant_user row (kept, recoverable).
    summary.deactivated = await _step(
        summary,
        "deactivate",
        deps.tenant_users.deactivate(user_id, tenant_id=tenant_id, now=datetime.now(UTC)),
        default=False,
    )

    # 6) Audit the whole purge with the per-store summary.
    await emit(
        deps.audit,
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=AuditAction.USER_PURGE,
        resource_type="user",
        resource_id=str(user_id),
        result=AuditResult.SUCCESS if not summary.failures else AuditResult.ERROR,
        reason=None if not summary.failures else "one or more purge steps failed",
        trace_id=trace_id,
        details=summary.as_dict(),
    )
    return summary
