"""In-memory ``ArtifactStore`` for unit tests."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from expert_work.persistence.artifact.base import ArtifactStore, ExpiredArtifactVersion
from expert_work.persistence.user_upload.base import TOUCH_INTERVAL
from expert_work.protocol import Artifact, ArtifactKind, ArtifactVersion

#: Aware sentinel so artifacts with no ``updated_at`` sort last without
#: a naive-vs-aware datetime comparison error.
_MIN_AWARE = datetime.min.replace(tzinfo=UTC)


def _version_clock(v: ArtifactVersion) -> datetime:
    """B-63 —— 与 SQL 实现同一个时钟,但内存里没有 run:``created_at`` / ``last_used_at``
    最晚的那个(会话最后一次 run、租户保留期由 SQL 集成测试覆盖)。"""
    return max(t for t in (v.created_at or _MIN_AWARE, v.last_used_at) if t is not None)


class InMemoryArtifactStore(ArtifactStore):
    def __init__(self) -> None:
        self._artifacts: dict[UUID, Artifact] = {}
        self._versions: list[ArtifactVersion] = []

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
        version_id: UUID | None = None,
        snapshot_key: str | None = None,
        size_bytes: int | None = None,
        sha256: str | None = None,
    ) -> ArtifactVersion:
        now = datetime.now(UTC)
        existing = next(
            (
                a
                for a in self._artifacts.values()
                if a.tenant_id == tenant_id
                and a.user_id == user_id
                and a.agent_key == agent_key
                and a.name == name
            ),
            None,
        )
        if existing is None:
            artifact = Artifact(
                id=uuid4(),
                tenant_id=tenant_id,
                user_id=user_id,
                agent_key=agent_key,
                name=name,
                kind=kind,
                latest_version=1,
                created_at=now,
                updated_at=now,
            )
        else:
            # Re-save on a soft-deleted name un-deletes it (Mini-ADR J-25).
            artifact = existing.model_copy(
                update={
                    "latest_version": existing.latest_version + 1,
                    "updated_at": now,
                    "deleted_at": None,
                }
            )
        self._artifacts[artifact.id] = artifact

        version = ArtifactVersion(
            id=version_id or uuid4(),
            artifact_id=artifact.id,
            tenant_id=tenant_id,
            user_id=user_id,
            version=artifact.latest_version,
            path_in_workspace=path_in_workspace,
            size_bytes=size_bytes,
            sha256=sha256,
            created_in_thread=created_in_thread,
            created_at=now,
            snapshot_key=snapshot_key,
        )
        self._versions.append(version)
        return version

    async def list_for_user(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        agent_key: str | None,
        include_deleted: bool = False,
    ) -> list[Artifact]:
        rows = [
            a
            for a in self._artifacts.values()
            if a.tenant_id == tenant_id
            and a.user_id == user_id
            # ``None`` = 不按 agent 过滤。与 SQL 档的谓词必须同义。
            and (agent_key is None or a.agent_key == agent_key)
            and (include_deleted or a.deleted_at is None)
        ]
        rows.sort(key=lambda a: a.updated_at or _MIN_AWARE, reverse=True)
        return rows

    async def list_all_tenants(
        self,
        *,
        include_deleted: bool = False,
    ) -> list[Artifact]:
        rows = [a for a in self._artifacts.values() if include_deleted or a.deleted_at is None]
        rows.sort(key=lambda a: a.updated_at or _MIN_AWARE, reverse=True)
        return rows

    async def get_latest_version(
        self, *, tenant_id: UUID, user_id: UUID, agent_key: str | None, name: str
    ) -> ArtifactVersion | None:
        # ``agent_key=None`` 时同名可能多行 —— 取 updated_at 最新的那条,与
        # SQL 档的 ``ORDER BY updated_at DESC LIMIT 1`` 同义。
        candidates = sorted(
            (
                a
                for a in self._artifacts.values()
                if a.tenant_id == tenant_id
                and a.user_id == user_id
                and (agent_key is None or a.agent_key == agent_key)
                and a.name == name
                and a.deleted_at is None
            ),
            key=lambda a: a.updated_at or _MIN_AWARE,
            reverse=True,
        )
        if not candidates:
            return None
        return self._latest_of(candidates[0])

    async def get_latest_version_by_id(
        self, *, tenant_id: UUID, user_id: UUID, artifact_id: UUID
    ) -> ArtifactVersion | None:
        artifact = self._artifacts.get(artifact_id)
        if (
            artifact is None
            or artifact.tenant_id != tenant_id
            or artifact.user_id != user_id
            or artifact.deleted_at is not None
        ):
            return None
        return self._latest_of(artifact)

    async def get_version_by_id(
        self, *, tenant_id: UUID, user_id: UUID, artifact_id: UUID, version: int
    ) -> ArtifactVersion | None:
        artifact = self._artifacts.get(artifact_id)
        if (
            artifact is None
            or artifact.tenant_id != tenant_id
            or artifact.user_id != user_id
            or artifact.deleted_at is not None
        ):
            return None
        return self._version_of(artifact, version)

    def _version_of(self, artifact: Artifact, version: int) -> ArtifactVersion | None:
        return next(
            (v for v in self._versions if v.artifact_id == artifact.id and v.version == version),
            None,
        )

    async def paths_still_registered(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        paths: Sequence[str],
        excluding_version_ids: Sequence[UUID],
    ) -> set[str]:
        wanted = set(paths)
        excluded = set(excluding_version_ids)
        return {
            v.path_in_workspace
            for v in self._versions
            if v.tenant_id == tenant_id
            and v.user_id == user_id
            and v.path_in_workspace in wanted
            and v.id not in excluded
        }

    async def bypasses_row_security(self) -> bool:
        return True  # 内存档没有 RLS

    async def existing_version_ids(self, ids: Sequence[UUID]) -> set[UUID]:
        wanted = set(ids)
        return {v.id for v in self._versions if v.id in wanted}

    def _latest_of(self, artifact: Artifact) -> ArtifactVersion | None:
        return next(
            (
                v
                for v in self._versions
                if v.artifact_id == artifact.id and v.version == artifact.latest_version
            ),
            None,
        )

    async def set_version_digest(self, *, version_id: UUID, size_bytes: int, sha256: str) -> None:
        self._versions = [
            v.model_copy(update={"size_bytes": size_bytes, "sha256": sha256})
            if v.id == version_id
            else v
            for v in self._versions
        ]

    async def soft_delete(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        agent_key: str | None,
        name: str,
        now: datetime,
    ) -> bool:
        # 同 get_latest_version:``agent_key=None`` 时取 updated_at 最新的那条。
        candidates = sorted(
            (
                a
                for a in self._artifacts.values()
                if a.tenant_id == tenant_id
                and a.user_id == user_id
                and (agent_key is None or a.agent_key == agent_key)
                and a.name == name
                and a.deleted_at is None
            ),
            key=lambda a: a.updated_at or _MIN_AWARE,
            reverse=True,
        )
        if not candidates:
            return False
        self._artifacts[candidates[0].id] = candidates[0].model_copy(update={"deleted_at": now})
        return True

    async def soft_delete_by_id(
        self, *, tenant_id: UUID, user_id: UUID, artifact_id: UUID, now: datetime
    ) -> bool:
        artifact = self._artifacts.get(artifact_id)
        if (
            artifact is None
            or artifact.tenant_id != tenant_id
            or artifact.user_id != user_id
            or artifact.deleted_at is not None
        ):
            return False
        self._artifacts[artifact_id] = artifact.model_copy(update={"deleted_at": now})
        return True

    async def list_expired(
        self,
        *,
        before: datetime,
        limit: int = 1000,
    ) -> list[Artifact]:
        rows = [
            a
            for a in self._artifacts.values()
            if a.deleted_at is not None and a.deleted_at < before
        ]
        rows.sort(key=lambda a: a.deleted_at or _MIN_AWARE)
        return rows[:limit]

    def _artifact_clock(self, artifact: Artifact) -> datetime:
        clocks = [_version_clock(v) for v in self._versions if v.artifact_id == artifact.id]
        return max(clocks) if clocks else (artifact.updated_at or _MIN_AWARE)

    async def list_active_past_retention(
        self,
        *,
        now: datetime,
        default_days: int,
        limit: int = 1000,
    ) -> list[Artifact]:
        window = timedelta(days=default_days)
        due = [
            (self._artifact_clock(a) + window, a)
            for a in self._artifacts.values()
            if a.deleted_at is None
        ]
        rows = sorted(((at, a) for at, a in due if at < now), key=lambda p: (p[0], str(p[1].id)))
        return [a for _, a in rows[:limit]]

    async def update_kind(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        artifact_id: UUID,
        kind: ArtifactKind,
    ) -> Artifact | None:
        artifact = self._artifacts.get(artifact_id)
        if (
            artifact is None
            or artifact.tenant_id != tenant_id
            or artifact.user_id != user_id
            or artifact.deleted_at is not None
        ):
            return None
        updated = artifact.model_copy(update={"kind": kind, "updated_at": datetime.now(UTC)})
        self._artifacts[artifact_id] = updated
        return updated

    async def list_versions(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        artifact_id: UUID,
    ) -> list[ArtifactVersion] | None:
        artifact = self._artifacts.get(artifact_id)
        if (
            artifact is None
            or artifact.tenant_id != tenant_id
            or artifact.user_id != user_id
            or artifact.deleted_at is not None
        ):
            return None
        rows = [v for v in self._versions if v.artifact_id == artifact.id]
        rows.sort(key=lambda v: v.version, reverse=True)
        return rows

    async def list_versions_expired(
        self,
        *,
        now: datetime,
        default_days: int,
        limit: int = 1000,
    ) -> list[ExpiredArtifactVersion]:
        window = timedelta(days=default_days)
        due: list[tuple[datetime, ArtifactVersion]] = []
        for v in self._versions:
            parent = self._artifacts.get(v.artifact_id)
            # 最新版用产物的时钟(各版本里最晚的),同 SQL 实现。
            if parent is not None and v.version == parent.latest_version:
                clock = self._artifact_clock(parent)
            else:
                clock = _version_clock(v)
            if clock + window < now:
                due.append((clock + window, v))
        # Same tiebreak as the SQL store: ``expires_at, artifact_id, version``.
        due.sort(key=lambda p: (p[0], str(p[1].artifact_id), p[1].version))
        return [
            ExpiredArtifactVersion(version=v, retention_days=default_days) for _, v in due[:limit]
        ]

    async def touch_version(self, *, version_id: UUID, tenant_id: UUID, now: datetime) -> bool:
        for index, v in enumerate(self._versions):
            if v.id != version_id or v.tenant_id != tenant_id:
                continue
            if v.last_used_at is not None and v.last_used_at >= now - TOUCH_INTERVAL:
                return False
            self._versions[index] = v.model_copy(update={"last_used_at": now})
            return True
        return False

    async def list_versions_by_artifact(self, *, artifact_id: UUID) -> list[ArtifactVersion]:
        rows = [v for v in self._versions if v.artifact_id == artifact_id]
        rows.sort(key=lambda v: v.version, reverse=True)
        return rows

    async def delete_versions(self, *, version_ids: Sequence[UUID]) -> int:
        ids = set(version_ids)
        before = len(self._versions)
        self._versions = [v for v in self._versions if v.id not in ids]
        return before - len(self._versions)

    async def mark_expired_if_versionless(self, *, artifact_id: UUID, now: datetime) -> bool:
        artifact = self._artifacts.get(artifact_id)
        if artifact is None or artifact.deleted_at is not None:
            return False
        if any(v.artifact_id == artifact_id for v in self._versions):
            return False
        self._artifacts[artifact_id] = artifact.model_copy(update={"deleted_at": now})
        return True

    async def hard_delete(self, *, artifact_ids: Sequence[UUID]) -> int:
        ids = set(artifact_ids)
        removed = 0
        for aid in list(self._artifacts):
            if aid in ids:
                del self._artifacts[aid]
                removed += 1
        self._versions = [v for v in self._versions if v.artifact_id not in ids]
        return removed

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        victim_ids = {
            aid
            for aid, a in self._artifacts.items()
            if a.tenant_id == tenant_id and a.user_id == user_id
        }
        for aid in victim_ids:
            del self._artifacts[aid]
        self._versions = [
            v for v in self._versions if not (v.tenant_id == tenant_id and v.user_id == user_id)
        ]
        return len(victim_ids)

    def _agent_ids(self, tenant_id: UUID, agent_key: str) -> set[UUID]:
        if not agent_key:
            raise ValueError("agent_key must be non-empty")
        return {
            aid
            for aid, a in self._artifacts.items()
            if a.tenant_id == tenant_id and a.agent_key == agent_key
        }

    async def list_versions_for_agent(
        self, *, tenant_id: UUID, agent_key: str
    ) -> list[ArtifactVersion]:
        ids = self._agent_ids(tenant_id, agent_key)
        return [v for v in self._versions if v.tenant_id == tenant_id and v.artifact_id in ids]

    async def count_for_agent(self, *, tenant_id: UUID, agent_key: str) -> int:
        return len(self._agent_ids(tenant_id, agent_key))

    async def delete_all_for_agent(
        self, *, tenant_id: UUID, agent_key: str, keep_users: Collection[UUID] = ()
    ) -> int:
        keep = set(keep_users)
        ids = {
            aid
            for aid in self._agent_ids(tenant_id, agent_key)
            if self._artifacts[aid].user_id not in keep
        }
        self._versions = [
            v for v in self._versions if not (v.tenant_id == tenant_id and v.artifact_id in ids)
        ]
        for aid in ids:
            del self._artifacts[aid]
        return len(ids)
