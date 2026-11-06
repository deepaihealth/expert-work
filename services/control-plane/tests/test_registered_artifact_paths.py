"""B-284 —— ``/agent`` 文件版本的观察不再给登记成产物的文件留版本:control-plane 给它的路径清单。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from control_plane.runtime import build_agent_file_version_store, registered_artifact_paths
from expert_work.persistence import InMemoryArtifactStore
from orchestrator.tools.agent_file_versions import NasAgentFileVersionStore
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore


@pytest.mark.asyncio
async def test_only_this_users_snapshotted_versions_under_the_agent_root() -> None:
    store = InMemoryArtifactStore()
    tenant, user, other = uuid4(), uuid4(), uuid4()
    for who, key, path, snapshot in (
        (user, "plan", "agents/plan/report.docx", "a" * 64),
        (user, "plan", "agents/plan/old.docx", None),  # pre-B-235: no copy
        (user, "plan", "agents/plan/threads/c1/work/out.md", "b" * 64),
        (other, "plan", "agents/plan/theirs.docx", "c" * 64),
        (user, "other", "agents/other/elsewhere.docx", "d" * 64),
    ):
        await store.save_version(
            tenant_id=tenant,
            user_id=who,
            agent_key=key,
            name=path.rsplit("/", 1)[-1],
            kind="document",
            path_in_workspace=path,
            created_in_thread="r",
            version_id=uuid4(),
            snapshot_key=None if snapshot is None else f"snap/{snapshot}",
            size_bytes=None if snapshot is None else 1,
            sha256=snapshot,
        )

    paths = await registered_artifact_paths(store)(tenant, user, "plan")

    assert paths == {"report.docx": {"a" * 64}, "threads/c1/work/out.md": {"b" * 64}}


def test_the_version_store_gets_the_lookup_only_with_an_artifact_store(tmp_path: object) -> None:
    workspace = NasWorkspaceStore(root=str(tmp_path))
    with_lookup = build_agent_file_version_store(workspace, InMemoryArtifactStore())
    without = build_agent_file_version_store(workspace)
    assert isinstance(with_lookup, NasAgentFileVersionStore)
    assert isinstance(without, NasAgentFileVersionStore)
    assert with_lookup.registered_paths is not None
    assert without.registered_paths is None
