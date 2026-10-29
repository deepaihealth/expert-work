"""B-60 —— 每次 exec 一个私有 ``/workspace`` 的命令串单源(spec §4.3)。"""

from __future__ import annotations

import os
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


def test_script_fails_closed_and_names_both_roots() -> None:
    assert EXEC_VIEW_SCRIPT.startswith("set -eu\n")
    assert 'mount --bind "$root" /workspace\n' in EXEC_VIEW_SCRIPT
    assert "mount -t tmpfs -o size=1k none /mnt/workspace\n" in EXEC_VIEW_SCRIPT
    assert "mount --bind /mnt/workspace /workspace\n" in EXEC_VIEW_SCRIPT  # 未绑分支
    assert EXEC_VIEW_SCRIPT.endswith('cd /workspace\nexec "$@"\n')
    # spec §零 #2:uploads 在 agents/<key>/uploads,挂用户根的 uploads 会遮住它 ——
    # B-236 起对话视图里 bind 进来的是 agent 根那一份,从不是用户根那一份。
    assert "/mnt/workspace/uploads" not in EXEC_VIEW_SCRIPT


def test_shared_is_bound_read_only_with_locked_flags_kept() -> None:
    assert "mount -o remount,bind,ro,nosuid,nodev,noexec /workspace/shared\n" in EXEC_VIEW_SCRIPT


def test_non_directory_shared_skips_the_mount_loudly_instead_of_bricking_the_agent() -> None:
    """全分支终审 I1 —— ``agents/<key>/shared`` 已经是普通文件或符号链接时(用户代码
    建得出来,平台工具只保留首段),``mkdir -p`` 会失败,``set -eu`` 就把那个 agent 的
    **每一次** exec —— 文件工具也在内 —— 整个炸掉,产品面无从恢复。守卫把这一格降级
    成「这次不挂 shared/」,写到 stderr,exec 照常跑完。"""
    assert (
        "if [ -L /workspace/shared ] || "
        "{ [ -e /workspace/shared ] && [ ! -d /workspace/shared ]; }; then\n"
    ) in EXEC_VIEW_SCRIPT
    assert (
        'echo "ew-exec-view: /workspace/shared is not a directory; shared/ not mounted" >&2\n'
    ) in EXEC_VIEW_SCRIPT


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
        "0",
        *_PY,
    ]


def test_argv_unbound_passes_an_empty_root() -> None:
    assert exec_view_argv("", _PY) == [*EXEC_VIEW_ARGV_PREFIX, "", "", "0", *_PY]


def test_build_exec_command_bound_and_unbound_verbatim() -> None:
    prefix = "umask 077 && unshare -Urm --propagation private -- sh -c " + shlex.quote(
        EXEC_VIEW_SCRIPT
    )
    assert (
        build_exec_command("plan-aaaaaaaa", "/tmp/ew-exec-abc.py")  # noqa: S108
        == (
            prefix + " ew-exec-view /mnt/workspace/agents/plan-aaaaaaaa '' 0"
            " python -E -P /tmp/ew-exec-abc.py"
        )
    )
    assert build_exec_command("", "/tmp/ew-exec-abc.py") == (  # noqa: S108
        prefix + " ew-exec-view '' '' 0 python -E -P /tmp/ew-exec-abc.py"
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


def test_argv_conversation_view_carries_the_conversation_and_compat_dirs() -> None:
    assert exec_view_argv("plan-aaaaaaaa", _PY, conv_id=_CONV, compat_dirs=("style", "模板")) == [
        *EXEC_VIEW_ARGV_PREFIX,
        "/mnt/workspace/agents/plan-aaaaaaaa",
        str(_CONV),
        "2",
        "style",
        "模板",
        *_PY,
    ]


def test_argv_unbound_never_carries_a_conversation() -> None:
    """没绑 agent 没有对话视图:对话 id 与目录名都不发,脚本走未绑分支。"""
    assert exec_view_argv("", _PY, conv_id=_CONV, compat_dirs=("style",)) == [
        *EXEC_VIEW_ARGV_PREFIX,
        "",
        "",
        "0",
        *_PY,
    ]


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "threads", "uploads", ".tool_results"])
def test_argv_refuses_an_unmappable_compat_dir(bad: str) -> None:
    with pytest.raises(ValueError):
        exec_view_argv("plan-aaaaaaaa", _PY, conv_id=_CONV, compat_dirs=(bad,))


def test_build_exec_command_quotes_compat_dir_names() -> None:
    cmd = build_exec_command(
        "plan-aaaaaaaa",
        "/tmp/x.py",  # noqa: S108
        conv_id=_CONV,
        compat_dirs=("客户 案例;rm",),
    )
    argv = shlex.split(cmd[len("umask 077 && ") :])
    assert argv[argv.index("ew-exec-view") + 1 :][:4] == [
        "/mnt/workspace/agents/plan-aaaaaaaa",
        str(_CONV),
        "1",
        "客户 案例;rm",
    ]


