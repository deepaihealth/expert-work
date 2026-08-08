"""Stream TE-7 — workspace file primitives (read_file / write_file / list_dir).

Two layers are tested:

1. **In-sandbox snippet logic** — the ``build_*_wrapper`` snippets are
   stdlib-only and take the workspace root as a parameter, so they run
   locally against a ``tmp_path`` to verify real file behaviour: atomic
   write, hashing, UTF-8 handling, and — critically — ``realpath``
   confinement against ``..`` and symlink escape.
2. **Tool orchestration** — ``ReadFileTool`` / ``WriteFileTool`` /
   ``ListDirTool`` parse the JSON envelope from a ``RecordingSandboxRuntime``
   into a ``ToolResult``, map errors to the right exception, and validate
   the orchestrator-side path / arg checks + ToolSpec metadata.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import stat
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.persistence import WORKSPACE_FILE_MODE
from orchestrator.tools import (
    EditFileTool,
    FileOpError,
    ListDirTool,
    ReadFileTool,
    SandboxOutcome,
    ToolBlockedError,
    ToolContext,
    WriteFileTool,
)
from orchestrator.tools.file_ops import (
    build_edit_wrapper,
    build_list_wrapper,
    build_read_wrapper,
    build_write_wrapper,
)
from orchestrator.tools.sandbox import RecordingSandboxRuntime

# --------------------------------------------------------------------------
# Layer 1 — in-sandbox snippet logic (run locally with ws = tmp_path)
# --------------------------------------------------------------------------


def _run_snippet(code: str) -> dict[str, Any]:
    """Execute a self-contained stdlib snippet and parse its JSON envelope."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(code, {})  # noqa: S102 — snippet is built from a fixed template
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    ws = str(tmp_path)
    written = _run_snippet(build_write_wrapper("notes.txt", "hello\nworld", ws=ws))
    assert written["ok"] is True
    assert written["size"] == len(b"hello\nworld")
    expected_hash = hashlib.sha256(b"hello\nworld").hexdigest()
    assert written["content_hash"] == expected_hash
    assert (tmp_path / "notes.txt").read_text() == "hello\nworld"

    read = _run_snippet(build_read_wrapper("notes.txt", cap=1000, ws=ws))
    assert read["ok"] is True
    assert read["content"] == "hello\nworld"
    assert read["content_hash"] == expected_hash
    assert read["truncated"] is False


def test_write_special_chars_roundtrip(tmp_path: Path) -> None:
    ws = str(tmp_path)
    payload = "quote ' double \" back\\slash \t tab 日本語"
    _run_snippet(build_write_wrapper("x.txt", payload, ws=ws))
    read = _run_snippet(build_read_wrapper("x.txt", cap=1000, ws=ws))
    assert read["content"] == payload


def test_write_creates_parent_dirs(tmp_path: Path) -> None:
    ws = str(tmp_path)
    out = _run_snippet(build_write_wrapper("a/b/c.txt", "deep", ws=ws))
    assert out["ok"] is True
    assert (tmp_path / "a" / "b" / "c.txt").read_text() == "deep"


def test_write_overwrites_atomically(tmp_path: Path) -> None:
    ws = str(tmp_path)
    _run_snippet(build_write_wrapper("f.txt", "v1", ws=ws))
    out = _run_snippet(build_write_wrapper("f.txt", "v2", ws=ws))
    assert out["ok"] is True
    assert (tmp_path / "f.txt").read_text() == "v2"
    # No temp file left behind by the atomic rename.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f.txt"]


