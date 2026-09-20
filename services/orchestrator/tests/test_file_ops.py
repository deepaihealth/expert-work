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
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from orchestrator.tools import (
    EditFileTool,
    FileOpError,
    ListDirTool,
    ReadFileTool,
    SandboxOutcome,
    SearchFilesTool,
    ToolBlockedError,
    ToolContext,
    WriteFileTool,
)
from orchestrator.tools.file_ops import (
    SandboxWorkspaceWriter,
    build_artifact_locate_wrapper,
    build_edit_wrapper,
    build_read_wrapper,
    build_write_wrapper,
)
from orchestrator.tools.sandbox import (
    RecordingSandboxRuntime,
    SandboxSupervisorError,
    WorkspaceFileNotFoundError,
    WorkspaceFileTooLargeError,
    WorkspaceNotADirectoryError,
    WorkspaceNotAFileError,
    WorkspacePathEscapeError,
    WorkspacePermissionError,
)
from orchestrator.tools.workspace_paths import WriteToSharedError
from orchestrator.tools.workspace_store import RecordingWorkspaceStore, WorkspaceFileEntry

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


def _ctx(*, tenant_id: UUID | None = None, agent_key: str = "") -> ToolContext:
    return ToolContext(
        tenant_id=tenant_id if tenant_id is not None else uuid4(),
        run_id=uuid4(),
        user_id=uuid4(),
        agent_key=agent_key,
    )


def _store(
    files: dict[str, bytes] | None = None, *, error: Exception | None = None
) -> RecordingWorkspaceStore:
    """B-84 —— 读走宿主 NAS 之后, read_file / list_dir 的桩是 WorkspaceStore 不是沙箱。

    ``files`` 按**用户根相对**路径给(``agents/<key>/x`` 这种), 因为作用域解析正是
    被测的东西 —— 桩替它把前缀吃掉就什么都验不出来了。``workspace_reads`` 记的是
    作用域拼完之后的那条路径, 断言直接查它:那是 store 真正会去 open 的东西。
    """
    tree = files or {}
    return RecordingWorkspaceStore(
        workspace_files=[
            WorkspaceFileEntry(path=rel, size=len(data)) for rel, data in sorted(tree.items())
        ],
        workspace_file_contents=dict(tree),
        workspace_file_error=error,
        workspace_list_error=error,
    )


def _client(
    stdout: str = "", *, exit_code: int = 0, timed_out: bool = False
) -> RecordingSandboxRuntime:
    client = RecordingSandboxRuntime()
    client.outcome = SandboxOutcome(
        stdout=stdout, stderr="", exit_code=exit_code, timed_out=timed_out
    )
    return client


async def test_read_file_returns_content_and_hash() -> None:
    store = _store({"a.txt": b"hi"})
    result = await ReadFileTool(store=store).call({"path": "a.txt"}, ctx=_ctx())
    assert result.content == "hi"
    # 整个文件的 sha256 —— edit_file 的 expected_hash CAS 拿它比对。
    assert result.meta["content_hash"] == hashlib.sha256(b"hi").hexdigest()
    assert result.meta["size"] == 2
    assert result.meta["path"] == "a.txt"
    assert result.meta["truncated"] is False


async def test_read_file_truncates_at_the_output_cap() -> None:
    store = _store({"a.txt": b"x" * 50})
    result = await ReadFileTool(store=store, output_char_cap=10).call({"path": "a.txt"}, ctx=_ctx())
    assert result.content == "x" * 10
    assert result.meta["truncated"] is True
    # 哈希是**整个文件**的, 不是截断那一截的 —— 截断过的哈希对不上任何东西。
    assert result.meta["content_hash"] == hashlib.sha256(b"x" * 50).hexdigest()
    assert result.meta["size"] == 50


async def test_read_file_rejects_binary() -> None:
    store = _store({"a.bin": b"\xff\xfe\x00"})
    with pytest.raises(FileOpError, match="binary_unsupported"):
        await ReadFileTool(store=store).call({"path": "a.bin"}, ctx=_ctx())


