"""``retention_cleanup_job.workspace_files`` —— 三条碰文件规则共用的路径闸。

布局拼法与 orchestrator 的 NAS store 是两处独立声明(job 不 import orchestrator),
第一组测试把两边钉在一起;其余测试是闸本身:只 unlink 一个普通文件、不删目录、
不跟 symlink、不出用户根。
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from orchestrator.context.workspace_projection import THREADS_DIR as ORCH_THREADS_DIR
from orchestrator.tools.nas_workspace_store import (
    DELETED_DIR as ORCH_DELETED_DIR,
)
from orchestrator.tools.nas_workspace_store import (
    workspace_deleted_marker,
    workspace_user_root,
)
from retention_cleanup_job.workspace_files import (
    DELETED_DIR,
    THREADS_DIR,
    UnsafeWorkspacePathError,
    deleted_marker,
    iter_thread_dirs,
    remove_thread_dir,
    unlink_registered_file,
    user_root,
)

# ---------------------------------------------------------------------- parity


def test_layout_matches_orchestrator_nas_store(tmp_path: Path) -> None:
    tenant, user = uuid4(), uuid4()
    assert user_root(str(tmp_path), tenant, user) == workspace_user_root(
        str(tmp_path), tenant, user
    )
    assert deleted_marker(str(tmp_path), tenant, user) == workspace_deleted_marker(
        str(tmp_path), tenant, user
    )
    assert DELETED_DIR == ORCH_DELETED_DIR
    assert THREADS_DIR == ORCH_THREADS_DIR


# --------------------------------------------------------- unlink_registered_file


def _seed(root: Path, tenant: object, user: object, rel: str, data: bytes = b"x") -> Path:
    target = root / str(tenant) / str(user) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def test_unlink_removes_only_that_file_and_keeps_its_directory(tmp_path: Path) -> None:
    tenant, user = uuid4(), uuid4()
    victim = _seed(tmp_path, tenant, user, "高血压21天随访/day1.md")
    sibling = _seed(tmp_path, tenant, user, "高血压21天随访/notes.md")

    assert unlink_registered_file(str(tmp_path), tenant, user, "高血压21天随访/day1.md") is True

    assert not victim.exists()
    assert sibling.exists()
    assert victim.parent.is_dir()


def test_unlink_missing_file_or_missing_user_root_is_false(tmp_path: Path) -> None:
    tenant, user = uuid4(), uuid4()
    assert unlink_registered_file(str(tmp_path), tenant, user, "nope.md") is False
    (tmp_path / str(tenant) / str(user)).mkdir(parents=True)
    assert unlink_registered_file(str(tmp_path), tenant, user, "nope.md") is False


@pytest.mark.parametrize("bad", ["", "   ", "/etc/passwd", "../x", "a/../../x", "a\0b", "."])
def test_unlink_rejects_paths_that_are_not_relative_workspace_paths(
    tmp_path: Path, bad: str
) -> None:
    tenant, user = uuid4(), uuid4()
    with pytest.raises(UnsafeWorkspacePathError):
        unlink_registered_file(str(tmp_path), tenant, user, bad)


def test_unlink_refuses_symlink_leaf_and_directory(tmp_path: Path) -> None:
    tenant, user = uuid4(), uuid4()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    root = tmp_path / str(tenant) / str(user)
    root.mkdir(parents=True)
    (root / "link.txt").symlink_to(outside)
    (root / "adir").mkdir()

    with pytest.raises(UnsafeWorkspacePathError):
        unlink_registered_file(str(tmp_path), tenant, user, "link.txt")
    with pytest.raises(UnsafeWorkspacePathError):
        unlink_registered_file(str(tmp_path), tenant, user, "adir")
    assert outside.read_bytes() == b"secret"
    assert (root / "link.txt").is_symlink()


def test_unlink_refuses_escape_through_symlinked_parent(tmp_path: Path) -> None:
    tenant, user = uuid4(), uuid4()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_bytes(b"secret")
    root = tmp_path / str(tenant) / str(user)
    root.mkdir(parents=True)
    (root / "escape").symlink_to(outside)

    with pytest.raises(UnsafeWorkspacePathError):
        unlink_registered_file(str(tmp_path), tenant, user, "escape/victim.txt")
    assert (outside / "victim.txt").exists()


# ------------------------------------------------------------ remove_thread_dir


def test_remove_thread_dir_only_that_thread(tmp_path: Path) -> None:
    tenant, user = uuid4(), uuid4()
    gone, kept = uuid4(), uuid4()
    _seed(tmp_path, tenant, user, f"threads/{gone}/MEMORY.md")
    _seed(tmp_path, tenant, user, f"threads/{gone}/PLAN.md")
    _seed(tmp_path, tenant, user, f"threads/{kept}/MEMORY.md")

    assert remove_thread_dir(str(tmp_path), tenant, user, gone) is True
    assert not (tmp_path / str(tenant) / str(user) / "threads" / str(gone)).exists()
    assert (tmp_path / str(tenant) / str(user) / "threads" / str(kept) / "MEMORY.md").exists()
    assert remove_thread_dir(str(tmp_path), tenant, user, gone) is False


def test_remove_thread_dir_refuses_symlink(tmp_path: Path) -> None:
    tenant, user = uuid4(), uuid4()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_bytes(b"secret")
    threads = tmp_path / str(tenant) / str(user) / "threads"
    threads.mkdir(parents=True)
    tid = uuid4()
    (threads / str(tid)).symlink_to(outside)

    with pytest.raises(UnsafeWorkspacePathError):
        remove_thread_dir(str(tmp_path), tenant, user, tid)
    assert (outside / "victim.txt").exists()


# ---------------------------------------------------------------- iter_thread_dirs


def test_iter_thread_dirs_only_uuid_shaped_and_skips_soft_deleted_users(tmp_path: Path) -> None:
    tenant, live_user, deleted_user = uuid4(), uuid4(), uuid4()
    t1, t2 = uuid4(), uuid4()
    _seed(tmp_path, tenant, live_user, f"threads/{t1}/MEMORY.md")
    _seed(tmp_path, tenant, live_user, "threads/not-a-uuid/x.md")
    _seed(tmp_path, tenant, live_user, "style/rules.md")
    _seed(tmp_path, tenant, deleted_user, f"threads/{t2}/MEMORY.md")
    marker = deleted_marker(str(tmp_path), tenant, deleted_user)
    marker.parent.mkdir(parents=True)
    marker.touch()
    (tmp_path / "_scratch" / "junk").mkdir(parents=True)
    (tmp_path / "lost+found").mkdir()

    found = list(iter_thread_dirs(str(tmp_path)))

    assert [(d.tenant_id, d.user_id, d.thread_id) for d in found] == [(tenant, live_user, t1)]
    assert found[0].path == tmp_path / str(tenant) / str(live_user) / "threads" / str(t1)


def test_iter_thread_dirs_missing_root_is_empty(tmp_path: Path) -> None:
    assert list(iter_thread_dirs(str(tmp_path / "nope"))) == []
    assert os.path.exists(tmp_path)
