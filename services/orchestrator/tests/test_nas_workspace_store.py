"""``NasWorkspaceStore`` —— NAS 直读工作区(sandbox migration 波 2 Task 3)。

零 mock:真文件系统(``tmp_path``)驱动每一个用例,包括路径穿越四件套(``..``
相对路径 / 绝对路径 / 逃逸子树的符号链接 / URL 编码字面量)、读写 cap、以及
用 monkeypatch 精确注入的 TOCTOU race(检查后、syscall 前换中间目录为符号链
接)—— 这些恰恰是 ``_open_parent_dir_fd`` 的 dir_fd 逐段解析唯一的存在理由,
mock 掉文件系统就等于没测。行为契约对照 ``sandbox_supervisor.supervisor``
(``list_workspace_files`` /
``read_workspace_file`` / ``write_workspace_file`` / ``delete_workspace_file`` /
``mark_workspace_deleted``,``supervisor.py:485-583``)——task-3-brief.md
"语义 parity 清单"一节列了逐方法映射。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from orchestrator.tools.nas_workspace_store import DELETED_MARKER, NasWorkspaceStore
from orchestrator.tools.sandbox import RecordingSandboxRuntime, SandboxSupervisorError
from orchestrator.tools.workspace_store import WorkspaceFileEntry, WorkspaceStore


def _store(root: Path) -> NasWorkspaceStore:
    return NasWorkspaceStore(root=str(root))


def _swap_dir_for_symlink(directory: Path, target: Path) -> None:
    """Replace ``directory`` (a flat dir — no subdirectories) with a symlink to ``target``.

    Used by the dir_fd-pinning race tests to simulate a concurrent writer
    swapping an intermediate directory's *name* out from under an
    already-open ``dir_fd`` for it. Deliberately uses only plain (no
    ``dir_fd=``) removal calls — ``shutil.rmtree`` internally issues its own
    ``dir_fd``-relative ``os.unlink``/``os.open`` calls, which would
    re-trigger a test's ``os.open``/``os.unlink`` monkeypatch on itself
    (infinite recursion) whenever a patched call's target name happens to
    match something inside the directory being torn down.
    """
    for child in directory.iterdir():
        child.unlink()
    directory.rmdir()
    directory.symlink_to(target)


def test_satisfies_workspace_store_protocol(tmp_path: Path) -> None:
    assert isinstance(_store(tmp_path), WorkspaceStore)


def test_runtime_field_defaults_to_none(tmp_path: Path) -> None:
    """Task 4 接线用的字段;调用方不传就是 Task 3 的行为(``mark_deleted``
    跳过热会话拆除,只写软删标记)——dataclass 默认值即证明。"""
    assert _store(tmp_path).runtime is None


def test_instance_store_field_defaults_to_none(tmp_path: Path) -> None:
    """同上,``get_warm`` 查询用的另一半接线。"""
    assert _store(tmp_path).instance_store is None


# ---------------------------------------------------------------- 路径穿越四件套


async def test_read_file_rejects_dot_dot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SandboxSupervisorError):
        await store.read_file(tenant_id=uuid4(), user_id=uuid4(), path="../x")


async def test_read_file_rejects_absolute_path(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(SandboxSupervisorError):
        await store.read_file(tenant_id=uuid4(), user_id=uuid4(), path="/etc/passwd")


async def test_read_file_rejects_symlink_escaping_user_root(tmp_path: Path) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    user_root = tmp_path / str(tenant_id) / str(user_id)
    user_root.mkdir(parents=True)
    (user_root / "escape.txt").symlink_to(outside)

    store = _store(tmp_path)
    with pytest.raises(SandboxSupervisorError):
        await store.read_file(tenant_id=tenant_id, user_id=user_id, path="escape.txt")


async def test_url_encoded_traversal_is_a_literal_filename_not_a_decode(tmp_path: Path) -> None:
    """Store 不做 URL 解码 —— ``%2e%2e%2f`` 没有真实 ``/``,当成普通文件名,
    落在 user_root 子树内,不逃逸(brief 明确两种结局都算过,这里断言"不逃
    逸"这一条:写入的文件确实落在 user_root 下)。
    """
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    literal = "%2e%2e%2f"

    await store.write_file(tenant_id=tenant_id, user_id=user_id, path=literal, data=b"x")

    user_root = tmp_path / str(tenant_id) / str(user_id)
    target = user_root / literal
    assert target.is_file()
    assert target.read_bytes() == b"x"
    # 没有任何文件落在 user_root 子树之外(tmp_path 顶层没有新增文件)。
    assert list(tmp_path.glob(literal)) == []


# ---------------------------------------------------------------- 读写删列 roundtrip


async def test_write_read_list_delete_roundtrip(tmp_path: Path) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)

    await store.write_file(
        tenant_id=tenant_id, user_id=user_id, path="out/report.txt", data=b"hello"
    )
    data = await store.read_file(tenant_id=tenant_id, user_id=user_id, path="out/report.txt")
    assert data == b"hello"

    files = await store.list_files(tenant_id=tenant_id, user_id=user_id)
    assert files == [WorkspaceFileEntry(path="out/report.txt", size=5)]

    await store.delete_file(tenant_id=tenant_id, user_id=user_id, path="out/report.txt")
    files_after = await store.list_files(tenant_id=tenant_id, user_id=user_id)
    assert files_after == []


async def test_list_files_hides_reserved_prefixes_and_marker(tmp_path: Path) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    await store.write_file(
        tenant_id=tenant_id, user_id=user_id, path="skills/foo/skill.json", data=b"{}"
    )
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="uploads/a.txt", data=b"in")
    await store.write_file(
        tenant_id=tenant_id, user_id=user_id, path="out.txt", data=b"agent output"
    )
    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)

    files = await store.list_files(tenant_id=tenant_id, user_id=user_id)
    assert files == [WorkspaceFileEntry(path="out.txt", size=len(b"agent output"))]


async def test_delete_file_rejects_reserved_path(tmp_path: Path) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="uploads/a.txt", data=b"in")

    with pytest.raises(SandboxSupervisorError):
        await store.delete_file(tenant_id=tenant_id, user_id=user_id, path="uploads/a.txt")

    # 拒绝删除意味着文件原样还在。
    data = await store.read_file(tenant_id=tenant_id, user_id=user_id, path="uploads/a.txt")
    assert data == b"in"


async def test_delete_file_missing_is_a_noop(tmp_path: Path) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    await store.delete_file(tenant_id=tenant_id, user_id=user_id, path="nope.txt")


# ---------------------------------------------------------------- Critical 修复回归:
# marker 不能被 delete_file 直接删掉(解除软删),也不能被 write_file 伪造。


async def test_delete_file_rejects_the_deleted_marker(tmp_path: Path) -> None:
    """delete_file 直接删 marker 等于绕过 mark_deleted 之外的任何流程解除软
    删——审查者实测复现的 Critical。拒绝之后 marker 必须原样还在。"""
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)

    with pytest.raises(SandboxSupervisorError):
        await store.delete_file(tenant_id=tenant_id, user_id=user_id, path=DELETED_MARKER)

    user_root = tmp_path / str(tenant_id) / str(user_id)
    assert (user_root / DELETED_MARKER).is_file()


async def test_write_file_rejects_the_deleted_marker(tmp_path: Path) -> None:
    """write_file 能写出这个文件名等于能伪造"该工作区已软删"的状态,不用
    走 mark_deleted——同一 Critical 的另一半。"""
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)

    with pytest.raises(SandboxSupervisorError):
        await store.write_file(
            tenant_id=tenant_id, user_id=user_id, path=DELETED_MARKER, data=b"forged"
        )

    user_root = tmp_path / str(tenant_id) / str(user_id)
    assert not (user_root / DELETED_MARKER).exists()


# ---------------------------------------------------------------- Important 修复回归(第二轮):
# 复审者用 monkeypatch 精确注入 race,在 write/read/delete 三个方法上都复现
# 了"检查后 syscall 前换中间目录为外部 symlink"的逃逸——包括第一轮"再次
# resolve + O_NOFOLLOW"修法本身:因为 os.open(target)/os.unlink(target) 拿到
# 的是重新拼出的字符串路径,内核仍会从头 walk 并跟随中间分量的 symlink,一次
# "检查完再查一次"挡不住第二次 race。结构性修法是 dir_fd 逐段 openat——一旦
# 某个中间目录的 dir_fd 已经打开,它就钉死在当时的 inode 上,后续无论那个
# 名字在其父目录里被换成什么(rename/unlink/symlink),都不会影响已经持有的
# fd。这里精确复现复审者的三种形态:在"中间目录的 dir_fd 已经拿到、最终
# syscall 还没发生"这个更窄的窗口里换 "sub" 为指向子树外的符号链接。
#
# 断言口径(照复审者原话)= 不逃逸:外部文件未被写 / 未被读到 / 未被删,不
# 强制要求成功还是报错(dir_fd 钉住原 inode 后,操作本身完全可能在"已被
# unlink 的原目录"里正常成功——那依然是安全的,只要没有触达 "outside")。


async def test_write_file_dir_fd_pinning_survives_intermediate_rename_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复审者复现形态(write):`_open_parent_dir_fd` 已经为中间目录 "sub"
    拿到 dir_fd 之后、写入叶子文件 "out.txt" 的最终 openat 之前,并发写手把
    "sub" 这个名字在其父目录(user_root)里整个换成指向子树外 "outside" 的
    符号链接。dir_fd 钉住的是 "sub" 原来的 inode,不受这次改名影响——载荷
    不能落到 "outside" 里。"""
    tenant_id, user_id = uuid4(), uuid4()
    outside = tmp_path / "outside"
    outside.mkdir()
    user_root = tmp_path / str(tenant_id) / str(user_id)
    sub = user_root / "sub"
    sub.mkdir(parents=True)
    store = _store(tmp_path)

    real_open = os.open
    triggered = False

    def _racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        # 匹配"打开叶子文件 out.txt 这一步"——按 basename 匹配而不是要求
        # dir_fd 关键字,这样同一条 race 对 dir_fd 实现(本 fix)和纯字符串
        # 路径实现(上一轮的实现)都能在正确的时刻注入:两者都是"先拿到/
        # 校验完 parent,再对叶子文件名调用 os.open"这个形状,只是 parent
        # 的表示方式不同(fd vs 字符串)。one-shot ``triggered`` 标记必须在
        # 调用 ``_swap_dir_for_symlink`` *之前* 置位——它自己的
        # ``child.unlink()`` 也会经过这同一个被 monkeypatch 的 ``os.open``
        # / ``os.unlink``,不提前置位会递归自触发。
        nonlocal triggered
        if not triggered and os.path.basename(os.fspath(path)) == "out.txt":
            triggered = True
            # 模拟并发写手在这个窗口把 "sub" 整个替换成指向子树外的符号
            # 链接。
            _swap_dir_for_symlink(sub, outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _racing_open)

    try:
        await store.write_file(
            tenant_id=tenant_id, user_id=user_id, path="sub/out.txt", data=b"pwned"
        )
    except SandboxSupervisorError:
        pass

    # 关键断言:不逃逸——载荷没有落到子树外的 "outside" 里。
    assert not (outside / "out.txt").exists()


async def test_read_file_dir_fd_pinning_survives_intermediate_rename_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一族 race,读路径:"sub" 的 dir_fd 已经拿到之后、读取叶子文件
    "a.txt" 的最终 openat 之前,把 "sub" 换成指向子树外 "outside" 的符号链
    接——"outside/a.txt" 里放着一个诱饵秘密内容。读到的内容绝不能是这个
    秘密。"""
    tenant_id, user_id = uuid4(), uuid4()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.txt").write_text("outside secret")
    user_root = tmp_path / str(tenant_id) / str(user_id)
    sub = user_root / "sub"
    sub.mkdir(parents=True)
    (sub / "a.txt").write_text("inside legit")
    store = _store(tmp_path)

    real_open = os.open
    triggered = False

    def _racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal triggered
        if not triggered and os.path.basename(os.fspath(path)) == "a.txt":
            triggered = True
            _swap_dir_for_symlink(sub, outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _racing_open)

    try:
        data: bytes | None = await store.read_file(
            tenant_id=tenant_id, user_id=user_id, path="sub/a.txt"
        )
    except SandboxSupervisorError:
        data = None

    # 关键断言:不逃逸——读到的绝不是子树外那个诱饵文件的内容。
    assert data != b"outside secret"


async def test_delete_file_dir_fd_pinning_survives_intermediate_rename_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一族 race,删除路径——复审者特别指出"unlink 不解引用末段 symlink
    所以免疫"这个结论只对末段成立,中间分量照样会被跟随:"sub" 的 dir_fd
    已经拿到之后、删除叶子文件 "victim.txt" 的最终 unlinkat 之前,把 "sub"
    换成指向子树外 "outside" 的符号链接,"outside/victim.txt" 是不相关租户
    的文件。这个文件绝不能被删掉。"""
    tenant_id, user_id = uuid4(), uuid4()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_victim = outside / "victim.txt"
    outside_victim.write_text("do not delete me")
    user_root = tmp_path / str(tenant_id) / str(user_id)
    sub = user_root / "sub"
    sub.mkdir(parents=True)
    (sub / "victim.txt").write_text("delete me")
    store = _store(tmp_path)

    real_unlink = os.unlink
    triggered = False

    def _racing_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal triggered
        if not triggered and os.path.basename(os.fspath(path)) == "victim.txt":
            triggered = True
            _swap_dir_for_symlink(sub, outside)
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", _racing_unlink)

    try:
        await store.delete_file(tenant_id=tenant_id, user_id=user_id, path="sub/victim.txt")
    except SandboxSupervisorError:
        pass

    # 关键断言:不逃逸——子树外不相关租户的文件必须原封不动。
    assert outside_victim.exists()
    assert outside_victim.read_text() == "do not delete me"


# ---------------------------------------------------------------- 读写 cap


async def test_read_file_rejects_over_cap(tmp_path: Path) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    user_root = tmp_path / str(tenant_id) / str(user_id)
    user_root.mkdir(parents=True)
    big = user_root / "big.bin"
    with big.open("wb") as f:
        f.seek(10 * 1024 * 1024)  # 10MiB + 1 字节,seek 造稀疏文件不真占磁盘
        f.write(b"\x00")

    store = _store(tmp_path)
    with pytest.raises(SandboxSupervisorError):
        await store.read_file(tenant_id=tenant_id, user_id=user_id, path="big.bin")


async def test_write_file_rejects_over_cap(tmp_path: Path) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    data = b"\x00" * (25 * 1024 * 1024 + 1)

    with pytest.raises(SandboxSupervisorError):
        await store.write_file(tenant_id=tenant_id, user_id=user_id, path="huge.bin", data=data)

    files = await store.list_files(tenant_id=tenant_id, user_id=user_id)
    assert files == []


# ---------------------------------------------------------------- mark_deleted


async def test_mark_deleted_is_idempotent_and_writes_the_marker(tmp_path: Path) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)

    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)
    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)  # 幂等,不抛

    user_root = tmp_path / str(tenant_id) / str(user_id)
    assert (user_root / DELETED_MARKER).is_file()


