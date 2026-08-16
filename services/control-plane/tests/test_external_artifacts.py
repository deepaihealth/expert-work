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

from control_plane.api._external import external_subject_id
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import Settings
from expert_work.persistence import ArtifactStore, TenantUserStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
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


@pytest.fixture
def _ctx() -> _Ctx:
    app = create_app(
        settings=_build_settings(),
        jwt_verifier=build_test_jwt_verifier(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
    )
    return _Ctx(app=app, artifact_store=app.state.artifact_store)  # type: ignore[attr-defined]


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
