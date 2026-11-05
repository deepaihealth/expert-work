"""Abstract :class:`AgentInstanceStore` — Stream Agent-Templates (M1-5b).

CRUD over per-(tenant, agent_code, end-user) bindings. Tenant-scoped (RLS); every
method takes ``tenant_id`` and the caller runs inside the tenant's RLS scope.
"""

from __future__ import annotations

import abc
from uuid import UUID

from expert_work.protocol import AgentInstanceRecord


class AgentInstanceStore(abc.ABC):
    """Per-user agent-instance bindings."""

    @abc.abstractmethod
    async def touch(
        self, *, tenant_id: UUID, agent_code: str, user_id: UUID
    ) -> AgentInstanceRecord:
        """Idempotent upsert keyed by ``(tenant_id, agent_code, user_id)``: create
        the binding on first use, bump ``last_active_at`` otherwise. Returns it."""

    @abc.abstractmethod
    async def get(
        self, *, tenant_id: UUID, agent_code: str, user_id: UUID
    ) -> AgentInstanceRecord | None:
        """Return one binding, or None if absent."""

    @abc.abstractmethod
    async def list_by_agent(
        self, *, tenant_id: UUID, agent_code: str, limit: int = 100, offset: int = 0
    ) -> list[AgentInstanceRecord]:
        """End-users bound to one agent, most-recently-active first."""

    @abc.abstractmethod
    async def list_by_user(
        self, *, tenant_id: UUID, user_id: UUID, limit: int = 100, offset: int = 0
    ) -> list[AgentInstanceRecord]:
        """Agents one end-user uses, most-recently-active first."""

    @abc.abstractmethod
    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Phase 3a (purge_user) — hard-delete EVERY instance binding for a user.

        Removes all of the user's ``agent_instance`` rows and returns the count
        deleted (0 when none / re-purge). Tenant- AND user-scoped — the
        ``(tenant_id, user_id)`` predicate never touches another tenant's or
        user's bindings."""

    @abc.abstractmethod
    async def count_for_agent(self, *, tenant_id: UUID, agent_code: str) -> int:
        """B-278 —— how many end-users are bound to ``agent_code`` (purge preview)."""

    @abc.abstractmethod
    async def delete_for_agent(self, *, tenant_id: UUID, agent_code: str) -> int:
        """B-278 —— hard-delete every binding to ``agent_code`` (agent purge).

        Tenant-scoped; returns the rows removed (0 on a re-run)."""
