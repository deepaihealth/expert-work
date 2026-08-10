"""Endpoint tests for ``/v1/members`` — Stream R W2 (invite/list/resend/revoke).

A tenant admin (JWT carries ``admin`` role → ``user:write``) onboards members.
Uses a Fake Keycloak so the full flow runs without a live IdP; covers the
batch happy path, per-item conflict isolation, resend compensation, and the
revoke/suspend branches.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.auth import JWTVerifier
from control_plane.keycloak import FakeKeycloakAdminClient, KeycloakUnavailableError
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from tests.auth_fixtures import make_test_jwt


@pytest.fixture
def audit_store() -> InMemoryAuditLogStore:
    return InMemoryAuditLogStore()


@pytest.fixture
async def admin_app(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
    audit_store: InMemoryAuditLogStore,
) -> AsyncIterator[tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient]]:
    kc = FakeKeycloakAdminClient()
    app = create_app(
        settings=settings,
        lifecycle=lifecycle,
        jwt_verifier=jwt_verifier,
        keycloak_admin_client=kc,
        audit_logger=build_default_audit_logger(audit_store),
    )
    tenant_id = uuid4()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        yield client, tenant_id, app, kc


@pytest.fixture
async def app_password_mode(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
    audit_store: InMemoryAuditLogStore,
) -> AsyncIterator[tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient]]:
    """Same wiring as ``admin_app`` but with ``member_provisioning_mode="password"``."""
    kc = FakeKeycloakAdminClient()
    app = create_app(
        settings=settings.model_copy(update={"member_provisioning_mode": "password"}),
        lifecycle=lifecycle,
        jwt_verifier=jwt_verifier,
        keycloak_admin_client=kc,
        audit_logger=build_default_audit_logger(audit_store),
    )
    tenant_id = uuid4()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        yield client, tenant_id, app, kc


@pytest.fixture
async def app_email_mode(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
    audit_store: InMemoryAuditLogStore,
) -> AsyncIterator[tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient]]:
    """Same wiring as ``admin_app`` but with ``member_provisioning_mode="email"`` explicit."""
    kc = FakeKeycloakAdminClient()
    app = create_app(
        settings=settings.model_copy(update={"member_provisioning_mode": "email"}),
        lifecycle=lifecycle,
        jwt_verifier=jwt_verifier,
        keycloak_admin_client=kc,
        audit_logger=build_default_audit_logger(audit_store),
    )
    tenant_id = uuid4()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        yield client, tenant_id, app, kc


def _admin_headers(tenant_id: UUID) -> dict[str, str]:
    # Default roles=("admin",) → user:write/read.
    return {"Authorization": f"Bearer {make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()))}"}


def _viewer_headers(tenant_id: UUID) -> dict[str, str]:
    token = make_test_jwt(tenant_id=tenant_id, subject=str(uuid4()), roles=("viewer",))
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_invite_batch_happy_path(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, tenant_id, _app, kc = admin_app
    resp = await client.post(
        "/v1/members/invite",
        json={
            "invitations": [
                {"email": "a@co.com", "role": "viewer"},
                {"email": "B@Co.com", "role": "operator", "display_name": "Bob"},
            ]
        },
        headers=_admin_headers(tenant_id),
    )
    assert resp.status_code == 201, resp.text
    results = resp.json()["data"]["results"]
    assert len(results) == 2
    assert all(r["error_code"] is None and r["status"] == "invited" for r in results)
    assert results[1]["email"] == "b@co.com"  # normalised
    assert len(kc.users) == 2


@pytest.mark.asyncio
async def test_invite_conflict_is_per_item(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, tenant_id, _app, kc = admin_app
    kc.raise_exists_for.add("taken@co.com")
    resp = await client.post(
        "/v1/members/invite",
        json={
            "invitations": [
                {"email": "taken@co.com", "role": "viewer"},
                {"email": "ok@co.com", "role": "viewer"},
            ]
        },
        headers=_admin_headers(tenant_id),
    )
    assert resp.status_code == 201
    results = {r["email"]: r for r in resp.json()["data"]["results"]}
    assert results["taken@co.com"]["error_code"] == "MEMBER_KEYCLOAK_CONFLICT"
    assert results["ok@co.com"]["error_code"] is None  # the other one still succeeded


@pytest.mark.asyncio
async def test_list_filters_by_status(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, tenant_id, _app, _kc = admin_app
    await client.post(
        "/v1/members/invite",
        json={"invitations": [{"email": "a@co.com", "role": "viewer"}]},
        headers=_admin_headers(tenant_id),
    )
    resp = await client.get("/v1/members", headers=_admin_headers(tenant_id))
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["email"] == "a@co.com"

    invited = await client.get("/v1/members?status=invited", headers=_admin_headers(tenant_id))
    assert invited.json()["data"]["total"] == 1
    active = await client.get("/v1/members?status=active", headers=_admin_headers(tenant_id))
    assert active.json()["data"]["total"] == 0


@pytest.mark.asyncio
async def test_viewer_cannot_invite(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, tenant_id, _app, _kc = admin_app
    resp = await client.post(
        "/v1/members/invite",
        json={"invitations": [{"email": "a@co.com", "role": "viewer"}]},
        headers=_viewer_headers(tenant_id),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_revoke_invited_member(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, tenant_id, app, kc = admin_app
    inv = await client.post(
        "/v1/members/invite",
        json={"invitations": [{"email": "a@co.com", "role": "viewer"}]},
        headers=_admin_headers(tenant_id),
    )
    member_id = inv.json()["data"]["results"][0]["member_id"]
    resp = await client.delete(f"/v1/members/{member_id}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 204
    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=UUID(member_id))
    assert member is not None and member.status == "revoked"
    assert len(kc.users) == 0  # Keycloak account deleted


@pytest.mark.asyncio
async def test_revoke_missing_member_404(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, tenant_id, _app, _kc = admin_app
    resp = await client.delete(f"/v1/members/{uuid4()}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 404


# --- revoke/suspend role-binding cleanup (delete-hygiene PR2 T5) -------------


@pytest.mark.asyncio
async def test_revoke_invited_member_removes_role_binding(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
) -> None:
    from expert_work.protocol import AuditAction, AuditQuery

    client, tenant_id, app, _kc = admin_app
    inv = await client.post(
        "/v1/members/invite",
        json={"invitations": [{"email": "a@co.com", "role": "viewer"}]},
        headers=_admin_headers(tenant_id),
    )
    member_id = UUID(inv.json()["data"]["results"][0]["member_id"])
    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)
    assert member is not None and member.keycloak_user_id is not None
    kc_user_id = UUID(member.keycloak_user_id)
    bindings_before = await app.state.role_binding_repo.list_for_subject(  # type: ignore[attr-defined]
        subject_type="user", subject_id=kc_user_id, tenant_id=tenant_id
    )
    assert len(bindings_before) == 1  # invite_member wrote the tenant-scope binding

    resp = await client.delete(f"/v1/members/{member_id}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 204

    bindings_after = await app.state.role_binding_repo.list_for_subject(  # type: ignore[attr-defined]
        subject_type="user", subject_id=kc_user_id, tenant_id=tenant_id
    )
    assert bindings_after == []

    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    revoke_rows = [r for r in page.entries if r.action is AuditAction.MEMBER_REVOKE]
    assert len(revoke_rows) == 1
    assert revoke_rows[0].details["role_bindings_removed"] == 1
    assert revoke_rows[0].details["role_bindings_cleanup_failed"] is False


@pytest.mark.asyncio
async def test_suspend_active_member_removes_role_binding(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
) -> None:
    from datetime import UTC, datetime

    from expert_work.protocol import AuditAction, AuditQuery

    client, tenant_id, app, _kc = admin_app
    inv = await client.post(
        "/v1/members/invite",
        json={"invitations": [{"email": "a@co.com", "role": "viewer"}]},
        headers=_admin_headers(tenant_id),
    )
    member_id = UUID(inv.json()["data"]["results"][0]["member_id"])
    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)
    assert member is not None and member.keycloak_user_id is not None
    kc_user_id = UUID(member.keycloak_user_id)
    moved = await app.state.tenant_member_repo.transition(
        member_id=member_id, tenant_id=tenant_id, to="active", now=datetime.now(UTC)
    )
    assert moved

    resp = await client.delete(f"/v1/members/{member_id}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 204
    active_member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)
    assert active_member is not None and active_member.status == "suspended"

    bindings_after = await app.state.role_binding_repo.list_for_subject(  # type: ignore[attr-defined]
        subject_type="user", subject_id=kc_user_id, tenant_id=tenant_id
    )
    assert bindings_after == []

    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    suspend_rows = [r for r in page.entries if r.action is AuditAction.MEMBER_SUSPEND]
    assert len(suspend_rows) == 1
    assert suspend_rows[0].details["role_bindings_removed"] == 1


@pytest.mark.asyncio
async def test_revoke_member_without_keycloak_user_id_skips_cleanup(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
) -> None:
    """A member that never got a Keycloak account has no binding to clean up."""
    from expert_work.protocol import AuditAction, AuditQuery

    client, tenant_id, app, _kc = admin_app
    member = await app.state.tenant_member_repo.create(
        tenant_id=tenant_id,
        email="a@co.com",
        role="viewer",
        invited_by=str(uuid4()),
        keycloak_user_id=None,
    )
    resp = await client.delete(f"/v1/members/{member.id}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 204
    revoked = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member.id)
    assert revoked is not None and revoked.status == "revoked"

    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    revoke_rows = [r for r in page.entries if r.action is AuditAction.MEMBER_REVOKE]
    assert len(revoke_rows) == 1
    assert revoke_rows[0].details["role_bindings_removed"] == 0


@pytest.mark.asyncio
async def test_revoke_role_binding_cleanup_failure_does_not_fail_request(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``delete_for_subject`` failure must not roll back the status transition,
    must not raise past the endpoint, and must be flagged in the audit details
    so an operator can find + hand-clean the orphaned binding."""
    from expert_work.protocol import AuditAction, AuditQuery

    client, tenant_id, app, _kc = admin_app
    inv = await client.post(
        "/v1/members/invite",
        json={"invitations": [{"email": "a@co.com", "role": "viewer"}]},
        headers=_admin_headers(tenant_id),
    )
    member_id = UUID(inv.json()["data"]["results"][0]["member_id"])

    async def _boom(**_kwargs: object) -> int:
        raise RuntimeError("role binding store unavailable")

    monkeypatch.setattr(app.state.role_binding_repo, "delete_for_subject", _boom)

    resp = await client.delete(f"/v1/members/{member_id}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 204  # cleanup failure does not surface as a request error

    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)
    assert member is not None and member.status == "revoked"  # transition already committed

    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    revoke_rows = [r for r in page.entries if r.action is AuditAction.MEMBER_REVOKE]
    assert len(revoke_rows) == 1
    assert revoke_rows[0].details["role_bindings_removed"] == 0
    assert revoke_rows[0].details["role_bindings_cleanup_failed"] is True


