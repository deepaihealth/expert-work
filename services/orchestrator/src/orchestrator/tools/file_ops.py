"""Workspace file primitives — Stream TE-7.

``read_file`` / ``write_file`` / ``list_dir`` give the agent *structured*
access to its per-user workspace, instead of pushing every file operation
through the ``bash`` black box (TE-5). Structured tools declare path
metadata for the scheduler (Stream L.L6), a ``side_effect`` level for the
TE-4 gate, and — for ``read_file`` — a ``content_hash`` that TE-9's
optimistic-concurrency ``edit_file`` consumes (expected-hash CAS).

**Execution locus (TE-ADR-2, 2026-06-05 复议)** — these tools ride the
*same* ``exec`` channel as ``bash``: each operation is a small, stdlib-only
Python snippet run via :func:`run_in_sandbox` in the agent's J.15 warm
sandbox. The initial design leaned toward a dedicated supervisor file API,
but that API's backing implementation cold-starts a throwaway container per
call (seconds); riding the warm session is milliseconds and keeps a
read→verify→atomic-rename sequence atomic within a single ``exec``. The
snippet operates on the ``/workspace`` mount and prints one JSON envelope
to stdout, which the tool parses back into a :class:`ToolResult`.

**Snippet construction** — a snippet is ``_PARAMS = <json>`` (the operation
arguments, ``json.dumps``-encoded then embedded as a ``repr()`` Python
string literal — double-escaped, so no Python-level injection regardless of
``path``/``content`` bytes) followed by a fixed, ``stdlib``-only body that
reads ``json.loads(_PARAMS)``. Bodies take the workspace root as a parameter
so the confinement / atomic-write logic is unit-testable against a temp
directory, not only in a live sandbox.

**Safety** — the snippet confines every path to the workspace root via
``os.path.realpath`` (defeats ``..`` traversal *and* symlink escape, which a
``PurePosixPath`` check on the orchestrator side cannot see; a path the OS
rejects, e.g. an embedded NUL, resolves to a confinement denial rather than
a crash). The orchestrator side additionally rejects absolute / ``..`` / NUL
paths up front. ``write_file`` writes atomically (same-dir temp file +
``os.replace``) so a concurrent reader always sees a complete old-or-new
snapshot — this is what lets reads run lock-free under TE-8's per-workspace
write lock. Read / list / write are size-bounded to keep a large
attacker-influenced file from OOM-ing the (per-user) sandbox.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from expert_work.persistence import WORKSPACE_FILE_MODE
from orchestrator.tools.locks import NullWorkspaceLock, WorkspaceLock
from orchestrator.tools.registry import (
    ToolBlockedError,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from orchestrator.tools.sandbox import (
    DEFAULT_OUTPUT_CHAR_CAP,
    SandboxOutcome,
    SandboxRuntime,
    run_in_sandbox,
)

#: Workspace mount inside the sandbox (see infra/sandbox-image).
_WORKSPACE_ROOT = "/workspace"
#: Largest file ``read_file`` will pull into the sandbox (whole file is hashed
#: for TE-9 CAS, so the read can't be capped to the returned slice). Bigger
#: files should use the dedicated ``read_workspace_file`` download path.
_MAX_READ_BYTES = 10 * 1024 * 1024
#: Largest ``write_file`` payload (chars). Bounds the snippet source shipped
#: over the exec channel and the resulting file.
_MAX_WRITE_CHARS = 10 * 1024 * 1024
#: Largest number of directory entries ``list_dir`` returns (sorted prefix;
#: sets ``truncated`` when exceeded).
_MAX_LIST_ENTRIES = 1000


class FileOpError(RuntimeError):
    """A workspace file operation failed for a non-security reason
    (missing file, binary content, I/O error). The ReAct tools node wraps
    it into a ``ToolMessage(status='error')`` (Mini-ADR E-12) so the model
    sees the structured ``error`` kind and self-corrects (re-read, fix path)."""


def _require_path(args: Mapping[str, Any], *, tool: str, default: str | None = None) -> str:
    """Validate the orchestrator-side ``path`` arg: a relative workspace
    path without ``..`` or NUL. Mirrors ``artifact.py:_validate_path`` for a
    consistent contract across file-touching tools; the in-sandbox snippet
    re-checks via ``realpath`` to also defeat symlink escape."""
    raw = args.get("path", default)
    if not isinstance(raw, str):
        msg = f"{tool} requires a 'path' string"
        raise ValueError(msg)
    cleaned = raw.strip()
    if not cleaned:
        msg = f"{tool} requires a non-empty 'path'"
        raise ValueError(msg)
    if "\x00" in cleaned:
        msg = f"{tool} path must not contain a NUL byte"
        raise ValueError(msg)
    # Fold the well-known sandbox root: every tool description anchors paths at
    # /workspace, so models routinely pass "/workspace/foo" — treat that as the
    # workspace-relative "foo" instead of bouncing the call (a reject costs a
    # whole recovery round). Only this exact root folds; any other absolute
    # path (and any ``..`` that survives the fold) still rejects below, and the
    # in-sandbox realpath re-check remains the actual escape boundary.
    if cleaned in ("/workspace", "/workspace/"):
        cleaned = "."
    elif cleaned.startswith("/workspace/"):
        cleaned = cleaned[len("/workspace/") :]
    if cleaned.startswith("/") or ".." in PurePosixPath(cleaned).parts:
        msg = f"{tool} path must be a relative workspace path without '..': {raw!r}"
        raise ValueError(msg)
    return cleaned


# ---------------------------------------------------------------------------
# In-sandbox snippets. Each is ``_PARAMS = <repr of json>`` + a fixed,
# stdlib-only body that prints exactly one JSON envelope to stdout. ``_PARAMS``
# is JSON so values (path/content) round-trip as data, and the whole literal
# is ``repr``-embedded so there is no Python-level injection. ``_resolve``
# realpath-confines a relative path to the workspace root, returning ``None``
# on escape (or on any path the OS rejects, e.g. embedded NUL).
# ---------------------------------------------------------------------------

_PRELUDE = f"""\
import hashlib, json, os, tempfile

