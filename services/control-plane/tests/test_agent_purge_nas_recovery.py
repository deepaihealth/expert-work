"""B-278 review —— a NAS folder the purge cannot simply ``rm -rf`` must not leave the
name stuck in ``purging`` forever.

The model can ``chmod 000`` a directory in its own workspace, leave files another
uid owns, or put a symlink where the purge expects a directory. The purge first
gives directories it owns back their owner bits, then deletes; a symlink at the
end of the path is unlinked, never followed; a folder that still will not go is
renamed out of the way (``.purged-<name>-…``, counted as ``nas_quarantined``) so
a future same-name agent cannot inherit it. Only a folder that can be neither
removed nor renamed is a failure — and it holds back only that user's rows.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from control_plane.purge import agent_purge_nas
from control_plane.purge.agent_purge import agent_key_for, purge_agent
from orchestrator.tools.agent_file_versions import NasAgentFileVersionStore
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from tests.test_agent_purge import _X, _counts, _World, _world


async def _users(world: _World) -> list[UUID]:
    metas = await world.threads.list_by_tenant(world.t1, agent_name=_X, include_archived=True)
    return sorted(m.user_id for m in metas if m.user_id is not None)


def _agent_dir(root: Path, tenant: UUID, user: UUID) -> Path:
    return root / str(tenant) / str(user) / "agents" / agent_key_for(_X)


async def _nas_purge(world: _World, root: Path, **overrides: Any) -> Any:
    nas = NasWorkspaceStore(root=str(root))
    deps = replace(
        world.deps(),
        workspace_store=nas,
        workspace_root=str(root),
        agent_file_versions=NasAgentFileVersionStore(workspace=nas),
        artifact_snapshots=None,
        **overrides,
    )
    return await purge_agent(deps, tenant_id=world.t1, name=_X)


@pytest.mark.asyncio
async def test_a_chmod_000_directory_is_made_removable_and_deleted(tmp_path: Path) -> None:
    world, _ = await _world()
    user = (await _users(world))[0]
    locked = _agent_dir(tmp_path, world.t1, user) / "out" / "locked"
    locked.mkdir(parents=True)
    (locked / "secret.txt").write_text("x")
    locked.chmod(0)
    locked.parent.chmod(0o500)  # and a read-only parent

    summary = await _nas_purge(world, tmp_path)

    assert summary.ok, summary.as_dict()
    assert not _agent_dir(tmp_path, world.t1, user).exists()
    assert summary.nas_quarantined == 0


@pytest.mark.asyncio
async def test_a_folder_that_will_not_go_is_quarantined_and_the_purge_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _ = await _world()
    user = (await _users(world))[0]
    target = _agent_dir(tmp_path, world.t1, user)
    (target / "f").mkdir(parents=True)
    real_rmtree = shutil.rmtree

    def _foreign_owned(path: Any, *args: Any, **kwargs: Any) -> None:
        if path == agent_key_for(_X):
            raise PermissionError(1, "Operation not permitted")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", _foreign_owned)

    summary = await _nas_purge(world, tmp_path)

    assert summary.ok, summary.as_dict()
    assert summary.nas_quarantined == 1
    assert not target.exists()
    parked = [p.name for p in target.parent.iterdir()]
    assert len(parked) == 1 and parked[0].startswith(f".purged-{agent_key_for(_X)}-")


@pytest.mark.asyncio
async def test_an_unrecoverable_folder_holds_back_only_that_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _ = await _world()
    stuck, other = await _users(world)
    (_agent_dir(tmp_path, world.t1, stuck) / "f").mkdir(parents=True)
    (_agent_dir(tmp_path, world.t1, other) / "f").mkdir(parents=True)
    real_rmtree = shutil.rmtree
    real_rename = os.rename
    stuck_parent = os.lstat(_agent_dir(tmp_path, world.t1, stuck).parent).st_ino

    def _stuck(fd: int) -> bool:
        return os.fstat(fd).st_ino == stuck_parent

    def _no_rmtree(path: Any, *args: Any, dir_fd: int | None = None, **kwargs: Any) -> None:
        if dir_fd is not None and _stuck(dir_fd):
            raise PermissionError(1, "Operation not permitted")
        real_rmtree(path, *args, dir_fd=dir_fd, **kwargs)

    def _no_rename(src: Any, dst: Any, *, src_dir_fd: int | None = None, **kwargs: Any) -> None:
        if src_dir_fd is not None and _stuck(src_dir_fd):
            raise PermissionError(1, "Operation not permitted")
        real_rename(src, dst, src_dir_fd=src_dir_fd, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", _no_rmtree)
    monkeypatch.setattr(os, "rename", _no_rename)

    summary = await _nas_purge(world, tmp_path)

    assert summary.failures == {"agent_workspace": "PermissionError"}
    assert summary.nas_failed_users == 1
    assert not summary.agent_spec_deleted  # visible, and the name waits for a re-run
    assert not _agent_dir(tmp_path, world.t1, other).exists()
    left = await world.threads.list_by_tenant(world.t1, agent_name=_X, include_archived=True)
    assert [m.user_id for m in left] == [stuck]  # only the stuck user's rows are kept
    instances = await world.instances.list_by_agent(tenant_id=world.t1, agent_code=_X)
    assert [i.user_id for i in instances] == [stuck]

    monkeypatch.undo()
    rerun = await _nas_purge(world, tmp_path)
    assert rerun.ok, rerun.as_dict()
    after = await _counts(world, world.t1, _X)
    assert all(v == 0 for v in after.values()), after


@pytest.mark.asyncio
async def test_without_a_nas_root_a_failed_user_keeps_only_their_anchors() -> None:
    world, _ = await _world()
    stuck, _other = await _users(world)
    original = world.workspace.delete_tree

    async def _fails_for_stuck(*, tenant_id: UUID, user_id: UUID, path: str) -> None:
        if user_id == stuck and path.startswith("agents/"):
            raise PermissionError(1, "Operation not permitted")
        await original(tenant_id=tenant_id, user_id=user_id, path=path)

    world.workspace.delete_tree = _fails_for_stuck  # type: ignore[method-assign]
    summary = await purge_agent(world.deps(), tenant_id=world.t1, name=_X)

    assert summary.failures == {"agent_workspace": "PermissionError"}
    left = await world.threads.list_by_tenant(world.t1, agent_name=_X, include_archived=True)
    assert [m.user_id for m in left] == [stuck]
    instances = await world.instances.list_by_agent(tenant_id=world.t1, agent_code=_X)
    assert [i.user_id for i in instances] == [stuck]
    versions = await world.artifacts.list_versions_for_agent(
        tenant_id=world.t1, agent_key=agent_key_for(_X)
    )
    assert {v.user_id for v in versions} == {stuck}


@pytest.mark.asyncio
async def test_a_symlink_as_the_agent_folder_is_unlinked_not_followed(tmp_path: Path) -> None:
    world, _ = await _world()
    user = (await _users(world))[0]
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.md").write_text("not the agent's")
    link = _agent_dir(tmp_path, world.t1, user)
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)

    summary = await _nas_purge(world, tmp_path)

    assert summary.ok, summary.as_dict()
    assert not os.path.lexists(link)
    assert (outside / "keep.md").exists()
    assert summary.nas_symlinks_unlinked == 1


@pytest.mark.asyncio
async def test_a_symlinked_user_root_is_skipped_and_counted(tmp_path: Path) -> None:
    world, _ = await _world()
    user = (await _users(world))[0]
    outside = tmp_path / "other-root"
    (outside / "agents" / agent_key_for(_X)).mkdir(parents=True)
    keep = outside / "agents" / agent_key_for(_X) / "keep.md"
    keep.write_text("someone else's")
    (tmp_path / str(world.t1)).mkdir()
    (tmp_path / str(world.t1) / str(user)).symlink_to(outside, target_is_directory=True)

    summary = await _nas_purge(world, tmp_path)

    assert summary.ok, summary.as_dict()
    assert keep.exists()
    assert summary.nas_symlinks_skipped >= 1
    assert "nas_symlink" in summary.skipped
    assert summary.as_dict()["nas_symlinks_skipped"] == summary.nas_symlinks_skipped


def test_owner_bits_are_only_added_never_removed(tmp_path: Path) -> None:
    d = tmp_path / "d"
    d.mkdir()
    d.chmod(0o055)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        agent_purge_nas._make_removable(fd, "d")
    finally:
        os.close(fd)
    assert stat.S_IMODE(os.lstat(d).st_mode) == 0o755