async def test_write_file_parses_envelope() -> None:
    env = {"ok": True, "content_hash": "deadbeef", "size": 5, "path": "a.txt"}
    client = _client(json.dumps(env))
    result = await WriteFileTool(client=client).call(
        {"path": "a.txt", "content": "hello"}, ctx=_ctx()
    )
    assert "5 bytes" in result.content
    assert result.meta["content_hash"] == "deadbeef"


async def test_list_dir_formats_entries() -> None:
    store = _store({"a.txt": b"abc", "sub/b.txt": b"b"})
    result = await ListDirTool(store=store).call({"path": "."}, ctx=_ctx())
    # 渲染与 B-84 之前逐字相同。
    assert "a.txt  (3 bytes)" in result.content
    assert "sub/" in result.content
    assert result.meta["n_entries"] == 2


async def test_path_escape_raises_blocked() -> None:
    """越权是**安全拒绝**(审计记 tool:blocked), 不是模型自己纠得过来的失败。"""
    store = _store(error=WorkspacePathEscapeError("nope"))
    with pytest.raises(ToolBlockedError):
        await ReadFileTool(store=store).call({"path": "a.txt"}, ctx=_ctx())


@pytest.mark.parametrize(
    ("raised", "kind"),
    [
        (WorkspaceFileNotFoundError("x"), "not_found"),
        (WorkspaceNotAFileError("x"), "is_a_directory"),
        (WorkspaceNotADirectoryError("x"), "not_a_directory"),
        (WorkspaceFileTooLargeError("x"), "file_too_large"),
        # 读不动 != 不存在(W2-BUG-1 的教训)—— 归 io_error, 模型才不会据此
        # 断定文件没了, 然后把它重打一遍(这正是 B-84 要治的那条路径)。
        (WorkspacePermissionError("x"), "io_error"),
        (SandboxSupervisorError("boom"), "io_error"),
    ],
)
async def test_store_errors_keep_the_envelope_vocabulary(raised: Exception, kind: str) -> None:
    store = _store(error=raised)
    with pytest.raises(FileOpError, match=kind):
        await ReadFileTool(store=store).call({"path": "a.txt"}, ctx=_ctx())


# 下面四条钉的是 envelope 解析本身。read_file 不再走那条路了(B-84), 但
# write_file / edit_file / read_document 仍然走 —— 所以断言换到 write_file 上,
# 覆盖不丢。
async def test_nonzero_exit_raises_fileop() -> None:
    client = _client("boom", exit_code=1)
    with pytest.raises(FileOpError, match="exit 1"):
        await WriteFileTool(client=client).call({"path": "a.txt", "content": "x"}, ctx=_ctx())


async def test_timed_out_raises_fileop() -> None:
    client = _client("", timed_out=True)
    with pytest.raises(FileOpError, match="timed out"):
        await WriteFileTool(client=client).call({"path": "a.txt", "content": "x"}, ctx=_ctx())


async def test_unparseable_stdout_raises_fileop() -> None:
    client = _client("not json at all")
    with pytest.raises(FileOpError, match="unparseable"):
        await WriteFileTool(client=client).call({"path": "a.txt", "content": "x"}, ctx=_ctx())


async def test_io_error_detail_surfaced() -> None:
    client = _client(json.dumps({"ok": False, "error": "io_error", "detail": "disk full"}))
    with pytest.raises(FileOpError, match=r"io_error \(disk full\)"):
        await WriteFileTool(client=client).call({"path": "a.txt", "content": "x"}, ctx=_ctx())


async def test_non_object_envelope_raises_fileop() -> None:
    client = _client("42")
    with pytest.raises(FileOpError, match="non-object"):
        await WriteFileTool(client=client).call({"path": "a.txt", "content": "x"}, ctx=_ctx())