_P = json.loads(_PARAMS)
_WS = os.path.realpath(_P["ws"])


def _resolve(rel):
    try:
        full = os.path.realpath(os.path.join(_WS, rel))
    except (ValueError, OSError):
        return None
    if full == _WS or full.startswith(_WS + os.sep):
        return full
    return None


def _atomic_write(full, data):
    parent = os.path.dirname(full) or _WS
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        # mkstemp 恒定落地 0600 —— 这是它的安全契约,与调用方 umask 无关;而
        # os.replace 是纯粹的 rename,不改变目标 inode 的权限位,只会把这个
        # 0600 原样带过去。工作区目录侧写的是 control-plane、读的是沙箱里
        # 另一个 uid 的 agent(或反过来),不在这里放开 group 可读,经这条
        # 路径写出的**每一个**文件都会被对面 uid 拒之门外(W2-BUG-1:前端能
        # 列出 781 字节的 MEMORY.md,下载却 404)。
        #
        # chmod 必须排在 os.replace **之前**:反过来的话,目标文件在两次系统
        # 调用之间会有一个短暂但真实可观测的 0600 窗口 —— 并发读者只要撞上
        # 这个窗口就是一次随机失败,而不是稳定复现的 bug,会更难查。
        #
        # 0o640 的 group 位是承重的:workspace 目录是 setgid(0o2770),新建
        # 文件的 group 自动继承成共享 gid,这里只需要把 group 的 r 位打开即
        # 可让另一侧读到;other 依旧全零。字面量来自
        # ``expert_work.persistence.WORKSPACE_FILE_MODE``(Task 1 钉的唯一
        # 事实源)—— 这段源码要发进沙箱执行,沙箱里的解释器 import 不到
        # ``expert_work.persistence``,所以只能在这里(orchestrator 进程内,
        # 拼接 snippet 源码文本的时刻)把常量值转成字面量插值进去,不能留一个
        # sandbox 内的 import。
        os.chmod(tmp, {oct(WORKSPACE_FILE_MODE)})
        os.replace(tmp, full)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
