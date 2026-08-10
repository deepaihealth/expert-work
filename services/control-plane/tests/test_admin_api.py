"""End-to-end tests for ``/v1/service_accounts``, ``/v1/api_keys``,
``/v1/role_bindings`` admin endpoints — Stream C.3.

Tests piggy-back on the existing in-memory app fixture from conftest,
issuing JWTs with the appropriate role claim. The API-key bearer is
also exercised end-to-end: after creating a key via the admin API we
turn around and use it to call ``/v1/agents`` (which permits any
authenticated principal in M0).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from control_plane.api.api_keys import _vault_name
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditAction, AuditQuery
from expert_work.runtime.secret_store import SecretNotFoundError
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID


@pytest.fixture
def audit_store() -> InMemoryAuditLogStore:
    return InMemoryAuditLogStore()


def _admin_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + make_test_jwt(tenant_id=_TENANT, subject="admin-user", roles=("admin",))
    }


def _viewer_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer "
        + make_test_jwt(tenant_id=_TENANT, subject="viewer-user", roles=("viewer",))
    }


@pytest.fixture
async def admin_client(
    audit_store: InMemoryAuditLogStore,
) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        yield client


@pytest.fixture
async def admin_client_and_app(
    audit_store: InMemoryAuditLogStore,
) -> AsyncIterator[tuple[AsyncClient, FastAPI]]:
    """Same wiring as ``admin_client``, but also yields ``app`` so a test
    can reach ``app.state`` directly — e.g. to seed an ``api_key`` row
    that bypasses the create endpoint (and so never lands in the vault),
    or to inspect ``app.state.secret_store`` after an endpoint call."""
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        yield client, app


# ---------------------------------------------------------------------------
# /v1/service_accounts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_can_create_and_list_service_accounts(
    admin_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    create = await admin_client.post(
        "/v1/service_accounts",
        json={"name": "ingestion-bot", "description": "automated ingestion"},
        headers=_admin_headers(),
    )
    assert create.status_code == 201
    sa = create.json()["data"]
    assert sa["name"] == "ingestion-bot"

    listing = await admin_client.get("/v1/service_accounts", headers=_admin_headers())
    assert listing.status_code == 200
    items = listing.json()["data"]["items"]
    assert len(items) == 1

    page = await audit_store.query(AuditQuery(tenant_id=_TENANT))
    assert any(
        r.action is AuditAction.SERVICE_ACCOUNT_CREATE and r.resource_id == sa["id"]
        for r in page.entries
    )


@pytest.mark.asyncio
async def test_duplicate_service_account_returns_409(admin_client: AsyncClient) -> None:
    await admin_client.post(
        "/v1/service_accounts",
        json={"name": "dup", "description": ""},
        headers=_admin_headers(),
    )
    second = await admin_client.post(
        "/v1/service_accounts",
        json={"name": "dup", "description": ""},
        headers=_admin_headers(),
    )
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_viewer_cannot_create_service_account(admin_client: AsyncClient) -> None:
    response = await admin_client.post(
        "/v1/service_accounts",
        json={"name": "denied", "description": ""},
        headers=_viewer_headers(),
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_delete_service_account(admin_client: AsyncClient) -> None:
    create = await admin_client.post(
        "/v1/service_accounts",
        json={"name": "to-delete", "description": ""},
        headers=_admin_headers(),
    )
    sa_id = create.json()["data"]["id"]
    deleted = await admin_client.delete(f"/v1/service_accounts/{sa_id}", headers=_admin_headers())
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_delete_service_account_purges_vault_orphans(
    admin_client_and_app: tuple[AsyncClient, FastAPI],
) -> None:
    """``api_key`` rows FK-cascade (``ondelete=CASCADE``) when the parent
    ``service_account`` is deleted, but the vault has no such cascade —
    the endpoint must purge each key's vault entry itself or the
    plaintext orphans forever with the DB row gone."""
    client, app = admin_client_and_app
    minted = await _mint_key(client)
    sa_id = minted["api_key"]["service_account_id"]
    key_id = minted["api_key"]["id"]

    deleted = await client.delete(f"/v1/service_accounts/{sa_id}", headers=_admin_headers())
    assert deleted.status_code == 204

    vault_name = _vault_name(_TENANT, UUID(key_id))
    with pytest.raises(SecretNotFoundError):
        await app.state.secret_store.get(vault_name)


# ---------------------------------------------------------------------------
# /v1/service_accounts/{id}/api_keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_can_mint_api_key_and_then_use_it(
    admin_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    sa = (
        await admin_client.post(
            "/v1/service_accounts",
            json={"name": "robot", "description": ""},
            headers=_admin_headers(),
        )
    ).json()["data"]

    create_key = await admin_client.post(
        f"/v1/service_accounts/{sa['id']}/api_keys",
        json={"scopes": ["admin"], "expires_at": None},
        headers=_admin_headers(),
    )
    assert create_key.status_code == 201
    body = create_key.json()["data"]
    plaintext = body["plaintext"]
    assert plaintext.startswith("aforge_pat_")
    # Plaintext is only returned this once.
    assert "plaintext" in body
    # The key row mirrors the SA.
    assert body["api_key"]["service_account_id"] == sa["id"]

    # Now turn around and use the API key to authenticate to a non-admin route.
    response = await admin_client.get(
        "/v1/agents", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert response.status_code == 200

    page = await audit_store.query(AuditQuery(tenant_id=_TENANT))
    assert any(r.action is AuditAction.API_KEY_CREATE for r in page.entries)


@pytest.mark.asyncio
async def test_create_api_key_for_unknown_sa_returns_404(admin_client: AsyncClient) -> None:
    response = await admin_client.post(
        f"/v1/service_accounts/{uuid4()}/api_keys",
        json={"scopes": ["read"]},
        headers=_admin_headers(),
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_revoked_api_key_no_longer_authenticates(
    admin_client: AsyncClient,
) -> None:
    sa = (
        await admin_client.post(
            "/v1/service_accounts",
            json={"name": "to-revoke", "description": ""},
            headers=_admin_headers(),
        )
    ).json()["data"]
    create_key = await admin_client.post(
        f"/v1/service_accounts/{sa['id']}/api_keys",
        json={"scopes": ["admin"]},
        headers=_admin_headers(),
    )
    body = create_key.json()["data"]
    key_id = body["api_key"]["id"]
    plaintext = body["plaintext"]

    # Pre-revoke: works.
    pre = await admin_client.get("/v1/agents", headers={"Authorization": f"Bearer {plaintext}"})
    assert pre.status_code == 200

    # Revoke.
    revoked = await admin_client.delete(f"/v1/api_keys/{key_id}", headers=_admin_headers())
    assert revoked.status_code == 204

    # Post-revoke: 401.
    post = await admin_client.get("/v1/agents", headers={"Authorization": f"Bearer {plaintext}"})
    assert post.status_code == 401


# ---------------------------------------------------------------------------
# /v1/api_keys/{id}/rotate — Stream K.K1 double-active rotation
# ---------------------------------------------------------------------------


async def _mint_key(admin_client: AsyncClient) -> dict[str, object]:
    """Helper: create a service account + an API key, return the
    ``data`` envelope from the create_api_key response (with plaintext)."""
    sa = (
        await admin_client.post(
            "/v1/service_accounts",
            json={"name": f"rot-{uuid4().hex[:8]}", "description": ""},
            headers=_admin_headers(),
        )
    ).json()["data"]
    create_key = await admin_client.post(
        f"/v1/service_accounts/{sa['id']}/api_keys",
        json={"scopes": ["admin"]},
        headers=_admin_headers(),
    )
    assert create_key.status_code == 201
    return create_key.json()["data"]  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_api_key_rotation_returns_new_plaintext_and_marks_old_rotated(
    admin_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    """``/rotate`` mints a fresh bearer and stamps ``rotated_at`` on the old row."""
    minted = await _mint_key(admin_client)
    old_key_id = minted["api_key"]["id"]
    old_plaintext = minted["plaintext"]

    response = await admin_client.post(
        f"/v1/api_keys/{old_key_id}/rotate",
        json={"grace_period_s": 300},
        headers=_admin_headers(),
    )
    assert response.status_code == 201, response.text
    body = response.json()["data"]

    # Old row carries rotated_at + grace_period_s, same id.
    assert body["old_api_key"]["id"] == old_key_id
    assert body["old_api_key"]["rotated_at"] is not None
    assert body["old_api_key"]["grace_period_s"] == 300

    # New row is a different ApiKey with a fresh plaintext.
    new_plaintext = body["new_api_key"]["plaintext"]
    assert new_plaintext.startswith("aforge_pat_")
    assert new_plaintext != old_plaintext
    assert body["new_api_key"]["api_key"]["id"] != old_key_id
    # Scopes / service_account_id inherited from the old key.
    assert (
        body["new_api_key"]["api_key"]["service_account_id"]
        == minted["api_key"]["service_account_id"]
    )
    assert body["new_api_key"]["api_key"]["scopes"] == ["admin"]

    # Audit row landed.
    page = await audit_store.query(AuditQuery(tenant_id=_TENANT))
    rotate_rows = [r for r in page.entries if r.action is AuditAction.API_KEY_ROTATE]
    assert len(rotate_rows) == 1
    assert rotate_rows[0].resource_id == old_key_id
    assert rotate_rows[0].details["new_api_key_id"] == body["new_api_key"]["api_key"]["id"]
    assert rotate_rows[0].details["grace_period_s"] == 300


@pytest.mark.asyncio
async def test_api_key_rotation_double_active_during_grace(
    admin_client: AsyncClient,
) -> None:
    """Inside the grace window both old and new bearers authenticate.

    Uses ``grace_period_s=3600`` so the window is wide open during the
    test (no clock manipulation needed).
    """
    minted = await _mint_key(admin_client)
    old_plaintext = minted["plaintext"]

    response = await admin_client.post(
        f"/v1/api_keys/{minted['api_key']['id']}/rotate",
        json={"grace_period_s": 3600},
        headers=_admin_headers(),
    )
    assert response.status_code == 201
    new_plaintext = response.json()["data"]["new_api_key"]["plaintext"]

    # Both verify.
    old_resp = await admin_client.get(
        "/v1/agents", headers={"Authorization": f"Bearer {old_plaintext}"}
    )
    new_resp = await admin_client.get(
        "/v1/agents", headers={"Authorization": f"Bearer {new_plaintext}"}
    )
    assert old_resp.status_code == 200, "old bearer must still verify inside grace"
    assert new_resp.status_code == 200, "new bearer must verify immediately"


@pytest.mark.asyncio
async def test_api_key_rotation_old_rejected_after_grace_expires(
    admin_client: AsyncClient,
) -> None:
    """With ``grace_period_s=0`` the old bearer is dead immediately.

    This is the corner case for emergency rotation — the operator
    wanted an effective immediate revocation but with a paired
    replacement minted in one call. After grace=0, the old bearer
    fails to authenticate while the new bearer works.
    """
    minted = await _mint_key(admin_client)
    old_plaintext = minted["plaintext"]

    response = await admin_client.post(
        f"/v1/api_keys/{minted['api_key']['id']}/rotate",
        json={"grace_period_s": 0},
        headers=_admin_headers(),
    )
    assert response.status_code == 201
    new_plaintext = response.json()["data"]["new_api_key"]["plaintext"]

    old_resp = await admin_client.get(
        "/v1/agents", headers={"Authorization": f"Bearer {old_plaintext}"}
    )
    new_resp = await admin_client.get(
        "/v1/agents", headers={"Authorization": f"Bearer {new_plaintext}"}
    )
    assert old_resp.status_code == 401, "old bearer must die when grace=0"
    assert new_resp.status_code == 200, "new bearer must work"


@pytest.mark.asyncio
async def test_api_key_rotation_unknown_id_returns_404(admin_client: AsyncClient) -> None:
    response = await admin_client.post(
        f"/v1/api_keys/{uuid4()}/rotate",
        json={"grace_period_s": 300},
        headers=_admin_headers(),
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_api_key_rotation_refuses_already_rotated_key(
    admin_client: AsyncClient,
) -> None:
    """Rotating an already-rotated key returns 404 — operators pick one
    action at a time so the audit trail stays unambiguous."""
    minted = await _mint_key(admin_client)
    key_id = minted["api_key"]["id"]

    first = await admin_client.post(
        f"/v1/api_keys/{key_id}/rotate",
        json={"grace_period_s": 300},
        headers=_admin_headers(),
    )
    assert first.status_code == 201

    second = await admin_client.post(
        f"/v1/api_keys/{key_id}/rotate",
        json={"grace_period_s": 300},
        headers=_admin_headers(),
    )
    assert second.status_code == 404


@pytest.mark.asyncio
async def test_api_key_rotation_refuses_revoked_key(admin_client: AsyncClient) -> None:
    """``DELETE`` followed by ``/rotate`` returns 404 — same reason."""
    minted = await _mint_key(admin_client)
    key_id = minted["api_key"]["id"]

    revoke = await admin_client.delete(f"/v1/api_keys/{key_id}", headers=_admin_headers())
    assert revoke.status_code == 204

    rotated = await admin_client.post(
        f"/v1/api_keys/{key_id}/rotate",
        json={"grace_period_s": 300},
        headers=_admin_headers(),
    )
    assert rotated.status_code == 404


# ---------------------------------------------------------------------------
# /v1/api_keys/{id}/reveal — plaintext re-display via the encrypted vault
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_then_reveal_roundtrip(admin_client: AsyncClient) -> None:
    """The plaintext handed back at creation is exactly what reveal returns."""
    minted = await _mint_key(admin_client)
    key_id = minted["api_key"]["id"]
    plaintext = minted["plaintext"]

    revealed = await admin_client.post(f"/v1/api_keys/{key_id}/reveal", headers=_admin_headers())
    assert revealed.status_code == 200
    body = revealed.json()
    assert body["success"] is True
    assert body["error"] is None
    assert body["data"]["plaintext"] == plaintext


@pytest.mark.asyncio
async def test_reveal_is_audited(
    admin_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    """Reveal writes an ``api_key:reveal`` audit row, and no audit row's
    serialized JSON ever contains the plaintext (details stay clean)."""
    minted = await _mint_key(admin_client)
    key_id = minted["api_key"]["id"]
    plaintext = minted["plaintext"]

    revealed = await admin_client.post(f"/v1/api_keys/{key_id}/reveal", headers=_admin_headers())
    assert revealed.status_code == 200

    page = await audit_store.query(AuditQuery(tenant_id=_TENANT))
    reveal_rows = [r for r in page.entries if r.action is AuditAction.API_KEY_REVEAL]
    assert len(reveal_rows) == 1
    assert reveal_rows[0].resource_id == key_id

    for row in page.entries:
        assert plaintext not in row.model_dump_json()


@pytest.mark.asyncio
async def test_reveal_predates_vault_404(
    admin_client_and_app: tuple[AsyncClient, FastAPI],
) -> None:
    """A key row created directly in the store — bypassing the create
    endpoint, so nothing was ever ``put`` into the vault — reveals as
    404 ``API_KEY_PLAINTEXT_UNAVAILABLE`` (the pre-existing-key path)."""
    client, app = admin_client_and_app
    sa = (
        await client.post(
            "/v1/service_accounts",
            json={"name": "predates-vault", "description": ""},
            headers=_admin_headers(),
        )
    ).json()["data"]

    row = await app.state.api_key_repo.create(
        tenant_id=_TENANT,
        service_account_id=UUID(sa["id"]),
        prefix=f"hk_{uuid4().hex[:8]}",
        secret_hash="x" * 64,
        scopes=[],
        expires_at=None,
        created_by="seed",
    )

    revealed = await client.post(f"/v1/api_keys/{row.id}/reveal", headers=_admin_headers())
    assert revealed.status_code == 404
    assert revealed.json()["detail"]["code"] == "API_KEY_PLAINTEXT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_revoke_clears_vault(
    admin_client_and_app: tuple[AsyncClient, FastAPI],
) -> None:
    """``DELETE /v1/api_keys/{id}`` also purges the vault entry — ``get()``
    on that name raises ``SecretNotFoundError`` afterward."""
    client, app = admin_client_and_app
    minted = await _mint_key(client)
    key_id = minted["api_key"]["id"]

    revoke = await client.delete(f"/v1/api_keys/{key_id}", headers=_admin_headers())
    assert revoke.status_code == 204

    vault_name = _vault_name(_TENANT, UUID(key_id))
    with pytest.raises(SecretNotFoundError):
        await app.state.secret_store.get(vault_name)


@pytest.mark.asyncio
async def test_rotate_stores_new_plaintext(admin_client: AsyncClient) -> None:
    """After rotate, revealing the *new* key id returns the rotated
    plaintext — not the plaintext from the original create call."""
    minted = await _mint_key(admin_client)
    old_plaintext = minted["plaintext"]

    rotate = await admin_client.post(
        f"/v1/api_keys/{minted['api_key']['id']}/rotate",
        json={"grace_period_s": 300},
        headers=_admin_headers(),
    )
    assert rotate.status_code == 201
    new_body = rotate.json()["data"]["new_api_key"]
    new_key_id = new_body["api_key"]["id"]
    new_plaintext = new_body["plaintext"]
    assert new_plaintext != old_plaintext

    revealed = await admin_client.post(
        f"/v1/api_keys/{new_key_id}/reveal", headers=_admin_headers()
    )
    assert revealed.status_code == 200
    assert revealed.json()["data"]["plaintext"] == new_plaintext


@pytest.mark.asyncio
async def test_list_response_has_no_plaintext(admin_client: AsyncClient) -> None:
    """The plaintext must never leak through ``GET /v1/api_keys`` — neither
    the actual secret string nor a stray ``plaintext`` field on any row."""
    minted = await _mint_key(admin_client)
    plaintext = minted["plaintext"]

    listing = await admin_client.get("/v1/api_keys", headers=_admin_headers())
    assert listing.status_code == 200
    assert plaintext not in listing.text
    for item in listing.json()["data"]["items"]:
        assert "plaintext" not in item


@pytest.mark.asyncio
async def test_reveal_rejects_other_tenant_key(admin_client: AsyncClient) -> None:
    """Tenant A must not reveal tenant B's key — the tenant filter has to
    reject before the vault is ever touched, so this is 404
    ``API_KEY_NOT_FOUND`` (not ``API_KEY_PLAINTEXT_UNAVAILABLE``, which
    would imply the key was found but had no vault entry)."""
    other_tenant = uuid4()
    other_headers = {
        "Authorization": "Bearer "
        + make_test_jwt(tenant_id=other_tenant, subject="tenant-b-admin", roles=("admin",))
    }

    sa = (
        await admin_client.post(
            "/v1/service_accounts",
            json={"name": "tenant-b-sa", "description": ""},
            headers=other_headers,
        )
    ).json()["data"]
    create_key = await admin_client.post(
        f"/v1/service_accounts/{sa['id']}/api_keys",
        json={"scopes": ["admin"]},
        headers=other_headers,
    )
    assert create_key.status_code == 201
    key_id = create_key.json()["data"]["api_key"]["id"]

    revealed = await admin_client.post(f"/v1/api_keys/{key_id}/reveal", headers=_admin_headers())
    assert revealed.status_code == 404
    assert revealed.json()["detail"]["code"] == "API_KEY_NOT_FOUND"


# ---------------------------------------------------------------------------
# /v1/role_bindings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_can_grant_and_revoke_role_binding(
    admin_client: AsyncClient,
) -> None:
    subject_id = uuid4()
    create = await admin_client.post(
        "/v1/role_bindings",
        json={
            "subject_type": "service_account",
            "subject_id": str(subject_id),
            "role": "operator",
        },
        headers=_admin_headers(),
    )
    assert create.status_code == 201
    binding = create.json()["data"]
    assert binding["role"] == "operator"

    listing = await admin_client.get("/v1/role_bindings", headers=_admin_headers())
    assert listing.status_code == 200
    assert listing.json()["data"]["total"] == 1

    deleted = await admin_client.delete(
        f"/v1/role_bindings/{binding['id']}", headers=_admin_headers()
    )
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_duplicate_role_binding_returns_409(admin_client: AsyncClient) -> None:
    subject_id = uuid4()
    payload = {
        "subject_type": "user",
        "subject_id": str(subject_id),
        "role": "viewer",
    }
    await admin_client.post("/v1/role_bindings", json=payload, headers=_admin_headers())
    second = await admin_client.post("/v1/role_bindings", json=payload, headers=_admin_headers())
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_viewer_cannot_grant_roles(admin_client: AsyncClient) -> None:
    response = await admin_client.post(
        "/v1/role_bindings",
        json={
            "subject_type": "user",
            "subject_id": str(uuid4()),
            "role": "admin",
        },
        headers=_viewer_headers(),
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Cross-tenant guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_cannot_see_other_tenants_service_accounts(
    admin_client: AsyncClient,
) -> None:
    await admin_client.post(
        "/v1/service_accounts",
        json={"name": "tenant-a-sa", "description": ""},
        headers=_admin_headers(),
    )
    other_tenant = UUID("11111111-1111-1111-1111-111111111111")
    other_jwt = make_test_jwt(tenant_id=other_tenant, subject="other-admin", roles=("admin",))
    listing = await admin_client.get(
        "/v1/service_accounts",
        headers={"Authorization": f"Bearer {other_jwt}"},
    )
    assert listing.status_code == 200
    assert listing.json()["data"]["total"] == 0
