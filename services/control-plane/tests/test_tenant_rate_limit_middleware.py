"""HTTP tests for :class:`TenantRateLimitMiddleware` — Stream C.6."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from redis.exceptions import RedisError

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.middleware.rate_limit import _rate_limit_backend_errors
from control_plane.ratelimit import InProcessTokenBucketLimiter, RateLimitDecision, RateLimiter
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditAction, AuditQuery
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier, make_test_jwt

_TENANT = DEFAULT_DEV_TENANT_ID


@dataclass
class _RedisDownLimiter:
    """Simulates a Redis-backed limiter whose backend is unreachable."""

    calls: list[tuple[str, str]]

    async def acquire(
        self,
        *,
        dimension: str,
        key: str,
        capacity: int | None = None,
        refill_per_sec: float | None = None,
    ) -> RateLimitDecision:
        self.calls.append((dimension, key))
        raise RedisError("connection refused")


@pytest.fixture
def audit_store() -> InMemoryAuditLogStore:
    return InMemoryAuditLogStore()


def _settings(*, tenant_rate_limit_enabled: bool = True) -> Settings:
    return Settings(
        env="dev",
        auth_mode="dev",
        # Gateway tier kept wide open so it doesn't interfere with the
        # per-tenant tests.
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        # Tenant tier is what the tests exercise — concrete values
        # injected per test via the ``tenant_rate_limiter`` kwarg.
        tenant_rate_limit_enabled=tenant_rate_limit_enabled,
        tenant_rate_limit_capacity=10_000,
        tenant_rate_limit_refill_per_sec=10_000.0,
        tenant_rate_limit_audit_sample_every=1,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


def _build_client(
    *,
    audit_store: InMemoryAuditLogStore,
    tenant_limiter: RateLimiter,
    enabled: bool = True,
) -> AsyncClient:
    settings = _settings(tenant_rate_limit_enabled=enabled)
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
        tenant_rate_limiter=tenant_limiter,
        enable_reaper=False,
    )
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://control-plane.test")


# ---------------------------------------------------------------------------
# Happy path / exemption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticated_request_passes_with_room_in_bucket(
    audit_store: InMemoryAuditLogStore,
) -> None:
    limiter = InProcessTokenBucketLimiter(capacity=10, refill_per_sec=10.0)
    async with _build_client(audit_store=audit_store, tenant_limiter=limiter) as client:
        token = make_test_jwt(tenant_id=_TENANT)
        resp = await client.get("/v1/agents", headers={"Authorization": f"Bearer {token}"})
        # 200 OK (empty list) — admin role has agent:read.
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health_path_bypasses_tenant_limiter(
    audit_store: InMemoryAuditLogStore,
) -> None:
    """Even a fully-drained bucket lets /healthz/* through."""
    limiter = InProcessTokenBucketLimiter(capacity=1, refill_per_sec=0.001)
    # Drain via a separate call so /healthz hits an empty bucket if it
    # were inside the limit ring (it isn't — exempt prefix).
    await limiter.acquire(dimension="tenant", key="anything")
    async with _build_client(audit_store=audit_store, tenant_limiter=limiter) as client:
        resp = await client.get("/healthz/live")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_disabled_middleware_is_a_no_op(audit_store: InMemoryAuditLogStore) -> None:
    """With ``tenant_rate_limit_enabled=False`` the limiter is never consulted."""
    limiter = InProcessTokenBucketLimiter(capacity=1, refill_per_sec=0.001)
    async with _build_client(
        audit_store=audit_store, tenant_limiter=limiter, enabled=False
    ) as client:
        token = make_test_jwt(tenant_id=_TENANT)
        # 50 requests, all should pass.
        for _ in range(50):
            resp = await client.get("/v1/agents", headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code == 200


@pytest.mark.asyncio
async def test_redis_error_fails_open_and_counts_backend_error(
    audit_store: InMemoryAuditLogStore,
) -> None:
    """subsystems/16 § 5.2 — a Redis outage on the tenant tier must not become
    an HTTP outage: the request is allowed through (fail OPEN) and the
    backend-error counter records the degradation for on-call."""
    limiter = _RedisDownLimiter(calls=[])
    before = _rate_limit_backend_errors.labels(backend="tenant")._value.get()  # type: ignore[attr-defined]
    async with _build_client(audit_store=audit_store, tenant_limiter=limiter) as client:
        token = make_test_jwt(tenant_id=_TENANT)
        resp = await client.get("/v1/agents", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert limiter.calls  # the limiter was actually invoked, not skipped
    after = _rate_limit_backend_errors.labels(backend="tenant")._value.get()  # type: ignore[attr-defined]
    assert after == before + 1


# ---------------------------------------------------------------------------
# Denial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_burst_exhaustion_returns_429_with_retry_after(
    audit_store: InMemoryAuditLogStore,
) -> None:
    # capacity=1, slow refill so the second hit is firmly denied.
    limiter = InProcessTokenBucketLimiter(capacity=1, refill_per_sec=0.01)
    async with _build_client(audit_store=audit_store, tenant_limiter=limiter) as client:
        token = make_test_jwt(tenant_id=_TENANT)

        first = await client.get("/v1/agents", headers={"Authorization": f"Bearer {token}"})
        assert first.status_code == 200

        denied = await client.get("/v1/agents", headers={"Authorization": f"Bearer {token}"})
        assert denied.status_code == 429
        body = denied.json()
        assert body["success"] is False
        assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
        assert body["error"]["dimension"] == "tenant"
        assert body["error"]["retry_after_s"] >= 1
        assert denied.headers["Retry-After"] == str(body["error"]["retry_after_s"])


@pytest.mark.asyncio
async def test_denial_emits_sampled_audit(audit_store: InMemoryAuditLogStore) -> None:
    """``tenant_rate_limit_audit_sample_every=1`` → every denial audits."""
    limiter = InProcessTokenBucketLimiter(capacity=1, refill_per_sec=0.01)
    async with _build_client(audit_store=audit_store, tenant_limiter=limiter) as client:
        token = make_test_jwt(tenant_id=_TENANT)
        first = await client.get("/v1/agents", headers={"Authorization": f"Bearer {token}"})
        assert first.status_code == 200
        denied = await client.get("/v1/agents", headers={"Authorization": f"Bearer {token}"})
        assert denied.status_code == 429

    page = await audit_store.query(AuditQuery(tenant_id=_TENANT))
    matches = [
        e
        for e in page.entries
        if e.action is AuditAction.QUOTA_RATE_LIMIT_DENIED
        and e.details.get("dimension") == "tenant"
    ]
    assert matches, f"no tenant-dim denial audit; saw: {[e.action.value for e in page.entries]}"


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_buckets_are_isolated(audit_store: InMemoryAuditLogStore) -> None:
    """Drained tenant A bucket does not throttle tenant B."""
    limiter = InProcessTokenBucketLimiter(capacity=1, refill_per_sec=0.01)
    other_tenant = uuid4()
    async with _build_client(audit_store=audit_store, tenant_limiter=limiter) as client:
        token_a = make_test_jwt(tenant_id=_TENANT)
        token_b = make_test_jwt(tenant_id=other_tenant)

        # Drain tenant A.
        first_a = await client.get("/v1/agents", headers={"Authorization": f"Bearer {token_a}"})
        assert first_a.status_code == 200
        denied_a = await client.get("/v1/agents", headers={"Authorization": f"Bearer {token_a}"})
        assert denied_a.status_code == 429

        # Tenant B still has a full bucket.
        first_b = await client.get("/v1/agents", headers={"Authorization": f"Bearer {token_b}"})
        assert first_b.status_code == 200


@pytest.mark.asyncio
async def test_tenant_override_tightens_limit(audit_store: InMemoryAuditLogStore) -> None:
    """Stream C.6 — a per-tenant rate_limit_override caps a wide-open limiter.

    Default tenant tier is wide (10k), but the seeded tenant config overrides to
    burst=1, so the second request in quick succession is 429'd.
    """
    from expert_work.persistence.tenant_config import InMemoryTenantConfigStore
    from expert_work.protocol.tenant_config import TenantConfigPatch

    repo = InMemoryTenantConfigStore()
    await repo.upsert(
        tenant_id=_TENANT,
        patch=TenantConfigPatch(
            display_name="T",
            rate_limit_override={"requests_per_minute": 60, "burst": 1},
        ),
        actor_id="seed",
    )
    limiter = InProcessTokenBucketLimiter(capacity=10_000, refill_per_sec=10_000.0)
    app = create_app(
        settings=_settings(),
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
        tenant_rate_limiter=limiter,
        tenant_config_repo=repo,
        enable_reaper=False,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT)}"}
        r1 = await client.get("/v1/agents", headers=headers)
        r2 = await client.get("/v1/agents", headers=headers)
        assert r1.status_code == 200  # override burst=1 → first allowed
        assert r2.status_code == 429  # ...second drained the 1-token override bucket