"""

_READ_MAIN = """

def _main():
    full = _resolve(_P["rel"])
    if full is None:
        return {"ok": False, "error": "path_escapes_workspace"}
    try:
        size = os.path.getsize(full)
    except FileNotFoundError:
        return {"ok": False, "error": "not_found"}
    except OSError as exc:
        return {"ok": False, "error": "io_error", "detail": str(exc)}
    if size > _P["max_bytes"]:
        return {"ok": False, "error": "file_too_large", "size": size}
    try:
        with open(full, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return {"ok": False, "error": "not_found"}
    except IsADirectoryError:
        return {"ok": False, "error": "is_a_directory"}
    except OSError as exc:
        return {"ok": False, "error": "io_error", "detail": str(exc)}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "error": "binary_unsupported", "size": len(data)}
    cap = _P["cap"]
    return {
        "ok": True,
        "content": text[:cap],
        "content_hash": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "truncated": len(text) > cap,
    }


print(json.dumps(_main()))
"""

_WRITE_MAIN = """

def _main():
    full = _resolve(_P["rel"])
    if full is None:
        return {"ok": False, "error": "path_escapes_workspace"}
    if os.path.isdir(full):
        return {"ok": False, "error": "is_a_directory"}
    try:
        data = _P["content"].encode("utf-8")
    except UnicodeEncodeError:
        return {"ok": False, "error": "invalid_unicode"}
    try:
        _atomic_write(full, data)
    except OSError as exc:
        return {"ok": False, "error": "io_error", "detail": str(exc)}
    return {
        "ok": True,
        "content_hash": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "path": _P["rel"],
    }


print(json.dumps(_main()))
"""

_EDIT_MAIN = """

def _fuzzy_line_span(text, old):
    # Unique line range [i, j) in text whose strip-normalized lines equal old's
    # strip-normalized lines, or "ambiguous" / None. strip() ignores BOTH
    # leading indent and trailing whitespace (tolerant of LLM indent drift);
    # the uniqueness gate sends any multi-site match to "ambiguous" so a
    # wrong-indent line can't be silently mass-edited.
    tl = text.split("\\n")
    ol = old.split("\\n")
    if len(ol) > 1 and ol[-1] == "":
        ol = ol[:-1]
    on = [x.strip() for x in ol]
    k = len(on)
    if k == 0:
        return None
    tn = [x.strip() for x in tl]
    hits = [i for i in range(len(tl) - k + 1) if tn[i:i + k] == on]
    if len(hits) == 1:
        return (hits[0], hits[0] + k)
    if len(hits) > 1:
        return "ambiguous"
    return None


def _candidate(text, old):
    import difflib

    ol = [x.strip() for x in old.split("\\n") if x.strip()]
    if not ol:
        return None
    lines = text.split("\\n")
    stripped = [x.strip() for x in lines]
    close = difflib.get_close_matches(ol[0], stripped, n=1, cutoff=0.6)
    if not close:
        return None
    for idx, s in enumerate(stripped):
        if s == close[0]:
            return "near line " + str(idx + 1) + ": " + lines[idx].strip()[:80]
    return None


