"""B-278 review —— a NAS folder the purge cannot simply ``rm -rf`` must not leave the
name stuck in ``purging`` forever, and must never be reported removed while it is
still on disk.

The model can ``chmod 000`` a directory in its own workspace, build a very deep
tree, leave files another uid owns, or put a symlink where the purge expects a
directory; a janitor or a second purge can delete entries under our feet. The
removal (``expert_work.persistence.workspace.safe_remove``) adds owner bits back on
directories we own (never through a link), removes iteratively, verifies with
``lstat``, unlinks a symlink target / skips a symlink on the way, and parks what
still will not go in ``{tenant}/.purged-agents/{user}/`` outside the user root.
Only an entry that can be neither removed nor parked is a failure — and it holds
back only that user's rows.
"""

from __future__ import annotations

import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from control_plane.purge.agent_purge import agent_key_for, purge_agent
from expert_work.persistence import purged_agents_dir
from expert_work.persistence.workspace import safe_remove
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


def _parent_inode(root: Path, tenant: UUID, user: UUID) -> int:
    return os.lstat(_agent_dir(root, tenant, user).parent).st_ino


@pytest.mark.asyncio
async def test_a_folder_that_will_not_go_is_parked_outside_the_user_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _ = await _world()
    user = (await _users(world))[0]
    target = _agent_dir(tmp_path, world.t1, user)
    (target / "f").mkdir(parents=True)
    (target / "f" / "data.txt").write_text("x" * 100)
    real = safe_remove.remove_tree_at

    def _foreign_owned(dfd: int, name: str) -> bool:
        return False if name == agent_key_for(_X) else real(dfd, name)

    monkeypatch.setattr(safe_remove, "remove_tree_at", _foreign_owned)

    summary = await _nas_purge(world, tmp_path)

    assert summary.ok, summary.as_dict()
    assert summary.nas_quarantined == 1
    assert not target.exists()
    assert sorted(p.name for p in target.parent.iterdir()) == []  # nothing left in the root
    holding = purged_agents_dir(str(tmp_path), world.t1, user)
    parked = [p.name for p in holding.iterdir()]
    assert len(parked) == 1 and parked[0].startswith(f"{agent_key_for(_X)}-")
    # Invisible to the workspace browser (and so to the user-root quota walk).
    listing = await NasWorkspaceStore(root=str(tmp_path)).list_files(
        tenant_id=world.t1, user_id=user
    )
    assert not any("data.txt" in e.path for e in listing)


@pytest.mark.asyncio
async def test_an_unrecoverable_folder_holds_back_only_that_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world, _ = await _world()
    stuck, other = await _users(world)
    (_agent_dir(tmp_path, world.t1, stuck) / "f").mkdir(parents=True)
    (_agent_dir(tmp_path, world.t1, other) / "f").mkdir(parents=True)
    stuck_parent = _parent_inode(tmp_path, world.t1, stuck)
    real_remove = safe_remove.remove_tree_at

    def _no_remove(dfd: int, name: str) -> bool:
        if os.fstat(dfd).st_ino == stuck_parent:
            return False
        return real_remove(dfd, name)

    def _no_park(root: str, tenant_id: UUID, owner_id: UUID, src_dfd: int, name: str) -> str:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(safe_remove, "remove_tree_at", _no_remove)
    monkeypatch.setattr(safe_remove, "_quarantine", _no_park)

    summary = await _nas_purge(world, tmp_path)

    assert summary.failures == {"agent_workspace": "NasEntryStuckError"}
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


