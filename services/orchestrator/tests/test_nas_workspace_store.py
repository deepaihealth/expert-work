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
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from orchestrator.tools.nas_workspace_store import (
    DELETED_DIR,
    NasWorkspaceStore,
    workspace_deleted_marker,
    workspace_user_root,
)
from orchestrator.tools.sandbox import (
    RecordingSandboxRuntime,
    SandboxSupervisorError,
    WorkspacePermissionError,
)
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


def test_workspace_user_root_matches_store_internal_user_root(tmp_path: Path) -> None:
    """Task 4 审查 Minor —— ``NasWorkspaceStore`` 与 ``AgentSandboxClient``
    共用同一个路径函数,不再各拼各的。这里断言公开函数与该 store 私有的
    ``_user_root`` 逐字节同一个结果(``_user_root`` 现在就是这个函数的薄
    封装,但断言值相等而不是断言"调用了同一个函数",防止未来有人把
    ``_user_root`` 改回独立拼接又不留痕迹)。"""
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)

    assert workspace_user_root(str(tmp_path), tenant_id, user_id) == store._user_root(
        tenant_id, user_id
    )
    assert workspace_user_root(str(tmp_path), tenant_id, user_id) == (
        tmp_path / str(tenant_id) / str(user_id)
    )


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


async def test_list_files_hides_reserved_prefixes(tmp_path: Path) -> None:
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


# --------------------------------------------- 跨 uid 写冲突(Critical 复审第 6 条)


async def test_write_file_creates_intermediate_dirs_owner_only(tmp_path: Path) -> None:
    """`_openat_dir` 新建的每一层中间目录都要 ``fchmod`` 到 ``0o700``——不然
    ``os.mkdir`` 的默认 mode 会被这个进程的 umask(常见 0o022)掩成 0o755,
    读/写这棵子树都会踩坑(read/list 不受影响,是这个坑本身难被发现的原
    因)。control-plane 与沙箱里的 agent 现在是同一个 uid(方向变更,spec §
    六:共享 gid → 统一 uid),属主位就够两侧读写,不需要再放开
    group/other。这里两层嵌套(`a/` 与 `a/b/`)都要落 0o700,不是只有最
    外层。
    """
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)

    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a/b/c.txt", data=b"x")

    user_root = tmp_path / str(tenant_id) / str(user_id)
    assert stat.S_IMODE((user_root / "a").stat().st_mode) == 0o700
    assert stat.S_IMODE((user_root / "a" / "b").stat().st_mode) == 0o700


async def test_write_file_does_not_reset_mode_of_a_pre_existing_intermediate_dir(
    tmp_path: Path,
) -> None:
    """新建目录才 chmod——不是 belt-and-braces 地把整条路径上每一层都强行
    改成 0o700。一个已经存在的目录(这里模拟沙箱自己用受限 mode 建的
    `a/`)在 ``_openat_dir`` 的快路径(``O_NOFOLLOW`` 直接 open 成功)里直接
    返回,不会被这次 write 顺手改权限——只有这次调用真正带出来的新目录
    (`a/b`)才落 0o700。预先存在的 `a/` 故意留在一个与 0o700 不同的 mode
    (0o750),这样"没被改动"和"碰巧也是 0o700"才不会混为一谈。
    """
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    user_root = tmp_path / str(tenant_id) / str(user_id)
    restricted = user_root / "a"
    restricted.mkdir(parents=True)
    restricted.chmod(0o750)

    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a/b/c.txt", data=b"x")

    assert stat.S_IMODE(restricted.stat().st_mode) == 0o750, "预先存在的目录被意外改权限了"
    assert stat.S_IMODE((restricted / "b").stat().st_mode) == 0o700, "本次新建的目录没有收紧权限"


# ------------------------------------------------- 权限失败单独归因(W2-BUG-1 的诊断
# 成本几乎全在"读不动被收成不存在"这一条上,fix loop 换来的独立改进,与 uid 方案正交)


async def test_created_user_root_is_owner_only(tmp_path: Path) -> None:
    """``write_file`` 建出来的用户根目录必须是 0o700——属主位够两侧(同 uid)
    读写,不再需要放开 group/other。"""
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)

    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a.txt", data=b"x")

    user_root = tmp_path / str(tenant_id) / str(user_id)
    assert stat.S_IMODE(user_root.stat().st_mode) == 0o700


async def test_write_file_lands_owner_only_mode(tmp_path: Path) -> None:
    """``write_file`` 落地的文件是 0o600——不是 0o644 也不是 0o640:属主就是
    读方,不需要 group/other 位。"""
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)

    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a.txt", data=b"x")

    leaf = tmp_path / str(tenant_id) / str(user_id) / "a.txt"
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o600


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root 无视文件权限位,chmod 0o000 之后依旧读得动——这条测试的前提"
    "(权限位真的能挡住读)在 root 下不成立",
)
async def test_read_file_reports_permission_denied_distinctly(tmp_path: Path) -> None:
    """读不动 ≠ 不存在。

    W2-BUG-1 的诊断成本几乎全在这一条上:``PermissionError`` 被收成
    "workspace file not found",端点翻成 404,用户看到"文件不存在"而它明明
    列在上一屏 —— 只能靠翻服务端日志才诊断得出来。
    """
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a.txt", data=b"x")
    leaf = tmp_path / str(tenant_id) / str(user_id) / "a.txt"
    os.chmod(leaf, 0o000)

    with pytest.raises(WorkspacePermissionError):
        await store.read_file(tenant_id=tenant_id, user_id=user_id, path="a.txt")


