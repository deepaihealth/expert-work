"""In-memory ``ArtifactStore`` for unit tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from expert_work.persistence.artifact.base import ArtifactStore
from expert_work.protocol import Artifact, ArtifactKind, ArtifactVersion

#: Aware sentinel so artifacts with no ``updated_at`` sort last without
#: a naive-vs-aware datetime comparison error.
_MIN_AWARE = datetime.min.replace(tzinfo=UTC)


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
            id=uuid4(),
            artifact_id=artifact.id,
            tenant_id=tenant_id,
            user_id=user_id,
            version=artifact.latest_version,
            path_in_workspace=path_in_workspace,
            created_in_thread=created_in_thread,
            created_at=now,
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

    async def list_active_past_retention(
        self,
        *,
        before: datetime,
        limit: int = 1000,
    ) -> list[Artifact]:
        rows = [
            a
            for a in self._artifacts.values()
            if a.deleted_at is None and (a.updated_at or _MIN_AWARE) < before
        ]
        rows.sort(key=lambda a: a.updated_at or _MIN_AWARE)
        return rows[:limit]

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
        before: datetime,
        limit: int = 1000,
    ) -> list[ArtifactVersion]:
        rows = [v for v in self._versions if v.created_at is not None and v.created_at < before]
        # Same tiebreak as the SQL store: ``created_at, artifact_id, version``.
        rows.sort(key=lambda v: (v.created_at or _MIN_AWARE, str(v.artifact_id), v.version))
        return rows[:limit]

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
