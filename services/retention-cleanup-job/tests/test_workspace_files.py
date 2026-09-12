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
    agent_subtree,
    deleted_marker,
    iter_thread_dirs,
    iter_tool_result_dirs,
    remove_thread_dir,
    remove_tool_result_dir,
    unlink_registered_file,
    user_root,
    validate_workspace_root,
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


# ------------------------------------------------ 审查补:中间层 symlink + 根校验


@pytest.mark.parametrize("which", ["tenant", "user"])
def test_unlink_refuses_escape_through_symlinked_tenant_or_user_dir(
    tmp_path: Path, which: str
) -> None:
    """tenant 或 user 目录本身是指向工作区外的 symlink:登记路径在词法上仍在
    ``{root}/{tenant}/{user}`` 下,但真实落点在外面 —— 必须拒绝。"""
    tenant, user = uuid4(), uuid4()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim.txt").write_bytes(b"secret")
    if which == "tenant":
        (tmp_path / str(tenant)).symlink_to(outside)
        (outside / str(user)).mkdir()
        (outside / str(user) / "victim.txt").write_bytes(b"secret")
        target = outside / str(user) / "victim.txt"
    else:
        (tmp_path / str(tenant)).mkdir()
        (tmp_path / str(tenant) / str(user)).symlink_to(outside)
        target = outside / "victim.txt"

    with pytest.raises(UnsafeWorkspacePathError):
        unlink_registered_file(str(tmp_path), tenant, user, "victim.txt")
    assert target.read_bytes() == b"secret"


def test_remove_thread_dir_refuses_symlinked_user_dir(tmp_path: Path) -> None:
    tenant, user, tid = uuid4(), uuid4(), uuid4()
    outside = tmp_path / "outside"
    (outside / "threads" / str(tid)).mkdir(parents=True)
    (outside / "threads" / str(tid) / "MEMORY.md").write_bytes(b"secret")
    (tmp_path / str(tenant)).mkdir()
    (tmp_path / str(tenant) / str(user)).symlink_to(outside)

    with pytest.raises(UnsafeWorkspacePathError):
        remove_thread_dir(str(tmp_path), tenant, user, tid)
    assert (outside / "threads" / str(tid) / "MEMORY.md").exists()


def test_iter_thread_dirs_skips_symlinked_tenant_and_user_dirs(tmp_path: Path) -> None:
    tenant, user, tid = uuid4(), uuid4(), uuid4()
    outside = tmp_path / "outside"
    (outside / str(user) / "threads" / str(tid)).mkdir(parents=True)
    (tmp_path / str(tenant)).symlink_to(outside)

    assert list(iter_thread_dirs(str(tmp_path))) == []


def test_iter_thread_dirs_reports_newest_mtime_of_dir_and_direct_entries(tmp_path: Path) -> None:
    import time

    tenant, user, tid = uuid4(), uuid4(), uuid4()
    memory = _seed(tmp_path, tenant, user, f"threads/{tid}/MEMORY.md")
    old = time.time() - 30 * 86400
    os.utime(memory.parent, (old, old))
    os.utime(memory, (old, old))
    (found,) = iter_thread_dirs(str(tmp_path))
    assert abs(found.newest_mtime - old) < 2

    # A file inside touched later than the dir itself bumps the reading.
    fresh = time.time() - 60
    os.utime(memory, (fresh, fresh))
    (found,) = iter_thread_dirs(str(tmp_path))
    assert abs(found.newest_mtime - fresh) < 2


def test_validate_workspace_root_accepts_a_real_directory(tmp_path: Path) -> None:
    assert validate_workspace_root(str(tmp_path)) == tmp_path.resolve()


@pytest.mark.parametrize("shape", ["missing", "file", "symlink"])
def test_validate_workspace_root_rejects_missing_file_and_symlink(
    tmp_path: Path, shape: str
) -> None:
    """NAS 没挂上(目录不存在)最危险:每个登记文件都会「看起来不在」,行被删、
    字节永远留在没挂上的卷里。文件 / symlink 同样拒绝。"""
    if shape == "missing":
        root = tmp_path / "nope"
    elif shape == "file":
        root = tmp_path / "afile"
        root.write_bytes(b"x")
    else:
        real = tmp_path / "real"
        real.mkdir()
        root = tmp_path / "link"
        root.symlink_to(real)
    with pytest.raises(ValueError, match="workspace_root"):
        validate_workspace_root(str(root))


# ------------------------------------------------------- B-50 按 agent 分层之后


def _seed_dir(root: Path, rel: str) -> Path:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "PLAN.md").write_bytes(b"p")
    return d


