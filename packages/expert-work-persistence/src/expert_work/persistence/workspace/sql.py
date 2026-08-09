"""SQLAlchemy-backed ``UserWorkspaceStore`` (Postgres / asyncpg)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import UserWorkspaceRow
from expert_work.persistence.workspace.base import (
    UserWorkspaceStore,
    WorkspaceNotFoundError,
    workspace_volume_name,
)
from expert_work.protocol import UserWorkspace


def _row_to_workspace(row: UserWorkspaceRow) -> UserWorkspace:
    return UserWorkspace(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        volume_name=row.volume_name,
        size_bytes=row.size_bytes,
        size_limit_bytes=row.size_limit_bytes,
        created_at=row.created_at,
        last_accessed_at=row.last_accessed_at,
        last_write_at=row.last_write_at,
        deleted_at=row.deleted_at,
        archived_object_key=row.archived_object_key,
    )


class SqlUserWorkspaceStore(UserWorkspaceStore):
    """Postgres-backed per-user workspace registry."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def resolve(self, *, tenant_id: UUID, user_id: UUID) -> UserWorkspace:
        now = datetime.now(UTC)
        # INSERT ... ON CONFLICT DO UPDATE — a race-free idempotent
        # upsert. ``volume_name`` is deterministic, so a conflicting
        # row already holds the same name; only ``last_accessed_at``
        # is bumped (and only for active rows — soft-deleted rows are
        # read-only on the resolve path; Mini-ADR J-36).
        insert_stmt = pg_insert(UserWorkspaceRow).values(
            tenant_id=tenant_id,
            user_id=user_id,
            volume_name=workspace_volume_name(tenant_id, user_id),
            created_at=now,
            last_accessed_at=now,
        )
        upsert = insert_stmt.on_conflict_do_update(
            constraint="user_workspace_identity_uniq",
            set_={"last_accessed_at": now},
            # Don't bump last_accessed_at on rows that have been soft-deleted.
            where=UserWorkspaceRow.deleted_at.is_(None),
        ).returning(UserWorkspaceRow.id)
        async with self._sf() as session:
            result = await session.execute(upsert)
            workspace_id = result.scalar_one_or_none()
            if workspace_id is None:
                # ON CONFLICT WHERE filtered out a soft-deleted row;
                # fetch it as-is (the soft-deleted row).
                lookup = await session.execute(
                    select(UserWorkspaceRow).where(
                        UserWorkspaceRow.tenant_id == tenant_id,
                        UserWorkspaceRow.user_id == user_id,
                    )
                )
                return _row_to_workspace(lookup.scalar_one())
            await session.commit()
            row = await session.get(UserWorkspaceRow, workspace_id)
            # Just inserted/updated in this transaction — always resolves.
            if row is None:
                msg = f"workspace {workspace_id} vanished between upsert and fetch"
                raise RuntimeError(msg)
            return _row_to_workspace(row)

    async def get(self, *, tenant_id: UUID, user_id: UUID) -> UserWorkspace | None:
        async with self._sf() as session:
            result = await session.execute(
                select(UserWorkspaceRow).where(
                    UserWorkspaceRow.tenant_id == tenant_id,
                    UserWorkspaceRow.user_id == user_id,
                )
            )
            row = result.scalar_one_or_none()
            return _row_to_workspace(row) if row is not None else None

    async def update_size(self, *, workspace_id: UUID, size_bytes: int) -> None:
        async with self._sf() as session:
            result = await session.execute(
                update(UserWorkspaceRow)
                .where(UserWorkspaceRow.id == workspace_id)
                .values(size_bytes=size_bytes)
            )
            # rowcount is supported by CursorResult for UPDATE statements;
            # mypy stubs for SQLAlchemy's async API don't expose it yet.
            if result.rowcount == 0:  # type: ignore[attr-defined]
                raise WorkspaceNotFoundError(workspace_id)
            await session.commit()

    async def add_size(self, *, workspace_id: UUID, delta_bytes: int) -> None:
        async with self._sf() as session:
            await session.execute(
                update(UserWorkspaceRow)
                .where(UserWorkspaceRow.id == workspace_id)
                .values(size_bytes=func.greatest(UserWorkspaceRow.size_bytes + delta_bytes, 0))
            )
            await session.commit()

    async def soft_delete(self, *, workspace_id: UUID, now: datetime) -> None:
        async with self._sf() as session:
            # Idempotent: only set deleted_at if currently NULL.
            result = await session.execute(
                update(UserWorkspaceRow)
                .where(
                    UserWorkspaceRow.id == workspace_id,
                    UserWorkspaceRow.deleted_at.is_(None),
                )
                .values(deleted_at=now)
            )
            await session.commit()
            if result.rowcount == 0:  # type: ignore[attr-defined]
                # Either row doesn't exist or already soft-deleted.
                # Disambiguate by checking existence.
                lookup = await session.get(UserWorkspaceRow, workspace_id)
                if lookup is None:
                    raise WorkspaceNotFoundError(workspace_id)
                # else: already soft-deleted → idempotent no-op.

    async def mark_archived(self, *, workspace_id: UUID, archived_object_key: str) -> None:
        async with self._sf() as session:
            # CHECK constraint user_workspace_archive_consistency
            # enforces "archived implies deleted" at DB level; this
            # filter is belt-and-suspenders.
            result = await session.execute(
                update(UserWorkspaceRow)
                .where(
                    UserWorkspaceRow.id == workspace_id,
                    UserWorkspaceRow.deleted_at.is_not(None),
                )
                .values(archived_object_key=archived_object_key)
            )
            await session.commit()
            if result.rowcount == 0:  # type: ignore[attr-defined]
                lookup = await session.get(UserWorkspaceRow, workspace_id)
                if lookup is None:
                    raise WorkspaceNotFoundError(workspace_id)
                raise ValueError("cannot archive a workspace that isn't soft-deleted")

    async def list_pending_archive(self) -> list[UserWorkspace]:
        async with self._sf() as session:
            # Backed by partial index user_workspace_pending_archive_idx.
            result = await session.execute(
                select(UserWorkspaceRow).where(
                    UserWorkspaceRow.deleted_at.is_not(None),
                    UserWorkspaceRow.archived_object_key.is_(None),
                )
            )
            return [_row_to_workspace(row) for row in result.scalars().all()]

    async def list_active(self) -> list[UserWorkspace]:
        async with self._sf() as session:
            result = await session.execute(
                select(UserWorkspaceRow).where(UserWorkspaceRow.deleted_at.is_(None))
            )
            return [_row_to_workspace(row) for row in result.scalars().all()]

    async def list_archived_expired(
        self, *, before: datetime, limit: int = 100
    ) -> list[UserWorkspace]:
        async with self._sf() as session:
            result = await session.execute(
                select(UserWorkspaceRow)
                .where(
                    UserWorkspaceRow.deleted_at.is_not(None),
                    UserWorkspaceRow.deleted_at < before,
                    UserWorkspaceRow.archived_object_key.is_not(None),
                )
                .order_by(UserWorkspaceRow.deleted_at.asc())
                .limit(limit)
            )
            return [_row_to_workspace(row) for row in result.scalars().all()]

    async def hard_delete(self, *, workspace_id: UUID) -> bool:
        async with self._sf() as session:
            result = await session.execute(
                delete(UserWorkspaceRow).where(UserWorkspaceRow.id == workspace_id)
            )
            await session.commit()
            # rowcount is supported by CursorResult for DELETE statements;
            # mypy stubs for SQLAlchemy's async API don't expose it yet.
            return bool(result.rowcount)  # type: ignore[attr-defined]