async def test_empty_dir_formats_empty() -> None:
    store = _store()
    result = await ListDirTool(store=store).call({"path": "sub"}, ctx=_ctx())
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
    with pytest.raises(ValueError, match="path"):
        await ReadFileTool(store=_store()).call({"path": bad}, ctx=_ctx())


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
    store = _store()
    if expected_rel == ".":
        # 折成作用域根 = 一个目录。保住沙箱片段当年的那个 kind。
        with pytest.raises(FileOpError, match="is_a_directory"):
            await ReadFileTool(store=store).call({"path": raw}, ctx=_ctx())
        return
    await ReadFileTool(store=store).call({"path": raw}, ctx=_ctx())
    assert store.workspace_reads[-1][2] == expected_rel


async def test_write_requires_content_string() -> None:
    client = _client(json.dumps({"ok": True, "content_hash": "x", "size": 0, "path": "a"}))
    with pytest.raises(ValueError, match="content"):
        await WriteFileTool(client=client).call({"path": "a.txt"}, ctx=_ctx())


async def test_list_dir_defaults_to_dot() -> None:
    store = _store()
    await ListDirTool(store=store).call({}, ctx=_ctx())
    # 未绑 agent 时作用域根就是用户根 —— 记下的路径是空串。
    assert store.workspace_reads[-1][2] == ""


async def test_missing_tenant_blocked() -> None:
    ctx = ToolContext(tenant_id=None, run_id=uuid4(), user_id=uuid4())
    with pytest.raises(ToolBlockedError, match="tenant"):
        await ListDirTool(store=_store()).call({"path": "."}, ctx=ctx)


async def test_user_run_reads_that_users_workspace() -> None:
    store = _store({"a.txt": b"x"})
    ctx = _ctx()
    await ReadFileTool(store=store).call({"path": "a.txt"}, ctx=ctx)
    assert store.workspace_reads[-1][:2] == (ctx.tenant_id, ctx.user_id)


async def test_user_less_run_is_blocked_not_silently_empty() -> None:
    """B-84 —— 没有用户绑定的 run 在 NAS 上根本没有工作区(它的 /workspace 是沙箱里
    的临时 tmpfs)。明着拒, 不回落成"读到一棵空树" —— 后者会把"这个 run 没有持久
    工作区"伪装成"你的文件不在了", 正是这一波要消灭的那句误判。"""
    ctx = ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=None)
    with pytest.raises(ToolBlockedError, match="user binding"):
        await ReadFileTool(store=_store()).call({"path": "a.txt"}, ctx=ctx)
    with pytest.raises(ToolBlockedError, match="user binding"):
        await ListDirTool(store=_store()).call({"path": "."}, ctx=ctx)


def test_specs_metadata() -> None:
    read = ReadFileTool(store=_store()).spec
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

    listing = ListDirTool(store=_store()).spec
    assert listing.is_read_only is True
    assert listing.resolved_side_effect == "read_only"


# ---------------------------------------------------------------------------
# B-50 Task 7 —— 文件工具按 agent 分层(Task 14 / PR6 已摘掉迁移期读回落)
#
# B-84 —— 读挪到宿主 NAS 之后,断言的对象跟着变:写路径(write/edit)仍然查片段
# 源码里内嵌的 ``ws``,读路径(read_file / list_dir)查 store 记下的那条**用户根
# 相对**路径。后者是 store 真正会去 ``open`` 的东西,比任何中间变量都接近事实,
# 而且它把「作用域前缀拼对了没有」直接摆在断言里 —— 这正是 agent_key 用错(拿
# agent 名去拼)时唯一会露出来的地方,那种错的失败形态是「目录是空的」不是报错。
# ---------------------------------------------------------------------------

