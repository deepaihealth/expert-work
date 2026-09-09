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


# --- member purge (X-4 ① 2026-09-09: two-step — target must be deactivated) ---


def _headers_as(tenant_id: UUID, subject: str, *roles: str) -> dict[str, str]:
    """JWT for ``subject`` (the Keycloak sub) carrying ``roles`` (default admin)."""
    token = make_test_jwt(tenant_id=tenant_id, subject=subject, roles=roles or ("admin",))
    return {"Authorization": f"Bearer {token}"}


def _operator_headers(tenant_id: UUID) -> dict[str, str]:
    return _headers_as(tenant_id, str(uuid4()), "operator")


async def _invite_one(
    client: AsyncClient, tenant_id: UUID, email: str = "leaver@co.com", role: str = "viewer"
) -> UUID:
    inv = await client.post(
        "/v1/members/invite",
        json={"invitations": [{"email": email, "role": role}]},
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


async def _deactivate(client: AsyncClient, tenant_id: UUID, member_id: UUID) -> None:
    """Step one of the two-step offboarding: ``DELETE`` (invited→revoked / active→suspended)."""
    resp = await client.delete(f"/v1/members/{member_id}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 204, resp.text


async def _kc_uuid(app: object, tenant_id: UUID, member_id: UUID) -> UUID:
    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)  # type: ignore[attr-defined]
    assert member is not None and member.keycloak_user_id is not None
    return UUID(member.keycloak_user_id)


async def _regrant(app: object, tenant_id: UUID, kc_uuid: UUID) -> None:
    """A role binding that survived / was re-granted after the suspend — the
    purge must sweep it (suspend already removed the original one)."""
    from expert_work.protocol import Role

    await app.state.role_binding_repo.create(  # type: ignore[attr-defined]
        subject_type="user",
        subject_id=kc_uuid,
        tenant_id=tenant_id,
        role=Role.VIEWER,
        granted_by="test",
    )


async def _purge_rows(audit_store: InMemoryAuditLogStore, tenant_id: UUID) -> list[object]:
    from expert_work.protocol import AuditAction, AuditQuery

    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    return [r for r in page.entries if r.action is AuditAction.MEMBER_PURGE]


@pytest.mark.asyncio
async def test_purge_suspended_member_deletes_kc_role_bindings_and_data(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
) -> None:
    """① suspended (signed in, has data) → KC account deleted, role bindings
    swept, data cascade ran, tenant_user deactivated, MEMBER_PURGE audited."""
    from orchestrator.tools.workspace_store import RecordingWorkspaceStore

    client, tenant_id, app, kc = admin_app
    # create_app has no supervisor URL in tests — wire the recording fake so
    # the workspace step of the cascade runs (otherwise it logs a failure and
    # ``purge.ok`` could never be asserted True).
    app.state.workspace_store = RecordingWorkspaceStore()  # type: ignore[attr-defined]
    member_id = await _invite_one(client, tenant_id)
    user_id, thread_id = await _activate_with_data(app, tenant_id, member_id)
    await _deactivate(client, tenant_id, member_id)
    kc_uuid = await _kc_uuid(app, tenant_id, member_id)
    await _regrant(app, tenant_id, kc_uuid)
    assert len(kc.users) == 1  # suspend only disabled the account

    actor = str(uuid4())
    resp = await client.post(
        f"/v1/members/{member_id}:purge", headers=_headers_as(tenant_id, actor)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["member_id"] == str(member_id)
    assert data["status"] == "suspended"  # no further transition
    assert data["kc_deleted"] is True
    assert data["role_bindings_removed"] == 1
    assert data["role_bindings_cleanup_failed"] is False
    assert data["data_purged"] is True
    assert data["data_purge_failed"] is False
    assert data["purge"]["user_id"] == str(user_id)
    assert data["purge"]["threads_purged"] == 1
    assert data["purge"]["deactivated"] is True
    assert data["purge"]["ok"] is True

    assert len(kc.users) == 0  # deleted, not merely disabled
    bindings = await app.state.role_binding_repo.list_for_subject(  # type: ignore[attr-defined]
        subject_type="user", subject_id=kc_uuid, tenant_id=tenant_id
    )
    assert bindings == []
    gone = await app.state.thread_meta_repo.get(thread_id, tenant_id=tenant_id)  # type: ignore[attr-defined]
    assert gone is None
    user = await app.state.tenant_user_repo.get(user_id, tenant_id=tenant_id)  # type: ignore[attr-defined]
    assert user is not None and user.deleted_at is not None
    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)  # type: ignore[attr-defined]
    assert member is not None and member.status == "suspended"

    rows = await _purge_rows(audit_store, tenant_id)
    assert len(rows) == 1
    assert rows[0].actor_id == actor  # type: ignore[attr-defined]
    assert rows[0].resource_id == str(member_id)  # type: ignore[attr-defined]
    details = rows[0].details  # type: ignore[attr-defined]
    assert details["email"] == "leaver@co.com"
    assert details["from_status"] == "suspended"
    assert details["kc_deleted"] is True
    assert details["role_bindings_removed"] == 1
    assert details["data_purged"] is True
    assert details["purge_ok"] is True  # ran AND every store succeeded


@pytest.mark.asyncio
async def test_purge_revoked_member_without_login_skips_data_step(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
) -> None:
    """② revoked (never signed in) → no data step; KC delete is a no-op
    (revoke already deleted the account) and stays idempotent."""
    client, tenant_id, app, kc = admin_app
    member_id = await _invite_one(client, tenant_id)
    await _deactivate(client, tenant_id, member_id)
    assert len(kc.users) == 0  # revoke deletes the KC account

    resp = await client.post(f"/v1/members/{member_id}:purge", headers=_admin_headers(tenant_id))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "revoked"
    assert data["kc_deleted"] is True
    assert data["data_purged"] is False  # subject_id NULL — never logged in
    assert data["data_purge_failed"] is False
    assert data["purge"] is None

    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)  # type: ignore[attr-defined]
    assert member is not None and member.status == "revoked"

    rows = await _purge_rows(audit_store, tenant_id)
    assert len(rows) == 1
    details = rows[0].details  # type: ignore[attr-defined]
    assert details["from_status"] == "revoked"
    assert details["data_purged"] is False
    # No data step ran at all — accountability distinguishes that from "ran
    # and every store succeeded" (True) and "ran, some store failed" (False).
    assert details["purge_ok"] is None


