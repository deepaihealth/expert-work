"""B-326 —— ``GET /v1/public/files/{id}``:不登录下载分享链接指向的那一版产物。

* 有效链接 → 字节 + 与对外下载同一套头(MIME / Content-Disposition / nosniff / 版本号),
  外加 ``no-store``;记一条 ``artifact:share_download`` 审计;
* 查不到 / 过期 / 产物已删 / 租户停用 → 同一个不透明 404;过期的行顺手删掉;
* 链接只在它自己的租户 + 用户里解析:行上写的租户 / 用户和产物对不上就是 404;
* 鉴权豁免只给 ``/v1/public/…``:别的路径没凭据照旧 401。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.runtime import build_tool_env
from control_plane.settings import Settings
from expert_work.persistence import ArtifactStore, InMemoryArtifactStore
from expert_work.persistence.artifact_share import (
    ArtifactShareLink,
    InMemoryArtifactShareLinkStore,
)
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var, current_user_id_var
from expert_work.protocol import ArtifactVersion, AuditAction, CheckResult, QuotaDimension
from orchestrator.tools import RecordingWorkspaceStore
from orchestrator.tools.artifact_snapshots import InMemoryArtifactSnapshotStore
from orchestrator.tools.share_file import ShareLinkService
from tests.auth_fixtures import TEST_AUDIENCE, TEST_ISSUER, build_test_jwt_verifier

_AGENT = "report-agent"
_PDF = b"%PDF-1.7 fake"


def _settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "service_name": "control_plane_test",
        "env": "dev",
        "auth_mode": "dev",
        "db_dsn": "postgresql+asyncpg://test@localhost/test",
        "rate_limit_burst": 10_000,
        "rate_limit_per_second": 10_000.0,
        "oidc_issuer": TEST_ISSUER,
        "oidc_audience": [TEST_AUDIENCE],
    }
    fields.update(overrides)
    return Settings(**fields)


@dataclass
class _Env:
    client: AsyncClient
    app: Any
    artifacts: ArtifactStore
    links: InMemoryArtifactShareLinkStore
    snapshots: InMemoryArtifactSnapshotStore
    workspace: RecordingWorkspaceStore
    audit: InMemoryAuditLogStore

    async def save(
        self, tenant: UUID, user: UUID, data: bytes, name: str = "report.pdf"
    ) -> ArtifactVersion:
        path = f"agents/{_AGENT}/{name}"
        self.workspace.workspace_file_contents[path] = data
        version_id = uuid4()
        digest = await self.snapshots.capture(
            tenant_id=tenant, user_id=user, path=path, snapshot_key=str(version_id)
        )
        return await self.artifacts.save_version(
            tenant_id=tenant,
            user_id=user,
            agent_key=_AGENT,
            name=name,
            kind="document",
            path_in_workspace=path,
            created_in_thread="t",
            version_id=version_id,
            snapshot_key=str(version_id),
            size_bytes=digest.size_bytes,
            sha256=digest.sha256,
        )

    def link(self, version: ArtifactVersion, **overrides: Any) -> str:
        fields: dict[str, Any] = {
            "id": uuid4().hex[:16],
            "tenant_id": version.tenant_id,
            "user_id": version.user_id,
            "agent_key": _AGENT,
            "artifact_id": version.artifact_id,
            "artifact_version_id": version.id,
            "version": version.version,
            "run_id": uuid4(),
            "thread_id": uuid4(),
            "created_at": datetime.now(UTC),
            "expires_at": datetime.now(UTC) + timedelta(hours=2),
        }
        fields.update(overrides)
        self.links.put(ArtifactShareLink(**fields))
        return str(fields["id"])


@pytest.fixture
async def env() -> Any:
    audit = InMemoryAuditLogStore()
    app = create_app(
        settings=_settings(),
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(audit),
    )
    workspace = RecordingWorkspaceStore()
    snapshots = InMemoryArtifactSnapshotStore(workspace=workspace)
    app.state.workspace_store = workspace
    app.state.artifact_snapshots = snapshots
    links = app.state.artifact_share_link_store
    assert isinstance(links, InMemoryArtifactShareLinkStore)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://cp.test") as client:
        yield _Env(
            client=client,
            app=app,
            artifacts=app.state.artifact_store,
            links=links,
            snapshots=snapshots,
            workspace=workspace,
            audit=audit,
        )


def _assert_opaque_404(response: Any) -> None:
    assert response.status_code == 404
    assert response.json() == {
        "success": False,
        "data": None,
        "error": {"code": "FILE_NOT_FOUND", "message": "file not found or link expired"},
    }


@pytest.mark.asyncio
async def test_valid_link_serves_bytes_without_any_credentials(env: _Env) -> None:
    tenant, user = uuid4(), uuid4()
    version = await env.save(tenant, user, _PDF)
    link_id = env.link(version)
    response = await env.client.get(f"/v1/public/files/{link_id}")
    assert response.status_code == 200
    assert response.content == _PDF
    # Same whitelist as the external download: .pdf is not on it → opaque bytes, attachment.
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment")
    assert "report.pdf" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-artifact-version"] == "1"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    rows = [e for e in env.audit._rows.values() if e.action == AuditAction.ARTIFACT_SHARE_DOWNLOAD]
    assert len(rows) == 1
    assert rows[0].tenant_id == tenant
    assert rows[0].resource_id == "report.pdf"
    assert link_id not in str(rows[0].details)  # only a prefix is recorded


@pytest.mark.asyncio
async def test_a_link_stops_after_its_download_cap_without_charging_quota(env: _Env) -> None:
    """M-e —— the cap is per link; past it the same opaque 404, and the tenant's
    download quota is no longer charged."""
    tenant, user = uuid4(), uuid4()
    version = await env.save(tenant, user, _PDF)
    link_id = env.link(version)
    env.app.state.share_link_max_downloads = 2
    charged: list[Any] = []

    class _CountingQuota:
        async def check(self, req: Any) -> CheckResult:
            charged.append(req)
            return CheckResult(allowed=True)

    env.app.state.quota_service = _CountingQuota()
    statuses = [(await env.client.get(f"/v1/public/files/{link_id}")).status_code for _ in range(3)]
    assert statuses == [200, 200, 404]
    _assert_opaque_404(await env.client.get(f"/v1/public/files/{link_id}"))
    assert len(charged) == 2
    stored = await env.links.get_public(link_id=link_id)
    assert stored is not None
    assert stored.download_count == 2


def test_download_cap_setting_defaults_to_20() -> None:
    assert _settings().share_link_max_downloads == 20
    with pytest.raises(ValueError):
        _settings(share_link_max_downloads=0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "/7%E5%A4%A9%E8%A1%80%E7%B3%96%E6%8A%A5%E5%91%8A.pdf",
        "/report.pdf",
        "/totally-wrong.exe",
    ],
)
async def test_both_path_forms_serve_and_the_name_segment_is_cosmetic(
    env: _Env, suffix: str
) -> None:
    """``…/files/<id>`` and ``…/files/<id>/<name>`` serve the same bytes; the name is
    ignored (any value), Content-Disposition keeps the real name."""
    tenant, user = uuid4(), uuid4()
    version = await env.save(tenant, user, _PDF)
    link_id = env.link(version)
    response = await env.client.get(f"/v1/public/files/{link_id}{suffix}")
    assert response.status_code == 200, response.text
    assert response.content == _PDF
    assert "report.pdf" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_a_wrong_id_with_a_real_name_is_still_404(env: _Env) -> None:
    tenant, user = uuid4(), uuid4()
    await env.save(tenant, user, _PDF)
    _assert_opaque_404(await env.client.get("/v1/public/files/Zz9Zz9Zz9Zz9Zz9Z/report.pdf"))


@pytest.mark.asyncio
async def test_link_is_pinned_to_its_version(env: _Env) -> None:
    tenant, user = uuid4(), uuid4()
    v1 = await env.save(tenant, user, b"first")
    await env.save(tenant, user, b"second")
    response = await env.client.get(f"/v1/public/files/{env.link(v1)}")
    assert response.status_code == 200
    assert response.content == b"first"
    assert response.headers["x-artifact-version"] == "1"


@pytest.mark.asyncio
async def test_html_is_forced_to_attachment(env: _Env) -> None:
    tenant, user = uuid4(), uuid4()
    version = await env.save(tenant, user, b"<script>x</script>", name="page.html")
    response = await env.client.get(f"/v1/public/files/{env.link(version)}")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")


@pytest.mark.asyncio
async def test_expired_link_is_the_same_404_and_is_kept(env: _Env) -> None:
    """H1 —— 公开下载从不删行(过期行由留存作业清):删了的话,读清单就没法从它续链。"""
    tenant, user = uuid4(), uuid4()
    version = await env.save(tenant, user, _PDF)
    link_id = env.link(version, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    _assert_opaque_404(await env.client.get(f"/v1/public/files/{link_id}"))
    assert await env.links.get_public(link_id=link_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("link_id", ["Zz9Zz9Zz9Zz9Zz9Z", "short", "x" * 17, "bad%00char0000000"])
async def test_unknown_or_malformed_id_is_404(env: _Env, link_id: str) -> None:
    _assert_opaque_404(await env.client.get(f"/v1/public/files/{link_id}"))


@pytest.mark.asyncio
async def test_soft_deleted_artifact_is_404(env: _Env) -> None:
    tenant, user = uuid4(), uuid4()
    version = await env.save(tenant, user, _PDF)
    link_id = env.link(version)
    await env.artifacts.soft_delete(
        tenant_id=tenant, user_id=user, agent_key=_AGENT, name="report.pdf", now=datetime.now(UTC)
    )
    _assert_opaque_404(await env.client.get(f"/v1/public/files/{link_id}"))


@pytest.mark.asyncio
async def test_link_to_a_removed_version_is_404(env: _Env) -> None:
    """Retention / purge removed the version row; the link row has no FK and stays."""
    tenant, user = uuid4(), uuid4()
    v1 = await env.save(tenant, user, b"first")
    await env.save(tenant, user, b"second")
    link_id = env.link(v1)
    await env.artifacts.delete_versions(version_ids=[v1.id])
    _assert_opaque_404(await env.client.get(f"/v1/public/files/{link_id}"))


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["tenant_id", "user_id", "agent_key", "artifact_version_id"])
async def test_link_resolves_only_inside_its_own_scope(env: _Env, field: str) -> None:
    """A row naming another tenant / user / agent (or a different version row) than the
    artifact it points at reaches nothing — reads run in the row's own scope."""
    tenant, user = uuid4(), uuid4()
    version = await env.save(tenant, user, _PDF)
    value: Any = "other-agent" if field == "agent_key" else uuid4()
    _assert_opaque_404(
        await env.client.get(f"/v1/public/files/{env.link(version, **{field: value})}")
    )


