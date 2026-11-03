"""B-262 P3 —— 老对话的平台文件迁进本对话 ``work/``:宿主侧复制(真文件系统)。

复制只碰目录 fd、不跟符号链接、不覆盖已有的名字、先写临时文件再链接进去;上传件按审计 /
登记行记下的 sha256 / 大小核对,对不上(P0 之前被别的客户同名覆盖了)就不复制。整个对话
合计一个字节上限,超出的那一项跳过、记数。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from orchestrator.tools import platform_migration as pm
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.platform_migration import (
    ExpectedUpload,
    copy_platform_files,
    platform_migrated,
    write_migrated_marker,
)

_TENANT, _USER = uuid4(), uuid4()
_KEY = "plan-aaaaaaaa"
_CONV = UUID("11111111-2222-3333-4444-555555555555")


def _agent(root: Path) -> Path:
    return root / str(_TENANT) / str(_USER) / "agents" / _KEY


def _work(root: Path) -> Path:
    return _agent(root) / "threads" / str(_CONV) / "work"


def _put(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _copy(
    root: Path, uploads=(), runs=(), max_bytes=pm.MAX_MIGRATION_BYTES
) -> pm.MigrationCopyReport:
    return copy_platform_files(
        NasWorkspaceStore(root=str(root)),
        _TENANT,
        _USER,
        _KEY,
        _CONV,
        uploads=tuple(uploads),
        run_ids=tuple(runs),
        max_bytes=max_bytes,
    )


def test_own_uploads_are_copied_when_their_content_checks_out(tmp_path: Path) -> None:
    a = _put(_agent(tmp_path) / "uploads" / "a.pdf", b"AAAA")
    _put(_agent(tmp_path) / "uploads" / "b.pdf", b"BBBB")
    os.utime(a, (1_000_000, 1_000_000))

    report = _copy(
        tmp_path,
        uploads=[
            ExpectedUpload(name="a.pdf", sha256s=frozenset({_sha(b"AAAA")})),
            ExpectedUpload(name="b.pdf", sizes=frozenset({4}), sha256s=frozenset({_sha(b"BBBB")})),
        ],
    )

    assert (_work(tmp_path) / "uploads" / "a.pdf").read_bytes() == b"AAAA"
    assert (_work(tmp_path) / "uploads" / "b.pdf").read_bytes() == b"BBBB"
    assert os.stat(_work(tmp_path) / "uploads" / "a.pdf").st_mtime == 1_000_000
    assert (report.uploads_copied, report.files_copied, report.bytes_copied) == (2, 2, 8)
    assert a.exists()  # 源不动:老视图的其它对话、/agent 下都还在


def test_an_upload_without_a_recorded_sha256_is_unverifiable(tmp_path: Path) -> None:
    """B-262 review 3 —— 只知道大小不够:同样大小的别人的件也对得上。没有 sha256 就不复制,
    记成 ``unverifiable``(不挡迁移)。"""
    _put(_agent(tmp_path) / "uploads" / "notes.txt", b"theirs:BBB")

    report = _copy(tmp_path, uploads=[ExpectedUpload(name="notes.txt", sizes=frozenset({10}))])

    assert not (_work(tmp_path) / "uploads" / "notes.txt").exists()
    assert (report.uploads_unverifiable, report.files_copied) == (1, 0)


def test_an_upload_overwritten_by_another_customer_is_not_copied(tmp_path: Path) -> None:
    """P0 之前同名上传会覆盖:agent 根里的 ``report.pdf`` 已是别的客户的了。sha / 大小对不上
    就不复制 —— 宁可缺一份,不把别人的件放进这个对话。"""
    _put(_agent(tmp_path) / "uploads" / "report.pdf", b"another customer")

    report = _copy(
        tmp_path,
        uploads=[
            ExpectedUpload(name="report.pdf", sha256s=frozenset({_sha(b"mine")})),
        ],
    )
    sized = _copy(
        tmp_path,
        uploads=[
            ExpectedUpload(
                name="report.pdf", sizes=frozenset({4}), sha256s=frozenset({_sha(b"mine")})
            )
        ],
    )

    assert not (_work(tmp_path) / "uploads" / "report.pdf").exists()
    assert report.uploads_mismatched == 1 and sized.uploads_mismatched == 1


def test_existing_names_are_never_overwritten(tmp_path: Path) -> None:
    _put(_agent(tmp_path) / "uploads" / "a.pdf", b"AAAA")
    _put(_work(tmp_path) / "uploads" / "a.pdf", b"newer")
    run = uuid4()
    _put(_agent(tmp_path) / ".tool_results" / str(run) / "x.txt", b"old")
    _put(_work(tmp_path) / ".tool_results" / str(run) / "y.txt", b"kept")

    report = _copy(
        tmp_path,
        uploads=[ExpectedUpload(name="a.pdf", sha256s=frozenset({_sha(b"AAAA")}))],
        runs=[run],
    )

    assert (_work(tmp_path) / "uploads" / "a.pdf").read_bytes() == b"newer"
    assert not (_work(tmp_path) / ".tool_results" / str(run) / "x.txt").exists()
    assert (report.uploads_existing, report.runs_existing, report.files_copied) == (1, 1, 0)


def test_own_run_tool_results_are_copied_as_whole_trees(tmp_path: Path) -> None:
    run, other = uuid4(), uuid4()
    base = _agent(tmp_path) / ".tool_results" / str(run)
    _put(base / "c1-bash.txt", b"overflow")
    _put(base / "figures" / ("a" * 16) / ("b" * 16) / "_u3" / "page-3.jpg", b"jpg")
    _put(_agent(tmp_path) / ".tool_results" / str(other) / "secret.txt", b"other conversation")

    report = _copy(tmp_path, runs=[run, uuid4()])

    dst = _work(tmp_path) / ".tool_results"
    assert (dst / str(run) / "c1-bash.txt").read_bytes() == b"overflow"
    assert (dst / str(run) / "figures" / ("a" * 16) / ("b" * 16) / "_u3" / "page-3.jpg").exists()
    assert not (dst / str(other)).exists()
    assert (report.runs_copied, report.runs_missing, report.files_copied) == (1, 1, 2)
    assert not [n for n in os.listdir(dst.parent.parent) if n.startswith(pm.MIGRATE_TMP_PREFIX)]


def test_the_byte_cap_applies_to_tool_results_only(tmp_path: Path) -> None:
    """一个对话合计 ``max_bytes`` 只算工具结果(review 3):本对话的上传不受它限,超了也照复制。
    放不下的 run 整个跳过、记数,其余照常。"""
    _put(_agent(tmp_path) / "uploads" / "big.pdf", b"U" * 20)
    first, second = uuid4(), uuid4()
    _put(_agent(tmp_path) / ".tool_results" / str(first) / "x.txt", b"X" * 6)
    _put(_agent(tmp_path) / ".tool_results" / str(second) / "y.txt", b"Y" * 6)

    report = _copy(
        tmp_path,
        uploads=[ExpectedUpload(name="big.pdf", sha256s=frozenset({_sha(b"U" * 20)}))],
        runs=[first, second],
        max_bytes=10,
    )

    assert (_work(tmp_path) / "uploads" / "big.pdf").exists()
    assert (_work(tmp_path) / ".tool_results" / str(first) / "x.txt").exists()
    assert not (_work(tmp_path) / ".tool_results" / str(second)).exists()
    assert (report.uploads_copied, report.runs_copied, report.runs_over_cap) == (1, 1, 1)
    assert report.bytes_copied == 26


def test_an_upload_that_cannot_be_read_fails_the_copy(tmp_path: Path) -> None:
    """review 3 —— 上传件读不动(不是「不存在」也不是「内容对不上」):整次复制失败,调用方不翻、
    不写标记,下次再试 —— 不能让本对话的上传就这么滞留在 agent 根。"""
    src = _put(_agent(tmp_path) / "uploads" / "a.pdf", b"AAAA")
    src.chmod(0)
    try:
        with pytest.raises(PermissionError):
            _copy(
                tmp_path, uploads=[ExpectedUpload(name="a.pdf", sha256s=frozenset({_sha(b"AAAA")}))]
            )
    finally:
        src.chmod(0o600)


def test_symlinks_are_never_followed(tmp_path: Path) -> None:
    outside = _put(tmp_path / "outside" / "secret.pdf", b"SECRET")
    (_agent(tmp_path) / "uploads").mkdir(parents=True)
    (_agent(tmp_path) / "uploads" / "a.pdf").symlink_to(outside)
    run = uuid4()
    (_agent(tmp_path) / ".tool_results").mkdir(parents=True)
    (_agent(tmp_path) / ".tool_results" / str(run)).symlink_to(outside.parent)

    report = _copy(
        tmp_path,
        uploads=[ExpectedUpload(name="a.pdf", sha256s=frozenset({_sha(b"SECRET")}))],
        runs=[run],
    )

    assert not (_work(tmp_path) / "uploads" / "a.pdf").exists()
    assert not (_work(tmp_path) / ".tool_results" / str(run)).exists()
    assert (report.uploads_missing, report.runs_missing, report.files_copied) == (1, 1, 0)


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", ".hidden", "x\x00y"])
def test_unsafe_upload_names_are_refused(tmp_path: Path, name: str) -> None:
    report = _copy(tmp_path, uploads=[ExpectedUpload(name=name, sha256s=frozenset({"0" * 64}))])
    assert report.uploads_missing == 1 and report.files_copied == 0


def test_the_marker_is_written_once_and_read_back(tmp_path: Path) -> None:
    store = NasWorkspaceStore(root=str(tmp_path))
    assert platform_migrated(store, _TENANT, _USER, _KEY, _CONV) is False
    report = _copy(tmp_path)
    write_migrated_marker(store, _TENANT, _USER, _KEY, _CONV, report)
    write_migrated_marker(store, _TENANT, _USER, _KEY, _CONV, report)
    assert platform_migrated(store, _TENANT, _USER, _KEY, _CONV) is True
    marker = _agent(tmp_path) / "threads" / str(_CONV) / ".platform-migrated"
    assert marker.is_file()


async def test_the_marker_is_not_listed_as_a_workspace_file(tmp_path: Path) -> None:
    """标记是平台状态,不是产出:浏览列表不列它(与 ``.seeded`` 同一处理)。"""
    store = NasWorkspaceStore(root=str(tmp_path))
    _put(_agent(tmp_path) / "notes.md", b"n")
    write_migrated_marker(store, _TENANT, _USER, _KEY, _CONV, _copy(tmp_path))

    paths = [e.path for e in await store.list_files(tenant_id=_TENANT, user_id=_USER)]

    assert f"agents/{_KEY}/notes.md" in paths
    assert not [p for p in paths if p.endswith(pm.MIGRATED_MARKER)]
