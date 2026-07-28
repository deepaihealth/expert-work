"""Abstract :class:`PlatformDelegationConfigStore` — perf phase2 PR3.

Single-row singleton storing the platform-global delegation-gate capacity:
``max_concurrent_delegations``. Tenant-less (platform-global), so SQL callers
MUST be inside ``bypass_rls_session()`` — no per-tenant RLS scope, exactly
like ``platform_tool_budget_config``.

An absent row means "not configured" → the platform falls back to its
built-in default.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformDelegationConfigRow:
    """The platform's delegation-gate capacity (non-secret)."""

    max_concurrent_delegations: int
    updated_by: str | None


class PlatformDelegationConfigStore(abc.ABC):
    """Persistence Protocol for the single-row platform delegation config."""

    @abc.abstractmethod
    async def get(self) -> PlatformDelegationConfigRow | None:
        """The singleton row, or None if not configured. SQL callers bypass RLS."""

    @abc.abstractmethod
    async def put(self, *, max_concurrent_delegations: int, updated_by: str | None) -> None:
        """Upsert the singleton row (last write wins). SQL callers bypass RLS."""
