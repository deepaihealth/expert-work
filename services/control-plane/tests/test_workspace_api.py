"""Tests for ``/v1/workspace`` — user-scoped workspace browse / download / delete.

The point of this router (vs the thread-scoped ``/v1/sessions/{id}/workspace*``)
is that access is keyed on the *user*, not a thread — so none of these tests
create a thread, and the workspace stays reachable after every session is gone.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import InMemoryArtifactStore, InMemoryTenantUserStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import AuditAction, AuditQuery
from orchestrator.tools import (
    RecordingWorkspaceStore,
    SandboxSupervisorError,
    WorkspaceFileEntry,
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
# A store-side ``WorkspacePermissionError`` message shaped like the real one
# (path / uid / mode) — used to prove the HTTP layer never echoes it back.
_LEAKY_DETAIL = (
    "PermissionError(13, 'Permission denied'): "
    "'/mnt/workspaces/t-1/u-1/report.pdf' uid=10002 mode=0o600"
)


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
async def setup() -> AsyncIterator[tuple[AsyncClient, RecordingWorkspaceStore, UUID]]:
    users, artifacts, user_id = await _seed()
    app = create_app(
        settings=_settings(),
        tenant_user_repo=users,
        artifact_repo=artifacts,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    store = RecordingWorkspaceStore(
        workspace_file=_CONTENT,
        workspace_files=[WorkspaceFileEntry(path="out.txt", size=11)],
    )
    app.state.workspace_store = store
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers()
    ) as client:
        yield client, store, user_id


# ---------------------------------------------------------------------------
# GET /v1/workspace — meta + artifacts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workspace_null_when_no_vm_ever_started(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    """``workspaces.get`` never provisions — a null workspace is truthful."""
    client, _, _ = setup
    resp = await client.get("/v1/workspace")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["workspace"] is None
    # Artifacts are still surfaced independent of a live volume.
    assert [a["name"] for a in data["artifacts"]] == ["report.md"]


@pytest.mark.asyncio
async def test_get_workspace_returns_meta_when_seeded(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    client, _, user_id = setup
    # A VM having started for this user is modelled by a resolved row.
    ws_store = client._transport.app.state.user_workspace_store  # type: ignore[attr-defined,union-attr]
    seeded = await ws_store.resolve(tenant_id=_TENANT, user_id=user_id)
    resp = await client.get("/v1/workspace")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["workspace"]["volume_name"] == seeded.volume_name


@pytest.mark.asyncio
async def test_get_workspace_reachable_with_no_thread(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    """The whole point: no thread exists, yet the workspace is reachable."""
    client, _, _ = setup
    resp = await client.get("/v1/workspace/files")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["files"] == [{"path": "out.txt", "size": 11}]


# ---------------------------------------------------------------------------
# GET /v1/workspace/files — browse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_files_self(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    client, supervisor, user_id = setup
    resp = await client.get("/v1/workspace/files")
    assert resp.status_code == 200
    assert resp.json()["data"]["files"] == [{"path": "out.txt", "size": 11}]
    # Supervisor read is keyed to the caller's own workspace.
    assert supervisor.workspace_reads[-1] == (_TENANT, user_id, "")


@pytest.mark.asyncio
async def test_admin_lists_another_users_files_via_user_id(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    client, supervisor, user_id = setup
    resp = await client.get(f"/v1/workspace/files?user_id={user_id}", headers=_headers("user-b"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["files"] == [{"path": "out.txt", "size": 11}]
    assert supervisor.workspace_reads[-1] == (_TENANT, user_id, "")


@pytest.mark.asyncio
async def test_non_admin_files_for_someone_else_is_403(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    client, _, user_id = setup
    viewer_jwt = make_test_jwt(tenant_id=_TENANT, subject="user-b", roles=("viewer",))
    resp = await client.get(
        f"/v1/workspace/files?user_id={user_id}",
        headers={"Authorization": f"Bearer {viewer_jwt}"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "USER_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_list_files_without_supervisor_returns_empty() -> None:
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
        resp = await client.get("/v1/workspace/files")
    assert resp.status_code == 200
    assert resp.json()["data"]["files"] == []


@pytest.mark.asyncio
async def test_list_files_reports_permission_denied_as_server_error(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    """store 抛 WorkspacePermissionError → 500,不是 200 空列表。

    权限读不动是**服务端配置问题**(共享 uid 没配上 / 存量目录属主没迁移 /
    目录 mode 不对),不是"这个用户没有文件"。这里如果和普通
    SandboxSupervisorError 一样吞成 ``{"files": []}``,用户会看到"工作区是
    空的"——比 404 还坏,连"出错了"都看不到,诊断成本全压在服务端日志上
    (W2-BUG-1)。响应体不含路径 / uid / mode。
    """
    client, supervisor, _ = setup
    supervisor.workspace_list_error = WorkspacePermissionError(_LEAKY_DETAIL)
    resp = await client.get("/v1/workspace/files")
    assert resp.status_code == 500, resp.text
    assert "/mnt/workspaces" not in resp.text
    assert "10002" not in resp.text
    assert "0o600" not in resp.text


@pytest.mark.asyncio
async def test_list_files_still_empty_on_a_generic_supervisor_error(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    """对照组:普通 SandboxSupervisorError(非权限)仍降级成空列表,不是 500。

    防止把"supervisor 一时联系不上就先给个空列表,别吓着用户"这条既有姿态
    一并改坏——只有权限失败这一种从"吞成空"里拆出来。
    """
    client, supervisor, _ = setup
    supervisor.workspace_list_error = SandboxSupervisorError("supervisor unreachable")
    resp = await client.get("/v1/workspace/files")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["files"] == []


# ---------------------------------------------------------------------------
# GET /v1/workspace/file — download
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_file_self(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    client, supervisor, user_id = setup
    resp = await client.get("/v1/workspace/file", params={"path": "out.txt"})
    assert resp.status_code == 200
    assert resp.content == _CONTENT
    assert resp.headers["x-content-type-options"] == "nosniff"
    # ``.txt`` is text-like → inline (non-active content), same as artifact .md.
    assert "inline" in resp.headers["content-disposition"]
    assert supervisor.workspace_reads[-1] == (_TENANT, user_id, "out.txt")


@pytest.mark.asyncio
async def test_admin_downloads_another_users_file_via_user_id(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    client, _, user_id = setup
    resp = await client.get(
        "/v1/workspace/file",
        params={"path": "out.txt", "user_id": str(user_id)},
        headers=_headers("user-b"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == _CONTENT


@pytest.mark.asyncio
async def test_non_admin_download_someone_else_is_403(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    client, _, user_id = setup
    viewer_jwt = make_test_jwt(tenant_id=_TENANT, subject="user-b", roles=("viewer",))
    resp = await client.get(
        "/v1/workspace/file",
        params={"path": "out.txt", "user_id": str(user_id)},
        headers={"Authorization": f"Bearer {viewer_jwt}"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "USER_SCOPE_FORBIDDEN"


@pytest.mark.asyncio
async def test_download_html_file_is_forced_attachment(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    """Active content (HTML) must never inline-render — stored-XSS red line."""
    client, _, _ = setup
    resp = await client.get("/v1/workspace/file", params={"path": "page.html"})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_download_traversal_path_is_400(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    client, _, _ = setup
    resp = await client.get("/v1/workspace/file", params={"path": "../etc/passwd"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_download_without_supervisor_is_404() -> None:
    users, artifacts, _ = await _seed()
    app = create_app(
        settings=_settings(),
        tenant_user_repo=users,
        artifact_repo=artifacts,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers()
    ) as client:
        resp = await client.get("/v1/workspace/file", params={"path": "out.txt"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_file_reports_permission_denied_as_server_error(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    """store 抛 WorkspacePermissionError → 500,不是 404。

    404 的语义是"不存在 / 你不该知道它存在";权限读不动是服务端配置问题,
    塞进 404 会让用户看到"文件不存在",而它明明列在上一屏(W2-BUG-1)。响应体
    不含路径 / uid / mode。
    """
    client, supervisor, _ = setup
    supervisor.workspace_file_error = WorkspacePermissionError(_LEAKY_DETAIL)
    resp = await client.get("/v1/workspace/file", params={"path": "out.txt"})
    assert resp.status_code == 500, resp.text
    assert "/mnt/workspaces" not in resp.text
    assert "10002" not in resp.text
    assert "0o600" not in resp.text


@pytest.mark.asyncio
async def test_download_file_still_404s_on_a_generic_supervisor_error(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    """对照组:普通 SandboxSupervisorError(非权限,比如真的没这个文件)仍是 404。

    防止把"隐藏跨用户存在性"这条既有安全姿态一并改坏——只有权限失败这一种
    从 404 里拆出来。
    """
    client, supervisor, _ = setup
    supervisor.workspace_file_error = SandboxSupervisorError("not found")
    resp = await client.get("/v1/workspace/file", params={"path": "out.txt"})
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# DELETE /v1/workspace/file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_file_self_records_on_supervisor(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    client, supervisor, user_id = setup
    resp = await client.delete("/v1/workspace/file", params={"path": "out.txt"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["deleted"] == "out.txt"
    assert supervisor.workspace_deletes[-1] == (_TENANT, user_id, "out.txt")


@pytest.mark.asyncio
async def test_admin_deletes_another_users_file_via_user_id(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    client, supervisor, user_id = setup
    resp = await client.delete(
        "/v1/workspace/file",
        params={"path": "out.txt", "user_id": str(user_id)},
        headers=_headers("user-b"),
    )
    assert resp.status_code == 200, resp.text
    assert supervisor.workspace_deletes[-1] == (_TENANT, user_id, "out.txt")


@pytest.mark.asyncio
async def test_non_admin_delete_someone_else_is_403(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    client, supervisor, user_id = setup
    viewer_jwt = make_test_jwt(tenant_id=_TENANT, subject="user-b", roles=("viewer",))
    resp = await client.delete(
        "/v1/workspace/file",
        params={"path": "out.txt", "user_id": str(user_id)},
        headers={"Authorization": f"Bearer {viewer_jwt}"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "USER_SCOPE_FORBIDDEN"
    # The gate fires before any supervisor mutation.
    assert supervisor.workspace_deletes == []


@pytest.mark.asyncio
async def test_delete_traversal_path_is_400(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    client, _, _ = setup
    resp = await client.delete("/v1/workspace/file", params={"path": "/abs/path"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_file_reports_permission_denied_as_server_error(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    """store 抛 WorkspacePermissionError → 500,不是 404。

    同下载:权限失败是服务端配置问题,不应该被 404 的"不存在/你不该知道它
    存在"语义盖过去。响应体不含路径 / uid / mode。
    """
    client, supervisor, _ = setup
    supervisor.workspace_delete_error = WorkspacePermissionError(_LEAKY_DETAIL)
    resp = await client.delete("/v1/workspace/file", params={"path": "out.txt"})
    assert resp.status_code == 500, resp.text
    assert "/mnt/workspaces" not in resp.text
    assert "10002" not in resp.text
    assert "0o600" not in resp.text


@pytest.mark.asyncio
async def test_delete_file_still_404s_on_a_generic_supervisor_error(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    """对照组:普通 SandboxSupervisorError(非权限)仍是 404,既有姿态不变。"""
    client, supervisor, _ = setup
    supervisor.workspace_delete_error = SandboxSupervisorError("not found")
    resp = await client.delete("/v1/workspace/file", params={"path": "out.txt"})
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Phase 2 — governance auditing (Phase 1 delete had none)
# ---------------------------------------------------------------------------


async def _app_with_audit() -> tuple[AsyncClient, InMemoryAuditLogStore, UUID]:
    """Fresh app whose audit store is accessible (the ``setup`` fixture hides it)."""
    users, artifacts, user_id = await _seed()
    audit_store = InMemoryAuditLogStore()
    app = create_app(
        settings=_settings(),
        tenant_user_repo=users,
        artifact_repo=artifacts,
        audit_logger=build_default_audit_logger(audit_store),
        jwt_verifier=build_test_jwt_verifier(),
    )
    app.state.workspace_store = RecordingWorkspaceStore(workspace_file=_CONTENT)
    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://cp.test", headers=_headers())
    return client, audit_store, user_id


@pytest.mark.asyncio
async def test_delete_file_emits_audit() -> None:
    client, audit_store, _ = await _app_with_audit()
    async with client:
        resp = await client.delete("/v1/workspace/file", params={"path": "out.txt"})
        assert resp.status_code == 200, resp.text
    page = await audit_store.query(AuditQuery(tenant_id=_TENANT))
    dels = [r for r in page.entries if r.action is AuditAction.WORKSPACE_FILE_DELETE]
    assert dels and dels[-1].details.get("path") == "out.txt"


@pytest.mark.asyncio
async def test_admin_view_another_users_workspace_emits_view_audit() -> None:
    client, audit_store, user_id = await _app_with_audit()
    async with client:
        # user-b (admin) opens user-a's workspace via the governance target.
        resp = await client.get(f"/v1/workspace?user_id={user_id}", headers=_headers("user-b"))
        assert resp.status_code == 200, resp.text
    page = await audit_store.query(AuditQuery(tenant_id=_TENANT))
    views = [r for r in page.entries if r.action is AuditAction.USER_DATA_VIEW]
    assert views and views[-1].resource_id == str(user_id)
    assert views[-1].details.get("view") == "workspace"


@pytest.mark.asyncio
async def test_self_workspace_view_does_not_emit_view_audit() -> None:
    client, audit_store, _ = await _app_with_audit()
    async with client:
        # The owner reading her own workspace leaves no cross-user trail.
        resp = await client.get("/v1/workspace")
        assert resp.status_code == 200
    page = await audit_store.query(AuditQuery(tenant_id=_TENANT))
    assert not any(r.action is AuditAction.USER_DATA_VIEW for r in page.entries)


# ---------------------------------------------------------------------------
# W3 — workspace 读端点接跨租户 scope(系统管理员租户切换器)
#
# 三件套 per endpoint:system_admin 带目标租户 tenant_id(+user_id)→ 200
# 命中目标租户用户数据;普通租户用户带他租户 tenant_id → 403
# TENANT_NOT_ALLOWED;tenant_id=* → 400 SCOPE_ALL_NOT_SUPPORTED。照 W2 先例。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workspace_system_admin_target_tenant_200(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    client, _, user_id = setup
    headers = await grant_system_admin(client)
    params = {"tenant_id": str(_TENANT), "user_id": str(user_id)}

    meta = await client.get("/v1/workspace", params=params, headers=headers)
    assert meta.status_code == 200, meta.text
    assert [a["name"] for a in meta.json()["data"]["artifacts"]] == ["report.md"]

    files = await client.get("/v1/workspace/files", params=params, headers=headers)
    assert files.status_code == 200, files.text
    assert files.json()["data"]["files"] == [{"path": "out.txt", "size": 11}]

    download = await client.get(
        "/v1/workspace/file", params={**params, "path": "out.txt"}, headers=headers
    )
    assert download.status_code == 200, download.text
    assert download.content == _CONTENT


@pytest.mark.asyncio
async def test_workspace_foreign_tenant_user_403(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    client, _, user_id = setup
    foreign = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}
    for name, path, extra in [
        ("get_workspace", "/v1/workspace", {}),
        ("list_files", "/v1/workspace/files", {}),
        ("download", "/v1/workspace/file", {"path": "out.txt"}),
    ]:
        resp = await client.get(
            path,
            params={"tenant_id": str(_TENANT), "user_id": str(user_id), **extra},
            headers=foreign,
        )
        assert resp.status_code == 403, f"{name}: {resp.status_code} {resp.text}"
        assert resp.json()["detail"]["code"] == "TENANT_NOT_ALLOWED", name


@pytest.mark.asyncio
async def test_workspace_tenant_id_star_400(
    setup: tuple[AsyncClient, RecordingWorkspaceStore, UUID],
) -> None:
    client, _, _ = setup
    for name, path, extra in [
        ("get_workspace", "/v1/workspace", {}),
        ("list_files", "/v1/workspace/files", {}),
        ("download", "/v1/workspace/file", {"path": "out.txt"}),
    ]:
        resp = await client.get(path, params={"tenant_id": "*", **extra})
        assert resp.status_code == 400, f"{name}: {resp.status_code} {resp.text}"
        assert resp.json()["detail"]["code"] == "SCOPE_ALL_NOT_SUPPORTED", name
