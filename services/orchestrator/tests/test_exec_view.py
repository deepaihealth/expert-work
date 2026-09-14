"""B-60 —— 每次 exec 一个私有 ``/workspace`` 的命令串单源(spec §4.3)。"""

from __future__ import annotations

import shlex
import subprocess

import pytest

from orchestrator.tools.exec_view import (
    EXEC_VIEW_ARGV_PREFIX,
    EXEC_VIEW_SCRIPT,
    build_exec_command,
    exec_view_argv,
)

_PY = ["python", "-E", "-P", "/tmp/ew-exec-abc.py"]  # noqa: S108 — 沙箱 tmpfs 路径,不是本机


def test_script_fails_closed_and_names_both_roots() -> None:
    assert EXEC_VIEW_SCRIPT.startswith("set -eu\n")
    assert 'mount --bind "$root" /workspace\n' in EXEC_VIEW_SCRIPT
    assert "mount -t tmpfs -o size=1k none /mnt/workspace\n" in EXEC_VIEW_SCRIPT
    assert "mount --bind /mnt/workspace /workspace\n" in EXEC_VIEW_SCRIPT  # 未绑分支
    assert EXEC_VIEW_SCRIPT.endswith('cd /workspace\nexec "$@"\n')
    # spec §零 #2:uploads 在 agents/<key>/uploads,挂用户根的 uploads 会遮住它
    assert "uploads" not in EXEC_VIEW_SCRIPT


def test_shared_is_bound_read_only_with_locked_flags_kept() -> None:
    assert "mount -o remount,bind,ro,nosuid,nodev,noexec /workspace/shared\n" in EXEC_VIEW_SCRIPT


def test_script_parses_as_posix_sh() -> None:
    assert subprocess.run(["sh", "-n"], input=EXEC_VIEW_SCRIPT, text=True, check=False).returncode == 0


def test_argv_prefix_is_unshare_user_and_mount_namespaces() -> None:
    assert EXEC_VIEW_ARGV_PREFIX == (
        "unshare", "-Urm", "--propagation", "private", "--",
        "sh", "-c", EXEC_VIEW_SCRIPT, "ew-exec-view",
    )


def test_argv_bound_passes_the_nas_root_as_first_positional() -> None:
    assert exec_view_argv("plan-aaaaaaaa", _PY) == [
        *EXEC_VIEW_ARGV_PREFIX, "/mnt/workspace/agents/plan-aaaaaaaa", *_PY,
    ]


def test_argv_unbound_passes_an_empty_root() -> None:
    assert exec_view_argv("", _PY) == [*EXEC_VIEW_ARGV_PREFIX, "", *_PY]


def test_build_exec_command_bound_and_unbound_verbatim() -> None:
    prefix = "umask 077 && unshare -Urm --propagation private -- sh -c " + shlex.quote(EXEC_VIEW_SCRIPT)
    assert build_exec_command("plan-aaaaaaaa", "/tmp/ew-exec-abc.py") == (  # noqa: S108
        prefix + " ew-exec-view /mnt/workspace/agents/plan-aaaaaaaa python -E -P /tmp/ew-exec-abc.py"
    )
    assert build_exec_command("", "/tmp/ew-exec-abc.py") == (  # noqa: S108
        prefix + " ew-exec-view '' python -E -P /tmp/ew-exec-abc.py"
    )


def test_script_path_is_quoted_not_interpolated() -> None:
    cmd = build_exec_command("", "/tmp/a b;rm -rf x.py")  # noqa: S108
    assert shlex.split(cmd[len("umask 077 && ") :])[-1] == "/tmp/a b;rm -rf x.py"  # noqa: S108


@pytest.mark.parametrize("bad", ["..", "a/b", "a b"])
def test_bad_agent_key_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        build_exec_command(bad, "/tmp/x.py")  # noqa: S108