def test_iter_thread_dirs_finds_both_legacy_and_agent_scoped(tmp_path: Path) -> None:
    """搬迁期两种位置同时存在 —— 只认一处的话另一处的孤儿永远清不掉。

    而且不会有任何东西报错:漏掉的目录在扫描器眼里就是「不存在」。
    """
    tenant, user = uuid4(), uuid4()
    legacy, scoped = uuid4(), uuid4()
    base = tmp_path / str(tenant) / str(user)
    _seed_dir(base, f"{THREADS_DIR}/{legacy}")
    _seed_dir(base, f"agents/plan-aaaaaaaa/{THREADS_DIR}/{scoped}")

    found = {(d.thread_id, d.agent_key) for d in iter_thread_dirs(str(tmp_path))}

    assert found == {(legacy, ""), (scoped, "plan-aaaaaaaa")}


def test_iter_thread_dirs_never_descends_into_shared(tmp_path: Path) -> None:
    """``shared/`` 是新的「不许碰」区 —— 装的是搬迁时反推不出归属的 legacy。

    那批文件**按定义**查不到登记行,一旦被扫进来就会被当成孤儿按 mtime 删掉,
    正是这个模块的不变式要挡的形状。这里造一个 ``shared/threads/<uuid>/``
    (搬迁确实会造出这个形状)来钉住。
    """
    tenant, user = uuid4(), uuid4()
    orphaned = uuid4()
    base = tmp_path / str(tenant) / str(user)
    _seed_dir(base, f"shared/{THREADS_DIR}/{orphaned}")

    assert list(iter_thread_dirs(str(tmp_path))) == []


def test_iter_tool_result_dirs_finds_both_locations(tmp_path: Path) -> None:
    """``.tool_results/<run_id>/`` 与 threads 同一套枚举规矩。"""
    tenant, user = uuid4(), uuid4()
    legacy, scoped = uuid4(), uuid4()
    base = tmp_path / str(tenant) / str(user)
    _seed_dir(base, f".tool_results/{legacy}")
    _seed_dir(base, f"agents/sop-bbbbbbbb/.tool_results/{scoped}")

    found = {(d.run_id, d.agent_key) for d in iter_tool_result_dirs(str(tmp_path))}

    assert found == {(legacy, ""), (scoped, "sop-bbbbbbbb")}


def test_iter_tool_result_dirs_never_descends_into_shared(tmp_path: Path) -> None:
    tenant, user = uuid4(), uuid4()
    base = tmp_path / str(tenant) / str(user)
    _seed_dir(base, f"shared/.tool_results/{uuid4()}")

    assert list(iter_tool_result_dirs(str(tmp_path))) == []


def test_remove_dirs_honour_the_agent_key(tmp_path: Path) -> None:
    """带 agent_key 删的是 agent 子树里那个,不带删的是用户根那个 —— 互不影响。"""
    tenant, user = uuid4(), uuid4()
    tid, run_id = uuid4(), uuid4()
    base = tmp_path / str(tenant) / str(user)
    legacy_t = _seed_dir(base, f"{THREADS_DIR}/{tid}")
    scoped_t = _seed_dir(base, f"agents/plan-aaaaaaaa/{THREADS_DIR}/{tid}")
    legacy_r = _seed_dir(base, f".tool_results/{run_id}")
    scoped_r = _seed_dir(base, f"agents/plan-aaaaaaaa/.tool_results/{run_id}")

    assert remove_thread_dir(str(tmp_path), tenant, user, tid, agent_key="plan-aaaaaaaa")
    assert remove_tool_result_dir(str(tmp_path), tenant, user, run_id, agent_key="plan-aaaaaaaa")

    assert not scoped_t.exists()
    assert not scoped_r.exists()
    assert legacy_t.exists(), "同名的用户根目录不该被连带删掉"
    assert legacy_r.exists()

    assert remove_thread_dir(str(tmp_path), tenant, user, tid)
    assert remove_tool_result_dir(str(tmp_path), tenant, user, run_id)
    assert not legacy_t.exists()
    assert not legacy_r.exists()


@pytest.mark.parametrize("bad", ["../..", "a/b", "", "."])
def test_agent_subtree_rejects_unsafe_keys(tmp_path: Path, bad: str) -> None:
    """目录名从 scandir 读来,会被拼进一条要 rmtree 的路径 —— 自己校验形状。

    空串是例外:它**不是**不安全的值,是「搬迁前的用户根位置」这个明确语义。
    """
    if bad == "":
        assert agent_subtree(tmp_path, bad) == tmp_path
        return
    with pytest.raises(UnsafeWorkspacePathError):
        agent_subtree(tmp_path, bad)