@pytest.mark.asyncio
async def test_purge_active_member_409_zero_side_effects(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
) -> None:
    """③ active → 409 MEMBER_NOT_DEACTIVATED; nothing touched (two-step guard)."""
    client, tenant_id, app, kc = admin_app
    member_id = await _invite_one(client, tenant_id)
    _user_id, thread_id = await _activate_with_data(app, tenant_id, member_id)
    kc_uuid = await _kc_uuid(app, tenant_id, member_id)

    resp = await client.post(f"/v1/members/{member_id}:purge", headers=_admin_headers(tenant_id))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MEMBER_NOT_DEACTIVATED"

    assert len(kc.users) == 1
    bindings = await app.state.role_binding_repo.list_for_subject(  # type: ignore[attr-defined]
        subject_type="user", subject_id=kc_uuid, tenant_id=tenant_id
    )
    assert len(bindings) == 1
    still = await app.state.thread_meta_repo.get(thread_id, tenant_id=tenant_id)  # type: ignore[attr-defined]
    assert still is not None
    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)  # type: ignore[attr-defined]
    assert member is not None and member.status == "active"
    assert await _purge_rows(audit_store, tenant_id) == []


@pytest.mark.asyncio
async def test_purge_invited_member_409(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """③b invited (pending) → 409 too; withdraw the invite (DELETE) first."""
    client, tenant_id, app, kc = admin_app
    member_id = await _invite_one(client, tenant_id)

    resp = await client.post(f"/v1/members/{member_id}:purge", headers=_admin_headers(tenant_id))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MEMBER_NOT_DEACTIVATED"
    assert len(kc.users) == 1
    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)  # type: ignore[attr-defined]
    assert member is not None and member.status == "invited"