@pytest.mark.asyncio
async def test_revoke_skips_cleanup_when_transition_loses_race(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``transition`` reports ``moved=False`` (lost a concurrent race — e.g.
    another request already revoked/suspended the member first), the role
    binding must be left alone: no cleanup call, no ghost audit of a deletion
    that didn't happen."""
    client, tenant_id, app, _kc = admin_app
    inv = await client.post(
        "/v1/members/invite",
        json={"invitations": [{"email": "a@co.com", "role": "viewer"}]},
        headers=_admin_headers(tenant_id),
    )
    member_id = UUID(inv.json()["data"]["results"][0]["member_id"])

    async def _not_moved(**_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(app.state.tenant_member_repo, "transition", _not_moved)

    called = False

    async def _delete_for_subject(**_kwargs: object) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(app.state.role_binding_repo, "delete_for_subject", _delete_for_subject)

    resp = await client.delete(f"/v1/members/{member_id}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 204
    assert called is False


@pytest.mark.asyncio
async def test_resend_non_invited_409(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, tenant_id, _app, _kc = admin_app
    inv = await client.post(
        "/v1/members/invite",
        json={"invitations": [{"email": "a@co.com", "role": "viewer"}]},
        headers=_admin_headers(tenant_id),
    )
    member_id = inv.json()["data"]["results"][0]["member_id"]
    # Revoke first, then a resend must 409 (not invited any more).
    await client.delete(f"/v1/members/{member_id}", headers=_admin_headers(tenant_id))
    resp = await client.post(f"/v1/members/{member_id}/resend", headers=_admin_headers(tenant_id))
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "MEMBER_NOT_RESENDABLE"


# --- reset-password (Stream U PR F) -------------------------------------------


@pytest.mark.asyncio
async def test_reset_password_happy_path(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, tenant_id, app, kc = admin_app
    member = await app.state.tenant_member_repo.create(
        tenant_id=tenant_id,
        email="a@co.com",
        role="viewer",
        invited_by=str(uuid4()),
        keycloak_user_id="kc-user-1",
    )
    resp = await client.post(
        f"/v1/members/{member.id}/reset-password",
        json={"password": "hunter2pass"},
        headers=_admin_headers(tenant_id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["member_id"] == str(member.id)
    assert kc.password_resets == [("kc-user-1", "hunter2pass", True)]


@pytest.mark.asyncio
async def test_reset_password_no_keycloak_user_409(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, tenant_id, app, kc = admin_app
    member = await app.state.tenant_member_repo.create(
        tenant_id=tenant_id,
        email="a@co.com",
        role="viewer",
        invited_by=str(uuid4()),
        keycloak_user_id=None,
    )
    resp = await client.post(
        f"/v1/members/{member.id}/reset-password",
        json={"password": "hunter2pass"},
        headers=_admin_headers(tenant_id),
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "MEMBER_NO_KEYCLOAK_USER"
    assert kc.password_resets == []


@pytest.mark.asyncio
async def test_reset_password_unknown_member_404(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, tenant_id, _app, _kc = admin_app
    resp = await client.post(
        f"/v1/members/{uuid4()}/reset-password",
        json={"password": "hunter2pass"},
        headers=_admin_headers(tenant_id),
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "MEMBER_NOT_FOUND"


@pytest.mark.asyncio
async def test_reset_password_viewer_forbidden(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, tenant_id, app, _kc = admin_app
    member = await app.state.tenant_member_repo.create(
        tenant_id=tenant_id,
        email="a@co.com",
        role="viewer",
        invited_by=str(uuid4()),
        keycloak_user_id="kc-user-1",
    )
    resp = await client.post(
        f"/v1/members/{member.id}/reset-password",
        json={"password": "hunter2pass"},
        headers=_viewer_headers(tenant_id),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reset_password_keycloak_unavailable_502(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, tenant_id, app, kc = admin_app
    kc.reset_password_unavailable = True
    member = await app.state.tenant_member_repo.create(
        tenant_id=tenant_id,
        email="a@co.com",
        role="viewer",
        invited_by=str(uuid4()),
        keycloak_user_id="kc-user-1",
    )
    resp = await client.post(
        f"/v1/members/{member.id}/reset-password",
        json={"password": "hunter2pass"},
        headers=_admin_headers(tenant_id),
    )
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "KEYCLOAK_UNAVAILABLE"


@pytest.mark.asyncio
async def test_reset_password_too_short_422(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, tenant_id, app, kc = admin_app
    member = await app.state.tenant_member_repo.create(
        tenant_id=tenant_id,
        email="a@co.com",
        role="viewer",
        invited_by=str(uuid4()),
        keycloak_user_id="kc-user-1",
    )
    resp = await client.post(
        f"/v1/members/{member.id}/reset-password",
        json={"password": "short"},
        headers=_admin_headers(tenant_id),
    )
    assert resp.status_code == 422
    assert kc.password_resets == []


# --- one-shot deactivate + purge (delete-hygiene PR5 T2) ----------------------


async def _invite_one(client: AsyncClient, tenant_id: UUID, email: str = "leaver@co.com") -> UUID:
    inv = await client.post(
        "/v1/members/invite",
        json={"invitations": [{"email": email, "role": "viewer"}]},
        headers=_admin_headers(tenant_id),
    )
    assert inv.status_code == 201, inv.text
    return UUID(inv.json()["data"]["results"][0]["member_id"])


async def _activate_with_data(app: object, tenant_id: UUID, member_id: UUID) -> tuple[UUID, UUID]:
    """First-login link (subject_id back-fill) + one thread of user data.

    Returns ``(tenant_user.id, thread_id)``.
    """
    from datetime import UTC, datetime

    user = await app.state.tenant_user_repo.resolve(  # type: ignore[attr-defined]
        tenant_id=tenant_id, subject_type="user", subject_id="emp-sub", display_name="Emp"
    )
    moved = await app.state.tenant_member_repo.transition(  # type: ignore[attr-defined]
        member_id=member_id,
        tenant_id=tenant_id,
        to="active",
        now=datetime.now(UTC),
        subject_id=user.id,
    )
    assert moved
    thread_id = uuid4()
    await app.state.thread_meta_repo.create(  # type: ignore[attr-defined]
        thread_id=thread_id,
        tenant_id=tenant_id,
        created_by="seed",
        user_id=user.id,
        agent_name="alpha",
        agent_version="1.0.0",
    )
    return user.id, thread_id


@pytest.mark.asyncio
async def test_purge_invited_member_revokes_and_deletes_kc_account(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
) -> None:
    """① invited → revoked; KC account deleted; no data step (never logged in)."""
    from expert_work.protocol import AuditAction, AuditQuery

    client, tenant_id, app, kc = admin_app
    member_id = await _invite_one(client, tenant_id)
    assert len(kc.users) == 1

    resp = await client.post(f"/v1/members/{member_id}:purge", headers=_admin_headers(tenant_id))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["member_id"] == str(member_id)
    assert data["status"] == "revoked"
    assert data["kc_deleted"] is True
    assert data["kc_delete_failed"] is False
    assert data["role_bindings_removed"] == 1
    assert data["role_bindings_cleanup_failed"] is False
    assert data["data_purged"] is False  # subject_id NULL — never logged in
    assert data["data_purge_failed"] is False
    assert data["purge"] is None

    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)  # type: ignore[attr-defined]
    assert member is not None and member.status == "revoked"
    assert len(kc.users) == 0  # KC account deleted (D2)

    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    rows = [r for r in page.entries if r.action is AuditAction.MEMBER_PURGE]
    assert len(rows) == 1
    assert rows[0].resource_id == str(member_id)
    assert rows[0].details["email"] == "leaver@co.com"
    assert rows[0].details["from_status"] == "invited"
    assert rows[0].details["kc_deleted"] is True
    assert rows[0].details["role_bindings_removed"] == 1
    assert rows[0].details["data_purged"] is False
    assert rows[0].details["data_purge_failed"] is False
    # No data step ran at all — accountability distinguishes that from "ran
    # and every store succeeded" (True) and "ran, some store failed" (False).
    assert rows[0].details["purge_ok"] is None


@pytest.mark.asyncio
async def test_purge_active_member_suspends_deletes_kc_and_purges_data(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
) -> None:
    """② active (subject_id + data) → suspended; KC deleted; data cascade ran."""
    from expert_work.protocol import AuditAction, AuditQuery
    from orchestrator.tools.workspace_store import RecordingWorkspaceStore

    client, tenant_id, app, kc = admin_app
    # create_app has no supervisor URL in tests — wire the recording fake so
    # the workspace step of the cascade runs (otherwise it logs a failure and
    # ``purge.ok`` could never be asserted True).
    app.state.workspace_store = RecordingWorkspaceStore()  # type: ignore[attr-defined]
    member_id = await _invite_one(client, tenant_id)
    user_id, thread_id = await _activate_with_data(app, tenant_id, member_id)

    resp = await client.post(f"/v1/members/{member_id}:purge", headers=_admin_headers(tenant_id))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "suspended"
    assert data["kc_deleted"] is True
    assert data["data_purged"] is True
    assert data["data_purge_failed"] is False
    assert data["purge"] is not None
    assert data["purge"]["user_id"] == str(user_id)
    assert data["purge"]["threads_purged"] == 1
    assert data["purge"]["deactivated"] is True
    assert data["purge"]["ok"] is True

    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)  # type: ignore[attr-defined]
    assert member is not None and member.status == "suspended"
    assert len(kc.users) == 0  # deleted, not merely disabled (D2)
    gone = await app.state.thread_meta_repo.get(thread_id, tenant_id=tenant_id)  # type: ignore[attr-defined]
    assert gone is None  # the data row is actually gone

    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    rows = [r for r in page.entries if r.action is AuditAction.MEMBER_PURGE]
    assert len(rows) == 1
    assert rows[0].details["from_status"] == "active"
    assert rows[0].details["data_purged"] is True
    assert rows[0].details["data_purge_failed"] is False
    assert rows[0].details["purge_ok"] is True  # ran AND every store succeeded


@pytest.mark.asyncio
async def test_purge_suspended_member_backfills_without_transition(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """③ suspended backfill — status unchanged, KC deleted, data cascade ran."""
    from datetime import UTC, datetime

    client, tenant_id, app, kc = admin_app
    member_id = await _invite_one(client, tenant_id)
    _user_id, thread_id = await _activate_with_data(app, tenant_id, member_id)
    moved = await app.state.tenant_member_repo.transition(  # type: ignore[attr-defined]
        member_id=member_id, tenant_id=tenant_id, to="suspended", now=datetime.now(UTC)
    )
    assert moved
    assert len(kc.users) == 1  # suspend never deleted the KC account before

    resp = await client.post(f"/v1/members/{member_id}:purge", headers=_admin_headers(tenant_id))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "suspended"  # no further transition
    assert data["kc_deleted"] is True
    assert data["data_purged"] is True

    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)  # type: ignore[attr-defined]
    assert member is not None and member.status == "suspended"
    assert len(kc.users) == 0
    gone = await app.state.thread_meta_repo.get(thread_id, tenant_id=tenant_id)  # type: ignore[attr-defined]
    assert gone is None


@pytest.mark.asyncio
async def test_purge_rerun_is_idempotent(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """④ re-running the purge is a safe no-op (200, every step no-ops)."""
    from orchestrator.tools.workspace_store import RecordingWorkspaceStore

    client, tenant_id, app, _kc = admin_app
    app.state.workspace_store = RecordingWorkspaceStore()  # type: ignore[attr-defined]
    member_id = await _invite_one(client, tenant_id)
    await _activate_with_data(app, tenant_id, member_id)

    first = await client.post(f"/v1/members/{member_id}:purge", headers=_admin_headers(tenant_id))
    assert first.status_code == 200, first.text

    second = await client.post(f"/v1/members/{member_id}:purge", headers=_admin_headers(tenant_id))
    assert second.status_code == 200, second.text
    data = second.json()["data"]
    assert data["status"] == "suspended"
    assert data["kc_delete_failed"] is False  # fake delete_user is idempotent
    assert data["role_bindings_removed"] == 0  # already removed on the first run
    assert data["role_bindings_cleanup_failed"] is False
    assert data["data_purged"] is True  # re-run retries the cascade, safe no-op
    assert data["purge"]["threads_purged"] == 0
    assert data["purge"]["ok"] is True

    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)  # type: ignore[attr-defined]
    assert member is not None and member.status == "suspended"


@pytest.mark.asyncio
async def test_purge_kc_unavailable_flags_and_continues(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⑤ KC down → 200 with ``kc_delete_failed``; every other step still runs."""
    client, tenant_id, app, kc = admin_app
    member_id = await _invite_one(client, tenant_id)
    _user_id, thread_id = await _activate_with_data(app, tenant_id, member_id)

    async def _kc_boom(**_kwargs: object) -> None:
        raise KeycloakUnavailableError("forced-unavailable (test)")

    monkeypatch.setattr(kc, "delete_user", _kc_boom)

    resp = await client.post(f"/v1/members/{member_id}:purge", headers=_admin_headers(tenant_id))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["kc_deleted"] is False
    assert data["kc_delete_failed"] is True
    assert data["status"] == "suspended"
    assert data["role_bindings_removed"] == 1
    assert data["data_purged"] is True

    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)  # type: ignore[attr-defined]
    assert member is not None and member.status == "suspended"
    gone = await app.state.thread_meta_repo.get(thread_id, tenant_id=tenant_id)  # type: ignore[attr-defined]
    assert gone is None


@pytest.mark.asyncio
async def test_purge_viewer_forbidden(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """⑥ non-admin gets 403; nothing happens."""
    client, tenant_id, _app, kc = admin_app
    member_id = await _invite_one(client, tenant_id)

    resp = await client.post(f"/v1/members/{member_id}:purge", headers=_viewer_headers(tenant_id))
    assert resp.status_code == 403
    assert len(kc.users) == 1  # untouched


@pytest.mark.asyncio
async def test_purge_transition_conflict_409_blocks_all_side_effects(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⑦ a lost transition race → 409 MEMBER_STATE_CONFLICT, ZERO side effects.

    Continuing past a failed lifecycle move would purge a still-active
    member's data (half-state) — the whole point of the blocking rule.
    """
    from expert_work.protocol import AuditAction, AuditQuery

    client, tenant_id, app, kc = admin_app
    member_id = await _invite_one(client, tenant_id)
    _user_id, thread_id = await _activate_with_data(app, tenant_id, member_id)
    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)  # type: ignore[attr-defined]
    assert member is not None and member.keycloak_user_id is not None
    kc_uuid = UUID(member.keycloak_user_id)

    async def _not_moved(**_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(app.state.tenant_member_repo, "transition", _not_moved)  # type: ignore[attr-defined]

    resp = await client.post(f"/v1/members/{member_id}:purge", headers=_admin_headers(tenant_id))
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "MEMBER_STATE_CONFLICT"

    # No side effects at all: KC account, role binding, data, audit all intact.
    assert len(kc.users) == 1
    bindings = await app.state.role_binding_repo.list_for_subject(  # type: ignore[attr-defined]
        subject_type="user", subject_id=kc_uuid, tenant_id=tenant_id
    )
    assert len(bindings) == 1
    still = await app.state.thread_meta_repo.get(thread_id, tenant_id=tenant_id)  # type: ignore[attr-defined]
    assert still is not None
    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    assert [r for r in page.entries if r.action is AuditAction.MEMBER_PURGE] == []


@pytest.mark.asyncio
async def test_purge_partial_cascade_records_purge_ok_false(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⑧ one cascade store fails → 200, ``purge.ok`` false, audit ``purge_ok`` false.

    ``data_purged`` alone can't tell "ran and fully succeeded" from "ran and
    half-failed" — the audit row carries ``purge_ok`` so the offboarding is
    accountable without re-deriving it from the (unstored) summary.
    """
    from expert_work.protocol import AuditAction, AuditQuery
    from orchestrator.tools.workspace_store import RecordingWorkspaceStore

    client, tenant_id, app, _kc = admin_app
    # Supervisor wired so the ONLY failure is the injected one.
    app.state.workspace_store = RecordingWorkspaceStore()  # type: ignore[attr-defined]
    member_id = await _invite_one(client, tenant_id)
    await _activate_with_data(app, tenant_id, member_id)

    # Message shaped like the realistic leak: a driver connect error carrying
    # the DSN, password included. Asserted absent from the response below.
    secret_in_message = "connect failed: postgresql://purge:s3cr3t@rds-internal:5432/ew"

    async def _memory_boom(**_kwargs: object) -> int:
        raise RuntimeError(secret_in_message)

    monkeypatch.setattr(app.state.memory_repo, "delete_all_for_user", _memory_boom)  # type: ignore[attr-defined]

    resp = await client.post(f"/v1/members/{member_id}:purge", headers=_admin_headers(tenant_id))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["data_purged"] is True  # the step ran…
    assert data["data_purge_failed"] is False  # …and did not blow up as a whole
    assert data["purge"]["ok"] is False  # …but a store inside it failed
    assert "memory_item" in data["purge"]["failures"]
    # Exception TYPE only, never str(exc) — and asserted HERE because this is
    # the real leak boundary: a live HTTP response body, via purge_user's
    # shared `_step` wrapper (the path ~20 steps go through). The unit test in
    # test_user_purge.py only covers the two hand-rolled `except` blocks, so
    # without this line a regression in `_step` itself stays green.
    assert data["purge"]["failures"]["memory_item"] == "RuntimeError"
    assert secret_in_message not in resp.text

    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    rows = [r for r in page.entries if r.action is AuditAction.MEMBER_PURGE]
    assert len(rows) == 1
    assert rows[0].details["data_purged"] is True
    assert rows[0].details["purge_ok"] is False


@pytest.mark.asyncio
async def test_purge_data_step_failure_is_best_effort_and_audited(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⑨ data-step *resolution* blows up → 200 + flag, not a 500 with no audit row.

    ``users.get`` / dep assembly sit OUTSIDE ``purge_user``'s per-step
    best-effort net. A transient failure there used to 500 *after* the
    Keycloak account was already deleted — the destructive prefix happened
    with no audit trail at all.
    """
    from expert_work.protocol import AuditAction, AuditQuery

    client, tenant_id, app, kc = admin_app
    member_id = await _invite_one(client, tenant_id)
    _user_id, thread_id = await _activate_with_data(app, tenant_id, member_id)

    async def _users_get_boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("forced registry read failure (test)")

    monkeypatch.setattr(app.state.tenant_user_repo, "get", _users_get_boom)  # type: ignore[attr-defined]

    resp = await client.post(f"/v1/members/{member_id}:purge", headers=_admin_headers(tenant_id))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["data_purged"] is False
    assert data["data_purge_failed"] is True
    assert data["purge"] is None
    # The steps before it still ran and are still reported truthfully.
    assert data["status"] == "suspended"
    assert data["kc_deleted"] is True
    assert data["role_bindings_removed"] == 1
    assert len(kc.users) == 0
    still = await app.state.thread_meta_repo.get(thread_id, tenant_id=tenant_id)  # type: ignore[attr-defined]
    assert still is not None  # data untouched — the operator must re-run

    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    rows = [r for r in page.entries if r.action is AuditAction.MEMBER_PURGE]
    assert len(rows) == 1  # the audit row lands even though the data step died
    assert rows[0].details["kc_deleted"] is True
    assert rows[0].details["data_purged"] is False
    assert rows[0].details["data_purge_failed"] is True
    assert rows[0].details["purge_ok"] is None


@pytest.mark.asyncio
async def test_cross_tenant_list_requires_system_admin(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """Stream ACCT — ``?tenant_id=*`` is system_admin-only; tenant admin gets 403."""
    client, tenant_id, _app, _kc = admin_app
    resp = await client.get(
        "/v1/members", params={"tenant_id": "*"}, headers=_admin_headers(tenant_id)
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "CROSS_TENANT_FORBIDDEN"


@pytest.mark.asyncio
async def test_cross_tenant_list_aggregates_for_system_admin(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    from expert_work.protocol import Role

    client, tenant_a, app, _kc = admin_app
    tenant_b = uuid4()
    # Invite one member in each of two tenants.
    for tenant, email in ((tenant_a, "a@t1.com"), (tenant_b, "b@t2.com")):
        r = await client.post(
            "/v1/members/invite",
            json={"invitations": [{"email": email, "role": "viewer"}]},
            headers=_admin_headers(tenant),
        )
        assert r.status_code == 201, r.text

    # Promote a subject to platform system_admin by seeding a platform binding.
    sysadmin = uuid4()
    await app.state.role_binding_repo.create(  # type: ignore[attr-defined]
        subject_type="user",
        subject_id=sysadmin,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="test",
    )
    token = make_test_jwt(tenant_id=uuid4(), subject=str(sysadmin), roles=("admin",))
    resp = await client.get(
        "/v1/members",
        params={"tenant_id": "*"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    tenants_seen = {item["tenant_id"] for item in items}
    assert {str(tenant_a), str(tenant_b)} <= tenants_seen


# ---------------------------------------------------------------------------
# W3 — members C-2 修复:具体 tenant_id 不再被静默忽略
#
# system_admin 带具体他租户 UUID → 返回该租户成员;普通租户 admin 带他租户
# UUID → 403 TENANT_NOT_ALLOWED;"*" 聚合行为不回归(上方既有两测)。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_members_system_admin_concrete_foreign_tenant(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    from expert_work.protocol import Role

    client, tenant_a, app, _kc = admin_app
    tenant_b = uuid4()
    for tenant, email in ((tenant_a, "a@t1.com"), (tenant_b, "b@t2.com")):
        r = await client.post(
            "/v1/members/invite",
            json={"invitations": [{"email": email, "role": "viewer"}]},
            headers=_admin_headers(tenant),
        )
        assert r.status_code == 201, r.text

    sysadmin = uuid4()
    await app.state.role_binding_repo.create(  # type: ignore[attr-defined]
        subject_type="user",
        subject_id=sysadmin,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        platform_scope=True,
        granted_by="test",
    )
    token = make_test_jwt(tenant_id=uuid4(), subject=str(sysadmin), roles=("admin",))
    resp = await client.get(
        "/v1/members",
        params={"tenant_id": str(tenant_b)},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]["items"]
    # C-2 修复断言:具体 UUID 生效——只见 tenant_b 的成员,不再落回归属租户。
    assert [i["email"] for i in items] == ["b@t2.com"]
    assert {i["tenant_id"] for i in items} == {str(tenant_b)}


@pytest.mark.asyncio
async def test_list_members_foreign_tenant_user_403(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """普通租户 admin 带他租户 UUID:此前被静默忽略返回自家名册,现在 403。"""
    client, tenant_a, _app, _kc = admin_app
    foreign_tenant = uuid4()
    resp = await client.get(
        "/v1/members",
        params={"tenant_id": str(foreign_tenant)},
        headers=_admin_headers(tenant_a),
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "TENANT_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# member-password-provisioning Task 2 — invite/resend password branch.
#
# ``member_provisioning_mode == "password"`` swaps the Keycloak set-password
# email for a server-generated temporary password (Task 1's
# ``generate_initial_password``), written via ``reset_password(temporary=True)``
# and returned once in the response. Global constraint: the password never
# lands in the audit log.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invite_password_mode_sets_temp_password_and_skips_email(
    app_password_mode: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, tenant_id, _app, kc = app_password_mode
    resp = await client.post(
        "/v1/members/invite",
        json={"invitations": [{"email": "pw-mode@example.com", "role": "viewer"}]},
        headers=_admin_headers(tenant_id),
    )
    assert resp.status_code == 201, resp.text
    item = resp.json()["data"]["results"][0]
    pw = item["initial_password"]
    assert re.fullmatch(r"[a-z]+-[a-z]+-[a-z]+-\d{4}", pw)
    assert kc.password_resets[-1][2] is True  # temporary
    assert kc.password_resets[-1][1] == pw  # 响应里的就是写进 KC 的
    assert len(kc.users) == 1
    stored = next(iter(kc.users.values()))
    assert stored.emails_sent == 0  # 不发邮件
    assert stored.email_verified is True  # email_verified


@pytest.mark.asyncio
async def test_invite_email_mode_unchanged(
    app_email_mode: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, tenant_id, _app, kc = app_email_mode
    resp = await client.post(
        "/v1/members/invite",
        json={"invitations": [{"email": "em-mode@example.com", "role": "viewer"}]},
        headers=_admin_headers(tenant_id),
    )
    assert resp.status_code == 201, resp.text
    item = resp.json()["data"]["results"][0]
    assert item["initial_password"] is None
    assert kc.password_resets == []
    assert len(kc.users) == 1
    stored = next(iter(kc.users.values()))
    assert stored.emails_sent == 1
    assert stored.email_verified is False


@pytest.mark.asyncio
async def test_resend_password_mode_regenerates(
    app_password_mode: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    # 先邀请拿到 member_id 与第一枚密码,再 resend,断言:新密码 != 旧密码、
    # 又一次 reset_password(temporary=True)、仍然零邮件。
    client, tenant_id, _app, kc = app_password_mode
    inv = await client.post(
        "/v1/members/invite",
        json={"invitations": [{"email": "resend-pw@example.com", "role": "viewer"}]},
        headers=_admin_headers(tenant_id),
    )
    assert inv.status_code == 201, inv.text
    item = inv.json()["data"]["results"][0]
    member_id = item["member_id"]
    first_pw = item["initial_password"]
    assert first_pw is not None

    resend_resp = await client.post(
        f"/v1/members/{member_id}/resend", headers=_admin_headers(tenant_id)
    )
    assert resend_resp.status_code == 200, resend_resp.text
    second_pw = resend_resp.json()["data"]["initial_password"]
    assert second_pw is not None
    assert second_pw != first_pw

    assert len(kc.password_resets) == 2
    assert kc.password_resets[-1][2] is True  # temporary again
    assert kc.password_resets[-1][1] == second_pw
    stored = next(iter(kc.users.values()))
    assert stored.emails_sent == 0  # still zero emails


@pytest.mark.asyncio
async def test_password_never_in_audit(
    app_password_mode: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
) -> None:
    # invite 后扫全部已 emit 的 audit 事件序列化 JSON,断言初始密码子串不出现。
    from expert_work.protocol import AuditQuery

    client, tenant_id, _app, _kc = app_password_mode
    resp = await client.post(
        "/v1/members/invite",
        json={"invitations": [{"email": "audit-pw@example.com", "role": "viewer"}]},
        headers=_admin_headers(tenant_id),
    )
    assert resp.status_code == 201, resp.text
    pw = resp.json()["data"]["results"][0]["initial_password"]
    assert pw is not None

    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    serialized = "\n".join(entry.model_dump_json() for entry in page.entries)
    assert pw not in serialized
