"""对外工作区端点(P2-b Task 1)—— ``GET /v1/agents/{agent_code}/workspace/files``.

Fixture shape mirrors ``test_external_hardening.py`` / ``test_workspace_api.py``:
a service-account (API-key-shaped) JWT client scoped to one tenant, plus a
``RecordingWorkspaceStore`` standing in for the sandbox supervisor.

``TenantUserStore`` has no ``count_all()`` — the task brief's sample test used
one, but no such method exists on the store (base / memory / sql all lack it;
confirmed by grep before writing this file). The "no ghost row minted"
assertion below instead counts ``list_by_tenant(..., subject_type="user")``
rows before/after, the same technique ``test_external_hardening.py``'s
``has_subject`` helper already uses for the identical check on the sibling
``/v1/agents/{agent_code}/sessions`` endpoint.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.api._external import external_subject_id
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.persistence.tenant_user import TenantUserStore
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
    make_test_jwt,
)

_AGENT_CODE = "plain-agent"
#: Fixed (not per-test ``uuid4()``) so the ``user_store`` fixture and a test
#: body can both address it without a shared context object — mirrors
#: ``test_workspace_api.py``'s module-level ``_TENANT``.
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
    ``scopes`` and make the scope gate untestable.
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
class _Agent:
    code: str


@dataclass
class _SeededWorkspace:
    agent_code: str
    user_id: str
    expected_bytes: bytes


@dataclass
class _Ctx:
    app: object
    workspace_store: RecordingWorkspaceStore


@pytest.fixture
def _ctx() -> _Ctx:
    app = create_app(
        settings=_build_settings(),
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
    )
    store = RecordingWorkspaceStore()
    app.state.workspace_store = store
    return _Ctx(app=app, workspace_store=store)


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
    """Same tenant, same app, but a service-account key minted with zero scopes."""
    transport = ASGITransport(app=_ctx.app)
    async with AsyncClient(
        transport=transport, base_url="http://cp.test", headers=_headers(scopes=())
    ) as client:
        yield client


@pytest.fixture
def plain_agent() -> _Agent:
    return _Agent(code=_AGENT_CODE)


@pytest.fixture
def user_store(_ctx: _Ctx) -> TenantUserStore:
    return _ctx.app.state.tenant_user_repo  # type: ignore[no-any-return,attr-defined]


@pytest.fixture
async def seeded_workspace(_ctx: _Ctx, user_store: TenantUserStore) -> _SeededWorkspace:
    """A recognized end-user with one file already sitting in the recorder.

    ``RecordingWorkspaceStore.read_file`` ignores the requested ``path`` and
    always returns ``workspace_file`` (a single fixture, not a per-path
    map) — so ``expected_bytes`` is what any successful download of this
    user's workspace returns, regardless of which path was asked for.
    """
    user_id = "报表用户"
    await user_store.resolve(
        tenant_id=_TENANT_ID,
        subject_type="user",
        subject_id=external_subject_id(user_id),
    )
    expected_bytes = b"report body"
    _ctx.workspace_store.workspace_files = [WorkspaceFileEntry(path="报表.xlsx", size=42)]
    _ctx.workspace_store.workspace_file = expected_bytes
    return _SeededWorkspace(agent_code=_AGENT_CODE, user_id=user_id, expected_bytes=expected_bytes)


@pytest.fixture
async def workspace_store_raising_permission_error(
    _ctx: _Ctx, user_store: TenantUserStore
) -> RecordingWorkspaceStore:
    """A recognized user ("u1") whose store call blows up with a permission error.

    Must seed the user first — an unrecognized ``user_id`` short-circuits to
    an empty list before the store is ever called (``mint=False``), which
    would make this fixture's error invisible to the endpoint and the test
    meaningless.
    """
    await user_store.resolve(
        tenant_id=_TENANT_ID, subject_type="user", subject_id=external_subject_id("u1")
    )
    _ctx.workspace_store.workspace_list_error = WorkspacePermissionError("boom")
    return _ctx.workspace_store


@pytest.fixture
async def workspace_store_raising_supervisor_error(
    _ctx: _Ctx, user_store: TenantUserStore
) -> RecordingWorkspaceStore:
    """对照组 fixture —— 同一个 helper 里,普通 ``SandboxSupervisorError``(非
    权限,比如 supervisor 一时联系不上)必须仍降级成空列表,不是 500。防止
    "权限失败要报错"这条改动把它的父类 ``SandboxSupervisorError`` 的既有降级
    行为一并改坏。"""
    await user_store.resolve(
        tenant_id=_TENANT_ID, subject_type="user", subject_id=external_subject_id("u1")
    )
    _ctx.workspace_store.workspace_list_error = SandboxSupervisorError("supervisor unreachable")
    return _ctx.workspace_store


@pytest.fixture
async def workspace_store_raising_file_permission_error(
    _ctx: _Ctx, user_store: TenantUserStore
) -> RecordingWorkspaceStore:
    """A recognized user ("u1") whose file *read* (not list) blows up with a
    permission error — the download-endpoint analogue of
    ``workspace_store_raising_permission_error`` above."""
    await user_store.resolve(
        tenant_id=_TENANT_ID, subject_type="user", subject_id=external_subject_id("u1")
    )
    _ctx.workspace_store.workspace_file_error = WorkspacePermissionError("boom")
    return _ctx.workspace_store


async def _user_count(users: TenantUserStore, *, tenant_id: UUID = _TENANT_ID) -> int:
    rows = await users.list_by_tenant(tenant_id, subject_type="user", limit=_MAX_LIST_LIMIT)
    return len(rows)


@pytest.mark.asyncio
async def test_list_files_returns_envelope(external_client, seeded_workspace) -> None:
    resp = await external_client.get(
        f"/v1/agents/{seeded_workspace.agent_code}/workspace/files",
        params={"user_id": seeded_workspace.user_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True and body["error"] is None
    assert {f["path"] for f in body["data"]["files"]} == {"报表.xlsx"}


@pytest.mark.asyncio
async def test_list_files_scopes_store_call_to_the_requested_user(
    external_client, _ctx: _Ctx, user_store: TenantUserStore
) -> None:
    """跨用户隔离 —— store 调用必须落在被请求的那个 ``user_id`` 解析出的
    ``tenant_user.id`` 上,不是别的用户 / 一个固定值。

    ``RecordingWorkspaceStore`` 对所有调用者返回同一份 ``workspace_files``
    (它不按用户分内容),所以这里不能靠响应体判断隔离是否生效 —— 改成检查
    store 实际收到的 ``(tenant_id, user_id)`` 调用参数,与 ``user_id=cust-a``
    独立解析出的 ``tenant_user.id`` 精确相等,且与另一个用户 ``cust-b`` 的不
    相等。摘掉身份解析(比如改成不管 ``user_id`` 传什么都用同一个内部身份)
    会让这条断言失败。
    """
    user_a = await user_store.resolve(
        tenant_id=_TENANT_ID, subject_type="user", subject_id=external_subject_id("cust-a")
    )
    user_b = await user_store.resolve(
        tenant_id=_TENANT_ID, subject_type="user", subject_id=external_subject_id("cust-b")
    )
    assert user_a.id != user_b.id

    resp = await external_client.get(
        f"/v1/agents/{_AGENT_CODE}/workspace/files", params={"user_id": "cust-a"}
    )
    assert resp.status_code == 200
    assert _ctx.workspace_store.workspace_reads[-1] == (_TENANT_ID, user_a.id, "")
    assert _ctx.workspace_store.workspace_reads[-1][1] != user_b.id


@pytest.mark.asyncio
async def test_list_files_unknown_user_returns_empty_not_mint(
    external_client, plain_agent, user_store
) -> None:
    """读路径 mint=False —— 没见过的 user_id 不能铸出 tenant_user 行。"""
    before = await _user_count(user_store)
    resp = await external_client.get(
        f"/v1/agents/{plain_agent.code}/workspace/files",
        params={"user_id": "从没见过的用户"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["files"] == []
    assert await _user_count(user_store) == before


@pytest.mark.asyncio
async def test_list_files_requires_read_scope(external_client_no_scope, plain_agent) -> None:
    resp = await external_client_no_scope.get(
        f"/v1/agents/{plain_agent.code}/workspace/files", params={"user_id": "u1"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_files_permission_error_is_500_not_empty(
    external_client, plain_agent, workspace_store_raising_permission_error
) -> None:
    """权限失败必须冒出来 —— 吞成空列表会让用户以为工作区是空的。"""
    resp = await external_client.get(
        f"/v1/agents/{plain_agent.code}/workspace/files", params={"user_id": "u1"}
    )
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_list_files_still_empty_on_a_generic_supervisor_error(
    external_client, plain_agent, workspace_store_raising_supervisor_error
) -> None:
    """对照组:普通 SandboxSupervisorError(非权限)仍降级成空列表,不是 500 ——
    只有 WorkspacePermissionError 那一支才不许吞。"""
    resp = await external_client.get(
        f"/v1/agents/{plain_agent.code}/workspace/files", params={"user_id": "u1"}
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["files"] == []


# ---------------------------------------------------------------------------
# GET /v1/agents/{agent_code}/workspace/file  (Task 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_returns_bytes_with_safe_headers(external_client, seeded_workspace) -> None:
    resp = await external_client.get(
        f"/v1/agents/{seeded_workspace.agent_code}/workspace/file",
        params={"user_id": seeded_workspace.user_id, "path": "报表.xlsx"},
    )
    assert resp.status_code == 200
    assert resp.content == seeded_workspace.expected_bytes
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    # ``.xlsx`` isn't in any inline whitelist (``_artifact_mime.py``) → the
    # safe default is octet-stream + attachment.
    assert resp.headers["Content-Disposition"].startswith("attachment")


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "..",
        "",
        "a/../../etc/passwd",
        "reports/..",
        "  ",
        "a\x00../../etc/passwd",  # NUL byte alongside a real traversal segment.
    ],
)
@pytest.mark.asyncio
async def test_download_path_traversal_rejected(external_client, seeded_workspace, bad) -> None:
    resp = await external_client.get(
        f"/v1/agents/{seeded_workspace.agent_code}/workspace/file",
        params={"user_id": seeded_workspace.user_id, "path": bad},
    )
    assert resp.status_code == 400, f"{bad!r} 应被拒,实际 {resp.status_code}"


@pytest.mark.asyncio
async def test_download_html_forced_to_attachment(external_client, seeded_workspace) -> None:
    """活动内容必须 attachment —— 否则是存储型 XSS。

    ``RecordingWorkspaceStore.read_file`` ignores ``path`` and always returns
    the same fixture bytes, so this reuses ``seeded_workspace`` (no separate
    "html" fixture needed) and only checks the disposition, which is derived
    purely from the ``path`` extension, not the actual bytes returned.
    """
    resp = await external_client.get(
        f"/v1/agents/{seeded_workspace.agent_code}/workspace/file",
        params={"user_id": seeded_workspace.user_id, "path": "x.html"},
    )
    assert resp.status_code == 200
    assert resp.headers["Content-Disposition"].startswith("attachment")


@pytest.mark.asyncio
async def test_download_scopes_store_call_to_the_requested_user(
    external_client, _ctx: _Ctx, user_store: TenantUserStore
) -> None:
    """跨用户隔离 —— store 的 ``read_file`` 调用必须落在被请求的那个 ``user_id``
    解析出的 ``tenant_user.id`` 上,不是别的用户 / 一个固定值。

    Mirrors ``test_list_files_scopes_store_call_to_the_requested_user``:
    ``RecordingWorkspaceStore`` returns the same bytes for any caller, so the
    response body can't prove isolation — only the store's recorded call
    args can. Hardcoding the download endpoint's identity resolution to one
    fixed user (dropping ``lookup_external_user_id``) makes this assertion
    fail.
    """
    user_a = await user_store.resolve(
        tenant_id=_TENANT_ID, subject_type="user", subject_id=external_subject_id("cust-a")
    )
    user_b = await user_store.resolve(
        tenant_id=_TENANT_ID, subject_type="user", subject_id=external_subject_id("cust-b")
    )
    assert user_a.id != user_b.id
    _ctx.workspace_store.workspace_file = b"whatever"

    resp = await external_client.get(
        f"/v1/agents/{_AGENT_CODE}/workspace/file",
        params={"user_id": "cust-a", "path": "report.txt"},
    )
    assert resp.status_code == 200
    assert _ctx.workspace_store.workspace_reads[-1] == (_TENANT_ID, user_a.id, "report.txt")
    assert _ctx.workspace_store.workspace_reads[-1][1] != user_b.id


@pytest.mark.asyncio
async def test_download_unknown_user_and_missing_file_are_the_same_opaque_404(
    external_client, _ctx: _Ctx, seeded_workspace
) -> None:
    """跨用户(注册表压根没见过的 ``user_id``)与「文件不存在」必须是**同一个**
    不透明 404 —— 第三方不能靠状态码 / 响应体差异探测出「这个用户没建过档」
    还是「建过档但没这份文件」。

    第一个请求走 ``lookup_external_user_id`` 的 ``None`` 分支(mint=False,从
    没见过这个 user_id);第二个请求是已知用户,但把 store 配成对 ``read_file``
    抛 ``SandboxSupervisorError``(真实 supervisor 里"文件不存在"的等价物)。
    两者都必须落在 ``_workspace_file_response`` 里同一句
    ``raise HTTPException(status_code=404, detail="file not found")``。
    """
    unknown_user_resp = await external_client.get(
        f"/v1/agents/{seeded_workspace.agent_code}/workspace/file",
        params={"user_id": "从没见过的用户", "path": "报表.xlsx"},
    )
    _ctx.workspace_store.workspace_file_error = SandboxSupervisorError("missing")
    missing_file_resp = await external_client.get(
        f"/v1/agents/{seeded_workspace.agent_code}/workspace/file",
        params={"user_id": seeded_workspace.user_id, "path": "不存在.txt"},
    )
    assert unknown_user_resp.status_code == missing_file_resp.status_code == 404
    assert unknown_user_resp.json() == missing_file_resp.json()


@pytest.mark.asyncio
async def test_download_requires_read_scope(external_client_no_scope, plain_agent) -> None:
    resp = await external_client_no_scope.get(
        f"/v1/agents/{plain_agent.code}/workspace/file",
        params={"user_id": "u1", "path": "report.txt"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_download_permission_error_is_500_not_404(
    external_client, plain_agent, workspace_store_raising_file_permission_error
) -> None:
    """权限失败必须冒出来 —— 塞进不透明 404 会让用户看到"文件不存在",而它明明
    列在上一屏(与 list 端点的 500 语义一致)。"""
    resp = await external_client.get(
        f"/v1/agents/{plain_agent.code}/workspace/file",
        params={"user_id": "u1", "path": "报表.xlsx"},
    )
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_download_still_404s_on_a_generic_supervisor_error(
    external_client, plain_agent, user_store: TenantUserStore, _ctx: _Ctx
) -> None:
    """对照组:普通 SandboxSupervisorError(非权限)仍是 404,不是 500 —— 只有
    WorkspacePermissionError 那一支才升级。"""
    await user_store.resolve(
        tenant_id=_TENANT_ID, subject_type="user", subject_id=external_subject_id("u1")
    )
    _ctx.workspace_store.workspace_file_error = SandboxSupervisorError("not found")
    resp = await external_client.get(
        f"/v1/agents/{plain_agent.code}/workspace/file",
        params={"user_id": "u1", "path": "报表.xlsx"},
    )
    assert resp.status_code == 404
