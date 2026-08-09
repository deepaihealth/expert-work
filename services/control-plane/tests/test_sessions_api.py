"""End-to-end tests for ``/v1/sessions`` CRUD + lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, TypedDict
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol import ApprovalRecord, AuditPage, AuditQuery
from expert_work.runtime.runs import (
    DisconnectMode,
    InMemoryRunStore,
    RunEventRecord,
    RunInfo,
    RunStatus,
)
from orchestrator.tools import (
    RecordingWorkspaceStore,
    SandboxSupervisorError,
    WorkspaceFileEntry,
    WorkspacePermissionError,
)
from tests.agent_fixtures import stub_agent_runtime
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    grant_system_admin,
    make_test_jwt,
)


class _SeedState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


async def _seed_thread_messages(
    checkpointer: InMemorySaver, thread_id: str, messages: list[BaseMessage]
) -> None:
    """Write one checkpoint holding ``messages`` for ``thread_id`` (mirrors a
    real run leaving a durable checkpoint the backfill can read)."""
    graph = StateGraph(_SeedState)
    graph.add_node("n", lambda _state: {"messages": []})
    graph.add_edge(START, "n")
    seeded = graph.compile(checkpointer=checkpointer)
    await seeded.ainvoke(
        {"messages": messages},
        config={"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
    )


_DEFAULT_TENANT = DEFAULT_DEV_TENANT_ID

_AGENT_YAML = """\
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: code-reviewer
  version: "1.0.0"
  tenant: platform-eng
spec:
  tenant_config: {}
  model:
    provider: anthropic
    name: claude-sonnet-4-5
  system_prompt:
    template: "you are a reviewer"
  sandbox:
    resources: { cpu: "1.0", memory: "1Gi" }
    network:
      egress: proxy
      allowlist: ["api.anthropic.com"]
    filesystem:
      readonly_root: true
      writable: ["/workspace"]
