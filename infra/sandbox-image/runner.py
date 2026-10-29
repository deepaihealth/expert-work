"""Expert Work sandbox runner — PID 1 inside the ``exec_python`` sandbox container.

Protocol (STREAM-F-DESIGN § 4.2): line-delimited JSON over stdin / stdout.
The Sandbox Supervisor (Stream F.1) attaches to the container's stdio,
writes one request object per line, and reads one response per line:

    → {"code": "<python source>", "timeout_s": 30}
    → {"code": "...", "timeout_s": 30, "envs": {"PYTHONUSERBASE": "/opt/agents/a1"},
       "agent_root": "/mnt/workspace/agents/a1",
       "conv_id": "<uuid>", "thread_id": "<uuid>", "compat_dirs": ["style"]}
    ← {"stdout": "...", "stderr": "...", "exit_code": 0, "timed_out": false}

``envs`` (sandbox migration wave 2, spec 决策 10) is optional and merged onto
the child process's environment — currently just ``PYTHONUSERBASE`` per-agent
isolation, sent by the supervisor's own ``ExecRequest.envs`` field.

``agent_root`` (B-60) is optional too — the agent's directory on the NAS
mount. The runner binds it as ``/workspace`` inside a private mount
namespace it creates for this one exec, so the child's ``/workspace`` is
that agent's directory and no other agent's directory is even there.

``conv_id`` / ``compat_dirs`` (B-236) are optional too: with a ``conv_id``
the child's ``/workspace`` is that conversation's own directory
(``<agent_root>/threads/<conv_id>/work``) and ``/agent`` is the agent
directory; ``thread_id`` is the conversation's own thread (the one
``threads/<id>/`` left visible — every other conversation's directory is
covered in both views); ``compat_dirs`` are agent-directory names mapped into
``/workspace`` during the transition window.

The submitted code runs in a *child* ``python -c`` process rather than in
this interpreter. A child is killable on timeout and isolates a crashing
or ``sys.exit``-ing snippet from the runner loop; gVisor (Stream F.3) is
the actual security boundary, so this split is purely for timeout control
and loop survival, not isolation.

This module is image code — it ships into the sandbox via the Dockerfile
and has no dependency on the rest of the codebase, so it stays a single
self-contained stdlib-only file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TextIO

#: Applied when a request omits ``timeout_s``.
DEFAULT_TIMEOUT_S = 30
#: Hard ceiling — a request asking for more is clamped down. Matches the
#: sandbox-instance lifetime ceiling in subsystem 14.
MAX_TIMEOUT_S = 300
#: Captured stdout / stderr is capped to this many characters before it
#: goes into the JSON response — a transport safety net so a chatty
#: snippet cannot produce an unbounded response line. The exec_python
#: tool applies its own (smaller, LLM-budget) truncation on top.
MAX_OUTPUT_CHARS = 1_000_000

#: B-60 —— 每次 exec 自己的 user+mount namespace 里把 agent 目录 bind 成 /workspace、
#: shared/ 只读挂入、tmpfs 盖掉 /mnt/workspace。**与 orchestrator
#: ``exec_view.EXEC_VIEW_SCRIPT`` 逐字相同**(这个文件是镜像代码,不能 import 仓库;
#: 契约测试 ``test_exec_view_script_matches_the_sandbox_image`` 用 ast 比对两份字面量,
#: 改一边必红)。参数走位置参数:``$1`` = bind 源(空串 = 未绑,bind 整个用户根),
#: ``$2`` = 对话 id(B-236,空串 = 老视图),``$3`` = 本对话线程 id(老视图里留出自己那一格
#: ``threads/<id>/``),``$4`` = 过渡期目录个数,其后是目录名。挂载阶段由脚本内嵌的一段
#: ``/usr/local/bin/python3 -I -S`` 按 fd 完成(``open_tree`` / ``move_mount``,理由见
#: ``exec_view`` 的模块文档),所以镜像必须在这个绝对路径上有 python。它只分隔 NAS 视图:
#: ``/tmp`` / ``$HOME`` / ``/opt/agents`` / ``/opt/skills`` 是这个沙箱里所有 exec(同一用户的
#: 所有 agent 与对话)共用的本地状态,不在它管辖之内(见 B-242)。
_EXEC_VIEW_SCRIPT = """\
set -eu
ew_path="${PATH-}"
PATH=/usr/sbin:/usr/bin:/sbin:/bin
root="$1"; conv="$2"; own="$3"; n="$4"; shift 4
if [ ! -d /agent ]; then
  echo "ew-exec-view: image lacks /agent" >&2
  exit 97