def test_write_snippet_chmods_the_temp_file_before_rename() -> None:
    """``_atomic_write`` 必须在 ``os.replace`` 之前 chmod。

    ``tempfile.mkstemp`` 恒定 0600(它的安全契约,与 umask 无关),而
    ``os.replace`` 保留源 inode 的权限位 —— 所以不 chmod 的话,经这条路写出
    的**每一个**文件都是 0600,control-plane(另一个 uid)一律读不了。这正是
    W2-BUG-1:前端列得出、下载报 404。

    顺序也是承重的:chmod 必须在 replace **之前**。之后再 chmod 的话,目标
    文件在两个系统调用之间有一个 0600 的可观测窗口,读方正好撞上就是一次
    随机失败。这条测试断的是 snippet 源码文本(snippet 真正的执行发生在
    沙箱里,这条单测跑不到那一步),下面 ``test_atomic_write_lands_group_
    readable`` 补上真跑一遍的行为断言。
    """
    src = build_write_wrapper("a.txt", "x")
    chmod_at = src.index("os.chmod(")
    replace_at = src.index("os.replace(")
    assert chmod_at < replace_at, "chmod 必须在 os.replace 之前"
    # 锚定到真实调用点(而不是裸子串搜索 "0o640")—— 上面那段 WHY 注释的
    # 说明文字里也会提到 mode 相关字样,一个不挂在调用点上的裸子串断言测不
    # 出「chmod 参数被错改成别的数,注释没跟着改」这种情况,是重构无感的
    # 假阳性覆盖。
    assert f"os.chmod(tmp, {oct(WORKSPACE_FILE_MODE)})" in src


def test_atomic_write_lands_group_readable(tmp_path: Path) -> None:
    """真跑一遍 snippet 里的 ``_atomic_write``,断言落地 mode 是 0640。

    上一条测的是源码文本(能防重构把 chmod 删掉),这一条测的是真实文件系统
    行为 —— 两条都要,文本断言挡不住"chmod 了但 mode 写错"。``build_write_
    wrapper`` 本就接受 ``ws`` 参数(不用像 brief 草稿那样对 snippet 源码文本
    做字符串替换),配合既有的 ``_run_snippet`` 直接在本进程内跑。
    """
    out = _run_snippet(build_write_wrapper("out.txt", "hello", ws=str(tmp_path)))
    assert out["ok"] is True
    assert stat.S_IMODE(os.stat(tmp_path / "out.txt").st_mode) == 0o640


def test_read_cap_truncates_content_but_hashes_full(tmp_path: Path) -> None:
    ws = str(tmp_path)
    body = "x" * 100
    _run_snippet(build_write_wrapper("big.txt", body, ws=ws))
    read = _run_snippet(build_read_wrapper("big.txt", cap=10, ws=ws))
    assert read["content"] == "x" * 10
    assert read["truncated"] is True
    assert read["size"] == 100
    assert read["content_hash"] == hashlib.sha256(body.encode()).hexdigest()


def test_read_not_found(tmp_path: Path) -> None:
    out = _run_snippet(build_read_wrapper("missing.txt", cap=100, ws=str(tmp_path)))
    assert out == {"ok": False, "error": "not_found"}