async def test_mark_deleted_creates_the_user_dir_when_missing(tmp_path: Path) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)

    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)

    user_root = tmp_path / str(tenant_id) / str(user_id)
    assert user_root.is_dir()


# ---------------------------------------------------------------- mark_deleted 热会话拆除(Task 4)


@dataclass
class _FakeInstanceStore:
    """``SandboxInstanceStore.get_warm`` 的最小替身 —— 只测
    ``NasWorkspaceStore.mark_deleted`` 消费它的那一个方法,预置
    ``(tenant_id, user_id) -> (sandbox_id, container_id)``。"""

    warm: dict[tuple[UUID, UUID], tuple[UUID, str]] = field(default_factory=dict)

    async def get_warm(self, *, tenant_id: UUID, user_id: UUID) -> tuple[UUID, str] | None:
        return self.warm.get((tenant_id, user_id))


async def test_mark_deleted_destroys_warm_session(tmp_path: Path) -> None:
    """``runtime``/``instance_store`` 都配了、且用户确实有一个热会话 ——
    ``mark_deleted`` 必须 ``destroy`` 它(reason="workspace_deleted"),
    而不只是留一个 marker 文件让它悬在那里活到 idle TTL。"""
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    runtime = RecordingSandboxRuntime()
    instance_store = _FakeInstanceStore(warm={(tenant_id, user_id): (sandbox_id, "e2b-live")})
    store = NasWorkspaceStore(root=str(tmp_path), runtime=runtime, instance_store=instance_store)

    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)

    assert runtime.destroyed == [(sandbox_id, "workspace_deleted")]
    # marker 仍然照常落盘 —— 热会话拆除是在它之上的追加动作,不是替代。
    assert (tmp_path / str(tenant_id) / str(user_id) / DELETED_MARKER).is_file()


