"""``NasWorkspaceStore`` —— NAS 直读工作区(sandbox migration 波 2 Task 3)。

零 mock:真文件系统(``tmp_path``)驱动每一个用例,包括路径穿越四件套(``..``
相对路径 / 绝对路径 / 逃逸子树的符号链接 / URL 编码字面量)和读写 cap —— 这些
恰恰是 ``_resolve_user_path`` 唯一的存在理由,mock 掉文件系统就等于没测。
行为契约对照 ``sandbox_supervisor.supervisor``(``list_workspace_files`` /
``read_workspace_file`` / ``write_workspace_file`` / ``delete_workspace_file`` /
``mark_workspace_deleted``,``supervisor.py:485-583``)——task-3-brief.md
"语义 parity 清单"一节列了逐方法映射。
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from orchestrator.tools.nas_workspace_store import DELETED_MARKER, NasWorkspaceStore
from orchestrator.tools.sandbox import SandboxSupervisorError
from orchestrator.tools.workspace_store import WorkspaceFileEntry, WorkspaceStore


def _store(root: Path) -> NasWorkspaceStore:
    return NasWorkspaceStore(root=str(root))


def test_satisfies_workspace_store_protocol(tmp_path: Path) -> None:
    assert isinstance(_store(tmp_path), WorkspaceStore)


def test_runtime_field_defaults_to_none(tmp_path: Path) -> None:
    """Task 4 接线用的字段;本 task 恒 ``None``(dataclass 默认值即证明)。"""
    assert _store(tmp_path).runtime is None


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


# ---------------------------------------------------------------- Important 修复回归:
# 初始路径校验(检查时刻)与实际文件操作(使用时刻)之间的 TOCTOU 窗口期,一个
# 共享同一棵树、跑不可信代码的并发写手把中间目录/最终文件换成指向子树外的
# 符号链接。用 monkeypatch 在真实的检查→操作两步之间精确注入这次"race",
# 不依赖真并发(真并发不确定,这里要的是确定性复现)。


async def test_write_file_toctou_symlink_planted_after_initial_check_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审查者复现形态:初始 `_resolve_user_path` 校验通过后(此时 "evil" 目
    录还不存在)、`mkdir` 落盘前的窗口期,并发写手把 "evil" 换成指向子树外
    的符号链接。修复后必须在写入任何字节之前的复验挡住——不能真的把攻击
    载荷写到子树外。"""
    tenant_id, user_id = uuid4(), uuid4()
    outside = tmp_path / "outside"
    outside.mkdir()
    user_root = tmp_path / str(tenant_id) / str(user_id)
    user_root.mkdir(parents=True)  # 预先建好,只剩 "evil" 这一级待建——race 只发生在这一级
    store = _store(tmp_path)

    real_mkdir = Path.mkdir

    def _racing_mkdir(
        self: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if self.name == "evil" and not self.exists():
            # 模拟并发写手在我们的初始校验之后、我们的 mkdir 落盘之前把
            # "evil" 变成一个指向子树外的符号链接。
            self.symlink_to(outside)
            return None
        return real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", _racing_mkdir)

    with pytest.raises(SandboxSupervisorError):
        await store.write_file(
            tenant_id=tenant_id, user_id=user_id, path="evil/out.txt", data=b"pwned"
        )

    # 关键断言:攻击载荷没有真的落到子树外。
    assert not (outside / "out.txt").exists()
    assert list(outside.iterdir()) == []


async def test_read_file_toctou_symlink_planted_after_initial_check_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一族 race,读路径:初始校验时 "a.txt" 是一个合法的普通文件,校验
    通过后、真正打开读取前的窗口期,并发写手把它换成指向子树外的符号链
    接。``O_NOFOLLOW`` 必须在打开这一步挡住,不能把子树外文件的内容读出
    来。"""
    tenant_id, user_id = uuid4(), uuid4()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    user_root = tmp_path / str(tenant_id) / str(user_id)
    user_root.mkdir(parents=True)
    target = user_root / "a.txt"
    target.write_text("legit")  # 初始校验时是普通文件,校验会通过

    store = _store(tmp_path)
    real_open = os.open

    def _racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        p = path if isinstance(path, Path) else Path(str(path))
        if p.name == "a.txt" and p.is_file() and not p.is_symlink():
            p.unlink()
            p.symlink_to(secret)
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", _racing_open)

    with pytest.raises(SandboxSupervisorError):
        await store.read_file(tenant_id=tenant_id, user_id=user_id, path="a.txt")


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


# ---------------------------------------------------------------- 目录不存在


async def test_list_files_missing_user_dir_returns_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    files = await store.list_files(tenant_id=uuid4(), user_id=uuid4())
    assert files == []