"""


@pytest.fixture
def audit_store() -> InMemoryAuditLogStore:
    return InMemoryAuditLogStore()


@pytest.fixture
async def session_client(audit_store: InMemoryAuditLogStore) -> AsyncIterator[AsyncClient]:
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
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT)}"}
    async with AsyncClient(
        transport=transport,
        base_url="http://control-plane.test",
        headers=headers,
    ) as client:
        # Seed the agent so /v1/sessions can find it.
        await client.post("/v1/agents", json={"manifest_yaml": _AGENT_YAML})
        yield client


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_creates_session_for_known_agent(
    session_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    response = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    assert response.status_code == 201
    meta = response.json()["data"]
    assert meta["agent_name"] == "code-reviewer"
    assert meta["status"] == "active"
    assert meta["thread_id"]
    page = await audit_store.query(AuditQuery(tenant_id=_DEFAULT_TENANT))
    assert any(r.action.value == "session:write" for r in page.entries)


@pytest.mark.asyncio
async def test_post_rejects_unknown_agent(session_client: AsyncClient) -> None:
    response = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "no-such", "agent_version": "9.9.9"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "AGENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_post_rejects_deleted_agent(session_client: AsyncClient) -> None:
    # Soft-delete the seeded agent first.
    await session_client.delete("/v1/agents/code-reviewer/1.0.0")
    response = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "AGENT_NOT_FOUND"


# ---------------------------------------------------------------------------
# Stream R (R-9) — tenant default agent + latest-version resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_resolves_latest_version_when_omitted(session_client: AsyncClient) -> None:
    # Only name given → latest ACTIVE version of that agent is used.
    response = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer"},
    )
    assert response.status_code == 201, response.text
    meta = response.json()["data"]
    assert meta["agent_name"] == "code-reviewer"
    assert meta["agent_version"] == "1.0.0"


@pytest.mark.asyncio
async def test_post_uses_tenant_default_when_name_omitted(session_client: AsyncClient) -> None:
    # Point the tenant default at the seeded agent, then create with no agent.
    await session_client.put(
        f"/v1/tenants/{_DEFAULT_TENANT}/config",
        json={"display_name": "Dev Tenant", "default_agent_name": "code-reviewer"},
    )
    response = await session_client.post("/v1/sessions", json={})
    assert response.status_code == 201, response.text
    assert response.json()["data"]["agent_name"] == "code-reviewer"


@pytest.mark.asyncio
async def test_post_no_agent_no_default_falls_back_to_canonical(
    session_client: AsyncClient,
) -> None:
    # No name, no tenant default → platform fallback ``canonical-agent``,
    # which isn't registered here → AGENT_NOT_FOUND (not a 500).
    response = await session_client.post("/v1/sessions", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "AGENT_NOT_FOUND"


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_single_session(session_client: AsyncClient) -> None:
    create = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    thread_id = create.json()["data"]["thread_id"]
    response = await session_client.get(f"/v1/sessions/{thread_id}")
    assert response.status_code == 200
    assert response.json()["data"]["thread_id"] == thread_id


@pytest.mark.asyncio
async def test_get_session_workspace_read_only_null(session_client: AsyncClient) -> None:
    # Playground-Uplift D4 — read-only inspector. A fresh thread has no
    # provisioned workspace, so it truthfully reports null (no VM started yet).
    create = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    thread_id = create.json()["data"]["thread_id"]
    response = await session_client.get(f"/v1/sessions/{thread_id}/workspace")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["workspace"] is None
    assert data["artifacts"] == []


@pytest.mark.asyncio
async def test_get_session_workspace_404_for_unknown(session_client: AsyncClient) -> None:
    response = await session_client.get(
        "/v1/sessions/00000000-0000-0000-0000-000000000099/workspace"
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_workspace_files_empty_without_supervisor(session_client: AsyncClient) -> None:
    # No supervisor wired in the test app → the browse endpoint degrades to an
    # empty list rather than erroring the inspector.
    create = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    thread_id = create.json()["data"]["thread_id"]
    response = await session_client.get(f"/v1/sessions/{thread_id}/workspace/files")
    assert response.status_code == 200
    assert response.json()["data"]["files"] == []


@pytest.mark.asyncio
async def test_workspace_file_download_rejects_path_traversal(session_client: AsyncClient) -> None:
    create = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    thread_id = create.json()["data"]["thread_id"]
    response = await session_client.get(
        f"/v1/sessions/{thread_id}/workspace/file",
        params={"path": "../../etc/passwd"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_workspace_file_download_404_without_supervisor(session_client: AsyncClient) -> None:
    create = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    thread_id = create.json()["data"]["thread_id"]
    response = await session_client.get(
        f"/v1/sessions/{thread_id}/workspace/file", params={"path": "report.pdf"}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_workspace_files_and_download_with_supervisor(
    audit_store: InMemoryAuditLogStore,
) -> None:
    # Inject a recording supervisor so the browse + download paths run end to end.
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
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT)}"}
    async with AsyncClient(
        transport=transport, base_url="http://control-plane.test", headers=headers
    ) as client:
        await client.post("/v1/agents", json={"manifest_yaml": _AGENT_YAML})
        # Override the lifespan-set (None) client with a recording one.
        app.state.workspace_store = RecordingWorkspaceStore(
            workspace_files=[WorkspaceFileEntry(path="report.pdf", size=2048)],
            workspace_file=b"%PDF-1.4 hello",
        )
        create = await client.post(
            "/v1/sessions",
            json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
        )
        thread_id = create.json()["data"]["thread_id"]

        listing = await client.get(f"/v1/sessions/{thread_id}/workspace/files")
        assert listing.status_code == 200
        files = listing.json()["data"]["files"]
        assert files == [{"path": "report.pdf", "size": 2048}]

        download = await client.get(
            f"/v1/sessions/{thread_id}/workspace/file", params={"path": "report.pdf"}
        )
        assert download.status_code == 200
        assert download.content == b"%PDF-1.4 hello"
        assert "attachment" in download.headers["content-disposition"]
        assert "report.pdf" in download.headers["content-disposition"]

        # Delete proxies the path through to the supervisor (thread-scoped).
        deleted = await client.request(
            "DELETE", f"/v1/sessions/{thread_id}/workspace/file", params={"path": "report.pdf"}
        )
        assert deleted.status_code == 200
        assert deleted.json()["data"]["deleted"] == "report.pdf"
        store = app.state.workspace_store
        assert [d[2] for d in store.workspace_deletes] == ["report.pdf"]

        # A traversal path is rejected before reaching the supervisor.
        bad = await client.request(
            "DELETE", f"/v1/sessions/{thread_id}/workspace/file", params={"path": "../etc/passwd"}
        )
        assert bad.status_code == 400
        assert [d[2] for d in store.workspace_deletes] == ["report.pdf"]


# ---------------------------------------------------------------------------
# W2-BUG-1 — thread-scoped workspace endpoints: permission failures must not
# be laundered into 404 / an empty list. Mirrors ``test_workspace_api.py``'s
# coverage of the user-scoped ``/v1/workspace*`` router one-for-one.
# ---------------------------------------------------------------------------

# A store-side ``WorkspacePermissionError`` message shaped like the real one
# (path / uid / mode) — used to prove the HTTP layer never echoes it back.
_LEAKY_DETAIL = (
    "PermissionError(13, 'Permission denied'): "
    "'/mnt/workspaces/t-1/u-1/report.pdf' uid=10002 mode=0o600"
)


@pytest.fixture
async def session_client_with_store(
    audit_store: InMemoryAuditLogStore,
) -> AsyncIterator[tuple[AsyncClient, RecordingWorkspaceStore, str]]:
    """A live thread + an injected supervisor, for the workspace-file tests below."""
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
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT)}"}
    async with AsyncClient(
        transport=transport, base_url="http://control-plane.test", headers=headers
    ) as client:
        await client.post("/v1/agents", json={"manifest_yaml": _AGENT_YAML})
        store = RecordingWorkspaceStore()
        app.state.workspace_store = store
        create = await client.post(
            "/v1/sessions",
            json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
        )
        thread_id = create.json()["data"]["thread_id"]
        yield client, store, thread_id


@pytest.mark.asyncio
async def test_session_workspace_list_reports_permission_denied_as_server_error(
    session_client_with_store: tuple[AsyncClient, RecordingWorkspaceStore, str],
) -> None:
    """store 抛 WorkspacePermissionError → 500,不是 200 空列表(见 workspace.py 同款)。"""
    client, store, thread_id = session_client_with_store
    store.workspace_list_error = WorkspacePermissionError(_LEAKY_DETAIL)
    resp = await client.get(f"/v1/sessions/{thread_id}/workspace/files")
    assert resp.status_code == 500, resp.text
    assert "/mnt/workspaces" not in resp.text
    assert "10002" not in resp.text
    assert "0o600" not in resp.text


@pytest.mark.asyncio
async def test_session_workspace_list_still_empty_on_a_generic_supervisor_error(
    session_client_with_store: tuple[AsyncClient, RecordingWorkspaceStore, str],
) -> None:
    """对照组:普通 SandboxSupervisorError(非权限)仍降级成空列表,不是 500。"""
    client, store, thread_id = session_client_with_store
    store.workspace_list_error = SandboxSupervisorError("supervisor unreachable")
    resp = await client.get(f"/v1/sessions/{thread_id}/workspace/files")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["files"] == []


@pytest.mark.asyncio
async def test_session_workspace_download_reports_server_error_on_permission_denied(
    session_client_with_store: tuple[AsyncClient, RecordingWorkspaceStore, str],
) -> None:
    """store 抛 WorkspacePermissionError → 500,不是 404。

    404 的语义是"不存在 / 你不该知道它存在";权限读不动是**服务端配置问题**,
    塞进 404 会让用户看到"文件不存在"而它明明列在上一屏 —— W2-BUG-1 的诊断
    成本几乎全在这里。响应体不含路径 / uid / mode。
    """
    client, store, thread_id = session_client_with_store
    store.workspace_file_error = WorkspacePermissionError(_LEAKY_DETAIL)
    resp = await client.get(
        f"/v1/sessions/{thread_id}/workspace/file", params={"path": "report.pdf"}
    )
    assert resp.status_code == 500, resp.text
    assert "/mnt/workspaces" not in resp.text
    assert "10002" not in resp.text
    assert "0o600" not in resp.text


@pytest.mark.asyncio
async def test_session_workspace_download_still_404s_on_a_missing_file(
    session_client_with_store: tuple[AsyncClient, RecordingWorkspaceStore, str],
) -> None:
    """对照组:普通 SandboxSupervisorError 仍是 404。

    防止把"隐藏跨用户存在性"这条既有安全姿态一并改坏。
    """
    client, store, thread_id = session_client_with_store
    store.workspace_file_error = SandboxSupervisorError("not found")
    resp = await client.get(
        f"/v1/sessions/{thread_id}/workspace/file", params={"path": "report.pdf"}
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_session_workspace_delete_reports_server_error_on_permission_denied(
    session_client_with_store: tuple[AsyncClient, RecordingWorkspaceStore, str],
) -> None:
    """同下载:权限失败是服务端配置问题,不应该被 404 盖过去。响应体不含路径/uid/mode。"""
    client, store, thread_id = session_client_with_store
    store.workspace_delete_error = WorkspacePermissionError(_LEAKY_DETAIL)
    resp = await client.request(
        "DELETE", f"/v1/sessions/{thread_id}/workspace/file", params={"path": "report.pdf"}
    )
    assert resp.status_code == 500, resp.text
    assert "/mnt/workspaces" not in resp.text
    assert "10002" not in resp.text
    assert "0o600" not in resp.text


@pytest.mark.asyncio
async def test_session_workspace_delete_still_404s_on_a_generic_supervisor_error(
    session_client_with_store: tuple[AsyncClient, RecordingWorkspaceStore, str],
) -> None:
    """对照组:普通 SandboxSupervisorError(非权限)仍是 404,既有姿态不变。"""
    client, store, thread_id = session_client_with_store
    store.workspace_delete_error = SandboxSupervisorError("not found")
    resp = await client.request(
        "DELETE", f"/v1/sessions/{thread_id}/workspace/file", params={"path": "report.pdf"}
    )
    assert resp.status_code == 404, resp.text


async def _seed_artifact_for_thread(
    client: AsyncClient, thread_id: str, *, name: str = "report.pdf"
) -> None:
    """Save an artifact version row so ``.../workspace/artifacts/{name}/download``
    resolves — mirrors ``test_session_workspace_file_and_artifact_download_system_admin_200``'s
    seeding, factored out since the two tests below need it too."""
    app = client._transport.app  # type: ignore[attr-defined,union-attr]
    meta = await app.state.thread_meta_repo.get(UUID(thread_id), tenant_id=_DEFAULT_TENANT)
    assert meta is not None and meta.user_id is not None
    await app.state.artifact_store.save_version(
        tenant_id=_DEFAULT_TENANT,
        user_id=meta.user_id,
        name=name,
        kind="document",
        path_in_workspace=name,
        created_in_thread=thread_id,
    )


@pytest.mark.asyncio
async def test_session_artifact_download_reports_server_error_on_permission_denied(
    session_client_with_store: tuple[AsyncClient, RecordingWorkspaceStore, str],
) -> None:
    """store 抛 WorkspacePermissionError → 500,不是 404。

    元数据行(artifact 版本)存在,内容读不动是权限问题(服务端配置),不是
    "这个 artifact 不存在"——``artifacts`` 的内容其实就是同一个 workspace_store
    上的一个文件,"gone / unreadable" 合并成一个 404 正是这整个 program 要拆
    开的那类混淆(复审第二轮找到的第四/五处站点之一)。响应体不含路径/uid/mode。
    """
    client, store, thread_id = session_client_with_store
    await _seed_artifact_for_thread(client, thread_id)
    store.workspace_file_error = WorkspacePermissionError(_LEAKY_DETAIL)
    resp = await client.get(f"/v1/sessions/{thread_id}/workspace/artifacts/report.pdf/download")
    assert resp.status_code == 500, resp.text
    assert "/mnt/workspaces" not in resp.text
    assert "10002" not in resp.text
    assert "0o600" not in resp.text


@pytest.mark.asyncio
async def test_session_artifact_download_still_404s_on_a_generic_supervisor_error(
    session_client_with_store: tuple[AsyncClient, RecordingWorkspaceStore, str],
) -> None:
    """对照组:普通 SandboxSupervisorError(真的读不到内容)仍是 404,既有姿态不变。"""
    client, store, thread_id = session_client_with_store
    await _seed_artifact_for_thread(client, thread_id)
    store.workspace_file_error = SandboxSupervisorError("content gone")
    resp = await client.get(f"/v1/sessions/{thread_id}/workspace/artifacts/report.pdf/download")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_get_returns_404_for_unknown(session_client: AsyncClient) -> None:
    response = await session_client.get("/v1/sessions/00000000-0000-0000-0000-000000000099")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_filters_by_status(session_client: AsyncClient) -> None:
    for _ in range(3):
        await session_client.post(
            "/v1/sessions",
            json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
        )
    response = await session_client.get("/v1/sessions?status=active")
    assert response.status_code == 200
    assert response.json()["data"]["total"] == 3


# ---------------------------------------------------------------------------
# pause / resume / cancel state machine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_then_resume(session_client: AsyncClient) -> None:
    create = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    tid = create.json()["data"]["thread_id"]
    pause = await session_client.post(f"/v1/sessions/{tid}:pause", json={})
    assert pause.status_code == 200
    assert pause.json()["data"]["status"] == "paused"

    resume = await session_client.post(f"/v1/sessions/{tid}:resume", json={"reason": "ack"})
    assert resume.status_code == 200
    assert resume.json()["data"]["status"] == "active"


@pytest.mark.asyncio
async def test_resume_from_active_returns_409(session_client: AsyncClient) -> None:
    create = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    tid = create.json()["data"]["thread_id"]
    response = await session_client.post(f"/v1/sessions/{tid}:resume", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SESSION_STATE_CONFLICT"


@pytest.mark.asyncio
async def test_pause_terminal_session_returns_409(session_client: AsyncClient) -> None:
    create = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    tid = create.json()["data"]["thread_id"]
    await session_client.post(f"/v1/sessions/{tid}:cancel", json={})
    response = await session_client.post(f"/v1/sessions/{tid}:pause", json={})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_cancel_emits_session_cancel_audit(
    session_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    create = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    tid = create.json()["data"]["thread_id"]
    response = await session_client.post(
        f"/v1/sessions/{tid}:cancel", json={"reason": "user_abort"}
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "cancelled"

    page = await audit_store.query(AuditQuery(tenant_id=_DEFAULT_TENANT))
    assert any(r.action.value == "session:cancel" and r.resource_id == tid for r in page.entries)


@pytest.mark.asyncio
async def test_cancel_paused_session_succeeds(session_client: AsyncClient) -> None:
    create = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    tid = create.json()["data"]["thread_id"]
    await session_client.post(f"/v1/sessions/{tid}:pause", json={})
    response = await session_client.post(f"/v1/sessions/{tid}:cancel", json={})
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_transition_404_for_unknown_thread(session_client: AsyncClient) -> None:
    response = await session_client.post(
        "/v1/sessions/00000000-0000-0000-0000-000000000099:pause",
        json={},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# tenant isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_other_tenant_cannot_see_session(session_client: AsyncClient) -> None:
    from uuid import UUID

    create = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    tid = create.json()["data"]["thread_id"]
    other_tenant = UUID("11111111-1111-1111-1111-111111111111")
    other_jwt = make_test_jwt(tenant_id=other_tenant)
    response = await session_client.get(
        f"/v1/sessions/{tid}",
        headers={"Authorization": f"Bearer {other_jwt}"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# per-user isolation — Stream J.14
# ---------------------------------------------------------------------------


def _user_headers(subject: str) -> dict[str, str]:
    """Bearer headers for a non-admin user in the default tenant."""
    jwt = make_test_jwt(tenant_id=_DEFAULT_TENANT, subject=subject, roles=("viewer",))
    return {"Authorization": f"Bearer {jwt}"}


async def _create_as(client: AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
        headers=headers,
    )
    assert response.status_code == 201
    return str(response.json()["data"]["thread_id"])


@pytest.mark.asyncio
async def test_create_stamps_owning_user(session_client: AsyncClient) -> None:
    response = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
        headers=_user_headers("user-a"),
    )
    assert response.status_code == 201
    assert response.json()["data"]["user_id"] is not None


@pytest.mark.asyncio
async def test_user_cannot_see_another_users_session(session_client: AsyncClient) -> None:
    tid = await _create_as(session_client, _user_headers("user-a"))
    # The owner sees it.
    own = await session_client.get(f"/v1/sessions/{tid}", headers=_user_headers("user-a"))
    assert own.status_code == 200
    # A different user in the same tenant gets 404 — existence stays hidden.
    other = await session_client.get(f"/v1/sessions/{tid}", headers=_user_headers("user-b"))
    assert other.status_code == 404


@pytest.mark.asyncio
async def test_admin_can_see_any_users_session(session_client: AsyncClient) -> None:
    tid = await _create_as(session_client, _user_headers("user-a"))
    # The default fixture headers are an admin JWT — tenant-wide access.
    response = await session_client.get(f"/v1/sessions/{tid}")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_list_scopes_to_caller_user(session_client: AsyncClient) -> None:
    await _create_as(session_client, _user_headers("user-a"))
    await _create_as(session_client, _user_headers("user-a"))
    await _create_as(session_client, _user_headers("user-b"))

    a_list = await session_client.get("/v1/sessions", headers=_user_headers("user-a"))
    assert a_list.json()["data"]["total"] == 2
    b_list = await session_client.get("/v1/sessions", headers=_user_headers("user-b"))
    assert b_list.json()["data"]["total"] == 1
    # Admin, too, sees only their own threads — they created none here.
    admin_list = await session_client.get("/v1/sessions")
    assert admin_list.json()["data"]["total"] == 0


@pytest.mark.asyncio
async def test_user_cannot_transition_another_users_session(
    session_client: AsyncClient,
) -> None:
    tid = await _create_as(session_client, _user_headers("user-a"))
    intruder = await session_client.post(
        f"/v1/sessions/{tid}:pause", json={}, headers=_user_headers("user-b")
    )
    assert intruder.status_code == 404
    owner = await session_client.post(
        f"/v1/sessions/{tid}:pause", json={}, headers=_user_headers("user-a")
    )
    assert owner.status_code == 200


@pytest.mark.asyncio
async def test_machine_principal_session_is_unowned(session_client: AsyncClient) -> None:
    """A service-account caller has no per-user instance — its threads
    carry no ``user_id`` and stay tenant-scoped (legacy behaviour)."""
    sa_jwt = make_test_jwt(
        tenant_id=_DEFAULT_TENANT,
        subject="sa-1",
        sub_type="service_account",
        roles=("admin",),
    )
    sa_headers = {"Authorization": f"Bearer {sa_jwt}"}
    tid = await _create_as(session_client, sa_headers)
    create_meta = await session_client.get(f"/v1/sessions/{tid}", headers=sa_headers)
    assert create_meta.json()["data"]["user_id"] is None
    # A plain user can still read an unowned thread.
    plain = await session_client.get(f"/v1/sessions/{tid}", headers=_user_headers("user-a"))
    assert plain.status_code == 200


# ---------------------------------------------------------------------------
# Session-history uplift — rename / search / archive / purge
# ---------------------------------------------------------------------------


async def _create(session_client: AsyncClient) -> str:
    response = await session_client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    assert response.status_code == 201
    return str(response.json()["data"]["thread_id"])


@pytest.mark.asyncio
async def test_rename_sets_title(
    session_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    tid = await _create(session_client)
    resp = await session_client.patch(f"/v1/sessions/{tid}", json={"title": "  季度报告  "})
    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "季度报告"  # trimmed

    fetched = await session_client.get(f"/v1/sessions/{tid}")
    assert fetched.json()["data"]["title"] == "季度报告"

    page = await audit_store.query(AuditQuery(tenant_id=_DEFAULT_TENANT))
    assert any(r.action.value == "session:write" and r.resource_id == tid for r in page.entries)


@pytest.mark.asyncio
async def test_rename_rejects_blank_title(session_client: AsyncClient) -> None:
    tid = await _create(session_client)
    # Whitespace-only passes min_length=1 but strips to empty → 422.
    resp = await session_client.patch(f"/v1/sessions/{tid}", json={"title": "   "})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_rename_404_for_unknown(session_client: AsyncClient) -> None:
    resp = await session_client.patch(
        "/v1/sessions/00000000-0000-0000-0000-000000000099", json={"title": "x"}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_backfills_title_from_checkpoint() -> None:
    # Pre-existing threads (created before auto-titling) have a NULL title; the
    # list backfills it from the checkpoint's first user message + persists.
    settings = Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )
    run_store = InMemoryRunStore()
    checkpointer = InMemorySaver()
    runtime = stub_agent_runtime(run_store=run_store)
    runtime.durable_checkpointer = checkpointer
    app = create_app(
        settings=settings,
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
        agent_runtime=runtime,
        run_repo=run_store,
    )
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT)}"}
    async with AsyncClient(
        transport=transport, base_url="http://control-plane.test", headers=headers
    ) as client:
        await client.post("/v1/agents", json={"manifest_yaml": _AGENT_YAML})
        tid = await _create(client)
        # Freshly created — no title.
        pre = await client.get(f"/v1/sessions/{tid}")
        assert pre.json()["data"]["title"] is None

        await _seed_thread_messages(checkpointer, tid, [HumanMessage(content="帮我写季度经营报告")])

        listed = await client.get("/v1/sessions")
        row = next(m for m in listed.json()["data"]["items"] if m["thread_id"] == tid)
        assert row["title"] == "帮我写季度经营报告"

        # Persisted — a follow-up single GET now carries the title too.
        after = await client.get(f"/v1/sessions/{tid}")
        assert after.json()["data"]["title"] == "帮我写季度经营报告"


@pytest.mark.asyncio
async def test_list_q_filters_by_title(session_client: AsyncClient) -> None:
    a = await _create(session_client)
    b = await _create(session_client)
    await session_client.patch(f"/v1/sessions/{a}", json={"title": "Quarterly Report"})
    await session_client.patch(f"/v1/sessions/{b}", json={"title": "今天天气"})

    hit = await session_client.get("/v1/sessions?q=report")
    ids = [m["thread_id"] for m in hit.json()["data"]["items"]]
    assert ids == [a]

    zh = await session_client.get("/v1/sessions?q=天气")
    assert [m["thread_id"] for m in zh.json()["data"]["items"]] == [b]

    none = await session_client.get("/v1/sessions?q=zzz")
    assert none.json()["data"]["items"] == []


@pytest.mark.asyncio
async def test_list_filters_by_agent_name(session_client: AsyncClient) -> None:
    a = await _create(session_client)
    matched = await session_client.get("/v1/sessions?agent_name=code-reviewer")
    assert a in {m["thread_id"] for m in matched.json()["data"]["items"]}

    other = await session_client.get("/v1/sessions?agent_name=nonexistent-agent")
    assert other.json()["data"]["items"] == []


@pytest.mark.asyncio
async def test_archive_hides_from_list_but_stays_reachable(
    session_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    keep = await _create(session_client)
    drop = await _create(session_client)

    resp = await session_client.delete(f"/v1/sessions/{drop}")
    assert resp.status_code == 200
    assert resp.json()["data"]["archived"] == drop

    default = await session_client.get("/v1/sessions")
    default_ids = {m["thread_id"] for m in default.json()["data"]["items"]}
    assert keep in default_ids
    assert drop not in default_ids

    with_archived = await session_client.get("/v1/sessions?include_archived=true")
    all_ids = {m["thread_id"] for m in with_archived.json()["data"]["items"]}
    assert {keep, drop} <= all_ids

    # A direct GET still resolves the archived thread (soft, reversible).
    still = await session_client.get(f"/v1/sessions/{drop}")
    assert still.status_code == 200
    assert still.json()["data"]["status"] == "archived"

    page = await audit_store.query(AuditQuery(tenant_id=_DEFAULT_TENANT))
    assert any(r.action.value == "session:write" and r.resource_id == drop for r in page.entries)


@pytest.mark.asyncio
async def test_archive_404_for_unknown(session_client: AsyncClient) -> None:
    resp = await session_client.delete("/v1/sessions/00000000-0000-0000-0000-000000000099")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_non_owner_cannot_rename_or_archive(session_client: AsyncClient) -> None:
    # Owned by user-a; user-b (a different plain user) must get 404 on both.
    tid = await _create_as(session_client, _user_headers("user-a"))
    b = _user_headers("user-b")
    rename = await session_client.patch(f"/v1/sessions/{tid}", json={"title": "x"}, headers=b)
    assert rename.status_code == 404
    archive = await session_client.delete(f"/v1/sessions/{tid}", headers=b)
    assert archive.status_code == 404


# ---------------------------------------------------------------------------
# purge — hard-delete cascade (deletion-hygiene PR3)
# ---------------------------------------------------------------------------


def _run_info(thread_id: str) -> RunInfo:
    now = datetime.now(UTC)
    return RunInfo(
        run_id=uuid4(),
        tenant_id=_DEFAULT_TENANT,
        thread_id=UUID(thread_id),
        user_id=None,
        status=RunStatus.SUCCESS,
        on_disconnect=DisconnectMode.CANCEL,
        is_resume=False,
        error=None,
        created_at=now,
        updated_at=now,
        finished_at=now,
    )


def _pending_approval(thread_id: str, run_id: UUID) -> ApprovalRecord:
    now = datetime.now(UTC)
    return ApprovalRecord(
        id=uuid4(),
        tenant_id=_DEFAULT_TENANT,
        run_id=run_id,
        thread_id=UUID(thread_id),
        request_id="approval:purge-seed",
        node="tools",
        reason_kind="policy_gate",
        action_summary="approval-gated tool 'send_email'",
        proposed_args={"to": "ops@example.com"},
        requested_at=now,
        timeout_at=now + timedelta(hours=24),
    )


def _purge_audit_details(page: AuditPage, tid: str) -> dict[str, object]:
    return next(
        r.details for r in page.entries if r.resource_id == tid and r.details.get("op") == "purge"
    )


@pytest.mark.asyncio
async def test_purge_cascades_runs_events_and_approvals(
    session_client: AsyncClient, audit_store: InMemoryAuditLogStore
) -> None:
    """Purge removes the run rows, their run_event children AND the thread's
    ``agent_approval`` rows — no orphans left behind."""
    tid = await _create(session_client)
    app = session_client._transport.app  # type: ignore[attr-defined,union-attr]
    info = _run_info(tid)
    await app.state.run_store.create(info)
    now = datetime.now(UTC)
    await app.state.run_event_store.append(
        RunEventRecord(
            run_id=info.run_id,
            seq=0,
            event_name="run_started",
            data={"status": "running"},
            created_at_ms=int(now.timestamp() * 1000),
            created_at=now,
        )
    )
    await app.state.approval_store.create(_pending_approval(tid, info.run_id))

    resp = await session_client.post(f"/v1/sessions/{tid}:purge")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["purged"] == tid
    assert data["runs"] == 1
    assert data["approvals"] == 1

    # Store level — every thread-scoped row is gone.
    assert await app.state.run_store.get(run_id=info.run_id, tenant_id=_DEFAULT_TENANT) is None
    assert list(await app.state.run_event_store.list(run_id=info.run_id)) == []
    assert (
        await app.state.approval_store.get_by_run(run_id=info.run_id, tenant_id=_DEFAULT_TENANT)
        is None
    )

    # HTTP level — the session 404s and the runs index no longer knows the thread.
    assert (await session_client.get(f"/v1/sessions/{tid}")).status_code == 404
    runs_page = await session_client.get(f"/v1/runs?q={tid}")
    assert runs_page.status_code == 200
    assert runs_page.json()["data"]["items"] == []

    page = await audit_store.query(AuditQuery(tenant_id=_DEFAULT_TENANT))
    details = _purge_audit_details(page, tid)
    assert details["meta_removed"] is True
    assert details["runs"] == 1
    assert details["approvals"] == 1


@pytest.mark.asyncio
async def test_purge_run_delete_failure_is_audit_visible(
    session_client: AsyncClient,
    audit_store: InMemoryAuditLogStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed run cascade stays best-effort (200) but the failure is
    flagged in both the response data and the audit details."""
    tid = await _create(session_client)
    app = session_client._transport.app  # type: ignore[attr-defined,union-attr]

    async def _boom(thread_id: object, *, tenant_id: object) -> int:
        raise RuntimeError("store down")

    monkeypatch.setattr(app.state.agent_runtime.run_manager, "delete_by_thread", _boom)

    resp = await session_client.post(f"/v1/sessions/{tid}:purge")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["runs"] == 0
    assert body["data"]["runs_delete_failed"] is True

    # Best-effort — the thread_meta row is still removed last.
    assert (await session_client.get(f"/v1/sessions/{tid}")).status_code == 404

    page = await audit_store.query(AuditQuery(tenant_id=_DEFAULT_TENANT))
    details = _purge_audit_details(page, tid)
    assert details["runs_delete_failed"] is True
    assert details["meta_removed"] is True