def _main():
    full = _resolve(_P["rel"])
    if full is None:
        return {"ok": False, "error": "path_escapes_workspace"}
    try:
        with open(full, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return {"ok": False, "error": "not_found"}
    except IsADirectoryError:
        return {"ok": False, "error": "is_a_directory"}
    except OSError as exc:
        return {"ok": False, "error": "io_error", "detail": str(exc)}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "error": "binary_unsupported", "size": len(data)}
    current_hash = hashlib.sha256(data).hexdigest()
    expected = _P.get("expected_hash")
    if expected is not None and expected != current_hash:
        return {
            "ok": False,
            "error": "stale",
            "detail": "current_hash=" + current_hash,
            "current_hash": current_hash,
        }
    old = _P["old"]
    new = _P["new"]
    # Level 1 — exact substring (byte-precise). count / replace share
    # non-overlapping semantics, so count matches what replace() targets.
    count = text.count(old)
    if count > 1:
        return {"ok": False, "error": "ambiguous", "detail": "count=" + str(count), "count": count}
    if count == 1:
        updated = text.replace(old, new, 1)
        match = "exact"
    else:
        # Level 2 — whitespace-normalized line-block fallback (handles LLM
        # indent / trailing-space drift). Replaces the matched line range.
        span = _fuzzy_line_span(text, old)
        if span == "ambiguous":
            return {"ok": False, "error": "ambiguous", "detail": "multiple fuzzy matches"}
        if span is None:
            result = {"ok": False, "error": "no_match"}
            hint = _candidate(text, old)
            if hint:
                result["detail"] = hint
            return result
        i, j = span
        # Preserve a uniformly-CRLF file's endings; mixed / LF rebuild as LF.
        if "\\r\\n" in text and "\\n" not in text.replace("\\r\\n", ""):
            nl = "\\r\\n"
        else:
            nl = "\\n"
        tl = text.split(nl)
        new_lines = new.replace("\\r\\n", "\\n").split("\\n")
        updated = nl.join(tl[:i] + new_lines + tl[j:])
        match = "fuzzy"
    try:
        out = updated.encode("utf-8")
    except UnicodeEncodeError:
        return {"ok": False, "error": "invalid_unicode"}
    try:
        _atomic_write(full, out)
    except OSError as exc:
        return {"ok": False, "error": "io_error", "detail": str(exc)}
    return {
        "ok": True,
        "content_hash": hashlib.sha256(out).hexdigest(),
        "size": len(out),
        "path": _P["rel"],
        "match": match,
    }


print(json.dumps(_main()))
"""

_LIST_MAIN = """

def _main():
    full = _resolve(_P["rel"])
    if full is None:
        return {"ok": False, "error": "path_escapes_workspace"}
    entries = []
    truncated = False
    try:
        with os.scandir(full) as it:
            for entry in it:
                if len(entries) >= _P["max_entries"]:
                    truncated = True
                    break
                try:
                    is_dir = entry.is_dir()
                    size = entry.stat().st_size if entry.is_file() else None
                except OSError:
                    # Broken symlink / racing unlink — degrade gracefully.
                    is_dir = False
                    size = None
                entries.append({"name": entry.name, "is_dir": is_dir, "size": size})
    except FileNotFoundError:
        return {"ok": False, "error": "not_found"}
    except NotADirectoryError:
        return {"ok": False, "error": "not_a_directory"}
    except OSError as exc:
        return {"ok": False, "error": "io_error", "detail": str(exc)}
    entries.sort(key=lambda e: e["name"])
    return {"ok": True, "entries": entries, "truncated": truncated}


