"""Unit tests for :class:`RedisQuotaService` — B-19 dimension routing.

The real Lua bucket needs a live Redis (integration scope); these tests
pin the Python side with a scripted fake client: which bucket keys a
``check`` evaluates (dimension routing) and how the Lua reply — including
the ``retry_ms = -1`` sticky sentinel — maps onto :class:`CheckResult`.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from control_plane.quota import RedisQuotaService
from expert_work.persistence.quota import (
    InMemoryTenantQuotaStore,
    InMemoryTokenReservationStore,
)
from expert_work.protocol import CheckRequest, QuotaDimension, TenantQuotaPatch


class _ScriptedRedis:
    """Fake async Redis: records every ``evalsha`` call and returns a
    scripted ``{allowed, retry_ms, remaining}`` triple per key substring."""

    def __init__(
        self,
        results: dict[str, tuple[int, int, int]] | None = None,
        default: tuple[int, int, int] = (1, 0, 99),
    ) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self._results = results or {}
        self._default = default

    async def script_load(self, source: str) -> str:
        return "test-sha"

    async def evalsha(self, sha: str, numkeys: int, key: str, *argv: str) -> list[int]:
        self.calls.append((key, [str(a) for a in argv]))
        for fragment, triple in self._results.items():
            if fragment in key:
                return list(triple)
        return list(self._default)


def _service(redis: _ScriptedRedis, store: InMemoryTenantQuotaStore) -> RedisQuotaService:
    return RedisQuotaService(
        redis_client=redis,  # type: ignore[arg-type]
        quota_store=store,
        reservation_store=InMemoryTokenReservationStore(),
    )


async def _seed(store: InMemoryTenantQuotaStore, tenant: UUID, patch: TenantQuotaPatch) -> None:
    await store.upsert(tenant_id=tenant, patch=patch, updated_by="test")


async def _seed_qps_and_image(store: InMemoryTenantQuotaStore, tenant: UUID) -> None:
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(dimension=QuotaDimension.QPS, scope={}, limit_value=10, burst=10),
    )
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.IMAGE_UPLOAD_COUNT_30D, scope={}, limit_value=3, burst=3
        ),
    )
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.IMAGE_STORAGE_BYTES, scope={}, limit_value=1024, burst=None
        ),
    )


@pytest.mark.asyncio
async def test_redis_check_skips_foreign_dimensions_for_session() -> None:
    """B-19 ① — a ``resource_kind="session"`` check must only evaluate the
    QPS bucket; the image buckets are never touched in Redis."""
    tenant = uuid4()
    store = InMemoryTenantQuotaStore()
    await _seed_qps_and_image(store, tenant)
    redis = _ScriptedRedis()
    svc = _service(redis, store)

    result = await svc.check(CheckRequest(tenant_id=tenant, cost=1, resource_kind="session"))

    assert result.allowed
    keys = [key for key, _argv in redis.calls]
    assert len(keys) == 1
    assert "qps" in keys[0]
    assert not any("img_" in key for key in keys)


@pytest.mark.asyncio
async def test_redis_check_image_upload_hits_all_its_buckets() -> None:
    """B-19 regression guard — ``resource_kind="image_upload"`` still
    evaluates QPS + count + bytes."""
    tenant = uuid4()
    store = InMemoryTenantQuotaStore()
    await _seed_qps_and_image(store, tenant)
    redis = _ScriptedRedis()
    svc = _service(redis, store)

    result = await svc.check(
        CheckRequest(
            tenant_id=tenant,
            cost=1,
            resource_kind="image_upload",
            cost_overrides={QuotaDimension.IMAGE_STORAGE_BYTES: 600},
        )
    )

    assert result.allowed
    keys = [key for key, _argv in redis.calls]
    assert len(keys) == 3
    assert any("qps" in key for key in keys)
    assert any("img_count_30d" in key for key in keys)
    assert any("img_bytes" in key for key in keys)


@pytest.mark.asyncio
async def test_redis_check_resource_kind_none_hits_every_bucket() -> None:
    """Compat — direct callers without ``resource_kind`` keep evaluating
    every configured dimension."""
    tenant = uuid4()
    store = InMemoryTenantQuotaStore()
    await _seed_qps_and_image(store, tenant)
    redis = _ScriptedRedis()
    svc = _service(redis, store)

    result = await svc.check(CheckRequest(tenant_id=tenant, cost=1))

    assert result.allowed
    assert len(redis.calls) == 3


@pytest.mark.asyncio
async def test_redis_sticky_denial_maps_negative_retry_to_none() -> None:
    """B-19 ② — the Lua sticky sentinel ``retry_ms = -1`` (refill=0, no
    meaningful retry) maps to ``retry_after_s=None``."""
    tenant = uuid4()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.IMAGE_STORAGE_BYTES, scope={}, limit_value=100, burst=None
        ),
    )
    redis = _ScriptedRedis(results={"img_bytes": (0, -1, 0)})
    svc = _service(redis, store)

    denied = await svc.check(
        CheckRequest(
            tenant_id=tenant,
            cost=1,
            resource_kind="image_upload",
            cost_overrides={QuotaDimension.IMAGE_STORAGE_BYTES: 200},
        )
    )

    assert not denied.allowed
    assert denied.blocked_dimension is QuotaDimension.IMAGE_STORAGE_BYTES
    assert denied.retry_after_s is None
    # The sticky row hands the Lua script a zero refill rate — the guard
    # in the script keys off exactly this argv. Assert the value, not its
    # formatting: B-32 changed the wire unit from ``int(rate * 1000)`` to
    # the unscaled float, so the same zero now spells "0.0".
    (_key, argv) = redis.calls[0]
    assert float(argv[1]) == 0.0


@pytest.mark.asyncio
async def test_redis_refill_denial_maps_retry_ms_to_seconds() -> None:
    """Non-regression — refillable dimensions keep the ceil(ms→s) mapping."""
    tenant = uuid4()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(dimension=QuotaDimension.QPS, scope={}, limit_value=1, burst=1),
    )
    redis = _ScriptedRedis(results={"qps": (0, 1500, 0)})
    svc = _service(redis, store)

    denied = await svc.check(CheckRequest(tenant_id=tenant, cost=1, resource_kind="session"))

    assert not denied.allowed
    assert denied.blocked_dimension is QuotaDimension.QPS
    assert denied.retry_after_s == 2
