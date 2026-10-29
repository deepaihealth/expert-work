"""SQLAlchemy-backed ``ArtifactStore`` (Postgres / asyncpg)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.artifact.base import ArtifactStore
from expert_work.persistence.models import ArtifactRow, ArtifactVersionRow
from expert_work.persistence.rls import require_rls_bypass
from expert_work.protocol import Artifact, ArtifactKind, ArtifactVersion


def _row_to_artifact(row: ArtifactRow) -> Artifact:
    return Artifact(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        agent_key=row.agent_key,
        name=row.name,
        kind=row.kind,  # type: ignore[arg-type]
        latest_version=row.latest_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
        archived_object_key=row.archived_object_key,
    )


def _row_to_version(row: ArtifactVersionRow) -> ArtifactVersion:
    return ArtifactVersion(
        id=row.id,
        artifact_id=row.artifact_id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        version=row.version,
        path_in_workspace=row.path_in_workspace,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        created_in_thread=row.created_in_thread,
        created_at=row.created_at,
        snapshot_key=row.snapshot_key,
    )


class SqlArtifactStore(ArtifactStore):
    """Postgres-backed agent-artifact registry."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

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
        # INSERT ... ON CONFLICT DO UPDATE — a race-free upsert of the
        # logical artifact. On first save ``latest_version`` is 1; a
        # repeat save bumps it. ``kind`` is left out of the conflict
        # SET, so an existing artifact keeps its original kind. Mini-ADR
        # J-25: a re-save on a soft-deleted name un-deletes it.
        insert_artifact = pg_insert(ArtifactRow).values(
            tenant_id=tenant_id,
            user_id=user_id,
            agent_key=agent_key,
            name=name,
            kind=kind,
            latest_version=1,
            created_at=now,
            updated_at=now,
        )
        upsert = insert_artifact.on_conflict_do_update(
            constraint="artifact_identity_uniq",
            set_={
                "latest_version": ArtifactRow.latest_version + 1,
                "updated_at": now,
                "deleted_at": None,
            },
        ).returning(ArtifactRow.id, ArtifactRow.latest_version)

        version_id = version_id or uuid4()
        async with self._sf() as session:
            artifact_id, version = (await session.execute(upsert)).one()
            session.add(
                ArtifactVersionRow(
                    id=version_id,
                    artifact_id=artifact_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    version=version,
                    path_in_workspace=path_in_workspace,
                    size_bytes=size_bytes,
                    sha256=sha256,
                    created_in_thread=created_in_thread,
                    created_at=now,
                    snapshot_key=snapshot_key,
                )
            )
            await session.commit()

        return ArtifactVersion(
            id=version_id,
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            user_id=user_id,
            version=version,
            path_in_workspace=path_in_workspace,
            size_bytes=size_bytes,
            sha256=sha256,
            created_in_thread=created_in_thread,
            created_at=now,
            snapshot_key=snapshot_key,
        )

    async def list_for_user(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        agent_key: str | None,
        include_deleted: bool = False,
    ) -> list[Artifact]:
        stmt = (
            select(ArtifactRow)
            .where(ArtifactRow.tenant_id == tenant_id, ArtifactRow.user_id == user_id)
            .order_by(ArtifactRow.updated_at.desc())
        )
        # ``None`` = 不按 agent 过滤。与内存档的谓词必须同义。
        if agent_key is not None:
            stmt = stmt.where(ArtifactRow.agent_key == agent_key)
        if not include_deleted:
            stmt = stmt.where(ArtifactRow.deleted_at.is_(None))
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_artifact(row) for row in rows]

    async def list_all_tenants(
        self,
        *,
        include_deleted: bool = False,
    ) -> list[Artifact]:
        # Stream N — no tenant / user filter; caller must wrap in bypass_rls_session().
        stmt = select(ArtifactRow).order_by(ArtifactRow.updated_at.desc())
        if not include_deleted:
            stmt = stmt.where(ArtifactRow.deleted_at.is_(None))
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_artifact(row) for row in rows]

    async def get_latest_version(
        self, *, tenant_id: UUID, user_id: UUID, agent_key: str | None, name: str
    ) -> ArtifactVersion | None:
        # ``agent_key=None`` 时同名可能多行 —— 必须 ORDER BY + LIMIT 1,
        # 原来的 ``scalar_one_or_none()`` 在多行时是**抛异常**不是返回 None。
        stmt = (
            select(ArtifactRow)
            .where(
                ArtifactRow.tenant_id == tenant_id,
                ArtifactRow.user_id == user_id,
                ArtifactRow.name == name,
                ArtifactRow.deleted_at.is_(None),
            )
            .order_by(ArtifactRow.updated_at.desc())
            .limit(1)
        )
        if agent_key is not None:
            stmt = stmt.where(ArtifactRow.agent_key == agent_key)
        async with self._sf() as session:
            artifact = (await session.execute(stmt)).scalar_one_or_none()
            if artifact is None:
                return None
            return await self._latest_of(session, artifact)

    async def get_latest_version_by_id(
        self, *, tenant_id: UUID, user_id: UUID, artifact_id: UUID
    ) -> ArtifactVersion | None:
        async with self._sf() as session:
            artifact = (
                await session.execute(
                    select(ArtifactRow).where(
                        ArtifactRow.id == artifact_id,
                        ArtifactRow.tenant_id == tenant_id,
                        ArtifactRow.user_id == user_id,
                        ArtifactRow.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if artifact is None:
                return None
            return await self._latest_of(session, artifact)

    async def get_version_by_id(
        self, *, tenant_id: UUID, user_id: UUID, artifact_id: UUID, version: int
    ) -> ArtifactVersion | None:
        async with self._sf() as session:
            artifact = (
                await session.execute(
                    select(ArtifactRow.id).where(
                        ArtifactRow.id == artifact_id,
                        ArtifactRow.tenant_id == tenant_id,
                        ArtifactRow.user_id == user_id,
                        ArtifactRow.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if artifact is None:
                return None
            return await self._version_of(session, artifact, version)

    @staticmethod
    async def _version_of(
        session: AsyncSession, artifact_id: UUID, version: int
    ) -> ArtifactVersion | None:
        row = (
            await session.execute(
                select(ArtifactVersionRow).where(
                    ArtifactVersionRow.artifact_id == artifact_id,
                    ArtifactVersionRow.version == version,
                )
            )
        ).scalar_one_or_none()
        return _row_to_version(row) if row is not None else None

    async def paths_still_registered(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        paths: Sequence[str],
        excluding_version_ids: Sequence[UUID],
    ) -> set[str]:
        # 强制 RLS 后没 bypass 的会话拿到空集 =「没人在用」→ 删掉别的版本还在用的文件。
        require_rls_bypass("ArtifactStore.paths_still_registered")
        if not paths:
            return set()
        stmt = select(ArtifactVersionRow.path_in_workspace).where(
            ArtifactVersionRow.tenant_id == tenant_id,
            ArtifactVersionRow.user_id == user_id,
            ArtifactVersionRow.path_in_workspace.in_(list(paths)),
        )
        if excluding_version_ids:
            stmt = stmt.where(ArtifactVersionRow.id.not_in(list(excluding_version_ids)))
        async with self._sf() as session:
            rows = (await session.execute(stmt.distinct())).scalars().all()
        return set(rows)

    async def bypasses_row_security(self) -> bool:
        async with self._sf() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT rolbypassrls OR rolsuper FROM pg_roles WHERE rolname = current_user"
                    )
                )
            ).scalar_one_or_none()
        return bool(row)

    async def existing_version_ids(self, ids: Sequence[UUID]) -> set[UUID]:
        # 强制 RLS 后没 bypass 的会话拿到空集 = 所有副本都是孤儿 → 全删。
        require_rls_bypass("ArtifactStore.existing_version_ids")
        if not ids:
            return set()
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(ArtifactVersionRow.id).where(ArtifactVersionRow.id.in_(list(ids)))
                    )
                )
                .scalars()
                .all()
            )
        return set(rows)

    @staticmethod
    async def _latest_of(session: AsyncSession, artifact: ArtifactRow) -> ArtifactVersion | None:
        row = (
            await session.execute(
                select(ArtifactVersionRow).where(
                    ArtifactVersionRow.artifact_id == artifact.id,
                    ArtifactVersionRow.version == artifact.latest_version,
                )
            )
        ).scalar_one_or_none()
        return _row_to_version(row) if row is not None else None

    async def set_version_digest(self, *, version_id: UUID, size_bytes: int, sha256: str) -> None:
        async with self._sf() as session:
            await session.execute(
                update(ArtifactVersionRow)
                .where(ArtifactVersionRow.id == version_id)
                .values(size_bytes=size_bytes, sha256=sha256)
            )
            await session.commit()

    async def soft_delete(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        agent_key: str | None,
        name: str,
        now: datetime,
    ) -> bool:
        # ``agent_key=None`` 时同名可能多行。原来是无 LIMIT 的批量 UPDATE ——
        # 那样会把两个 agent 的同名产物一起删掉。先定位到唯一一行再改。
        target = (
            select(ArtifactRow.id)
            .where(
                ArtifactRow.tenant_id == tenant_id,
                ArtifactRow.user_id == user_id,
                ArtifactRow.name == name,
                ArtifactRow.deleted_at.is_(None),
            )
            .order_by(ArtifactRow.updated_at.desc())
            .limit(1)
        )
        if agent_key is not None:
            target = target.where(ArtifactRow.agent_key == agent_key)
        async with self._sf() as session:
            result = await session.execute(
                update(ArtifactRow)
                .where(ArtifactRow.id.in_(target.scalar_subquery()))
                .values(deleted_at=now)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def soft_delete_by_id(
        self, *, tenant_id: UUID, user_id: UUID, artifact_id: UUID, now: datetime
    ) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                update(ArtifactRow)
                .where(
                    ArtifactRow.id == artifact_id,
                    ArtifactRow.tenant_id == tenant_id,
                    ArtifactRow.user_id == user_id,
                    ArtifactRow.deleted_at.is_(None),
                )
                .values(deleted_at=now)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def list_expired(
        self,
        *,
        before: datetime,
        limit: int = 1000,
    ) -> list[Artifact]:
        stmt = (
            select(ArtifactRow)
            .where(ArtifactRow.deleted_at.is_not(None), ArtifactRow.deleted_at < before)
            .order_by(ArtifactRow.deleted_at.asc())
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_artifact(row) for row in rows]

    async def list_active_past_retention(
        self,
        *,
        before: datetime,
        limit: int = 1000,
    ) -> list[Artifact]:
        stmt = (
            select(ArtifactRow)
            .where(ArtifactRow.deleted_at.is_(None), ArtifactRow.updated_at < before)
            .order_by(ArtifactRow.updated_at.asc())
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_artifact(row) for row in rows]

    async def update_kind(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        artifact_id: UUID,
        kind: ArtifactKind,
    ) -> Artifact | None:
        now = datetime.now(UTC)
        async with self._sf() as session:
            result = await session.execute(
                update(ArtifactRow)
                .where(
                    ArtifactRow.id == artifact_id,
                    ArtifactRow.tenant_id == tenant_id,
                    ArtifactRow.user_id == user_id,
                    ArtifactRow.deleted_at.is_(None),
                )
                .values(kind=kind, updated_at=now)
                .returning(ArtifactRow)
            )
            row = result.scalar_one_or_none()
            if row is None:
                await session.commit()
                return None
            await session.commit()
        return _row_to_artifact(row)

    async def list_versions(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        artifact_id: UUID,
    ) -> list[ArtifactVersion] | None:
        async with self._sf() as session:
            artifact = (
                await session.execute(
                    select(ArtifactRow).where(
                        ArtifactRow.id == artifact_id,
                        ArtifactRow.tenant_id == tenant_id,
                        ArtifactRow.user_id == user_id,
                        ArtifactRow.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if artifact is None:
                return None
            rows = (
                (
                    await session.execute(
                        select(ArtifactVersionRow)
                        .where(ArtifactVersionRow.artifact_id == artifact.id)
                        .order_by(ArtifactVersionRow.version.desc())
                    )
                )
                .scalars()
                .all()
            )
        return [_row_to_version(row) for row in rows]

    async def list_versions_expired(
        self,
        *,
        before: datetime,
        limit: int = 1000,
    ) -> list[ArtifactVersion]:
        # 同一 ``created_at`` 的版本(同一事务里连登记几版、时钟粒度内的批量)按
        # ``artifact_id, version`` 定序 —— 老版本先于新版本,跨 store 实现同义;
        # 用 ``id``(uuid4)做 tiebreak 则同一产物两个版本谁先谁后是随机的
        # (CI 实测:三跑两红)。
        stmt = (
            select(ArtifactVersionRow)
            .where(ArtifactVersionRow.created_at < before)
            .order_by(
                ArtifactVersionRow.created_at.asc(),
                ArtifactVersionRow.artifact_id.asc(),
                ArtifactVersionRow.version.asc(),
            )
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_version(row) for row in rows]

    async def list_versions_by_artifact(self, *, artifact_id: UUID) -> list[ArtifactVersion]:
        stmt = (
            select(ArtifactVersionRow)
            .where(ArtifactVersionRow.artifact_id == artifact_id)
            .order_by(ArtifactVersionRow.version.desc())
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_version(row) for row in rows]

    async def delete_versions(self, *, version_ids: Sequence[UUID]) -> int:
        if not version_ids:
            return 0
        async with self._sf() as session:
            result = await session.execute(
                delete(ArtifactVersionRow).where(ArtifactVersionRow.id.in_(list(version_ids)))
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def mark_expired_if_versionless(self, *, artifact_id: UUID, now: datetime) -> bool:
        remaining = (
            select(ArtifactVersionRow.id)
            .where(ArtifactVersionRow.artifact_id == artifact_id)
            .exists()
        )
        async with self._sf() as session:
            result = await session.execute(
                update(ArtifactRow)
                .where(
                    ArtifactRow.id == artifact_id,
                    ArtifactRow.deleted_at.is_(None),
                    ~remaining,
                )
                .values(deleted_at=now)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

    async def hard_delete(self, *, artifact_ids: Sequence[UUID]) -> int:
        if not artifact_ids:
            return 0
        ids = list(artifact_ids)
        async with self._sf() as session:
            # Versions first — no FK ON DELETE CASCADE because
            # ``artifact_id`` is a bare UUID column (FORCE-RLS footgun,
            # Mini-ADR J-1a).
            await session.execute(
                delete(ArtifactVersionRow).where(ArtifactVersionRow.artifact_id.in_(ids))
            )
            result = await session.execute(delete(ArtifactRow).where(ArtifactRow.id.in_(ids)))
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        async with self._sf() as session:
            # Versions first — no FK cascade (``artifact_id`` is a bare UUID
            # column, FORCE-RLS footgun J-1a). Both tables denormalise
            # ``tenant_id`` / ``user_id``, so each delete is scoped directly.
            await session.execute(
                delete(ArtifactVersionRow).where(
                    ArtifactVersionRow.tenant_id == tenant_id,
                    ArtifactVersionRow.user_id == user_id,
                )
            )
            result = await session.execute(
                delete(ArtifactRow).where(
                    ArtifactRow.tenant_id == tenant_id,
                    ArtifactRow.user_id == user_id,
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)