print(json.dumps(_main()))
"""


def _snippet(params: Mapping[str, Any], main: str) -> str:
    """Assemble a snippet: ``_PARAMS`` literal + shared prelude + op body."""
    return f"_PARAMS = {json.dumps(params)!r}\n" + _PRELUDE + main


def build_read_wrapper(
    rel: str, *, cap: int, ws: str = _WORKSPACE_ROOT, max_bytes: int = _MAX_READ_BYTES
) -> str:
    """Snippet that reads ``ws/rel`` and prints a JSON read envelope."""
    return _snippet({"ws": ws, "rel": rel, "cap": cap, "max_bytes": max_bytes}, _READ_MAIN)


def build_write_wrapper(rel: str, content: str, *, ws: str = _WORKSPACE_ROOT) -> str:
    """Snippet that atomically writes ``content`` to ``ws/rel``."""
    return _snippet({"ws": ws, "rel": rel, "content": content}, _WRITE_MAIN)


def build_list_wrapper(
    rel: str, *, ws: str = _WORKSPACE_ROOT, max_entries: int = _MAX_LIST_ENTRIES
) -> str:
    """Snippet that lists directory ``ws/rel`` and prints a JSON envelope."""
    return _snippet({"ws": ws, "rel": rel, "max_entries": max_entries}, _LIST_MAIN)


def build_edit_wrapper(
    rel: str,
    old: str,
    new: str,
    *,
    expected_hash: str | None = None,
    ws: str = _WORKSPACE_ROOT,
) -> str:
    """Snippet that replaces an exact substring in ``ws/rel`` (atomic write),
    with an optional ``expected_hash`` compare-and-swap."""
    params: dict[str, Any] = {"ws": ws, "rel": rel, "old": old, "new": new}
    if expected_hash is not None:
        params["expected_hash"] = expected_hash
    return _snippet(params, _EDIT_MAIN)


def parse_envelope(outcome: SandboxOutcome, *, tool: str) -> Mapping[str, Any]:
    """Parse the single JSON envelope the snippet prints to stdout.

    Raises :class:`FileOpError` when the sandbox timed out, the snippet
    crashed (non-zero exit), or stdout isn't the expected JSON object. The
    crash message is deliberately generic — the raw sandbox ``stderr`` (which
    can carry a traceback) is not echoed to the model."""
    if outcome.timed_out:
        msg = f"{tool} timed out"
        raise FileOpError(msg)
    if outcome.exit_code != 0:
        msg = f"{tool} failed in sandbox (exit {outcome.exit_code})"
        raise FileOpError(msg)
    text = outcome.stdout.strip()
    if not text:
        msg = f"{tool} produced no output"
        raise FileOpError(msg)
    try:
        env = json.loads(text.splitlines()[-1])
    except (ValueError, IndexError) as exc:
        msg = f"{tool} produced unparseable output: {exc}"
        raise FileOpError(msg) from exc
    if not isinstance(env, dict):
        msg = f"{tool} produced a non-object envelope"
        raise FileOpError(msg)
    return env


def _raise_for_error(env: Mapping[str, Any], *, tool: str) -> None:
    """Map a ``{"ok": False, ...}`` envelope to the right exception.

    ``path_escapes_workspace`` is a security denial → :class:`ToolBlockedError`
    (audited as ``tool:blocked``); every other kind is an operational error
    the model self-corrects on → :class:`FileOpError`."""
    if env.get("ok"):
        return
    kind = env.get("error", "unknown")
    if kind == "path_escapes_workspace":
        msg = f"{tool}: path escapes the workspace"
        raise ToolBlockedError(msg)
    detail = env.get("detail")
    msg = f"{tool} failed: {kind}" + (f" ({detail})" if detail else "")
    raise FileOpError(msg)


@dataclass
class ReadFileTool:
    """Read a UTF-8 text file from the agent's workspace (exposed as ``read_file``)."""

    client: SandboxRuntime
    output_char_cap: int = DEFAULT_OUTPUT_CHAR_CAP
    #: skill-runtime §5.1 — activated skill files seeded under /opt/skills/<agent_key>/.
    skill_seed_files: tuple[tuple[str, bytes], ...] = ()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="read_file",
            description=(
                "Read a UTF-8 text file from the agent's workspace and return "
                "its contents plus a content hash (pass the hash to edit_file "
                "for safe concurrent edits). Path is relative to /workspace."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file path (no leading '/' or '..').",
                    },
                },
                "required": ["path"],
            },
            is_read_only=True,
            path_args=("path",),
            side_effect="read_only",
            idempotent=True,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        rel = _require_path(args, tool="read_file")
        outcome = await run_in_sandbox(
            self.client,
            code=build_read_wrapper(rel, cap=self.output_char_cap),
            timeout_s=None,
            ctx=ctx,
            tool_label="read_file",
            fallback_thread_id="read_file",
            seed_files=self.skill_seed_files,
        )
        env = parse_envelope(outcome, tool="read_file")
        _raise_for_error(env, tool="read_file")
        return ToolResult(
            content=str(env.get("content", "")),
            meta={
                "path": rel,
                "content_hash": env.get("content_hash"),
                "size": env.get("size"),
                "truncated": bool(env.get("truncated")),
            },
        )


