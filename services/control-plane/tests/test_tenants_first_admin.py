"""Endpoint tests for ``POST /v1/tenants`` first-admin provisioning — Stream R W1.

Covers the DB-first compensation flow (Mini-ADR R-4): happy path, the
Keycloak-conflict 409, and the Keycloak-unavailable 502 — asserting in each
case that the local state lands where the design says it should.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.auth import JWTVerifier
from control_plane.keycloak import FakeKeycloakAdminClient, KeycloakUnavailableError
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.protocol import Role
from tests.auth_fixtures import make_test_jwt


class _UnavailableKeycloak(FakeKeycloakAdminClient):
    async def create_user(
        self,
        *,
        email: str,
        tenant_id: UUID,
        display_name: str | None,
        email_verified: bool = False,
    ):
        raise KeycloakUnavailableError("down")


async def _make_client(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
    keycloak,
) -> tuple[AsyncClient, UUID, object]:
    app = create_app(
        settings=settings,
        lifecycle=lifecycle,
        jwt_verifier=jwt_verifier,
        keycloak_admin_client=keycloak,
    )
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
    client = AsyncClient(transport=transport, base_url="http://control-plane.test")
    return client, sys_admin_id, app


def _headers(sys_admin_id: UUID) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4(), subject=str(sys_admin_id))}"
    }


@pytest.fixture
async def fake_kc_app(
    settings: Settings, lifecycle: Lifecycle, jwt_verifier: JWTVerifier
) -> AsyncIterator[tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient]]:
    kc = FakeKeycloakAdminClient()
    client, sys_admin_id, app = await _make_client(settings, lifecycle, jwt_verifier, kc)
    async with client:
        yield client, sys_admin_id, app, kc


@pytest.mark.asyncio
async def test_first_admin_happy_path(
    fake_kc_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, sys_admin_id, app, kc = fake_kc_app
    resp = await client.post(
        "/v1/tenants",
        json={"display_name": "Acme", "first_admin_email": "boss@acme.com"},
        headers=_headers(sys_admin_id),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    # Backwards-compatible tenant fields still at the top level.
    tenant_id = UUID(data["tenant_id"])
    # first_admin summary present.
    fa = data["first_admin"]
    assert fa["email"] == "boss@acme.com"
    assert fa["status"] == "invited"
    kc_user_id = fa["keycloak_user_id"]
    assert kc_user_id in kc.users
    assert kc.users[kc_user_id].emails_sent == 1  # setup email sent

    # Roster row exists, invited, in the new tenant.
    member = await app.state.tenant_member_repo.get(
        tenant_id=tenant_id, member_id=UUID(fa["member_id"])
    )
    assert member is not None and member.status == "invited" and member.role == "admin"

    # Cross-tenant ADMIN role binding written for the Keycloak subject.
    bindings = await app.state.role_binding_repo.list_for_subject(
        subject_type="user", subject_id=UUID(kc_user_id), tenant_id=tenant_id
    )
    assert any(b.role == Role.ADMIN for b in bindings)


@pytest.mark.asyncio
async def test_first_admin_email_normalised_lowercase(
    fake_kc_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, sys_admin_id, _app, _kc = fake_kc_app
    resp = await client.post(
        "/v1/tenants",
        json={"display_name": "Acme", "first_admin_email": "Boss@Acme.COM"},
        headers=_headers(sys_admin_id),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["first_admin"]["email"] == "boss@acme.com"


@pytest.mark.asyncio
async def test_bare_tenant_still_works_without_first_admin(
    fake_kc_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, sys_admin_id, _app, _kc = fake_kc_app
    resp = await client.post(
        "/v1/tenants",
        json={"display_name": "NoAdmin Co"},
        headers=_headers(sys_admin_id),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["display_name"] == "NoAdmin Co"
    assert "first_admin" not in data  # only present when requested


@pytest.mark.asyncio
async def test_invalid_email_rejected(
    fake_kc_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, sys_admin_id, _app, _kc = fake_kc_app
    resp = await client.post(
        "/v1/tenants",
        json={"display_name": "Acme", "first_admin_email": "not-an-email"},
        headers=_headers(sys_admin_id),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_keycloak_conflict_returns_409_but_tenant_created(
    fake_kc_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, sys_admin_id, _app, kc = fake_kc_app
    kc.raise_exists_for.add("taken@acme.com")
    resp = await client.post(
        "/v1/tenants",
        json={
            "tenant_id": str(uuid4()),
            "display_name": "Acme",
            "first_admin_email": "taken@acme.com",
        },
        headers=_headers(sys_admin_id),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "MEMBER_KEYCLOAK_CONFLICT"


@pytest.mark.asyncio
async def test_keycloak_unavailable_returns_502(
    settings: Settings, lifecycle: Lifecycle, jwt_verifier: JWTVerifier
) -> None:
    client, sys_admin_id, _app = await _make_client(
        settings, lifecycle, jwt_verifier, _UnavailableKeycloak()
    )
    async with client:
        resp = await client.post(
            "/v1/tenants",
            json={"display_name": "Acme", "first_admin_email": "boss@acme.com"},
            headers=_headers(sys_admin_id),
        )
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "KEYCLOAK_UNAVAILABLE"