fi
case "$n" in
  ''|*[!0-9]*) echo "ew-exec-view: bad compat count" >&2; exit 98 ;;
esac
if [ "$n" -gt "$#" ]; then
  echo "ew-exec-view: bad compat count" >&2
  exit 98
fi
for id in "$conv" "$own"; do
  case "$id" in
    */*|.|..) echo "ew-exec-view: bad conversation id" >&2; exit 98 ;;
  esac
done
if [ -n "$conv" ]; then
  own="$conv"
fi
i=0
for d in "$@"; do
  [ "$i" -lt "$n" ] || break
  case "$d" in
    ''|.|..|*/*|threads|uploads|inputs|.tool_results|shared|skills|agents)
      echo "ew-exec-view: bad compat dir" >&2; exit 98 ;;
  esac
  i=$((i + 1))
done
/usr/local/bin/python3 -I -S -c '
import ctypes, os, stat, sys

# Every NAS path is reached by an O_NOFOLLOW openat walk and mounted by fd
# (open_tree + move_mount, fsopen/fsmount for tmpfs): a name swapped for a
# symlink after it was opened changes nothing. No external command runs here.
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
libc.mount.argtypes = (
    ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p
)
OPEN_TREE, MOVE_MOUNT, FSOPEN, FSCONFIG, FSMOUNT = 428, 429, 430, 431, 432
CLONE = 0x1 | os.O_CLOEXEC | 0x1000  # OPEN_TREE_CLONE | OPEN_TREE_CLOEXEC | AT_EMPTY_PATH
MOVE = 0x4 | 0x40  # MOVE_MOUNT_F_EMPTY_PATH | MOVE_MOUNT_T_EMPTY_PATH
REMOUNT_RO = 0x20 | 0x1000 | 0x1 | 0x2 | 0x4 | 0x8  # REMOUNT BIND RDONLY NOSUID NODEV NOEXEC
DIR = os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC
LIST = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC
PLATFORM_DIRS = ("uploads", "inputs", ".tool_results")


def sc(nr, *args):
    r = libc.syscall(
        ctypes.c_long(nr), *[ctypes.c_long(a) if isinstance(a, int) else a for a in args]
    )
    if r < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return r


def say(msg):
    sys.stderr.write("ew-exec-view: " + msg + chr(10))


def die(msg):
    say(msg)
    sys.exit(98)


def sub(fd, name, make, flags=DIR):
    if name in ("", ".", "..") or "/" in name:
        raise OSError(22, "bad name")
    try:
        got = os.open(name, flags, dir_fd=fd)
    except FileNotFoundError:
        if not make:
            raise
        try:
            os.mkdir(name, 0o777, dir_fd=fd)
        except FileExistsError:
            pass
        got = os.open(name, flags, dir_fd=fd)
    if not stat.S_ISDIR(os.fstat(got).st_mode):
        os.close(got)
        raise NotADirectoryError(20, "not a directory")
    return got


def attach(tree, target):
    sc(MOVE_MOUNT, tree, b"", target, b"", MOVE)
    return tree


def bind(source, target):
    return attach(sc(OPEN_TREE, source, b"", CLONE), target)


def bind_ro(source, target, what):
    tree = bind(source, target)
    fd_path = ("/proc/self/fd/" + str(tree)).encode()
    if libc.mount(None, fd_path, None, REMOUNT_RO, None) != 0:
        die(what + " could not be made read-only")


def tmpfs(*options):
    fs = sc(FSOPEN, b"tmpfs", 1)
    for key, value in options:
        sc(FSCONFIG, fs, 1, key, value, 0)
    sc(FSCONFIG, fs, 6, None, None, 0)
    mnt = sc(FSMOUNT, fs, 1, 0)
    os.close(fs)
    return mnt


