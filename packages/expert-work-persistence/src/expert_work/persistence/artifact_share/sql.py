"""SQLAlchemy-backed ``ArtifactShareLinkStore`` — B-326."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, delete, func, select, text, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.artifact_share.base import (
    ArtifactShareLink,
    ArtifactShareLinkStore,
    new_link_id,
)
from expert_work.persistence.models import ArtifactShareLinkRow
from expert_work.persistence.rls import require_rls_bypass


def _row_to_dto(row: ArtifactShareLinkRow) -> ArtifactShareLink:
    return ArtifactShareLink(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        agent_key=row.agent_key,
        artifact_id=row.artifact_id,
        artifact_version_id=row.artifact_version_id,
        version=row.version,
        run_id=row.run_id,
        thread_id=row.thread_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        download_count=row.download_count,
        file_name=row.file_name,
    )


class SqlArtifactShareLinkStore(ArtifactShareLinkStore):
    """Postgres-backed share-link registry."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def mint_or_reuse(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        agent_key: str,
        artifact_id: UUID,
        artifact_version_id: UUID,
        version: int,
        thread_id: UUID,
        run_id: UUID | None,
        file_name: str,
        ttl: timedelta,
        now: datetime,
        max_downloads: int,
    ) -> ArtifactShareLink:
        async with self._sf() as session:
            # One reusable link per (version, conversation), also across replicas: the
            # read-then-insert runs under a transaction-scoped advisory lock on that pair.
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"artifact_share_links:{thread_id}:{artifact_version_id}"},
            )
            # Judge and stamp on one clock: the caller's ``now`` was read before waiting
            # for the lock (and on whichever replica); the database clock read after it
            # orders concurrent callers the way the lock did.
            db_now = (await session.execute(text("SELECT clock_timestamp()"))).scalar_one()
            now = max(now, db_now)
            newest = (
                await session.execute(
                    select(ArtifactShareLinkRow)
                    .where(
                        ArtifactShareLinkRow.tenant_id == tenant_id,
                        ArtifactShareLinkRow.user_id == user_id,
                        ArtifactShareLinkRow.thread_id == thread_id,
                        ArtifactShareLinkRow.artifact_version_id == artifact_version_id,
                    )
                    .order_by(ArtifactShareLinkRow.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if newest is not None:
                dto = _row_to_dto(newest)
                if dto.reusable(now, ttl=ttl, max_downloads=max_downloads):
                    await session.commit()
                    return dto
            row = ArtifactShareLinkRow(
                id=new_link_id(),
                tenant_id=tenant_id,
                user_id=user_id,
                agent_key=agent_key,
                artifact_id=artifact_id,
                artifact_version_id=artifact_version_id,
                version=version,
                run_id=run_id,
                thread_id=thread_id,
                created_at=now,
                expires_at=now + ttl,
                file_name=file_name,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return _row_to_dto(row)

    async def newest_for(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        keys: Sequence[tuple[UUID, UUID]],
    ) -> dict[tuple[UUID, UUID], ArtifactShareLink]:
        wanted = list(dict.fromkeys(keys))
        if not wanted:
            return {}
        pair = tuple_(ArtifactShareLinkRow.thread_id, ArtifactShareLinkRow.artifact_version_id)
        async with self._sf() as session:
            rows = (
                (
                    await session.execute(
                        select(ArtifactShareLinkRow)
                        .where(
                            ArtifactShareLinkRow.tenant_id == tenant_id,
                            ArtifactShareLinkRow.user_id == user_id,
                            pair.in_(wanted),
                        )
                        .distinct(
                            ArtifactShareLinkRow.thread_id,
                            ArtifactShareLinkRow.artifact_version_id,
                        )
                        .order_by(
                            ArtifactShareLinkRow.thread_id,
                            ArtifactShareLinkRow.artifact_version_id,
                            ArtifactShareLinkRow.created_at.desc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return {(row.thread_id, row.artifact_version_id): _row_to_dto(row) for row in rows}

    async def get_public(self, *, link_id: str) -> ArtifactShareLink | None:
        require_rls_bypass("ArtifactShareLinkStore.get_public")
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(ArtifactShareLinkRow).where(ArtifactShareLinkRow.id == link_id)
                )
            ).scalar_one_or_none()
        return _row_to_dto(row) if row is not None else None

    async def claim_download(self, *, link_id: str, max_downloads: int) -> bool:
        require_rls_bypass("ArtifactShareLinkStore.claim_download")
        async with self._sf() as session:
            claimed = (
                await session.execute(
                    update(ArtifactShareLinkRow)
                    .where(
                        ArtifactShareLinkRow.id == link_id,
                        ArtifactShareLinkRow.download_count < max_downloads,
                    )
                    .values(download_count=ArtifactShareLinkRow.download_count + 1)
                    .returning(ArtifactShareLinkRow.id)
                )
            ).scalar_one_or_none()
            await session.commit()
        return claimed is not None

    async def refund_download(self, *, link_id: str) -> None:
        require_rls_bypass("ArtifactShareLinkStore.refund_download")
        async with self._sf() as session:
            await session.execute(
                update(ArtifactShareLinkRow)
                .where(ArtifactShareLinkRow.id == link_id)
                .values(download_count=func.greatest(ArtifactShareLinkRow.download_count - 1, 0))
            )
            await session.commit()

    async def delete_for_threads(self, *, tenant_id: UUID, thread_ids: Sequence[UUID]) -> int:
        if not thread_ids:
            return 0
        return await self._delete_where(
            ArtifactShareLinkRow.tenant_id == tenant_id,
            ArtifactShareLinkRow.thread_id.in_(list(thread_ids)),
        )

    async def delete_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        return await self._delete_where(
            ArtifactShareLinkRow.tenant_id == tenant_id,
            ArtifactShareLinkRow.user_id == user_id,
        )

    async def delete_for_agent(
        self, *, tenant_id: UUID, agent_key: str, keep_users: Collection[UUID] = ()
    ) -> int:
        return await self._delete_where(
            ArtifactShareLinkRow.tenant_id == tenant_id,
            ArtifactShareLinkRow.agent_key == agent_key,
            ArtifactShareLinkRow.user_id.notin_(list(keep_users)),
        )

    async def _delete_where(self, *conditions: ColumnElement[bool]) -> int:
        async with self._sf() as session:
            result = await session.execute(delete(ArtifactShareLinkRow).where(*conditions))
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)
