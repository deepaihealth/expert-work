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

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, NoReturn
from uuid import UUID

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
    SandboxSupervisorError,
    WorkspaceFileNotFoundError,
    WorkspaceFileTooLargeError,
    WorkspaceNotADirectoryError,
    WorkspaceNotAFileError,
    WorkspacePathEscapeError,
    WorkspacePermissionError,
    run_in_sandbox,
)
from orchestrator.tools.sandbox_image_contract import EXEC_VIEW
from orchestrator.tools.workspace_paths import (
    AGENTS_DIR,
    SHARED_PREFIX,
    agent_view_alias,
    resolve_scope,
)
from orchestrator.tools.workspace_scope import store_scope
from orchestrator.tools.workspace_store import (
    DEFAULT_SEARCH_RESULTS,
    WorkspaceSearchResult,
    WorkspaceStore,
)

#: ``shared/`` 目录名 —— 由前缀反推,不写第二份字面量(前缀与目录必须永远同名)。
_SHARED_DIR_NAME = SHARED_PREFIX.rstrip(":")

#: Workspace mount inside the sandbox (see infra/sandbox-image).
#:
#: B-60 —— 视图根,唯一真源是 ``sandbox_image_contract.EXEC_VIEW``:绑了 agent 时
#: ``/workspace`` 就是 agent 目录,没绑时是整个用户根 —— 两种情况片段的 ``ws``
#: 参数都是这同一个值。
_WORKSPACE_ROOT = EXEC_VIEW
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


def _require_path(
    args: Mapping[str, Any], *, tool: str, default: str | None = None, agent_key: str = ""
) -> str:
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
    # B-50 —— ``shared:`` 是显式作用域前缀,不是路径的一部分。剥掉再校验,
    # 校验完由 ``resolve_scope`` 重新识别;剥掉之后的余部它自己也再查一遍 ``..``。
    prefix = ""
    if cleaned.startswith(SHARED_PREFIX):
        prefix, cleaned = SHARED_PREFIX, cleaned[len(SHARED_PREFIX) :].strip()
        if not cleaned:
            msg = f"{tool} requires a non-empty 'path'"
            raise ValueError(msg)
    # B-50 —— 先折**自己**那一段。``list_dir`` 现在回给模型的是 agent 根下的
    # 相对名,但模型也会照着工具描述回传 ``/workspace/agents/<自己>/x``;不先折
    # 这一段,下面的通用 ``/workspace/`` 折叠会留下 ``agents/<自己>/x``,再拼一次
    # agent 根就成了 ``agents/<自己>/agents/<自己>/x``。
    if not prefix and agent_key:
        own = f"{agent_view_alias(agent_key)}/"
        if cleaned == own.rstrip("/"):
            cleaned = "."
        elif cleaned.startswith(own):
            cleaned = cleaned[len(own) :]
    if cleaned in (EXEC_VIEW, EXEC_VIEW + "/"):
        cleaned = "."
    elif cleaned.startswith(EXEC_VIEW + "/"):
        cleaned = cleaned[len(EXEC_VIEW) + 1 :]
    if cleaned.startswith("/") or ".." in PurePosixPath(cleaned).parts:
        msg = f"{tool} path must be a relative workspace path without '..': {raw!r}"
        raise ValueError(msg)
    # B-50 —— ``agents/`` 是布局的保留段,绑了 agent 的调用一律不许拿它寻址。
    # 不拒的话迁移期读回落正好把 ``agents/<别人的 key>/x`` 办成一次合法的跨 agent
    # 读:自己根下找不到 → 回落用户根 → 不偏不倚命中别人的目录。这正是 B-50 要
    # 关掉的那扇门,不能在开门的同一个 PR 里自己留一条缝。
    # B-60 —— ``shared/`` 同样保留:视图里 /workspace/shared 是只读 bind(spec §4.3),裸
    # 相对路径写进去是 EROFS、读则读到 legacy 区。要读就显式 ``shared:``,写一律不许。
    if agent_key and not prefix:
        head = PurePosixPath(cleaned).parts[:1]
        if head == (AGENTS_DIR,):
            msg = (
                f"{tool} path must be relative to your own workspace; "
                f"{AGENTS_DIR!r} is a reserved layout segment: {raw!r}"
            )
            raise ValueError(msg)
        if head == (_SHARED_DIR_NAME,):
            msg = (
                f"{tool}: {_SHARED_DIR_NAME!r} is the read-only shared area — read it with "
                f"the {SHARED_PREFIX!r} prefix; it is not a directory in your workspace: {raw!r}"
            )
            raise ValueError(msg)
    return prefix + cleaned