# 带上尾随逗号:``_PARAMS`` 是 ``json.dumps`` 的产物,``"ws": "/workspace"``
# 本身是 ``"ws": "/workspace/agents/…"`` 的**子串** —— 不钉逗号的话「断言落在
# 用户根」这件事恒真,测试看着在咬其实没咬。
_USER_WS = '"ws": "/workspace",'
#: B-60 —— 绑了 agent 时 ``ws`` 不再拼 ``agents/<key>``,片段看的就是视图根。值与
#: ``_USER_WS`` 恰好相同(视图对绑没绑都是 ``/workspace``),但断言意图不同 ——
#: 这个名字标的是「绑了 agent 的调用现在也落在这里」,别跟「未绑 agent 走用户根」
#: 混为一谈。
_VIEW_WS = '"ws": "/workspace",'
#: 宿主侧作用域前缀 —— NAS 上的目录名是 ``agent_key``(净化后的名字 + 原名
#: sha256 的前 8 位),**不是 agent 名**。
_AGENT_KEY = "plan-aaaaaaaa"


class _SequenceRuntime(RecordingSandboxRuntime):
    """按顺序吐多个 outcome。

    PR6 摘掉回落之后仍然需要它:第二个 outcome 是「用户根上有这个文件」的诱饵,
    单 outcome 的桩喂不出这个形状,也就证明不了「没去读第二次」。"""

    def __init__(self, stdouts: list[str]) -> None:
        super().__init__()
        self._stdouts = list(stdouts)

    async def exec(  # type: ignore[override]
        self,
        *,
        sandbox_id: UUID,
        code: str,
        timeout_s: int | None,
        agent_key: str = "",
        run_id: UUID | None = None,
    ) -> SandboxOutcome:
        self.execs.append((sandbox_id, code))
        self.exec_agent_keys.append(agent_key)
        self.exec_run_ids.append(run_id)
        stdout = self._stdouts.pop(0) if self._stdouts else ""
        return SandboxOutcome(stdout=stdout, stderr="", exit_code=0, timed_out=False)


_NOT_FOUND = json.dumps({"ok": False, "error": "not_found"})


async def test_read_file_resolves_under_agent_root() -> None:
    """B-84 —— 宿主侧:绑了 agent 的读落在 ``agents/<agent_key>/`` 下。

    断言写全路径而不是「以它开头」:后者在实现把前缀拼成 ``agents/agents/<key>``
    之类的时候照样为真。
    """
    store = _store({f"agents/{_AGENT_KEY}/MEMORY.md": b"hi"})
    result = await ReadFileTool(store=store).call(
        {"path": "MEMORY.md"}, ctx=_ctx(agent_key=_AGENT_KEY)
    )
    assert result.content == "hi"
    assert store.workspace_reads[-1][2] == f"agents/{_AGENT_KEY}/MEMORY.md"


async def test_read_file_without_agent_key_stays_at_user_root() -> None:
    """未绑 agent(空串)= B-50 之前的行为,一字不改。"""
    store = _store({"MEMORY.md": b"hi"})
    result = await ReadFileTool(store=store).call({"path": "MEMORY.md"}, ctx=_ctx())
    assert result.content == "hi"
    assert store.workspace_reads[-1][2] == "MEMORY.md"


async def test_write_file_never_falls_back() -> None:
    """写永远落 agent 目录,即使那里还不存在(片段自己 makedirs)。"""
    client = _SequenceRuntime([_NOT_FOUND, json.dumps({"ok": True, "size": 2})])
    # 第一次就 not_found → 照常抛;关键是**没有**第二次 exec。
    with contextlib.suppress(FileOpError):
        await WriteFileTool(client=client).call(
            {"path": "MEMORY.md", "content": "hi"}, ctx=_ctx(agent_key="plan-aaaaaaaa")
        )
    assert len(client.execs) == 1
    assert _VIEW_WS in client.execs[0][1]


