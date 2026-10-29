"""B-235 —— 三个下载端点读版本副本,并能按版本号取某一版。

事故形状:同一用户两段会话用同一个智能体、存同一个名字,两个版本指着同一个工作区文件,
后一段会话的登记把前一版的内容盖掉;前一段会话的对话页下载到的是后一段会话的产出。

现在每一版都有登记那一刻的副本。三个端点:

* 会话面 ``GET /v1/sessions/{t}/workspace/artifacts/{name}/download?version=N`` —— 对话页 /
  调试台每一轮按「这一轮登记的那一版」下载;
* 控制台 ``GET /v1/artifacts/download?artifact_id=…&version=N`` —— 产物页逐版下载;
* 对外 ``GET /v1/agents/{code}/artifacts/download?version=N`` —— ``version`` 从「校验」改成
  「取这一版」。

B-235 之前登记的版本没有副本:是最新版就照旧读工作区文件;不是最新版 → 409(对话页据此
提示「这一版内容没有单独保留」并给「下载最新版」),永远不拿最新版冒充。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.api._external import external_subject_id
from control_plane.api._external_agent_scope import agent_key_for_code
from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence import ArtifactStore
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol.agent_key import sanitize_agent_key
from orchestrator.tools import RecordingWorkspaceStore, WorkspacePermissionError
from orchestrator.tools.artifact_snapshots import (
    ArtifactSnapshotReadError,
    InMemoryArtifactSnapshotStore,
)
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
_NAME = "report.txt"

_AGENT_YAML = """\
apiVersion: expert_work.io/v1
kind: Agent
metadata:
  name: report-writer
  version: "1.0.0"
  tenant: platform-eng
spec:
  tenant_config: {}
  model:
    provider: anthropic
    name: claude-sonnet-4-5
  system_prompt:
    template: "you write reports"
  sandbox:
    resources: { cpu: "1.0", memory: "1Gi" }
    network:
      egress: proxy
      allowlist: ["api.anthropic.com"]
    filesystem:
      readonly_root: true
      writable: ["/workspace"]
