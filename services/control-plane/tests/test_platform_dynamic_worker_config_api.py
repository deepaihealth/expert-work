"""Endpoint tests for ``/v1/platform/dynamic-worker-config`` — B3 PR2.

Mirrors ``test_platform_tool_budget_config_api.py``: the full ``create_app``
harness wires ``platform_dynamic_worker_config_service`` onto ``app.state``; a
system_admin role_binding is seeded; principal via JWT.
"""

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
from tests.auth_fixtures import make_test_jwt


async def _seed_admin(app: object) -> UUID:
    sys_admin_id = uuid4()
    await app.state.role_binding_repo.create(  # type: ignore[attr-defined]
        subject_type="user",
        subject_id=sys_admin_id,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="seed",
    )
    return sys_admin_id


def _headers(subject: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4(), subject=str(subject))}"}


def _body(**overrides: int) -> dict[str, int]:
    """A valid six-field write payload(弹性 worker 预算:default 档 + cap 档)。"""
    return {
        "max_concurrent": 5,
        "max_per_run": 32,
        "max_iterations": 48,
        "cap_max_concurrent": 10,
        "cap_max_per_run": 64,
        "cap_max_iterations": 128,
        **overrides,
    }


@pytest.fixture
async def admin_client(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> AsyncIterator[tuple[AsyncClient, UUID]]:
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    sys_admin_id = await _seed_admin(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        yield client, sys_admin_id


@pytest.mark.asyncio
async def test_non_admin_forbidden(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, _ = admin_client
    resp = await client.get("/v1/platform/dynamic-worker-config", headers=_headers(uuid4()))
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "PLATFORM_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_put_non_admin_forbidden(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, _ = admin_client
    resp = await client.put(
        "/v1/platform/dynamic-worker-config",
        headers=_headers(uuid4()),
        json=_body(),
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "PLATFORM_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_get_unset_uses_env_default(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    resp = await client.get("/v1/platform/dynamic-worker-config", headers=_headers(admin))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {
        "configured": None,
        "effective": {
            "max_concurrent": 3,
            "max_per_run": 16,
            "max_iterations": 32,
            "cap_max_concurrent": 10,
            "cap_max_per_run": 64,
            "cap_max_iterations": 128,
        },
    }


@pytest.mark.asyncio
async def test_put_then_get_reflects(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    put_resp = await client.put(
        "/v1/platform/dynamic-worker-config",
        headers=_headers(admin),
        json=_body(),
    )
    assert put_resp.status_code == 200, put_resp.text
    expected = _body()
    assert put_resp.json()["data"] == {"configured": expected, "effective": expected}

    get_resp = await client.get("/v1/platform/dynamic-worker-config", headers=_headers(admin))
    assert get_resp.json()["data"] == {"configured": expected, "effective": expected}


@pytest.mark.asyncio
async def test_put_rejects_out_of_bounds(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/dynamic-worker-config",
        headers=_headers(admin),
        json=_body(cap_max_per_run=1025),
    )
    assert resp.status_code == 422

    resp = await client.put(
        "/v1/platform/dynamic-worker-config",
        headers=_headers(admin),
        json=_body(max_iterations=0),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_rejects_default_above_cap(admin_client: tuple[AsyncClient, UUID]) -> None:
    """default 档不得超过 cap 档 —— 否则「未配置的 agent」反而拿到超顶预算。"""
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/dynamic-worker-config",
        headers=_headers(admin),
        json=_body(max_iterations=200, cap_max_iterations=128),
    )
    assert resp.status_code == 422
    resp = await client.put(
        "/v1/platform/dynamic-worker-config",
        headers=_headers(admin),
        json=_body(max_concurrent=12, cap_max_concurrent=10),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_rejects_unknown_field(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/dynamic-worker-config",
        headers=_headers(admin),
        json=_body(bogus=1),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_emits_audit(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/dynamic-worker-config",
        headers=_headers(admin),
        json=_body(),
    )
    assert resp.status_code == 200, resp.text
    store = client._transport.app.state.audit_logger._store  # type: ignore[attr-defined]
    entries = list(store._rows.values())
    matched = [e for e in entries if e.action.value == "platform_dynamic_worker_config:updated"]
    assert matched, "expected a PLATFORM_DYNAMIC_WORKER_UPDATED audit row"
    assert matched[0].details == _body()


# ─── PR-E3b — invalidation-bus broadcast on the write endpoint ─────────────


class _SpyBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)

    def publish_soon(self, event: object) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_put_broadcasts_platform_dynamic_worker(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    """``put`` self-invalidates THIS pod's cache; the endpoint must also
    broadcast ``platform_dynamic_worker`` so peer replicas drop theirs."""
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    spy_bus = _SpyBus()
    app.state.invalidation_bus = spy_bus
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        resp = await client.put(
            "/v1/platform/dynamic-worker-config",
            headers=_headers(admin),
            json=_body(),
        )
    assert resp.status_code == 200, resp.text
    assert len(spy_bus.events) == 1
    assert spy_bus.events[0].kind == "platform_dynamic_worker"  # type: ignore[attr-defined]
