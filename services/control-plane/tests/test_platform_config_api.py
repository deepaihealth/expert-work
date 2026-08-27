"""Endpoint tests for ``/v1/platform/credentials`` — Stream P (P-11/P-12)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.api.platform_config import _canonical_secret_name
from control_plane.app import create_app
from control_plane.auth import JWTVerifier
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle
from expert_work.protocol import Role
from expert_work.runtime.secret_store import SecretNotFoundError
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
    resp = await client.get("/v1/platform/credentials", headers=_headers(uuid4()))
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "PLATFORM_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_get_lists_full_catalog(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    resp = await client.get("/v1/platform/credentials", headers=_headers(admin))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    provs = {p["provider"]: p for p in data["providers"]}
    assert "anthropic" in provs  # full catalog rendered
    assert provs["anthropic"]["source"] == "unset"  # nothing configured yet
    assert provs["anthropic"]["secret_ref"] is None
    assert {t["tool"] for t in data["tools"]} == {"web_search"}


@pytest.mark.asyncio
async def test_put_provider_then_get_reflects_db_source(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    put = await client.put(
        "/v1/platform/credentials/providers/anthropic",
        json={"secret_ref": "kms://platform/anthropic", "enabled": True},
        headers=_headers(admin),
    )
    assert put.status_code == 200, put.text
    assert put.json()["data"]["secret_ref"] == "kms://platform/anthropic"

    got = await client.get("/v1/platform/credentials", headers=_headers(admin))
    row = next(p for p in got.json()["data"]["providers"] if p["provider"] == "anthropic")
    assert row["source"] == "db"
    assert row["secret_ref"] == "kms://platform/anthropic"
    assert row["enabled"] is True


@pytest.mark.asyncio
async def test_put_unknown_provider_422(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/credentials/providers/not-a-provider",
        json={"secret_ref": "kms://x"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "UNKNOWN_PROVIDER"


@pytest.mark.asyncio
async def test_put_plaintext_ref_rejected(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/credentials/providers/anthropic",
        json={"secret_ref": "sk-ant-plaintext"},
        headers=_headers(admin),
    )
    assert resp.status_code == 422  # PlatformSecretWrite ref validator


@pytest.mark.asyncio
async def test_paste_raw_value_encrypts_and_stores_ref(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    """Stream Q (PR C) — pasting a raw ``value`` encrypts it into the
    SecretStore and stores only the generated ``secret://`` ref in the catalog;
    the value is resolvable via the store but never appears in the catalog."""
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        put = await client.put(
            "/v1/platform/credentials/providers/anthropic",
            json={"value": "sk-ant-REAL-KEY", "enabled": True},
            headers=_headers(admin),
        )
        assert put.status_code == 200, put.text
        ref = put.json()["data"]["secret_ref"]
        # Catalog holds a generated ref, not the value.
        assert ref == "secret://expert-work/platform/llm/anthropic"
        assert "sk-ant-REAL-KEY" not in put.text

        # The value is resolvable through the SecretStore the app wired.
        resolved = await app.state.secret_store.get("expert-work/platform/llm/anthropic")  # type: ignore[attr-defined]
        assert resolved == "sk-ant-REAL-KEY"


@pytest.mark.asyncio
async def test_put_rejects_both_value_and_ref(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/credentials/providers/anthropic",
        json={"secret_ref": "kms://x", "value": "sk-ant", "enabled": True},
        headers=_headers(admin),
    )
    assert resp.status_code == 422  # exactly-one-of validator


@pytest.mark.asyncio
async def test_put_rejects_neither_value_nor_ref(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    resp = await client.put(
        "/v1/platform/credentials/providers/anthropic",
        json={"enabled": True},
        headers=_headers(admin),
    )
    assert resp.status_code == 422  # exactly-one-of validator


@pytest.mark.asyncio
async def test_delete_provider_204_then_404(admin_client: tuple[AsyncClient, UUID]) -> None:
    client, admin = admin_client
    await client.put(
        "/v1/platform/credentials/providers/openai",
        json={"secret_ref": "kms://openai", "enabled": True},
        headers=_headers(admin),
    )
    first = await client.delete(
        "/v1/platform/credentials/providers/openai", headers=_headers(admin)
    )
    assert first.status_code == 204
    second = await client.delete(
        "/v1/platform/credentials/providers/openai", headers=_headers(admin)
    )
    assert second.status_code == 404


@pytest.mark.asyncio
async def test_delete_env_defined_provider_409(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    settings = settings.model_copy(
        update={
            "supported_providers": ["anthropic"],
            "platform_provider_credentials": {"anthropic": "secret://env-anthropic"},
        }
    )
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        resp = await client.delete(
            "/v1/platform/credentials/providers/anthropic", headers=_headers(admin)
        )
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "PLATFORM_CREDENTIAL_IN_USE"


# ─── per-tenant overrides (Stream HX-8) ────────────────────────────────


async def _seed_tenant(app: object) -> UUID:
    tenant_id = uuid4()
    await app.state.tenant_config_repo.create(  # type: ignore[attr-defined]
        tenant_id=tenant_id, display_name="T", actor_id="seed"
    )
    return tenant_id


@pytest.mark.asyncio
async def test_tenant_override_put_get_delete_round_trip(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    tenant_id = await _seed_tenant(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        # Upsert with a pasted value → encrypted, tenant-namespaced ref.
        put = await client.put(
            f"/v1/platform/credentials/tenants/{tenant_id}/providers/anthropic",
            json={"value": "sk-ant-TENANT-KEY", "enabled": True},
            headers=_headers(admin),
        )
        assert put.status_code == 200, put.text
        ref = put.json()["data"]["secret_ref"]
        assert ref == f"secret://expert-work/platform/tenant/{tenant_id}/llm/anthropic"
        assert "sk-ant-TENANT-KEY" not in put.text

        # Tenant view: override row present, effective source = tenant.
        view = await client.get(
            f"/v1/platform/credentials/tenants/{tenant_id}", headers=_headers(admin)
        )
        assert view.status_code == 200, view.text
        provs = {p["provider"]: p for p in view.json()["data"]["providers"]}
        assert provs["anthropic"]["effective_source"] == "tenant"
        assert provs["anthropic"]["effective_ref"] == ref
        assert provs["anthropic"]["override"]["enabled"] is True

        # Catalog GET counts the override.
        catalog = await client.get("/v1/platform/credentials", headers=_headers(admin))
        cat_provs = {p["provider"]: p for p in catalog.json()["data"]["providers"]}
        assert cat_provs["anthropic"]["tenant_override_count"] == 1

        # Delete → fallback; 404 on the second delete.
        deleted = await client.delete(
            f"/v1/platform/credentials/tenants/{tenant_id}/providers/anthropic",
            headers=_headers(admin),
        )
        assert deleted.status_code == 204
        again = await client.delete(
            f"/v1/platform/credentials/tenants/{tenant_id}/providers/anthropic",
            headers=_headers(admin),
        )
        assert again.status_code == 404


@pytest.mark.asyncio
async def test_tenant_override_disabled_shows_suppressed(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    tenant_id = await _seed_tenant(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        # Platform row first, then a disabled tenant override on top.
        await client.put(
            "/v1/platform/credentials/tools/web_search",
            json={"secret_ref": "kms://platform/tavily", "enabled": True},
            headers=_headers(admin),
        )
        put = await client.put(
            f"/v1/platform/credentials/tenants/{tenant_id}/tools/web_search",
            json={"secret_ref": "kms://tenant/tavily", "enabled": False},
            headers=_headers(admin),
        )
        assert put.status_code == 200, put.text

        view = await client.get(
            f"/v1/platform/credentials/tenants/{tenant_id}", headers=_headers(admin)
        )
        tools = {t["tool"]: t for t in view.json()["data"]["tools"]}
        # HX-H2: disabled override suppresses — no fallback to the platform ref.
        assert tools["web_search"]["effective_source"] == "suppressed"
        assert tools["web_search"]["effective_ref"] is None


@pytest.mark.asyncio
async def test_tenant_override_unknown_tenant_404(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    resp = await client.put(
        f"/v1/platform/credentials/tenants/{uuid4()}/providers/anthropic",
        json={"secret_ref": "kms://x", "enabled": True},
        headers=_headers(admin),
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "TENANT_NOT_FOUND"


@pytest.mark.asyncio
async def test_tenant_override_unknown_provider_422_and_non_admin_403(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    tenant_id = await _seed_tenant(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        bad = await client.put(
            f"/v1/platform/credentials/tenants/{tenant_id}/providers/not-a-provider",
            json={"secret_ref": "kms://x", "enabled": True},
            headers=_headers(admin),
        )
        assert bad.status_code == 422
        assert bad.json()["detail"]["code"] == "UNKNOWN_PROVIDER"

        forbidden = await client.get(
            f"/v1/platform/credentials/tenants/{tenant_id}", headers=_headers(uuid4())
        )
        assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_tenant_override_takes_effect_in_service_view(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    """End-to-end within the app: PUT override → invalidate → the app's
    PlatformSecretsService tenant-effective view reflects it immediately.
    (The resolver hop on top of this view is covered by the overlay unit
    tests; ``credentials_resolver`` itself is lifespan-only state.)"""
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    tenant_id = await _seed_tenant(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        await client.put(
            "/v1/platform/credentials/providers/anthropic",
            json={"secret_ref": "kms://platform/anthropic", "enabled": True},
            headers=_headers(admin),
        )
        await client.put(
            f"/v1/platform/credentials/tenants/{tenant_id}/providers/anthropic",
            json={"secret_ref": "kms://tenant/anthropic", "enabled": True},
            headers=_headers(admin),
        )
        service = app.state.platform_secrets_service  # type: ignore[attr-defined]
        view = await service.effective_provider_credentials_for(tenant_id)
        assert view.get("anthropic") == "kms://tenant/anthropic"

        # Another (existing) tenant still sees the platform ref.
        other = await _seed_tenant(app)
        view_other = await service.effective_provider_credentials_for(other)
        assert view_other.get("anthropic") == "kms://platform/anthropic"


# ---------------------------------------------------------------------------
# Stream Y-MK — per-provider multi-key API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multikey_upsert_and_catalog_lists_keys(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    # default key (priority 100) + a higher-priority sibling.
    await client.put(
        "/v1/platform/credentials/providers/deepseek",
        json={"secret_ref": "kms://deepseek/default", "enabled": True},
        headers=_headers(admin),
    )
    put = await client.put(
        "/v1/platform/credentials/providers/deepseek/keys/acct-b",
        json={"secret_ref": "kms://deepseek/b", "enabled": True, "priority": 10},
        headers=_headers(admin),
    )
    assert put.status_code == 200, put.text
    assert put.json()["data"]["key_id"] == "acct-b"
    assert put.json()["data"]["priority"] == 10

    got = await client.get("/v1/platform/credentials", headers=_headers(admin))
    row = next(p for p in got.json()["data"]["providers"] if p["provider"] == "deepseek")
    keys = {k["key_id"]: k for k in row["keys"]}
    assert set(keys) == {"default", "acct-b"}
    assert keys["acct-b"]["priority"] == 10
    # Keys are priority-sorted; best (lowest) first.
    assert row["keys"][0]["key_id"] == "acct-b"


@pytest.mark.asyncio
async def test_multikey_delete_sibling_keeps_others(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    await client.put(
        "/v1/platform/credentials/providers/deepseek",
        json={"secret_ref": "kms://deepseek/default", "enabled": True},
        headers=_headers(admin),
    )
    await client.put(
        "/v1/platform/credentials/providers/deepseek/keys/acct-b",
        json={"secret_ref": "kms://deepseek/b", "enabled": True, "priority": 10},
        headers=_headers(admin),
    )
    delete = await client.delete(
        "/v1/platform/credentials/providers/deepseek/keys/acct-b",
        headers=_headers(admin),
    )
    assert delete.status_code == 204, delete.text

    got = await client.get("/v1/platform/credentials", headers=_headers(admin))
    row = next(p for p in got.json()["data"]["providers"] if p["provider"] == "deepseek")
    assert [k["key_id"] for k in row["keys"]] == ["default"]


@pytest.mark.asyncio
async def test_multikey_delete_missing_key_404(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    resp = await client.delete(
        "/v1/platform/credentials/providers/deepseek/keys/nope",
        headers=_headers(admin),
    )
    assert resp.status_code == 404


# ─── canonical secret name helper + delete-side secret cleanup ────────────
#
# Deletion interface hygiene PR2 (Task 6): the 5 delete endpoints below now
# best-effort purge the platform-managed secret behind a catalog row, but
# must never touch an operator-supplied external secret:// / kms:// ref.


def test_canonical_secret_name_matches_legacy_literals() -> None:
    """Byte-identical regression against the pre-helper inline literals."""
    tenant_id = uuid4()
    assert _canonical_secret_name(provider="anthropic") == "expert-work/platform/llm/anthropic"
    assert (
        _canonical_secret_name(provider="anthropic", key_id="default")
        == "expert-work/platform/llm/anthropic"
    )
    assert (
        _canonical_secret_name(provider="anthropic", key_id="acct-b")
        == "expert-work/platform/llm/anthropic/acct-b"
    )
    assert _canonical_secret_name(tool="web_search") == "expert-work/platform/tool/web_search"
    assert (
        _canonical_secret_name(tenant_id=tenant_id, provider="anthropic")
        == f"expert-work/platform/tenant/{tenant_id}/llm/anthropic"
    )
    assert (
        _canonical_secret_name(tenant_id=tenant_id, tool="web_search")
        == f"expert-work/platform/tenant/{tenant_id}/tool/web_search"
    )


def _audit_details(app: object, *, action: str, resource_id: str) -> dict[str, object]:
    store = app.state.audit_logger._store  # type: ignore[attr-defined]
    entries = [
        e for e in store._rows.values() if e.action.value == action and e.resource_id == resource_id
    ]
    assert entries, f"expected an audit row for {action} / {resource_id}"
    return entries[-1].details


@pytest.mark.asyncio
async def test_delete_provider_paste_mode_purges_managed_secret(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    app = client._transport.app  # type: ignore[attr-defined]
    put = await client.put(
        "/v1/platform/credentials/providers/openai",
        json={"value": "sk-openai-REAL", "enabled": True},
        headers=_headers(admin),
    )
    assert put.status_code == 200, put.text
    assert await app.state.secret_store.get("expert-work/platform/llm/openai") == "sk-openai-REAL"

    resp = await client.delete("/v1/platform/credentials/providers/openai", headers=_headers(admin))
    assert resp.status_code == 204

    with pytest.raises(SecretNotFoundError):
        await app.state.secret_store.get("expert-work/platform/llm/openai")
    details = _audit_details(
        app, action="platform_credential:provider_delete", resource_id="openai"
    )
    assert details["secret_deleted"] is True


@pytest.mark.asyncio
async def test_delete_provider_ref_mode_leaves_external_secret(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    app = client._transport.app  # type: ignore[attr-defined]
    await app.state.secret_store.put("external/openai-sentinel", "sentinel-value")
    put = await client.put(
        "/v1/platform/credentials/providers/openai",
        json={"secret_ref": "secret://external/openai-sentinel", "enabled": True},
        headers=_headers(admin),
    )
    assert put.status_code == 200, put.text

    resp = await client.delete("/v1/platform/credentials/providers/openai", headers=_headers(admin))
    assert resp.status_code == 204

    assert await app.state.secret_store.get("external/openai-sentinel") == "sentinel-value"
    details = _audit_details(
        app, action="platform_credential:provider_delete", resource_id="openai"
    )
    assert details["secret_deleted"] is False


@pytest.mark.asyncio
async def test_delete_provider_key_paste_mode_purges_managed_secret(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    app = client._transport.app  # type: ignore[attr-defined]
    await client.put(
        "/v1/platform/credentials/providers/deepseek",
        json={"secret_ref": "kms://deepseek/default", "enabled": True},
        headers=_headers(admin),
    )
    put = await client.put(
        "/v1/platform/credentials/providers/deepseek/keys/acct-b",
        json={"value": "sk-deepseek-b", "enabled": True, "priority": 10},
        headers=_headers(admin),
    )
    assert put.status_code == 200, put.text
    assert (
        await app.state.secret_store.get("expert-work/platform/llm/deepseek/acct-b")
        == "sk-deepseek-b"
    )

    resp = await client.delete(
        "/v1/platform/credentials/providers/deepseek/keys/acct-b", headers=_headers(admin)
    )
    assert resp.status_code == 204

    with pytest.raises(SecretNotFoundError):
        await app.state.secret_store.get("expert-work/platform/llm/deepseek/acct-b")
    details = _audit_details(
        app, action="platform_credential:provider_delete", resource_id="deepseek#acct-b"
    )
    assert details["secret_deleted"] is True


@pytest.mark.asyncio
async def test_delete_provider_key_ref_mode_leaves_external_secret(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    app = client._transport.app  # type: ignore[attr-defined]
    await client.put(
        "/v1/platform/credentials/providers/deepseek",
        json={"secret_ref": "kms://deepseek/default", "enabled": True},
        headers=_headers(admin),
    )
    await app.state.secret_store.put("external/deepseek-b-sentinel", "sentinel-value")
    put = await client.put(
        "/v1/platform/credentials/providers/deepseek/keys/acct-b",
        json={
            "secret_ref": "secret://external/deepseek-b-sentinel",
            "enabled": True,
            "priority": 10,
        },
        headers=_headers(admin),
    )
    assert put.status_code == 200, put.text

    resp = await client.delete(
        "/v1/platform/credentials/providers/deepseek/keys/acct-b", headers=_headers(admin)
    )
    assert resp.status_code == 204

    assert await app.state.secret_store.get("external/deepseek-b-sentinel") == "sentinel-value"
    details = _audit_details(
        app, action="platform_credential:provider_delete", resource_id="deepseek#acct-b"
    )
    assert details["secret_deleted"] is False


@pytest.mark.asyncio
async def test_delete_tool_paste_mode_purges_managed_secret(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    app = client._transport.app  # type: ignore[attr-defined]
    put = await client.put(
        "/v1/platform/credentials/tools/web_search",
        json={"value": "tavily-REAL-KEY", "enabled": True},
        headers=_headers(admin),
    )
    assert put.status_code == 200, put.text
    assert (
        await app.state.secret_store.get("expert-work/platform/tool/web_search")
        == "tavily-REAL-KEY"
    )

    resp = await client.delete("/v1/platform/credentials/tools/web_search", headers=_headers(admin))
    assert resp.status_code == 204

    with pytest.raises(SecretNotFoundError):
        await app.state.secret_store.get("expert-work/platform/tool/web_search")
    details = _audit_details(
        app, action="platform_credential:tool_delete", resource_id="web_search"
    )
    assert details["secret_deleted"] is True


@pytest.mark.asyncio
async def test_delete_tool_ref_mode_leaves_external_secret(
    admin_client: tuple[AsyncClient, UUID],
) -> None:
    client, admin = admin_client
    app = client._transport.app  # type: ignore[attr-defined]
    await app.state.secret_store.put("external/tavily-sentinel", "sentinel-value")
    put = await client.put(
        "/v1/platform/credentials/tools/web_search",
        json={"secret_ref": "secret://external/tavily-sentinel", "enabled": True},
        headers=_headers(admin),
    )
    assert put.status_code == 200, put.text

    resp = await client.delete("/v1/platform/credentials/tools/web_search", headers=_headers(admin))
    assert resp.status_code == 204

    assert await app.state.secret_store.get("external/tavily-sentinel") == "sentinel-value"
    details = _audit_details(
        app, action="platform_credential:tool_delete", resource_id="web_search"
    )
    assert details["secret_deleted"] is False


@pytest.mark.asyncio
async def test_delete_tenant_provider_paste_mode_purges_managed_secret(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    tenant_id = await _seed_tenant(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        put = await client.put(
            f"/v1/platform/credentials/tenants/{tenant_id}/providers/anthropic",
            json={"value": "sk-ant-TENANT-KEY", "enabled": True},
            headers=_headers(admin),
        )
        assert put.status_code == 200, put.text
        name = f"expert-work/platform/tenant/{tenant_id}/llm/anthropic"
        assert await app.state.secret_store.get(name) == "sk-ant-TENANT-KEY"

        resp = await client.delete(
            f"/v1/platform/credentials/tenants/{tenant_id}/providers/anthropic",
            headers=_headers(admin),
        )
        assert resp.status_code == 204

        with pytest.raises(SecretNotFoundError):
            await app.state.secret_store.get(name)
        details = _audit_details(
            app, action="platform_credential:tenant_provider_delete", resource_id="anthropic"
        )
        assert details["secret_deleted"] is True
        assert details["tenant_id"] == str(tenant_id)


@pytest.mark.asyncio
async def test_delete_tenant_provider_ref_mode_leaves_external_secret(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    tenant_id = await _seed_tenant(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        await app.state.secret_store.put("external/tenant-anthropic-sentinel", "sentinel-value")
        put = await client.put(
            f"/v1/platform/credentials/tenants/{tenant_id}/providers/anthropic",
            json={"secret_ref": "secret://external/tenant-anthropic-sentinel", "enabled": True},
            headers=_headers(admin),
        )
        assert put.status_code == 200, put.text

        resp = await client.delete(
            f"/v1/platform/credentials/tenants/{tenant_id}/providers/anthropic",
            headers=_headers(admin),
        )
        assert resp.status_code == 204

        assert (
            await app.state.secret_store.get("external/tenant-anthropic-sentinel")
            == "sentinel-value"
        )
        details = _audit_details(
            app, action="platform_credential:tenant_provider_delete", resource_id="anthropic"
        )
        assert details["secret_deleted"] is False


@pytest.mark.asyncio
async def test_delete_tenant_tool_paste_mode_purges_managed_secret(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    tenant_id = await _seed_tenant(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        put = await client.put(
            f"/v1/platform/credentials/tenants/{tenant_id}/tools/web_search",
            json={"value": "tavily-TENANT-KEY", "enabled": True},
            headers=_headers(admin),
        )
        assert put.status_code == 200, put.text
        name = f"expert-work/platform/tenant/{tenant_id}/tool/web_search"
        assert await app.state.secret_store.get(name) == "tavily-TENANT-KEY"

        resp = await client.delete(
            f"/v1/platform/credentials/tenants/{tenant_id}/tools/web_search",
            headers=_headers(admin),
        )
        assert resp.status_code == 204

        with pytest.raises(SecretNotFoundError):
            await app.state.secret_store.get(name)
        details = _audit_details(
            app, action="platform_credential:tenant_tool_delete", resource_id="web_search"
        )
        assert details["secret_deleted"] is True
        assert details["tenant_id"] == str(tenant_id)


@pytest.mark.asyncio
async def test_delete_tenant_tool_ref_mode_leaves_external_secret(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    tenant_id = await _seed_tenant(app)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        await app.state.secret_store.put("external/tenant-tavily-sentinel", "sentinel-value")
        put = await client.put(
            f"/v1/platform/credentials/tenants/{tenant_id}/tools/web_search",
            json={"secret_ref": "secret://external/tenant-tavily-sentinel", "enabled": True},
            headers=_headers(admin),
        )
        assert put.status_code == 200, put.text

        resp = await client.delete(
            f"/v1/platform/credentials/tenants/{tenant_id}/tools/web_search",
            headers=_headers(admin),
        )
        assert resp.status_code == 204

        assert (
            await app.state.secret_store.get("external/tenant-tavily-sentinel") == "sentinel-value"
        )
        details = _audit_details(
            app, action="platform_credential:tenant_tool_delete", resource_id="web_search"
        )
        assert details["secret_deleted"] is False


# ─── PR-E3b — invalidation-bus broadcast on credential writes ──────────────


class _SpyRuntime:
    def __init__(self) -> None:
        self.tenant_calls: list[UUID] = []
        self.all_calls = 0

    def invalidate_all(self) -> None:
        self.all_calls += 1

    def invalidate_tenant(self, tenant_id: UUID) -> None:
        self.tenant_calls.append(tenant_id)


class _SpyBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)

    def publish_soon(self, event: object) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_platform_put_evicts_locally_and_broadcasts_platform_secrets(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    """A platform-level credential write must drop THIS pod's builds and
    broadcast a tenant-less ``platform_secrets`` event for peer replicas."""
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    spy_runtime = _SpyRuntime()
    spy_bus = _SpyBus()
    app.state.agent_runtime = spy_runtime
    app.state.invalidation_bus = spy_bus
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        put = await client.put(
            "/v1/platform/credentials/providers/anthropic",
            json={"secret_ref": "kms://platform/anthropic", "enabled": True},
            headers=_headers(admin),
        )
    assert put.status_code == 200, put.text
    assert spy_runtime.all_calls == 1
    assert spy_runtime.tenant_calls == []
    assert len(spy_bus.events) == 1
    event = spy_bus.events[0]
    assert event.kind == "platform_secrets"  # type: ignore[attr-defined]
    assert event.tenant_id is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_tenant_override_put_broadcasts_platform_secrets_with_tenant(
    settings: Settings,
    lifecycle: Lifecycle,
    jwt_verifier: JWTVerifier,
) -> None:
    app = create_app(settings=settings, lifecycle=lifecycle, jwt_verifier=jwt_verifier)
    admin = await _seed_admin(app)
    tenant_id = await _seed_tenant(app)
    spy_runtime = _SpyRuntime()
    spy_bus = _SpyBus()
    app.state.agent_runtime = spy_runtime
    app.state.invalidation_bus = spy_bus
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://control-plane.test") as client:
        put = await client.put(
            f"/v1/platform/credentials/tenants/{tenant_id}/providers/anthropic",
            json={"value": "sk-ant-TENANT-KEY", "enabled": True},
            headers=_headers(admin),
        )
    assert put.status_code == 200, put.text
    # Tenant-scoped: only that tenant's builds drop locally …
    assert spy_runtime.tenant_calls == [tenant_id]
    assert spy_runtime.all_calls == 0
    # … and the broadcast carries the tenant so peers do the same.
    assert len(spy_bus.events) == 1
    event = spy_bus.events[0]
    assert event.kind == "platform_secrets"  # type: ignore[attr-defined]
    assert event.tenant_id == str(tenant_id)  # type: ignore[attr-defined]
