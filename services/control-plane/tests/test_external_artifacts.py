"""对外产物端点测试 —— 阶段 3 PR-B(``GET /v1/agents/{agent_code}/artifacts``)。

Fixture shape mirrors ``test_external_workspace.py``: a service-account
(API-key-shaped) JWT client scoped to one tenant, with the app's default
in-memory ``ArtifactStore`` / ``TenantUserStore`` (``create_app`` defaults
both to an in-memory backend when ``settings.store_backend`` is not
``"sql"`` — confirmed by reading ``app.py``'s ``resolved_artifact_store`` /
``resolved_tenant_users`` construction — the same default
``test_external_workspace.py`` relies on for its own ``user_store``
fixture).

The task brief's sample test used ``external_client`` / ``seed_artifact`` /
``soft_delete_artifact`` / ``count_tenant_users`` fixture names verbatim,
but assumed all four already existed in this suite. Only ``external_client``
does (recreated here with the identical service-account JWT shape as
``test_external_workspace.py`` — see its docstring on why ``roles=()``
matters for a scope-gate test). The other three are new, module-local
fixtures:

* ``seed_artifact`` / ``soft_delete_artifact`` wrap
  ``ArtifactStore.save_version`` / ``soft_delete`` behind the module's own
  ``user_id`` (app-supplied string) → ``tenant_user.id`` resolution, mirroring
  how ``test_external_workspace.py``'s ``seeded_workspace`` resolves via
  ``user_store.resolve(..., subject_id=external_subject_id(user_id))``.
* ``count_tenant_users`` — there is no ``count_all()`` on ``TenantUserStore``
  (confirmed absent from base / memory / sql before writing this file); it is
  implemented via ``list_by_tenant(..., subject_type="user")``, the same
  substitution ``test_external_workspace.py``'s own ``_user_count`` helper
  already makes for the identical "no ghost row minted" assertion on the
  sibling ``/workspace/files`` endpoint.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.api import external_artifacts as _external_artifacts_module
from control_plane.api._external import external_subject_id
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.persistence import ArtifactStore, TenantUserStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import ArtifactVersion
from orchestrator.tools import RecordingWorkspaceStore, WorkspacePermissionError
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

#: Fixed (not per-test ``uuid4()``) so fixtures and test bodies can both
#: address it — mirrors ``test_external_workspace.py``'s module-level
#: ``_TENANT_ID``.
_TENANT_ID = uuid4()
_MAX_LIST_LIMIT = 1000


def _build_settings() -> Settings:
    return Settings(
        service_name="control_plane_test",
        env="dev",
        auth_mode="dev",
        db_dsn="postgresql+asyncpg://test@localhost/test",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


def _headers(*, scopes: tuple[str, ...]) -> dict[str, str]:
    """Bearer headers for a service-account (API-key style) principal.

    ``roles=()`` — ``make_test_jwt``'s default ``roles=("admin",)`` is meant
    for human-JWT tests; a service-account principal's RBAC roles come from
    ``scopes`` (``auth/rbac.py:_collect_roles``), so leaving the default in
    place would grant ADMIN via the JWT ``roles`` claim regardless of
    ``scopes``.
    """
    jwt = make_test_jwt(
        tenant_id=_TENANT_ID,
        subject="third-party-app",
        sub_type="service_account",
        roles=(),
        scopes=scopes,
    )
    return {"Authorization": f"Bearer {jwt}"}


@dataclass
class _Ctx:
    app: object
    artifact_store: ArtifactStore
    workspace_store: RecordingWorkspaceStore


@pytest.fixture
def _ctx() -> _Ctx:
    app = create_app(
        settings=_build_settings(),
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
    )
    workspace_store = RecordingWorkspaceStore()
    app.state.workspace_store = workspace_store  # type: ignore[attr-defined]
    return _Ctx(
        app=app,
        artifact_store=app.state.artifact_store,  # type: ignore[attr-defined]
        workspace_store=workspace_store,
    )


@pytest.fixture
async def external_client(_ctx: _Ctx) -> AsyncIterator[AsyncClient]:
    """A service-account principal with ``read`` scope — the third-party plane."""
    transport = ASGITransport(app=_ctx.app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers(scopes=("read",))
    ) as client:
        yield client


@pytest.fixture
async def external_client_no_scope(_ctx: _Ctx) -> AsyncIterator[AsyncClient]:
    """Same tenant, same app, but a service-account key minted with zero scopes.

    Mirrors ``test_external_workspace.py``'s fixture of the same name — the
    ``require("session", "read")`` gate on this router is otherwise untested
    behaviourally (``test_external_only_gate.py``'s auto-discovery only checks
    ``external_only()`` / employee-JWT denial, not scope; the
    ``test_console_lockdown.py`` behavioural table uses an ``admin``-scope key,
    which trivially satisfies ``read`` and so can't catch this gate being
    dropped or miswired).
    """
    transport = ASGITransport(app=_ctx.app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers(scopes=())
    ) as client:
        yield client


@pytest.fixture
def user_store(_ctx: _Ctx) -> TenantUserStore:
    return _ctx.app.state.tenant_user_repo  # type: ignore[no-any-return,attr-defined]


@pytest.fixture
def seed_artifact(_ctx: _Ctx, user_store: TenantUserStore) -> Callable[..., Awaitable[None]]:
    """Register one artifact version for an app-supplied ``user_id``.

    Resolves ``user_id`` through the same ``external_subject_id`` namespace
    the endpoint's ``lookup_external_user_id`` reads back from, so a seeded
    row is actually visible to the handler under test.
    """

    async def _seed(*, user_id: str, name: str, kind: str = "document") -> None:
        row = await user_store.resolve(
            tenant_id=_TENANT_ID,
            subject_type="user",
            subject_id=external_subject_id(user_id),
        )
        await _ctx.artifact_store.save_version(
            tenant_id=_TENANT_ID,
            user_id=row.id,
            name=name,
            kind=kind,  # type: ignore[arg-type]
            path_in_workspace=name,
            created_in_thread="t-1",
        )

    return _seed


@pytest.fixture
def soft_delete_artifact(_ctx: _Ctx, user_store: TenantUserStore) -> Callable[..., Awaitable[None]]:
    async def _soft_delete(*, user_id: str, name: str) -> None:
        row = await user_store.resolve(
            tenant_id=_TENANT_ID,
            subject_type="user",
            subject_id=external_subject_id(user_id),
        )
        hit = await _ctx.artifact_store.soft_delete(
            tenant_id=_TENANT_ID, user_id=row.id, name=name, now=datetime.now(UTC)
        )
        assert hit, f"soft_delete_artifact: no active artifact named {name!r} for {user_id!r}"

    return _soft_delete


@pytest.fixture
def count_tenant_users(user_store: TenantUserStore) -> Callable[[], Awaitable[int]]:
    """Count this tenant's ``subject_type="user"`` rows.

    ``TenantUserStore`` has no ``count_all()`` (confirmed absent from base /
    memory / sql). ``test_external_workspace.py``'s ``_user_count`` helper
    makes the identical substitution for the sibling "no ghost row minted"
    assertion on ``GET .../workspace/files``.
    """

    async def _count() -> int:
        rows = await user_store.list_by_tenant(
            _TENANT_ID, subject_type="user", limit=_MAX_LIST_LIMIT
        )
        return len(rows)

    return _count


@pytest.fixture
def seed_artifact_with_content(
    _ctx: _Ctx, user_store: TenantUserStore
) -> Callable[..., Awaitable[None]]:
    """Like ``seed_artifact``, but also drops real bytes into the
    workspace-store recorder so the download endpoint has something to read.

    ``RecordingWorkspaceStore.read_file`` ignores the requested path and
    always returns whatever ``workspace_file`` is currently set to (same
    single-fixture shape ``test_external_workspace.py``'s ``seeded_workspace``
    relies on) — fine here because every test using this fixture seeds and
    downloads exactly one artifact.
    """

    async def _seed(*, user_id: str, name: str, content: bytes, kind: str = "document") -> None:
        row = await user_store.resolve(
            tenant_id=_TENANT_ID,
            subject_type="user",
            subject_id=external_subject_id(user_id),
        )
        await _ctx.artifact_store.save_version(
            tenant_id=_TENANT_ID,
            user_id=row.id,
            name=name,
            kind=kind,  # type: ignore[arg-type]
            path_in_workspace=name,
            created_in_thread="t-1",
        )
        _ctx.workspace_store.workspace_file = content

    return _seed


@pytest.fixture
def break_workspace_permission(_ctx: _Ctx) -> Callable[[], None]:
    """Make the next ``workspace_store.read_file`` call raise
    ``WorkspacePermissionError`` — the download handler's permission-failure
    path (see ``test_permission_error_is_500_not_404`` and its except-order
    mutation test)."""

    def _break() -> None:
        _ctx.workspace_store.workspace_file_error = WorkspacePermissionError("boom")

    return _break


@pytest.fixture
def get_latest_version(
    _ctx: _Ctx, user_store: TenantUserStore
) -> Callable[[str, str], Awaitable[ArtifactVersion | None]]:
    """Read back an artifact's latest version row by the app-supplied
    ``user_id`` — used to assert the digest-backfill side effect."""

    async def _get(user_id: str, name: str) -> ArtifactVersion | None:
        row = await user_store.resolve(
            tenant_id=_TENANT_ID,
            subject_type="user",
            subject_id=external_subject_id(user_id),
        )
        return await _ctx.artifact_store.get_latest_version(
            tenant_id=_TENANT_ID, user_id=row.id, name=name
        )

    return _get


@pytest.fixture
def quota_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Record ``(resource_kind, cost)`` pairs the download handler passes to
    ``check_admission``.

    ``check_admission``'s underlying ``CheckRequest`` has no ``resource_kind``
    field at all — it only reaches the audit log's ``resource_id`` on a
    *denial*, which this suite's unconfigured (always-allow) default quota
    service never produces. So the only way to observe "the handler really
    passed resource_kind='artifact_download'" is to wrap the name the
    handler calls (module-level import, patched where it is looked up —
    ``external_artifacts.check_admission``), not to stub
    ``QuotaService.check``.
    """
    calls: list[tuple[str, int]] = []
    original = _external_artifacts_module.check_admission

    async def _recording(*, resource_kind: str, cost: int = 1, **kwargs: object):
        calls.append((resource_kind, cost))
        return await original(resource_kind=resource_kind, cost=cost, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(_external_artifacts_module, "check_admission", _recording)
    return calls


@pytest.mark.asyncio
async def test_list_returns_active_artifacts(external_client, seed_artifact) -> None:
    """列表返回 name/kind/latest_version/created_at/updated_at 五个字段。"""
    await seed_artifact(user_id="u-1", name="report.docx", kind="document")
    resp = await external_client.get("/v1/agents/test-agent/artifacts", params={"user_id": "u-1"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    items = body["data"]["artifacts"]
    assert len(items) == 1
    assert set(items[0]) == {
        "name",
        "kind",
        "latest_version",
        "created_at",
        "updated_at",
    }, "字段集必须精确 —— 多给 size_bytes 会误导(它只在首次下载后才有值)"
    assert items[0]["name"] == "report.docx"


@pytest.mark.asyncio
async def test_list_hides_soft_deleted(
    external_client, seed_artifact, soft_delete_artifact
) -> None:
    """软删的产物不出现在列表里。"""
    await seed_artifact(user_id="u-1", name="gone.docx", kind="document")
    await soft_delete_artifact(user_id="u-1", name="gone.docx")
    resp = await external_client.get("/v1/agents/test-agent/artifacts", params={"user_id": "u-1"})
    assert [a["name"] for a in resp.json()["data"]["artifacts"]] == []


@pytest.mark.asyncio
async def test_unknown_user_gets_empty_list_and_mints_nothing(
    external_client, count_tenant_users
) -> None:
    """未知 user_id 返回空列表,且不建 tenant_user 行。

    这条是 P1 review T3 的不变式:第三方拿任意字符串刷这个端点,不能每刷
    一次就留一行幽灵用户。删掉实现里的 mint=False 语义(改用
    resolve_external_user_id)时这条必须红。
    """
    before = await count_tenant_users()
    resp = await external_client.get(
        "/v1/agents/test-agent/artifacts", params={"user_id": "never-seen-before"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["artifacts"] == []
    assert await count_tenant_users() == before


@pytest.mark.asyncio
async def test_agent_code_does_not_filter(external_client, seed_artifact) -> None:
    """agent_code 不参与过滤 —— 换一个(甚至不存在的)agent_code 拿到同一份列表。"""
    await seed_artifact(user_id="u-1", name="report.docx", kind="document")
    a = await external_client.get("/v1/agents/test-agent/artifacts", params={"user_id": "u-1"})
    b = await external_client.get(
        "/v1/agents/no-such-agent-at-all/artifacts", params={"user_id": "u-1"}
    )
    assert a.status_code == b.status_code == 200
    assert a.json()["data"] == b.json()["data"]


@pytest.mark.asyncio
async def test_list_requires_read_scope(external_client_no_scope) -> None:
    """零 scope 的 key 必须被 ``require("session", "read")`` 拒掉。

    评审 Important:这道闸此前没有任何行为测试守着 —— 手滑删掉 / 改错
    ``Depends(require("session", "read"))`` 时,全仓测试套件不会变红,第三方
    拿一把零 scope 的 key 就能列出任意终端用户的产物清单。见
    ``test_external_workspace.py::test_list_files_requires_read_scope``。
    """
    resp = await external_client_no_scope.get(
        "/v1/agents/test-agent/artifacts", params={"user_id": "u-1"}
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /v1/agents/{agent_code}/artifacts/download  (Task 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_returns_raw_bytes_not_envelope(
    external_client, seed_artifact_with_content
) -> None:
    """成功响应是裸字节流,不套 {success, data, error}。"""
    await seed_artifact_with_content(
        user_id="u-1", name="report.txt", kind="document", content=b"hello"
    )
    resp = await external_client.get(
        "/v1/agents/test-agent/artifacts/download",
        params={"user_id": "u-1", "name": "report.txt"},
    )
    assert resp.status_code == 200
    assert resp.content == b"hello"
    assert resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_download_forces_attachment_for_active_content(
    external_client, seed_artifact_with_content
) -> None:
    """HTML/SVG 这类可执行内容强制 attachment —— XSS 防护。"""
    await seed_artifact_with_content(
        user_id="u-1", name="page.html", kind="document", content=b"<script>alert(1)</script>"
    )
    resp = await external_client.get(
        "/v1/agents/test-agent/artifacts/download",
        params={"user_id": "u-1", "name": "page.html"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("attachment")


@pytest.mark.asyncio
async def test_download_unknown_name_is_404_enveloped(external_client) -> None:
    """不存在的 name → 404,错误路径套信封。"""
    resp = await external_client.get(
        "/v1/agents/test-agent/artifacts/download",
        params={"user_id": "u-1", "name": "nope.txt"},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "ARTIFACT_NOT_FOUND"


@pytest.mark.asyncio
async def test_permission_error_is_500_not_404(
    external_client, seed_artifact_with_content, break_workspace_permission
) -> None:
    """权限失败 → 500,不能和「不存在」合并成 404。

    W2-BUG-1 的教训:元数据行在、内容读不动是**服务端配置问题**,报 404 会
    让对接方永远在查自己的 name 是不是拼错了。这条测试同时锁住 except 顺序 ——
    WorkspacePermissionError 是 SandboxSupervisorError 的子类,把它写在后面
    就永远走不到,这个断言会红。
    """
    await seed_artifact_with_content(
        user_id="u-1", name="report.txt", kind="document", content=b"x"
    )
    break_workspace_permission()
    resp = await external_client.get(
        "/v1/agents/test-agent/artifacts/download",
        params={"user_id": "u-1", "name": "report.txt"},
    )
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "ARTIFACT_CONTENT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_download_backfills_digest_on_first_read(
    external_client, seed_artifact_with_content, get_latest_version
) -> None:
    """首次下载回填 size_bytes / sha256。"""
    await seed_artifact_with_content(
        user_id="u-1", name="report.txt", kind="document", content=b"hello"
    )
    assert (await get_latest_version("u-1", "report.txt")).size_bytes is None
    await external_client.get(
        "/v1/agents/test-agent/artifacts/download",
        params={"user_id": "u-1", "name": "report.txt"},
    )
    version = await get_latest_version("u-1", "report.txt")
    assert version.size_bytes == 5
    assert version.sha256 == ("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")


@pytest.mark.asyncio
async def test_download_deducts_quota(
    external_client, seed_artifact_with_content, quota_calls
) -> None:
    """下载走配额准入 —— resource_kind='artifact_download', cost=1。"""
    await seed_artifact_with_content(
        user_id="u-1", name="report.txt", kind="document", content=b"x"
    )
    await external_client.get(
        "/v1/agents/test-agent/artifacts/download",
        params={"user_id": "u-1", "name": "report.txt"},
    )
    assert ("artifact_download", 1) in quota_calls


@pytest.mark.asyncio
async def test_download_requires_read_scope(external_client_no_scope) -> None:
    """零 scope 的 key 必须被 ``require("session", "read")`` 拒掉 —— 同
    ``test_list_requires_read_scope``,下载端点是这道闸独立的第二个挂点,删掉
    / 改错不会被上一条测试捕获(评审 Important,见 Task 1 review 留下的经验)。
    """
    resp = await external_client_no_scope.get(
        "/v1/agents/test-agent/artifacts/download",
        params={"user_id": "u-1", "name": "report.txt"},
    )
    assert resp.status_code == 403