async def test_write_file_skips_chmod_for_a_pre_existing_user_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户根已经存在(CSI subPath 建的、迁移脚本建的、备份恢复出来的,总
    之不是这次调用创建的)时,这次 ``write_file`` 完全不该碰它的 mode:
    ``chmod`` 只对*属主*放行,对一个我们不是属主的既存目录会 EPERM,而旧
    版本这里无条件跑,会把每一次后续上传都变成 "failed to create workspace
    directory"(一条谎报,目录明明早就在)。

    用一个"一被调用就断言失败"的假身份钉死"压根不该调它"——不是断言没抛
    异常(那样一个吞掉 EPERM 的实现也能蒙混过关),而是直接证明这个函数从
    未被触碰。
    """
    tenant_id, user_id = uuid4(), uuid4()
    user_root = tmp_path / str(tenant_id) / str(user_id)
    user_root.mkdir(parents=True)  # 模拟这棵目录不是这次调用建的

    def _boom_chmod(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("os.chmod must not run for a pre-existing user root")

    monkeypatch.setattr(os, "chmod", _boom_chmod)
    store = _store(tmp_path)

    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a.txt", data=b"x")

    assert (user_root / "a.txt").read_bytes() == b"x"


async def test_write_file_wraps_a_write_failure_as_sandbox_supervisor_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ENOSPC``/``EDQUOT``(NAS 配额、磁盘满)是这里真实会发生的失败,
    ``handle.write(data)`` 之前完全没包在任何 ``try/except`` 里——一个裸
    ``OSError`` 会越过这个 store 自己在同一个方法上面反复强调的"错误类型
    统一"边界。

    ``os.fdopen`` 给的是带缓冲的 ``BufferedWriter``(默认 8 KiB),小于这个
    缓冲区的写入(恰好就是这整个任务的旗舰复现场景——``MEMORY.md`` 那种大
    小的文件)根本不会在 ``write()`` 这一步真正碰到底层 ``write(2)``
    syscall,``ENOSPC``/``EDQUOT`` 只会在 close/flush 时才浮出来。这里的假
    ``write()`` 因此改成正常返回(模拟被缓冲、暂时"成功"),真正的失败挪到
    ``__exit__``(模拟 close 时才炸)——只把 ``try`` 套在 ``handle.write``
    外面而不套住 ``with`` 块本身的隐式 close/flush 的话,这条测试就会因为
    一个裸 ``OSError`` 越过 ``pytest.raises(SandboxSupervisorError)`` 而
    失败。
    """
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)

    class _BoomWriter:
        def __enter__(self) -> _BoomWriter:
            return self

        def __exit__(self, *_exc_info: object) -> None:
            # close()/flush() 时才炸——缓冲写入的真实失败点。
            raise OSError(28, "No space left on device")  # ENOSPC

        def write(self, data: bytes) -> int:
            return len(data)  # 落进缓冲区,暂时"成功",不代表真的到盘了。

    def _fake_fdopen(fd: int, _mode: str) -> _BoomWriter:
        os.close(fd)  # 断言路径不需要真的写,先把 fd 还回去避免泄漏。
        return _BoomWriter()

    monkeypatch.setattr(os, "fdopen", _fake_fdopen)

    with pytest.raises(SandboxSupervisorError):
        await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a.txt", data=b"x")


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root 无视目录权限位,父目录没有 w 位照样建得出新文件——这条测试的前提在 root 下不成立",
)
async def test_write_file_reports_permission_denied_distinctly(tmp_path: Path) -> None:
    """写不动 ≠ 不存在,同 ``read_file`` 那条(见上面)。锁的是**父目录**的
    写权限,不是某个已存在文件的权限位——新建文件需要目录的 ``w`` 位,不是
    文件自己的,这是这条分支在生产里最可能被触发的方式(见
    ``_open_parent_dir_fd`` 里那段关于 NAS 根没 chmod 的说明)。
    """
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    user_root = tmp_path / str(tenant_id) / str(user_id)
    user_root.mkdir(parents=True)
    os.chmod(user_root, 0o500)  # r-x,没有 w —— 目录里建不出新文件

    with pytest.raises(WorkspacePermissionError):
        await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a.txt", data=b"x")


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root 无视目录权限位,父目录没有 w 位照样 unlink 得掉里面的文件"
    "——这条测试的前提在 root 下不成立",
)
async def test_delete_file_reports_permission_denied_distinctly(tmp_path: Path) -> None:
    """删不动 ≠ 不存在。``unlink`` 需要的是父目录的 ``w`` 位,不是文件自己
    的权限位——锁父目录(而不是文件本身)才是复现这条分支的正确姿势。
    """
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a.txt", data=b"x")
    user_root = tmp_path / str(tenant_id) / str(user_id)
    os.chmod(user_root, 0o500)  # 文件建完之后再锁父目录

    with pytest.raises(WorkspacePermissionError):
        await store.delete_file(tenant_id=tenant_id, user_id=user_id, path="a.txt")


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root 无视目录权限位,0o000 依旧扫得动子目录——这条测试的前提在 root 下不成立",
)
async def test_list_files_reports_permission_denied_for_unreadable_subtree(
    tmp_path: Path,
) -> None:
    """``os.walk`` 默认 ``onerror=None`` 会把扫不动的子树静默吞掉(不报
    错、不列出)——这恰恰是列表权限失败最常见的形态,比单个文件的 ``lstat``
    失败常见得多。

    必须显式接 ``onerror`` 让它炸出来;不然一次真实的权限故障会被这层静默
    悄悄变成"工作区是空的"(``control_plane/api/workspace.py`` 把
    ``SandboxSupervisorError`` 及其子类翻成 ``{"success": true, "files":
    []}``),这正是这整个任务要根治的那类失败。

    复审 I-1 —— ``onerror`` 回调之前裸拼 ``os.walk`` 给的 ``exc.filename``
    (永远是绝对路径),而这恰恰是两个 is_dir 兄弟分支里*会真的触发*的那一
    个(``test_list_files_raises_when_tenant_dir_is_unreadable`` 锁的是祖先
    目录不可读,``onerror=`` 根本没轮到;这条才是 onerror 真正被调用的形
    状)。之前只有前者被这条"不带绝对路径"的断言钉住,后者是漏网的那半
    边。
    """
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="sub/a.txt", data=b"x")
    user_root = tmp_path / str(tenant_id) / str(user_id)
    sub = user_root / "sub"
    os.chmod(sub, 0o000)
    try:
        with pytest.raises(WorkspacePermissionError) as excinfo:
            await store.list_files(tenant_id=tenant_id, user_id=user_id)
    finally:
        # 0o000 连 pytest 自己的 tmp_path 清理都进不去(既不能列目录也不能
        # 递归删)——测试结束前放宽回来,不留垃圾目录。
        os.chmod(sub, 0o700)

    assert str(tmp_path) not in str(excinfo.value)
    assert "'sub'" in str(excinfo.value), "工作区相对路径应该还在,不是被整个抹掉了"


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root 无视目录权限位,0o000 依旧穿得透——这条测试的前提在 root 下不成立",
)
async def test_list_files_raises_when_tenant_dir_is_unreadable(tmp_path: Path) -> None:
    """``list_files`` 里 ``onerror=`` 接手之前有一句检查用户根是不是目录;
    祖先目录(这里是 ``{tenant_id}/`` 本身)没有搜索权限时,这一句必须是一
    个包了边的 ``PermissionError`` 出口——不然 ``onerror=`` 根本还没轮到,
    权限故障就已经漏出去了。

    这句检查是版本无关的 ``stat.S_ISDIR(os.stat(...).st_mode)``,不是
    ``Path.is_dir()``——见 ``list_files`` 的实现注释:``Path.is_dir()`` 的
    错误吞咽行为不跨 CPython 版本稳定,3.14 的默认路径会无条件吞掉
    ``PermissionError``,悄悄重演这条测试想关掉的失败。
    """
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a.txt", data=b"x")
    tenant_dir = tmp_path / str(tenant_id)
    os.chmod(tenant_dir, 0o000)
    try:
        with pytest.raises(WorkspacePermissionError) as excinfo:
            await store.list_files(tenant_id=tenant_id, user_id=user_id)
    finally:
        os.chmod(tenant_dir, 0o700)

    # 消息不该带绝对服务端挂载路径:同一方法里其它兄弟(lstat 失败那条)已经
    # 用工作区相对路径,这里也一样,不带这次检查目标(用户根自己)的绝对路径。
    assert str(tmp_path) not in str(excinfo.value)


