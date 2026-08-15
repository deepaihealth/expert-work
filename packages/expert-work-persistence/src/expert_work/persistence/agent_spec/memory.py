"""In-memory :class:`AgentSpecStore` — used by control-plane tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from expert_work.persistence.agent_spec.base import (
    AgentSpecStore,
    AgentSpecUpdateResult,
    DuplicateAgentSpecError,
)
from expert_work.protocol import (
    AgentSpec,
    AgentSpecRecord,
    AgentSpecRevisionRecord,
    AgentSpecStatus,
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


class InMemoryAgentSpecStore(AgentSpecStore):
    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, str, str], AgentSpecRecord] = {}
        self._revisions: dict[tuple[UUID, str, str], list[AgentSpecRevisionRecord]] = {}
        self._lock = asyncio.Lock()

    def _append_revision(
        self,
        key: tuple[UUID, str, str],
        *,
        spec: AgentSpec,
        spec_sha256: str,
        actor_id: str,
    ) -> int:
        history = self._revisions.setdefault(key, [])
        revision = len(history) + 1
        history.append(
            AgentSpecRevisionRecord(
                id=uuid4(),
                tenant_id=key[0],
                agent_name=key[1],
                agent_version=key[2],
                revision=revision,
                spec=spec,
                spec_sha256=spec_sha256,
                actor_id=actor_id,
                created_at=_now(),
            )
        )
        return revision

    async def create(
        self,
        *,
        tenant_id: UUID,
        spec: AgentSpec,
        spec_sha256: str,
        created_by: str,
    ) -> AgentSpecRecord:
        name = spec.metadata.name
        version = spec.metadata.version
        async with self._lock:
            key = (tenant_id, name, version)
            if key in self._rows:
                raise DuplicateAgentSpecError(tenant_id=tenant_id, name=name, version=version)
            now = _now()
            record = AgentSpecRecord(
                id=uuid4(),
                tenant_id=tenant_id,
                name=name,
                version=version,
                spec=spec,
                spec_sha256=spec_sha256,
                status=AgentSpecStatus.ACTIVE,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            self._rows[key] = record
            self._append_revision(key, spec=spec, spec_sha256=spec_sha256, actor_id=created_by)
            return record

    async def get(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        include_deleted: bool = False,
    ) -> AgentSpecRecord | None:
        async with self._lock:
            record = self._rows.get((tenant_id, name, version))
        if record is None:
            return None
        if not include_deleted and record.status is AgentSpecStatus.DELETED:
            return None
        return record

    async def list_by_tenant(
        self,
        *,
        tenant_id: UUID,
        status: AgentSpecStatus | None = None,
        name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentSpecRecord]:
        async with self._lock:
            matched = [
                r
                for r in self._rows.values()
                if r.tenant_id == tenant_id
                and (status is None or r.status is status)
                and (name is None or r.name == name)
            ]
        matched.sort(key=lambda r: r.created_at, reverse=True)
        return matched[offset : offset + limit]

    async def list_distinct_active_by_tenant(
        self, *, tenant_id: UUID, limit: int = 100, offset: int = 0
    ) -> list[AgentSpecRecord]:
        async with self._lock:
            active = [
                r
                for r in self._rows.values()
                if r.tenant_id == tenant_id and r.status is AgentSpecStatus.ACTIVE
            ]
        by_name: dict[str, AgentSpecRecord] = {}
        for r in active:
            current = by_name.get(r.name)
            # tie-break: created_at DESC, id DESC — must match the SQL
            # backend's ORDER BY name, created_at DESC, id DESC exactly (see
            # base.py docstring), or the two stores can pick a different
            # "newest" row on a created_at tie.
            if current is None or (r.created_at, r.id) > (current.created_at, current.id):
                by_name[r.name] = r
        ordered = sorted(by_name.values(), key=lambda r: r.name)
        return ordered[offset : offset + limit]

    async def count_distinct_active_by_tenant(self, *, tenant_id: UUID) -> int:
        async with self._lock:
            names = {
                r.name
                for r in self._rows.values()
                if r.tenant_id == tenant_id and r.status is AgentSpecStatus.ACTIVE
            }
        return len(names)

    async def list_all_tenants(
        self,
        *,
        status: AgentSpecStatus | None = None,
        name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentSpecRecord]:
        # Stream N — no tenant_id filter; relies on the caller wrapping
        # the call in ``bypass_rls_session()`` for SQL stores.
        async with self._lock:
            matched = [
                r
                for r in self._rows.values()
                if (status is None or r.status is status) and (name is None or r.name == name)
            ]
        matched.sort(key=lambda r: r.created_at, reverse=True)
        return matched[offset : offset + limit]

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
        async with self._lock:
            key = (tenant_id, name, version)
            existing = self._rows.get(key)
            if existing is None or existing.status is AgentSpecStatus.DELETED:
                return None
            prev_sha = existing.spec_sha256
            if prev_sha == spec_sha256:
                # No-op: identical content, nothing changes, nothing recorded.
                return AgentSpecUpdateResult(record=existing, revision=None, prev_sha256=prev_sha)
            replaced = existing.model_copy(
                update={
                    "spec": spec,
                    "spec_sha256": spec_sha256,
                    "updated_at": _now(),
                }
            )
            self._rows[key] = replaced
            revision = self._append_revision(
                key, spec=spec, spec_sha256=spec_sha256, actor_id=updated_by
            )
            return AgentSpecUpdateResult(record=replaced, revision=revision, prev_sha256=prev_sha)

    async def list_revisions(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentSpecRevisionRecord]:
        async with self._lock:
            history = list(self._revisions.get((tenant_id, name, version), []))
        history.sort(key=lambda r: r.revision, reverse=True)
        return history[offset : offset + limit]

    async def get_revision(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        revision: int,
    ) -> AgentSpecRevisionRecord | None:
        async with self._lock:
            history = self._revisions.get((tenant_id, name, version), [])
            return next((r for r in history if r.revision == revision), None)

    async def update_status(
        self,
        *,
        tenant_id: UUID,
        name: str,
        version: str,
        status: AgentSpecStatus,
    ) -> AgentSpecRecord | None:
        async with self._lock:
            key = (tenant_id, name, version)
            existing = self._rows.get(key)
            if existing is None:
                return None
            replaced = existing.model_copy(update={"status": status, "updated_at": _now()})
            self._rows[key] = replaced
            return replaced
