"""Abstract ``ArtifactStore`` repository — Stream J.9.

Implementations:
- :class:`expert_work.persistence.artifact.memory.InMemoryArtifactStore`
- :class:`expert_work.persistence.artifact.sql.SqlArtifactStore`
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from expert_work.protocol import Artifact, ArtifactKind, ArtifactVersion


class ArtifactStore(abc.ABC):
    """Agent-artifact registry, scoped to ``(tenant_id, user_id)``.

    Lifecycle (Mini-ADR J-25): ``deleted_at IS NULL`` is the active
    state. :meth:`soft_delete` flips ``deleted_at`` on a per-name row
    (versions ride along); the retention sweep finds those past their
    horizon via :meth:`list_expired` and removes them via
    :meth:`hard_delete`. ``archived_object_key`` is reserved for the
    follow-up archive flow.
    """

    @abc.abstractmethod
    async def save_version(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        name: str,
        kind: ArtifactKind,
        path_in_workspace: str,
        created_in_thread: str,
    ) -> ArtifactVersion:
        """Register a new version of artifact ``name``.

        Creates the logical artifact at version 1 on first save, else
        appends the next version and bumps ``latest_version``. ``kind``
        is honoured only at creation — a later save never changes the
        kind of an existing artifact. Returns the new version row.

        Saving onto a soft-deleted name un-deletes it (clears
        ``deleted_at``) — a re-save is the user's explicit re-activation.
        """

    @abc.abstractmethod
    async def list_for_user(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        include_deleted: bool = False,
    ) -> list[Artifact]:
        """The user's logical artifacts, most-recently-updated first.

        Soft-deleted rows are hidden by default; ``include_deleted=True``
        returns them too (admin / audit use).
        """

    @abc.abstractmethod
    async def list_all_tenants(
        self,
        *,
        include_deleted: bool = False,
    ) -> list[Artifact]:
        """Cross-tenant artifact list — Stream N (Mini-ADR N-4).

        Caller MUST be inside ``bypass_rls_session()``. No
        ``tenant_id`` / ``user_id`` filter — the platform admin view
        aggregates every user's artifacts across every tenant.
        Most-recently-updated first.
        """

    @abc.abstractmethod
    async def get_latest_version(
        self, *, tenant_id: UUID, user_id: UUID, name: str
    ) -> ArtifactVersion | None:
        """Return the newest version of artifact ``name``, or ``None``.

        ``None`` when the user has no *active* artifact under that name
        — soft-deleted rows are hidden here too (callers turn that into
        404, identical to the cross-user case). Never reveals a
        cross-user artifact.
        """

    @abc.abstractmethod
    async def set_version_digest(self, *, version_id: UUID, size_bytes: int, sha256: str) -> None:
        """Backfill a version's ``size_bytes`` / ``sha256``.

        Called the first time the content is read — the digest is not
        known at ``save_version`` time (the content lives in the
        workspace volume, which the persistence layer cannot read).
        """

    @abc.abstractmethod
    async def soft_delete(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        name: str,
        now: datetime,
    ) -> bool:
        """Flip ``deleted_at`` on an active artifact; return ``True`` on hit.

        Returns ``False`` when the name is unknown for this user, or
        already soft-deleted (callers turn both into 404 — same
        hides-cross-user rule as :meth:`get_latest_version`). Idempotent:
        a second soft-delete on the same row is a no-op miss.
        """

    @abc.abstractmethod
    async def list_expired(
        self,
        *,
        before: datetime,
        limit: int = 1000,
    ) -> list[Artifact]:
        """Soft-deleted rows past the hard-delete horizon.

        ``before`` is ``now - hard_delete_grace`` — any row with
        ``deleted_at < before`` (and ``deleted_at IS NOT NULL``) is
        eligible to be hard-deleted. The retention sweep walks these
        in batches.
        """

    @abc.abstractmethod
    async def list_active_past_retention(
        self,
        *,
        before: datetime,
        limit: int = 1000,
    ) -> list[Artifact]:
        """Active rows whose ``updated_at < before`` — past their retention window.

        The retention sweep calls this with ``now - retention_days``
        and soft-deletes each returned row, kicking off the hard-delete
        countdown.
        """

    @abc.abstractmethod
    async def update_kind(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        name: str,
        kind: ArtifactKind,
    ) -> Artifact | None:
        """Mini-ADR J-25 — change the artifact's ``kind``.

        Returns the updated row on success. Returns ``None`` when the
        name is unknown / soft-deleted / cross-user (callers turn that
        into 404 — same hiding rule as :meth:`get_latest_version`).
        Idempotent: passing the current ``kind`` is a successful no-op
        that still returns the row.
        """

    @abc.abstractmethod
    async def list_versions(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        name: str,
    ) -> list[ArtifactVersion] | None:
        """Mini-ADR J-25 — every version of one logical artifact, newest first.

        Returns ``None`` when the parent artifact is unknown / soft-deleted
        / cross-user (callers turn that into 404 — same hiding rule).
        Returning ``[]`` would conflate "artifact exists with no
        versions" (impossible — versions are created on save) with
        "artifact doesn't exist"; ``None`` keeps the two distinguishable.
        """

    @abc.abstractmethod
    async def list_versions_expired(
        self,
        *,
        before: datetime,
        limit: int = 1000,
    ) -> list[ArtifactVersion]:
        """留存链 B-28 —— 跨租户列出 ``created_at < before`` 的版本行,最老在前。

        Caller MUST be inside an RLS bypass scope (the retention job's
        ``_bypass_rls``). 按版本而不是按逻辑产物判:同名产物有多个版本时,
        老版本先到期、新版本留着。父 ``artifact`` 行是否软删不影响 —— 版本
        行到期就是到期。
        """

    @abc.abstractmethod
    async def list_versions_by_artifact(self, *, artifact_id: UUID) -> list[ArtifactVersion]:
        """One artifact's every version row, newest first — **regardless of the
        parent row's ``deleted_at``**. :meth:`list_versions` hides soft-deleted
        parents (API 404 semantics); the retention sweep needs the paths of a
        soft-deleted artifact's versions to unlink the files before
        :meth:`hard_delete` drops the rows. Caller MUST be inside an RLS
        bypass scope. ``[]`` when the artifact is unknown."""

    @abc.abstractmethod
    async def delete_versions(self, *, version_ids: Sequence[UUID]) -> int:
        """留存链 B-28 —— 按 id 删版本行,返回删掉的行数。

        Only the version rows: the parent ``artifact`` row and its
        ``latest_version`` are left alone (see
        :meth:`mark_expired_if_versionless` for the parent's fate). Caller
        has already unlinked the workspace files. Idempotent — unknown ids
        are skipped."""

    @abc.abstractmethod
    async def mark_expired_if_versionless(self, *, artifact_id: UUID, now: datetime) -> bool:
        """留存链 B-28 —— 一个逻辑产物的版本全部清完后,把行标成过期。

        Sets ``deleted_at = now`` on an **active** row that has no version
        rows left; returns ``True`` on that transition. ``False`` when the
        row still has versions, is already soft-deleted, or is unknown.
        Reuses the J-25 soft-delete state on purpose: the row then follows
        the existing hard-delete grace path, and a re-save on the name
        un-deletes it exactly like a user-initiated soft delete."""

    @abc.abstractmethod
    async def hard_delete(self, *, artifact_ids: Sequence[UUID]) -> int:
        """Remove the named artifact rows + their version rows.

        Caller has already cleared the workspace files (the retention
        job unlinks every remaining version's file first, via
        :meth:`list_versions_by_artifact`).
        Returns the count of ``artifact`` rows actually deleted.
        Cascades deletion of the corresponding ``artifact_version`` rows.
        """

    @abc.abstractmethod
    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Phase 3a (purge_user) — hard-delete EVERY artifact for a user.

        Removes all of the user's ``artifact`` rows (active AND soft-deleted)
        and their ``artifact_version`` rows in one shot; returns the count of
        ``artifact`` rows deleted. Tenant- AND user-scoped — the
        ``(tenant_id, user_id)`` predicate never touches another tenant's or
        user's artifacts. Workspace file bytes are removed separately by the
        workspace soft-delete + archive path."""