def test_a_tree_still_on_disk_is_never_reported_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the delete did, success is decided by ``lstat`` afterwards."""
    tenant, user = uuid4(), uuid4()
    target = tmp_path / str(tenant) / str(user) / "agents" / "k-1"
    (target / "a").mkdir(parents=True)

    def _did_nothing(dfd: int, name: str) -> None:
        return None

    monkeypatch.setattr(safe_remove, "_flatten_remove", _did_nothing)

    outcome = safe_remove.remove_entry(
        str(tmp_path), [str(tenant), str(user), "agents"], "k-1", tenant_id=tenant, owner_id=user
    )

    assert outcome == safe_remove.QUARANTINED
    assert not target.exists()


def test_entries_vanishing_mid_delete_still_end_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A janitor / second purge deleting under our feet: ENOENT on a child is fine,
    and the tree must really be gone (reviewer repro: a/b/c left behind)."""
    tenant, user = uuid4(), uuid4()
    base = tmp_path / str(tenant) / str(user) / "agents" / "k-1"
    for d in ("a", "b", "c"):
        (base / d).mkdir(parents=True)
        (base / d / "f.txt").write_text("x")
    real_unlink = os.unlink
    raced = [False]

    def _racing_unlink(name: Any, *args: Any, **kwargs: Any) -> None:
        if not raced[0]:
            raced[0] = True
            real_unlink(name, *args, **kwargs)  # someone else got there first
        real_unlink(name, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", _racing_unlink)

    outcome = safe_remove.remove_entry(
        str(tmp_path), [str(tenant), str(user), "agents"], "k-1", tenant_id=tenant, owner_id=user
    )

    assert raced[0]
    assert outcome == safe_remove.REMOVED
    assert not os.path.lexists(base)


def test_a_vanished_child_does_not_abort_the_delete_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOENT on a child is tolerated inside one pass (not only by the retry)."""
    base = tmp_path / "k-1"
    for d in ("a", "b"):
        (base / d).mkdir(parents=True)
        (base / d / "f.txt").write_text("x")
    real_unlink = os.unlink

    def _already_gone(name: Any, *args: Any, **kwargs: Any) -> None:
        real_unlink(name, *args, **kwargs)
        real_unlink(name, *args, **kwargs)  # the second call is what a racing deleter causes

    monkeypatch.setattr(os, "unlink", _already_gone)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        safe_remove._flatten_remove(fd, "k-1")  # must not raise
    finally:
        os.close(fd)
    assert not os.path.lexists(base)


def test_a_very_deep_tree_is_removed_without_recursion(tmp_path: Path) -> None:
    tenant, user = uuid4(), uuid4()
    agents = tmp_path / str(tenant) / str(user) / "agents"
    agents.mkdir(parents=True)
    fd = os.open(agents, os.O_RDONLY | os.O_DIRECTORY)
    os.mkdir("k-1", dir_fd=fd)
    cur = os.open("k-1", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
    os.close(fd)
    for _ in range(1_100):  # deeper than the recursion limit, and than PATH_MAX allows
        os.mkdir("d", dir_fd=cur)
        nxt = os.open("d", os.O_RDONLY | os.O_DIRECTORY, dir_fd=cur)
        os.close(cur)
        cur = nxt
    os.close(cur)

    outcome = safe_remove.remove_entry(
        str(tmp_path), [str(tenant), str(user), "agents"], "k-1", tenant_id=tenant, owner_id=user
    )

    assert outcome == safe_remove.REMOVED
    assert not (agents / "k-1").exists()


def test_quarantine_sweep_removes_only_entries_older_than_the_cutoff(tmp_path: Path) -> None:
    tenant, user = uuid4(), uuid4()
    holding = purged_agents_dir(str(tmp_path), tenant, user)
    holding.mkdir(parents=True)
    old = holding / f"k-1-1000-{'a' * 8}"
    new = holding / f"k-1-2000-{'b' * 8}"
    stranger = holding / "not-ours"
    for d in (old, new, stranger):
        (d / "x").mkdir(parents=True)

    removed, failed = safe_remove.sweep_quarantine(str(tmp_path), older_than=1_500)

    assert (removed, failed) == (1, 0)
    assert not old.exists() and new.exists() and stranger.exists()


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
        changed = safe_remove.add_owner_bits(fd, "d", os.lstat(d))
    finally:
        os.close(fd)
    assert changed
    assert stat.S_IMODE(os.lstat(d).st_mode) == 0o755


def test_owner_bits_never_reach_through_a_swapped_in_symlink(tmp_path: Path) -> None:
    """lstat said "directory", then the name became a link: nothing outside changes."""
    target = tmp_path / "outside"
    target.mkdir()
    target.chmod(0o500)
    seen_as_dir = os.lstat(target)
    (tmp_path / "swapped").symlink_to(target, target_is_directory=True)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        safe_remove.add_owner_bits(fd, "swapped", seen_as_dir)
    finally:
        os.close(fd)
    mode = stat.S_IMODE(os.lstat(target).st_mode)
    target.chmod(0o700)
    assert mode == 0o500


def test_owner_bits_never_land_on_a_swapped_in_directory(tmp_path: Path) -> None:
    """lstat saw one directory, then another one was renamed into its place."""
    (tmp_path / "seen").mkdir()
    (tmp_path / "seen").chmod(0o500)  # lacks owner write: add_owner_bits would act on it
    seen = os.lstat(tmp_path / "seen")
    (tmp_path / "seen").rename(tmp_path / "moved-away")
    other = tmp_path / "seen"
    other.mkdir()
    other.chmod(0o500)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        changed = safe_remove.add_owner_bits(fd, "seen", seen)
    finally:
        os.close(fd)
    mode = stat.S_IMODE(os.lstat(other).st_mode)
    other.chmod(0o700)
    assert not changed
    assert mode == 0o500