@pytest.mark.asyncio
async def test_purge_self_409(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
) -> None:
    """④ the caller's own member row → 409 MEMBER_PURGE_SELF, even when it is
    already suspended (a still-valid JWT could otherwise finish the job)."""
    client, tenant_id, app, kc = admin_app
    # A second active admin so suspending "me" clears the last-admin gate.
    other = await _invite_one(client, tenant_id, email="other@co.com", role="admin")
    await _activate(app, tenant_id, other, "sub-other")
    member_id = await _invite_one(client, tenant_id, email="me@co.com", role="admin")
    _user_id, thread_id = await _activate_with_data(app, tenant_id, member_id)
    await _deactivate(client, tenant_id, member_id)
    kc_uuid = await _kc_uuid(app, tenant_id, member_id)

    resp = await client.post(
        f"/v1/members/{member_id}:purge", headers=_headers_as(tenant_id, str(kc_uuid))
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MEMBER_PURGE_SELF"

    assert len(kc.users) == 2  # "me" and "other" both still there
    still = await app.state.thread_meta_repo.get(thread_id, tenant_id=tenant_id)  # type: ignore[attr-defined]
    assert still is not None
    assert await _purge_rows(audit_store, tenant_id) == []


@pytest.mark.asyncio
async def test_purge_operator_forbidden(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """⑤ operator (no ``user:write``) → 403; nothing happens."""
    client, tenant_id, app, kc = admin_app
    member_id = await _invite_one(client, tenant_id)
    _user_id, thread_id = await _activate_with_data(app, tenant_id, member_id)
    await _deactivate(client, tenant_id, member_id)  # suspend: KC account only disabled
    resp = await client.post(f"/v1/members/{member_id}:purge", headers=_operator_headers(tenant_id))
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "FORBIDDEN"
    assert len(kc.users) == 1
    still = await app.state.thread_meta_repo.get(thread_id, tenant_id=tenant_id)  # type: ignore[attr-defined]
    assert still is not None


@pytest.mark.asyncio
async def test_purge_viewer_forbidden(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """⑤b viewer → 403; nothing happens."""
    client, tenant_id, _app, kc = admin_app
    member_id = await _invite_one(client, tenant_id)

    resp = await client.post(f"/v1/members/{member_id}:purge", headers=_viewer_headers(tenant_id))
    assert resp.status_code == 403
    assert len(kc.users) == 1  # untouched


@pytest.mark.asyncio
async def test_purge_missing_member_404(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    client, tenant_id, _app, _kc = admin_app
    resp = await client.post(f"/v1/members/{uuid4()}:purge", headers=_admin_headers(tenant_id))
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "MEMBER_NOT_FOUND"


@pytest.mark.asyncio
async def test_purge_kc_unavailable_502_leaves_local_state_untouched(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⑥ KC down → 502 KEYCLOAK_UNAVAILABLE and NO local row is touched (role
    binding, thread, tenant_user, member status, audit) — the call is
    retryable; once KC is back the same request completes."""
    client, tenant_id, app, kc = admin_app
    member_id = await _invite_one(client, tenant_id)
    user_id, thread_id = await _activate_with_data(app, tenant_id, member_id)
    await _deactivate(client, tenant_id, member_id)
    kc_uuid = await _kc_uuid(app, tenant_id, member_id)
    await _regrant(app, tenant_id, kc_uuid)

    real_delete = kc.delete_user

    async def _kc_boom(**_kwargs: object) -> None:
        raise KeycloakUnavailableError("forced-unavailable (test)")

    monkeypatch.setattr(kc, "delete_user", _kc_boom)

    resp = await client.post(f"/v1/members/{member_id}:purge", headers=_admin_headers(tenant_id))
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["code"] == "KEYCLOAK_UNAVAILABLE"

    assert len(kc.users) == 1
    bindings = await app.state.role_binding_repo.list_for_subject(  # type: ignore[attr-defined]
        subject_type="user", subject_id=kc_uuid, tenant_id=tenant_id
    )
    assert len(bindings) == 1
    still = await app.state.thread_meta_repo.get(thread_id, tenant_id=tenant_id)  # type: ignore[attr-defined]
    assert still is not None
    user = await app.state.tenant_user_repo.get(user_id, tenant_id=tenant_id)  # type: ignore[attr-defined]
    assert user is not None and user.deleted_at is None
    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)  # type: ignore[attr-defined]
    assert member is not None and member.status == "suspended"
    assert await _purge_rows(audit_store, tenant_id) == []

    # KC back → the retry completes the whole cascade.
    monkeypatch.setattr(kc, "delete_user", real_delete)
    retry = await client.post(f"/v1/members/{member_id}:purge", headers=_admin_headers(tenant_id))
    assert retry.status_code == 200, retry.text
    assert len(kc.users) == 0
    assert retry.json()["data"]["role_bindings_removed"] == 1
    assert retry.json()["data"]["data_purged"] is True


@pytest.mark.asyncio
async def test_purge_rerun_is_idempotent(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """⑦ re-running the purge is a safe no-op (200, every step no-ops)."""
    from orchestrator.tools.workspace_store import RecordingWorkspaceStore

    client, tenant_id, app, kc = admin_app
    app.state.workspace_store = RecordingWorkspaceStore()  # type: ignore[attr-defined]
    member_id = await _invite_one(client, tenant_id)
    await _activate_with_data(app, tenant_id, member_id)
    await _deactivate(client, tenant_id, member_id)

    first = await client.post(f"/v1/members/{member_id}:purge", headers=_admin_headers(tenant_id))
    assert first.status_code == 200, first.text

    second = await client.post(f"/v1/members/{member_id}:purge", headers=_admin_headers(tenant_id))
    assert second.status_code == 200, second.text
    data = second.json()["data"]
    assert data["status"] == "suspended"
    assert data["kc_deleted"] is True  # fake delete_user is idempotent (404 = gone)
    assert data["role_bindings_removed"] == 0
    assert data["role_bindings_cleanup_failed"] is False
    assert data["data_purged"] is True  # re-run retries the cascade, safe no-op
    assert data["purge"]["threads_purged"] == 0
    assert data["purge"]["ok"] is True
    assert len(kc.users) == 0

    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)  # type: ignore[attr-defined]
    assert member is not None and member.status == "suspended"


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
    from orchestrator.tools.workspace_store import RecordingWorkspaceStore

    client, tenant_id, app, _kc = admin_app
    # Supervisor wired so the ONLY failure is the injected one.
    app.state.workspace_store = RecordingWorkspaceStore()  # type: ignore[attr-defined]
    member_id = await _invite_one(client, tenant_id)
    await _activate_with_data(app, tenant_id, member_id)
    await _deactivate(client, tenant_id, member_id)

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

    rows = await _purge_rows(audit_store, tenant_id)
    assert len(rows) == 1
    assert rows[0].details["data_purged"] is True  # type: ignore[attr-defined]
    assert rows[0].details["purge_ok"] is False  # type: ignore[attr-defined]


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
    client, tenant_id, app, kc = admin_app
    member_id = await _invite_one(client, tenant_id)
    _user_id, thread_id = await _activate_with_data(app, tenant_id, member_id)
    await _deactivate(client, tenant_id, member_id)

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
    assert len(kc.users) == 0
    still = await app.state.thread_meta_repo.get(thread_id, tenant_id=tenant_id)  # type: ignore[attr-defined]
    assert still is not None  # data untouched — the operator must re-run

    rows = await _purge_rows(audit_store, tenant_id)
    assert len(rows) == 1  # the audit row lands even though the data step died
    details = rows[0].details  # type: ignore[attr-defined]
    assert details["kc_deleted"] is True
    assert details["data_purged"] is False
    assert details["data_purge_failed"] is True
    assert details["purge_ok"] is None


# --- last active admin guard on DELETE (X-4 ① follow-up, 2026-09-09) ---------


async def _activate(app: object, tenant_id: UUID, member_id: UUID, sub: str) -> None:
    """First-login promotion (invited → active) without seeding data."""
    from datetime import UTC, datetime

    user = await app.state.tenant_user_repo.resolve(  # type: ignore[attr-defined]
        tenant_id=tenant_id, subject_type="user", subject_id=sub, display_name=sub
    )
    moved = await app.state.tenant_member_repo.transition(  # type: ignore[attr-defined]
        member_id=member_id,
        tenant_id=tenant_id,
        to="active",
        now=datetime.now(UTC),
        subject_id=user.id,
    )
    assert moved


async def _status(app: object, tenant_id: UUID, member_id: UUID) -> str:
    member = await app.state.tenant_member_repo.get(tenant_id=tenant_id, member_id=member_id)  # type: ignore[attr-defined]
    assert member is not None
    return str(member.status)


@pytest.mark.asyncio
async def test_suspend_one_of_two_active_admins_ok(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """Two active admins → suspending one is fine (one stays)."""
    client, tenant_id, app, _kc = admin_app
    a = await _invite_one(client, tenant_id, email="a@co.com", role="admin")
    b = await _invite_one(client, tenant_id, email="b@co.com", role="admin")
    await _activate(app, tenant_id, a, "sub-a")
    await _activate(app, tenant_id, b, "sub-b")

    resp = await client.delete(f"/v1/members/{b}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 204, resp.text
    assert await _status(app, tenant_id, b) == "suspended"
    assert await _status(app, tenant_id, a) == "active"


@pytest.mark.asyncio
async def test_suspend_last_active_admin_409(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
) -> None:
    """The only active admin → 409 MEMBER_LAST_ADMIN; nothing touched."""
    from expert_work.protocol import AuditAction, AuditQuery

    client, tenant_id, app, kc = admin_app
    a = await _invite_one(client, tenant_id, email="a@co.com", role="admin")
    await _activate(app, tenant_id, a, "sub-a")
    kc_uuid = await _kc_uuid(app, tenant_id, a)

    resp = await client.delete(f"/v1/members/{a}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MEMBER_LAST_ADMIN"

    assert await _status(app, tenant_id, a) == "active"
    assert kc.users[str(kc_uuid)].user.enabled is True
    bindings = await app.state.role_binding_repo.list_for_subject(  # type: ignore[attr-defined]
        subject_type="user", subject_id=kc_uuid, tenant_id=tenant_id
    )
    assert len(bindings) == 1
    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    assert [r for r in page.entries if r.action is AuditAction.MEMBER_SUSPEND] == []


@pytest.mark.asyncio
async def test_suspend_self_as_last_active_admin_409(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """Self-suspend by the only active admin → 409 (no lock-out)."""
    client, tenant_id, app, _kc = admin_app
    a = await _invite_one(client, tenant_id, email="me@co.com", role="admin")
    await _activate(app, tenant_id, a, "sub-me")
    kc_uuid = await _kc_uuid(app, tenant_id, a)

    resp = await client.delete(f"/v1/members/{a}", headers=_headers_as(tenant_id, str(kc_uuid)))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MEMBER_LAST_ADMIN"
    assert await _status(app, tenant_id, a) == "active"


@pytest.mark.asyncio
async def test_suspended_and_invited_admins_do_not_count(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """Only ``active`` counts: a suspended admin and an invited admin beside
    the last active one do not unlock the suspend."""
    client, tenant_id, app, _kc = admin_app
    a = await _invite_one(client, tenant_id, email="a@co.com", role="admin")
    b = await _invite_one(client, tenant_id, email="b@co.com", role="admin")
    _c_invited = await _invite_one(client, tenant_id, email="c@co.com", role="admin")
    await _activate(app, tenant_id, a, "sub-a")
    await _activate(app, tenant_id, b, "sub-b")
    first = await client.delete(f"/v1/members/{b}", headers=_admin_headers(tenant_id))
    assert first.status_code == 204, first.text  # a still active

    resp = await client.delete(f"/v1/members/{a}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MEMBER_LAST_ADMIN"
    assert await _status(app, tenant_id, a) == "active"


@pytest.mark.asyncio
async def test_last_admin_gate_ignores_non_admins_and_invited_admins(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """A single active admin does not block suspending a viewer, nor
    withdrawing an admin *invite* (invited is not active)."""
    client, tenant_id, app, _kc = admin_app
    a = await _invite_one(client, tenant_id, email="a@co.com", role="admin")
    v = await _invite_one(client, tenant_id, email="v@co.com", role="viewer")
    i = await _invite_one(client, tenant_id, email="i@co.com", role="admin")
    await _activate(app, tenant_id, a, "sub-a")
    await _activate(app, tenant_id, v, "sub-v")

    # An active *viewer* beside the admin does not make the admin removable.
    resp = await client.delete(f"/v1/members/{a}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MEMBER_LAST_ADMIN"

    resp = await client.delete(f"/v1/members/{v}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 204, resp.text
    assert await _status(app, tenant_id, v) == "suspended"

    resp = await client.delete(f"/v1/members/{i}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 204, resp.text
    assert await _status(app, tenant_id, i) == "revoked"
    assert await _status(app, tenant_id, a) == "active"


# --- last active admin guard on DELETE /v1/role_bindings/{id} ----------------


async def _admin_binding_id(app: object, tenant_id: UUID, member_id: UUID) -> UUID:
    """The tenant-scope ADMIN binding the invite wrote for ``member_id``."""
    from expert_work.protocol import Role

    kc_uuid = await _kc_uuid(app, tenant_id, member_id)
    bindings = await app.state.role_binding_repo.list_for_subject(  # type: ignore[attr-defined]
        subject_type="user", subject_id=kc_uuid, tenant_id=tenant_id
    )
    admin = [b for b in bindings if b.role is Role.ADMIN and not b.platform_scope]
    assert len(admin) == 1
    return admin[0].id


@pytest.mark.asyncio
async def test_delete_last_admin_role_binding_409(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
    audit_store: InMemoryAuditLogStore,
) -> None:
    """Deleting the tenant-scope ADMIN binding of the only active admin →
    409 MEMBER_LAST_ADMIN; the binding stays."""
    from expert_work.protocol import AuditAction, AuditQuery

    client, tenant_id, app, _kc = admin_app
    a = await _invite_one(client, tenant_id, email="a@co.com", role="admin")
    await _activate(app, tenant_id, a, "sub-a")
    binding_id = await _admin_binding_id(app, tenant_id, a)

    resp = await client.delete(f"/v1/role_bindings/{binding_id}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MEMBER_LAST_ADMIN"

    still = await app.state.role_binding_repo.list_for_tenant(tenant_id=tenant_id)  # type: ignore[attr-defined]
    assert binding_id in {b.id for b in still}
    page = await audit_store.query(AuditQuery(tenant_id=tenant_id))
    assert [r for r in page.entries if r.action is AuditAction.ROLE_BINDING_DELETE] == []


@pytest.mark.asyncio
async def test_delete_admin_role_binding_with_other_active_admin_204(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """Another active admin (with their own ADMIN binding) keeps the tenant
    reachable → 204 and the binding is gone."""
    client, tenant_id, app, _kc = admin_app
    a = await _invite_one(client, tenant_id, email="a@co.com", role="admin")
    b = await _invite_one(client, tenant_id, email="b@co.com", role="admin")
    await _activate(app, tenant_id, a, "sub-a")
    await _activate(app, tenant_id, b, "sub-b")
    binding_b = await _admin_binding_id(app, tenant_id, b)

    resp = await client.delete(f"/v1/role_bindings/{binding_b}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 204, resp.text
    left = await app.state.role_binding_repo.list_for_tenant(tenant_id=tenant_id)  # type: ignore[attr-defined]
    assert binding_b not in {x.id for x in left}

    # …and now a's binding is the last one: the same gate refuses it.
    binding_a = await _admin_binding_id(app, tenant_id, a)
    resp = await client.delete(f"/v1/role_bindings/{binding_a}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_delete_role_binding_gate_only_counts_active_admin_members(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """An ADMIN binding whose member is only *invited* (never signed in) does
    not keep the tenant reachable — deleting the active admin's binding is
    still refused; a viewer's binding is never gated."""
    client, tenant_id, app, _kc = admin_app
    a = await _invite_one(client, tenant_id, email="a@co.com", role="admin")
    _i = await _invite_one(client, tenant_id, email="i@co.com", role="admin")  # stays invited
    v = await _invite_one(client, tenant_id, email="v@co.com", role="viewer")
    await _activate(app, tenant_id, a, "sub-a")
    await _activate(app, tenant_id, v, "sub-v")

    binding_a = await _admin_binding_id(app, tenant_id, a)
    resp = await client.delete(f"/v1/role_bindings/{binding_a}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MEMBER_LAST_ADMIN"

    kc_v = await _kc_uuid(app, tenant_id, v)
    v_bindings = await app.state.role_binding_repo.list_for_subject(  # type: ignore[attr-defined]
        subject_type="user", subject_id=kc_v, tenant_id=tenant_id
    )
    assert len(v_bindings) == 1
    resp = await client.delete(
        f"/v1/role_bindings/{v_bindings[0].id}", headers=_admin_headers(tenant_id)
    )
    assert resp.status_code == 204, resp.text


@pytest.mark.asyncio
async def test_platform_scope_binding_is_outside_the_gate(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """Platform-scope bindings are neither counted nor blocked: a
    SYSTEM_ADMIN grant on the last admin's subject does not substitute for
    the tenant ADMIN binding (still 409), and the tenant route cannot reach
    the platform row at all (404, unchanged)."""
    from expert_work.protocol import Role

    client, tenant_id, app, _kc = admin_app
    a = await _invite_one(client, tenant_id, email="a@co.com", role="admin")
    await _activate(app, tenant_id, a, "sub-a")
    kc_a = await _kc_uuid(app, tenant_id, a)
    platform = await app.state.role_binding_repo.create(  # type: ignore[attr-defined]
        subject_type="user",
        subject_id=kc_a,
        tenant_id=None,
        role=Role.SYSTEM_ADMIN,
        granted_by="test",
        platform_scope=True,
    )
    binding_a = await _admin_binding_id(app, tenant_id, a)

    resp = await client.delete(f"/v1/role_bindings/{binding_a}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 409, resp.text

    resp = await client.delete(
        f"/v1/role_bindings/{platform.id}", headers=_admin_headers(tenant_id)
    )
    assert resp.status_code == 404, resp.text
    assert (
        await app.state.role_binding_repo.get_platform_admin_for_subject(  # type: ignore[attr-defined]
            subject_type="user", subject_id=kc_a
        )
        is not None
    )


@pytest.mark.asyncio
async def test_suspend_gate_requires_a_live_admin_binding(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """The member-suspend gate shares the joint check: a second active admin
    whose ADMIN binding is already gone does not keep the tenant reachable."""
    client, tenant_id, app, _kc = admin_app
    a = await _invite_one(client, tenant_id, email="a@co.com", role="admin")
    b = await _invite_one(client, tenant_id, email="b@co.com", role="admin")
    await _activate(app, tenant_id, a, "sub-a")
    await _activate(app, tenant_id, b, "sub-b")
    binding_b = await _admin_binding_id(app, tenant_id, b)
    resp = await client.delete(f"/v1/role_bindings/{binding_b}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 204, resp.text

    resp = await client.delete(f"/v1/members/{a}", headers=_admin_headers(tenant_id))
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "MEMBER_LAST_ADMIN"
    assert await _status(app, tenant_id, a) == "active"


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


@pytest.mark.asyncio
async def test_list_carries_last_active_at(
    admin_app: tuple[AsyncClient, UUID, object, FakeKeycloakAdminClient],
) -> None:
    """成员列表带「最后活跃」(2026-08-27):activated 行 join tenant_user.
    last_active_at,invited 行(subject_id 为空)为 null。"""
    client, tenant_id, app, _kc = admin_app
    inv = await client.post(
        "/v1/members/invite",
        json={
            "invitations": [
                {"email": "a@co.com", "role": "viewer"},
                {"email": "b@co.com", "role": "viewer"},
            ]
        },
        headers=_admin_headers(tenant_id),
    )
    results = inv.json()["data"]["results"]
    member_a = UUID(results[0]["member_id"])

    # 模拟 a 已激活:tenant_user 行存在(resolve 会 bump last_active_at),
    # roster 行 transition 到 active 并回填 subject_id。
    user = await app.state.tenant_user_repo.resolve(  # type: ignore[attr-defined]
        tenant_id=tenant_id, subject_type="user", subject_id="kc-a"
    )
    from datetime import UTC, datetime

    moved = await app.state.tenant_member_repo.transition(  # type: ignore[attr-defined]
        member_id=member_a,
        tenant_id=tenant_id,
        to="active",
        now=datetime.now(UTC),
        subject_id=user.id,
    )
    assert moved

    resp = await client.get("/v1/members", headers=_admin_headers(tenant_id))
    assert resp.status_code == 200
    items = {i["email"]: i for i in resp.json()["data"]["items"]}
    assert items["a@co.com"]["last_active_at"] is not None
    assert items["b@co.com"]["last_active_at"] is None
