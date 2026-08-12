"""In-memory ``TenantUserStore`` for unit tests."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from uuid import UUID, uuid4

from expert_work.persistence.tenant_user.base import TenantUserStore
from expert_work.protocol import SubjectType, TenantUser


class InMemoryTenantUserStore(TenantUserStore):
    def __init__(self) -> None:
        self._rows: dict[UUID, TenantUser] = {}

    async def resolve(
        self,
        *,
        tenant_id: UUID,
        subject_type: SubjectType,
        subject_id: str,
        display_name: str | None = None,
    ) -> TenantUser:
        now = datetime.now(UTC)
        for uid, row in self._rows.items():
            if (
                row.tenant_id == tenant_id
                and row.subject_type == subject_type
                and row.subject_id == subject_id
            ):
                updated = row.model_copy(
                    update={
                        "last_active_at": now,
                        "display_name": (
                            display_name if display_name is not None else row.display_name
                        ),
                        # A returning identity reactivates cleanly (Phase 3a):
                        # clear any purge stamp so the user is active + visible
                        # in the roster again, not invisible-but-producing-data.
                        "deleted_at": None,
                    }
                )
                self._rows[uid] = updated
                return updated
        user = TenantUser(
            id=uuid4(),
            tenant_id=tenant_id,
            subject_type=subject_type,
            subject_id=subject_id,
            display_name=display_name,
            created_at=now,
            last_active_at=now,
        )
        self._rows[user.id] = user
        return user

    async def get(self, user_id: UUID, *, tenant_id: UUID) -> TenantUser | None:
        row = self._rows.get(user_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def get_by_subject(
        self, *, tenant_id: UUID, subject_type: SubjectType, subject_id: str
    ) -> TenantUser | None:
        # No ``deleted_at`` filter — same semantics as ``get`` (see base.py
        # docstring), unlike ``list_by_tenant`` which excludes purged rows.
        for row in self._rows.values():
            if (
                row.tenant_id == tenant_id
                and row.subject_type == subject_type
                and row.subject_id == subject_id
            ):
                return row
        return None

    async def get_many(
        self, user_ids: Collection[UUID], *, tenant_id: UUID
    ) -> dict[UUID, TenantUser]:
        wanted = set(user_ids)
        return {
            uid: row
            for uid, row in self._rows.items()
            if uid in wanted and row.tenant_id == tenant_id
        }

    async def list_by_tenant(
        self,
        tenant_id: UUID,
        *,
        subject_type: SubjectType | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TenantUser]:
        rows = [
            row
            for row in self._rows.values()
            if row.tenant_id == tenant_id
            and row.deleted_at is None
            and (subject_type is None or row.subject_type == subject_type)
        ]
        rows.sort(
            key=lambda r: (
                r.last_active_at or datetime.min.replace(tzinfo=UTC),
                r.created_at or datetime.min.replace(tzinfo=UTC),
            ),
            reverse=True,
        )
        return rows[offset : offset + limit]

    async def deactivate(self, user_id: UUID, *, tenant_id: UUID, now: datetime) -> bool:
        row = self._rows.get(user_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        # Idempotent: keep the existing stamp so a re-purge is a no-op.
        if row.deleted_at is None:
            self._rows[user_id] = row.model_copy(update={"deleted_at": now})
        return True

    async def hard_delete_deactivated(self, *, before: datetime, limit: int = 1000) -> int:
        expired = sorted(
            (
                row
                for row in self._rows.values()
                if row.deleted_at is not None and row.deleted_at < before
            ),
            key=lambda row: row.deleted_at or datetime.min.replace(tzinfo=UTC),
        )[:limit]
        if not expired:
            return 0
        for row in expired:
            del self._rows[row.id]
        return len(expired)