async def test_list_files_reports_permission_denied_for_a_single_unstattable_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复审 M-3 —— ``for name in filenames`` 循环里那条 ``except
    PermissionError`` 之前没有测试覆盖过(删掉它整套 49/49 依旧全绿)。它守
    的是一个与"目录扫不动"不同的形状:``os.walk`` 已经能列出这个文件名(说
    明父目录本身可搜索/可读),但对**这一个条目**单独 ``lstat`` 却被拒——
    真实世界里 ACL/NFS 侧的逐条目权限位就能造出这种情况,本机 tmpfs 权限位
    造不出(POSIX 下 ``lstat`` 只需要祖先目录的搜索权限,不需要文件自己的
    任何权限位,父目录既然能被 ``os.walk`` 扫到,`lstat` 正常情况下不会单
    独被拒),所以用 monkeypatch 精确地只让目标文件的 ``lstat`` 抛
    ``PermissionError``,其余调用走真实实现。
    """
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a.txt", data=b"x")
    user_root = tmp_path / str(tenant_id) / str(user_id)
    target = user_root / "a.txt"
    real_lstat = Path.lstat

    def _fake_lstat(self: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if self == target:
            raise PermissionError(13, "Permission denied")
        return real_lstat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", _fake_lstat)

    with pytest.raises(WorkspacePermissionError) as excinfo:
        await store.list_files(tenant_id=tenant_id, user_id=user_id)

    assert "'a.txt'" in str(excinfo.value)


# ------------------------------------------- 全分支终审 C-1:marker 必须在挂载子树之外
#
# 旧实现把 marker 落在 ``{root}/{tenant}/{user}/`` —— 正是沙箱经 subPath 挂到
# ``/workspace`` 的那棵树。沙箱里的 agent 直接 ``write_file(".ew-workspace-
# deleted")`` 就能落出同一个文件,而软删闸读的就是它:一次提示注入可以把活跃
# 用户的工作区标记成待回收。文件名黑名单挡不住(沙箱根本不经过这个 store),
# 唯一的结构性修法是把 marker 搬出任何 subPath 会挂进沙箱的位置。


async def test_mark_deleted_writes_the_marker_outside_the_mounted_user_tree(
    tmp_path: Path,
) -> None:
    """marker 不能出现在 ``{root}/{tenant}/{user}/`` 下的任何位置 —— 那棵子树
    整个会被 ``csi-volume-config`` 的 ``subPath`` 挂进沙箱的 ``/workspace``。"""
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)

    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)

    user_root = tmp_path / str(tenant_id) / str(user_id)
    residents = list(user_root.rglob("*")) if user_root.exists() else []
    assert residents == [], f"marker(或别的东西)落在了沙箱可写的子树里:{residents}"


async def test_marker_lives_beside_the_user_tree_not_inside_it(tmp_path: Path) -> None:
    """落点是 ``{root}/{tenant}/.deleted/{user}`` —— 与用户目录平级、不被任何
    ``subPath`` 挂进沙箱,且目录名不是合法 UUID,不会与用户目录撞名。"""
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)

    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)

    assert (tmp_path / str(tenant_id) / ".deleted" / str(user_id)).is_file()


async def test_an_agent_written_marker_filename_does_not_soft_delete_anything(
    tmp_path: Path,
) -> None:
    """沙箱里能写进 ``/workspace`` 的任何文件名都不再具有软删语义 —— 这里用
    store 自己的 write_file 走一遍(端点可达的那条路径),断言三件事:写入
    被接受(marker 搬出树后,那份只在 NAS 侧存在、与 supervisor 后端不对称
    的文件名黑名单随之取消)、字节真的落进用户树、且软删状态并未成立。"""
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)

    await store.write_file(
        tenant_id=tenant_id, user_id=user_id, path=".ew-workspace-deleted", data=b"forged"
    )

    assert (tmp_path / str(tenant_id) / str(user_id) / ".ew-workspace-deleted").is_file()
    assert not (tmp_path / str(tenant_id) / DELETED_DIR).exists()
    # 同理,删它也不再被拒 —— 它就是个名字奇怪的普通文件。
    await store.delete_file(tenant_id=tenant_id, user_id=user_id, path=".ew-workspace-deleted")


async def test_list_files_does_not_hide_a_file_named_like_the_legacy_marker(
    tmp_path: Path,
) -> None:
    """全分支终审复审 New-2:浏览视图对 ``.ew-workspace-deleted`` 不做特判。

    marker 搬出用户树之后这个名字在树里已经没有任何含义,而
    ``SupervisorWorkspaceStore`` 从来没有过这条过滤——留着它就等于同一个
    用户文件在 docker 后端看得见、在 NAS 后端凭空消失。藏用户自己的文件换
    "屏幕上不出现平台样的名字",是这笔交易里更弱的一半。
    """
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    user_root = tmp_path / str(tenant_id) / str(user_id)
    user_root.mkdir(parents=True)
    (user_root / ".ew-workspace-deleted").write_text("")
    (user_root / "out.txt").write_text("x")

    files = await store.list_files(tenant_id=tenant_id, user_id=user_id)

    assert [f.path for f in files] == [".ew-workspace-deleted", "out.txt"]


# ------------------------------------------- 全分支终审 C-2 / M-1:路径归一化单一来源
#
# 守卫比原始字符串、定位走 ``PurePosixPath(...).parts``(会归一化掉 ``./``)——
# 两者对同一输入答案不同,``./`` 前缀因此绕过守卫。修法是让守卫与定位读同一份
# 归一化结果。


async def test_delete_file_rejects_a_dot_slash_prefixed_reserved_path(tmp_path: Path) -> None:
    """``./uploads/a.txt`` 与 ``uploads/a.txt`` 指同一个文件,守卫必须给同一
    个答案 —— 终审实测复现:前者被放行,文件真被删掉。"""
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="uploads/a.txt", data=b"in")

    with pytest.raises(SandboxSupervisorError):
        await store.delete_file(tenant_id=tenant_id, user_id=user_id, path="./uploads/a.txt")

    assert (tmp_path / str(tenant_id) / str(user_id) / "uploads" / "a.txt").is_file()


@pytest.mark.parametrize("path", [".", "./", " . ", ".//"])
async def test_dot_paths_raise_the_store_error_not_a_bare_indexerror(
    tmp_path: Path, path: str
) -> None:
    """``PurePosixPath(".").parts == ()`` → 旧实现 ``parts[-1]`` 抛裸
    ``IndexError`` 越过 store 边界,``/v1/workspace/file`` 只 catch
    ``SandboxSupervisorError`` → 500(supervisor 同输入是 404)。违反模块
    docstring 自己写的"错误类型统一"parity。"""
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)

    with pytest.raises(SandboxSupervisorError):
        await store.read_file(tenant_id=tenant_id, user_id=user_id, path=path)
    with pytest.raises(SandboxSupervisorError):
        await store.write_file(tenant_id=tenant_id, user_id=user_id, path=path, data=b"x")
    with pytest.raises(SandboxSupervisorError):
        await store.delete_file(tenant_id=tenant_id, user_id=user_id, path=path)


async def test_dot_segments_resolve_to_the_same_file_as_the_plain_path(tmp_path: Path) -> None:
    """归一化是单一来源的另一面:``x/./y`` / ``.//x`` 定位到与 ``x/y`` / ``x``
    同一个文件,不产生名字里带 ``.`` 的第二份。"""
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)

    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="x/./y", data=b"v")

    assert await store.read_file(tenant_id=tenant_id, user_id=user_id, path="x/y") == b"v"
    files = await store.list_files(tenant_id=tenant_id, user_id=user_id)
    assert files == [WorkspaceFileEntry(path="x/y", size=1)]


async def test_user_root_mkdir_failure_stays_inside_the_store_error_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-2 —— ``_open_parent_dir_fd`` 里 ``user_root.mkdir`` 没包 try,NAS 根
    忘了 ``chmod 1777`` 时裸 ``PermissionError`` 越过 store 边界 → 上传端点
    500 且不带任何线索(runbook 自己写着"发布后第一次上传文档报 500 先查这
    个")。"""
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    real_mkdir = Path.mkdir

    def _refuse(self: Path, *args: Any, **kwargs: Any) -> None:
        if str(self).startswith(str(tmp_path / str(tenant_id))):
            raise PermissionError(13, "Permission denied")
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _refuse)

    with pytest.raises(SandboxSupervisorError):
        await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a.txt", data=b"x")


