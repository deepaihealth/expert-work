"""Integration test for :class:`RedisQuotaService` — real Redis, real Lua.

B-32. Every existing quota test runs the **in-memory** engine, so the Lua
bucket's arithmetic had never been executed against a clock. It was wrong:
the script scaled the refill rate by 1000 (``rate_milli``) but kept
capacity / tokens / cost in whole tokens, so a bucket refilled 1000x too
fast. The retry formula carried the same factor, which made the two
self-consistent — nothing an assertion on ``retry_after_s`` alone could
catch.

The pair that pins it down:

* ``test_bucket_does_not_refill_1000x_too_fast`` — the drained bucket must
  still be empty 50 ms later at 1 token/s.
* ``test_bucket_does_refill_at_the_configured_rate`` — and it must have a
  token back after 1.2 s. Without this one, "never refills" would pass.

Plus the truncation half: rates below 1/1000 token per second floored to
``int(rate * 1000) == 0``, which the script reads as the *sticky ceiling*
sentinel — so the slow-drip 30-day dimensions never refilled at all and
reported "no retry will ever succeed".
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
import redis.asyncio as redis_async
from testcontainers.redis import RedisContainer

from control_plane.quota import InMemoryQuotaService, RedisQuotaService
from expert_work.persistence.quota import (
    InMemoryTenantQuotaStore,
    InMemoryTokenReservationStore,
)
from expert_work.protocol import CheckRequest, QuotaDimension, TenantQuotaPatch

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def redis_container() -> Iterator[RedisContainer]:
    with RedisContainer("public.ecr.aws/docker/library/redis:7-alpine") as container:
        yield container


@pytest.fixture
async def redis_client(redis_container: RedisContainer) -> AsyncIterator[redis_async.Redis]:
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    client = redis_async.from_url(
        f"redis://{host}:{port}/0", encoding="utf-8", decode_responses=True
    )
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


async def _seed(store: InMemoryTenantQuotaStore, tenant: object, patch: TenantQuotaPatch) -> None:
    await store.upsert(tenant_id=tenant, patch=patch, updated_by="test")  # type: ignore[arg-type]


def _service(redis: redis_async.Redis, store: InMemoryTenantQuotaStore) -> RedisQuotaService:
    return RedisQuotaService(
        redis_client=redis,
        quota_store=store,
        reservation_store=InMemoryTokenReservationStore(),
    )


# --------------------------------------------------------------- refill rate


@pytest.mark.asyncio
async def test_bucket_does_not_refill_1000x_too_fast(redis_client: redis_async.Redis) -> None:
    """B-32 — 1 token/s means 0.05 tokens in 50 ms, not 50."""
    from uuid import uuid4

    tenant = uuid4()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(dimension=QuotaDimension.QPS, scope={}, limit_value=1, burst=2),
    )
    svc = _service(redis_client, store)
    req = CheckRequest(tenant_id=tenant, cost=1, resource_kind="session")

    assert (await svc.check(req)).allowed
    assert (await svc.check(req)).allowed  # burst of 2 now spent

    await asyncio.sleep(0.05)

    denied = await svc.check(req)
    assert not denied.allowed, "50 ms at 1 token/s must not refill a spent burst"
    assert denied.blocked_dimension is QuotaDimension.QPS


@pytest.mark.asyncio
async def test_bucket_does_refill_at_the_configured_rate(redis_client: redis_async.Redis) -> None:
    """The other side of the same coin — 1 token/s does hand back a token
    after a second. Guards against "fix" the refill by zeroing it."""
    from uuid import uuid4

    tenant = uuid4()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(dimension=QuotaDimension.QPS, scope={}, limit_value=1, burst=2),
    )
    svc = _service(redis_client, store)
    req = CheckRequest(tenant_id=tenant, cost=1, resource_kind="session")

    assert (await svc.check(req)).allowed
    assert (await svc.check(req)).allowed

    await asyncio.sleep(1.2)

    assert (await svc.check(req)).allowed, "1.2 s at 1 token/s owes the bucket a token"
    assert not (await svc.check(req)).allowed, "…but only one"


# ------------------------------------------------------- slow-drip dimensions


@pytest.mark.asyncio
async def test_slow_drip_dimension_is_not_mistaken_for_a_sticky_ceiling(
    redis_client: redis_async.Redis,
) -> None:
    """B-32 second half — 100 uploads / 30 days is 3.9e-5 tokens/s, which
    ``int(rate * 1000)`` floored to 0. Zero is the script's *sticky ceiling*
    sentinel, so the dimension both stopped refilling and started claiming
    no retry could ever succeed."""
    from uuid import uuid4

    tenant = uuid4()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.IMAGE_UPLOAD_COUNT_30D, scope={}, limit_value=100, burst=1
        ),
    )
    svc = _service(redis_client, store)
    req = CheckRequest(tenant_id=tenant, cost=1, resource_kind="image_upload")

    assert (await svc.check(req)).allowed

    denied = await svc.check(req)
    assert not denied.allowed
    assert denied.blocked_dimension is QuotaDimension.IMAGE_UPLOAD_COUNT_30D
    assert denied.retry_after_s is not None, "a refilling dimension always has a retry time"
    # 1 token at 100/(30 days) → 30 days / 100 = 25920 s.
    assert denied.retry_after_s == 25_920


@pytest.mark.asyncio
async def test_sticky_ceiling_stays_sticky(redis_client: redis_async.Redis) -> None:
    """Regression guard — a genuine refill=0 ceiling keeps the sentinel."""
    from uuid import uuid4

    tenant = uuid4()
    store = InMemoryTenantQuotaStore()
    await _seed(
        store,
        tenant,
        TenantQuotaPatch(
            dimension=QuotaDimension.IMAGE_STORAGE_BYTES, scope={}, limit_value=100, burst=None
        ),
    )
    svc = _service(redis_client, store)
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


# ------------------------------------------------------------------- parity


@pytest.mark.asyncio
async def test_redis_and_in_memory_agree_on_the_slow_drip_retry(
    redis_client: redis_async.Redis,
) -> None:
    """The two engines are supposed to be the same predicate written twice.
    The slow-drip retry is where they had drifted furthest apart."""
    from uuid import uuid4

    patch = TenantQuotaPatch(
        dimension=QuotaDimension.IMAGE_UPLOAD_COUNT_30D, scope={}, limit_value=100, burst=1
    )
    req_kwargs = {"cost": 1, "resource_kind": "image_upload"}

    redis_tenant = uuid4()
    redis_store = InMemoryTenantQuotaStore()
    await _seed(redis_store, redis_tenant, patch)
    redis_svc = _service(redis_client, redis_store)
    await redis_svc.check(CheckRequest(tenant_id=redis_tenant, **req_kwargs))  # type: ignore[arg-type]
    redis_denied = await redis_svc.check(CheckRequest(tenant_id=redis_tenant, **req_kwargs))  # type: ignore[arg-type]

    mem_tenant = uuid4()
    mem_store = InMemoryTenantQuotaStore()
    await _seed(mem_store, mem_tenant, patch)
    mem_svc = InMemoryQuotaService(
        quota_store=mem_store, reservation_store=InMemoryTokenReservationStore()
    )
    await mem_svc.check(CheckRequest(tenant_id=mem_tenant, **req_kwargs))  # type: ignore[arg-type]
    mem_denied = await mem_svc.check(CheckRequest(tenant_id=mem_tenant, **req_kwargs))  # type: ignore[arg-type]

    assert redis_denied.allowed == mem_denied.allowed
    assert redis_denied.blocked_dimension == mem_denied.blocked_dimension
    assert redis_denied.retry_after_s == mem_denied.retry_after_s