def test_script_platform_dirs_are_the_resolver_platform_dirs() -> None:
    """脚本 bind 进 /workspace 的平台目录与宿主侧解析器认的是同一组。"""
    from orchestrator.tools.workspace_paths import PLATFORM_DIRS

    assert f"  for d in {' '.join(PLATFORM_DIRS)}; do\n" in EXEC_VIEW_SCRIPT


# —— 脚本演练:本机没有非特权 user namespace(macOS / CI host 都给不了),所以把脚本里的
# 三个绝对路径换成临时目录,``mount`` 换成一个记账的假命令(``--bind`` 落成符号链接,
# tmpfs 只记账),其余逐字照跑。验得住:参数解析、目录建在哪、哪些 bind 被做、过渡期
# 目录的判据、坏参数 fail closed。验不住:真的挂载语义 —— 那由 CI 起真镜像的
# ``infra/sandbox-image/smoke_test.py`` 覆盖。

_FAKE_MOUNT = """#!/bin/sh
echo "$*" >> "$EW_MOUNT_LOG"
if [ "$1" = "--bind" ]; then
  rmdir "$3" 2>/dev/null || true
  ln -s "$2" "$3"
fi
"""


class _Rehearsal:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.mnt = tmp / "mnt"
        self.ws = tmp / "ws"
        self.agent = tmp / "agent"
        self.root = self.mnt / "agents" / "plan-aaaaaaaa"
        self.log = tmp / "mount.log"
        self.bin = tmp / "bin"
        for d in (self.mnt, self.ws, self.agent, self.bin):
            d.mkdir()
        fake = self.bin / "mount"
        fake.write_text(_FAKE_MOUNT)
        fake.chmod(0o755)

    def run(
        self, root: str, conv: str, compat: list[str], cmd: str
    ) -> subprocess.CompletedProcess[str]:
        script = (
            EXEC_VIEW_SCRIPT.replace("/mnt/workspace", str(self.mnt))
            .replace("/workspace", str(self.ws))
            .replace("/agent", str(self.agent))
        )
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "EW_MOUNT_LOG": str(self.log),
        }
        return subprocess.run(
            [
                "sh",
                "-c",
                script,
                "ew-exec-view",
                root,
                conv,
                str(len(compat)),
                *compat,
                "sh",
                "-c",
                cmd,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def mounts(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


def test_rehearsal_conversation_view_binds_the_conversation_dir_and_the_agent_root(
    tmp_path: Path,
) -> None:
    r = _Rehearsal(tmp_path)
    proc = r.run(
        str(r.root), str(_CONV), [], "echo x > out.txt; echo y > " + str(r.agent) + "/a.txt"
    )

    assert proc.returncode == 0, proc.stderr
    work = r.root / "threads" / str(_CONV) / "work"
    assert (work / "out.txt").read_text() == "x\n"  # 相对路径落在本对话目录
    assert (r.root / "a.txt").read_text() == "y\n"  # /agent 落在 agent 根
    mounts = r.mounts()
    assert f"--bind {r.root} {r.agent}" in mounts
    assert f"--bind {work} {r.ws}" in mounts
    # 别的对话的目录在 /agent 底下被 tmpfs 盖住,只把本对话那一格 bind 回来
    assert f"-t tmpfs -o size=1k,mode=0500 none {r.agent}/threads" in mounts
    assert f"--bind {r.root}/threads/{_CONV} {r.agent}/threads/{_CONV}" in mounts
    for d in ("uploads", "inputs", ".tool_results"):
        assert f"--bind {r.root}/{d} {r.ws}/{d}" in mounts
    assert mounts[-1] == f"-t tmpfs -o size=1k none {r.mnt}"


def test_rehearsal_legacy_view_binds_the_agent_root_twice(tmp_path: Path) -> None:
    r = _Rehearsal(tmp_path)
    proc = r.run(str(r.root), "", [], "echo x > out.txt")

    assert proc.returncode == 0, proc.stderr
    assert (r.root / "out.txt").read_text() == "x\n"
    mounts = r.mounts()
    assert f"--bind {r.root} {r.agent}" in mounts
    assert f"--bind {r.root} {r.ws}" in mounts
    assert not any("threads" in m for m in mounts)


def test_rehearsal_unbound_binds_the_user_root_to_both_views(tmp_path: Path) -> None:
    r = _Rehearsal(tmp_path)
    proc = r.run("", "", [], "true")

    assert proc.returncode == 0, proc.stderr
    assert r.mounts() == [f"--bind {r.mnt} {r.agent}", f"--bind {r.mnt} {r.ws}"]


def test_rehearsal_missing_agent_mount_point_exits_97_before_any_mount(tmp_path: Path) -> None:
    r = _Rehearsal(tmp_path)
    r.agent.rmdir()
    proc = r.run(str(r.root), str(_CONV), [], "echo ran")

    assert proc.returncode == 97
    assert "image lacks" in proc.stderr
    assert "ran" not in proc.stdout
    assert r.mounts() == []


def test_rehearsal_compat_dir_maps_only_when_the_conversation_has_none(tmp_path: Path) -> None:
    """过渡期目录:agent 根有、本对话没有(或只有空桩)才映射;本对话自己有内容就不映射;
    根下的单个文件从不映射。"""
    r = _Rehearsal(tmp_path)
    work = r.root / "threads" / str(_CONV) / "work"
    (r.root / "style").mkdir(parents=True)
    (r.root / "own").mkdir()
    (r.root / "stub").mkdir()
    (r.root / "notes.md").write_text("root file")
    (work / "own").mkdir(parents=True)
    (work / "own" / "x").write_text("mine")
    (work / "stub").mkdir()  # 上一次 exec 留下的空挂载桩

    proc = r.run(str(r.root), str(_CONV), ["style", "own", "stub", "notes.md", "absent"], "true")

    assert proc.returncode == 0, proc.stderr
    mounts = r.mounts()
    assert f"--bind {r.root}/style {r.ws}/style" in mounts
    assert f"--bind {r.root}/stub {r.ws}/stub" in mounts
    assert not any(m.endswith(f"{r.ws}/own") for m in mounts)
    assert not any("notes.md" in m or "absent" in m for m in mounts)


def test_rehearsal_compat_dirs_are_ignored_in_the_legacy_view(tmp_path: Path) -> None:
    r = _Rehearsal(tmp_path)
    (r.root / "style").mkdir(parents=True)
    proc = r.run(str(r.root), "", ["style"], "true")

    assert proc.returncode == 0, proc.stderr
    assert not any("style" in m for m in r.mounts())


@pytest.mark.parametrize("bad", ["../x", "a/b", "threads", ".tool_results", "..", ""])
def test_rehearsal_bad_compat_dir_fails_closed(tmp_path: Path, bad: str) -> None:
    r = _Rehearsal(tmp_path)
    proc = r.run(str(r.root), str(_CONV), [bad], "echo ran")

    assert proc.returncode == 98, proc.stderr
    assert "ran" not in proc.stdout


@pytest.mark.parametrize(("conv", "count"), [("a/b", "0"), ("..", "0"), (str(_CONV), "x")])
def test_rehearsal_bad_conversation_or_count_fails_closed(
    tmp_path: Path, conv: str, count: str
) -> None:
    r = _Rehearsal(tmp_path)
    script = (
        EXEC_VIEW_SCRIPT.replace("/mnt/workspace", str(r.mnt))
        .replace("/workspace", str(r.ws))
        .replace("/agent", str(r.agent))
    )
    env = {**os.environ, "PATH": f"{r.bin}:{os.environ['PATH']}", "EW_MOUNT_LOG": str(r.log)}
    proc = subprocess.run(
        ["sh", "-c", script, "ew-exec-view", str(r.root), conv, count, "sh", "-c", "echo ran"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 98, proc.stderr
    assert "ran" not in proc.stdout


async def test_rehearsal_compat_predicate_matches_the_host_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """计划测试 10 —— 同一棵树,宿主侧 ``discover_compat_dirs`` 选中的目录与脚本真的
    bind 的目录必须是同一组(两边各写一份判据:不存在,或只是一个空的真目录)。"""
    from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
    from orchestrator.tools.workspace_compat import COMPAT_UNTIL_ENV, discover_compat_dirs
    from orchestrator.tools.workspace_paths import WorkspaceView

    tenant, user = UUID(int=7), UUID(int=8)
    r = _Rehearsal(tmp_path)
    r.root = r.mnt / str(tenant) / str(user) / "agents" / "plan-aaaaaaaa"
    work = r.root / "threads" / str(_CONV) / "work"
    for d in ("style", "own", "stub", "linked-target"):
        (r.root / d).mkdir(parents=True)
    (r.root / "linked").symlink_to(r.root / "linked-target")
    (work / "own").mkdir(parents=True)
    (work / "own" / "x").write_text("mine")
    (work / "stub").mkdir()
    (work / "linked-target").symlink_to(r.root / "style")
    candidates = ["linked", "linked-target", "own", "stub", "style"]

    monkeypatch.setenv(COMPAT_UNTIL_ENV, "2099-12-31")
    host = await discover_compat_dirs(
        NasWorkspaceStore(root=str(r.mnt)),
        tenant_id=tenant,
        user_id=user,
        view=WorkspaceView(agent_key="plan-aaaaaaaa", conv_id=_CONV),
    )
    proc = r.run(str(r.root), str(_CONV), candidates, "true")

    assert proc.returncode == 0, proc.stderr
    bound = sorted(
        m.split(" ")[-1].rsplit("/", 1)[-1]
        for m in r.mounts()
        if m.startswith(f"--bind {r.root}/") and any(m.endswith(f"/{c}") for c in candidates)
    )
    assert host == ("stub", "style")
    assert tuple(bound) == host