@pytest.mark.asyncio
async def test_purge_approval_delete_failure_is_audit_visible(
    session_client: AsyncClient,
    audit_store: InMemoryAuditLogStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed approval cascade is equally audit-visible."""
    tid = await _create(session_client)
    app = session_client._transport.app  # type: ignore[attr-defined,union-attr]

    async def _boom(*, thread_ids: object, tenant_id: object) -> int:
        raise RuntimeError("store down")

    monkeypatch.setattr(app.state.approval_store, "delete_for_threads", _boom)

    resp = await session_client.post(f"/v1/sessions/{tid}:purge")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["approvals"] == 0
    assert data["approvals_delete_failed"] is True

    page = await audit_store.query(AuditQuery(tenant_id=_DEFAULT_TENANT))
    details = _purge_audit_details(page, tid)
    assert details["approvals_delete_failed"] is True
    assert details["meta_removed"] is True


@pytest.mark.asyncio
async def test_repeat_purge_returns_404(session_client: AsyncClient) -> None:
    """Purge is idempotent at the HTTP surface: the first call removes the
    thread_meta row, so the ownership gate hides the thread from a second
    call (404 — current ``_load_owned_session`` semantics)."""
    tid = await _create(session_client)
    first = await session_client.post(f"/v1/sessions/{tid}:purge")
    assert first.status_code == 200
    assert first.json()["data"]["purged"] == tid

    second = await session_client.post(f"/v1/sessions/{tid}:purge")
    assert second.status_code == 404


# ---------------------------------------------------------------------------
# W2 — session 详情读端点接跨租户 scope(系统管理员租户切换器)
#
# 三件套 per endpoint:system_admin 带目标租户 tenant_id → 200;普通租户
# 用户带他租户 tenant_id → 403 TENANT_NOT_ALLOWED;tenant_id=* → 400
# SCOPE_ALL_NOT_SUPPORTED。
# ---------------------------------------------------------------------------


#: (name, path-suffix under /v1/sessions/{thread_id}, extra query params)
_SESSION_SCOPE_ENDPOINTS: list[tuple[str, str, dict[str, str]]] = [
    ("get_session", "", {}),
    ("workspace", "/workspace", {}),
    ("workspace_files", "/workspace/files", {}),
    ("workspace_file", "/workspace/file", {"path": "report.pdf"}),
    ("artifact_download", "/workspace/artifacts/report.pdf/download", {}),
]


async def _create_owned_session(client: AsyncClient) -> str:
    create = await client.post(
        "/v1/sessions",
        json={"agent_name": "code-reviewer", "agent_version": "1.0.0"},
    )
    assert create.status_code == 201, create.text
    return str(create.json()["data"]["thread_id"])


@pytest.mark.asyncio
async def test_get_session_system_admin_target_tenant_200(session_client: AsyncClient) -> None:
    thread_id = await _create_owned_session(session_client)
    headers = await grant_system_admin(session_client)
    resp = await session_client.get(
        f"/v1/sessions/{thread_id}",
        params={"tenant_id": str(_DEFAULT_TENANT)},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["thread_id"] == thread_id


@pytest.mark.asyncio
async def test_session_workspace_system_admin_target_tenant_200(
    session_client: AsyncClient,
) -> None:
    thread_id = await _create_owned_session(session_client)
    headers = await grant_system_admin(session_client)
    resp = await session_client.get(
        f"/v1/sessions/{thread_id}/workspace",
        params={"tenant_id": str(_DEFAULT_TENANT)},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["workspace"] is None  # no VM ever started — truthful null
    assert data["artifacts"] == []


@pytest.mark.asyncio
async def test_session_workspace_files_system_admin_target_tenant_200(
    session_client: AsyncClient,
) -> None:
    thread_id = await _create_owned_session(session_client)
    headers = await grant_system_admin(session_client)
    resp = await session_client.get(
        f"/v1/sessions/{thread_id}/workspace/files",
        params={"tenant_id": str(_DEFAULT_TENANT)},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["files"] == []  # no supervisor wired → degrade


@pytest.mark.asyncio
async def test_session_workspace_file_and_artifact_download_system_admin_200(
    audit_store: InMemoryAuditLogStore,
) -> None:
    """The two byte-download endpoints, end to end with a recording
    supervisor: a system_admin homed elsewhere reads the target tenant
    thread's workspace file and artifact via ``?tenant_id=``."""
    from orchestrator.tools import RecordingWorkspaceStore, WorkspaceFileEntry

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
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_DEFAULT_TENANT)}"}
    async with AsyncClient(
        transport=transport, base_url="http://control-plane.test", headers=headers
    ) as client:
        await client.post("/v1/agents", json={"manifest_yaml": _AGENT_YAML})
        app.state.workspace_store = RecordingWorkspaceStore(
            workspace_files=[WorkspaceFileEntry(path="report.pdf", size=2048)],
            workspace_file=b"%PDF-1.4 hello",
        )
        thread_id = await _create_owned_session(client)
        # The thread owner's artifact row, so the by-name download resolves.
        meta = await app.state.thread_meta_repo.get(UUID(thread_id), tenant_id=_DEFAULT_TENANT)
        assert meta is not None and meta.user_id is not None
        await app.state.artifact_store.save_version(
            tenant_id=_DEFAULT_TENANT,
            user_id=meta.user_id,
            name="report.pdf",
            kind="document",
            path_in_workspace="report.pdf",
            created_in_thread=thread_id,
        )

        sys_headers = await grant_system_admin(client)
        file_resp = await client.get(
            f"/v1/sessions/{thread_id}/workspace/file",
            params={"path": "report.pdf", "tenant_id": str(_DEFAULT_TENANT)},
            headers=sys_headers,
        )
        assert file_resp.status_code == 200, file_resp.text
        assert file_resp.content == b"%PDF-1.4 hello"

        artifact_resp = await client.get(
            f"/v1/sessions/{thread_id}/workspace/artifacts/report.pdf/download",
            params={"tenant_id": str(_DEFAULT_TENANT)},
            headers=sys_headers,
        )
        assert artifact_resp.status_code == 200, artifact_resp.text
        assert artifact_resp.content == b"%PDF-1.4 hello"


