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
    AgentSpecDraft,
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
        draft=_row_to_draft(row),
    )


def _row_to_draft(row: AgentSpecRow) -> AgentSpecDraft | None:
    """The row's unpublished draft, or ``None`` when it has none.

    The four draft columns are written and cleared as a unit, so any one of
    them being NULL means "no draft". Keyed on ``draft_spec_json`` because it
    is the one that cannot be a meaningful empty value.
    """
    if row.draft_spec_json is None:
        return None
    # The remaining three are non-NULL whenever the payload is (see the model);
    # asserting that here would turn a storage inconsistency into a 500 on a
    # read path, so trust the writers and let Pydantic reject a genuinely
    # malformed row.
    return AgentSpecDraft(
        spec=AgentSpec.model_validate(row.draft_spec_json),
        spec_sha256=row.draft_sha256 or "",
        updated_by=row.draft_updated_by or "",
        updated_at=row.draft_updated_at,  # type: ignore[arg-type]
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

    async def _live_row(self, session, *, tenant_id: UUID, name: str, version: str):
        """The non-deleted row for ``(tenant, name, version)``, locked for update."""
        return (
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

    async def save_draft(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        spec: AgentSpec,
        spec_sha256: str,
        updated_by: str,
    ) -> AgentSpecRecord | None:
        async with self._sf() as session:
            row = await self._live_row(session, tenant_id=tenant_id, name=name, version=version)
            if row is None:
                return None
            row.draft_spec_json = spec.model_dump(by_alias=True, mode="json")
            row.draft_sha256 = spec_sha256
            row.draft_updated_at = datetime.now(UTC)
            row.draft_updated_by = updated_by
            # 刻意不碰 ``updated_at``:那是**线上这一版**的时间戳,存草稿没有
            # 改变线上任何东西,让它跟着动会让「上次真正发布是什么时候」失真。
            await session.commit()
            await session.refresh(row)
            return _row_to_record(row)

    async def discard_draft(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
    ) -> AgentSpecRecord | None:
        async with self._sf() as session:
            row = await self._live_row(session, tenant_id=tenant_id, name=name, version=version)
            if row is None:
                return None
            row.draft_spec_json = None
            row.draft_sha256 = None
            row.draft_updated_at = None
            row.draft_updated_by = None
            await session.commit()
            await session.refresh(row)
            return _row_to_record(row)

    async def publish_draft(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        updated_by: str,
    ) -> AgentSpecUpdateResult | None:
        async with self._sf() as session:
            row = await self._live_row(session, tenant_id=tenant_id, name=name, version=version)
            if row is None or row.draft_spec_json is None:
                return None
            now = datetime.now(UTC)
            spec_json = row.draft_spec_json
            new_sha = row.draft_sha256 or ""
            prev_sha = row.spec_sha256
            revision: int | None = None
            if prev_sha != new_sha:
                # 与 update_spec 同一条规矩:同 sha 的发布不记历史。发布一份
                # 内容与线上完全相同的草稿不是「一次改动」,只是把编辑缓冲区
                # 清掉。
                revision = (
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
                        revision=revision,
                        spec_json=spec_json,
                        spec_sha256=new_sha,
                        actor_id=updated_by,
                        created_at=now,
                    )
                )
                row.spec_json = spec_json
                row.spec_sha256 = new_sha
                row.updated_at = now
            # 无论内容是否变化,草稿都清掉:发布之后编辑缓冲区就该是空的。
            row.draft_spec_json = None
            row.draft_sha256 = None
            row.draft_updated_at = None
            row.draft_updated_by = None
            await session.commit()
            await session.refresh(row)
            return AgentSpecUpdateResult(
                record=_row_to_record(row), revision=revision, prev_sha256=prev_sha
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