async def test_user_root_mkdir_permission_failure_is_a_workspace_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 3 fix round 2(Residual 3)—— 上一条测的是"没越过 store 边界"
    (泛化的 SandboxSupervisorError 也算过),但这恰恰是这整个任务的旗舰诊断
    场景——runbook 第一步让人查的就是这个(NAS 根没 chmod 1777、建不出第一
    个租户子树)。Task 5 靠窄类型 WorkspacePermissionError 才能把它翻成有归
    因的 500;还是泛化类型的话,这条 runbook 场景反而是唯一没拿到新错误类
    型的权限失败。

    复审 N-2 —— 假异常之前是 ``PermissionError(13, "Permission denied")``,
    两个参数,没有 ``filename``;真实的 ``Path.mkdir``/``os.chmod`` 失败会
    自带第三个 ``filename`` 参数,``str(exc)`` 会把它缝进去(实测坐实)。
    只有两个参数的假异常测不出"消息里到底有没有绝对路径"这件事——不管产
    线代码有没有做对,这条断言都会因为假异常本来就没有 filename 而"通
    过"。换成三个参数、真的带 ``filename`` 的假异常,断言才有咬合点。
    """
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    real_mkdir = Path.mkdir

    def _refuse(self: Path, *args: Any, **kwargs: Any) -> None:
        if str(self).startswith(str(tmp_path / str(tenant_id))):
            raise PermissionError(13, "Permission denied", str(self))
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _refuse)

    with pytest.raises(WorkspacePermissionError) as excinfo:
        await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a.txt", data=b"x")

    assert str(tmp_path) not in str(excinfo.value)


# ---------------------------------- 复审 C-1:用户根**自己**(不是子目录/子树)不可穿透
#
# 这是 read_file/write_file/delete_file 三个方法在 _open_parent_dir_fd 里共用的入口
# (list_files 走独立的 os.stat/os.walk,从不调用这个方法——上一版这里写"四个方法"
# 是错的,list_files 那条测试补在这里纯粹是为了四个方法在这个形状上对称覆盖,不是
# 因为它真的经过这段代码,见复审 N-4)——落到这里之前 create=False 的
# read_file/delete_file 完全没碰过上面的 mkdir 分支,是它们第一次、也是唯一一次触
# 到 user_root。之前这句只接 `except OSError`,把 PermissionError 也收成
# _WorkspacePathNotFoundError:read/write 端点翻 404("文件不存在",而它其实读不
# 动),delete_file 更糟——它把 _WorkspacePathNotFoundError 当 rm -f 语义直接吞掉、
# 返回成功,用户看到"删除成功"而文件原封不动地留在盘上。真实触发场景:uid 迁移之
# 后,一个迁移 Job 漏掉的用户根(属主还是旧 uid,新 uid 连 open(O_DIRECTORY) 都过
# 不去)。


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root 无视目录权限位,0o000 依旧穿得透——这条测试的前提在 root 下不成立",
)
async def test_read_file_reports_permission_denied_when_user_root_itself_is_unreadable(
    tmp_path: Path,
) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a.txt", data=b"x")
    user_root = tmp_path / str(tenant_id) / str(user_id)
    os.chmod(user_root, 0o000)
    try:
        with pytest.raises(WorkspacePermissionError):
            await store.read_file(tenant_id=tenant_id, user_id=user_id, path="a.txt")
    finally:
        os.chmod(user_root, 0o700)


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root 无视目录权限位,0o000 依旧穿得透——这条测试的前提在 root 下不成立",
)
async def test_write_file_reports_permission_denied_when_user_root_itself_is_unreadable(
    tmp_path: Path,
) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    user_root = tmp_path / str(tenant_id) / str(user_id)
    user_root.mkdir(parents=True)
    os.chmod(user_root, 0o000)
    try:
        with pytest.raises(WorkspacePermissionError):
            await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a.txt", data=b"x")
    finally:
        os.chmod(user_root, 0o700)


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root 无视目录权限位,0o000 依旧穿得透——这条测试的前提在 root 下不成立",
)
async def test_delete_file_reports_permission_denied_when_user_root_itself_is_unreadable(
    tmp_path: Path,
) -> None:
    """旗舰场景——这条之前会静默"成功"(rm -f 语义把 _WorkspacePathNotFoundError
    当"什么都没有,不用删"),用户看到删除成功,文件其实原封不动地留在盘上。"""
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a.txt", data=b"x")
    user_root = tmp_path / str(tenant_id) / str(user_id)
    os.chmod(user_root, 0o000)
    try:
        with pytest.raises(WorkspacePermissionError):
            await store.delete_file(tenant_id=tenant_id, user_id=user_id, path="a.txt")
    finally:
        os.chmod(user_root, 0o700)
    # 确认真的没删——把上面那句 pytest.raises 去掉、只吞异常的话,这句才是抓
    # "静默假成功" 的最后一道防线。
    assert (user_root / "a.txt").exists()


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root 无视目录权限位,0o000 依旧穿得透——这条测试的前提在 root 下不成立",
)
async def test_list_files_reports_permission_denied_when_user_root_itself_is_unreadable(
    tmp_path: Path,
) -> None:
    """复审 N-4,纠正上一版这里的错误说法—— list_files **不是**靠独立的
    ``os.stat(user_root)`` 分支答对这条。``os.stat`` 只需要祖先目录的搜索
    权限,对**目标自己**的权限位从不敏感——``user_root`` 是 ``0o000`` 时
    ``os.stat(user_root)`` 照样成功(实测坐实),is-dir 检查那半句压根不会
    抛。真正命中的是 ``os.walk`` 对 ``user_root`` 自己的首次 scandir——这一
    步才需要目标目录本身的搜索权限,失败会喂给 ``onerror=_on_walk_error``,
    与 ``test_list_files_reports_permission_denied_for_unreadable_subtree``
    是**同一个**机制,只是这条在 walk 的第 0 层(根自己)触发,那条在第 1
    层(``sub`` 子目录)触发。留着两条是为了跟本节其它三个方法
    (read/write/delete)在"用户根自己不可穿透"这个形状上对称覆盖,不是因
    为 list_files 这里真的走了一条独立代码路径。
    """
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a.txt", data=b"x")
    user_root = tmp_path / str(tenant_id) / str(user_id)
    os.chmod(user_root, 0o000)
    try:
        with pytest.raises(WorkspacePermissionError):
            await store.list_files(tenant_id=tenant_id, user_id=user_id)
    finally:
        os.chmod(user_root, 0o700)


# --------------------------------- 复审 N-1:C-1 同一个坑在深一层(中间路径分量)复现
#
# _open_parent_dir_fd 顶层的 os.open(user_root, ...) 只是入口;真正走多段路径
# (parts[:-1] 非空,即 path 至少两段,如 "sub/a.txt")时,中间分量由下面
# `for component in parts[:-1]` 这个循环里的 _openat_dir 逐段打开——上一轮只修了
# 入口,这个循环原样留着同一个坑:PermissionError 被下面那句宽 `except OSError`
# 收成 _WorkspacePathNotFoundError,delete_file 的 rm -f 语义把它当"本来就没有"
# 直接吞掉、报成功,文件其实原封不动地留在盘上。list_files 不经过这个方法(见上一
# 节),这里只覆盖 read/write/delete 三个方法。


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root 无视目录权限位,0o000 依旧穿得透——这条测试的前提在 root 下不成立",
)
async def test_read_file_reports_permission_denied_for_an_unreadable_intermediate_dir(
    tmp_path: Path,
) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="sub/a.txt", data=b"x")
    sub = tmp_path / str(tenant_id) / str(user_id) / "sub"
    os.chmod(sub, 0o000)
    try:
        with pytest.raises(WorkspacePermissionError):
            await store.read_file(tenant_id=tenant_id, user_id=user_id, path="sub/a.txt")
    finally:
        os.chmod(sub, 0o700)


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root 无视目录权限位,0o000 依旧穿得透——这条测试的前提在 root 下不成立",
)
async def test_write_file_reports_permission_denied_for_an_unreadable_intermediate_dir(
    tmp_path: Path,
) -> None:
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    sub = tmp_path / str(tenant_id) / str(user_id) / "sub"
    sub.mkdir(parents=True)
    os.chmod(sub, 0o000)
    try:
        with pytest.raises(WorkspacePermissionError):
            await store.write_file(
                tenant_id=tenant_id, user_id=user_id, path="sub/a.txt", data=b"x"
            )
    finally:
        os.chmod(sub, 0o700)


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root 无视目录权限位,0o000 依旧穿得透——这条测试的前提在 root 下不成立",
)
async def test_delete_file_reports_permission_denied_for_an_unreadable_intermediate_dir(
    tmp_path: Path,
) -> None:
    """旗舰复现(复审 N-1 原话)—— 这条之前会 ``RETURNED SUCCESS``(rm -f 语
    义把 _WorkspacePathNotFoundError 当"本来就没有"直接吞掉),文件其实原
    封不动地留在盘上;而且事后用 ``read_file`` 核实也会答"不存在"——两个
    诊断都指向错误的结论。
    """
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="sub/a.txt", data=b"x")
    sub = tmp_path / str(tenant_id) / str(user_id) / "sub"
    os.chmod(sub, 0o000)
    try:
        with pytest.raises(WorkspacePermissionError):
            await store.delete_file(tenant_id=tenant_id, user_id=user_id, path="sub/a.txt")
    finally:
        os.chmod(sub, 0o700)
    # 确认真的没删——同用户根那条的最后一道防线。
    assert (sub / "a.txt").exists()


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
        # 抢跑赢没赢都算通过:这个用例断言的是「不逃逸」,不是「一定被
        # 检测到」。O_NOFOLLOW 抓到符号链接就抛,没抓到就落在子树内——
        # 两种都对,唯一不能发生的是下面那条断言里的越界写/删。
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
        # 抢跑赢没赢都算通过:这个用例断言的是「不逃逸」,不是「一定被
        # 检测到」。O_NOFOLLOW 抓到符号链接就抛,没抓到就落在子树内——
        # 两种都对,唯一不能发生的是下面那条断言里的越界写/删。
        pass

    # 关键断言:不逃逸——子树外不相关租户的文件必须原封不动。
    assert outside_victim.exists()
    assert outside_victim.read_text() == "do not delete me"


# --------------------- 全分支终审 M-5:两种此前只有探针验过、没进正式套件的 race 形态
#
# 台账 Task 3 「minor (deferred)」原文:"两种 TOCTOU 形态只有探针验证未进正式
# 测试套件"。探针是一次性脚本,跑完就没了——形态本身进套件才拦得住回归。


async def test_openat_dir_create_then_reopen_gap_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """形态一:``_openat_dir`` 的 create 分支是 ``mkdir`` + 重新 ``open`` 两
    个 syscall,中间有一条真实的窗口。并发写手在这条缝里把刚建出来的目录换
    成指向子树外的符号链接——重新 open 带着 ``O_NOFOLLOW``,必须 fail-closed
    (``ELOOP`` → ``SandboxSupervisorError``),而不是跟过去把载荷写到外面。"""
    tenant_id, user_id = uuid4(), uuid4()
    outside = tmp_path / "outside"
    outside.mkdir()
    store = _store(tmp_path)
    user_root = tmp_path / str(tenant_id) / str(user_id)

    real_mkdir = os.mkdir
    triggered = False

    def _racing_mkdir(path: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal triggered
        real_mkdir(path, *args, **kwargs)
        if not triggered and os.fspath(path) == "sub":
            triggered = True
            # mkdir 已经成功、重新 open 还没发生 —— 正是那条缝。
            (user_root / "sub").rmdir()
            (user_root / "sub").symlink_to(outside)

    monkeypatch.setattr(os, "mkdir", _racing_mkdir)

    with pytest.raises(SandboxSupervisorError):
        await store.write_file(
            tenant_id=tenant_id, user_id=user_id, path="sub/out.txt", data=b"pwned"
        )

    assert not (outside / "out.txt").exists()


async def test_dir_fd_pinning_holds_at_a_deep_intermediate_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """形态二:换的不是第一段中间目录,而是链条**深处**的一段(``a/b/c``
    里的 ``c``)。逐段 openat 对每一段都同样钉死 inode,深度不该改变结论
    ——上一轮"再 resolve 一次"的修法恰恰在这里失效过。"""
    tenant_id, user_id = uuid4(), uuid4()
    outside = tmp_path / "outside"
    outside.mkdir()
    user_root = tmp_path / str(tenant_id) / str(user_id)
    deep = user_root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    store = _store(tmp_path)

    real_open = os.open
    triggered = False

    def _racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal triggered
        if not triggered and os.path.basename(os.fspath(path)) == "out.txt":
            triggered = True
            _swap_dir_for_symlink(deep, outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", _racing_open)

    try:
        await store.write_file(
            tenant_id=tenant_id, user_id=user_id, path="a/b/c/out.txt", data=b"pwned"
        )
    except SandboxSupervisorError:
        # 抢跑赢没赢都算通过:这个用例断言的是「不逃逸」,不是「一定被
        # 检测到」。O_NOFOLLOW 抓到符号链接就抛,没抓到就落在子树内——
        # 两种都对,唯一不能发生的是下面那条断言里的越界写/删。
        pass

    assert not (outside / "out.txt").exists()


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

    assert workspace_deleted_marker(str(tmp_path), tenant_id, user_id).is_file()


async def test_mark_deleted_creates_the_marker_dir_when_missing(tmp_path: Path) -> None:
    """``{root}/{tenant}/.deleted/`` 不存在时由 ``mark_deleted`` 自己带出来。

    0o700 与 ``_ensure_workspace_dir`` 建的用户工作区根现在恰好同值,但理由
    独立:这个目录只有 control-plane 一个写者,且没有任何 subPath 会把它挂
    进沙箱。钉 0o700 是为了让软删这条权威记录靠属主保护,而不是只靠挂载范
    围——万一 subPath 配宽了,沙箱也伪造/清不掉 marker。
    """
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)

    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)

    marker_dir = tmp_path / str(tenant_id) / DELETED_DIR
    assert marker_dir.is_dir()
    assert marker_dir.stat().st_mode & 0o777 == 0o700


async def test_mark_deleted_skips_chmod_for_a_pre_existing_marker_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复审 N-5 —— ``.deleted/`` 已经存在(不是这次 ``mark_deleted`` 调用带
    出来的,比如另一个用户先软删过、或者是 uid 迁移前老 control-plane 建
    的)时,不该碰它的 mode——同 ``_openat_dir``/用户根创建处的既有政策
    (只在真正带入存在时才 chmod),用一个"一被调用就断言失败"的假身份钉
    死"压根不该调它"。
    """
    tenant_id, user_id = uuid4(), uuid4()
    marker_dir = tmp_path / str(tenant_id) / DELETED_DIR
    marker_dir.mkdir(parents=True)  # 模拟这棵目录不是这次调用建的

    def _boom_chmod(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("os.chmod must not run for a pre-existing marker dir")

    monkeypatch.setattr(os, "chmod", _boom_chmod)
    store = _store(tmp_path)

    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)

    assert workspace_deleted_marker(str(tmp_path), tenant_id, user_id).is_file()


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root 无视目录权限位,0o000 依旧穿得透——这条测试的前提在 root 下不成立",
)
async def test_mark_deleted_raises_workspace_permission_error_when_marker_dir_is_foreign(
    tmp_path: Path,
) -> None:
    """复审 N-5 —— ``.deleted/`` 已经存在但当前进程碰不动它(uid 迁移落地
    当天,这棵目录还是老 control-plane 的 uid 建的,新进程既不能 chmod 它
    也不能在里面 touch 文件),之前会被一句不带归因的
    ``SandboxSupervisorError`` 兜底——跟 read/write/delete/list_files 建
    tenant 子树那半边一样,这个预期中的过渡态现在翻成窄类型
    ``WorkspacePermissionError``,不再是一句含混的失败;消息也不带这棵目
    录的绝对路径(同 N-2)。
    """
    tenant_id, user_id = uuid4(), uuid4()
    marker_dir = tmp_path / str(tenant_id) / DELETED_DIR
    marker_dir.mkdir(parents=True)
    os.chmod(marker_dir, 0o000)  # 模拟"不是我们建的,碰不动"
    store = _store(tmp_path)

    try:
        with pytest.raises(WorkspacePermissionError) as excinfo:
            await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)
    finally:
        os.chmod(marker_dir, 0o700)

    assert str(tmp_path) not in str(excinfo.value)


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
    assert workspace_deleted_marker(str(tmp_path), tenant_id, user_id).is_file()


