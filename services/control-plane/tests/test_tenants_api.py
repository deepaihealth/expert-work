"""Endpoint tests for ``POST /v1/tenants`` — Stream P (Mini-ADR P-1/P-2/P-5)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.auth import JWTVerifier
from control_plane.keycloak import FakeKeycloakAdminClient
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import Role
from tests.auth_fixtures import make_test_jwt


@pytest.fixture
async def admin_client(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> AsyncIterator[tuple[AsyncClient, UUID]]:
    """App + client; yields the client and the seeded system-admin subject id."""
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
        yield client, sys_admin_id


@pytest.fixture
def audit_store() -> InMemoryAuditLogStore:
    return InMemoryAuditLogStore()


@pytest.fixture
async def app_password_mode(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
    audit_store: InMemoryAuditLogStore,
) -> AsyncIterator[tuple[AsyncClient, UUID, FakeKeycloakAdminClient]]:
    """Same wiring as ``admin_client`` but with ``member_provisioning_mode="password"``,
    plus a fake Keycloak client + a real audit store so the password branch's
    Keycloak calls and audit trail are both inspectable."""
    kc = FakeKeycloakAdminClient()
    app = create_app(
        settings=settings.model_copy(update={"member_provisioning_mode": "password"}),
        lifecycle=lifecycle,
        jwt_verifier=jwt_verifier,
        keycloak_admin_client=kc,
        audit_logger=build_default_audit_logger(audit_store),
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
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        yield client, sys_admin_id, kc


@pytest.fixture
async def app_email_mode(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> AsyncIterator[tuple[AsyncClient, UUID, FakeKeycloakAdminClient]]:
    """Same wiring as ``admin_client`` but with ``member_provisioning_mode="email"``
    explicit, plus a fake Keycloak client for inspection."""
    kc = FakeKeycloakAdminClient()
    app = create_app(
        settings=settings.model_copy(update={"member_provisioning_mode": "email"}),
        lifecycle=lifecycle,
        jwt_verifier=jwt_verifier,
        keycloak_admin_client=kc,
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
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        yield client, sys_admin_id, kc


def _admin_headers(sys_admin_id: UUID) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4(), subject=str(sys_admin_id))}"
    }


def _non_admin_headers() -> dict[str, str]:
    # A valid-UUID subject with no platform-scope binding → not a system admin.
    return {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4(), subject=str(uuid4()))}"}


@pytest.mark.asyncio
async def test_system_admin_creates_tenant_server_generated_id(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, sys_admin_id = admin_client
    resp = await client.post(
        "/v1/tenants",
        json={"display_name": "Acme Inc"},
        headers=_admin_headers(sys_admin_id),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()["data"]
    assert data["display_name"] == "Acme Inc"
    assert data["plan"] == "free"
    # Server generated a tenant_id.
    UUID(data["tenant_id"])


@pytest.mark.asyncio
async def test_non_admin_cannot_create_tenant(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, _ = admin_client
    resp = await client.post(
        "/v1/tenants",
        json={"display_name": "Sneaky Co"},
        headers=_non_admin_headers(),
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "PLATFORM_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_duplicate_client_supplied_tenant_id_conflicts(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, sys_admin_id = admin_client
    tenant_id = str(uuid4())
    headers = _admin_headers(sys_admin_id)

    first = await client.post(
        "/v1/tenants",
        json={"tenant_id": tenant_id, "display_name": "First", "plan": "pro"},
        headers=headers,
    )
    assert first.status_code == 201, first.text
    assert first.json()["data"]["tenant_id"] == tenant_id
    assert first.json()["data"]["plan"] == "pro"

    dup = await client.post(
        "/v1/tenants",
        json={"tenant_id": tenant_id, "display_name": "Second"},
        headers=headers,
    )
    assert dup.status_code == 409
    assert dup.json()["detail"]["code"] == "TENANT_ALREADY_EXISTS"


@pytest.mark.asyncio
async def test_list_tenants_system_admin_lists_all(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, sys_admin_id = admin_client
    headers = _admin_headers(sys_admin_id)
    seeded_a = str(uuid4())
    seeded_b = str(uuid4())
    for tid, name in ((seeded_a, "Alpha"), (seeded_b, "Beta")):
        created = await client.post(
            "/v1/tenants",
            json={"tenant_id": tid, "display_name": name},
            headers=headers,
        )
        assert created.status_code == 201, created.text

    resp = await client.get("/v1/tenants", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    ids = {t["tenant_id"] for t in body["data"]}
    assert seeded_a in ids and seeded_b in ids
    assert set(body["data"][0].keys()) == {
        "tenant_id",
        "display_name",
        "plan",
        "status",
        "created_at",
        "is_platform",
    }
    assert all(t["status"] == "active" for t in body["data"])
    # Neither seeded tenant is the synthetic platform tenant.
    by_id = {t["tenant_id"]: t for t in body["data"]}
    assert by_id[seeded_a]["is_platform"] is False
    assert by_id[seeded_b]["is_platform"] is False


@pytest.mark.asyncio
async def test_list_tenants_flags_platform_tenant(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    """The row whose id == ``Settings.platform_tenant_id`` (well-known
    ``1111…`` UUID) is flagged ``is_platform`` so the admin UI can hide it."""
    client, sys_admin_id = admin_client
    headers = _admin_headers(sys_admin_id)
    platform_id = "11111111-1111-1111-1111-111111111111"
    created = await client.post(
        "/v1/tenants",
        json={"tenant_id": platform_id, "display_name": "Platform"},
        headers=headers,
    )
    assert created.status_code == 201, created.text

    resp = await client.get("/v1/tenants", headers=headers)
    assert resp.status_code == 200, resp.text
    by_id = {t["tenant_id"]: t for t in resp.json()["data"]}
    assert by_id[platform_id]["is_platform"] is True


@pytest.mark.asyncio
async def test_list_tenants_non_admin_forbidden(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, _ = admin_client
    resp = await client.get("/v1/tenants", headers=_non_admin_headers())
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_tenants_pagination(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, sys_admin_id = admin_client
    headers = _admin_headers(sys_admin_id)
    for name in ("One", "Two"):
        created = await client.post(
            "/v1/tenants",
            json={"tenant_id": str(uuid4()), "display_name": name},
            headers=headers,
        )
        assert created.status_code == 201, created.text

    resp = await client.get("/v1/tenants?limit=1&offset=0", headers=headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["data"]) == 1


# --- Stream U (PR E) — deactivate / activate + suspended-tenant enforcement ---


def _member_headers(tenant_id: UUID) -> dict[str, str]:
    """A non-admin member whose JWT carries ``tenant_id`` as its home tenant."""
    return {"Authorization": f"Bearer {make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()))}"}


async def _create_tenant(client: AsyncClient, sys_admin_id: UUID) -> str:
    resp = await client.post(
        "/v1/tenants",
        json={"tenant_id": str(uuid4()), "display_name": "Tgt"},
        headers=_admin_headers(sys_admin_id),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["tenant_id"]  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_system_admin_deactivate_then_activate(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, sys_admin_id = admin_client
    headers = _admin_headers(sys_admin_id)
    tid = await _create_tenant(client, sys_admin_id)

    deact = await client.post(f"/v1/tenants/{tid}/deactivate", headers=headers)
    assert deact.status_code == 200, deact.text
    assert deact.json()["data"] == {"tenant_id": tid, "status": "suspended"}

    listed = await client.get("/v1/tenants", headers=headers)
    row = next(t for t in listed.json()["data"] if t["tenant_id"] == tid)
    assert row["status"] == "suspended"

    act = await client.post(f"/v1/tenants/{tid}/activate", headers=headers)
    assert act.status_code == 200, act.text
    assert act.json()["data"]["status"] == "active"

    listed2 = await client.get("/v1/tenants", headers=headers)
    row2 = next(t for t in listed2.json()["data"] if t["tenant_id"] == tid)
    assert row2["status"] == "active"


@pytest.mark.asyncio
async def test_non_admin_cannot_deactivate(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, sys_admin_id = admin_client
    tid = await _create_tenant(client, sys_admin_id)
    resp = await client.post(f"/v1/tenants/{tid}/deactivate", headers=_non_admin_headers())
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "PLATFORM_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_deactivate_unknown_tenant_404(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, sys_admin_id = admin_client
    resp = await client.post(
        f"/v1/tenants/{uuid4()}/deactivate", headers=_admin_headers(sys_admin_id)
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "TENANT_NOT_FOUND"


@pytest.mark.asyncio
async def test_suspended_tenant_member_is_blocked_but_system_admin_is_not(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    """CRITICAL: after suspending T, a member of T is 403 TENANT_SUSPENDED on
    any authed route, while a system_admin can STILL act against T."""
    client, sys_admin_id = admin_client
    admin_headers = _admin_headers(sys_admin_id)
    tid = await _create_tenant(client, sys_admin_id)
    member_headers = _member_headers(UUID(tid))

    # Baseline: before suspension, the member's authed request is NOT 403'd by
    # the suspended-tenant gate (it may 404/other, but never TENANT_SUSPENDED).
    pre = await client.get("/v1/tenants", headers=member_headers)
    assert not (
        pre.status_code == 403 and pre.json().get("error", {}).get("code") == "TENANT_SUSPENDED"
    )

    # Suspend T.
    deact = await client.post(f"/v1/tenants/{tid}/deactivate", headers=admin_headers)
    assert deact.status_code == 200, deact.text

    # The member of T is now blocked on any authed route by the real middleware.
    blocked = await client.get("/v1/tenants", headers=member_headers)
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "TENANT_SUSPENDED"

    # system_admin is NOT blocked: it can list and reactivate T while suspended.
    still_lists = await client.get("/v1/tenants", headers=admin_headers)
    assert still_lists.status_code == 200, still_lists.text
    act = await client.post(f"/v1/tenants/{tid}/activate", headers=admin_headers)
    assert act.status_code == 200, act.text

    # After reactivation the member is unblocked by the gate again.
    after = await client.get("/v1/tenants", headers=member_headers)
    assert not (
        after.status_code == 403 and after.json().get("error", {}).get("code") == "TENANT_SUSPENDED"
    )


# ---------------------------------------------------------------------------
# member-password-provisioning Task 3 — first-admin (tenant creation) password
# branch.
#
# ``member_provisioning_mode == "password"`` swaps the Keycloak set-password
# email for a server-generated temporary password (Task 1's
# ``generate_initial_password``), written via ``reset_password(temporary=True)``
# and returned once in ``data.first_admin.initial_password``. Global
# constraint: the password never lands in the audit log.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_tenant_password_mode_returns_initial_password(
    app_password_mode: tuple[AsyncClient, UUID, FakeKeycloakAdminClient],
) -> None:
    client, sys_admin_id, kc = app_password_mode
    resp = await client.post(
        "/v1/tenants",
        json={"display_name": "PW 租户", "first_admin_email": "boss@example.com"},
        headers=_admin_headers(sys_admin_id),
    )
    assert resp.status_code == 201, resp.text
    fa = resp.json()["data"]["first_admin"]
    assert re.fullmatch(r"[a-z]+-[a-z]+-[a-z]+-\d{4}", fa["initial_password"])
    assert kc.password_resets[-1][2] is True  # temporary
    assert kc.password_resets[-1][1] == fa["initial_password"]  # 响应里的就是写进 KC 的
    stored = kc.users[fa["keycloak_user_id"]]
    assert stored.emails_sent == 0  # 不发邮件
    assert stored.email_verified is True


@pytest.mark.asyncio
async def test_create_tenant_email_mode_unchanged(
    app_email_mode: tuple[AsyncClient, UUID, FakeKeycloakAdminClient],
) -> None:
    client, sys_admin_id, kc = app_email_mode
    resp = await client.post(
        "/v1/tenants",
        json={"display_name": "EM 租户", "first_admin_email": "boss2@example.com"},
        headers=_admin_headers(sys_admin_id),
    )
    assert resp.status_code == 201, resp.text
    fa = resp.json()["data"]["first_admin"]
    assert fa["initial_password"] is None
    assert kc.password_resets == []
    stored = kc.users[fa["keycloak_user_id"]]
    assert stored.emails_sent == 1
    assert stored.email_verified is False


@pytest.mark.asyncio
async def test_create_tenant_password_never_in_audit(
    app_password_mode: tuple[AsyncClient, UUID, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
) -> None:
    # create-tenant 后扫全部已 emit 的 audit 事件序列化 JSON,断言初始密码子串不出现。
    from expert_work.protocol import AuditQuery

    client, sys_admin_id, _kc = app_password_mode
    resp = await client.post(
        "/v1/tenants",
        json={"display_name": "审计 租户", "first_admin_email": "audit-pw@example.com"},
        headers=_admin_headers(sys_admin_id),
    )
    assert resp.status_code == 201, resp.text
    fa = resp.json()["data"]["first_admin"]
    pw = fa["initial_password"]
    assert pw is not None

    tenant_id = UUID(resp.json()["data"]["tenant_id"])
    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    serialized = "\n".join(entry.model_dump_json() for entry in page.entries)
    assert pw not in serialized


# ─── PR-E3b — invalidation-bus broadcast on tenant status flips ────────────


class _SpyBusE3b:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)

    def publish_soon(self, event: object) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_status_flips_broadcast_tenant_status(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    """Suspend/reactivate must broadcast ``tenant_status`` so peer replicas
    drop their TTL cache immediately (a suspended tenant otherwise keeps
    passing the auth gate on other pods for up to the TTL)."""
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
    spy_bus = _SpyBusE3b()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        tid = await _create_tenant(client, sys_admin_id)
        app.state.invalidation_bus = spy_bus  # after create: only status flips publish

        deact = await client.post(
            f"/v1/tenants/{tid}/deactivate", headers=_admin_headers(sys_admin_id)
        )
        assert deact.status_code == 200, deact.text
        act = await client.post(f"/v1/tenants/{tid}/activate", headers=_admin_headers(sys_admin_id))
        assert act.status_code == 200, act.text

    assert len(spy_bus.events) == 2
    for event in spy_bus.events:
        assert event.kind == "tenant_status"  # type: ignore[attr-defined]
        assert event.tenant_id == tid  # type: ignore[attr-defined]
