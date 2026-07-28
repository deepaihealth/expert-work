"""Unit tests for :class:`PlatformDelegationConfigService` — perf phase2 PR3.

DB-wins over the constructor-injected ``env_default``; TTL-cached with
``invalidate()`` on write for immediate effect on the writing instance.
"""

from __future__ import annotations

import pytest

from control_plane.platform_delegation_config import (
    DelegationConfig,
    PlatformDelegationConfigService,
)
from expert_work.persistence.platform_delegation_config import (
    InMemoryPlatformDelegationConfigStore,
)

_ENV_DEFAULT = DelegationConfig(16)


def _service() -> PlatformDelegationConfigService:
    # ttl 0 ⇒ every read reloads, so writes are visible without invalidate races.
    return PlatformDelegationConfigService(
        store=InMemoryPlatformDelegationConfigStore(),
        env_default=_ENV_DEFAULT,
        ttl_seconds=0.0,
    )


@pytest.mark.asyncio
async def test_unset_uses_env_default() -> None:
    svc = _service()
    assert await svc.effective() == _ENV_DEFAULT
    assert await svc.configured() is None


@pytest.mark.asyncio
async def test_db_row_wins_over_env() -> None:
    svc = _service()
    await svc.put(max_concurrent_delegations=5, updated_by="admin")
    expected = DelegationConfig(5)
    assert await svc.effective() == expected
    assert await svc.configured() == expected


@pytest.mark.asyncio
async def test_put_invalidates_cache() -> None:
    # Long TTL: only invalidate-on-write makes the new value visible.
    svc = PlatformDelegationConfigService(
        store=InMemoryPlatformDelegationConfigStore(),
        env_default=_ENV_DEFAULT,
        ttl_seconds=9999.0,
    )
    assert await svc.effective() == _ENV_DEFAULT  # warm the cache (env default)
    await svc.put(max_concurrent_delegations=5, updated_by="admin")
    assert await svc.effective() == DelegationConfig(5)  # invalidate made it visible
