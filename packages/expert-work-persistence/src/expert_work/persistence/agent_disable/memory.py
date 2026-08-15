"""In-memory :class:`AgentDisableStore` — Stream RT-4 (RT-ADR-16)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from expert_work.persistence.agent_disable.base import AgentDisableStore
from expert_work.protocol import AgentDisableRecord


def _now() -> datetime:
    return datetime.now(tz=UTC)


class InMemoryAgentDisableStore(AgentDisableStore):
    """Single-dict store keyed on ``(tenant_id, agent_name)``; lock-guarded."""

    def __init__(self) -> None:
        self._rows: dict[tuple[UUID, str], AgentDisableRecord] = {}
        self._lock = asyncio.Lock()

    async def get(self, *, tenant_id: UUID, agent_name: str) -> AgentDisableRecord | None:
        async with self._lock:
            return self._rows.get((tenant_id, agent_name))

    async def set_disabled(
        self,
        *,
        tenant_id: UUID,
        agent_name: str,
        disabled: bool,
        reason: str | None,
        disabled_by: str | None,
    ) -> AgentDisableRecord:
        now = _now()
        async with self._lock:
            record = AgentDisableRecord(
                tenant_id=tenant_id,
                agent_name=agent_name,
                disabled=disabled,
                # Enable clears the disable metadata; disable stamps it.
                reason=reason if disabled else None,
                disabled_by=disabled_by if disabled else None,
                disabled_at=now if disabled else None,
                updated_at=now,
            )
            self._rows[(tenant_id, agent_name)] = record
            return record

    async def list_disabled_names(self, *, tenant_id: UUID) -> set[str]:
        async with self._lock:
            return {
                name
                for (row_tenant, name), record in self._rows.items()
                if row_tenant == tenant_id and record.disabled
            }
