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
    """Agent-artifact registry, scoped to ``(tenant_id, user_id, agent_key)``.

    B-50 —— ``agent_key`` 进身份键之前,同一用户下两个 agent 存同名产物会合并
    成一行、旧字节被覆盖。现在它们是两条独立行,各自版本序列。

    **``agent_key`` 一律必传,没有默认值。** 传 ``None`` 表示「不按 agent 过滤」
    (控制台全量视图、留存 job 的运维视角、以及对外端点在 PR4 收口之前的迁移期)。
    不给默认值是刻意的:新调用方必须**明确写出来**自己要不要按 agent 过滤,
    而不是继承一个静默的「看全部」。谁可以传 ``None`` 有登记表盯着,见
    ``control-plane/tests/test_artifact_agent_scope_callers.py``。

    按 **name** 寻址的方法都带 ``agent_key`` —— 四元组键下 name 不再是身份。
    控制台浏览全量、手里只有列表行,所以它走 ``*_by_id`` 那组(``artifact_id``
    永远无歧义);``update_kind`` / ``list_versions`` 只有控制台在用,直接就是
    按 id 的。

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
        agent_key: str,
        name: str,
        kind: ArtifactKind,
        path_in_workspace: str,
        created_in_thread: str,
    ) -> ArtifactVersion:
        """Register a new version of ``(agent_key, name)``.

        ``agent_key`` 空串 = 没绑 agent 的调用方(合成执行路径)。它与任何
        非空 key 都是不同的身份,不会跟谁合并。

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
        agent_key: str | None,
        include_deleted: bool = False,
    ) -> list[Artifact]:
        """The user's logical artifacts, most-recently-updated first.

        ``agent_key`` 给值则只列该 agent 的;``None`` 不过滤(全量)。

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
        self, *, tenant_id: UUID, user_id: UUID, agent_key: str | None, name: str
    ) -> ArtifactVersion | None:
        """Return the newest version of ``(agent_key, name)``, or ``None``.

        ``agent_key=None`` 不按 agent 过滤 —— 同名多行时取 ``updated_at``
        最新的那条。只有对外端点在 PR4 收口之前这么用;控制台走
        :meth:`get_latest_version_by_id`。

        ``None`` when the user has no *active* artifact under that name
        — soft-deleted rows are hidden here too (callers turn that into
        404, identical to the cross-user case). Never reveals a
        cross-user artifact.
        """

    @abc.abstractmethod
    async def get_latest_version_by_id(
        self, *, tenant_id: UUID, user_id: UUID, artifact_id: UUID
    ) -> ArtifactVersion | None:
        """B-50 —— 按 ``artifact_id`` 取最新版本,给控制台全量视图用。

        四元组键之后 name 不再是身份,而控制台是跨 agent 浏览的:它手里有
        列表行(带 id),用 id 寻址永远无歧义。仍受 ``(tenant_id, user_id)``
        约束 —— id 是 UUID 不等于可以跨用户取。``None`` = 未知 / 已软删 /
        跨用户(调用方一律转 404,与 :meth:`get_latest_version` 同一套隐藏规则)。
        """

    @abc.abstractmethod
    async def soft_delete_by_id(
        self, *, tenant_id: UUID, user_id: UUID, artifact_id: UUID, now: datetime
    ) -> bool:
        """B-50 —— 按 ``artifact_id`` 软删,给控制台全量视图用。

        理由与返回语义同 :meth:`get_latest_version_by_id`;幂等性同
        :meth:`soft_delete`(第二次是 no-op miss)。
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
        agent_key: str | None,
        name: str,
        now: datetime,
    ) -> bool:
        """Flip ``deleted_at`` on an active artifact; return ``True`` on hit.

        ``agent_key=None`` 不按 agent 过滤 —— 同名多行时删 ``updated_at``
        最新的那条。同 :meth:`get_latest_version` 的口径。

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
        artifact_id: UUID,
        kind: ArtifactKind,
    ) -> Artifact | None:
        """Mini-ADR J-25 — change the artifact's ``kind``.

        B-50 —— 按 ``artifact_id`` 而不是 name:四元组键下 name 不是身份,
        而这个方法只有控制台全量视图在用,它手里就是列表行。

        Returns the updated row on success. Returns ``None`` when the
        id is unknown / soft-deleted / cross-user (callers turn that
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
        artifact_id: UUID,
    ) -> list[ArtifactVersion] | None:
        """Mini-ADR J-25 — every version of one logical artifact, newest first.

        B-50 —— 按 ``artifact_id`` 而不是 name,理由同 :meth:`update_kind`。

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