# ---------------------------------------------------------------------------
# In-sandbox snippets. Each is ``_PARAMS = <repr of json>`` + a fixed,
# stdlib-only body that prints exactly one JSON envelope to stdout. ``_PARAMS``
# is JSON so values (path/content) round-trip as data, and the whole literal
# is ``repr``-embedded so there is no Python-level injection. ``_resolve``
# realpath-confines a relative path to the workspace root, returning ``None``
# on escape (or on any path the OS rejects, e.g. embedded NUL).
# ---------------------------------------------------------------------------

_PRELUDE = """\
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

_ARTIFACT_LOCATE_MAIN = """

def _main():
    full = _resolve(_P["rel"])
    if full is None:
        return {"ok": False, "error": "path_escapes_workspace"}
    if os.path.isdir(full):
        return {"ok": False, "error": "not_a_file"}
    if not os.path.isfile(full):
        return {"ok": False, "error": "not_found"}
    return {"ok": True, "size": os.path.getsize(full)}


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


def build_artifact_locate_wrapper(rel: str, *, ws: str = _WORKSPACE_ROOT) -> str:
    """Snippet ``save_artifact`` runs before registering ``rel``: stat it under ``ws``.

    B-60 —— only looks inside the exec view. The #1551 "claim from the user root"
    branch is gone with the hole it papered over: exec code cannot write to the
    user root any more (spec §4.8). Envelope: ``{"ok": True, "size": N}`` or
    ``{"ok": False, "error": "not_found" | "not_a_file" | "path_escapes_workspace"}``.
    """
    return _snippet({"ws": ws, "rel": rel}, _ARTIFACT_LOCATE_MAIN)


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


async def run_scoped_read(
    client: SandboxRuntime,
    *,
    build: Callable[[str], str],
    ws: str,
    ctx: ToolContext,
    tool: str,
    seed_files: tuple[tuple[str, bytes], ...],
) -> Mapping[str, Any]:
    """在作用域根 ``ws`` 下跑一次读。**只跑一次,不回落。**

    B-50 PR6(Task 14)—— 这里曾经是 ``read_with_legacy_fallback``:agent 根下
    ``not_found`` 时再拿用户根读一次,给存量文件在搬迁脚本跑之前续命。存量搬迁
    跑完并验收通过之后那一跳就成了纯粹的隔离漏洞 —— 它让 agent 读得到用户根上
    的东西,而用户根上剩下的恰恰是**别的 agent 的历史文件**。

    摘掉之后的后果明说:任何**回到旧扁平布局**的工作区(日备恢复,
    ``docs/runbooks/volume-restore.md``)对 agent 直接不可见。处置是恢复完手工
    跑一次搬迁脚本 —— 它是幂等的,随时重跑安全。
    """
    outcome = await run_in_sandbox(
        client,
        code=build(ws),
        timeout_s=None,
        ctx=ctx,
        tool_label=tool,
        fallback_thread_id=tool,
        seed_files=seed_files,
    )
    return parse_envelope(outcome, tool=tool)


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


def _require_workspace_binding(ctx: ToolContext, *, tool: str) -> tuple[UUID, UUID]:
    """The ``(tenant_id, user_id)`` the host-side workspace lives under.

    B-84 —— 宿主侧的工作区按 ``(tenant, user)`` 存(``{root}/{tenant}/{user}/``)。
    一个**没有用户绑定**的 run 在 NAS 上根本没有工作区:它的 ``/workspace`` 是沙箱
    里的一块临时 tmpfs, 随沙箱生灭。

    所以这里**明着拒**, 不回落成"读到一棵空树"。后者会把"这个 run 没有持久工作
    区"伪装成"你的文件不在了" —— 正是 B-84 这一波要消灭的那句误判。措辞照
    ``run_in_sandbox`` 缺租户绑定时的同款写法。
    """
    if ctx.tenant_id is None:
        msg = f"{tool} requires a tenant binding (ctx.tenant_id)"
        raise ToolBlockedError(msg)
    if ctx.user_id is None:
        msg = (
            f"{tool} requires a user binding (ctx.user_id): this run has no persistent "
            "workspace, so there are no stored files to read"
        )
        raise ToolBlockedError(msg)
    return ctx.tenant_id, ctx.user_id