async def test_edit_file_never_falls_back() -> None:
    client = _SequenceRuntime([_NOT_FOUND, json.dumps({"ok": True, "size": 2})])
    with contextlib.suppress(FileOpError):
        await EditFileTool(client=client).call(
            {"path": "MEMORY.md", "old_string": "a", "new_string": "b"},
            ctx=_ctx(agent_key="plan-aaaaaaaa"),
        )
    assert len(client.execs) == 1
    assert _VIEW_WS in client.execs[0][1]


async def test_read_file_never_reaches_the_user_root() -> None:
    """PR6 —— agent 根下 ``not_found`` 就是 ``not_found``,**不许再去用户根捞一次**。

    桩里用户根上**有**同名文件(``MEMORY.md`` 那条),agent 根下没有。回落还在的
    话这条会拿到 ``user root``、变绿 —— 所以断言是「抛错」而不是「内容不对」:
    后者在实现返回空串时也为真。

    摘掉回落之后**用户根上剩下的恰恰是别的 agent 的历史文件**,那一跳就是纯粹的
    跨 agent 读洞。
    """
    store = _store({"MEMORY.md": b"user root"})
    with pytest.raises(FileOpError, match="not_found"):
        await ReadFileTool(store=store).call({"path": "MEMORY.md"}, ctx=_ctx(agent_key=_AGENT_KEY))
    assert store.workspace_reads[-1][2] == f"agents/{_AGENT_KEY}/MEMORY.md"


async def test_list_dir_never_reaches_the_user_root() -> None:
    """同上,列目录这条路也不许回落:用户根上的 ``x.txt`` 不许出现在 agent 的列表里。"""
    store = _store({"x.txt": b"user root"})
    result = await ListDirTool(store=store).call({"path": "."}, ctx=_ctx(agent_key=_AGENT_KEY))
    assert result.meta["n_entries"] == 0
    assert store.workspace_reads[-1][2] == f"agents/{_AGENT_KEY}"


async def test_read_file_without_agent_key_reads_the_user_root_directly() -> None:
    """未绑 agent 的读**本来就**落用户根 —— 那是作用域本身,不是回落。

    这条与上面两条的区别正是 PR6 要保住的边界:没有 agent 身份时用户根就是
    唯一的根;有 agent 身份时用户根**不可达**。
    """
    store = _store({"MEMORY.md": b"user root"})
    result = await ReadFileTool(store=store).call({"path": "MEMORY.md"}, ctx=_ctx())
    assert result.content == "user root"


async def test_shared_prefix_reads_shared_root() -> None:
    store = _store({"shared/style/PLAN_STYLE.md": b"legacy"})
    result = await ReadFileTool(store=store).call(
        {"path": "shared:style/PLAN_STYLE.md"}, ctx=_ctx(agent_key=_AGENT_KEY)
    )
    assert result.content == "legacy"
    assert store.workspace_reads[-1][2] == "shared/style/PLAN_STYLE.md"


async def test_shared_prefix_does_not_fall_back() -> None:
    """``shared:`` 是显式寻址;读不到就是读不到 —— 不许改写成 agent 根下的同名文件。"""
    store = _store({f"agents/{_AGENT_KEY}/x.md": b"wrong"})
    with pytest.raises(FileOpError, match="not_found"):
        await ReadFileTool(store=store).call(
            {"path": "shared:x.md"}, ctx=_ctx(agent_key=_AGENT_KEY)
        )
    assert store.workspace_reads[-1][2] == "shared/x.md"


async def test_write_to_shared_is_refused() -> None:
    with pytest.raises(WriteToSharedError):
        await WriteFileTool(client=_client()).call(
            {"path": "shared:x.md", "content": "no"}, ctx=_ctx(agent_key="plan-aaaaaaaa")
        )


