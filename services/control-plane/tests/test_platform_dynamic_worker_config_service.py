"""Unit tests for :class:`PlatformDynamicWorkerConfigService` — B3 PR2.

DB-wins over the constructor-injected ``env_default``; TTL-cached with
``invalidate()`` on write for immediate effect on the writing instance.
弹性 worker 预算(2026-08-28)— config carries two tiers: the default
(``max_*``) and the hard cap (``cap_max_*``) a per-agent request clamps to.
"""

from __future__ import annotations

import pytest

from control_plane.platform_dynamic_worker_config import (
    DynamicWorkerConfig,
    PlatformDynamicWorkerConfigService,
)
from expert_work.persistence.platform_dynamic_worker_config import (
    InMemoryPlatformDynamicWorkerConfigStore,
)

_ENV_DEFAULT = DynamicWorkerConfig(
    max_concurrent=3,
    max_per_run=16,
    max_iterations=32,
    cap_max_concurrent=10,
    cap_max_per_run=64,
    cap_max_iterations=128,
)

_DB_VALUE = DynamicWorkerConfig(
    max_concurrent=5,
    max_per_run=32,
    max_iterations=48,
    cap_max_concurrent=8,
    cap_max_per_run=96,
    cap_max_iterations=96,
)


def _put_kwargs(cfg: DynamicWorkerConfig) -> dict[str, int]:
    return {
        "max_concurrent": cfg.max_concurrent,
        "max_per_run": cfg.max_per_run,
        "max_iterations": cfg.max_iterations,
        "cap_max_concurrent": cfg.cap_max_concurrent,
        "cap_max_per_run": cfg.cap_max_per_run,
        "cap_max_iterations": cfg.cap_max_iterations,
    }


def _service() -> PlatformDynamicWorkerConfigService:
    # ttl 0 ⇒ every read reloads, so writes are visible without invalidate races.
    return PlatformDynamicWorkerConfigService(
        store=InMemoryPlatformDynamicWorkerConfigStore(),
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
    await svc.put(**_put_kwargs(_DB_VALUE), updated_by="admin")
    assert await svc.effective() == _DB_VALUE
    assert await svc.configured() == _DB_VALUE


@pytest.mark.asyncio
async def test_put_invalidates_cache() -> None:
    # Long TTL: only invalidate-on-write makes the new value visible.
    svc = PlatformDynamicWorkerConfigService(
        store=InMemoryPlatformDynamicWorkerConfigStore(),
        env_default=_ENV_DEFAULT,
        ttl_seconds=9999.0,
    )
    assert await svc.effective() == _ENV_DEFAULT  # warm the cache (env default)
    await svc.put(**_put_kwargs(_DB_VALUE), updated_by="admin")
    assert await svc.effective() == _DB_VALUE  # invalidate made it visible
