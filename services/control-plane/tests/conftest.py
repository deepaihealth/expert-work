"""Shared fixtures for the Control Plane test suite."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.auth import JWTVerifier
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.common.lifecycle import Lifecycle
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        service_name="control_plane_test",
        env="dev",
        auth_mode="dev",
        default_dev_tenant_id=DEFAULT_DEV_TENANT_ID,
        default_dev_actor_id="anonymous",
        # Avoid touching a real Postgres in unit-only tests.
        db_dsn="postgresql+asyncpg://test@localhost/test",
        # Default fixture buckets are deliberately huge so non-rate-limit
        # tests never trip 429; B.2 tests build their own constrained
        # limiter via ``create_app(rate_limiter=...)``.
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        # C.1 — point at the test issuer / audience used by ``make_test_jwt``.
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


@pytest.fixture
def jwt_verifier() -> JWTVerifier:
    """Verifier wired to the in-process test keypair (no Keycloak)."""
    return build_test_jwt_verifier()


@pytest.fixture
def jwt_factory() -> Callable[..., str]:
    """Return a callable that mints a fresh JWT for the test process.

    Default tenant is ``DEFAULT_DEV_TENANT_ID`` so pre-C.1 tests that
    relied on the dev nil-UUID continue to operate unchanged.
    """

    def _factory(
        *,
        tenant_id: UUID = DEFAULT_DEV_TENANT_ID,
        subject: str = "dev-user",
        roles: tuple[str, ...] = ("admin",),
        ttl_s: int = 3600,
        **kwargs: object,
    ) -> str:
        return make_test_jwt(
            tenant_id=tenant_id,
            subject=subject,
            roles=roles,
            ttl_s=ttl_s,
            **kwargs,
        )

    return _factory


@pytest.fixture
def auth_headers(jwt_factory: Callable[..., str]) -> dict[str, str]:
    """Default JWT bearer headers wired to ``DEFAULT_DEV_TENANT_ID``."""
    return {"Authorization": f"Bearer {jwt_factory()}"}


@pytest.fixture
def lifecycle() -> Lifecycle:
    lc = Lifecycle()
    # B.1 health tests want a service that's already past STARTING; the
    # integration test that exercises the lifespan path drives state
    # transitions explicitly.
    lc.mark_ready()
    return lc


@pytest.fixture
async def client(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
    auth_headers: dict[str, str],
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://control-plane.test",
        headers=auth_headers,
    ) as client:
        yield client


def known_dev_tenant() -> UUID:
    return DEFAULT_DEV_TENANT_ID