def need(fd, name, label):
    try:
        return sub(fd, name, True)
    except OSError:
        die(label + " is not a plain directory under the agent root")


def hide_threads(view, own_dir, own):
    cover = tmpfs((b"size", b"1k"), (b"mode", b"0500"))
    slot = None
    if own:
        os.mkdir(own, 0o777, dir_fd=cover)
        slot = sub(cover, own, False)
    attach(cover, sub(view, "threads", False))
    if slot is not None:
        bind(own_dir, slot)


def main(root, conv, own, compat):
    nas = os.open("/mnt/workspace", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    agent = os.open("/agent", DIR)
    workspace = os.open("/workspace", DIR)
    if not root:
        bind(nas, agent)
        bind(nas, workspace)
        return
    try:
        head, mid, key = root.rsplit("/", 2)
        base = os.open(head or "/", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        top = sub(sub(base, mid, True), key, True)
    except (OSError, ValueError):
        die("the agent root is a symlink or not a plain directory")
    threads = need(top, "threads", root + "/threads")
    own_dir = need(threads, own, root + "/threads/" + own) if own else None
    work = need(own_dir, "work", root + "/threads/" + own + "/work") if conv else None
    view = bind(top, agent)
    if conv:
        ws = bind(work, workspace)
        for d in PLATFORM_DIRS:
            try:
                source = sub(top, d, True)
            except OSError:
                say(d + " is not a plain directory in the agent root; not mounted")
                continue
            try:
                target = sub(ws, d, True)
            except OSError:
                say("/workspace/" + d + " is not a directory; " + d + "/ not mounted")
                continue
            try:
                bind(source, target)
            except OSError as e:
                say(d + "/ not mounted: " + str(e))
    else:
        ws = bind(top, workspace)
        hide_threads(ws, own_dir, own)
    hide_threads(view, own_dir, own)
    try:
        shared = sub(nas, "shared", False)
    except FileNotFoundError:
        shared = None
    except OSError:
        say("shared is not a plain directory in the user root; shared/ not mounted")
        shared = None
    if shared is not None:
        try:
            target = sub(ws, "shared", True)
        except OSError:
            say("/workspace/shared is not a directory; shared/ not mounted")
        else:
            bind_ro(shared, target, "shared/")
    for d in compat if conv else ():
        try:
            source = sub(top, d, False)
            target = sub(ws, d, True, LIST)
            if os.listdir(target):
                continue
        except OSError:
            continue
        try:
            bind_ro(source, target, d + "/")
        except OSError as e:
            say(d + "/ not mounted: " + str(e))
    attach(tmpfs((b"size", b"1k")), nas)


try:
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[5 : 5 + int(sys.argv[4])])
except OSError as e:
    die("mount failed: " + str(e))
' "$root" "$conv" "$own" "$n" "$@"
shift "$n"
cd /workspace
PATH="$ew_path"
exec /usr/bin/unshare -Ur -- "$@"
"""

#: A response is always this 4-key shape, so the supervisor parses one
#: schema whether the run succeeded, failed, timed out, or the request
#: itself was malformed.
Response = dict[str, str | int | bool]


def _exec_argv(
    code: str,
    agent_root: str | None,
    conv_id: str | None = None,
    compat_dirs: list[str] | None = None,
    thread_id: str | None = None,
) -> list[str]:
    """The child process argv: the B-60 namespace wrapper around ``python -c code``.

    Split out of :func:`run_once` so the runner's own unit tests can swap it for a
    plain ``python -c`` argv — ``unshare -Urm`` needs user namespaces and the
    script needs ``/mnt/workspace``, neither of which exists on a CI host or a
    developer's macOS. The contract gate
    ``test_sandbox_runtime_contract.test_exec_view_script_matches_the_sandbox_image``
    finds this list literal by ``ast`` and pins it against the orchestrator's copy.

    B-236 —— the conversation id and the compat names only travel with an agent
    root (no agent, no conversation view); the script re-validates every name.
    """
    conv = conv_id if agent_root and conv_id else ""
    thread = thread_id if agent_root and thread_id else ""
    compat = list(compat_dirs or ()) if conv else []
    return [
        "unshare",
        "-Urm",
        "--propagation",
        "private",
        "--",
        "sh",
        "-c",
        _EXEC_VIEW_SCRIPT,
        "ew-exec-view",
        agent_root or "",
        conv,
        thread,
        str(len(compat)),
        *compat,
        # -E -P, deliberately NOT -I: -I implies -s, which kicks the user
        # site out of sys.path and silently breaks `pip install --user`
        # (the image's PIP_USER=1 flow). -E keeps PYTHON* env-config
        # isolation; -P keeps the script dir / cwd off sys.path.
        sys.executable,
        "-E",
        "-P",
        "-c",
        code,
    ]


def run_once(
    code: str,
    timeout_s: int,
    envs: dict[str, str] | None = None,
    agent_root: str | None = None,
    conv_id: str | None = None,
    compat_dirs: list[str] | None = None,
    thread_id: str | None = None,
) -> Response:
    """Run ``code`` in a child Python process; capture stdout / stderr / exit.

    ``timeout_s`` is clamped to ``[1, MAX_TIMEOUT_S]``. On timeout the child is
    killed and ``timed_out`` is ``True`` with ``exit_code`` -1.

    ``envs`` (sandbox migration wave 2, spec 决策 10) is merged onto this runner
    process's own environment for the child only.

    ``agent_root`` (B-60) is the agent's directory on the NAS mount. The child
    runs inside its own user + mount namespace (``unshare -Urm``) where that
    directory is bind-mounted as ``/workspace``, ``shared/`` is bound read-only
    and the NAS mount itself is covered by an empty tmpfs — so absolute
    ``/workspace/x`` lands in the agent's directory and other agents' NAS
    directories are not in the view. That partitions the NAS only: sandbox-local
    state (``/tmp``, ``$HOME``, ``/opt/agents``, ``/opt/skills``) is shared by every
    exec in this sandbox, i.e. by all agents and conversations of the same user.
    ``None`` → the whole user root is ``/workspace``.
    The directory is created inside the namespace (``mkdir -p``, umask 0o077
    inherited from this process). Any mount failure exits non-zero before the
    code runs: fail closed, never fall back to the shared root.
    """
    timeout_s = max(1, min(timeout_s, MAX_TIMEOUT_S))
    child_env = {**os.environ, **envs} if envs else None
    argv = _exec_argv(code, agent_root, conv_id, compat_dirs, thread_id)
    try:
        proc = subprocess.run(  # noqa: S603 - arbitrary code execution is the tool
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=child_env,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "stdout": _cap(_as_text(exc.stdout)),
            "stderr": _cap(_as_text(exc.stderr)),
            "exit_code": -1,
            "timed_out": True,
        }
    return {
        "stdout": _cap(proc.stdout),
        "stderr": _cap(proc.stderr),
        "exit_code": proc.returncode,
        "timed_out": False,
    }


def handle_request(request: dict[str, object]) -> Response:
    """Validate one decoded request object and dispatch it to :func:`run_once`."""
    code = request.get("code")
    if not isinstance(code, str):
        return _error("request missing required string field 'code'")
    raw_timeout = request.get("timeout_s", DEFAULT_TIMEOUT_S)
    # JSON numbers decode to int / float; a bool is an int subclass we
    # explicitly reject. Anything else falls back to the default.
    timeout_s = raw_timeout if type(raw_timeout) is int else DEFAULT_TIMEOUT_S
    raw_envs = request.get("envs")
    envs = (
        {k: v for k, v in raw_envs.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(raw_envs, dict)
        else None
    )
    raw_root = request.get("agent_root")
    agent_root = raw_root if isinstance(raw_root, str) and raw_root else None
    raw_conv = request.get("conv_id")
    conv_id = raw_conv if isinstance(raw_conv, str) and raw_conv else None
    raw_compat = request.get("compat_dirs")
    compat_dirs = (
        [d for d in raw_compat if isinstance(d, str)] if isinstance(raw_compat, list) else None
    )
    raw_thread = request.get("thread_id")
    thread_id = raw_thread if isinstance(raw_thread, str) and raw_thread else None
    return run_once(code, timeout_s, envs, agent_root, conv_id, compat_dirs, thread_id)


def handle_line(line: str) -> Response:
    """Decode one stdin line into a request and run it.

    A blank line yields ``None``-equivalent skipping at the caller; here
    any non-JSON or non-object payload becomes a structured error response
    so the supervisor never has to special-case a parse failure.
    """
    try:
        request = json.loads(line)
    except json.JSONDecodeError as exc:
        return _error(f"invalid JSON request: {exc}")
    if not isinstance(request, dict):
        return _error("request must be a JSON object")
    return handle_request(request)


def main(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
    """Emit a readiness line, then serve requests until stdin EOF.

    The leading ``{"ready": true}`` line lets the supervisor confirm the
    runner booted (the acquire-time health check) before sending code.

    Sets this process's umask to ``0o077`` before serving any request. A
    process's umask is inherited by every child it fork/execs, so this one
    call covers every ``run_once`` → ``subprocess.run`` child for the
    runner's whole lifetime — without it, the submitted code's own
    ``mkdir``/``open`` calls (Python ``os.mkdir`` or a shelled-out
    ``mkdir -p``) would land at the sandbox's *default* umask (commonly
    ``0o022``), producing ``0o755`` directories / ``0o644`` files —
    group/other-readable, wider than this workspace's owner-only target.
    ``0o077`` clears every group/other bit, matching
    ``NasWorkspaceStore._DIR_MODE`` / ``_LEAF_FILE_MODE`` (``0o700`` /
    ``0o600`` — see ``orchestrator/tools/nas_workspace_store.py``), the
    workspace's own settled target mode now that control-plane and this
    sandbox's agent run as the same uid (workspace-gid-sharing design § 六).

    History, so nobody reintroduces ``umask 0``: this used to be
    ``os.umask(0)`` (fully permissive, ``0o777``/``0o666``), added to paper
    over a *cross-uid* write conflict from before the uid-unification
    direction change — control-plane and this sandbox's agent were
    different uids, and a mode masked to ``0o755``/``0o644`` by the default
    umask still let control-plane ``read``/``list`` but not delete or
    overwrite an agent-written file. That premise is gone: same uid means
    owner bits alone are enough on both sides, so there is no longer a
    reason to leave files group/other-writable at all — hence ``0o077``,
    not merely "less than 0". This must still agree with this repo's
    ``AgentSandboxClient.exec`` and its ``umask 077 &&`` prefix on the cloud
    (E2B) backend — both backends stay in lockstep or one of them silently
    reopens this exact hole (contract-tested for parity in
    ``test_sandbox_runtime_contract.py``). It also depends on
    ``sandbox_supervisor.docker_client._AUX_CONTAINER_HARDENING_ARGS``
    carrying ``--cap-add DAC_OVERRIDE``: tightening this umask makes
    exec-created files ``0o700``/``0o600``, and the supervisor's root
    aux containers (``--cap-drop ALL``) cannot read/write/delete an
    owner-only file without that capability — see that constant's comment.
    """
    os.umask(0o077)
    stdout.write(json.dumps({"ready": True}) + "\n")
    stdout.flush()
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        stdout.write(json.dumps(handle_line(line)) + "\n")
        stdout.flush()


def _error(message: str) -> Response:
    """A response for a request the runner could not even attempt to run."""
    return {
        "stdout": "",
        "stderr": f"[runner error] {message}",
        "exit_code": -1,
        "timed_out": False,
    }


def _as_text(value: str | bytes | None) -> str:
    """Normalise captured output to ``str`` — ``TimeoutExpired`` may carry bytes."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _cap(text: str) -> str:
    """Bound captured output to :data:`MAX_OUTPUT_CHARS` (head + tail kept)."""
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    dropped = len(text) - 2 * half
    return f"{text[:half]}\n[... {dropped} chars truncated ...]\n{text[-half:]}"


if __name__ == "__main__":
    main()