@pytest.mark.parametrize("name,suffix,extra", _SESSION_SCOPE_ENDPOINTS)
@pytest.mark.asyncio
async def test_session_detail_foreign_tenant_user_403(
    session_client: AsyncClient, name: str, suffix: str, extra: dict[str, str]
) -> None:
    thread_id = await _create_owned_session(session_client)
    foreign = {"Authorization": f"Bearer {make_test_jwt(tenant_id=uuid4())}"}
    resp = await session_client.get(
        f"/v1/sessions/{thread_id}{suffix}",
        params={"tenant_id": str(_DEFAULT_TENANT), **extra},
        headers=foreign,
    )
    assert resp.status_code == 403, f"{name}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"]["code"] == "TENANT_NOT_ALLOWED", name


@pytest.mark.parametrize("name,suffix,extra", _SESSION_SCOPE_ENDPOINTS)
@pytest.mark.asyncio
async def test_session_detail_tenant_id_star_400(
    session_client: AsyncClient, name: str, suffix: str, extra: dict[str, str]
) -> None:
    thread_id = await _create_owned_session(session_client)
    resp = await session_client.get(
        f"/v1/sessions/{thread_id}{suffix}",
        params={"tenant_id": "*", **extra},
    )
    assert resp.status_code == 400, f"{name}: {resp.status_code} {resp.text}"
    assert resp.json()["detail"]["code"] == "SCOPE_ALL_NOT_SUPPORTED", name
