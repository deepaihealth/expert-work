"""SQLAlchemy-backed :class:`TenantConfigStore` — Stream C.7."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models import TenantConfigRow
from expert_work.persistence.tenant_config.base import (
    TenantConfigAlreadyExistsError,
    TenantConfigNotFoundError,
    TenantConfigStore,
)
from expert_work.persistence.tenant_config.memory import FirstUpsertRequiresDisplayNameError
from expert_work.protocol import (
    CredentialsMode,
    MemoryRecallMode,
    TenantConfigPatch,
    TenantConfigRecord,
    TenantPlan,
    TenantStatus,
    Tool,
    TriggerFireScanMode,
)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _row_to_record(row: TenantConfigRow) -> TenantConfigRecord:
    return TenantConfigRecord(
        tenant_id=row.tenant_id,
        display_name=row.display_name,
        plan=TenantPlan(row.plan),
        status=cast(TenantStatus, row.status),
        model_credentials_ref={str(k): str(v) for k, v in row.model_credentials_ref.items()},
        mcp_allowlist=[str(x) for x in row.mcp_allowlist],
        rate_limit_override=dict(row.rate_limit_override),
        pii_fields=[str(x) for x in row.pii_fields],
        http_tool_allowlist=[str(x) for x in row.http_tool_allowlist],
        http_tool_denylist=[str(x) for x in row.http_tool_denylist],
        mcp_servers=[dict(x) for x in row.mcp_servers],
        audit_retention_days=row.audit_retention_days,
        event_log_retention_days=row.event_log_retention_days,
        trigger_fire_scan_mode=cast(TriggerFireScanMode, row.trigger_fire_scan_mode),
        memory_recall_mode=cast(MemoryRecallMode, row.memory_recall_mode),
        skill_stale_days=row.skill_stale_days,
        skill_archive_days=row.skill_archive_days,
        # Capability Uplift Sprint #7 — MemoryConsolidator thresholds.
        memory_consolidation_min_cluster_size=row.memory_consolidation_min_cluster_size,
        memory_consolidation_similarity=row.memory_consolidation_similarity,
        memory_purge_enabled=row.memory_purge_enabled,
        memory_purge_min_age_days=row.memory_purge_min_age_days,
        memory_predictive_review_enabled=row.memory_predictive_review_enabled,
        # Stream O — credentials mode + tool credentials.
        credentials_mode=cast(CredentialsMode, row.credentials_mode),
        tool_credentials={cast(Tool, str(k)): str(v) for k, v in row.tool_credentials.items()},
        mcp_credentials={str(k): str(v) for k, v in row.mcp_credentials.items()},
        default_agent_name=row.default_agent_name,
        allow_custom_mcp_servers=row.allow_custom_mcp_servers,
        skill_evolution_enabled=row.skill_evolution_enabled,
        skill_evolution_judge_sample_pct=row.skill_evolution_judge_sample_pct,
        created_at=row.created_at,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


class SqlTenantConfigStore(TenantConfigStore):
    """Postgres-backed tenant config repository."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def get(self, *, tenant_id: UUID) -> TenantConfigRecord | None:
        async with self._sf() as session:
            row = await session.get(TenantConfigRow, tenant_id)
        return _row_to_record(row) if row is not None else None

    async def create(
        self,
        *,
        tenant_id: UUID,
        display_name: str,
        plan: TenantPlan | None = None,
        actor_id: str,
    ) -> TenantConfigRecord:
        # Strict INSERT (no ON CONFLICT) — a pre-existing tenant must raise,
        # not silently merge (Mini-ADR P-3). Every other column relies on its
        # server_default ('{}' / '[]' / 'free' / …) so a fresh tenant starts
        # from the platform baseline; admins tune the rest via ``upsert``.
        now = _utc_now()
        stmt = (
            pg_insert(TenantConfigRow)
            .values(
                tenant_id=tenant_id,
                display_name=display_name,
                plan=(plan or TenantPlan.FREE).value,
                created_at=now,
                updated_at=now,
                updated_by=actor_id,
            )
            .returning(TenantConfigRow)
        )
        async with self._sf() as session:
            try:
                row = (await session.execute(stmt)).scalar_one()
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise TenantConfigAlreadyExistsError(tenant_id=tenant_id) from exc
            await session.refresh(row)
            return _row_to_record(row)

    async def upsert(
        self,
        *,
        tenant_id: UUID,
        patch: TenantConfigPatch,
        actor_id: str,
    ) -> TenantConfigRecord:
        async with self._sf() as session:
            existing = await session.get(TenantConfigRow, tenant_id)
            if existing is None:
                if patch.display_name is None:
                    msg = (
                        "first upsert for a tenant must include display_name; "
                        f"got tenant_id={tenant_id}"
                    )
                    raise FirstUpsertRequiresDisplayNameError(msg)
                # Use INSERT ... ON CONFLICT DO UPDATE so a concurrent
                # writer that beat us to the first insert doesn't
                # cause a uniqueness collision.
                now = _utc_now()
                values: dict[str, object] = {
                    "tenant_id": tenant_id,
                    "display_name": patch.display_name,
                    "plan": (patch.plan or TenantPlan.FREE).value,
                    "model_credentials_ref": dict(patch.model_credentials_ref or {}),
                    "mcp_allowlist": list(patch.mcp_allowlist or []),
                    "rate_limit_override": dict(patch.rate_limit_override or {}),
                    "pii_fields": list(patch.pii_fields or []),
                    "http_tool_allowlist": list(patch.http_tool_allowlist or []),
                    "http_tool_denylist": list(patch.http_tool_denylist or []),
                    "mcp_servers": list(patch.mcp_servers or []),
                    "created_at": now,
                    "updated_at": now,
                    "updated_by": actor_id,
                }
                if patch.audit_retention_days is not None:
                    values["audit_retention_days"] = patch.audit_retention_days
                if patch.event_log_retention_days is not None:
                    values["event_log_retention_days"] = patch.event_log_retention_days
                if patch.trigger_fire_scan_mode is not None:
                    values["trigger_fire_scan_mode"] = patch.trigger_fire_scan_mode
                if patch.memory_recall_mode is not None:
                    values["memory_recall_mode"] = patch.memory_recall_mode
                if patch.skill_stale_days is not None:
                    values["skill_stale_days"] = patch.skill_stale_days
                if patch.skill_archive_days is not None:
                    values["skill_archive_days"] = patch.skill_archive_days
                # Capability Uplift Sprint #7 — MemoryConsolidator
                # thresholds (Mini-ADR U-38).
                if patch.memory_consolidation_min_cluster_size is not None:
                    values["memory_consolidation_min_cluster_size"] = (
                        patch.memory_consolidation_min_cluster_size
                    )
                if patch.memory_consolidation_similarity is not None:
                    values["memory_consolidation_similarity"] = (
                        patch.memory_consolidation_similarity
                    )
                if patch.memory_purge_enabled is not None:
                    values["memory_purge_enabled"] = patch.memory_purge_enabled
                if patch.memory_purge_min_age_days is not None:
                    values["memory_purge_min_age_days"] = patch.memory_purge_min_age_days
                if patch.memory_predictive_review_enabled is not None:
                    values["memory_predictive_review_enabled"] = (
                        patch.memory_predictive_review_enabled
                    )
                # Stream O — credentials mode + tool credentials.
                if patch.credentials_mode is not None:
                    values["credentials_mode"] = patch.credentials_mode
                if patch.tool_credentials is not None:
                    values["tool_credentials"] = {
                        str(k): str(v) for k, v in patch.tool_credentials.items()
                    }
                if patch.mcp_credentials is not None:
                    values["mcp_credentials"] = {
                        str(k): str(v) for k, v in patch.mcp_credentials.items()
                    }
                if patch.default_agent_name is not None:
                    values["default_agent_name"] = patch.default_agent_name
                if patch.allow_custom_mcp_servers is not None:
                    values["allow_custom_mcp_servers"] = patch.allow_custom_mcp_servers
                if patch.skill_evolution_enabled is not None:
                    values["skill_evolution_enabled"] = patch.skill_evolution_enabled
                if patch.skill_evolution_judge_sample_pct is not None:
                    values["skill_evolution_judge_sample_pct"] = (
                        patch.skill_evolution_judge_sample_pct
                    )
                stmt = (
                    pg_insert(TenantConfigRow)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["tenant_id"],
                        set_={
                            "display_name": patch.display_name,
                            "updated_at": now,
                            "updated_by": actor_id,
                        },
                    )
                    .returning(TenantConfigRow)
                )
                row = (await session.execute(stmt)).scalar_one()
                await session.commit()
                await session.refresh(row)
                return _row_to_record(row)

            # Update path — merge non-None patch fields.
            if patch.display_name is not None:
                existing.display_name = patch.display_name
            if patch.plan is not None:
                existing.plan = patch.plan.value
            if patch.model_credentials_ref is not None:
                existing.model_credentials_ref = dict(patch.model_credentials_ref)
            if patch.mcp_allowlist is not None:
                existing.mcp_allowlist = list(patch.mcp_allowlist)
            if patch.rate_limit_override is not None:
                existing.rate_limit_override = dict(patch.rate_limit_override)
            if patch.pii_fields is not None:
                existing.pii_fields = list(patch.pii_fields)
            if patch.http_tool_allowlist is not None:
                existing.http_tool_allowlist = list(patch.http_tool_allowlist)
            if patch.http_tool_denylist is not None:
                existing.http_tool_denylist = list(patch.http_tool_denylist)
            if patch.mcp_servers is not None:
                existing.mcp_servers = list(patch.mcp_servers)
            if patch.audit_retention_days is not None:
                existing.audit_retention_days = patch.audit_retention_days
            if patch.event_log_retention_days is not None:
                existing.event_log_retention_days = patch.event_log_retention_days
            if patch.trigger_fire_scan_mode is not None:
                existing.trigger_fire_scan_mode = patch.trigger_fire_scan_mode
            if patch.memory_recall_mode is not None:
                existing.memory_recall_mode = patch.memory_recall_mode
            # Capability Uplift Sprint #4 — apply both fields, then let
            # the DB CHECK (``skill_archive_days > skill_stale_days``)
            # catch any invariant violation on commit. The Pydantic
            # model validator also runs in :func:`_row_to_record` so
            # the same invariant is enforced if the SQL side ever
            # disagrees.
            if patch.skill_stale_days is not None:
                existing.skill_stale_days = patch.skill_stale_days
            if patch.skill_archive_days is not None:
                existing.skill_archive_days = patch.skill_archive_days
            # Capability Uplift Sprint #7 — MemoryConsolidator thresholds.
            # DB CHECK constraints in migration 0046 catch out-of-range
            # values; the Pydantic record_validator re-checks on read.
            if patch.memory_consolidation_min_cluster_size is not None:
                existing.memory_consolidation_min_cluster_size = (
                    patch.memory_consolidation_min_cluster_size
                )
            if patch.memory_consolidation_similarity is not None:
                existing.memory_consolidation_similarity = patch.memory_consolidation_similarity
            if patch.memory_purge_enabled is not None:
                existing.memory_purge_enabled = patch.memory_purge_enabled
            if patch.memory_purge_min_age_days is not None:
                existing.memory_purge_min_age_days = patch.memory_purge_min_age_days
            if patch.memory_predictive_review_enabled is not None:
                existing.memory_predictive_review_enabled = patch.memory_predictive_review_enabled
            # Stream O — credentials mode + tool credentials. The
            # all-or-nothing invariant for ``mode='tenant'`` is enforced
            # by the TenantConfigService gate (Mini-ADR O-4), not here
            # — the store accepts a syntactically valid patch and lets
            # the service decide validity.
            if patch.credentials_mode is not None:
                existing.credentials_mode = patch.credentials_mode
            if patch.tool_credentials is not None:
                existing.tool_credentials = {
                    str(k): str(v) for k, v in patch.tool_credentials.items()
                }
            if patch.mcp_credentials is not None:
                existing.mcp_credentials = {
                    str(k): str(v) for k, v in patch.mcp_credentials.items()
                }
            if patch.default_agent_name is not None:
                existing.default_agent_name = patch.default_agent_name
            if patch.allow_custom_mcp_servers is not None:
                existing.allow_custom_mcp_servers = patch.allow_custom_mcp_servers
            if patch.skill_evolution_enabled is not None:
                existing.skill_evolution_enabled = patch.skill_evolution_enabled
            if patch.skill_evolution_judge_sample_pct is not None:
                existing.skill_evolution_judge_sample_pct = patch.skill_evolution_judge_sample_pct
            existing.updated_at = _utc_now()
            existing.updated_by = actor_id
            await session.commit()
            await session.refresh(existing)
            return _row_to_record(existing)

    async def _mutate_mcp_allowlist(
        self,
        *,
        tenant_id: UUID,
        name: str,
        actor_id: str,
        add: bool,
    ) -> tuple[TenantConfigRecord, bool]:
        # BUG-1(2026-08-24,见 base.py):同一事务内 SELECT ... FOR UPDATE 行锁
        # 后合并——并发/跨副本的 add/remove 在行锁上串行,谁都抹不掉谁。
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(TenantConfigRow)
                    .where(TenantConfigRow.tenant_id == tenant_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise TenantConfigNotFoundError(tenant_id=tenant_id)
            current = list(row.mcp_allowlist or [])
            if add:
                if name in current:
                    return _row_to_record(row), False
                row.mcp_allowlist = [*current, name]
            else:
                if name not in current:
                    return _row_to_record(row), False
                row.mcp_allowlist = [n for n in current if n != name]
            row.updated_at = _utc_now()
            row.updated_by = actor_id
            await session.commit()
            await session.refresh(row)
            return _row_to_record(row), True

    async def add_mcp_allowlist_name(
        self, *, tenant_id: UUID, name: str, actor_id: str
    ) -> tuple[TenantConfigRecord, bool]:
        return await self._mutate_mcp_allowlist(
            tenant_id=tenant_id, name=name, actor_id=actor_id, add=True
        )

    async def remove_mcp_allowlist_name(
        self, *, tenant_id: UUID, name: str, actor_id: str
    ) -> tuple[TenantConfigRecord, bool]:
        return await self._mutate_mcp_allowlist(
            tenant_id=tenant_id, name=name, actor_id=actor_id, add=False
        )

    async def set_status(
        self, *, tenant_id: UUID, status: str, actor_id: str
    ) -> TenantConfigRecord:
        async with self._sf() as session:
            existing = await session.get(TenantConfigRow, tenant_id)
            if existing is None:
                raise TenantConfigNotFoundError(tenant_id=tenant_id)
            existing.status = status
            existing.updated_at = _utc_now()
            existing.updated_by = actor_id
            await session.commit()
            await session.refresh(existing)
            return _row_to_record(existing)

    async def list_all(self, *, limit: int = 50, offset: int = 0) -> list[TenantConfigRecord]:
        async with self._sf() as session:
            result = await session.execute(
                select(TenantConfigRow)
                .order_by(TenantConfigRow.created_at)
                .limit(limit)
                .offset(offset)
            )
            rows = result.scalars().all()
        return [_row_to_record(r) for r in rows]
