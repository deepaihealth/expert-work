"""In-memory :class:`AgentInstanceStore` — Stream Agent-Templates (M1-5b)."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from datetime import UTC, datetime
from uuid import UUID, uuid4

from expert_work.persistence.agent_instance.base import AgentInstanceStore
from expert_work.protocol import AgentInstanceRecord


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class InMemoryAgentInstanceStore(AgentInstanceStore):
    """Dict-backed bindings keyed by ``(tenant_id, agent_code, user_id)``."""

    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, str, UUID], AgentInstanceRecord] = {}
        self._lock = asyncio.Lock()

    async def touch(
        self, *, tenant_id: UUID, agent_code: str, user_id: UUID
    ) -> AgentInstanceRecord:
        key = (tenant_id, agent_code, user_id)
        async with self._lock:
            now = _utc_now()
            existing = self._rows.get(key)
            if existing is None:
                record = AgentInstanceRecord(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    agent_code=agent_code,
                    user_id=user_id,
                    created_at=now,
                    last_active_at=now,
                )
            else:
                record = existing.model_copy(update={"last_active_at": now})
            self._rows[key] = record
            return record

    async def get(
        self, *, tenant_id: UUID, agent_code: str, user_id: UUID
    ) -> AgentInstanceRecord | None:
        async with self._lock:
            return self._rows.get((tenant_id, agent_code, user_id))

    async def list_by_agent(
        self, *, tenant_id: UUID, agent_code: str, limit: int = 100, offset: int = 0
    ) -> list[AgentInstanceRecord]:
        async with self._lock:
            rows = [
                r
                for r in self._rows.values()
                if r.tenant_id == tenant_id and r.agent_code == agent_code
            ]
        rows.sort(key=lambda r: r.last_active_at, reverse=True)
        return rows[offset : offset + limit]

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        async with self._lock:
            victims = [
                key
                for key, r in self._rows.items()
                if r.tenant_id == tenant_id and r.user_id == user_id
            ]
            for key in victims:
                del self._rows[key]
            return len(victims)

    async def list_by_user(
        self, *, tenant_id: UUID, user_id: UUID, limit: int = 100, offset: int = 0
    ) -> list[AgentInstanceRecord]:
        async with self._lock:
            rows = [
                r for r in self._rows.values() if r.tenant_id == tenant_id and r.user_id == user_id
            ]
        rows.sort(key=lambda r: r.last_active_at, reverse=True)
        return rows[offset : offset + limit]

    async def count_for_agent(self, *, tenant_id: UUID, agent_code: str) -> int:
        async with self._lock:
            return sum(
                1
                for r in self._rows.values()
                if r.tenant_id == tenant_id and r.agent_code == agent_code
            )

    async def delete_for_agent(
        self, *, tenant_id: UUID, agent_code: str, keep_users: Collection[UUID] = ()
    ) -> int:
        keep = set(keep_users)
        async with self._lock:
            victims = [
                key
                for key, r in self._rows.items()
                if r.tenant_id == tenant_id and r.agent_code == agent_code and r.user_id not in keep
            ]
            for key in victims:
                del self._rows[key]
            return len(victims)