async def test_absolute_agent_path_folds_to_relative() -> None:
    """模型会照抄 ``list_dir`` 的输出回传绝对路径 —— 折掉自己那一段,别退回去。"""
    store = _store({f"agents/{_AGENT_KEY}/MEMORY.md": b"hi"})
    result = await ReadFileTool(store=store).call(
        {"path": f"/workspace/agents/{_AGENT_KEY}/MEMORY.md"},
        ctx=_ctx(agent_key=_AGENT_KEY),
    )
    assert result.content == "hi"
    # 折了一次就够 —— 没折的话会变成 agents/<key>/agents/<key>/MEMORY.md。
    assert store.workspace_reads[-1][2] == f"agents/{_AGENT_KEY}/MEMORY.md"


async def test_another_agents_absolute_path_is_not_folded() -> None:
    """只折自己那一段。别人的 key 折掉就等于把跨 agent 读装成了合法调用。"""
    with pytest.raises(ValueError, match="relative"):
        await ReadFileTool(store=_store()).call(
            {"path": "/workspace/agents/sop-bbbbbbbb/MEMORY.md"},
            ctx=_ctx(agent_key=_AGENT_KEY),
        )


@pytest.mark.parametrize("path", ["shared/x.md", "shared", "/workspace/shared/x.md"])
async def test_bare_shared_segment_is_reserved_when_bound(path: str) -> None:
    """视图里 /workspace/shared 是只读 bind;裸 shared/… 写会 EROFS、读会读到别的东西。
    拒掉并指向 shared: 前缀,而不是静默 io_error。"""
    with pytest.raises(ValueError, match="shared:"):
        await ReadFileTool(store=_store()).call({"path": path}, ctx=_ctx(agent_key=_AGENT_KEY))


async def test_bare_shared_segment_is_plain_when_unbound() -> None:
    store = _store({"shared/x.md": b"plain"})
    result = await ReadFileTool(store=store).call({"path": "shared/x.md"}, ctx=_ctx())
    assert result.content == "plain"
    assert store.workspace_reads[-1][2] == "shared/x.md"


async def test_projection_writer_targets_the_exec_view() -> None:
    """B-60 —— 投影(``threads/<tid>/PLAN.md``)随视图落进 agent 目录:绑了 agent 的进程
    里根本没有用户根可写。删除侧 ``sessions.py`` 与留存 job 早已两处都删
    (``agents/<key>/threads/…`` 与用户根 ``threads/…``),不需要跟着改。"""
    client = _client(json.dumps({"ok": True, "size": 2}))
    writer = SandboxWorkspaceWriter(client=client, ctx=_ctx(agent_key="plan-aaaaaaaa"))
    await writer.write(rel="threads/t1/PLAN.md", content="x")
    assert _VIEW_WS in client.execs[-1][1]


# ---------------------------------------------------------------------------
# save_artifact 的定位片段 —— 真跑在 tmp_path 上。B-60 之后只在视图里 stat,不认领:
# exec 已经写不到用户根,认领分支是带着洞形状的死代码(spec §4.8)。
# ---------------------------------------------------------------------------


def test_locate_finds_a_file_under_ws(tmp_path: Path) -> None:
    (tmp_path / "deck.pptx").write_bytes(b"x" * 7)
    env = _run_snippet(build_artifact_locate_wrapper("deck.pptx", ws=str(tmp_path)))
    assert env == {"ok": True, "size": 7}


def test_locate_reports_not_found_and_never_looks_outside_ws(tmp_path: Path) -> None:
    """用户根上有同名文件、ws 是它的子目录 —— 必须 not_found,不能「找到」。"""
    ws = tmp_path / "agents" / "me-aaaaaaaa"
    ws.mkdir(parents=True)
    (tmp_path / "deck.pptx").write_bytes(b"y")
    env = _run_snippet(build_artifact_locate_wrapper("deck.pptx", ws=str(ws)))
    assert env == {"ok": False, "error": "not_found"}
    assert (tmp_path / "deck.pptx").exists(), "不认领:源文件必须原地不动"


def test_locate_rejects_a_directory(tmp_path: Path) -> None:
    (tmp_path / "outputs").mkdir()
    env = _run_snippet(build_artifact_locate_wrapper("outputs", ws=str(tmp_path)))
    assert env == {"ok": False, "error": "not_a_file"}