async def test_mark_deleted_skips_teardown_when_no_warm_session(tmp_path: Path) -> None:
    """该用户当下没有热会话(``get_warm`` 返回 ``None``)—— 不该调
    ``destroy``,marker 仍然写。"""
    tenant_id, user_id = uuid4(), uuid4()
    runtime = RecordingSandboxRuntime()
    instance_store = _FakeInstanceStore()
    store = NasWorkspaceStore(root=str(tmp_path), runtime=runtime, instance_store=instance_store)

    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)

    assert runtime.destroyed == []
    assert (tmp_path / str(tenant_id) / str(user_id) / DELETED_MARKER).is_file()


async def test_mark_deleted_skips_teardown_without_both_wired(tmp_path: Path) -> None:
    """``runtime``/``instance_store`` 两者只配一个(接线半成品)—— 按"两者都
    没配"同样降级为跳过,不炸 ``AttributeError``(见 ``mark_deleted`` 的
    docstring:这两个字段只由 ``build_workspace_store`` 一起注入,单配一个
    是接线 bug,不该表现成一个看着像文件系统故障的异常)。"""
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    instance_store = _FakeInstanceStore(warm={(tenant_id, user_id): (sandbox_id, "e2b-live")})
    store = NasWorkspaceStore(root=str(tmp_path), runtime=None, instance_store=instance_store)

    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)  # 不抛

    assert (tmp_path / str(tenant_id) / str(user_id) / DELETED_MARKER).is_file()


# ---------------------------------------------------------------- 目录不存在


async def test_list_files_missing_user_dir_returns_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    files = await store.list_files(tenant_id=uuid4(), user_id=uuid4())
    assert files == []
