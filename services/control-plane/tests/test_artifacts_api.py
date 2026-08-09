"""Tests for ``GET /v1/artifacts`` — Stream J.9 artifact list + download."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import InMemoryArtifactStore, InMemoryTenantUserStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from orchestrator.tools import (
    RecordingWorkspaceStore,
    SandboxSupervisorError,
    WorkspacePermissionError,
)
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    grant_system_admin,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
_SUBJECT = "user-a"
_CONTENT = b"report body"


def _settings() -> Settings:
    return Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


def _headers(subject: str = _SUBJECT) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT, subject=subject)}"}


async def _seed() -> tuple[InMemoryTenantUserStore, InMemoryArtifactStore, UUID]:
    """A user store + artifact store with one artifact owned by ``_SUBJECT``."""
    users = InMemoryTenantUserStore()
    artifacts = InMemoryArtifactStore()
    user = await users.resolve(tenant_id=_TENANT, subject_type="user", subject_id=_SUBJECT)
    await artifacts.save_version(
        tenant_id=_TENANT,
        user_id=user.id,
        name="report.md",
        kind="document",
        path_in_workspace="report.md",
        created_in_thread="t-1",
    )
    return users, artifacts, user.id


@pytest.fixture
async def setup() -> AsyncIterator[tuple[AsyncClient, InMemoryArtifactStore, UUID]]:
    users, artifacts, user_id = await _seed()
    app = create_app(
        settings=_settings(),
        tenant_user_repo=users,
        artifact_repo=artifacts,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    app.state.workspace_store = RecordingWorkspaceStore(workspace_file=_CONTENT)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers()
    ) as client:
        yield client, artifacts, user_id


@pytest.mark.asyncio
async def test_list_artifacts_returns_user_artifacts(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, _, _ = setup
    resp = await client.get("/v1/artifacts")
    assert resp.status_code == 200
    artifacts = resp.json()["artifacts"]
    assert artifacts == [{"name": "report.md", "kind": "document", "latest_version": 1}]


@pytest.mark.asyncio
async def test_list_artifacts_isolated_per_user(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, _, _ = setup
    # A different user sees none of user-a's artifacts.
    resp = await client.get("/v1/artifacts", headers=_headers("user-b"))
    assert resp.status_code == 200
    assert resp.json()["artifacts"] == []


@pytest.mark.asyncio
async def test_admin_lists_another_users_artifacts_via_user_id(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    """Conversation-centric IA M2 — ``?user_id=`` is the tenant admin's
    governance view (the user-detail Artifacts tab). The default JWT
    carries the admin role, so user-b can read user-a's list."""
    client, _, user_id = setup
    resp = await client.get(f"/v1/artifacts?user_id={user_id}", headers=_headers("user-b"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["artifacts"] == [
        {"name": "report.md", "kind": "document", "latest_version": 1}
    ]


@pytest.mark.asyncio
async def test_non_admin_user_id_for_someone_else_is_403(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, _, user_id = setup
    viewer_jwt = make_test_jwt(tenant_id=_TENANT, subject="user-b", roles=("viewer",))
    resp = await client.get(
        f"/v1/artifacts?user_id={user_id}",
        headers={"Authorization": f"Bearer {viewer_jwt}"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "USER_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_admin_downloads_another_users_artifact_via_user_id(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    """H.8-F1 — the governance actions accept a ``user_id`` target: an
    admin (user-b) downloads user-a's artifact; the supervisor read is
    keyed to the target's workspace."""
    client, _, user_id = setup
    resp = await client.get(
        "/v1/artifacts/download",
        params={"name": "report.md", "user_id": str(user_id)},
        headers=_headers("user-b"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == _CONTENT


@pytest.mark.asyncio
async def test_admin_deletes_another_users_artifact_via_user_id(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, artifacts, user_id = setup
    resp = await client.delete(
        f"/v1/artifacts/report.md?user_id={user_id}", headers=_headers("user-b")
    )
    assert resp.status_code == 200, resp.text
    remaining = await artifacts.list_for_user(tenant_id=_TENANT, user_id=user_id)
    assert all(a.name != "report.md" for a in remaining)


@pytest.mark.asyncio
async def test_non_admin_action_on_someone_else_is_403(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, _, user_id = setup
    viewer_jwt = make_test_jwt(tenant_id=_TENANT, subject="user-b", roles=("viewer",))
    resp = await client.delete(
        f"/v1/artifacts/report.md?user_id={user_id}",
        headers={"Authorization": f"Bearer {viewer_jwt}"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "USER_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_download_artifact_returns_content(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, _, _ = setup
    resp = await client.get("/v1/artifacts/download", params={"name": "report.md"})
    assert resp.status_code == 200
    assert resp.content == _CONTENT
    # MIME-aware (Mini-ADR J-25 § 10.5) — ``.md`` is text-like, inline.
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert "inline" in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_download_artifact_backfills_digest(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, artifacts, user_id = setup
    await client.get("/v1/artifacts/download", params={"name": "report.md"})

    latest = await artifacts.get_latest_version(
        tenant_id=_TENANT, user_id=user_id, name="report.md"
    )
    assert latest is not None
    assert latest.size_bytes == len(_CONTENT)
    assert latest.sha256 == hashlib.sha256(_CONTENT).hexdigest()


@pytest.mark.asyncio
async def test_download_unknown_artifact_returns_404(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, _, _ = setup
    resp = await client.get("/v1/artifacts/download", params={"name": "missing.md"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_reports_permission_denied_as_server_error(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    """store 抛 WorkspacePermissionError → 500,不是 404。

    元数据行(artifact 版本)存在,内容读不动是权限问题(服务端配置),不是
    "这个 artifact 不存在"——这里原来的注释自己就写着"the file is gone /
    unreadable"两种情况合并成一个 404,正是这一整个 program 要拆开的那类混淆
    (复审第二轮指出的第四/五处站点之一)。响应体不含路径 / uid / mode。
    """
    client, _, _ = setup
    store = client._transport.app.state.workspace_store  # type: ignore[attr-defined,union-attr]
    store.workspace_file_error = WorkspacePermissionError(
        "PermissionError(13, 'Permission denied'): "
        "'/mnt/workspaces/t-1/u-1/report.md' uid=10002 mode=0o600"
    )
    resp = await client.get("/v1/artifacts/download", params={"name": "report.md"})
    assert resp.status_code == 500, resp.text
    assert "/mnt/workspaces" not in resp.text
    assert "10002" not in resp.text
    assert "0o600" not in resp.text


@pytest.mark.asyncio
async def test_download_still_404s_on_a_generic_supervisor_error(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    """对照组:普通 SandboxSupervisorError(真的读不到内容)仍是 404,既有姿态不变。"""
    client, _, _ = setup
    store = client._transport.app.state.workspace_store  # type: ignore[attr-defined,union-attr]
    store.workspace_file_error = SandboxSupervisorError("content gone")
    resp = await client.get("/v1/artifacts/download", params={"name": "report.md"})
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_download_without_supervisor_returns_503() -> None:
    users, artifacts, _ = await _seed()
    app = create_app(
        settings=_settings(),
        tenant_user_repo=users,
        artifact_repo=artifacts,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    # No sandbox_supervisor_url → app.state.sandbox_runtime is None.
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers()
    ) as client:
        resp = await client.get("/v1/artifacts/download", params={"name": "report.md"})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_download_429_when_download_count_quota_exhausted(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    """After ``ARTIFACT_DOWNLOAD_COUNT_30D`` is consumed the next
    download returns 429 with the dimension surfaced — Mini-ADR J-25."""
    from expert_work.protocol import QuotaDimension, TenantQuotaPatch

    client, _, _ = setup
    # Seed a 2-burst capacity bucket.
    app = client._transport.app  # type: ignore[attr-defined,union-attr]
    patch = TenantQuotaPatch(
        dimension=QuotaDimension.ARTIFACT_DOWNLOAD_COUNT_30D,
        scope={},
        limit_value=2,
        burst=2,
    )
    await app.state.tenant_quota_repo.upsert(tenant_id=_TENANT, patch=patch, updated_by="test")

    # 2 downloads within capacity (200 + 200).
    for _ in range(2):
        ok = await client.get("/v1/artifacts/download", params={"name": "report.md"})
        assert ok.status_code == 200
    # 3rd exceeds capacity; slow drip cannot refill in-time.
    denied = await client.get("/v1/artifacts/download", params={"name": "report.md"})
    assert denied.status_code == 429
    body = denied.json()
    assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    assert body["error"]["dimension"] == QuotaDimension.ARTIFACT_DOWNLOAD_COUNT_30D.value
    assert denied.headers["Retry-After"]


# ---------------------------------------------------------------------------
# J.9-step3 — MIME-aware download + XSS defence (STREAM-J-DESIGN § 10.5)
# ---------------------------------------------------------------------------


async def _seed_artifact_with_path(
    artifacts: InMemoryArtifactStore,
    user_id: UUID,
    *,
    name: str,
    kind: str,
    path: str,
) -> None:
    """Add one extra artifact alongside the ``setup`` fixture's ``report.md``."""
    await artifacts.save_version(
        tenant_id=_TENANT,
        user_id=user_id,
        name=name,
        kind=kind,  # type: ignore[arg-type]
        path_in_workspace=path,
        created_in_thread="t",
    )


@pytest.mark.asyncio
async def test_download_html_artifact_is_forced_attachment(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    """HTML artifacts must never inline-render — stored-XSS (c) red line."""
    client, artifacts, user_id = setup
    await _seed_artifact_with_path(
        artifacts, user_id, name="report.html", kind="document", path="report.html"
    )
    resp = await client.get("/v1/artifacts/download", params={"name": "report.html"})
    assert resp.status_code == 200
    # Real MIME surfaces for logging, but disposition stops the browser.
    assert "text/html" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_download_svg_artifact_is_forced_attachment(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    """SVG is active content (can embed ``<script>``) — same XSS rule."""
    client, artifacts, user_id = setup
    await _seed_artifact_with_path(
        artifacts, user_id, name="logo.svg", kind="data", path="logo.svg"
    )
    resp = await client.get("/v1/artifacts/download", params={"name": "logo.svg"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert "attachment" in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_download_image_artifact_is_inline_with_image_mime(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, artifacts, user_id = setup
    await _seed_artifact_with_path(
        artifacts, user_id, name="photo.png", kind="data", path="photo.png"
    )
    resp = await client.get("/v1/artifacts/download", params={"name": "photo.png"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert "inline" in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_download_unknown_extension_is_octet_stream(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    """Anything not on the whitelist falls through to attachment+octet-stream."""
    client, artifacts, user_id = setup
    await _seed_artifact_with_path(
        artifacts, user_id, name="dump.bin", kind="data", path="dump.bin"
    )
    resp = await client.get("/v1/artifacts/download", params={"name": "dump.bin"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert "attachment" in resp.headers["content-disposition"]


# ---------------------------------------------------------------------------
# J.9-step3 — DELETE / PATCH / versions endpoints + audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_artifact_soft_deletes_and_audits(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, artifacts, user_id = setup
    resp = await client.delete("/v1/artifacts/report.md")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "report.md"}
    # Default list hides; include_deleted=True reveals the soft-deleted row.
    assert await artifacts.list_for_user(tenant_id=_TENANT, user_id=user_id) == []
    deleted = await artifacts.list_for_user(
        tenant_id=_TENANT, user_id=user_id, include_deleted=True
    )
    assert len(deleted) == 1
    # Subsequent download returns 404 (same hiding rule).
    redownload = await client.get("/v1/artifacts/download", params={"name": "report.md"})
    assert redownload.status_code == 404


@pytest.mark.asyncio
async def test_delete_unknown_returns_404(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, _, _ = setup
    resp = await client.delete("/v1/artifacts/missing.md")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_cross_user_returns_404(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    """Cross-user delete returns 404 (hides existence) — never 403."""
    client, _, _ = setup
    resp = await client.delete("/v1/artifacts/report.md", headers=_headers("user-b"))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_artifact_updates_kind_and_audits(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, _, _ = setup
    resp = await client.patch("/v1/artifacts/report.md", json={"kind": "code"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "report.md"
    assert body["kind"] == "code"
    # List reflects the update.
    listing = await client.get("/v1/artifacts")
    assert listing.json()["artifacts"][0]["kind"] == "code"


@pytest.mark.asyncio
async def test_patch_unknown_returns_404(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, _, _ = setup
    resp = await client.patch("/v1/artifacts/missing.md", json={"kind": "code"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_invalid_kind_returns_422(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, _, _ = setup
    resp = await client.patch("/v1/artifacts/report.md", json={"kind": "nonsense"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_extra_fields_rejected(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    """``extra='forbid'`` keeps the schema narrow."""
    client, _, _ = setup
    resp = await client.patch("/v1/artifacts/report.md", json={"kind": "code", "rogue": "x"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_versions_returns_versions_desc(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, artifacts, user_id = setup
    # Add a v2 to the seeded ``report.md``.
    await artifacts.save_version(
        tenant_id=_TENANT,
        user_id=user_id,
        name="report.md",
        kind="document",
        path_in_workspace="report.md",
        created_in_thread="t-2",
    )
    resp = await client.get("/v1/artifacts/report.md/versions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "report.md"
    versions = body["versions"]
    assert len(versions) == 2
    assert [v["version"] for v in versions] == [2, 1]
    # First version's digest is still NULL — backfilled only on download.
    assert versions[0]["size_bytes"] is None


@pytest.mark.asyncio
async def test_list_versions_unknown_returns_404(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, _, _ = setup
    resp = await client.get("/v1/artifacts/missing.md/versions")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# W2 — artifact 详情读端点接跨租户 scope(系统管理员租户切换器)
#
# 三件套 per endpoint:system_admin 带目标租户 tenant_id → 200;普通租户
# 用户带他租户 tenant_id → 403 TENANT_NOT_ALLOWED;tenant_id=* → 400
# SCOPE_ALL_NOT_SUPPORTED。
# ---------------------------------------------------------------------------


#: (name, path, extra query params) — the two artifact detail read endpoints.
_ARTIFACT_SCOPE_ENDPOINTS: list[tuple[str, str, dict[str, str]]] = [
    ("download", "/v1/artifacts/download", {"name": "report.md"}),
    ("versions", "/v1/artifacts/report.md/versions", {}),
]


@pytest.mark.asyncio
async def test_download_system_admin_target_tenant_200(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, _, user_id = setup
    headers = await grant_system_admin(client)
    resp = await client.get(
        "/v1/artifacts/download",
        params={"name": "report.md", "tenant_id": str(_TENANT), "user_id": str(user_id)},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == _CONTENT


@pytest.mark.asyncio
async def test_versions_system_admin_target_tenant_200(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
) -> None:
    client, _, user_id = setup
    headers = await grant_system_admin(client)
    resp = await client.get(
        "/v1/artifacts/report.md/versions",
        params={"tenant_id": str(_TENANT), "user_id": str(user_id)},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert [v["version"] for v in resp.json()["versions"]] == [1]


@pytest.mark.parametrize("name,path,extra", _ARTIFACT_SCOPE_ENDPOINTS)
@pytest.mark.asyncio
async def test_artifact_detail_foreign_tenant_user_403(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
    name: str,
    path: str,
    extra: dict[str, str],
) -> None:
    client, _, _ = setup
    foreign = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}
    resp = await client.get(path, params={"tenant_id": str(_TENANT), **extra}, headers=foreign)
    assert resp.status_code == 403, f"{name}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"]["code"] == "TENANT_NOT_ALLOWED", name


@pytest.mark.parametrize("name,path,extra", _ARTIFACT_SCOPE_ENDPOINTS)
@pytest.mark.asyncio
async def test_artifact_detail_tenant_id_star_400(
    setup: tuple[AsyncClient, InMemoryArtifactStore, UUID],
    name: str,
    path: str,
    extra: dict[str, str],
) -> None:
    client, _, _ = setup
    resp = await client.get(path, params={"tenant_id": "*", **extra})
    assert resp.status_code == 400, f"{name}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"]["code"] == "SCOPE_ALL_NOT_SUPPORTED", name
