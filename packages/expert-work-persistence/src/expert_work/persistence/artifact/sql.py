"""SQLAlchemy-backed ``ArtifactStore`` (Postgres / asyncpg)."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CTE, ColumnElement, and_, case, cast, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.artifact.base import ArtifactStore, ExpiredArtifactVersion
from expert_work.persistence.bulk_delete import count_where
from expert_work.persistence.models import (
    AgentRunRow,
    ArtifactRow,
    ArtifactVersionRow,
    TenantConfigRow,
)
from expert_work.persistence.rls import require_rls_bypass
from expert_work.persistence.user_upload.base import TOUCH_INTERVAL
from expert_work.protocol import Artifact, ArtifactKind, ArtifactVersion

#: ``created_in_thread`` 里长得像 UUID 的才当 run id(见 :func:`_clocks`)。反过来把
#: ``agent_run.id`` 转成 text 去比用不上主键索引。
_UUID_PATTERN = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def _clocks() -> tuple[CTE, CTE]:
    """B-63 —— 每个版本与每个产物的「最后使用」,用 CTE **一次**算完(不再每行一个相关子查询)。

    * 一版自己的 = ``greatest(created_at, last_used_at, 登记它的会话最后一次 run)``。
      ``created_in_thread`` 存的是 run id(TEXT;没有 run 时是 ``"save-artifact"`` 之类的回落
      常量),只有长得像 UUID 的才 cast 去按主键找那个 run —— cast 回落常量会让整条查询报错
      (迁移 0154 踩过);run → 会话、会话 → 最后一次 run 都只认**同租户**的 run。会话的最后
      一次 run 先收拢出涉及到的会话,再对这些会话的 ``agent_run`` 聚合一遍。
    * 产物的 = 它各版本时钟里最晚的那个。

    返回 ``(version_clock, artifact_clock)``:前者每版一行 ``(id, artifact_id, tenant_id,
    version, clock)``,后者每个有版本的产物一行 ``(artifact_id, clock)``。
    """
    v = ArtifactVersionRow
    run_id = case(
        (v.created_in_thread.regexp_match(_UUID_PATTERN), cast(v.created_in_thread, PG_UUID)),
        else_=None,
    )
    versions = select(
        v.id,
        v.artifact_id,
        v.tenant_id,
        v.version,
        v.created_at,
        v.last_used_at,
        run_id.label("run_id"),
    ).cte("artifact_version_runs")
    source = (
        select(AgentRunRow.id.label("run_id"), AgentRunRow.tenant_id, AgentRunRow.thread_id)
        .join(
            versions,
            and_(
                AgentRunRow.id == versions.c.run_id, AgentRunRow.tenant_id == versions.c.tenant_id
            ),
        )
        .distinct()
        .cte("artifact_source_runs")
    )
    # 租户条件在 run 这一跳与下面 join 回版本时各有一份,是纵深防御:任何一份单独都足以挡住
    # 别的租户的 run(去掉其中一份测试仍绿,两份都去掉才红)。
    threads = select(source.c.tenant_id, source.c.thread_id).distinct().cte("artifact_threads")
    last_run = (
        select(
            AgentRunRow.tenant_id,
            AgentRunRow.thread_id,
            func.max(AgentRunRow.created_at).label("at"),
        )
        .join(
            threads,
            and_(
                AgentRunRow.thread_id == threads.c.thread_id,
                AgentRunRow.tenant_id == threads.c.tenant_id,
            ),
        )
        .group_by(AgentRunRow.tenant_id, AgentRunRow.thread_id)
        .cte("artifact_thread_last_run")
    )
    version_clock = (
        select(
            versions.c.id,
            versions.c.artifact_id,
            versions.c.tenant_id,
            versions.c.version,
            func.greatest(versions.c.created_at, versions.c.last_used_at, last_run.c.at).label(
                "clock"
            ),
        )
        .select_from(versions)
        .outerjoin(
            source,
            and_(
                source.c.run_id == versions.c.run_id,
                source.c.tenant_id == versions.c.tenant_id,
            ),
        )
        .outerjoin(
            last_run,
            and_(
                last_run.c.thread_id == source.c.thread_id,
                last_run.c.tenant_id == source.c.tenant_id,
            ),
        )
        .cte("artifact_version_clock")
    )
    artifact_clock = (
        select(version_clock.c.artifact_id, func.max(version_clock.c.clock).label("clock"))
        .group_by(version_clock.c.artifact_id)
        .cte("artifact_clock")
    )
    return version_clock, artifact_clock


def _retention_days(default_days: int) -> ColumnElement[int]:
    """租户的 ``artifact_retention_days``,没设(或没有配置行,外连接为空)用 ``default_days``。"""
    return func.coalesce(TenantConfigRow.artifact_retention_days, default_days)


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
        last_used_at=row.last_used_at,
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
        now: datetime,
        default_days: int,
        limit: int = 1000,
    ) -> list[Artifact]:
        _version_clock, artifact_clock = _clocks()
        clock = func.coalesce(artifact_clock.c.clock, ArtifactRow.updated_at)
        expiry = (
            select(
                ArtifactRow.id,
                (clock + func.make_interval(0, 0, 0, _retention_days(default_days))).label(
                    "expires_at"
                ),
            )
            .outerjoin(artifact_clock, artifact_clock.c.artifact_id == ArtifactRow.id)
            .outerjoin(TenantConfigRow, TenantConfigRow.tenant_id == ArtifactRow.tenant_id)
            .where(ArtifactRow.deleted_at.is_(None))
            .subquery("artifact_expiry")
        )
        stmt = (
            select(ArtifactRow)
            .join(expiry, expiry.c.id == ArtifactRow.id)
            .where(expiry.c.expires_at < now)
            .order_by(expiry.c.expires_at.asc(), ArtifactRow.id.asc())
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
        now: datetime,
        default_days: int,
        limit: int = 1000,
    ) -> list[ExpiredArtifactVersion]:
        version_clock, artifact_clock = _clocks()
        days = _retention_days(default_days)
        # 最新版用产物的时钟(各版本里最晚的):最新版不会先于旧版本被清掉,产物也恰好在
        # 最新版到期时离开列表(:meth:`list_active_past_retention` 用的是同一个产物时钟)。
        # 父行不在(没有外键的孤儿版本)时按这一版自己的时钟。
        clock = case(
            (version_clock.c.version == ArtifactRow.latest_version, artifact_clock.c.clock),
            else_=version_clock.c.clock,
        )
        expiry = (
            select(
                version_clock.c.id,
                (clock + func.make_interval(0, 0, 0, days)).label("expires_at"),
                days.label("days"),
            )
            .select_from(version_clock)
            .outerjoin(ArtifactRow, ArtifactRow.id == version_clock.c.artifact_id)
            .outerjoin(artifact_clock, artifact_clock.c.artifact_id == version_clock.c.artifact_id)
            .outerjoin(TenantConfigRow, TenantConfigRow.tenant_id == version_clock.c.tenant_id)
            .subquery("artifact_version_expiry")
        )
        # 同一到期时刻的版本按 ``artifact_id, version`` 定序 —— 老版本先于新版本,跨 store
        # 实现同义;用 ``id``(uuid4)做 tiebreak 则同一产物两个版本谁先谁后是随机的
        # (CI 实测:三跑两红)。
        v = ArtifactVersionRow
        stmt = (
            select(v, expiry.c.days)
            .join(expiry, expiry.c.id == v.id)
            .where(expiry.c.expires_at < now)
            .order_by(expiry.c.expires_at.asc(), v.artifact_id.asc(), v.version.asc())
            .limit(limit)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        return [
            ExpiredArtifactVersion(version=_row_to_version(row), retention_days=int(d))
            for row, d in rows
        ]

    async def touch_version(self, *, version_id: UUID, tenant_id: UUID, now: datetime) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                update(ArtifactVersionRow)
                .where(
                    ArtifactVersionRow.id == version_id,
                    ArtifactVersionRow.tenant_id == tenant_id,
                    or_(
                        ArtifactVersionRow.last_used_at.is_(None),
                        ArtifactVersionRow.last_used_at < now - TOUCH_INTERVAL,
                    ),
                )
                .values(last_used_at=now)
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0) > 0

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

    @staticmethod
    def _agent_artifact_ids(tenant_id: UUID, agent_key: str) -> Any:
        if not agent_key:
            raise ValueError("agent_key must be non-empty")
        return select(ArtifactRow.id).where(
            ArtifactRow.tenant_id == tenant_id, ArtifactRow.agent_key == agent_key
        )

    async def list_versions_for_agent(
        self, *, tenant_id: UUID, agent_key: str
    ) -> list[ArtifactVersion]:
        stmt = select(ArtifactVersionRow).where(
            ArtifactVersionRow.tenant_id == tenant_id,
            ArtifactVersionRow.artifact_id.in_(self._agent_artifact_ids(tenant_id, agent_key)),
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_version(row) for row in rows]

    async def snapshotted_digests(
        self, *, tenant_id: UUID, user_id: UUID, path_prefix: str
    ) -> dict[str, set[str]]:
        stmt = select(ArtifactVersionRow.path_in_workspace, ArtifactVersionRow.sha256).where(
            ArtifactVersionRow.tenant_id == tenant_id,
            ArtifactVersionRow.user_id == user_id,
            ArtifactVersionRow.snapshot_key.is_not(None),
            ArtifactVersionRow.sha256.is_not(None),
            ArtifactVersionRow.path_in_workspace.startswith(path_prefix, autoescape=True),
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        out: dict[str, set[str]] = {}
        for path, digest in rows:
            out.setdefault(path, set()).add(digest)
        return out

    async def count_for_agent(self, *, tenant_id: UUID, agent_key: str) -> int:
        if not agent_key:
            raise ValueError("agent_key must be non-empty")
        return await count_where(
            self._sf,
            ArtifactRow,
            ArtifactRow.tenant_id == tenant_id,
            ArtifactRow.agent_key == agent_key,
        )

    async def delete_all_for_agent(
        self, *, tenant_id: UUID, agent_key: str, keep_users: Collection[UUID] = ()
    ) -> int:
        keep = list(keep_users)
        ids = self._agent_artifact_ids(tenant_id, agent_key).where(ArtifactRow.user_id.notin_(keep))
        async with self._sf() as session:
            # Versions first — ``artifact_id`` is a bare UUID column, no cascade.
            await session.execute(
                delete(ArtifactVersionRow).where(
                    ArtifactVersionRow.tenant_id == tenant_id,
                    ArtifactVersionRow.artifact_id.in_(ids),
                )
            )
            result = await session.execute(
                delete(ArtifactRow).where(
                    ArtifactRow.tenant_id == tenant_id,
                    ArtifactRow.agent_key == agent_key,
                    ArtifactRow.user_id.notin_(keep),
                )
            )
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)