def test_locate_escape_is_blocked(tmp_path: Path) -> None:
    env = _run_snippet(build_artifact_locate_wrapper("../../etc/passwd", ws=str(tmp_path)))
    assert env == {"ok": False, "error": "path_escapes_workspace"}


# ---------------------------------------------------------------------------
# B-84 Task 5 —— search_files 工具层
# ---------------------------------------------------------------------------


async def test_search_files_renders_hits() -> None:
    store = _store(
        {
            f"agents/{_AGENT_KEY}/style/render_plan.py": b"def render(plan): ...",
            f"agents/{_AGENT_KEY}/notes.md": b"nothing here",
        }
    )
    result = await SearchFilesTool(store=store).call(
        {"name_glob": "*.py"}, ctx=_ctx(agent_key=_AGENT_KEY)
    )
    assert "style/render_plan.py" in result.content
    assert result.meta["paths"] == ["style/render_plan.py"]
    assert result.meta["n_results"] == 1
    assert result.meta["truncated"] is False


async def test_search_files_says_so_when_nothing_matches() -> None:
    """空结果要说人话 —— 一个空串会被模型读成"工具坏了"。"""
    result = await SearchFilesTool(store=_store({"a.txt": b"x"})).call(
        {"name_glob": "*.py"}, ctx=_ctx()
    )
    assert result.content == "(no matching files)"
    assert result.meta["n_results"] == 0


async def test_search_files_marks_truncation_in_the_text() -> None:
    """截断时**明说还有**, 不静悄悄少几行:被悄悄截断的列表会被读成"就这些了"。"""
    store = _store({f"f{i}.txt": b"needle" for i in range(5)})
    result = await SearchFilesTool(store=store, max_results=2).call(
        {"content": "needle"}, ctx=_ctx()
    )
    assert result.meta["truncated"] is True
    assert "more matches not listed" in result.content


async def test_search_files_requires_a_term() -> None:
    with pytest.raises(ValueError, match="name_glob"):
        await SearchFilesTool(store=_store()).call({}, ctx=_ctx())


async def test_search_files_treats_an_empty_term_as_absent() -> None:
    """``name_glob=""`` 谁也匹配不上 —— 当成真条件会让一次手滑静悄悄回零结果。"""
    with pytest.raises(ValueError, match="name_glob"):
        await SearchFilesTool(store=_store()).call({"name_glob": "  "}, ctx=_ctx())


async def test_search_files_rejects_a_non_string_term() -> None:
    with pytest.raises(ValueError, match="content"):
        await SearchFilesTool(store=_store()).call({"content": 42}, ctx=_ctx())


async def test_search_files_stays_in_the_agent_scope() -> None:
    """别的 agent 与 shared 下的同名文件一条都不许出现。"""
    store = _store(
        {
            f"agents/{_AGENT_KEY}/x.py": b"mine",
            "agents/other-bbbbbbbb/x.py": b"theirs",
            "shared/x.py": b"legacy",
        }
    )
    result = await SearchFilesTool(store=store).call(
        {"name_glob": "x.py"}, ctx=_ctx(agent_key=_AGENT_KEY)
    )
    assert result.meta["paths"] == ["x.py"]


async def test_search_files_needs_a_user_binding() -> None:
    ctx = ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=None)
    with pytest.raises(ToolBlockedError, match="user binding"):
        await SearchFilesTool(store=_store()).call({"name_glob": "*"}, ctx=ctx)


def test_search_files_spec_tells_the_model_how_it_differs_from_list_dir() -> None:
    spec = SearchFilesTool(store=_store()).spec
    assert spec.name == "search_files"
    assert spec.is_read_only is True
    assert spec.resolved_side_effect == "read_only"
    # 分工写进描述里 —— 描述是给模型读的, 不写清楚它就会拿 list_dir 当搜索用。
    assert "list_dir" in spec.description