"""


def _settings() -> Settings:
    return Settings(
        env="dev",
        auth_mode="dev",
        rate_limit_burst=10_000,
        rate_limit_per_second=10_000.0,
        oidc_issuer=TEST_ISSUER,
        oidc_audience=[TEST_AUDIENCE],
    )


@dataclass
class _World:
    client: AsyncClient
    artifacts: ArtifactStore
    workspace: RecordingWorkspaceStore
    snapshots: InMemoryArtifactSnapshotStore


def _app() -> tuple[object, RecordingWorkspaceStore, InMemoryArtifactSnapshotStore]:
    app = create_app(
        settings=_settings(),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    workspace = RecordingWorkspaceStore(workspace_file=b"")
    snapshots = InMemoryArtifactSnapshotStore(workspace=workspace)
    app.state.workspace_store = workspace
    app.state.artifact_snapshots = snapshots
    return app, workspace, snapshots


async def _save(
    world: _World,
    *,
    user_id: UUID,
    agent_key: str,
    content: bytes,
    with_copy: bool = True,
) -> int:
    """登记一版:``with_copy=True`` 走 B-235 的写法(先复制再登记),否则是上线前的老写法。

    之后工作区文件就是 ``content`` —— 同名的下一次登记会把它盖掉,正如事故里那样。
    """
    world.workspace.workspace_file = content
    if not with_copy:
        version = await world.artifacts.save_version(
            tenant_id=_TENANT,
            user_id=user_id,
            agent_key=agent_key,
            name=_NAME,
            kind="document",
            path_in_workspace=_NAME,
            created_in_thread="t",
        )
        return version.version
    version_id = uuid4()
    digest = await world.snapshots.capture(
        tenant_id=_TENANT, user_id=user_id, path=_NAME, snapshot_key=str(version_id)
    )
    version = await world.artifacts.save_version(
        tenant_id=_TENANT,
        user_id=user_id,
        agent_key=agent_key,
        name=_NAME,
        kind="document",
        path_in_workspace=_NAME,
        created_in_thread="t",
        version_id=version_id,
        snapshot_key=str(version_id),
        size_bytes=digest.size_bytes,
        sha256=digest.sha256,
    )
    return version.version


# --------------------------------------------------------------------------- 会话面


@dataclass
class _Session:
    world: _World
    thread_id: str
    user_id: UUID
    agent_key: str

    async def save(self, content: bytes, *, with_copy: bool = True) -> int:
        return await _save(
            self.world,
            user_id=self.user_id,
            agent_key=self.agent_key,
            content=content,
            with_copy=with_copy,
        )

    async def download(self, version: int | None = None) -> object:
        params = {} if version is None else {"version": version}
        return await self.world.client.get(
            f"/v1/sessions/{self.thread_id}/workspace/artifacts/{_NAME}/download", params=params
        )


@pytest.fixture
async def session() -> AsyncIterator[_Session]:
    app, workspace, snapshots = _app()
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT)}"}
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://cp.test",
        headers=headers,
    ) as client:
        await client.post("/v1/agents", json={"manifest_yaml": _AGENT_YAML})
        create = await client.post(
            "/v1/sessions", json={"agent_name": "report-writer", "agent_version": "1.0.0"}
        )
        thread_id = create.json()["data"]["thread_id"]
        meta = await app.state.thread_meta_repo.get(UUID(thread_id), tenant_id=_TENANT)  # type: ignore[attr-defined]
        assert meta is not None and meta.user_id is not None
        world = _World(
            client=client,
            artifacts=app.state.artifact_store,  # type: ignore[attr-defined]
            workspace=workspace,
            snapshots=snapshots,
        )
        yield _Session(
            world=world,
            thread_id=thread_id,
            user_id=meta.user_id,
            agent_key=sanitize_agent_key(meta.agent_name or ""),
        )


@pytest.mark.asyncio
async def test_session_download_serves_the_version_that_turn_registered(
    session: _Session,
) -> None:
    """事故重放:v1 = AAA,之后另一段会话登记 v2 = BBB(工作区文件已被盖成 BBB)。"""
    await session.save(b"AAA")
    await session.save(b"BBB")

    resp = await session.download(version=1)

    assert resp.status_code == 200, resp.text  # type: ignore[attr-defined]
    assert resp.content == b"AAA", "按版本下载拿到了后来那一版的内容"  # type: ignore[attr-defined]
    assert resp.headers["X-Artifact-Version"] == "1"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_session_download_without_version_serves_the_latest_copy_not_the_file(
    session: _Session,
) -> None:
    await session.save(b"AAA")
    await session.save(b"BBB")
    session.world.workspace.workspace_file = b"CCC-edited-after-saving"

    resp = await session.download()

    assert resp.status_code == 200  # type: ignore[attr-defined]
    assert resp.content == b"BBB", "最新版下载读的是工作区当前文件,不是登记时的副本"  # type: ignore[attr-defined]
    assert resp.headers["X-Artifact-Version"] == "2"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_session_download_of_pre_release_versions(session: _Session) -> None:
    """B-235 之前登记的版本:最新版照旧读工作区文件;更早的版本 409,不拿最新版冒充。"""
    await session.save(b"OLD-1", with_copy=False)
    await session.save(b"OLD-2", with_copy=False)

    latest = await session.download()
    assert (latest.status_code, latest.content) == (200, b"OLD-2")  # type: ignore[attr-defined]
    pinned_latest = await session.download(version=2)
    assert (pinned_latest.status_code, pinned_latest.content) == (200, b"OLD-2")  # type: ignore[attr-defined]

    older = await session.download(version=1)
    assert older.status_code == 409, older.text  # type: ignore[attr-defined]
    assert b"OLD-2" not in older.content  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_session_download_of_an_unknown_version_is_404(session: _Session) -> None:
    await session.save(b"AAA")

    resp = await session.download(version=7)

    assert resp.status_code == 404  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_session_download_of_a_lost_copy_is_404_not_the_current_file(
    session: _Session,
) -> None:
    await session.save(b"AAA")
    await session.world.snapshots.delete_all_for_user(tenant_id=_TENANT, user_id=session.user_id)
    session.world.workspace.workspace_file = b"something else"

    resp = await session.download(version=1)

    assert resp.status_code == 404  # type: ignore[attr-defined]
    assert b"something else" not in resp.content  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- 控制台


@pytest.fixture
async def console() -> AsyncIterator[tuple[_World, UUID]]:
    app, workspace, snapshots = _app()
    subject = "user-b235"
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT, subject=subject)}"}
    user = await app.state.tenant_user_repo.resolve(  # type: ignore[attr-defined]
        tenant_id=_TENANT, subject_type="user", subject_id=subject
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://cp.test",
        headers=headers,
    ) as client:
        yield (
            _World(
                client=client,
                artifacts=app.state.artifact_store,  # type: ignore[attr-defined]
                workspace=workspace,
                snapshots=snapshots,
            ),
            user.id,
        )


async def _console_artifact_id(client: AsyncClient) -> str:
    resp = await client.get("/v1/artifacts")
    return str(next(a["id"] for a in resp.json()["artifacts"] if a["name"] == _NAME))


@pytest.mark.asyncio
async def test_console_download_by_version_and_retained_flags(
    console: tuple[_World, UUID],
) -> None:
    world, user_id = console
    await _save(world, user_id=user_id, agent_key="a-1", content=b"OLD", with_copy=False)
    await _save(world, user_id=user_id, agent_key="a-1", content=b"AAA")
    await _save(world, user_id=user_id, agent_key="a-1", content=b"BBB")
    artifact_id = await _console_artifact_id(world.client)

    def _get(version: int | None) -> object:
        params: dict[str, object] = {"artifact_id": artifact_id}
        if version is not None:
            params["version"] = version
        return world.client.get("/v1/artifacts/download", params=params)

    v2 = await _get(2)
    assert (v2.status_code, v2.content) == (200, b"AAA")  # type: ignore[attr-defined]
    assert v2.headers["X-Artifact-Version"] == "2"  # type: ignore[attr-defined]
    latest = await _get(None)
    assert (latest.status_code, latest.content) == (200, b"BBB")  # type: ignore[attr-defined]
    assert (await _get(1)).status_code == 409  # type: ignore[attr-defined]
    assert (await _get(9)).status_code == 404  # type: ignore[attr-defined]

    versions = await world.client.get(f"/v1/artifacts/{artifact_id}/versions")
    retained = {v["version"]: v["retained"] for v in versions.json()["versions"]}
    assert retained == {3: True, 2: True, 1: False}


# --------------------------------------------------------------------------- 对外


_AGENT_CODE = "report-writer"
_EXTERNAL_USER = "u-b235"


@pytest.fixture
async def external() -> AsyncIterator[tuple[_World, UUID]]:
    app, workspace, snapshots = _app()
    jwt = make_test_jwt(
        tenant_id=_TENANT,
        subject="third-party-app",
        sub_type="service_account",
        roles=(),
        scopes=("read",),
    )
    user = await app.state.tenant_user_repo.resolve(  # type: ignore[attr-defined]
        tenant_id=_TENANT, subject_type="user", subject_id=external_subject_id(_EXTERNAL_USER)
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://cp.test",
        headers={"Authorization": f"Bearer {jwt}"},
    ) as client:
        yield (
            _World(
                client=client,
                artifacts=app.state.artifact_store,  # type: ignore[attr-defined]
                workspace=workspace,
                snapshots=snapshots,
            ),
            user.id,
        )


async def _external_get(world: _World, version: int | None) -> object:
    params: dict[str, object] = {"user_id": _EXTERNAL_USER, "name": _NAME}
    if version is not None:
        params["version"] = version
    return await world.client.get(f"/v1/agents/{_AGENT_CODE}/artifacts/download", params=params)


@pytest.mark.asyncio
async def test_external_version_fetches_that_version(external: tuple[_World, UUID]) -> None:
    world, user_id = external
    key = agent_key_for_code(_AGENT_CODE)
    await _save(world, user_id=user_id, agent_key=key, content=b"AAA")
    await _save(world, user_id=user_id, agent_key=key, content=b"BBB")

    old = await _external_get(world, 1)
    assert old.status_code == 200, old.text  # type: ignore[attr-defined]
    assert old.content == b"AAA"  # type: ignore[attr-defined]
    assert old.headers["X-Artifact-Version"] == "1"  # type: ignore[attr-defined]

    latest = await _external_get(world, None)
    assert (latest.content, latest.headers["X-Artifact-Version"]) == (b"BBB", "2")  # type: ignore[attr-defined]

    unknown = await _external_get(world, 5)
    assert unknown.status_code == 409  # type: ignore[attr-defined]
    assert unknown.json()["error"]["code"] == "ARTIFACT_VERSION_MISMATCH"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_external_pre_release_older_version_stays_409(
    external: tuple[_World, UUID],
) -> None:
    world, user_id = external
    key = agent_key_for_code(_AGENT_CODE)
    await _save(world, user_id=user_id, agent_key=key, content=b"OLD-1", with_copy=False)
    await _save(world, user_id=user_id, agent_key=key, content=b"OLD-2", with_copy=False)

    older = await _external_get(world, 1)
    assert older.status_code == 409  # type: ignore[attr-defined]
    assert older.json()["error"]["code"] == "ARTIFACT_VERSION_MISMATCH"  # type: ignore[attr-defined]
    latest = await _external_get(world, 2)
    assert (latest.status_code, latest.content) == (200, b"OLD-2")  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status"),
    [
        (WorkspacePermissionError("copy not readable"), 500),
        (ArtifactSnapshotReadError("copy unreadable (5)"), 404),
    ],
)
async def test_copy_read_failures_go_through_the_error_ladder(
    session: _Session, monkeypatch: pytest.MonkeyPatch, error: Exception, status: int
) -> None:
    """读副本失败:没权限是服务端配置问题(500),I/O 失败按「内容取不到」(404);
    都不能冒成没有处理的异常。"""
    await session.save(b"AAA")

    async def broken(**_: object) -> bytes:
        raise error

    monkeypatch.setattr(session.world.snapshots, "read", broken)
    resp = await session.download(version=1)

    assert resp.status_code == status, resp.text  # type: ignore[attr-defined]
    assert "copy not readable" not in resp.text  # type: ignore[attr-defined]
