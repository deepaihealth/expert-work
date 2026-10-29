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
    assert 'bind_same "$root" /workspace || fail "agent root changed"\n' in EXEC_VIEW_SCRIPT
    assert "mount -t tmpfs -o size=1k none /mnt/workspace\n" in EXEC_VIEW_SCRIPT
    assert "mount --bind /mnt/workspace /workspace\n" in EXEC_VIEW_SCRIPT  # 未绑分支
    # 载荷跑在嵌套的 user namespace 里:它不拥有这个 mount ns,卸不掉任何一层覆盖
    assert EXEC_VIEW_SCRIPT.endswith('cd /workspace\nexec unshare -Ur -- "$@"\n')
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
        "",
        "0",
        *_PY,
    ]


def test_argv_unbound_passes_an_empty_root() -> None:
    assert exec_view_argv("", _PY) == [*EXEC_VIEW_ARGV_PREFIX, "", "", "", "0", *_PY]


def test_build_exec_command_bound_and_unbound_verbatim() -> None:
    prefix = "umask 077 && unshare -Urm --propagation private -- sh -c " + shlex.quote(
        EXEC_VIEW_SCRIPT
    )
    assert (
        build_exec_command("plan-aaaaaaaa", "/tmp/ew-exec-abc.py")  # noqa: S108
        == (
            prefix + " ew-exec-view /mnt/workspace/agents/plan-aaaaaaaa '' '' 0"
            " python -E -P /tmp/ew-exec-abc.py"
        )
    )
    assert build_exec_command("", "/tmp/ew-exec-abc.py") == (  # noqa: S108
        prefix + " ew-exec-view '' '' '' 0 python -E -P /tmp/ew-exec-abc.py"
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


def test_argv_conversation_view_carries_the_conversation_and_compat_dirs() -> None:
    assert exec_view_argv(
        "plan-aaaaaaaa", _PY, conv_id=_CONV, thread_id=_CONV, compat_dirs=("style", "模板")
    ) == [
        *EXEC_VIEW_ARGV_PREFIX,
        "/mnt/workspace/agents/plan-aaaaaaaa",
        str(_CONV),
        str(_CONV),
        "2",
        "style",
        "模板",
        *_PY,
    ]


def test_argv_legacy_view_carries_only_the_own_thread() -> None:
    """老视图:``$2`` 照旧是空串(老视图的约定不变),线程 id 走单独的 ``$3``。"""
    assert exec_view_argv("plan-aaaaaaaa", _PY, thread_id=_CONV, compat_dirs=("style",)) == [
        *EXEC_VIEW_ARGV_PREFIX,
        "/mnt/workspace/agents/plan-aaaaaaaa",
        "",
        str(_CONV),
        "0",
        *_PY,
    ]


def test_argv_unbound_never_carries_a_conversation() -> None:
    """没绑 agent 没有对话视图:对话 id、线程 id 与目录名都不发,脚本走未绑分支。"""
    assert exec_view_argv("", _PY, conv_id=_CONV, thread_id=_CONV, compat_dirs=("style",)) == [
        *EXEC_VIEW_ARGV_PREFIX,
        "",
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
        thread_id=_CONV,
        compat_dirs=("客户 案例;rm",),
    )
    argv = shlex.split(cmd[len("umask 077 && ") :])
    assert argv[argv.index("ew-exec-view") + 1 :][:5] == [
        "/mnt/workspace/agents/plan-aaaaaaaa",
        str(_CONV),
        str(_CONV),
        "1",
        "客户 案例;rm",
    ]


def test_script_platform_dirs_are_the_resolver_platform_dirs() -> None:
    """脚本 bind 进 /workspace 的平台目录与宿主侧解析器认的是同一组。"""
    from orchestrator.tools.workspace_paths import PLATFORM_DIRS

    assert f"    for d in {' '.join(PLATFORM_DIRS)}; do\n" in EXEC_VIEW_SCRIPT


# —— 脚本演练:本机没有非特权 user namespace(macOS / CI host 都给不了),所以把脚本里的
# 三个绝对路径换成临时目录,``mount`` 换成一个记账的假命令(``--bind`` 落成符号链接,
# tmpfs 只记账),其余逐字照跑。验得住:参数解析、目录建在哪、哪些 bind 被做、bind 源的
# 真目录检查、过渡期目录的判据、坏参数 fail closed。验不住:真的挂载语义 —— 那由
# ``infra/sandbox-image/smoke_test.py`` 与 supervisor 的真镜像集成测试覆盖。

_FAKE_MOUNT = """#!/bin/sh
echo "$*" >> "$EW_MOUNT_LOG"
if [ "$1" = "--bind" ]; then
  src="$2"
  if [ -n "${EW_SWAP_SRC:-}" ] && [ "$2" = "$EW_SWAP_SRC" ]; then
    src="$EW_SWAP_TO"  # 模拟检查之后、mount 之前源被换成了别处的目录
  fi
  if [ -d "$3" ] && [ "$(cd -P "$3" && pwd -P)" = "$(cd -P "$src" && pwd -P)" ]; then
    exit 0  # 经由已经「挂上」的符号链接,目标本来就是源 —— 真内核里是一次同目录 bind
  fi
  rmdir "$3" 2>/dev/null || true
  ln -s "$src" "$3"
fi
"""

#: ``bind_same`` 用的 ``stat -c %d:%i -- <path>``;跟随符号链接(演练里的 bind 就是符号链接)。
_FAKE_STAT = """#!/bin/sh
exec python3 -c 'import os, sys; s = os.stat(sys.argv[1]); print(f"{s.st_dev}:{s.st_ino}")' "$4"
"""

_FAKE_UMOUNT = """#!/bin/sh
echo "umount $*" >> "$EW_MOUNT_LOG"
rm -f "$1" && mkdir "$1"
"""

#: 脚本末尾的 ``exec unshare -Ur -- "$@"``:本机给不了 user namespace,原样执行载荷并记一笔。
_FAKE_UNSHARE = """#!/bin/sh
echo "$*" > "$EW_UNSHARE_LOG"
while [ "$1" != "--" ]; do shift; done
shift
exec "$@"
"""


class _Rehearsal:
    def __init__(self, tmp: Path) -> None:
        tmp = tmp.resolve()  # 判据比的是解析后的路径;macOS 的临时目录在 /var → /private/var 底下
        self.tmp = tmp
        self.mnt = tmp / "mnt"
        self.ws = tmp / "ws"
        self.agent = tmp / "agent"
        self.root = self.mnt / "agents" / "plan-aaaaaaaa"
        self.log = tmp / "mount.log"
        self.bin = tmp / "bin"
        for d in (self.mnt, self.ws, self.agent, self.bin):
            d.mkdir()
        self.unshare_log = tmp / "unshare.log"
        self.extra_env: dict[str, str] = {}
        for name, body in (
            ("mount", _FAKE_MOUNT),
            ("stat", _FAKE_STAT),
            ("umount", _FAKE_UMOUNT),
            ("unshare", _FAKE_UNSHARE),
        ):
            fake = self.bin / name
            fake.write_text(body)
            fake.chmod(0o755)

    def run_raw(self, *args: str) -> subprocess.CompletedProcess[str]:
        script = (
            EXEC_VIEW_SCRIPT.replace("/mnt/workspace", str(self.mnt))
            .replace("/workspace", str(self.ws))
            .replace("/agent", str(self.agent))
        )
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "EW_MOUNT_LOG": str(self.log),
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
        self, root: str, conv: str, compat: list[str], cmd: str, *, own: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        thread = conv if own is None else own
        return self.run_raw(root, conv, thread, str(len(compat)), *compat, "sh", "-c", cmd)

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


def test_rehearsal_legacy_view_hides_other_threads_in_both_views(tmp_path: Path) -> None:
    """老视图:``/workspace`` 与 ``/agent`` 都是 agent 根,但两处的 ``threads/`` 都被盖住,
    只把本线程那一格 bind 回来 —— 发布后新开的对话的工作目录老对话看不到。"""
    r = _Rehearsal(tmp_path)
    proc = r.run(str(r.root), "", [], "echo x > out.txt", own=str(_CONV))

    assert proc.returncode == 0, proc.stderr
    assert (r.root / "out.txt").read_text() == "x\n"
    mounts = r.mounts()
    assert f"--bind {r.root} {r.agent}" in mounts
    assert f"--bind {r.root} {r.ws}" in mounts
    for view in (r.ws, r.agent):
        assert f"-t tmpfs -o size=1k,mode=0500 none {view}/threads" in mounts
        assert f"--bind {r.root}/threads/{_CONV} {view}/threads/{_CONV}" in mounts
    assert not any(_OTHER in m for m in mounts)


def test_rehearsal_legacy_view_without_a_thread_hides_every_thread(tmp_path: Path) -> None:
    r = _Rehearsal(tmp_path)
    proc = r.run(str(r.root), "", [], "true", own="")

    assert proc.returncode == 0, proc.stderr
    mounts = r.mounts()
    assert f"-t tmpfs -o size=1k,mode=0500 none {r.ws}/threads" in mounts
    assert f"-t tmpfs -o size=1k,mode=0500 none {r.agent}/threads" in mounts
    assert not any(m.startswith(f"--bind {r.root}/threads") for m in mounts)


def test_rehearsal_unbound_binds_the_user_root_to_both_views(tmp_path: Path) -> None:
    r = _Rehearsal(tmp_path)
    proc = r.run("", "", [], "true", own="")

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


@pytest.mark.parametrize("which", ["threads", "conversation", "work"])
@pytest.mark.parametrize("plant", ["symlink", "file"])
def test_rehearsal_conversation_dirs_that_are_not_plain_dirs_fail_closed(
    tmp_path: Path, which: str, plant: str
) -> None:
    """``threads`` / 对话目录 / 工作目录被换成符号链接(指向别的对话)或普通文件:
    98 退出,一个 bind 都不做,代码一行都不跑。"""
    r = _Rehearsal(tmp_path)
    other_work = r.root / "threads" / _OTHER / "work"
    other_work.mkdir(parents=True)
    (other_work / "secret.txt").write_text("other conversation")
    target = {
        "threads": r.root / "threads",
        "conversation": r.root / "threads" / str(_CONV),
        "work": r.root / "threads" / str(_CONV) / "work",
    }[which]
    if which == "threads":
        moved = r.root / "threads-real"
        (r.root / "threads").rename(moved)
        source: Path = moved
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        source = other_work if which == "work" else other_work.parent
    if plant == "symlink":
        target.symlink_to(source)
    else:
        target.write_text("planted")

    proc = r.run(str(r.root), str(_CONV), [], "echo ran")

    assert proc.returncode == 98, proc.stderr
    assert "not a plain directory" in proc.stderr
    assert "ran" not in proc.stdout
    assert not any("--bind" in m for m in r.mounts())


@pytest.mark.parametrize("plant", ["symlink", "file"])
def test_rehearsal_planted_platform_dir_is_skipped_not_fatal(tmp_path: Path, plant: str) -> None:
    """agent 根下的 ``uploads`` 被换成指向 ``threads`` 的符号链接(或一个普通文件):这次不挂
    它、写一行 stderr,exec 照常跑 —— 不能借它看见别的对话,也不能让之后每次 exec 都失败。"""
    r = _Rehearsal(tmp_path)
    r.root.mkdir(parents=True)
    if plant == "symlink":
        (r.root / "uploads").symlink_to("threads")
    else:
        (r.root / "uploads").write_text("planted")

    proc = r.run(str(r.root), str(_CONV), [], "echo ran")

    assert proc.returncode == 0, proc.stderr
    assert "ran" in proc.stdout
    assert "uploads is not a plain directory" in proc.stderr
    mounts = r.mounts()
    assert not any(m.endswith(f"{r.ws}/uploads") for m in mounts)
    assert f"--bind {r.root}/inputs {r.ws}/inputs" in mounts


def test_rehearsal_symlinked_agent_root_fails_closed(tmp_path: Path) -> None:
    r = _Rehearsal(tmp_path)
    elsewhere = r.mnt / "agents" / "someone-else"
    elsewhere.mkdir(parents=True)
    r.root.symlink_to(elsewhere)

    proc = r.run(str(r.root), str(_CONV), [], "echo ran")

    assert proc.returncode == 98, proc.stderr
    assert "ran" not in proc.stdout
    assert r.mounts() == []


def test_rehearsal_payload_runs_in_a_nested_user_namespace(tmp_path: Path) -> None:
    """载荷由 ``unshare -Ur`` 起:它所在的 user ns 不拥有挂载那一层,卸不掉覆盖。"""
    r = _Rehearsal(tmp_path)
    proc = r.run(str(r.root), str(_CONV), [], "echo ran")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "ran\n"
    assert r.unshare_log.read_text().split()[:3] == ["-Ur", "--", "sh"]


def test_rehearsal_swapped_work_dir_fails_closed(tmp_path: Path) -> None:
    """检查通过之后、mount 之前工作目录被换掉:bind 之后核对 ``设备:inode`` 对不上 → 卸掉、98。
    (本对话那一格的 bind 在演练里由符号链接父目录表达不出来,真内核上由 smoke 覆盖。)"""
    r = _Rehearsal(tmp_path)
    elsewhere = r.root / "threads" / _OTHER / "work"
    elsewhere.mkdir(parents=True)
    r.extra_env = {
        "EW_SWAP_SRC": str(r.root / "threads" / str(_CONV) / "work"),
        "EW_SWAP_TO": str(elsewhere),
    }
    proc = r.run(str(r.root), str(_CONV), [], "echo ran")

    assert proc.returncode == 98, proc.stderr
    assert "changed" in proc.stderr
    assert "ran" not in proc.stdout
    assert any(m.startswith("umount ") for m in r.mounts())


def test_rehearsal_swapped_platform_source_is_unmounted_and_skipped(tmp_path: Path) -> None:
    r = _Rehearsal(tmp_path)
    elsewhere = r.root / "threads" / _OTHER
    elsewhere.mkdir(parents=True)
    r.extra_env = {"EW_SWAP_SRC": str(r.root / "uploads"), "EW_SWAP_TO": str(elsewhere)}
    proc = r.run(str(r.root), str(_CONV), [], "ls uploads | wc -l")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "0"
    assert "uploads changed while it was being mounted" in proc.stderr
    assert f"umount {r.ws}/uploads" in r.mounts()


def test_rehearsal_compat_stub_created_concurrently_is_still_mapped(tmp_path: Path) -> None:
    """另一次 exec 恰好在「不存在」判断与 ``mkdir`` 之间建出了同一个空挂载桩:``mkdir`` 失败
    不是错(``set -e`` 下曾让整个 exec 失败),目录已经在就接着走,照样映射。"""
    r = _Rehearsal(tmp_path)
    (r.root / "style").mkdir(parents=True)
    racer = r.bin / "mkdir"
    racer.write_text(
        "#!/bin/sh\n"
        'if [ "$#" = 1 ] && [ "${1##*/}" = "$EW_RACE_DIR" ]; then /bin/mkdir "$1"; exit 1; fi\n'
        'exec /bin/mkdir "$@"\n'
    )
    racer.chmod(0o755)
    r.extra_env = {"EW_RACE_DIR": "style"}
    proc = r.run(str(r.root), str(_CONV), ["style"], "echo ran")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "ran\n"
    assert f"--bind {r.root}/style {r.ws}/style" in r.mounts()


def test_rehearsal_compat_dir_maps_only_when_the_conversation_has_none(tmp_path: Path) -> None:
    """过渡期目录:agent 根有、本对话没有(或只有空桩)才映射;本对话自己有内容就不映射;
    根下的单个文件、指向别处的符号链接从不映射。"""
    r = _Rehearsal(tmp_path)
    work = r.root / "threads" / str(_CONV) / "work"
    (r.root / "style").mkdir(parents=True)
    (r.root / "own").mkdir()
    (r.root / "stub").mkdir()
    (r.root / "notes.md").write_text("root file")
    (r.root / "threads" / _OTHER).mkdir(parents=True)
    (r.root / "linked").symlink_to(r.root / "threads" / _OTHER)
    (work / "own").mkdir(parents=True)
    (work / "own" / "x").write_text("mine")
    (work / "stub").mkdir()  # 上一次 exec 留下的空挂载桩

    proc = r.run(
        str(r.root), str(_CONV), ["style", "own", "stub", "notes.md", "absent", "linked"], "true"
    )

    assert proc.returncode == 0, proc.stderr
    mounts = r.mounts()
    assert f"--bind {r.root}/style {r.ws}/style" in mounts
    assert f"--bind {r.root}/stub {r.ws}/stub" in mounts
    assert not any(m.endswith(f"{r.ws}/own") for m in mounts)
    assert not any("notes.md" in m or "absent" in m or "linked" in m for m in mounts)


def test_rehearsal_compat_dirs_are_ignored_in_the_legacy_view(tmp_path: Path) -> None:
    r = _Rehearsal(tmp_path)
    (r.root / "style").mkdir(parents=True)
    proc = r.run(str(r.root), "", ["style"], "true", own="")

    assert proc.returncode == 0, proc.stderr
    assert not any("style" in m for m in r.mounts())


@pytest.mark.parametrize("bad", ["../x", "a/b", "threads", ".tool_results", "..", ""])
def test_rehearsal_bad_compat_dir_fails_closed(tmp_path: Path, bad: str) -> None:
    r = _Rehearsal(tmp_path)
    proc = r.run(str(r.root), str(_CONV), [bad], "echo ran")

    assert proc.returncode == 98, proc.stderr
    assert "ran" not in proc.stdout


@pytest.mark.parametrize(
    ("conv", "own", "count"),
    [("a/b", "", "0"), ("..", "", "0"), ("", "a/b", "0"), ("", "..", "0"), (str(_CONV), "", "x")],
)
def test_rehearsal_bad_ids_or_count_fail_closed(
    tmp_path: Path, conv: str, own: str, count: str
) -> None:
    r = _Rehearsal(tmp_path)
    proc = r.run_raw(str(r.root), conv, own, count, "sh", "-c", "echo ran")

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
