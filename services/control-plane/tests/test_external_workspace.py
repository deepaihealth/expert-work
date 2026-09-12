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
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.api._external import external_subject_id
from control_plane.api._external_agent_scope import agent_key_for_code
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

    B-50 PR4 —— 存储层的条目现在落在 ``agents/<agent_key>/`` 下(搬迁后的布局),
    对外投影会把这段前缀剥掉,所以响应里仍然是 ``报表.xlsx``。**这个 fixture
    种扁平路径的话整个列表会是空的**,那是正确行为(搬迁前的顶层残留不对外
    投影),但会让所有借这个 fixture 的用例变成在空列表上断言。
    """
    user_id = "报表用户"
    await user_store.resolve(
        tenant_id=_TENANT_ID,
        subject_type="user",
        subject_id=external_subject_id(user_id),
    )
    expected_bytes = b"report body"
    _ctx.workspace_store.workspace_files = [
        WorkspaceFileEntry(path=f"agents/{agent_key_for_code(_AGENT_CODE)}/报表.xlsx", size=42)
    ]
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
        "report\x00.txt",  # Pure NUL, no ``..`` segment — proves the NUL check itself,
        # not the pre-existing ``..`` guard catching it incidentally.
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
    # B-50 PR4 —— 服务端问的是投影后的路径(对接方给的是相对 agent 根的
    # ``report.txt``,存储层是 ``agents/<agent_key>/report.txt``)。
    assert _ctx.workspace_store.workspace_reads[-1] == (
        _TENANT_ID,
        user_a.id,
        f"agents/{agent_key_for_code(_AGENT_CODE)}/report.txt",
    )
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


# ---------------------------------------------------------------------------
# External-API-v1 P2-b minor — ``path`` had no upper bound at all (terminal
# review: a 30004-character ``path`` round-tripped as a 200 straight through
# to the supervisor). 4096 (POSIX ``PATH_MAX``) is added below; see the
# comment on the ``Query(max_length=4096)`` declaration in
# ``external_workspace.py`` for the full rationale.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_path_over_max_length_is_422(external_client, plain_agent) -> None:
    resp = await external_client.get(
        f"/v1/agents/{plain_agent.code}/workspace/file",
        params={"user_id": "u1", "path": "a" * 4097},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_download_path_at_max_length_is_not_rejected(
    external_client, seeded_workspace
) -> None:
    """The boundary itself must still work — 4096 exactly is a legitimate
    (if unusual) path, not a validation error. ``RecordingWorkspaceStore``
    ignores the requested path and always returns ``seeded_workspace``'s
    fixed bytes, so 200 here proves the request reached the handler at all,
    not that this exact path exists."""
    resp = await external_client.get(
        f"/v1/agents/{seeded_workspace.agent_code}/workspace/file",
        params={"user_id": seeded_workspace.user_id, "path": "a" * 4096},
    )
    assert resp.status_code == 200, resp.text
    assert resp.content == seeded_workspace.expected_bytes


# --------------------------------------------------------------------------
# B-50 PR4 —— 按 agent 收口 + 双向路径投影
# --------------------------------------------------------------------------


@dataclass
class _PathAwareWorkspaceStore(RecordingWorkspaceStore):
    """按路径建模的工作区桩。

    ``RecordingWorkspaceStore.read_file`` **不看 path**、恒回同一份
    ``workspace_file`` —— 拿它验「跨 agent 下载 404」会得到一条恒真的断言
    (它永远 200)。投影的判据必须能分辨「问的是哪条路径」,所以这里按一张
    ``path → bytes`` 表回答,问不到就抛 ``SandboxSupervisorError``(真 NAS
    store 在文件不存在时的形态)。
    """

    files: dict[str, bytes] = field(default_factory=dict)

    async def read_file(self, *, tenant_id: UUID, user_id: UUID, path: str) -> bytes:
        self.workspace_reads.append((tenant_id, user_id, path))
        if self.workspace_file_error is not None:
            raise self.workspace_file_error
        try:
            return self.files[path]
        except KeyError as exc:
            raise SandboxSupervisorError(f"no such file: {path}") from exc

    async def list_files(self, *, tenant_id: UUID, user_id: UUID) -> list[WorkspaceFileEntry]:
        self.workspace_reads.append((tenant_id, user_id, ""))
        if self.workspace_list_error is not None:
            raise self.workspace_list_error
        return [WorkspaceFileEntry(path=p, size=len(b)) for p, b in sorted(self.files.items())]


_AGENT_A = "agent-a"
_AGENT_B = "agent-b"
_KEY_A = agent_key_for_code(_AGENT_A)
_KEY_B = agent_key_for_code(_AGENT_B)


@dataclass
class _SeededTwoAgents:
    user_id: str
    internal_user_id: UUID
    store: _PathAwareWorkspaceStore


@pytest.fixture
async def two_agents(_ctx: _Ctx, user_store: TenantUserStore) -> _SeededTwoAgents:
    """搬迁后的布局:两个 agent 各有自己的子树,外加一份 ``shared/`` legacy
    和一份没搬完的顶层扁平残留。"""
    user_id = "双 agent 用户"
    row = await user_store.resolve(
        tenant_id=_TENANT_ID, subject_type="user", subject_id=external_subject_id(user_id)
    )
    store = _PathAwareWorkspaceStore(
        files={
            f"agents/{_KEY_A}/客户案例/x.md": b"a-body",
            f"agents/{_KEY_B}/b-only.md": b"b-body",
            "shared/MEMORY.md": b"legacy-shared",
            "未搬迁.md": b"flat-legacy",
        }
    )
    _ctx.app.state.workspace_store = store  # type: ignore[attr-defined]
    # 两个 agent 都要能被 scope=user 反查成 agent_code —— 反查表来自
    # thread_meta(这个用户跑过哪些 agent),所以得真建两条会话。
    threads = _ctx.app.state.thread_meta_repo  # type: ignore[attr-defined]
    for code in (_AGENT_A, _AGENT_B):
        await threads.create(
            thread_id=uuid4(),
            tenant_id=_TENANT_ID,
            created_by="x",
            user_id=row.id,
            agent_name=code,
        )
    return _SeededTwoAgents(user_id=user_id, internal_user_id=row.id, store=store)


@pytest.mark.asyncio
async def test_list_files_paths_are_relative_to_agent_root(
    external_client, two_agents: _SeededTwoAgents
) -> None:
    """对外 path 不带 ``agents/<key>/`` 前缀 —— 与搬迁前逐字节相同。

    这条是「对接方不用改代码」的唯一保证:他们缓存过的 path 必须继续能用,
    而且带 sha256 后缀的内部 ``agent_key`` 不能漏到第三方面前。
    """
    resp = await external_client.get(
        f"/v1/agents/{_AGENT_A}/workspace/files", params={"user_id": two_agents.user_id}
    )
    assert resp.status_code == 200
    paths = [f["path"] for f in resp.json()["data"]["files"]]
    assert paths == ["客户案例/x.md"]
    assert not any(p.startswith("agents/") for p in paths)
    assert not any(_KEY_A in p for p in paths)


@pytest.mark.asyncio
async def test_list_files_excludes_other_agents_shared_and_flat_legacy(
    external_client, two_agents: _SeededTwoAgents
) -> None:
    """B 的文件、``shared/`` 的 legacy、搬迁前的顶层扁平残留,都不在 A 的列表里。"""
    resp = await external_client.get(
        f"/v1/agents/{_AGENT_A}/workspace/files", params={"user_id": two_agents.user_id}
    )
    paths = {f["path"] for f in resp.json()["data"]["files"]}
    assert "b-only.md" not in paths
    assert not any("MEMORY.md" in p for p in paths)
    assert "未搬迁.md" not in paths


@pytest.mark.asyncio
async def test_download_accepts_the_path_it_handed_out(
    external_client, two_agents: _SeededTwoAgents
) -> None:
    """出口剥前缀、入口加前缀 —— 双向必须闭合。

    只做出口忘了入口,这一条会 404;那就是「列表看着对、下载全挂」的形态。
    """
    listed = (
        await external_client.get(
            f"/v1/agents/{_AGENT_A}/workspace/files", params={"user_id": two_agents.user_id}
        )
    ).json()["data"]["files"][0]["path"]
    resp = await external_client.get(
        f"/v1/agents/{_AGENT_A}/workspace/file",
        params={"user_id": two_agents.user_id, "path": listed},
    )
    assert resp.status_code == 200
    assert resp.content == b"a-body"
    # 服务端真的去问了带前缀的那条路径,不是碰巧桩里有同名文件。
    assert two_agents.store.workspace_reads[-1][2] == f"agents/{_KEY_A}/客户案例/x.md"


@pytest.mark.asyncio
async def test_download_across_agents_is_404(
    external_client, two_agents: _SeededTwoAgents
) -> None:
    """A 的 code 拿 B 的文件路径 → 与「不存在」同一个不透明 404。

    光断言 404 是**恒真**的:收口之前这条路径在桩里也不存在,照样 404。要能
    分开「没收口」和「收口了」两个假设,必须同时断言服务端**问的是哪条路径**
    —— 收口后它问的是 ``agents/<A 的 key>/b-only.md``(A 的根下没这个文件),
    而不是对接方给的裸 ``b-only.md``。
    """
    resp = await external_client.get(
        f"/v1/agents/{_AGENT_A}/workspace/file",
        params={"user_id": two_agents.user_id, "path": "b-only.md"},
    )
    assert resp.status_code == 404
    assert two_agents.store.workspace_reads[-1][2] == f"agents/{_KEY_A}/b-only.md"


@pytest.mark.asyncio
async def test_download_cannot_climb_into_another_agent(
    external_client, two_agents: _SeededTwoAgents
) -> None:
    """投影不是绕过 ``..`` 校验的后门。

    校验必须作用在对接方给的**原串**上、且早于拼前缀:反过来的话
    ``../<B 的 key>/b-only.md`` 会被拼成
    ``agents/<A 的 key>/../<B 的 key>/b-only.md`` —— ``..`` 还在,但已经爬不出
    用户根,于是校验放行,实际读到的是 B 的目录。
    """
    resp = await external_client.get(
        f"/v1/agents/{_AGENT_A}/workspace/file",
        params={"user_id": two_agents.user_id, "path": f"../{_KEY_B}/b-only.md"},
    )
    assert resp.status_code in (400, 404, 422)
    assert resp.content != b"b-body"


@pytest.mark.asyncio
async def test_download_cannot_reach_shared_by_path(
    external_client, two_agents: _SeededTwoAgents
) -> None:
    """``shared/`` 对外不可见 —— 直接拼路径也不行。"""
    resp = await external_client.get(
        f"/v1/agents/{_AGENT_A}/workspace/file",
        params={"user_id": two_agents.user_id, "path": "MEMORY.md"},
    )
    assert resp.status_code == 404
    resp = await external_client.get(
        f"/v1/agents/{_AGENT_A}/workspace/file",
        params={"user_id": two_agents.user_id, "path": "shared/MEMORY.md"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_scope_user_lists_every_agent_with_agent_code(
    external_client, two_agents: _SeededTwoAgents
) -> None:
    """``?scope=user`` 是对接方点名要的并集入口(一个 app 编排两个 agent)。

    条目仍按各自 agent 根给 path(不撞名),另用 ``agent_code`` 标明归属 ——
    下载端点不认 scope,所以不给归属就等于列出一堆下不动的东西。
    """
    resp = await external_client.get(
        f"/v1/agents/{_AGENT_A}/workspace/files",
        params={"user_id": two_agents.user_id, "scope": "user"},
    )
    assert resp.status_code == 200
    files = resp.json()["data"]["files"]
    assert {(f["agent_code"], f["path"]) for f in files} == {
        (_AGENT_A, "客户案例/x.md"),
        (_AGENT_B, "b-only.md"),
    }


@pytest.mark.asyncio
async def test_scope_user_never_leaks_agent_key(
    external_client, two_agents: _SeededTwoAgents
) -> None:
    """归属标识对外只能是 ``agent_code``;``agent_key`` 是内部命名。"""
    resp = await external_client.get(
        f"/v1/agents/{_AGENT_A}/workspace/files",
        params={"user_id": two_agents.user_id, "scope": "user"},
    )
    body = resp.text
    assert _KEY_A not in body and _KEY_B not in body
    assert not any("agent_key" in f for f in resp.json()["data"]["files"])


@pytest.mark.asyncio
async def test_scope_user_still_excludes_shared_and_flat_legacy(
    external_client, two_agents: _SeededTwoAgents
) -> None:
    """并集放开的是「别的 agent」,不是「归属不明」。"""
    resp = await external_client.get(
        f"/v1/agents/{_AGENT_A}/workspace/files",
        params={"user_id": two_agents.user_id, "scope": "user"},
    )
    paths = {f["path"] for f in resp.json()["data"]["files"]}
    assert not any("MEMORY.md" in p for p in paths)
    assert "未搬迁.md" not in paths


@pytest.mark.asyncio
async def test_scope_user_does_not_open_downloads(
    external_client, two_agents: _SeededTwoAgents
) -> None:
    """下载端点不认 ``scope`` —— 传了也不放开跨 agent。

    放开等于让 A 的 code 取到 B 的字节,那正是本设计要挡的。同上一条:只断言
    404 是恒真的,要一并钉住「问的仍然是 A 的根」。
    """
    resp = await external_client.get(
        f"/v1/agents/{_AGENT_A}/workspace/file",
        params={"user_id": two_agents.user_id, "path": "b-only.md", "scope": "user"},
    )
    assert resp.status_code == 404
    assert two_agents.store.workspace_reads[-1][2] == f"agents/{_KEY_A}/b-only.md"


@pytest.mark.asyncio
async def test_scope_defaults_to_agent(external_client, two_agents: _SeededTwoAgents) -> None:
    """不传 ``scope`` = 只看本 agent。默认值错了会静默把隔离整个放开。"""
    resp = await external_client.get(
        f"/v1/agents/{_AGENT_A}/workspace/files", params={"user_id": two_agents.user_id}
    )
    assert [f["path"] for f in resp.json()["data"]["files"]] == ["客户案例/x.md"]


@pytest.mark.asyncio
async def test_invalid_scope_is_422(external_client, two_agents: _SeededTwoAgents) -> None:
    resp = await external_client.get(
        f"/v1/agents/{_AGENT_A}/workspace/files",
        params={"user_id": two_agents.user_id, "scope": "tenant"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_scope_user_drops_agents_with_no_live_code(
    external_client, _ctx: _Ctx, user_store: TenantUserStore
) -> None:
    """反查不到 ``agent_code`` 的子树不进 ``scope=user``。

    agent 被删之后它的文件还在树里,但第三方**没有可用的 code 去下载它** ——
    列出来就是又一个下不动的条目。与 ``shared/`` 不对外投影同一个理由。
    """
    row = await user_store.resolve(
        tenant_id=_TENANT_ID, subject_type="user", subject_id=external_subject_id("孤儿树用户")
    )
    ghost = agent_key_for_code("已删除的 agent")
    store = _PathAwareWorkspaceStore(
        files={f"agents/{_KEY_A}/live.md": b"x", f"agents/{ghost}/ghost.md": b"y"}
    )
    _ctx.app.state.workspace_store = store  # type: ignore[attr-defined]
    threads = _ctx.app.state.thread_meta_repo  # type: ignore[attr-defined]
    await threads.create(
        thread_id=uuid4(),
        tenant_id=_TENANT_ID,
        created_by="x",
        user_id=row.id,
        agent_name=_AGENT_A,
    )
    resp = await external_client.get(
        f"/v1/agents/{_AGENT_A}/workspace/files",
        params={"user_id": "孤儿树用户", "scope": "user"},
    )
    assert [f["path"] for f in resp.json()["data"]["files"]] == ["live.md"]
