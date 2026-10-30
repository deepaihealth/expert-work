"""B-60 —— 每次 exec 一个私有 ``/workspace`` 的命令串单源(spec §4.3)。"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from orchestrator.tools.exec_view import (
    EXEC_VIEW_ARGV_PREFIX,
    EXEC_VIEW_SCRIPT,
    build_exec_command,
    exec_view_argv,
)

_PY = ["python", "-E", "-P", "/tmp/ew-exec-abc.py"]  # noqa: S108 — 沙箱 tmpfs 路径,不是本机


def _sh_and_helper() -> tuple[str, str]:
    """把脚本拆成 sh 部分与内嵌 python(``-c '…'`` 那一段单引号串)。"""
    start = EXEC_VIEW_SCRIPT.index(_HELPER_OPEN) + len(_HELPER_OPEN)
    end = EXEC_VIEW_SCRIPT.index(_HELPER_CLOSE, start)
    return (
        EXEC_VIEW_SCRIPT[:start] + EXEC_VIEW_SCRIPT[end:],
        EXEC_VIEW_SCRIPT[start:end],
    )


_HELPER_OPEN = "/usr/local/bin/python3 -I -S -c '"
_HELPER_CLOSE = '\n\' "$root" "$conv" "$own"\n'


def test_script_fails_closed_and_names_both_roots() -> None:
    assert EXEC_VIEW_SCRIPT.startswith("set -eu\n")
    _, helper = _sh_and_helper()
    assert "    view = bind(top, agent)\n" in helper
    assert "        ws = bind(work, workspace)\n" in helper  # 对话视图
    assert "        ws = bind(top, workspace)\n" in helper  # 老视图
    assert "        bind(nas, agent)\n        bind(nas, workspace)\n" in helper  # 未绑分支
    assert '    attach(tmpfs((b"size", b"1k")), nas)\n' in helper
    # 载荷跑在嵌套的 user namespace 里:它不拥有这个 mount ns,卸不掉任何一层覆盖;
    # PATH 还原成调用方给的值,unshare 按绝对路径找
    assert EXEC_VIEW_SCRIPT.endswith(
        'cd /workspace\nPATH="$ew_path"\nexec /usr/bin/unshare -Ur -- "$@"\n'
    )
    # spec §零 #2:uploads 在 agents/<key>/uploads,挂用户根的 uploads 会遮住它 ——
    # B-236 起对话视图里 bind 进来的是 agent 根那一份,从不是用户根那一份。
    assert "/mnt/workspace/uploads" not in EXEC_VIEW_SCRIPT


def test_sh_part_runs_no_mount_and_no_command_found_via_path() -> None:
    """review 3 —— 挂载全在内嵌 python 里按 fd 做;sh 只做参数校验和最后的 exec。sh 里
    出现的外部命令只有两个绝对路径(python 与 unshare),``PATH`` 在第二行就钉死。"""
    sh, _ = _sh_and_helper()
    assert sh.splitlines()[2] == "PATH=/usr/sbin:/usr/bin:/sbin:/bin"
    for word in ("mount", "umount", "mkdir", "stat", "ls ", "cd -P", "rmdir"):
        assert word not in sh, word
    assert "exec /usr/bin/unshare -Ur" in sh


def test_helper_opens_every_nas_path_without_following_symlinks() -> None:
    """内嵌 python 里每一次 ``os.open`` 要么带 ``O_NOFOLLOW`` 的 ``DIR``(经 ``sub``),
    要么是两个可信起点:``/mnt/workspace``(平台建的符号链接)与 agent 根的前缀。
    挂载只经 ``open_tree`` / ``move_mount`` / ``fsmount`` 与 ``/proc/self/fd`` 上的 remount。"""
    _, helper = _sh_and_helper()
    assert "DIR = os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC\n" in helper
    opens = re.findall(r"os\.open\(([^)]*)\)", helper)
    trusted = {
        '"/mnt/workspace", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC',
        'head or "/", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC',
    }
    for args in opens:
        assert args in trusted or args.endswith(("DIR", "DIR, dir_fd=fd")), args
    assert re.findall(r"libc\.mount\(([^)]*)\)", helper) == [
        "None, fd_path, None, REMOUNT_RO, None"
    ]


def test_embedded_helper_compiles_and_is_plain_ascii() -> None:
    """它是 sh 的单引号串(不许含单引号),``-c`` 的源码还要按 locale 解码(只用 ASCII)。"""
    _, helper = _sh_and_helper()
    assert "'" not in helper
    assert helper.isascii()
    compile(helper, "<ew-exec-view helper>", "exec")


def test_shared_is_bound_read_only_with_locked_flags_kept() -> None:
    """``MS_REMOUNT | MS_BIND | MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC``:userns 里
    remount 不能去掉源挂载上锁住的 flag,但可以加;atime 不写 = 沿用源挂载的。B-236 ——
    只读 bind 只剩 ``shared/`` 一处(agent 根的基础目录改为开场复制,不再挂进来)。"""
    _, helper = _sh_and_helper()
    assert "REMOUNT_RO = 0x20 | 0x1000 | 0x1 | 0x2 | 0x4 | 0x8" in helper
    assert re.findall(r"bind_ro\(([^)]*)\)\n", helper) == ['shared, target, "shared/"']


def test_non_directory_shared_skips_the_mount_loudly_instead_of_bricking_the_agent() -> None:
    """全分支终审 I1 —— ``agents/<key>/shared`` 已经是普通文件或符号链接时(用户代码
    建得出来,平台工具只保留首段),建挂载桩会失败;不能让那个 agent 的**每一次** exec ——
    文件工具也在内 —— 整个炸掉。守卫把这一格降级成「这次不挂 shared/」,写到 stderr,
    exec 照常跑完。"""
    _, helper = _sh_and_helper()
    assert 'say("/workspace/shared is not a directory; shared/ not mounted")\n' in helper


def test_script_parses_as_posix_sh() -> None:
    proc = subprocess.run(
        ["sh", "-n"],
        input=EXEC_VIEW_SCRIPT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_argv_prefix_is_unshare_user_and_mount_namespaces() -> None:
    assert EXEC_VIEW_ARGV_PREFIX == (
        "unshare",
        "-Urm",
        "--propagation",
        "private",
        "--",
        "sh",
        "-c",
        EXEC_VIEW_SCRIPT,
        "ew-exec-view",
    )


def test_argv_bound_passes_the_nas_root_as_first_positional() -> None:
    assert exec_view_argv("plan-aaaaaaaa", _PY) == [
        *EXEC_VIEW_ARGV_PREFIX,
        "/mnt/workspace/agents/plan-aaaaaaaa",
        "",
        "",
        *_PY,
    ]


def test_argv_unbound_passes_an_empty_root() -> None:
    assert exec_view_argv("", _PY) == [*EXEC_VIEW_ARGV_PREFIX, "", "", "", *_PY]


def test_build_exec_command_bound_and_unbound_verbatim() -> None:
    prefix = "umask 077 && unshare -Urm --propagation private -- sh -c " + shlex.quote(
        EXEC_VIEW_SCRIPT
    )
    assert (
        build_exec_command("plan-aaaaaaaa", "/tmp/ew-exec-abc.py")  # noqa: S108
        == (
            prefix + " ew-exec-view /mnt/workspace/agents/plan-aaaaaaaa '' ''"
            " python -E -P /tmp/ew-exec-abc.py"
        )
    )
    assert build_exec_command("", "/tmp/ew-exec-abc.py") == (  # noqa: S108
        prefix + " ew-exec-view '' '' '' python -E -P /tmp/ew-exec-abc.py"
    )


def test_script_path_is_quoted_not_interpolated() -> None:
    cmd = build_exec_command("", "/tmp/a b;rm -rf x.py")  # noqa: S108
    assert shlex.split(cmd[len("umask 077 && ") :])[-1] == "/tmp/a b;rm -rf x.py"  # noqa: S108


@pytest.mark.parametrize("bad", ["..", "a/b", "a b"])
def test_bad_agent_key_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        build_exec_command(bad, "/tmp/x.py")  # noqa: S108


# ---------------------------------------------------------------------------
# B-236 —— 对话视图。
# ---------------------------------------------------------------------------

_CONV = UUID("11111111-2222-3333-4444-555555555555")
_OTHER = "99999999-8888-7777-6666-555555555555"


def test_argv_conversation_view_carries_the_conversation() -> None:
    assert exec_view_argv("plan-aaaaaaaa", _PY, conv_id=_CONV, thread_id=_CONV) == [
        *EXEC_VIEW_ARGV_PREFIX,
        "/mnt/workspace/agents/plan-aaaaaaaa",
        str(_CONV),
        str(_CONV),
        *_PY,
    ]


def test_argv_legacy_view_carries_only_the_own_thread() -> None:
    """老视图:``$2`` 照旧是空串(老视图的约定不变),线程 id 走单独的 ``$3``。"""
    assert exec_view_argv("plan-aaaaaaaa", _PY, thread_id=_CONV) == [
        *EXEC_VIEW_ARGV_PREFIX,
        "/mnt/workspace/agents/plan-aaaaaaaa",
        "",
        str(_CONV),
        *_PY,
    ]


def test_argv_unbound_never_carries_a_conversation() -> None:
    """没绑 agent 没有对话视图:对话 id、线程 id 都不发,脚本走未绑分支。"""
    assert exec_view_argv("", _PY, conv_id=_CONV, thread_id=_CONV) == [
        *EXEC_VIEW_ARGV_PREFIX,
        "",
        "",
        "",
        *_PY,
    ]


def test_script_platform_dirs_are_the_resolver_platform_dirs() -> None:
    """脚本 bind 进 /workspace 的平台目录与宿主侧解析器认的是同一组。"""
    from orchestrator.tools.workspace_paths import PLATFORM_DIRS

    names = ", ".join(f'"{d}"' for d in PLATFORM_DIRS)
    assert f"PLATFORM_DIRS = ({names})\n" in EXEC_VIEW_SCRIPT


# —— sh 部分的演练:本机没有非特权 user namespace(macOS / CI host 都给不了),挂载阶段
# 又全在内嵌 python 里直接发系统调用,所以这里只演练 sh 那一层:把 ``/agent`` /
# ``/workspace`` 换成临时目录,把 python 换成记账的假命令(``EW_PY_RC`` 决定它的退出码),
# ``/usr/bin/unshare`` 换成原样执行载荷的假命令。验得住:参数校验、交给挂载阶段的参数、
# 挂载失败不跑载荷、``PATH`` 与工作目录的交接。真的挂载语义(含按 fd 挂、符号链接被拒、
# 只读、竞态)由 ``test_exec_view_kernel.py`` 在真镜像真内核上验。

_FAKE_PY = """#!/bin/sh
shift 4  # -I -S -c <helper>(其后只有挂载阶段的三个参数)
printf '%s\\n' "$@" > "$EW_PY_LOG"
exit "${EW_PY_RC:-0}"
"""

_FAKE_UNSHARE = """#!/bin/sh
echo "$*" > "$EW_UNSHARE_LOG"
while [ "$1" != "--" ]; do shift; done
shift
exec "$@"
"""


class _Rehearsal:
    def __init__(self, tmp: Path) -> None:
        self.ws = tmp / "ws"
        self.agent = tmp / "agent"
        self.bin = tmp / "bin"
        for d in (self.ws, self.agent, self.bin):
            d.mkdir()
        self.py_log = tmp / "py.log"
        self.unshare_log = tmp / "unshare.log"
        self.root = "/mnt/workspace/agents/plan-aaaaaaaa"
        self.extra_env: dict[str, str] = {}
        for name, body in (("python3", _FAKE_PY), ("unshare", _FAKE_UNSHARE)):
            fake = self.bin / name
            fake.write_text(body)
            fake.chmod(0o755)

    def run_raw(self, *args: str) -> subprocess.CompletedProcess[str]:
        script = (
            EXEC_VIEW_SCRIPT.replace("/usr/local/bin/python3", str(self.bin / "python3"))
            .replace("/usr/bin/unshare", str(self.bin / "unshare"))
            .replace("cd /workspace", f"cd {self.ws}")
            .replace("[ ! -d /agent ]", f"[ ! -d {self.agent} ]")
        )
        env = {
            **os.environ,
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "EW_PY_LOG": str(self.py_log),
            "EW_UNSHARE_LOG": str(self.unshare_log),
            **self.extra_env,
        }
        return subprocess.run(
            ["sh", "-c", script, "ew-exec-view", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def run(
        self, conv: str, cmd: str, *, own: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        thread = conv if own is None else own
        return self.run_raw(self.root, conv, thread, "sh", "-c", cmd)

    def helper_args(self) -> list[str] | None:
        return self.py_log.read_text().splitlines() if self.py_log.exists() else None


def test_rehearsal_hands_the_mount_phase_to_the_helper_then_runs_the_payload(
    tmp_path: Path,
) -> None:
    r = _Rehearsal(tmp_path)
    proc = r.run(str(_CONV), 'echo "$PWD|$PATH"')

    assert proc.returncode == 0, proc.stderr
    # 挂载阶段只拿到 $1..$3,载荷 argv 不交给它
    assert r.helper_args() == [r.root, str(_CONV), str(_CONV)]
    # 载荷:在 /workspace 里、PATH 还原成调用方的、由 unshare -Ur 起
    assert proc.stdout == f"{r.ws}|{r.bin}:/usr/bin:/bin\n"
    assert r.unshare_log.read_text().split()[:3] == ["-Ur", "--", "sh"]


def test_rehearsal_conversation_id_wins_over_the_thread_id(tmp_path: Path) -> None:
    """对话视图里本对话那一格就是对话 id;老视图里才用 ``$3``。"""
    r = _Rehearsal(tmp_path)
    assert r.run(str(_CONV), "true", own="").returncode == 0
    assert r.helper_args() == [r.root, str(_CONV), str(_CONV)]
    assert r.run("", "true", own=_OTHER).returncode == 0
    assert r.helper_args() == [r.root, "", _OTHER]


def test_rehearsal_mount_failure_never_runs_the_payload(tmp_path: Path) -> None:
    r = _Rehearsal(tmp_path)
    r.extra_env = {"EW_PY_RC": "98"}
    proc = r.run(str(_CONV), "echo ran")

    assert proc.returncode == 98
    assert "ran" not in proc.stdout
    assert not r.unshare_log.exists()


def test_rehearsal_missing_agent_mount_point_exits_97_before_any_mount(tmp_path: Path) -> None:
    r = _Rehearsal(tmp_path)
    r.agent.rmdir()
    proc = r.run(str(_CONV), "echo ran")

    assert proc.returncode == 97
    assert "image lacks" in proc.stderr
    assert "ran" not in proc.stdout
    assert r.helper_args() is None


@pytest.mark.parametrize(
    ("conv", "own"),
    [("a/b", ""), ("..", ""), ("", "a/b"), ("", "..")],
)
def test_rehearsal_bad_ids_fail_closed(tmp_path: Path, conv: str, own: str) -> None:
    r = _Rehearsal(tmp_path)
    proc = r.run_raw(r.root, conv, own, "sh", "-c", "echo ran")

    assert proc.returncode == 98, proc.stderr
    assert "ran" not in proc.stdout
    assert r.helper_args() is None