def _raise_for_store_error(exc: SandboxSupervisorError, *, tool: str) -> NoReturn:
    """Map a :class:`WorkspaceStore` failure onto the envelope vocabulary the model knows.

    B-84 —— 读路径挪到宿主之后, 失败不再是沙箱片段打印的 ``{"ok": false, "error":
    ...}``, 而是 store 抛的异常。**模型看到的 kind 必须逐字不变**(``not_found`` /
    ``is_a_directory`` / ``not_a_directory`` / ``file_too_large`` / ``io_error``, 越权
    仍是 :class:`ToolBlockedError`), 所以这里按**类型**分叉, 不按消息文本 —— 文本
    匹配是下一个人改一句文案就静默失效的那种判据。

    窄类型必须排在基类前面:它们都是 :class:`SandboxSupervisorError` 的子类。
    """
    if isinstance(exc, WorkspacePathEscapeError):
        msg = f"{tool}: path escapes the workspace"
        raise ToolBlockedError(msg) from exc
    if isinstance(exc, WorkspaceNotAFileError):
        msg = f"{tool} failed: is_a_directory"
        raise FileOpError(msg) from exc
    if isinstance(exc, WorkspaceNotADirectoryError):
        msg = f"{tool} failed: not_a_directory"
        raise FileOpError(msg) from exc
    if isinstance(exc, WorkspaceFileTooLargeError):
        msg = f"{tool} failed: file_too_large"
        raise FileOpError(msg) from exc
    if isinstance(exc, WorkspaceFileNotFoundError):
        msg = f"{tool} failed: not_found"
        raise FileOpError(msg) from exc
    if isinstance(exc, WorkspacePermissionError):
        # 读不动 != 不存在(W2-BUG-1 的同一条教训)。归到 io_error 而不是
        # not_found, 模型才不会据此断定文件没了。
        msg = f"{tool} failed: io_error (workspace not readable)"
        raise FileOpError(msg) from exc
    msg = f"{tool} failed: io_error ({exc})"
    raise FileOpError(msg) from exc


