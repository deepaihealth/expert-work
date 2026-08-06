"""``RetentionCleanupJob`` — the D.3 nightly sweep.

Per STREAM-D-DESIGN § 2.6 + Mini-ADR D-5: M0 walks the rows with
``DELETE ... WHERE ctid IN (SELECT ... LIMIT N)`` rather than partition
drops. Simple, no schema churn, and the per-tenant retention shapes
fit in a single SQL statement using a JOIN with ``tenant_config``.

Three independent passes per ``run_once``:

1.  ``audit_log`` — only ``backup_acked = true`` rows past
    ``audit_retention_days``. Unacked candidates are counted + logged
    so SRE notices when the D.1c worker is lagging; the rows
    themselves are **never** deleted while unacked.
2.  ``event_log`` — past ``event_log_retention_days``. No WORM gate
    in M0 (cold archive to S3 is a Stream G item).
3.  ``jwt_blacklist`` — past ``expires_at``. Global, not tenant-scoped.

The whole sweep runs as ``retention_cleanup_worker`` (migration 0010,
NOLOGIN BYPASSRLS with the minimum delete grants).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.approval import ApprovalStore
from expert_work.persistence.artifact import ArtifactStore
from expert_work.persistence.image_upload import ImageUploadStore
from expert_work.persistence.memory import MemoryStore
from expert_work.persistence.tenant_user import TenantUserStore
from expert_work.persistence.workspace import UserWorkspaceStore
from expert_work.protocol import ApprovalStatus
from expert_work.runtime.storage import ObjectStore

logger = logging.getLogger(__name__)

# The cleanup runs against a DB connection that's already authenticated
# as a role with DELETE privilege on the target tables — typically
# ``retention_cleanup_worker`` (NOLOGIN role from migration 0010;
# operators ``ALTER ROLE ... WITH LOGIN`` for the cron user, or assign
# the role to a separate LOGIN account that's a member). We deliberately
# do NOT issue ``SET LOCAL ROLE`` in this code path: under asyncpg +
# SQLAlchemy 2.0, a ``SET LOCAL ROLE`` followed by a DELETE that
# actually matches rows intermittently returns "permission denied"
# even when ``has_table_privilege`` confirms the GRANT. Connecting
# directly as the worker role sidesteps the issue entirely.


@dataclass(frozen=True)
class CleanupReport:
    """Tally produced by one ``run_once`` sweep."""

    audit_deleted: int = 0
    audit_skipped_unacked: int = 0
    event_deleted: int = 0
    jwt_blacklist_deleted: int = 0
    # Mini-ADR J-32 (J.6.补强-3b) — image lifecycle hard-delete counts.
    image_uploads_hard_deleted: int = 0
    image_object_keys_removed: int = 0
    image_object_keys_failed: int = 0
    # Mini-ADR J-25 (J.9-step1) — artifact lifecycle counts.
    artifacts_soft_deleted: int = 0
    artifacts_hard_deleted: int = 0
    # Mini-ADR J-24 (J.8-step3b) — approvals auto-rejected past their
    # 24h ``timeout_at``.
    approvals_timed_out: int = 0
    # Deletion hygiene PR1 (Task 7) — 90-day physical hard-delete sweeps.
    memory_hard_deleted: int = 0
    workspaces_hard_deleted: int = 0
    workspace_archives_removed: int = 0
    workspace_archives_failed: int = 0
    workspaces_pending_archive: int = 0
    tenant_users_hard_deleted: int = 0
    # 波 1 PR-E —— 沙箱出网审计的保留期清理。
    sandbox_egress_audit_deleted: int = 0
    duration_seconds: float = 0.0
    # Per-tenant breakdown of audit deletes (for observability).
    audit_deleted_by_tenant: dict[str, int] = field(default_factory=dict)


class RetentionCleanupJob:
    """One-shot retention sweep driven by ``tenant_config`` per-tenant TTLs."""

    def __init__(
        self,
        *,
        db_session_factory: async_sessionmaker[AsyncSession],
        batch_size: int = 10000,
        image_upload_store: ImageUploadStore | None = None,
        object_store: ObjectStore | None = None,
        image_retention_days: int = 90,
        artifact_store: ArtifactStore | None = None,
        artifact_retention_days: int = 90,
        artifact_hard_delete_grace_days: int = 60,
        approval_store: ApprovalStore | None = None,
        memory_store: MemoryStore | None = None,
        memory_hard_delete_grace_days: int = 90,
        workspace_store: UserWorkspaceStore | None = None,
        workspace_archive_retention_days: int = 90,
        tenant_user_store: TenantUserStore | None = None,
        tenant_user_hard_delete_grace_days: int = 90,
        sandbox_egress_audit_retention_days: int = 90,
    ) -> None:
        if batch_size <= 0:
            msg = "batch_size must be positive"
            raise ValueError(msg)
        if image_retention_days < 1:
            msg = "image_retention_days must be >= 1"
            raise ValueError(msg)
        if artifact_retention_days < 1:
            msg = "artifact_retention_days must be >= 1"
            raise ValueError(msg)
        if artifact_hard_delete_grace_days < 1:
            msg = "artifact_hard_delete_grace_days must be >= 1"
            raise ValueError(msg)
        if memory_hard_delete_grace_days < 1:
            msg = "memory_hard_delete_grace_days must be >= 1"
            raise ValueError(msg)
        if workspace_archive_retention_days < 1:
            msg = "workspace_archive_retention_days must be >= 1"
            raise ValueError(msg)
        if tenant_user_hard_delete_grace_days < 1:
            msg = "tenant_user_hard_delete_grace_days must be >= 1"
            raise ValueError(msg)
        if sandbox_egress_audit_retention_days < 1:
            msg = "sandbox_egress_audit_retention_days must be >= 1"
            raise ValueError(msg)
        self._sf = db_session_factory
        self._batch_size = batch_size
        self._image_upload_store = image_upload_store
        self._object_store = object_store
        self._image_retention_days = image_retention_days
        self._artifact_store = artifact_store
        self._artifact_retention_days = artifact_retention_days
        self._artifact_hard_delete_grace_days = artifact_hard_delete_grace_days
        self._approval_store = approval_store
        self._memory_store = memory_store
        self._memory_grace_days = memory_hard_delete_grace_days
        self._workspace_store = workspace_store
        self._workspace_retention_days = workspace_archive_retention_days
        self._tenant_user_store = tenant_user_store
        self._tenant_user_grace_days = tenant_user_hard_delete_grace_days
        self._sandbox_egress_audit_retention_days = sandbox_egress_audit_retention_days

    async def run_once(self) -> CleanupReport:
        """Run the retention passes once and return a tally.

        Each pass owns its own session + transaction so the
        ``SET LOCAL ROLE`` is re-issued cleanly per pass. Sharing one
        session across all three passes triggered intermittent
        ``permission denied`` failures on later DELETEs in CI even
        though the role had the grants — the per-pass isolation
        avoids whatever cross-statement state interaction caused that.

        Mini-ADR J-32 (J.6.补强-3b) — the image-upload pass runs when
        both an :class:`ImageUploadStore` and an :class:`ObjectStore`
        are wired; otherwise it's a no-op (the audit / event / jwt
        passes still run, so unit tests that don't care about images
        keep working).
        """
        started = time.monotonic()
        audit_deleted, audit_by_tenant = await self._delete_audit_log()
        audit_skipped = await self._count_unacked_past_retention()
        event_deleted = await self._delete_event_log()
        sandbox_egress_audit_deleted = await self._delete_sandbox_egress_audit()
        jwt_deleted = await self._delete_expired_jwt_blacklist()
        image_rows, image_keys_ok, image_keys_failed = await self._delete_expired_images()
        artifact_soft, artifact_hard = await self._sweep_artifacts()
        approvals_timed_out = await self._sweep_approval_timeouts()
        memory_hard_deleted = await self._sweep_memory()
        tenant_users_hard_deleted = await self._sweep_tenant_users()
        (
            workspaces_hard_deleted,
            workspace_archives_removed,
            workspace_archives_failed,
            workspaces_pending_archive,
        ) = await self._sweep_workspaces()

        return CleanupReport(
            audit_deleted=audit_deleted,
            audit_skipped_unacked=audit_skipped,
            audit_deleted_by_tenant=audit_by_tenant,
            event_deleted=event_deleted,
            jwt_blacklist_deleted=jwt_deleted,
            image_uploads_hard_deleted=image_rows,
            image_object_keys_removed=image_keys_ok,
            image_object_keys_failed=image_keys_failed,
            artifacts_soft_deleted=artifact_soft,
            artifacts_hard_deleted=artifact_hard,
            approvals_timed_out=approvals_timed_out,
            memory_hard_deleted=memory_hard_deleted,
            workspaces_hard_deleted=workspaces_hard_deleted,
            workspace_archives_removed=workspace_archives_removed,
            workspace_archives_failed=workspace_archives_failed,
            workspaces_pending_archive=workspaces_pending_archive,
            tenant_users_hard_deleted=tenant_users_hard_deleted,
            sandbox_egress_audit_deleted=sandbox_egress_audit_deleted,
            duration_seconds=time.monotonic() - started,
        )

    async def _sweep_approval_timeouts(self) -> int:
        """Mini-ADR J-24 (J.8-step3b) — auto-reject approvals past 24h.

        A run paused for human approval has a ``timeout_at`` (default
        ``requested_at + 24h``). A pending row past that horizon is
        auto-rejected: ``mark_decided`` flips it to ``TIMEOUT`` with
        ``decided_by='system'``, so a later ``POST .../resume`` is
        refused (409 already-decided) and the paused checkpoint becomes
        logically dead — no run pins an approval slot forever.

        No-op when no :class:`ApprovalStore` is wired (unit-test path /
        deployments not running J.8).
        """
        if self._approval_store is None:
            return 0
        now = datetime.now(UTC)
        expired = await self._approval_store.list_expired(before=now, limit=self._batch_size)
        timed_out = 0
        for row in expired:
            ok = await self._approval_store.mark_decided(
                run_id=row.run_id,
                tenant_id=row.tenant_id,
                status=ApprovalStatus.TIMEOUT,
                decided_by="system",
                decided_at=now,
            )
            if ok:
                timed_out += 1
        return timed_out

    async def _sweep_memory(self) -> int:
        """Deletion hygiene PR1 (Task 7) — physically remove memory rows
        soft-deleted (K.K6 forget) past ``memory_hard_delete_grace_days``.

        No-op when no :class:`MemoryStore` is wired (unit-test path /
        deployments not running this pass).
        """
        if self._memory_store is None:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=self._memory_grace_days)
        return await self._memory_store.hard_delete_expired(before=cutoff, limit=self._batch_size)

    async def _sweep_tenant_users(self) -> int:
        """Deletion hygiene PR1 (Task 7) — physically remove tenant_user rows
        deactivated (Phase 3a purge_user) past
        ``tenant_user_hard_delete_grace_days``.

        No-op when no :class:`TenantUserStore` is wired (unit-test path /
        deployments not running this pass).
        """
        if self._tenant_user_store is None:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=self._tenant_user_grace_days)
        return await self._tenant_user_store.hard_delete_deactivated(
            before=cutoff, limit=self._batch_size
        )

    async def _sweep_workspaces(self) -> tuple[int, int, int, int]:
        """Deletion hygiene PR1 (Task 7) — physically remove ``user_workspace``
        rows whose archive (J.15) has aged past
        ``workspace_archive_retention_days``.

        Returns ``(rows_hard_deleted, keys_removed, keys_failed,
        pending_archive)``.

        The takeaway with the image-upload pass (:meth:`_delete_expired_images`)
        is reversed here: a failed archive-key delete **keeps** the row
        (the row is the only remaining lookup for the orphaned key —
        losing it would leak the archive forever), and the row is retried
        on the next nightly sweep. ``ObjectStore.delete`` is itself
        idempotent — deleting an already-gone key does not raise — so a
        genuinely missing archive is silently absorbed as success; only a
        real failure (permission / network / backend error) trips the
        ``except`` branch. Rows still awaiting their J.15 archive job
        (``archived_object_key IS NULL``) are counted separately as
        ``pending_archive`` — they are not candidates for this sweep at all.

        No-op when either :class:`UserWorkspaceStore` or
        :class:`~expert_work.runtime.storage.ObjectStore` is missing
        (unit-test path / deployments without an object store wired).
        """
        if self._workspace_store is None or self._object_store is None:
            return 0, 0, 0, 0
        cutoff = datetime.now(UTC) - timedelta(days=self._workspace_retention_days)
        pending = [
            w
            for w in await self._workspace_store.list_pending_archive()
            if w.deleted_at is not None and w.deleted_at < cutoff
        ]
        rows = await self._workspace_store.list_archived_expired(
            before=cutoff, limit=self._batch_size
        )
        hard = keys_ok = keys_failed = 0
        for ws in rows:
            assert ws.archived_object_key is not None  # noqa: S101 - list_archived_expired 谓词保证
            try:
                await self._object_store.delete(ws.archived_object_key)
                keys_ok += 1
            except Exception:
                keys_failed += 1
                logger.exception(
                    "retention.workspace_archive_delete_failed key=%s",
                    ws.archived_object_key,
                )
                continue
            if await self._workspace_store.hard_delete(workspace_id=ws.id):
                hard += 1
        return hard, keys_ok, keys_failed, len(pending)

    async def _sweep_artifacts(self) -> tuple[int, int]:
        """Mini-ADR J-25 (J.9-step1) — two-stage artifact lifecycle sweep.

        Stage 1: active rows past ``artifact_retention_days`` → soft-delete
        (sets ``deleted_at``). Stage 2: soft-deleted rows past
        ``artifact_hard_delete_grace_days`` → hard-delete (row +
        version rows).

        No-op when :class:`ArtifactStore` is not wired (unit-test path
        + deployments not running J.9). Workspace files are *not*
        removed here — J.15 volume lifecycle (Mini-ADR J-36) owns the
        underlying bytes; ``J.9-step1`` deliberately stops at the
        metadata. The follow-up archive 中间档 (tar.zst → ObjectStore)
        will land in a later step that reuses the J.15 archive flow.
        """
        if self._artifact_store is None:
            return 0, 0
        now = datetime.now(UTC)
        # Stage 1 — soft-delete active rows past retention.
        soft_cutoff = now - timedelta(days=self._artifact_retention_days)
        active = await self._artifact_store.list_active_past_retention(
            before=soft_cutoff, limit=self._batch_size
        )
        soft_count = 0
        for row in active:
            ok = await self._artifact_store.soft_delete(
                tenant_id=row.tenant_id,
                user_id=row.user_id,
                name=row.name,
                now=now,
            )
            if ok:
                soft_count += 1
        # Stage 2 — hard-delete soft-deleted rows past the grace window.
        hard_cutoff = now - timedelta(days=self._artifact_hard_delete_grace_days)
        expired = await self._artifact_store.list_expired(
            before=hard_cutoff, limit=self._batch_size
        )
        if not expired:
            return soft_count, 0
        hard_count = await self._artifact_store.hard_delete(artifact_ids=[a.id for a in expired])
        return soft_count, hard_count

    async def _delete_expired_images(self) -> tuple[int, int, int]:
        """Remove image rows past their retention window + their bytes.

        Mini-ADR J-32 — finds the reapable ``image_upload`` rows: those
        whose ``created_at`` is older than ``now - image_retention_days``,
        plus **any already-soft-deleted row regardless of age** (a user
        delete means the blob should leave at the next sweep, not at the
        original retention horizon). Removes the object-store key for
        each, then hard-deletes the row.

        Object-store failures don't block row hard-delete: an orphaned
        key is a far smaller correctness problem than a stuck row
        whose key never goes away (the bytes are billed against
        ``IMAGE_STORAGE_BYTES``). The failure count lands in the report
        so SRE can investigate without the sweep stalling.

        Returns ``(rows_deleted, keys_removed, keys_failed)``. Returns
        ``(0, 0, 0)`` when either store is missing (unit-test path).
        """
        if self._image_upload_store is None or self._object_store is None:
            return 0, 0, 0
        cutoff = datetime.now(UTC) - timedelta(days=self._image_retention_days)
        expired = await self._image_upload_store.list_reapable(
            before=cutoff,
            limit=self._batch_size,
        )
        if not expired:
            return 0, 0, 0
        keys_ok = 0
        keys_failed = 0
        for row in expired:
            try:
                await self._object_store.delete(row.object_key)
                keys_ok += 1
            except Exception:
                keys_failed += 1
                logger.exception(
                    "retention.image_object_delete_failed key=%s",
                    row.object_key,
                )
        rows = await self._image_upload_store.hard_delete(
            image_ids=[r.id for r in expired],
        )
        return rows, keys_ok, keys_failed

    # ------------------------------------------------------------------
    # Per-table helpers (private). Each opens its own session + txn,
    # SETs LOCAL ROLE retention_cleanup_worker, runs one statement,
    # commits.
    # ------------------------------------------------------------------

    async def _delete_audit_log(self) -> tuple[int, dict[str, int]]:
        """Delete acked audit rows past their tenant's retention window.

        Uses ``ctid`` subquery to apply LIMIT to a DELETE (Postgres
        doesn't support ``DELETE ... LIMIT`` directly). RETURNING
        ``tenant_id`` lets us tally per-tenant deletes for the report.

        The ``backup_acked = true`` predicate is the WORM safety
        gate: unacked rows are skipped here and counted separately
        by ``_count_unacked_past_retention``.
        """
        async with self._sf() as session:
            result = await session.execute(
                text(
                    """
                    DELETE FROM audit_log
                    WHERE ctid IN (
                        SELECT a.ctid
                        FROM audit_log a
                        JOIN tenant_config c ON c.tenant_id = a.tenant_id
                        WHERE a.backup_acked = true
                          AND a.occurred_at < now() - (c.audit_retention_days || ' days')::interval
                        LIMIT :batch
                    )
                    RETURNING tenant_id
                    """
                ),
                {"batch": self._batch_size},
            )
            rows = result.fetchall()
            await session.commit()
        per_tenant: dict[str, int] = {}
        for row in rows:
            tid = str(row[0])
            per_tenant[tid] = per_tenant.get(tid, 0) + 1
        return len(rows), per_tenant

    async def _count_unacked_past_retention(self) -> int:
        """How many audit rows are *past* retention but still unacked.

        Steady-state value is 0. A growing number means the D.1c
        WORM backup worker is falling behind and needs investigation;
        we surface it on the report but never delete those rows.
        """
        async with self._sf() as session:
            result = await session.execute(
                text(
                    """
                    SELECT count(*)
                    FROM audit_log a
                    JOIN tenant_config c ON c.tenant_id = a.tenant_id
                    WHERE a.backup_acked = false
                      AND a.occurred_at < now() - (c.audit_retention_days || ' days')::interval
                    """
                )
            )
            count = int(result.scalar() or 0)
            await session.commit()
        return count

    async def _delete_event_log(self) -> int:
        """Two-step: read retentions, then per-tenant flat DELETE.

        Investigation in CI showed that ``DELETE FROM event_log WHERE
        ctid IN (SELECT … LIMIT N)`` consistently raises ``permission
        denied for table event_log`` even though ``has_table_privilege``
        + a trivial probe ``DELETE FROM event_log WHERE id = -999999``
        both succeed for the same role in the same session. The
        ``ctid``-subquery + ``LIMIT`` form is the only thing that
        differs from the audit_log path that *does* work — and rather
        than chase the asyncpg/SQLAlchemy quirk further, the flat
        ``DELETE … WHERE tenant_id = :t AND created_at < :cutoff``
        form is plenty for M0 retention volumes. M1 can add ``LIMIT``
        back if the table grows large enough to need batching, by
        which time we'll have partitioning anyway.
        """
        retentions = await self._read_event_retentions()
        total = 0
        for tenant_id, days in retentions:
            async with self._sf() as session:
                result = await session.execute(
                    text(
                        "DELETE FROM event_log "
                        "WHERE tenant_id = :t "
                        "  AND created_at < now() - make_interval(days => :d) "
                        "RETURNING id"
                    ),
                    {"t": tenant_id, "d": days},
                )
                total += len(result.fetchall())
                await session.commit()
        return total

    async def _delete_sandbox_egress_audit(self) -> int:
        """删掉过了保留期的沙箱出网审计行。

        ``ctid`` 子查询给 DELETE 限批(Postgres 不支持 ``DELETE ... LIMIT``),
        与本文件其他几个 pass 同款。全局窗口、无 per-tenant 覆盖 —— 同
        ``image_retention_days`` 的理由。表级 GRANT 见迁移 0143:这个 pass 以
        ``retention_cleanup_worker`` 角色执行,少了那条授权就是 permission denied。

        Uses ``make_interval(days => :days)`` rather than
        ``(:days || ' days')::interval`` — the latter made Postgres infer
        the bind parameter's type as ``text`` (from the ``||`` operator),
        and asyncpg then refused to bind the Python ``int`` into a
        text-typed parameter (``DataError: invalid input for query
        argument $1: 90 (expected str, got int)``). ``make_interval``
        takes an integer directly, sidestepping the type-inference gap;
        it's also what ``_delete_event_log`` already uses for the same
        reason.
        """
        async with self._sf() as session:
            result = await session.execute(
                text(
                    """
                    DELETE FROM sandbox_egress_audit
                    WHERE ctid IN (
                        SELECT a.ctid
                        FROM sandbox_egress_audit a
                        WHERE a.occurred_at < now() - make_interval(days => :days)
                        LIMIT :batch
                    )
                    """
                ),
                {
                    "days": self._sandbox_egress_audit_retention_days,
                    "batch": self._batch_size,
                },
            )
            await session.commit()
        # ``Result.rowcount`` is only typed on ``CursorResult``; the base
        # ``Result`` mypy sees from ``session.execute(text(...))`` exposes
        # it at runtime but not at the type level (same as
        # ``expert_work.persistence.workspace.sql`` / ``skill.sql``).
        return result.rowcount or 0  # type: ignore[attr-defined]

    async def _read_event_retentions(self) -> list[tuple[str, int]]:
        """Return ``(tenant_id, event_log_retention_days)`` for every tenant."""
        async with self._sf() as session:
            result = await session.execute(
                text("SELECT tenant_id::text, event_log_retention_days FROM tenant_config")
            )
            rows = [(str(r[0]), int(r[1])) for r in result.fetchall()]
            await session.commit()
        return rows

    async def _delete_expired_jwt_blacklist(self) -> int:
        """``jwt_blacklist`` is global — no tenant_id, expire_at-driven."""
        async with self._sf() as session:
            result = await session.execute(
                text("DELETE FROM jwt_blacklist WHERE expires_at < now() RETURNING jti")
            )
            rows = result.fetchall()
            await session.commit()
        return len(rows)
