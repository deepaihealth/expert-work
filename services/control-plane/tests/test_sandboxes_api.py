"""Endpoint tests for ``POST /v1/sandboxes/reap`` — Stream P (Mini-ADR P-14)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.auth import JWTVerifier
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.protocol import Role
from orchestrator.tools.sandbox import RecordingSandboxRuntime
from tests.auth_fixtures import make_test_jwt


def _headers(subject: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4(), subject=str(subject))}"}


@pytest.fixture
async def app_client(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> AsyncIterator[tuple[AsyncClient, object, UUID]]:
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    sys_admin_id = uuid4()
    await app.state.role_binding_repo.create(
        subject_type="user",
        subject_id=sys_admin_id,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        yield client, app, sys_admin_id


@pytest.mark.asyncio
async def test_non_admin_cannot_reap(app_client: tuple[AsyncClient, object, UUID]) -> None:
    client, app, _ = app_client
    app.state.sandbox_runtime = RecordingSandboxRuntime(reap_count=2)  # type: ignore[attr-defined]
    resp = await client.post("/v1/sandboxes/reap?force=true", headers=_headers(uuid4()))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reap_503_when_no_supervisor(app_client: tuple[AsyncClient, object, UUID]) -> None:
    client, app, admin = app_client
    app.state.sandbox_runtime = None  # type: ignore[attr-defined]
    resp = await client.post("/v1/sandboxes/reap?force=true", headers=_headers(admin))
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "SANDBOX_SUPERVISOR_UNCONFIGURED"


@pytest.mark.asyncio
async def test_system_admin_reap_returns_count(
    app_client: tuple[AsyncClient, object, UUID],
) -> None:
    client, app, admin = app_client
    fake = RecordingSandboxRuntime(reap_count=3)
    app.state.sandbox_runtime = fake  # type: ignore[attr-defined]
    resp = await client.post("/v1/sandboxes/reap?force=true", headers=_headers(admin))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["reaped_count"] == 3
    assert fake.reaped == [True]  # force propagated to the sandbox runtime