@pytest.mark.asyncio
async def test_suspended_tenant_links_are_404(env: _Env) -> None:
    class _Suspended:
        async def is_suspended(self, tenant_id: UUID) -> bool:
            del tenant_id
            return True

    tenant, user = uuid4(), uuid4()
    version = await env.save(tenant, user, _PDF)
    env.app.state.tenant_status_service = _Suspended()
    _assert_opaque_404(await env.client.get(f"/v1/public/files/{env.link(version)}"))


@dataclass
class _ScopeSpyArtifacts:
    """Wraps the real store; records the RLS context vars at each read."""

    inner: ArtifactStore
    seen: list[tuple[str, UUID | None, UUID | None, bool]]

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.inner, name)
        if name not in {"get_by_id", "get_version_by_id"}:
            return target

        async def _spy(**kwargs: Any) -> Any:
            self.seen.append(
                (name, current_tenant_id_var.get(), current_user_id_var.get(), bypass_rls_var.get())
            )
            return await target(**kwargs)

        return _spy


class _DenyingQuota:
    """Denies every check — the tenant's download quota is exhausted."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def check(self, req: Any) -> CheckResult:
        self.requests.append(req)
        return CheckResult(
            allowed=False,
            blocked_dimension=QuotaDimension.ARTIFACT_DOWNLOAD_COUNT_30D,
            retry_after_s=None,
        )


@pytest.mark.asyncio
async def test_quota_denial_is_429_with_no_bytes_and_no_artifact_reads(env: _Env) -> None:
    tenant, user = uuid4(), uuid4()
    version = await env.save(tenant, user, _PDF)
    link_id = env.link(version)
    quota = _DenyingQuota()
    env.app.state.quota_service = quota
    spy = _ScopeSpyArtifacts(inner=env.artifacts, seen=[])
    env.app.state.artifact_store = spy
    response = await env.client.get(f"/v1/public/files/{link_id}")
    assert response.status_code == 429
    assert _PDF not in response.content
    assert response.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    [request] = quota.requests
    assert request.tenant_id == tenant
    assert request.resource_kind == "artifact_download"
    assert spy.seen == []  # charged before any artifact read
    assert not [
        e for e in env.audit._rows.values() if e.action == AuditAction.ARTIFACT_SHARE_DOWNLOAD
    ]
    # L-f —— a download that did not happen does not use up the link's cap.
    stored = await env.links.get_public(link_id=link_id)
    assert stored is not None
    assert stored.download_count == 0


@pytest.mark.asyncio
async def test_a_failed_read_gives_the_cap_back(env: _Env) -> None:
    """L-f —— the version vanished after the claim (404): the claim is refunded."""
    tenant, user = uuid4(), uuid4()
    v1 = await env.save(tenant, user, b"first")
    await env.save(tenant, user, b"second")
    link_id = env.link(v1)
    await env.artifacts.delete_versions(version_ids=[v1.id])
    _assert_opaque_404(await env.client.get(f"/v1/public/files/{link_id}"))
    stored = await env.links.get_public(link_id=link_id)
    assert stored is not None
    assert stored.download_count == 0


@pytest.mark.asyncio
async def test_artifact_reads_run_in_the_links_own_scope(env: _Env) -> None:
    """The id lookup is cross-tenant (bypass); every read after it runs with the link's
    tenant + user GUCs and the bypass off — and the request leaves no context behind."""
    tenant, user = uuid4(), uuid4()
    version = await env.save(tenant, user, _PDF)
    link_id = env.link(version)
    spy = _ScopeSpyArtifacts(inner=env.artifacts, seen=[])
    env.app.state.artifact_store = spy
    response = await env.client.get(f"/v1/public/files/{link_id}")
    assert response.status_code == 200
    assert spy.seen == [
        ("get_by_id", tenant, user, False),
        ("get_version_by_id", tenant, user, False),
    ]
    assert current_tenant_id_var.get() is None
    assert current_user_id_var.get() is None
    assert bypass_rls_var.get() is False


# --------------------------------------------------- auth exemption scope


def test_public_prefix_is_auth_exempt_by_default() -> None:
    exempt = _settings().auth_exempt_path_prefixes
    assert "/v1/public/files" in exempt
    assert "/v1/public" not in exempt  # only the download route, not the whole namespace


def test_public_prefix_is_not_exempt_from_overload_shedding(env: _Env) -> None:
    """Auth-exempt, but the backpressure guard still covers it (DB + whole file in memory)."""
    [backpressure] = [
        m for m in env.app.user_middleware if m.cls.__name__ == "BackpressureMiddleware"
    ]
    exempt = backpressure.kwargs["exempt_path_prefixes"]
    assert "/v1/public/files" not in exempt
    assert "/healthz" in exempt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/v1/agents",
        "/v1/publicfiles/abc",
        "/v1/agents/x/artifacts",
        "/v1/public/other",
        "/v1/public/filesX/abc",
        "/v1/public/other/abc/report.pdf",
    ],
)
async def test_other_paths_still_require_credentials(env: _Env, path: str) -> None:
    response = await env.client.get(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_MISSING_CREDENTIALS"


# ---------------------------------------------------------------- settings


def test_public_base_url_is_normalized_and_validated() -> None:
    assert _settings().public_base_url is None
    assert _settings(public_base_url="").public_base_url is None
    assert (
        _settings(public_base_url="https://expert-work-test.deepaihealth.com/").public_base_url
        == "https://expert-work-test.deepaihealth.com"
    )
    with pytest.raises(ValueError, match="absolute http"):
        _settings(public_base_url="expert-work-test.deepaihealth.com")


class _TenantConfig:
    """``build_tool_env`` only closes over the service; nothing is called here."""


def test_build_tool_env_passes_the_share_service_through() -> None:
    service = ShareLinkService(
        links=InMemoryArtifactShareLinkStore(),
        artifacts=InMemoryArtifactStore(),
        public_base_url="https://x.example",
    )
    wired = build_tool_env(_TenantConfig(), share_links=service)  # type: ignore[arg-type]
    assert wired.share_links is service
    assert build_tool_env(_TenantConfig()).share_links is None  # type: ignore[arg-type]
