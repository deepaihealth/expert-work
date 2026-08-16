"""SQLAlchemy-backed :class:`AgentSpecStore` (Postgres / asyncpg)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from expert_work.persistence.agent_spec.base import (
    AgentSpecStore,
    AgentSpecUpdateResult,
    DuplicateAgentSpecError,
)
from expert_work.persistence.models import AgentSpecRevisionRow, AgentSpecRow
from expert_work.protocol import (
    AgentSpec,
    AgentSpecRecord,
    AgentSpecRevisionRecord,
    AgentSpecStatus,
)


def _row_to_record(row: AgentSpecRow) -> AgentSpecRecord:
    return AgentSpecRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        version=row.version,
        spec=AgentSpec.model_validate(row.spec_json),
        spec_sha256=row.spec_sha256,
        status=AgentSpecStatus(row.status),
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _revision_to_record(row: AgentSpecRevisionRow) -> AgentSpecRevisionRecord:
    return AgentSpecRevisionRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_name=row.agent_name,
        agent_version=row.agent_version,
        revision=row.revision,
        spec=AgentSpec.model_validate(row.spec_json),
        spec_sha256=row.spec_sha256,
        actor_id=row.actor_id,
        created_at=row.created_at,
    )


class SqlAgentSpecStore(AgentSpecStore):
    """Postgres-backed manifest registry."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def create(
        self,
        *,
        tenant_id: UUID,
        spec: AgentSpec,
        spec_sha256: str,
        created_by: str,
    ) -> AgentSpecRecord:
        now = datetime.now(UTC)
        spec_json = spec.model_dump(by_alias=True, mode="json")
        row = AgentSpecRow(
            tenant_id=tenant_id,
            name=spec.metadata.name,
            version=spec.metadata.version,
            spec_json=spec_json,
            spec_sha256=spec_sha256,
            status=AgentSpecStatus.ACTIVE.value,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        async with self._sf() as session:
            session.add(row)
            # Stream HX-5 — revision 1 lands in the same transaction.
            session.add(
                AgentSpecRevisionRow(
                    tenant_id=tenant_id,
                    agent_name=spec.metadata.name,
                    agent_version=spec.metadata.version,
                    revision=1,
                    spec_json=spec_json,
                    spec_sha256=spec_sha256,
                    actor_id=created_by,
                    created_at=now,
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:
                raise DuplicateAgentSpecError(
                    tenant_id=tenant_id,
                    name=spec.metadata.name,
                    version=spec.metadata.version,
                ) from exc
            await session.refresh(row)
            return _row_to_record(row)

    async def get(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        include_deleted: bool = False,
    ) -> AgentSpecRecord | None:
        stmt = select(AgentSpecRow).where(
            AgentSpecRow.tenant_id == tenant_id,
            AgentSpecRow.name == name,
            AgentSpecRow.version == version,
        )
        if not include_deleted:
            stmt = stmt.where(AgentSpecRow.status != AgentSpecStatus.DELETED.value)
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _row_to_record(row) if row is not None else None

    async def list_by_tenant(
        self,
        *,
        tenant_id: UUID,
        status: AgentSpecStatus | None = None,
        name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentSpecRecord]:
        stmt = select(AgentSpecRow).where(AgentSpecRow.tenant_id == tenant_id)
        if status is not None:
            stmt = stmt.where(AgentSpecRow.status == status.value)
        if name is not None:
            stmt = stmt.where(AgentSpecRow.name == name)
        stmt = stmt.order_by(AgentSpecRow.created_at.desc()).limit(limit).offset(offset)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(r) for r in rows]

    async def list_distinct_active_by_tenant(
        self, *, tenant_id: UUID, limit: int = 100, offset: int = 0
    ) -> list[AgentSpecRecord]:
        # DISTINCT ON (name) 要求 ORDER BY 以 name 起头;name, created_at DESC,
        # id DESC(tie-break,见 base.py 抽象方法的 docstring)让每组保留最新
        # 那行。外层再按 name 稳定排序后切片 —— LIMIT/OFFSET 必须打在去重
        # **之后**,否则就是本方法要修的那个 bug(C-1)。
        inner = (
            select(AgentSpecRow)
            .where(
                AgentSpecRow.tenant_id == tenant_id,
                AgentSpecRow.status == AgentSpecStatus.ACTIVE.value,
            )
            .distinct(AgentSpecRow.name)
            .order_by(AgentSpecRow.name, AgentSpecRow.created_at.desc(), AgentSpecRow.id.desc())
            .subquery()
        )
        row_cls = aliased(AgentSpecRow, inner)
        stmt = select(row_cls).order_by(inner.c.name).limit(limit).offset(offset)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(r) for r in rows]

    async def count_distinct_active_by_tenant(self, *, tenant_id: UUID) -> int:
        stmt = select(func.count(func.distinct(AgentSpecRow.name))).where(
            AgentSpecRow.tenant_id == tenant_id,
            AgentSpecRow.status == AgentSpecStatus.ACTIVE.value,
        )
        async with self._sf() as session:
            return (await session.execute(stmt)).scalar_one()

    async def list_all_tenants(
        self,
        *,
        status: AgentSpecStatus | None = None,
        name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentSpecRecord]:
        # Stream N (Mini-ADR N-4) — no tenant_id WHERE clause; caller MUST
        # have ``bypass_rls_var=True`` or RLS filters everything out.
        stmt = select(AgentSpecRow)
        if status is not None:
            stmt = stmt.where(AgentSpecRow.status == status.value)
        if name is not None:
            stmt = stmt.where(AgentSpecRow.name == name)
        stmt = stmt.order_by(AgentSpecRow.created_at.desc()).limit(limit).offset(offset)
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_row_to_record(r) for r in rows]

    async def update_spec(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        spec: AgentSpec,
        spec_sha256: str,
        updated_by: str,
    ) -> AgentSpecUpdateResult | None:
        # Stream HX-5 (Mini-ADR HX-E2) — the row-history table this
        # method's B.5-era comment promised: a content-changing update
        # appends one immutable revision in the same transaction; the
        # main row stays the single "current" pointer. ``updated_by``
        # lands on the revision row (the main row keeps its creator).
        now = datetime.now(UTC)
        spec_json = spec.model_dump(by_alias=True, mode="json")
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(AgentSpecRow)
                    .where(
                        AgentSpecRow.tenant_id == tenant_id,
                        AgentSpecRow.name == name,
                        AgentSpecRow.version == version,
                        AgentSpecRow.status != AgentSpecStatus.DELETED.value,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            prev_sha = row.spec_sha256
            if prev_sha == spec_sha256:
                # No-op: identical content, nothing changes, nothing recorded.
                return AgentSpecUpdateResult(
                    record=_row_to_record(row), revision=None, prev_sha256=prev_sha
                )
            next_revision = (
                await session.execute(
                    select(func.coalesce(func.max(AgentSpecRevisionRow.revision), 0)).where(
                        AgentSpecRevisionRow.tenant_id == tenant_id,
                        AgentSpecRevisionRow.agent_name == name,
                        AgentSpecRevisionRow.agent_version == version,
                    )
                )
            ).scalar_one() + 1
            session.add(
                AgentSpecRevisionRow(
                    tenant_id=tenant_id,
                    agent_name=name,
                    agent_version=version,
                    revision=next_revision,
                    spec_json=spec_json,
                    spec_sha256=spec_sha256,
                    actor_id=updated_by,
                    created_at=now,
                )
            )
            row.spec_json = spec_json
            row.spec_sha256 = spec_sha256
            row.updated_at = now
            await session.commit()
            await session.refresh(row)
            return AgentSpecUpdateResult(
                record=_row_to_record(row), revision=next_revision, prev_sha256=prev_sha
            )

    async def list_revisions(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentSpecRevisionRecord]:
        stmt = (
            select(AgentSpecRevisionRow)
            .where(
                AgentSpecRevisionRow.tenant_id == tenant_id,
                AgentSpecRevisionRow.agent_name == name,
                AgentSpecRevisionRow.agent_version == version,
            )
            .order_by(AgentSpecRevisionRow.revision.desc())
            .limit(limit)
            .offset(offset)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_revision_to_record(r) for r in rows]

    async def get_revision(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        revision: int,
    ) -> AgentSpecRevisionRecord | None:
        stmt = select(AgentSpecRevisionRow).where(
            AgentSpecRevisionRow.tenant_id == tenant_id,
            AgentSpecRevisionRow.agent_name == name,
            AgentSpecRevisionRow.agent_version == version,
            AgentSpecRevisionRow.revision == revision,
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _revision_to_record(row) if row is not None else None

    async def update_status(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        status: AgentSpecStatus,
    ) -> AgentSpecRecord | None:
        stmt = (
            update(AgentSpecRow)
            .where(
                AgentSpecRow.tenant_id == tenant_id,
                AgentSpecRow.name == name,
                AgentSpecRow.version == version,
            )
            .values(status=status.value, updated_at=datetime.now(UTC))
            .returning(AgentSpecRow)
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
            row = result.scalar_one_or_none()
        return _row_to_record(row) if row is not None else None
