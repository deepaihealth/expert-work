"""``secret_allowlist`` rows of one agent — the control-plane side (B-278).

The credential-proxy owns the read / add path of this table
(``credential_proxy.allowlist``). The agent purge only needs to count and drop
an agent's rows: they are keyed by ``(tenant, agent_name, agent_version,
secret_ref)``, so rows left behind would let a new agent created under the
same name inherit the purged agent's secret grants.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.bulk_delete import count_where, delete_where
from expert_work.persistence.models import SecretAllowlistRow


class AgentSecretAllowlistStore(abc.ABC):
    """Count / delete one agent's ``secret_allowlist`` rows (every version)."""

    @abc.abstractmethod
    async def count_for_agent(self, *, tenant_id: UUID, agent_name: str) -> int:
        """Rows :meth:`delete_for_agent` would remove (purge preview)."""

    @abc.abstractmethod
    async def delete_for_agent(self, *, tenant_id: UUID, agent_name: str) -> int:
        """Drop every row of ``agent_name`` (all versions); tenant-scoped."""


@dataclass
class InMemoryAgentSecretAllowlistStore(AgentSecretAllowlistStore):
    """Tests / ``persistence_backend="memory"``: ``(tenant, name, version, ref)`` keys."""

    rows: set[tuple[UUID, str, str, str]] = field(default_factory=set)

    def add(self, *, tenant_id: UUID, agent_name: str, agent_version: str, secret_ref: str) -> None:
        self.rows.add((tenant_id, agent_name, agent_version, secret_ref))

    async def count_for_agent(self, *, tenant_id: UUID, agent_name: str) -> int:
        return sum(1 for r in self.rows if r[0] == tenant_id and r[1] == agent_name)

    async def delete_for_agent(self, *, tenant_id: UUID, agent_name: str) -> int:
        victims = {r for r in self.rows if r[0] == tenant_id and r[1] == agent_name}
        self.rows -= victims
        return len(victims)


class SqlAgentSecretAllowlistStore(AgentSecretAllowlistStore):
    """Postgres-backed; ``WHERE tenant_id`` is the isolation (runtime role bypasses RLS)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def count_for_agent(self, *, tenant_id: UUID, agent_name: str) -> int:
        return await count_where(
            self._sf,
            SecretAllowlistRow,
            SecretAllowlistRow.tenant_id == tenant_id,
            SecretAllowlistRow.agent_name == agent_name,
        )

    async def delete_for_agent(self, *, tenant_id: UUID, agent_name: str) -> int:
        return await delete_where(
            self._sf,
            SecretAllowlistRow,
            SecretAllowlistRow.tenant_id == tenant_id,
            SecretAllowlistRow.agent_name == agent_name,
        )


__all__ = [
    "AgentSecretAllowlistStore",
    "InMemoryAgentSecretAllowlistStore",
    "SqlAgentSecretAllowlistStore",
]