async def test_mark_deleted_skips_teardown_when_no_warm_session(tmp_path: Path) -> None:
    """该用户当下没有热会话(``get_warm`` 返回 ``None``)—— 不该调
    ``destroy``,marker 仍然写。"""
    tenant_id, user_id = uuid4(), uuid4()
    runtime = RecordingSandboxRuntime()
    instance_store = _FakeInstanceStore()
    store = NasWorkspaceStore(root=str(tmp_path), runtime=runtime, instance_store=instance_store)

    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)

    assert runtime.destroyed == []
    assert workspace_deleted_marker(str(tmp_path), tenant_id, user_id).is_file()


async def test_mark_deleted_skips_teardown_without_both_wired(tmp_path: Path) -> None:
    """``runtime``/``instance_store`` 两者只配一个(接线半成品)—— 按"两者都
    没配"同样降级为跳过,不炸 ``AttributeError``(见 ``mark_deleted`` 的
    docstring:这两个字段只由 ``build_workspace_store`` 一起注入,单配一个
    是接线 bug,不该表现成一个看着像文件系统故障的异常)。"""
    tenant_id, user_id, sandbox_id = uuid4(), uuid4(), uuid4()
    instance_store = _FakeInstanceStore(warm={(tenant_id, user_id): (sandbox_id, "e2b-live")})
    store = NasWorkspaceStore(root=str(tmp_path), runtime=None, instance_store=instance_store)

    await store.mark_deleted(tenant_id=tenant_id, user_id=user_id)  # 不抛

    assert workspace_deleted_marker(str(tmp_path), tenant_id, user_id).is_file()


