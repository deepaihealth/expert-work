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
    """A recognized end-user with one file already sitting in the recorder."""
    user_id = "报表用户"
    await user_store.resolve(
        tenant_id=_TENANT_ID,
        subject_type="user",
        subject_id=external_subject_id(user_id),
    )
    _ctx.workspace_store.workspace_files = [WorkspaceFileEntry(path="报表.xlsx", size=42)]
    return _SeededWorkspace(agent_code=_AGENT_CODE, user_id=user_id)


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