def test_read_binary_unsupported(tmp_path: Path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    out = _run_snippet(build_read_wrapper("blob.bin", cap=100, ws=str(tmp_path)))
    assert out["ok"] is False
    assert out["error"] == "binary_unsupported"


def test_read_is_a_directory(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    out = _run_snippet(build_read_wrapper("sub", cap=100, ws=str(tmp_path)))
    assert out == {"ok": False, "error": "is_a_directory"}


def test_write_to_directory_path_rejected(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    out = _run_snippet(build_write_wrapper("sub", "data", ws=str(tmp_path)))
    assert out == {"ok": False, "error": "is_a_directory"}


def test_traversal_escape_rejected_in_snippet(tmp_path: Path) -> None:
    # Defense in depth: even if a '..' path reached the snippet, realpath
    # confinement rejects it (orchestrator side also rejects up front).
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "secret.txt").write_text("top secret")
    out = _run_snippet(build_read_wrapper("../secret.txt", cap=100, ws=str(ws)))
    assert out == {"ok": False, "error": "path_escapes_workspace"}


def test_symlink_escape_rejected_in_snippet(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("leaked")
    os.symlink(outside, ws / "link.txt")
    out = _run_snippet(build_read_wrapper("link.txt", cap=100, ws=str(ws)))
    assert out == {"ok": False, "error": "path_escapes_workspace"}


def test_symlink_dir_escape_on_write_rejected(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    os.symlink(target, ws / "escape")
    out = _run_snippet(build_write_wrapper("escape/pwn.txt", "x", ws=str(ws)))
    assert out == {"ok": False, "error": "path_escapes_workspace"}
    assert not (target / "pwn.txt").exists()


def test_list_dir_sorted(tmp_path: Path) -> None:
    ws = str(tmp_path)
    (tmp_path / "b.txt").write_text("bb")
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "sub").mkdir()
    out = _run_snippet(build_list_wrapper(".", ws=ws))
    assert out["ok"] is True
    names = [e["name"] for e in out["entries"]]
    assert names == ["a.txt", "b.txt", "sub"]
    by_name = {e["name"]: e for e in out["entries"]}
    assert by_name["a.txt"] == {"name": "a.txt", "is_dir": False, "size": 1}
    assert by_name["sub"]["is_dir"] is True


def test_list_dir_not_found(tmp_path: Path) -> None:
    out = _run_snippet(build_list_wrapper("nope", ws=str(tmp_path)))
    assert out == {"ok": False, "error": "not_found"}


def test_list_dir_not_a_directory(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("x")
    out = _run_snippet(build_list_wrapper("file.txt", ws=str(tmp_path)))
    assert out == {"ok": False, "error": "not_a_directory"}


def test_write_invalid_unicode(tmp_path: Path) -> None:
    # A lone surrogate is a valid str but not UTF-8 encodable (M-1).
    out = _run_snippet(build_write_wrapper("x.txt", "\ud800", ws=str(tmp_path)))
    assert out == {"ok": False, "error": "invalid_unicode"}
    assert not (tmp_path / "x.txt").exists()


def test_nul_in_path_resolves_to_escape(tmp_path: Path) -> None:
    # Even if a NUL reached the snippet, realpath raises ValueError which the
    # confinement guard turns into a denial, not a crash (M-2 defense in depth).
    out = _run_snippet(build_read_wrapper("a\x00b", cap=100, ws=str(tmp_path)))
    assert out == {"ok": False, "error": "path_escapes_workspace"}


def test_read_file_too_large(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("x" * 50)
    out = _run_snippet(build_read_wrapper("big.txt", cap=100, ws=str(tmp_path), max_bytes=10))
    assert out["ok"] is False
    assert out["error"] == "file_too_large"
    assert out["size"] == 50


def test_list_dir_truncates(tmp_path: Path) -> None:
    for name in ("a", "b", "c"):
        (tmp_path / name).write_text("x")
    out = _run_snippet(build_list_wrapper(".", ws=str(tmp_path), max_entries=2))
    assert out["ok"] is True
    assert out["truncated"] is True
    assert len(out["entries"]) == 2


# --- edit_file snippet (TE-9a: exact match + hard CAS) ---


def test_edit_exact_replace(tmp_path: Path) -> None:
    ws = str(tmp_path)
    (tmp_path / "f.txt").write_text("hello world")
    out = _run_snippet(build_edit_wrapper("f.txt", "world", "there", ws=ws))
    assert out["ok"] is True
    assert (tmp_path / "f.txt").read_text() == "hello there"
    assert out["content_hash"] == hashlib.sha256(b"hello there").hexdigest()


def test_edit_empty_new_deletes(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("abcXYZdef")
    out = _run_snippet(build_edit_wrapper("f.txt", "XYZ", "", ws=str(tmp_path)))
    assert out["ok"] is True
    assert (tmp_path / "f.txt").read_text() == "abcdef"


def test_edit_no_match(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hello")
    out = _run_snippet(build_edit_wrapper("f.txt", "absent", "x", ws=str(tmp_path)))
    assert out == {"ok": False, "error": "no_match"}


def test_edit_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("a a a")
    out = _run_snippet(build_edit_wrapper("f.txt", "a", "b", ws=str(tmp_path)))
    assert out["ok"] is False
    assert out["error"] == "ambiguous"
    assert out["count"] == 3


def test_edit_stale_when_hash_mismatch(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("current content")
    out = _run_snippet(
        build_edit_wrapper("f.txt", "current", "new", expected_hash="deadbeef", ws=str(tmp_path))
    )
    assert out["ok"] is False
    assert out["error"] == "stale"
    assert out["current_hash"] == hashlib.sha256(b"current content").hexdigest()
    # File untouched on stale.
    assert (tmp_path / "f.txt").read_text() == "current content"


def test_edit_cas_passes_with_correct_hash(tmp_path: Path) -> None:
    body = "current content"
    (tmp_path / "f.txt").write_text(body)
    good = hashlib.sha256(body.encode()).hexdigest()
    out = _run_snippet(
        build_edit_wrapper("f.txt", "current", "fresh", expected_hash=good, ws=str(tmp_path))
    )
    assert out["ok"] is True
    assert (tmp_path / "f.txt").read_text() == "fresh content"


def test_edit_not_found(tmp_path: Path) -> None:
    out = _run_snippet(build_edit_wrapper("missing.txt", "a", "b", ws=str(tmp_path)))
    assert out == {"ok": False, "error": "not_found"}


def test_edit_binary_unsupported(tmp_path: Path) -> None:
    (tmp_path / "b.bin").write_bytes(b"\xff\xfe")
    out = _run_snippet(build_edit_wrapper("b.bin", "a", "b", ws=str(tmp_path)))
    assert out["ok"] is False
    assert out["error"] == "binary_unsupported"


def test_edit_atomic_no_temp_leftover(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("v1 value")
    _run_snippet(build_edit_wrapper("f.txt", "v1", "v2", ws=str(tmp_path)))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f.txt"]


def test_edit_escape_rejected(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "secret.txt").write_text("top")
    out = _run_snippet(build_edit_wrapper("../secret.txt", "top", "x", ws=str(ws)))
    assert out == {"ok": False, "error": "path_escapes_workspace"}


def test_edit_directory_rejected(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    out = _run_snippet(build_edit_wrapper("sub", "a", "b", ws=str(tmp_path)))
    assert out == {"ok": False, "error": "is_a_directory"}


def test_edit_invalid_unicode_new_string(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hello")
    out = _run_snippet(build_edit_wrapper("f.txt", "hello", "\ud800", ws=str(tmp_path)))
    assert out == {"ok": False, "error": "invalid_unicode"}
    assert (tmp_path / "f.txt").read_text() == "hello"  # untouched


def test_edit_noop_when_old_equals_new(tmp_path: Path) -> None:
    # old == new (occurring once) rewrites identical bytes — documented as ok.
    (tmp_path / "f.txt").write_text("keep this")
    out = _run_snippet(build_edit_wrapper("f.txt", "keep", "keep", ws=str(tmp_path)))
    assert out["ok"] is True
    assert (tmp_path / "f.txt").read_text() == "keep this"


def test_edit_exact_reports_match_field(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hello world")
    out = _run_snippet(build_edit_wrapper("f.txt", "world", "there", ws=str(tmp_path)))
    assert out["match"] == "exact"


# --- TE-9b: whitespace-tolerant fuzzy fallback ---


def test_edit_fuzzy_trailing_whitespace(tmp_path: Path) -> None:
    # old has a trailing space the file lacks → exact fails, fuzzy line match hits.
    (tmp_path / "f.txt").write_text("x = 1\n")
    out = _run_snippet(build_edit_wrapper("f.txt", "x = 1 ", "x = 2", ws=str(tmp_path)))
    assert out["ok"] is True
    assert out["match"] == "fuzzy"
    assert (tmp_path / "f.txt").read_text() == "x = 2\n"


def test_edit_fuzzy_indent_block(tmp_path: Path) -> None:
    # old block is under-indented vs the file → exact fails, fuzzy hits the block.
    (tmp_path / "f.txt").write_text("if x:\n    y = 1\n    z = 2\n")
    out = _run_snippet(
        build_edit_wrapper("f.txt", "  y = 1\n  z = 2", "    y = 1\n    z = 3", ws=str(tmp_path))
    )
    assert out["ok"] is True
    assert out["match"] == "fuzzy"
    assert (tmp_path / "f.txt").read_text() == "if x:\n    y = 1\n    z = 3\n"


def test_edit_fuzzy_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("a=1\nx\na=1\n")
    out = _run_snippet(build_edit_wrapper("f.txt", "a=1 ", "a=2", ws=str(tmp_path)))
    assert out["ok"] is False
    assert out["error"] == "ambiguous"
    # File untouched.
    assert (tmp_path / "f.txt").read_text() == "a=1\nx\na=1\n"


def test_edit_no_match_offers_candidate(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("def calculate_total():\n    pass\n")
    out = _run_snippet(build_edit_wrapper("f.txt", "def calculate_totl():", "x", ws=str(tmp_path)))
    assert out["ok"] is False
    assert out["error"] == "no_match"
    assert "near line 1" in out["detail"]


def test_edit_fuzzy_preserves_crlf(tmp_path: Path) -> None:
    # A uniformly-CRLF file keeps its endings through the fuzzy path.
    (tmp_path / "f.txt").write_bytes(b"x = 1\r\ny = 2\r\n")
    out = _run_snippet(build_edit_wrapper("f.txt", "x = 1 ", "x = 9", ws=str(tmp_path)))
    assert out["ok"] is True
    assert out["match"] == "fuzzy"
    assert (tmp_path / "f.txt").read_bytes() == b"x = 9\r\ny = 2\r\n"


def test_edit_fuzzy_leaves_neighbours_untouched(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("line1\nx=1\nline3\n")
    out = _run_snippet(build_edit_wrapper("f.txt", "x=1 ", "X=1", ws=str(tmp_path)))
    assert out["match"] == "fuzzy"
    assert (tmp_path / "f.txt").read_text() == "line1\nX=1\nline3\n"


def test_edit_fuzzy_new_with_trailing_newline(tmp_path: Path) -> None:
    # new's own trailing newline is inserted verbatim (consistent with exact).
    (tmp_path / "f.txt").write_text("a\nx=1\nb\n")
    out = _run_snippet(build_edit_wrapper("f.txt", "x=1 ", "X=1\n", ws=str(tmp_path)))
    assert out["ok"] is True
    assert (tmp_path / "f.txt").read_text() == "a\nX=1\n\nb\n"


# --------------------------------------------------------------------------
# Layer 2 — tool orchestration (envelope parsing + checks + metadata)
# --------------------------------------------------------------------------


def _ctx(*, tenant_id: UUID | None = None) -> ToolContext:
    return ToolContext(
        tenant_id=tenant_id if tenant_id is not None else uuid4(),
        run_id=uuid4(),
        user_id=uuid4(),
    )


def _client(
    stdout: str = "", *, exit_code: int = 0, timed_out: bool = False
) -> RecordingSandboxRuntime:
    client = RecordingSandboxRuntime()
    client.outcome = SandboxOutcome(
        stdout=stdout, stderr="", exit_code=exit_code, timed_out=timed_out
    )
    return client


async def test_read_file_parses_envelope() -> None:
    env = {"ok": True, "content": "hi", "content_hash": "abc", "size": 2, "truncated": False}
    client = _client(json.dumps(env))
    result = await ReadFileTool(client=client).call({"path": "a.txt"}, ctx=_ctx())
    assert result.content == "hi"
    assert result.meta["content_hash"] == "abc"
    assert result.meta["size"] == 2
    assert result.meta["path"] == "a.txt"
    # Executed exactly one snippet, then released the sandbox.
    assert len(client.execs) == 1
    assert client.released


async def test_write_file_parses_envelope() -> None:
    env = {"ok": True, "content_hash": "deadbeef", "size": 5, "path": "a.txt"}
    client = _client(json.dumps(env))
    result = await WriteFileTool(client=client).call(
        {"path": "a.txt", "content": "hello"}, ctx=_ctx()
    )
    assert "5 bytes" in result.content
    assert result.meta["content_hash"] == "deadbeef"


async def test_list_dir_formats_entries() -> None:
    env = {
        "ok": True,
        "entries": [
            {"name": "a.txt", "is_dir": False, "size": 3},
            {"name": "sub", "is_dir": True, "size": None},
        ],
    }
    client = _client(json.dumps(env))
    result = await ListDirTool(client=client).call({"path": "."}, ctx=_ctx())
    assert "a.txt  (3 bytes)" in result.content
    assert "sub/" in result.content
    assert result.meta["n_entries"] == 2


async def test_path_escape_raises_blocked() -> None:
    client = _client(json.dumps({"ok": False, "error": "path_escapes_workspace"}))
    with pytest.raises(ToolBlockedError):
        await ReadFileTool(client=client).call({"path": "a.txt"}, ctx=_ctx())


async def test_not_found_raises_fileop() -> None:
    client = _client(json.dumps({"ok": False, "error": "not_found"}))
    with pytest.raises(FileOpError, match="not_found"):
        await ReadFileTool(client=client).call({"path": "a.txt"}, ctx=_ctx())


async def test_nonzero_exit_raises_fileop() -> None:
    client = _client("boom", exit_code=1)
    with pytest.raises(FileOpError, match="exit 1"):
        await ReadFileTool(client=client).call({"path": "a.txt"}, ctx=_ctx())


async def test_timed_out_raises_fileop() -> None:
    client = _client("", timed_out=True)
    with pytest.raises(FileOpError, match="timed out"):
        await ReadFileTool(client=client).call({"path": "a.txt"}, ctx=_ctx())


async def test_unparseable_stdout_raises_fileop() -> None:
    client = _client("not json at all")
    with pytest.raises(FileOpError, match="unparseable"):
        await ReadFileTool(client=client).call({"path": "a.txt"}, ctx=_ctx())


async def test_io_error_detail_surfaced() -> None:
    client = _client(json.dumps({"ok": False, "error": "io_error", "detail": "disk full"}))
    with pytest.raises(FileOpError, match=r"io_error \(disk full\)"):
        await ReadFileTool(client=client).call({"path": "a.txt"}, ctx=_ctx())


async def test_non_object_envelope_raises_fileop() -> None:
    client = _client("42")
    with pytest.raises(FileOpError, match="non-object"):
        await ReadFileTool(client=client).call({"path": "a.txt"}, ctx=_ctx())


async def test_empty_dir_formats_empty() -> None:
    client = _client(json.dumps({"ok": True, "entries": []}))
    result = await ListDirTool(client=client).call({"path": "sub"}, ctx=_ctx())
    assert result.content == "sub: (empty)"
    assert result.meta["n_entries"] == 0


async def test_write_content_too_large_rejected() -> None:
    from orchestrator.tools.file_ops import _MAX_WRITE_CHARS

    client = _client(json.dumps({"ok": True, "content_hash": "x", "size": 0, "path": "a"}))
    oversized = "x" * (_MAX_WRITE_CHARS + 1)
    with pytest.raises(ValueError, match="limit"):
        await WriteFileTool(client=client).call({"path": "a.txt", "content": oversized}, ctx=_ctx())


async def test_edit_file_parses_envelope() -> None:
    env = {"ok": True, "content_hash": "newhash", "size": 11, "path": "f.txt"}
    client = _client(json.dumps(env))
    result = await EditFileTool(client=client).call(
        {"path": "f.txt", "old_string": "a", "new_string": "b"}, ctx=_ctx()
    )
    assert "f.txt" in result.content
    assert result.meta["content_hash"] == "newhash"


async def test_edit_file_surfaces_match_kind() -> None:
    env = {"ok": True, "content_hash": "h", "size": 3, "path": "f.txt", "match": "fuzzy"}
    client = _client(json.dumps(env))
    result = await EditFileTool(client=client).call(
        {"path": "f.txt", "old_string": "a", "new_string": "b"}, ctx=_ctx()
    )
    assert result.meta["match"] == "fuzzy"
    assert "fuzzy match" in result.content


async def test_edit_no_match_raises_fileop() -> None:
    client = _client(json.dumps({"ok": False, "error": "no_match"}))
    with pytest.raises(FileOpError, match="no_match"):
        await EditFileTool(client=client).call(
            {"path": "f.txt", "old_string": "a", "new_string": "b"}, ctx=_ctx()
        )


async def test_edit_stale_surfaces_current_hash() -> None:
    env = {"ok": False, "error": "stale", "detail": "current_hash=abc", "current_hash": "abc"}
    client = _client(json.dumps(env))
    with pytest.raises(FileOpError, match=r"stale.*current_hash=abc"):
        await EditFileTool(client=client).call(
            {"path": "f.txt", "old_string": "a", "new_string": "b", "expected_hash": "old"},
            ctx=_ctx(),
        )


async def test_edit_requires_non_empty_old_string() -> None:
    client = _client(json.dumps({"ok": True, "content_hash": "h", "size": 0, "path": "f"}))
    with pytest.raises(ValueError, match="old_string"):
        await EditFileTool(client=client).call(
            {"path": "f.txt", "old_string": "", "new_string": "b"}, ctx=_ctx()
        )


async def test_edit_expected_hash_threaded_into_snippet() -> None:
    client = _client(json.dumps({"ok": True, "content_hash": "h", "size": 1, "path": "f.txt"}))
    await EditFileTool(client=client).call(
        {"path": "f.txt", "old_string": "a", "new_string": "b", "expected_hash": "cafe"},
        ctx=_ctx(),
    )
    code = client.execs[0][1]
    assert '"expected_hash": "cafe"' in code


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "../escape",
        "",
        "  ",
        "a\x00b",
        # The /workspace fold must not open an escape: a second slash or a
        # ``..`` surviving the fold still rejects.
        "/workspace//etc/passwd",
        "/workspace/../etc/passwd",
        "/workspacefoo/x",  # not the root prefix — plain absolute, rejected
    ],
)
async def test_require_path_rejects_bad_paths(bad: str) -> None:
    client = _client(json.dumps({"ok": True, "content": "", "content_hash": "x", "size": 0}))
    with pytest.raises(ValueError, match="path"):
        await ReadFileTool(client=client).call({"path": bad}, ctx=_ctx())


@pytest.mark.parametrize(
    ("raw", "expected_rel"),
    [
        # Models routinely anchor at the documented sandbox root — folded to
        # the workspace-relative path instead of bouncing the call.
        ("/workspace/build_ppt.py", "build_ppt.py"),
        ("/workspace/out/deck.pptx", "out/deck.pptx"),
        ("/workspace", "."),
        ("/workspace/", "."),
    ],
)
async def test_require_path_folds_workspace_root_prefix(raw: str, expected_rel: str) -> None:
    client = _client(json.dumps({"ok": True, "content": "", "content_hash": "x", "size": 0}))
    await ReadFileTool(client=client).call({"path": raw}, ctx=_ctx())
    code = client.execs[0][1]
    assert f'"rel": "{expected_rel}"' in code


async def test_write_requires_content_string() -> None:
    client = _client(json.dumps({"ok": True, "content_hash": "x", "size": 0, "path": "a"}))
    with pytest.raises(ValueError, match="content"):
        await WriteFileTool(client=client).call({"path": "a.txt"}, ctx=_ctx())


async def test_list_dir_defaults_to_dot() -> None:
    client = _client(json.dumps({"ok": True, "entries": []}))
    await ListDirTool(client=client).call({}, ctx=_ctx())
    # The snippet's _PARAMS targets the workspace root by default.
    code = client.execs[0][1]
    assert '"rel": "."' in code


async def test_missing_tenant_blocked() -> None:
    client = _client(json.dumps({"ok": True, "entries": []}))
    ctx = ToolContext(tenant_id=None, run_id=uuid4(), user_id=uuid4())
    with pytest.raises(ToolBlockedError, match="tenant"):
        await ListDirTool(client=client).call({"path": "."}, ctx=ctx)


async def test_user_run_passes_user_automatically() -> None:
    # Durability is automatic: a user-scoped run mounts the user's persistent
    # workspace volume — no manifest flag involved.
    client = _client(json.dumps({"ok": True, "content": "", "content_hash": "x", "size": 0}))
    ctx = _ctx()
    await ReadFileTool(client=client).call({"path": "a.txt"}, ctx=ctx)
    # acquire received the run's user_id (persistent workspace volume).
    assert client.acquired[0][2] == ctx.user_id


async def test_user_less_run_omits_user() -> None:
    client = _client(json.dumps({"ok": True, "content": "", "content_hash": "x", "size": 0}))
    ctx = ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=None)
    await ReadFileTool(client=client).call({"path": "a.txt"}, ctx=ctx)
    assert client.acquired[0][2] is None


def test_specs_metadata() -> None:
    read = ReadFileTool(client=_client()).spec
    assert read.name == "read_file"
    assert read.is_read_only is True
    assert read.resolved_side_effect == "read_only"
    assert read.idempotent is True
    assert read.path_args == ("path",)

    write = WriteFileTool(client=_client()).spec
    assert write.name == "write_file"
    assert write.is_read_only is False
    assert write.resolved_side_effect == "reversible"
    assert write.idempotent is True
    assert write.path_args == ("path",)

    listing = ListDirTool(client=_client()).spec
    assert listing.is_read_only is True
    assert listing.resolved_side_effect == "read_only"