@dataclass
class WriteFileTool:
    """Atomically write a UTF-8 text file in the workspace (exposed as ``write_file``)."""

    client: SandboxRuntime
    #: skill-runtime §5.1 — activated skill files seeded under /opt/skills/<agent_key>/.
    skill_seed_files: tuple[tuple[str, bytes], ...] = ()
    #: Stream TE-8 — cross-replica per-workspace write lock held around the
    #: write exec. Defaults to a no-op (single process / tests).
    workspace_lock: WorkspaceLock = field(default_factory=NullWorkspaceLock)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="write_file",
            description=(
                "Write (create or overwrite) a UTF-8 text file in the agent's "
                "workspace. The write is atomic. Returns the new content hash. "
                "Path is relative to /workspace; parent directories are created."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file path (no leading '/' or '..').",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file contents to write (UTF-8).",
                    },
                },
                "required": ["path", "content"],
            },
            path_args=("path",),
            side_effect="reversible",
            # Overwriting to a fixed content is repeatable with no extra effect.
            idempotent=True,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        rel = _require_path(args, tool="write_file")
        content = args.get("content")
        if not isinstance(content, str):
            msg = "write_file requires a 'content' string"
            raise ValueError(msg)
        if len(content) > _MAX_WRITE_CHARS:
            msg = f"write_file content exceeds the {_MAX_WRITE_CHARS}-character limit"
            raise ValueError(msg)
        # Stream TE-8 — hold the per-workspace write lock around the write exec
        # so concurrent writes (and bash) across replicas serialise. The lock
        # scopes to the run's user (the durable workspace that may be shared);
        # a user-less (ephemeral) run needs no lock.
        async with self.workspace_lock.acquire(tenant_id=ctx.tenant_id, user_id=ctx.user_id):
            outcome = await run_in_sandbox(
                self.client,
                code=build_write_wrapper(rel, content),
                timeout_s=None,
                ctx=ctx,
                tool_label="write_file",
                fallback_thread_id="write_file",
                seed_files=self.skill_seed_files,
            )
        env = parse_envelope(outcome, tool="write_file")
        _raise_for_error(env, tool="write_file")
        size = env.get("size")
        return ToolResult(
            content=f"Wrote {size} bytes to {rel}",
            meta={
                "path": rel,
                "content_hash": env.get("content_hash"),
                "size": size,
            },
        )


@dataclass
class ListDirTool:
    """List a workspace directory (exposed as ``list_dir``)."""

    client: SandboxRuntime
    #: skill-runtime §5.1 — activated skill files seeded under /opt/skills/<agent_key>/.
    skill_seed_files: tuple[tuple[str, bytes], ...] = ()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="list_dir",
            description=(
                "List the entries of a directory in the agent's workspace "
                "(name, is_dir, size). Path is relative to /workspace; "
                "defaults to the workspace root."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative directory path; defaults to '.'.",
                    },
                },
            },
            is_read_only=True,
            path_args=("path",),
            side_effect="read_only",
            idempotent=True,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        rel = _require_path(args, tool="list_dir", default=".")
        outcome = await run_in_sandbox(
            self.client,
            code=build_list_wrapper(rel),
            timeout_s=None,
            ctx=ctx,
            tool_label="list_dir",
            fallback_thread_id="list_dir",
            seed_files=self.skill_seed_files,
        )
        env = parse_envelope(outcome, tool="list_dir")
        _raise_for_error(env, tool="list_dir")
        entries = env.get("entries")
        if not isinstance(entries, list):
            entries = []
        return ToolResult(
            content=_format_entries(rel, entries, truncated=bool(env.get("truncated"))),
            meta={
                "path": rel,
                "entries": entries,
                "n_entries": len(entries),
                "truncated": bool(env.get("truncated")),
            },
        )


