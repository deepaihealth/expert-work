"""In-memory ``UserWorkspaceStore`` for unit tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from expert_work.persistence.workspace.base import (
    UserWorkspaceStore,
    WorkspaceNotFoundError,
    workspace_volume_name,
)
from expert_work.protocol import UserWorkspace


class InMemoryUserWorkspaceStore(UserWorkspaceStore):
    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, UUID], UserWorkspace] = {}

    async def resolve(self, *, tenant_id: UUID, user_id: UUID) -> UserWorkspace:
        now = datetime.now(UTC)
        key = (tenant_id, user_id)
        existing = self._rows.get(key)
        if existing is not None:
            if existing.deleted_at is not None:
                # Soft-deleted rows are read-only — don't bump last_accessed_at.
                return existing
            updated = existing.model_copy(update={"last_accessed_at": now})
            self._rows[key] = updated
            return updated
        workspace = UserWorkspace(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            volume_name=workspace_volume_name(tenant_id, user_id),
            size_bytes=0,
            created_at=now,
            last_accessed_at=now,
        )
        self._rows[key] = workspace
        return workspace

    async def get(self, *, tenant_id: UUID, user_id: UUID) -> UserWorkspace | None:
        return self._rows.get((tenant_id, user_id))

    def _find_by_id(self, workspace_id: UUID) -> tuple[UUID, UUID] | None:
        for key, row in self._rows.items():
            if row.id == workspace_id:
                return key
        return None

    async def update_size(self, *, workspace_id: UUID, size_bytes: int) -> None:
        key = self._find_by_id(workspace_id)
        if key is None:
            raise WorkspaceNotFoundError(workspace_id)
        self._rows[key] = self._rows[key].model_copy(update={"size_bytes": size_bytes})

    async def add_size(self, *, workspace_id: UUID, delta_bytes: int) -> None:
        key = self._find_by_id(workspace_id)
        if key is None:
            return
        row = self._rows[key]
        self._rows[key] = row.model_copy(
            update={"size_bytes": max(row.size_bytes + delta_bytes, 0)}
        )

    async def soft_delete(self, *, workspace_id: UUID, now: datetime) -> None:
        key = self._find_by_id(workspace_id)
        if key is None:
            raise WorkspaceNotFoundError(workspace_id)
        existing = self._rows[key]
        if existing.deleted_at is not None:
            return  # idempotent
        self._rows[key] = existing.model_copy(update={"deleted_at": now})

    async def mark_archived(self, *, workspace_id: UUID, archived_object_key: str) -> None:
        key = self._find_by_id(workspace_id)
        if key is None:
            raise WorkspaceNotFoundError(workspace_id)
        existing = self._rows[key]
        if existing.deleted_at is None:
            # Mirror the SQL CHECK constraint at the in-memory layer.
            raise ValueError("cannot archive a workspace that isn't soft-deleted")
        self._rows[key] = existing.model_copy(update={"archived_object_key": archived_object_key})

    async def list_pending_archive(self) -> list[UserWorkspace]:
        return [
            row
            for row in self._rows.values()
            if row.deleted_at is not None and row.archived_object_key is None
        ]

    async def list_active(self) -> list[UserWorkspace]:
        return [row for row in self._rows.values() if row.deleted_at is None]

    async def list_archived_expired(
        self, *, before: datetime, limit: int = 100
    ) -> list[UserWorkspace]:
        expired = sorted(
            (
                row
                for row in self._rows.values()
                if row.deleted_at is not None
                and row.deleted_at < before
                and row.archived_object_key is not None
            ),
            key=lambda row: row.deleted_at or datetime.min.replace(tzinfo=UTC),
        )
        return expired[:limit]

    async def hard_delete(self, *, workspace_id: UUID) -> bool:
        key = self._find_by_id(workspace_id)
        if key is None:
            return False
        del self._rows[key]
        return True