# ---------------------------------------------------------------- 目录不存在


async def test_list_files_missing_user_dir_returns_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    files = await store.list_files(tenant_id=uuid4(), user_id=uuid4())
    assert files == []


async def test_list_files_returns_empty_when_tenant_path_is_not_a_directory(
    tmp_path: Path,
) -> None:
    """``list_files`` 的 is-dir 检查换成 ``stat.S_ISDIR(os.stat(...).st_mode)``
    之后(版本无关,见该方法的实现注释),``ENOTDIR``(路径某个中间分量根本
    不是目录——这里是 ``{tenant_id}/`` 本身是个普通文件,不是"不存在")必须
    继续跟 ``ENOENT`` 一样当"没有"处理、返回空列表——这是 ``Path.is_dir()``
    原本免费提供的行为(它对 ``ENOTDIR`` 也返回 ``False``),换成裸
    ``os.stat`` 之后不会自动继承,得显式接 ``NotADirectoryError``。
    """
    tenant_id, user_id = uuid4(), uuid4()
    (tmp_path / str(tenant_id)).write_text("not a directory")

    store = _store(tmp_path)
    files = await store.list_files(tenant_id=tenant_id, user_id=user_id)

    assert files == []


async def test_list_files_does_not_use_path_is_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复审 M-2 ——
    ``docs/superpowers/plans/2026-08-08-workspace-gid-sharing.md`` Task A
    Step 7 保留清单 item 5(``is_dir()`` 换成
    ``stat.S_ISDIR(os.stat(...).st_mode)``)的分歧只在 Python 3.14 上表现
    出来(见该 fix 的实现注释;已经用真实 3.14.0 解释器验证过)。

    这条修复本身没有被 CI 实际跑的解释器版本(3.12/3.13)钉住:退回
    ``user_root.is_dir()`` 之后在 3.12/3.13 上全套测试依旧绿——包括看起来
    该抓到这个回退的 ``test_list_files_raises_when_tenant_dir_is_unreadable``
    (monkeypatch ``os.stat`` 让它抛 ``PermissionError`` 实测坐实:
    ``Path.is_dir()`` 内部本来就调 ``os.stat``,而 3.12/3.13 的
    ``Path.is_dir()`` 对 ``EACCES`` 本就会重新抛出、不吞掉——那条分歧只存
    在于 3.14,不存在于 CI 实跑的版本,靠断言异常类型/传播行为的测试在
    3.12/3.13 上钉不住"有没有退回 is_dir()"这件事)。

    真正能在**任何**解释器版本上钉住"没有退回用 ``Path.is_dir()``"的,是
    断言调用了哪个函数,不是断言异常传播行为——这里把 ``Path.is_dir`` 换
    成一个一被调用就断言失败的哨兵,配合真实文件系统场景跑一遍,证明
    ``list_files`` 走的是 ``os.stat``,压根没碰 ``is_dir()``。
    """

    def _boom_is_dir(self: Path) -> bool:
        raise AssertionError(
            "list_files must not call Path.is_dir() — Python 3.14 silently "
            "swallows PermissionError inside it, see the fix's docstring"
        )

    monkeypatch.setattr(Path, "is_dir", _boom_is_dir)
    tenant_id, user_id = uuid4(), uuid4()
    store = _store(tmp_path)
    await store.write_file(tenant_id=tenant_id, user_id=user_id, path="a.txt", data=b"x")

    files = await store.list_files(tenant_id=tenant_id, user_id=user_id)

    assert files == [WorkspaceFileEntry(path="a.txt", size=1)]