@dataclass
class ReadFileTool:
    """Read a UTF-8 text file from the agent's workspace (exposed as ``read_file``).

    **B-84 —— 读走宿主 NAS, 不再起沙箱。** control-plane 的 Pod 自己挂着同一卷
    (``infra/k8s/base/control-plane/deployment.yaml`` 的 ``/mnt/workspaces``), 所以
    列目录 / 读文件是一次本地目录操作, 零 sandbox acquire、零 exec。实测 60 天里
    ``list_dir`` 有 **11%(22/207)** 失败, 而那 22 次里每一次都是沙箱创建失败 ——
    一次失败的探测会让模型断定文件不存在, 然后把 21,173 个字符重打一遍。

    **沙箱写 → 宿主读是即时可见的。** 2026-09-20 在测试集群上真测过, 测的就是真实
    的那对客户端(沙箱 microVM 写, control-plane pod 读):新建文件 **8.3 ms** 宿主
    ``listdir`` 就看得见, 改写已有文件 **15.2 ms** 读到新值;两轮都是目录属性缓存与
    文件数据缓存全热的最坏情况。挂载参数里既没有 ``noac`` 也没有显式 ``actimeo``,
    按 NFSv3 默认属性缓存 30~60 秒去推断会得出完全相反的结论。**这里的结论以实测
    为准, 不以推断为准** —— 数据与测法见
    ``docs/superpowers/specs/2026-09-20-workspace-visibility-design.md`` §7b。余量也
    不是压着边跑:模型 ``write_file`` 之后要再调一次 ``read_file``, 中间隔着至少一个
    LLM 往返(秒级), 判据是 8~16 ms 对上秒级。

    **写仍然走沙箱**(``write_file`` / ``edit_file``)—— 它们与 B-60 的私有 ``/w``
    挂载空间和工作区写锁绑着, 挪它是另一个量级的改动。
    """

    store: WorkspaceStore
    output_char_cap: int = DEFAULT_OUTPUT_CHAR_CAP

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="read_file",
            description=(
                "Read a UTF-8 text file from your own workspace and return "
                "its contents plus a content hash (pass the hash to edit_file "
                "for safe concurrent edits). Paths are relative to your own "
                "workspace root. Files belonging to other agents working for "
                "the same user are not reachable. Prefix a path with 'shared:' "
                "to read the shared legacy area (read-only)."
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
        raw = _require_path(args, tool="read_file", agent_key=ctx.agent_key)
        ws, rel = resolve_scope(raw, agent_key=ctx.agent_key, tool="read_file")
        if not PurePosixPath(rel).parts:
            # ``/workspace`` 折成 ``.`` —— 指的是作用域根, 一个目录。沙箱片段过去
            # 在这里回 ``is_a_directory``, 保住同一个 kind:宿主侧的归一化会把 ``.``
            # 当成"不是一条合法文件路径"拒掉, 而那会让模型读到 ToolBlockedError
            # (越权)而不是"你要读的是个目录"。
            msg = "read_file failed: is_a_directory"
            raise FileOpError(msg)
        tenant_id, user_id = _require_workspace_binding(ctx, tool="read_file")
        try:
            data = await self.store.read_file(
                tenant_id=tenant_id,
                user_id=user_id,
                path=rel,
                scope=store_scope(ws, agent_key=ctx.agent_key),
                max_bytes=_MAX_READ_BYTES,
            )
        except SandboxSupervisorError as exc:
            _raise_for_store_error(exc, tool="read_file")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = "read_file failed: binary_unsupported"
            raise FileOpError(msg) from exc
        cap = self.output_char_cap
        return ToolResult(
            content=text[:cap],
            meta={
                "path": rel,
                # 整个文件的 sha256(不是返回给模型的那一截)—— edit_file 的
                # expected_hash CAS 拿它做比对, 截断过的哈希对不上任何东西。
                "content_hash": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "truncated": len(text) > cap,
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
                "Write (create or overwrite) a UTF-8 text file in your own "
                "workspace. The write is atomic. Returns the new content hash. "
                "Paths are relative to your own workspace root; parent "
                "directories are created. Writing to 'shared:' is refused — "
                "that area is read-only."
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
        raw = _require_path(args, tool="write_file", agent_key=ctx.agent_key)
        # 写永不回落 —— 新内容一律落自己的目录,从第一天起就分好。
        ws, rel = resolve_scope(raw, agent_key=ctx.agent_key, tool="write_file")
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
                code=build_write_wrapper(rel, content, ws=ws),
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
    """List a workspace directory (exposed as ``list_dir``).

    B-84 —— 同 :class:`ReadFileTool`:走宿主 NAS, 不起沙箱。理由与那条 NFS 实测结论
    都在那个类的 docstring 里, 不在这里重复一遍(重复的注释会各自漂移)。
    """

    store: WorkspaceStore

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="list_dir",
            description=(
                "List the entries of a directory in your own workspace "
                "(name, is_dir, size). Paths are relative to your own workspace "
                "root and default to it. Files belonging to other agents working "
                "for the same user are not listed here. Prefix a path with "
                "'shared:' to list the shared legacy area (read-only)."
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
        raw = _require_path(args, tool="list_dir", default=".", agent_key=ctx.agent_key)
        ws, rel = resolve_scope(raw, agent_key=ctx.agent_key, tool="list_dir")
        tenant_id, user_id = _require_workspace_binding(ctx, tool="list_dir")
        try:
            listing = await self.store.list_dir(
                tenant_id=tenant_id,
                user_id=user_id,
                scope=store_scope(ws, agent_key=ctx.agent_key),
                path=rel,
                max_entries=_MAX_LIST_ENTRIES,
            )
        except SandboxSupervisorError as exc:
            _raise_for_store_error(exc, tool="list_dir")
        # 渲染与 meta 的形状与 B-84 之前逐字相同 —— 改的是实现与失败率,
        # 对模型可见的语义(参数、返回形状、作用域)一个字不变。
        entries: list[Mapping[str, Any]] = [
            {"name": e.name, "is_dir": e.is_dir, "size": e.size} for e in listing.entries
        ]
        return ToolResult(
            content=_format_entries(rel, entries, truncated=listing.truncated),
            meta={
                "path": rel,
                "entries": entries,
                "n_entries": len(entries),
                "truncated": listing.truncated,
            },
        )


@dataclass
class SearchFilesTool:
    """Find files in the agent's workspace by name and/or content (``search_files``).

    B-84 —— 与 :class:`ListDirTool` 同一条宿主 NAS 读路径, 同一份 NFS 实测结论
    (见 :class:`ReadFileTool` 的 docstring)。分工写进了工具描述里:先 ``list_dir``
    看结构, 要找具体东西用 ``search_files``。
    """

    store: WorkspaceStore
    #: 一次最多回多少条。超了在渲染里明说"还有多少没列出", 不静悄悄少几行。
    max_results: int = DEFAULT_SEARCH_RESULTS

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="search_files",
            description=(
                "Search your own workspace for files by name pattern and/or by "
                "text content. Use list_dir to see what a directory holds; use "
                "this when you know roughly what you are looking for but not "
                "where it is. 'name_glob' matches the file name or the "
                "workspace-relative path (e.g. '*.py', 'style/*.md'); 'content' "
                "is a plain substring searched inside text files (binary files "
                "are skipped). Give at least one of them; giving both means "
                "name matches AND content contains. Results are file paths "
                "relative to your own workspace root - pass one straight to "
                "read_file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name_glob": {
                        "type": "string",
                        "description": "Glob for the file name or path, e.g. '*.py'.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Plain substring to find inside text files.",
                    },
                },
            },
            is_read_only=True,
            side_effect="read_only",
            idempotent=True,
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        name_glob = _optional_str(args, "name_glob", tool="search_files")
        content = _optional_str(args, "content", tool="search_files")
        if name_glob is None and content is None:
            msg = "search_files requires 'name_glob' and/or 'content'"
            raise ValueError(msg)
        # 作用域与 list_dir 完全一致 —— 同一个起点解析, 不另写一份。``.`` 是
        # "本作用域根", search 没有 path 参数, 搜的就是整个作用域。
        ws, _rel = resolve_scope(".", agent_key=ctx.agent_key, tool="search_files")
        tenant_id, user_id = _require_workspace_binding(ctx, tool="search_files")
        try:
            found = await self.store.search_files(
                tenant_id=tenant_id,
                user_id=user_id,
                scope=store_scope(ws, agent_key=ctx.agent_key),
                name_glob=name_glob,
                content=content,
                max_results=self.max_results,
            )
        except SandboxSupervisorError as exc:
            _raise_for_store_error(exc, tool="search_files")
        return ToolResult(
            content=_format_search(found),
            meta={
                "name_glob": name_glob,
                "content": content,
                "paths": [entry.path for entry in found.entries],
                "n_results": len(found.entries),
                "truncated": found.truncated,
            },
        )


def _optional_str(args: Mapping[str, Any], key: str, *, tool: str) -> str | None:
    """``args[key]`` 当字符串取, 空串与缺失都算没给。

    空串不当"给了"是有意的:``name_glob=""`` 谁也匹配不上, 把它当成一个真条件会
    让一次手滑的调用静悄悄回零结果, 而模型读到的是"工作区里没有这种文件"。
    """
    raw = args.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        msg = f"{tool} requires {key!r} to be a string"
        raise ValueError(msg)
    cleaned = raw.strip()
    if "\x00" in cleaned:
        msg = f"{tool} {key!r} must not contain a NUL byte"
        raise ValueError(msg)
    return cleaned or None


def _format_search(found: WorkspaceSearchResult) -> str:
    """Human-readable search result for the LLM.

    截断时**明说还有多少没列出**, 不静悄悄少几行 —— 一个被悄悄截断的列表会被读成
    "就这些了", 而那正是 B-84 要治的误判形态。
    """
    if not found.entries:
        return "(no matching files)"
    lines = [f"{entry.path}  ({entry.size} bytes)" for entry in found.entries]
    if found.truncated:
        lines.append(f"... (more matches not listed; showing the first {len(found.entries)})")
    return "\n".join(lines)


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
                "as stale if the file changed since you read it. Paths are relative "
                "to your own workspace root; editing 'shared:' is refused — that "
                "area is read-only."
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
        raw = _require_path(args, tool="edit_file", agent_key=ctx.agent_key)
        # 写永不回落(edit 也改字节)——「读得到 legacy」不等于「可以就地改它」:
        # 那会把一份归属不明的文件悄悄变成本 agent 的既成事实。
        ws, rel = resolve_scope(raw, agent_key=ctx.agent_key, tool="edit_file")
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
                code=build_edit_wrapper(rel, old, new, expected_hash=expected, ws=ws),
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