@dataclass
class EditFileTool:
    """Replace an exact substring in a workspace text file (exposed as ``edit_file``).

    Optimistic concurrency (TE-9a): with ``expected_hash`` the edit is a hard
    compare-and-swap — rejected as ``stale`` if the file changed since it was
    read. Exact match only here; fuzzy/anchored fallbacks land in TE-9b."""

    client: SandboxRuntime
    #: skill-runtime §5.1 — activated skill files seeded under /opt/skills/<agent_key>/.
    skill_seed_files: tuple[tuple[str, bytes], ...] = ()
    #: Stream TE-8 — write lock held around the edit exec.
    workspace_lock: WorkspaceLock = field(default_factory=NullWorkspaceLock)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="edit_file",
            description=(
                "Replace an exact substring in a workspace text file. 'old_string' "
                "must occur exactly once; if it isn't found exactly, a "
                "whitespace-tolerant line-block match is attempted (ignores indent / "
                "trailing-space drift; that fallback normalizes line endings to LF "
                "unless the file is uniformly CRLF). Optionally pass 'expected_hash' "
                "(from read_file) for a safe compare-and-swap: the edit is rejected "
                "as stale if the file changed since you read it. Path is relative to "
                "/workspace."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file path (no leading '/' or '..').",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to replace; must occur exactly once.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text (may be empty to delete).",
                    },
                    "expected_hash": {
                        "type": "string",
                        "description": "Optional content hash from read_file for compare-and-swap.",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
            path_args=("path",),
            side_effect="reversible",
            # Re-running the same edit fails (old_string no longer present), so
            # it is not idempotent.
            idempotent=False,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        rel = _require_path(args, tool="edit_file")
        old = args.get("old_string")
        if not isinstance(old, str) or old == "":
            msg = "edit_file requires a non-empty 'old_string'"
            raise ValueError(msg)
        new = args.get("new_string")
        if not isinstance(new, str):
            msg = "edit_file requires a 'new_string' string"
            raise ValueError(msg)
        if len(new) > _MAX_WRITE_CHARS:
            msg = f"edit_file new_string exceeds the {_MAX_WRITE_CHARS}-character limit"
            raise ValueError(msg)
        expected = args.get("expected_hash")
        if expected is not None and not isinstance(expected, str):
            msg = "edit_file 'expected_hash' must be a string"
            raise ValueError(msg)
        # Stream TE-8 — lock scopes to the run's user (durable workspace that
        # may be shared across replicas); a user-less run needs no lock.
        async with self.workspace_lock.acquire(tenant_id=ctx.tenant_id, user_id=ctx.user_id):
            outcome = await run_in_sandbox(
                self.client,
                code=build_edit_wrapper(rel, old, new, expected_hash=expected),
                timeout_s=None,
                ctx=ctx,
                tool_label="edit_file",
                fallback_thread_id="edit_file",
                seed_files=self.skill_seed_files,
            )
        env = parse_envelope(outcome, tool="edit_file")
        _raise_for_error(env, tool="edit_file")
        size = env.get("size")
        match = env.get("match")
        suffix = f" ({match} match)" if match else ""
        return ToolResult(
            content=f"Edited {rel} ({size} bytes){suffix}",
            meta={
                "path": rel,
                "content_hash": env.get("content_hash"),
                "size": size,
                "match": match,
            },
        )


@dataclass(frozen=True)
class SandboxWorkspaceWriter:
    """Stream CM-0 — the real ``WorkspaceFileWriter`` for state projection.

    Writes a projected file (``PLAN.md`` / ``TODO.md`` / ``MEMORY.md``) into
    the agent's ``/workspace`` through the warm-sandbox ``write_file`` snippet
    — the only channel with workspace-volume write access (Mini-ADR CM-A1).
    Bound to one run's :class:`ToolContext`; the graph rebuilds it per turn.
    Structurally satisfies ``orchestrator.context.WorkspaceFileWriter``.
    """

    client: SandboxRuntime
    ctx: ToolContext
    #: skill-runtime §5.1 — unused by the projection helpers (they write/read
    #: agent state, not skills); kept so the shared seed plumbing is uniform.
    skill_seed_files: tuple[tuple[str, bytes], ...] = ()

    async def write(self, *, rel: str, content: str) -> None:
        """Atomically write ``content`` to workspace-relative ``rel``. Raises
        :class:`FileOpError` / :class:`ToolBlockedError` on failure — the
        projector swallows those best-effort (Mini-ADR CM-A8).

        The mount follows ``ctx.user_id`` (durability automatic); this carrier's
        *existence* is still gated by the manifest ``persistent_workspace`` flag
        in ``agent_factory`` (CM-0 plan/state projection opt-in)."""
        outcome = await run_in_sandbox(
            self.client,
            code=build_write_wrapper(rel, content),
            timeout_s=None,
            ctx=self.ctx,
            tool_label="workspace_projection",
            fallback_thread_id="workspace_projection",
            seed_files=self.skill_seed_files,
        )
        env = parse_envelope(outcome, tool="workspace_projection")
        _raise_for_error(env, tool="workspace_projection")


@dataclass(frozen=True)
class SandboxWorkspaceReader:
    """Stream CM-0 PR2b — the real ``WorkspaceFileReader`` for state ingest.

    Reads a projected file back from ``/workspace`` through the warm-sandbox
    ``read_file`` snippet (the inverse of :class:`SandboxWorkspaceWriter`).
    Returns ``None`` when the file is absent so the ingester treats it as "no
    edit"; other failures raise (the ingester swallows them best-effort).
    Structurally satisfies ``orchestrator.context.WorkspaceFileReader``."""

    client: SandboxRuntime
    ctx: ToolContext
    #: skill-runtime §5.1 — unused by the projection helpers (they write/read
    #: agent state, not skills); kept so the shared seed plumbing is uniform.
    skill_seed_files: tuple[tuple[str, bytes], ...] = ()

    async def read(self, rel: str) -> str | None:
        # The mount follows ctx.user_id (durability automatic); this carrier's
        # existence stays gated by the manifest flag in agent_factory (CM-0).
        outcome = await run_in_sandbox(
            self.client,
            code=build_read_wrapper(rel, cap=DEFAULT_OUTPUT_CHAR_CAP),
            timeout_s=None,
            ctx=self.ctx,
            tool_label="workspace_ingest",
            fallback_thread_id="workspace_ingest",
            seed_files=self.skill_seed_files,
        )
        env = parse_envelope(outcome, tool="workspace_ingest")
        if not env.get("ok") and env.get("error") == "not_found":
            return None
        _raise_for_error(env, tool="workspace_ingest")
        return str(env.get("content", ""))


def _format_entries(rel: str, entries: list[Mapping[str, Any]], *, truncated: bool) -> str:
    """Human-readable directory listing for the LLM."""
    if not entries:
        return f"{rel}: (empty)"
    lines = []
    for entry in entries:
        marker = "/" if entry.get("is_dir") else ""
        size = entry.get("size")
        suffix = "" if size is None else f"  ({size} bytes)"
        lines.append(f"{entry.get('name')}{marker}{suffix}")
    if truncated:
        lines.append(f"... (truncated at {len(entries)} entries)")
    return "\n".join(lines)
