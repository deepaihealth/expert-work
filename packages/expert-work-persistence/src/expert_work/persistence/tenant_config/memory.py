"""In-memory :class:`TenantConfigStore` — Stream C.7."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from expert_work.persistence.tenant_config.base import (
    TenantConfigAlreadyExistsError,
    TenantConfigNotFoundError,
    TenantConfigStore,
)
from expert_work.protocol import TenantConfigPatch, TenantConfigRecord, TenantPlan


def _now() -> datetime:
    return datetime.now(tz=UTC)


class FirstUpsertRequiresDisplayNameError(ValueError):
    """``upsert`` for a tenant with no existing row must include ``display_name``."""


class InMemoryTenantConfigStore(TenantConfigStore):
    """Single-dict store; lock-guarded for asyncio safety."""

    def __init__(self) -> None:
        self._rows: dict[UUID, TenantConfigRecord] = {}
        self._lock = asyncio.Lock()

    async def get(self, *, tenant_id: UUID) -> TenantConfigRecord | None:
        async with self._lock:
            return self._rows.get(tenant_id)

    async def create(
        self,
        *,
        tenant_id: UUID,
        display_name: str,
        plan: TenantPlan | None = None,
        actor_id: str,
    ) -> TenantConfigRecord:
        now = _now()
        async with self._lock:
            if tenant_id in self._rows:
                raise TenantConfigAlreadyExistsError(tenant_id=tenant_id)
            # Only display_name / plan are set; every other field falls back
            # to its TenantConfigRecord default (mirrors the SQL store's
            # reliance on column server defaults) — Mini-ADR P-1/P-3.
            row = TenantConfigRecord(
                tenant_id=tenant_id,
                display_name=display_name,
                plan=plan or TenantPlan.FREE,
                created_at=now,
                updated_at=now,
                updated_by=actor_id,
            )
            self._rows[tenant_id] = row
            return row

    async def upsert(
        self,
        *,
        tenant_id: UUID,
        patch: TenantConfigPatch,
        actor_id: str,
    ) -> TenantConfigRecord:
        now = _now()
        async with self._lock:
            existing = self._rows.get(tenant_id)
            if existing is None:
                if patch.display_name is None:
                    msg = (
                        "first upsert for a tenant must include display_name; "
                        f"got tenant_id={tenant_id}"
                    )
                    raise FirstUpsertRequiresDisplayNameError(msg)
                # Use the field defaults from TenantConfigRecord unless the
                # patch overrides them; ``None`` means "use default".
                row_kwargs: dict[str, object] = {
                    "tenant_id": tenant_id,
                    "display_name": patch.display_name,
                    "plan": patch.plan or TenantPlan.FREE,
                    "model_credentials_ref": patch.model_credentials_ref or {},
                    "mcp_allowlist": patch.mcp_allowlist or [],
                    "rate_limit_override": patch.rate_limit_override or {},
                    "pii_fields": patch.pii_fields or [],
                    "http_tool_allowlist": patch.http_tool_allowlist or [],
                    "http_tool_denylist": patch.http_tool_denylist or [],
                    "mcp_servers": patch.mcp_servers or [],
                    "created_at": now,
                    "updated_at": now,
                    "updated_by": actor_id,
                }
                if patch.audit_retention_days is not None:
                    row_kwargs["audit_retention_days"] = patch.audit_retention_days
                if patch.event_log_retention_days is not None:
                    row_kwargs["event_log_retention_days"] = patch.event_log_retention_days
                if patch.trigger_fire_scan_mode is not None:
                    row_kwargs["trigger_fire_scan_mode"] = patch.trigger_fire_scan_mode
                if patch.memory_recall_mode is not None:
                    row_kwargs["memory_recall_mode"] = patch.memory_recall_mode
                if patch.skill_stale_days is not None:
                    row_kwargs["skill_stale_days"] = patch.skill_stale_days
                if patch.skill_archive_days is not None:
                    row_kwargs["skill_archive_days"] = patch.skill_archive_days
                # Capability Uplift Sprint #7 — MemoryConsolidator thresholds.
                if patch.memory_consolidation_min_cluster_size is not None:
                    row_kwargs["memory_consolidation_min_cluster_size"] = (
                        patch.memory_consolidation_min_cluster_size
                    )
                if patch.memory_consolidation_similarity is not None:
                    row_kwargs["memory_consolidation_similarity"] = (
                        patch.memory_consolidation_similarity
                    )
                if patch.memory_purge_enabled is not None:
                    row_kwargs["memory_purge_enabled"] = patch.memory_purge_enabled
                if patch.memory_purge_min_age_days is not None:
                    row_kwargs["memory_purge_min_age_days"] = patch.memory_purge_min_age_days
                if patch.memory_predictive_review_enabled is not None:
                    row_kwargs["memory_predictive_review_enabled"] = (
                        patch.memory_predictive_review_enabled
                    )
                # Stream O — credentials mode + tool credentials.
                if patch.credentials_mode is not None:
                    row_kwargs["credentials_mode"] = patch.credentials_mode
                if patch.tool_credentials is not None:
                    row_kwargs["tool_credentials"] = dict(patch.tool_credentials)
                if patch.mcp_credentials is not None:
                    row_kwargs["mcp_credentials"] = dict(patch.mcp_credentials)
                if patch.default_agent_name is not None:
                    row_kwargs["default_agent_name"] = patch.default_agent_name
                if patch.allow_custom_mcp_servers is not None:
                    row_kwargs["allow_custom_mcp_servers"] = patch.allow_custom_mcp_servers
                if patch.skill_evolution_enabled is not None:
                    row_kwargs["skill_evolution_enabled"] = patch.skill_evolution_enabled
                if patch.skill_evolution_judge_sample_pct is not None:
                    row_kwargs["skill_evolution_judge_sample_pct"] = (
                        patch.skill_evolution_judge_sample_pct
                    )
                row = TenantConfigRecord(**row_kwargs)  # type: ignore[arg-type]
            else:
                row = existing.model_copy(
                    update={
                        k: v
                        for k, v in {
                            "display_name": patch.display_name,
                            "plan": patch.plan,
                            "model_credentials_ref": patch.model_credentials_ref,
                            "mcp_allowlist": patch.mcp_allowlist,
                            "rate_limit_override": patch.rate_limit_override,
                            "pii_fields": patch.pii_fields,
                            "http_tool_allowlist": patch.http_tool_allowlist,
                            "http_tool_denylist": patch.http_tool_denylist,
                            "mcp_servers": patch.mcp_servers,
                            "audit_retention_days": patch.audit_retention_days,
                            "event_log_retention_days": patch.event_log_retention_days,
                            "trigger_fire_scan_mode": patch.trigger_fire_scan_mode,
                            "memory_recall_mode": patch.memory_recall_mode,
                            "skill_stale_days": patch.skill_stale_days,
                            "skill_archive_days": patch.skill_archive_days,
                            # Capability Uplift Sprint #7.
                            "memory_consolidation_min_cluster_size": (
                                patch.memory_consolidation_min_cluster_size
                            ),
                            "memory_consolidation_similarity": (
                                patch.memory_consolidation_similarity
                            ),
                            "memory_purge_enabled": patch.memory_purge_enabled,
                            "memory_purge_min_age_days": patch.memory_purge_min_age_days,
                            "memory_predictive_review_enabled": (
                                patch.memory_predictive_review_enabled
                            ),
                            # Stream O — credentials mode + tool credentials.
                            "credentials_mode": patch.credentials_mode,
                            "tool_credentials": patch.tool_credentials,
                            "mcp_credentials": patch.mcp_credentials,
                            # Stream R — tenant default agent.
                            "default_agent_name": patch.default_agent_name,
                            # Stream W — custom MCP kill-switch.
                            "allow_custom_mcp_servers": patch.allow_custom_mcp_servers,
                            # SE-16 (SE-A41) — skill-evolution rollout gate.
                            "skill_evolution_enabled": patch.skill_evolution_enabled,
                            # SE-16 (SE-A45) — implicit judge sample rate.
                            "skill_evolution_judge_sample_pct": (
                                patch.skill_evolution_judge_sample_pct
                            ),
                        }.items()
                        if v is not None
                    }
                    | {"updated_at": now, "updated_by": actor_id},
                )
                # Re-validate the merged row to catch the cross-field
                # invariant (archive_days > stale_days). model_copy
                # doesn't run validators; force a round-trip through
                # construction so an admin patch can't slip a config
                # that the platform rejects on first Curator read.
                row = TenantConfigRecord.model_validate(row.model_dump())
            self._rows[tenant_id] = row
            return row

    async def add_mcp_allowlist_name(
        self, *, tenant_id: UUID, name: str, actor_id: str
    ) -> tuple[TenantConfigRecord, bool]:
        # 与 SQL 后端同义:读-判-写在同一把 store 锁内完成(见 base.py 的
        # BUG-1 说明)。
        async with self._lock:
            existing = self._rows.get(tenant_id)
            if existing is None:
                raise TenantConfigNotFoundError(tenant_id=tenant_id)
            if name in existing.mcp_allowlist:
                return existing, False
            updated = existing.model_copy(
                update={
                    "mcp_allowlist": [*existing.mcp_allowlist, name],
                    "updated_by": actor_id,
                    "updated_at": _now(),
                }
            )
            self._rows[tenant_id] = updated
            return updated, True

    async def remove_mcp_allowlist_name(
        self, *, tenant_id: UUID, name: str, actor_id: str
    ) -> tuple[TenantConfigRecord, bool]:
        async with self._lock:
            existing = self._rows.get(tenant_id)
            if existing is None:
                raise TenantConfigNotFoundError(tenant_id=tenant_id)
            if name not in existing.mcp_allowlist:
                return existing, False
            updated = existing.model_copy(
                update={
                    "mcp_allowlist": [n for n in existing.mcp_allowlist if n != name],
                    "updated_by": actor_id,
                    "updated_at": _now(),
                }
            )
            self._rows[tenant_id] = updated
            return updated, True

    async def set_status(
        self, *, tenant_id: UUID, status: str, actor_id: str
    ) -> TenantConfigRecord:
        async with self._lock:
            existing = self._rows.get(tenant_id)
            if existing is None:
                raise TenantConfigNotFoundError(tenant_id=tenant_id)
            updated = existing.model_copy(
                update={"status": status, "updated_by": actor_id, "updated_at": _now()}
            )
            self._rows[tenant_id] = updated
            return updated

    async def list_all(self, *, limit: int = 50, offset: int = 0) -> list[TenantConfigRecord]:
        async with self._lock:
            ordered = sorted(self._rows.values(), key=lambda r: r.created_at)
        return ordered[offset : offset + limit]
