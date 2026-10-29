"""B-236 —— 删对话删它自己的 ``/workspace``,不删它登记过的产物。

对话的工作目录是 ``agents/<key>/threads/<会话>/work``,会话 purge 删整个
``agents/<key>/threads/<会话>``(B-27 起就这么删)。登记过的产物每一版都有 B-235 的副本,
放在 ``{tenant}/.artifact-versions/{user}/`` —— 用户根之外,不在任何对话目录里 —— 所以对话
删了,产物照样能下载(用户拍板:删对话不删其登记的产物)。真 NAS store 落盘验。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from control_plane.app import create_app
from control_plane.audit import build_default_audit_logger
from control_plane.settings import DEFAULT_DEV_TENANT_ID, Settings
from expert_work.persistence.audit_log import InMemoryAuditLogStore
from expert_work.protocol.agent_key import sanitize_agent_key
from orchestrator.tools.artifact_snapshots import NasArtifactSnapshotStore
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore, workspace_user_root
from tests.auth_fixtures import (
    TEST_AUDIENCE,
    TEST_ISSUER,
    build_test_jwt_verifier,
    make_test_jwt,
)

_TENANT = DEFAULT_DEV_TENANT_ID
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


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[tuple[AsyncClient, object, Path]]:
    app = create_app(
        settings=Settings(
            env="dev",
            auth_mode="dev",
            rate_limit_burst=10_000,
            rate_limit_per_second=10_000.0,
            oidc_issuer=TEST_ISSUER,
            oidc_audience=[TEST_AUDIENCE],
        ),
        audit_logger=build_default_audit_logger(InMemoryAuditLogStore()),
        jwt_verifier=build_test_jwt_verifier(),
    )
    store = NasWorkspaceStore(root=str(tmp_path))
    app.state.workspace_store = store
    app.state.artifact_snapshots = NasArtifactSnapshotStore(workspace=store)
    headers = {"Authorization": f"Bearer {make_test_jwt(tenant_id=_TENANT)}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://cp.test", headers=headers
    ) as client:
        resp = await client.post("/v1/agents", json={"manifest_yaml": _AGENT_YAML})
        assert resp.status_code in (200, 201), resp.text
        yield client, app, tmp_path


async def _session(client: AsyncClient) -> str:
    resp = await client.post(
        "/v1/sessions", json={"agent_name": "report-writer", "agent_version": "1.0.0"}
    )
    return str(resp.json()["data"]["thread_id"])


@pytest.mark.asyncio
async def test_purge_removes_the_work_dir_and_the_artifact_still_downloads(
    world: tuple[AsyncClient, object, Path],
) -> None:
    client, app, root = world
    purged, kept = await _session(client), await _session(client)
    meta = await app.state.thread_meta_repo.get(UUID(purged), tenant_id=_TENANT)  # type: ignore[attr-defined]
    assert meta is not None and meta.user_id is not None
    assert meta.workspace_layout == "conversation"
    key = sanitize_agent_key(meta.agent_name or "")
    rel = f"agents/{key}/threads/{purged}/work/report.txt"
    path = workspace_user_root(str(root), _TENANT, meta.user_id) / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(b"conversation report")

    version_id = uuid4()
    digest = await app.state.artifact_snapshots.capture(  # type: ignore[attr-defined]
        tenant_id=_TENANT, user_id=meta.user_id, path=rel, snapshot_key=str(version_id)
    )
    await app.state.artifact_store.save_version(  # type: ignore[attr-defined]
        tenant_id=_TENANT,
        user_id=meta.user_id,
        agent_key=key,
        name="report.txt",
        kind="document",
        path_in_workspace=rel,
        created_in_thread=purged,
        version_id=version_id,
        snapshot_key=str(version_id),
        size_bytes=digest.size_bytes,
        sha256=digest.sha256,
    )

    resp = await client.post(f"/v1/sessions/{purged}:purge")

    assert resp.status_code == 200, resp.text
    assert not path.parent.exists(), "对话的工作目录必须随对话删掉"
    assert not path.parent.parent.exists()
    download = await client.get(
        f"/v1/sessions/{kept}/workspace/artifacts/report.txt/download", params={"version": 1}
    )
    assert download.status_code == 200, download.text
    assert download.content == b"conversation report"
