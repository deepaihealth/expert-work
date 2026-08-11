"""SQLAlchemy-backed ``TenantUserStore`` (Postgres / asyncpg)."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import TenantUserRow
from expert_work.persistence.tenant_user.base import TenantUserStore
from expert_work.protocol import SubjectType, TenantUser


def _row_to_user(row: TenantUserRow) -> TenantUser:
    return TenantUser(
        id=row.id,
        tenant_id=row.tenant_id,
        subject_type=row.subject_type,  # type: ignore[arg-type]
        subject_id=row.subject_id,
        display_name=row.display_name,
        created_at=row.created_at,
        last_active_at=row.last_active_at,
        deleted_at=row.deleted_at,
    )


class SqlTenantUserStore(TenantUserStore):
    """Postgres-backed per-user registry repository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def resolve(
        self,
        *,
        tenant_id: UUID,
        subject_type: SubjectType,
        subject_id: str,
        display_name: str | None = None,
    ) -> TenantUser:
        now = datetime.now(UTC)
        # INSERT ... ON CONFLICT DO UPDATE — a race-free idempotent
        # upsert. ``display_name`` is only overwritten when the caller
        # supplies a non-NULL value, so a later resolve() with no name
        # never clobbers a name set by the admin UI.
        insert_stmt = pg_insert(TenantUserRow).values(
            tenant_id=tenant_id,
            subject_type=subject_type,
            subject_id=subject_id,
            display_name=display_name,
            created_at=now,
            last_active_at=now,
        )
        upsert = insert_stmt.on_conflict_do_update(
            constraint="tenant_user_identity_uniq",
            set_={
                "last_active_at": now,
                "display_name": func.coalesce(
                    insert_stmt.excluded.display_name,
                    TenantUserRow.display_name,
                ),
                # A returning identity reactivates cleanly (Phase 3a): clear any
                # purge stamp so the user is active + visible in the roster
                # again, not invisible-but-producing-data.
                "deleted_at": None,
            },
        ).returning(TenantUserRow.id)
        async with self._sf() as session:
            result = await session.execute(upsert)
            user_id = result.scalar_one()
            await session.commit()
            row = await session.get(TenantUserRow, user_id)
            # The row was just inserted/updated in this transaction —
            # ``get`` always resolves it.
            assert row is not None  # noqa: S101
            return _row_to_user(row)

    async def get(self, user_id: UUID, *, tenant_id: UUID) -> TenantUser | None:
        async with self._sf() as session:
            row = await session.get(TenantUserRow, user_id)
            if row is None or row.tenant_id != tenant_id:
                return None
            return _row_to_user(row)

    async def get_by_subject(
        self, *, tenant_id: UUID, subject_type: SubjectType, subject_id: str
    ) -> TenantUser | None:
        # Hits ``tenant_user_identity_uniq`` directly — no ``deleted_at``
        # predicate (see base.py docstring: same semantics as ``get``).
        stmt = select(TenantUserRow).where(
            TenantUserRow.tenant_id == tenant_id,
            TenantUserRow.subject_type == subject_type,
            TenantUserRow.subject_id == subject_id,
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return None if row is None else _row_to_user(row)

    async def get_many(
        self, user_ids: Collection[UUID], *, tenant_id: UUID
    ) -> dict[UUID, TenantUser]:
        ids = list(set(user_ids))
        if not ids:
            return {}
        stmt = select(TenantUserRow).where(
            TenantUserRow.id.in_(ids),
            TenantUserRow.tenant_id == tenant_id,
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return {row.id: _row_to_user(row) for row in rows}

    async def list_by_tenant(
        self,
        tenant_id: UUID,
        *,
        subject_type: SubjectType | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TenantUser]:
        stmt = select(TenantUserRow).where(
            TenantUserRow.tenant_id == tenant_id,
            TenantUserRow.deleted_at.is_(None),
        )
        if subject_type is not None:
            stmt = stmt.where(TenantUserRow.subject_type == subject_type)
        stmt = (
            stmt.order_by(
                TenantUserRow.last_active_at.desc(),
                TenantUserRow.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_user(row) for row in rows]

    async def deactivate(self, user_id: UUID, *, tenant_id: UUID, now: datetime) -> bool:
        # Idempotent: only stamp a row that is still active, so a re-purge
        # never rewrites the original deactivation time. Tenant-scoped —
        # the tenant predicate is the hard isolation boundary.
        stmt = (
            update(TenantUserRow)
            .where(
                TenantUserRow.id == user_id,
                TenantUserRow.tenant_id == tenant_id,
                TenantUserRow.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        if int(getattr(result, "rowcount", 0) or 0) > 0:
            return True
        # Either truly missing or already deactivated — differentiate with a
        # cheap existence probe so a re-purge is idempotent-True but an
        # unknown id is a clean False (matches the memory-store semantics).
        async with self._sf() as session:
            exists = (
                await session.execute(
                    select(TenantUserRow.id).where(
                        TenantUserRow.id == user_id,
                        TenantUserRow.tenant_id == tenant_id,
                    )
                )
            ).first()
        return exists is not None

    async def hard_delete_deactivated(self, *, before: datetime, limit: int = 1000) -> int:
        subq = (
            select(TenantUserRow.id)
            .where(
                TenantUserRow.deleted_at.is_not(None),
                TenantUserRow.deleted_at < before,
            )
            .order_by(TenantUserRow.deleted_at.asc())
            .limit(limit)
        )
        stmt = delete(TenantUserRow).where(TenantUserRow.id.in_(subq))
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)
