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
    EXEC_SCRIPT_DIR,
    EXEC_SCRIPT_VIEW_DIR,
    EXEC_SHELL_ENV,
    EXEC_VIEW_ARGV_PREFIX,
    EXEC_VIEW_SCRIPT,
    build_exec_command,
    exec_view_argv,
)

_PY = ["python", "-E", "-P", "/tmp/.ew-exec/ew-exec-abc.py"]  # noqa: S108 — 沙箱里的路径,不是本机
_SUM = "0" * 64


def _sh_and_helper() -> tuple[str, str]:
    """把脚本拆成 sh 部分与内嵌 python(``-c '…'`` 那一段单引号串)。"""
    start = EXEC_VIEW_SCRIPT.index(_HELPER_OPEN) + len(_HELPER_OPEN)
    end = EXEC_VIEW_SCRIPT.index(_HELPER_CLOSE, start)
    return (
        EXEC_VIEW_SCRIPT[:start] + EXEC_VIEW_SCRIPT[end:],
        EXEC_VIEW_SCRIPT[start:end],
    )


_HELPER_OPEN = "/usr/local/bin/python3 -I -S -c '"
_HELPER_CLOSE = '\n\' "$root" "$conv" "$own" "$code" "$sum" || { rc=$?; drop; exit "$rc"; }\n'


def test_script_fails_closed_and_names_both_roots() -> None:
    assert EXEC_VIEW_SCRIPT.startswith("set -eu\n")
    _, helper = _sh_and_helper()
    assert "    view = bind(top, agent)\n" in helper
    assert "        ws = bind(work, workspace)\n" in helper  # 对话视图
    assert "        ws = bind(top, workspace)\n" in helper  # 老视图
    assert "        bind(nas, agent)\n        bind(nas, workspace)\n" in helper  # 未绑分支
    assert '    attach(tmpfs((b"size", b"1k")), nas)\n' in helper
    # 载荷跑在嵌套的 user namespace 里:它不拥有这个 mount ns,卸不掉任何一层覆盖;
    # PATH 还原成调用方给的值,unshare 按绝对路径找;B-242 —— HOME 等设回镜像里的值
    assert EXEC_VIEW_SCRIPT.endswith(
        'cd /workspace\nPATH="$ew_path"\nHOME=/home/agent\nMPLCONFIGDIR=/home/agent/.mplconfig\n'
        "TMPDIR=/tmp\nexport HOME MPLCONFIGDIR TMPDIR\nunset BASH_ENV ENV\n"
        'exec /usr/bin/unshare -Ur -- "$@"\n'
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
        # B-242 —— 镜像里的本地目录(只经 local(),实参只有下面这几个字面量)
        "path, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC",
        # 代码文件:读的那一次不跟符号链接、不在 FIFO 上卡住;写的那一次在本次 exec 新建的 tmpfs 里
        "name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=tmp",
        "name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600, dir_fd=box",
        # 清理过期的对话副本时列目录;只读收尾时按挂载点拿 fd(不跟符号链接、只当 O_PATH)
        '".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=store',
        "point, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC",
    }
    for args in opens:
        assert args in trusted or args.endswith(("DIR", "DIR, dir_fd=fd")), args
    calls = re.findall(r"(?<!def )\blocal\(([^)]*)\)", helper)
    assert sorted(calls) == ['"/dev/shm"', '"/tmp"', "AGENTS", "SKILLS", "path"]
    assert "    for path in KEPT:\n" in helper  # path 只取自 KEPT 这个字面量
    assert re.findall(r"libc\.mount\(([^)]*)\)", helper) == [
        "None, fd_path, None, REMOUNT_BIND | flags, None"
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
    assert "RDONLY, NOSUID, NODEV, NOEXEC = 0x1, 0x2, 0x4, 0x8\n" in helper
    assert "REMOUNT_BIND = 0x20 | 0x1000  # MS_REMOUNT | MS_BIND\n" in helper
    assert "SEALED = RDONLY | NOSUID | NODEV | NOEXEC\n" in helper
    # 源挂载已有的 nosuid / nodev / noexec 在 userns 里是锁住的,remount 必须带上
    keep = "((os.ST_NOSUID, NOSUID), (os.ST_NODEV, NODEV), (os.ST_NOEXEC, NOEXEC))"
    assert f"    for st, ms in {keep}:\n" in helper
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
    """B-242 —— ``unshare`` 与 ``sh`` 写绝对路径:外层 shell 的函数 / ``PATH`` 截不到。"""
    assert EXEC_VIEW_ARGV_PREFIX == (
        "/usr/bin/unshare",
        "-Urmi",
        "--propagation",
        "private",
        "--",
        "/bin/sh",
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
        "",
        "",
        *_PY,
    ]


def test_argv_unbound_passes_an_empty_root() -> None:
    assert exec_view_argv("", _PY) == [*EXEC_VIEW_ARGV_PREFIX, "", "", "", "", "", *_PY]


def test_build_exec_command_bound_and_unbound_verbatim() -> None:
    """B-242 —— ``$4`` / ``$5`` 是 ``/tmp`` 下代码文件的名字与 sha256,载荷跑的是
    ``/tmp/.ew-exec/`` 里那一份(挂载阶段放进去的)。"""
    prefix = (
        "umask 077 && /usr/bin/unshare -Urmi --propagation private -- /bin/sh -c "
        + shlex.quote(EXEC_VIEW_SCRIPT)
    )
    assert build_exec_command("plan-aaaaaaaa", "ew-exec-abc.py", _SUM) == (
        prefix + f" ew-exec-view /mnt/workspace/agents/plan-aaaaaaaa '' '' ew-exec-abc.py {_SUM}"
        " python -E -P /tmp/.ew-exec/ew-exec-abc.py"
    )
    assert build_exec_command("", "ew-exec-abc.py", _SUM) == (
        prefix + f" ew-exec-view '' '' '' ew-exec-abc.py {_SUM}"
        " python -E -P /tmp/.ew-exec/ew-exec-abc.py"
    )
    assert (EXEC_SCRIPT_DIR, EXEC_SCRIPT_VIEW_DIR) == ("/tmp", "/tmp/.ew-exec")  # noqa: S108


def test_script_path_is_quoted_not_interpolated() -> None:
    cmd = build_exec_command("", "a b;rm -rf x.py", _SUM)
    argv = shlex.split(cmd[len("umask 077 && ") :])
    assert argv[-1] == "/tmp/.ew-exec/a b;rm -rf x.py"  # noqa: S108
    assert argv[-6:-4] == ["a b;rm -rf x.py", _SUM]


@pytest.mark.parametrize("bad", ["..", "a/b", "a b"])
def test_bad_agent_key_is_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        build_exec_command(bad, "x.py", _SUM)


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
        "",
        "",
        *_PY,
    ]


def test_argv_legacy_view_carries_only_the_own_thread() -> None:
    """老视图:``$2`` 照旧是空串(老视图的约定不变),线程 id 走单独的 ``$3``。"""
    assert exec_view_argv("plan-aaaaaaaa", _PY, thread_id=_CONV) == [
        *EXEC_VIEW_ARGV_PREFIX,
        "/mnt/workspace/agents/plan-aaaaaaaa",
        "",
        str(_CONV),
        "",
        "",
        *_PY,
    ]


def test_argv_unbound_never_carries_a_conversation() -> None:
    """没绑 agent 没有对话视图:对话 id、线程 id 都不发,脚本走未绑分支。"""
    assert exec_view_argv("", _PY, conv_id=_CONV, thread_id=_CONV) == [
        *EXEC_VIEW_ARGV_PREFIX,
        "",
        "",
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
shift 4  # -I -S -c <helper>(其后只有挂载阶段的五个参数)
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
        self.outer = tmp / "outer-tmp"  # 命名空间外的 /tmp(drop() 删这里的代码文件)
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
            .replace('"/tmp/$code"', f'"{self.outer}/$code"')
        )
        env = {
            **os.environ,
            # B-242 —— 外层 shell 给的是 EXEC_SHELL_ENV 那一套;载荷要拿回镜像里的值
            **EXEC_SHELL_ENV,
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
        self, conv: str, cmd: str, *, own: str | None = None, code: str = "", digest: str = ""
    ) -> subprocess.CompletedProcess[str]:
        thread = conv if own is None else own
        return self.run_raw(self.root, conv, thread, code, digest, "sh", "-c", cmd)

    def helper_args(self) -> list[str] | None:
        return self.py_log.read_text().splitlines() if self.py_log.exists() else None


def test_rehearsal_hands_the_mount_phase_to_the_helper_then_runs_the_payload(
    tmp_path: Path,
) -> None:
    r = _Rehearsal(tmp_path)
    proc = r.run(
        str(_CONV),
        'echo "$PWD|$PATH|$HOME|$MPLCONFIGDIR|$TMPDIR|${BASH_ENV-unset}|${ENV-unset}"',
        code="ew-exec-abc.py",
        digest=_SUM,
    )

    assert proc.returncode == 0, proc.stderr
    # 挂载阶段只拿到 $1..$5,载荷 argv 不交给它
    assert r.helper_args() == [r.root, str(_CONV), str(_CONV), "ew-exec-abc.py", _SUM]
    # 载荷:在 /workspace 里、PATH 还原成调用方的、由 unshare -Ur 起;B-242 —— HOME 等是镜像
    # 里的值(外层 shell 的 HOME=/ 不带进来),BASH_ENV / ENV 不留
    assert proc.stdout == (
        f"{r.ws}|{r.bin}:/usr/bin:/bin|/home/agent|/home/agent/.mplconfig|/tmp|unset|unset\n"
    )
    assert r.unshare_log.read_text().split()[:3] == ["-Ur", "--", "sh"]


def test_rehearsal_conversation_id_wins_over_the_thread_id(tmp_path: Path) -> None:
    """对话视图里本对话那一格就是对话 id;老视图里才用 ``$3``。"""
    r = _Rehearsal(tmp_path)
    assert r.run(str(_CONV), "true", own="").returncode == 0
    assert r.helper_args() == [r.root, str(_CONV), str(_CONV), "", ""]
    assert r.run("", "true", own=_OTHER).returncode == 0
    assert r.helper_args() == [r.root, "", _OTHER, "", ""]


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
    ("conv", "own", "code"),
    [
        ("a/b", "", ""),
        ("..", "", ""),
        ("", "a/b", ""),
        ("", "..", ""),
        # B-242 —— 代码文件名只能是 /tmp 下的一个普通名字
        ("", "", "../x.py"),
        ("", "", "a/b.py"),
        ("", "", ".ew-conv"),
        ("", "", ".."),
    ],
)
def test_rehearsal_bad_ids_fail_closed(tmp_path: Path, conv: str, own: str, code: str) -> None:
    r = _Rehearsal(tmp_path)
    proc = r.run_raw(r.root, conv, own, code, _SUM, "sh", "-c", "echo ran")

    assert proc.returncode == 98, proc.stderr
    assert "ran" not in proc.stdout
    assert r.helper_args() is None


# ---------------------------------------------------------------------------
# B-242 —— 外层登录 shell、代码文件、沙箱本地状态。真内核上的行为在
# ``test_exec_view_kernel.py``;这里钉生成的东西与 ``take`` 的逻辑。
# ---------------------------------------------------------------------------


def test_outer_shell_env_keeps_the_login_shell_off_writable_profiles() -> None:
    """envd 固定 ``/bin/bash -l -c``:``HOME`` 指向 root 所有的 ``/``,``bash -l`` 就读不到
    uid 10000 写得进的 ``~/.bash_profile``;``BASH_ENV`` / ``ENV`` 置空(只能覆盖不能删)。"""
    assert dict(EXEC_SHELL_ENV) == {"HOME": "/", "BASH_ENV": "", "ENV": ""}


def test_payload_env_and_covered_dirs_match_the_image_contract() -> None:
    from expert_work.persistence import SANDBOX_AGENTS_ROOT, SANDBOX_SKILLS_ROOT
    from orchestrator.tools.sandbox_image_contract import SANDBOX_HOME, SANDBOX_IMAGE_ENV

    sh, helper = _sh_and_helper()
    assert f"\nHOME={SANDBOX_HOME}\n" in sh
    assert f"\nMPLCONFIGDIR={SANDBOX_IMAGE_ENV['MPLCONFIGDIR']}\n" in sh
    assert sh.index("unset BASH_ENV ENV\n") < sh.index("exec /usr/bin/unshare")
    # uid 10000 在命名空间外写得进的本地目录(镜像里 find -writable 实测),一个不漏
    assert (
        f'KEPT = ("/tmp", "{SANDBOX_HOME}", "/var/tmp", "/run/lock")\n'
        f'AGENTS, SKILLS = "{SANDBOX_AGENTS_ROOT}", "{SANDBOX_SKILLS_ROOT}"\n'
    ) in helper
    # /dev/shm 每次 exec 一块有上限的新 tmpfs(不跨 exec 留东西,也不占沙箱的盘)
    assert '        attach(tmpfs((b"size", b"64m"), (b"mode", b"1777")), shm)\n' in helper


def test_local_view_opens_every_source_before_covering_anything() -> None:
    """盖上之后同一个名字指的就是盖子:源一律先拿 fd,再挂。代码文件在碰 NAS 之前就取走
    (删掉),之后无论哪一步失败,命名空间外的 ``/tmp`` 里都不剩它。"""
    _, helper = _sh_and_helper()
    body = helper[helper.index("def local_view(") : helper.index("def main(")]
    first_cover = body.index("    for path, base, source in kept:\n        tree = private(")
    for opened in ("kept.append((path, base, mine(base, own)))", "pyuser = mine(", "skill = sub("):
        assert body.index(opened) < first_cover, opened
    main = helper[helper.index("def main(") :]
    assert main.index("data = take(") < main.index("nas_view(")


def test_mount_plan_has_the_private_binds_and_read_only_skills() -> None:
    _, helper = _sh_and_helper()
    # /opt/agents 只留自己 key 一格(本对话的 PYTHONUSERBASE);/opt/skills 只留自己 key、只读
    assert "    only(agents, key, lambda slot: private(slot, pyuser))\n" in helper
    assert "    only(skills, key, None if skill is None else skills_ro)\n" in helper
    # 只读、不加 noexec(技能脚本可执行);源挂载锁着 noexec 时 ro() 照源带上
    assert '        ro(bind(skill, slot), SKILLS + "/" + key, RDONLY | NOSUID | NODEV)\n' in helper
    # 代码文件进本次 exec 自己的 tmpfs(有上限),挂在本对话 /tmp 的 .ew-exec 上,只读
    assert (
        '        box = tmpfs((b"size", str(len(data) + 65536).encode()), (b"mode", b"0700"))\n'
        in helper
    )
    assert (
        '        ro(attach(box, sub(tmp_tree, ".ew-exec", True)), "the exec script", SEALED)\n'
    ) in helper
    assert '            die("/tmp is not writable; nowhere to put the exec script")\n' in helper
    # 本对话那一份 = <目录>/.ew-conv/<本对话>;没有对话时一次性 tmpfs(有上限)
    assert '        store = sub(base, ".ew-conv", True)\n' in helper
    assert "        return sub(store, own, True)\n" in helper
    assert (
        '        return attach(tmpfs((b"size", b"256m"), (b"mode", b"0700")), target)\n' in helper
    )


def test_every_other_mount_is_frozen_read_only_last() -> None:
    """review M1 —— 名单之外、沙箱里写得进的路径不能变成共享:挂载阶段最后把命名空间里
    **不是本脚本挂的**每个挂载(``/proc`` 整棵除外:内层 ``unshare`` 要写 ``uid_map``,运行时在
    它下面放的遮罩不是存储)remount
    成只读;本脚本挂的都经 ``attach`` 记下 mount id。被盖住、按路径够不到的跳过。"""
    _, helper = _sh_and_helper()
    assert "    OURS.add(mnt_id(tree))\n" in helper
    main = helper[helper.index("def main(") :]
    assert main.index("local_view(") < main.index("    freeze()\n")
    body = helper[helper.index("def freeze(") : helper.index("def main(")]
    assert 'if int(row[0]) in OURS or point == b"/proc" or point.startswith(b"/proc/"):' in body
    assert "            if mnt_id(fd) == int(row[0]):\n                ro(fd, " in body


def _helper_functions(*names: str) -> dict[str, object]:
    """把内嵌 python 里的几个函数原样拿出来在本机跑(``take`` 不用任何 Linux 专有的东西)。"""
    import ast
    import hashlib
    import stat
    import sys

    _, helper = _sh_and_helper()
    tree = ast.parse(helper)
    picked = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in picked} == set(names)
    body: list[ast.stmt] = list(picked)
    ns: dict[str, object] = {"os": os, "stat": stat, "hashlib": hashlib, "sys": sys}
    exec(compile(ast.Module(body=body, type_ignores=[]), "<helper>", "exec"), ns)  # noqa: S102
    return ns


def _take(tmp: Path, name: str, digest: str) -> bytes:
    take = _helper_functions("take", "say", "die")["take"]
    fd = os.open(tmp, os.O_RDONLY)
    try:
        data = take(fd, name, digest)  # type: ignore[operator]
    finally:
        os.close(fd)
    assert isinstance(data, bytes)
    return data


def test_take_returns_the_code_and_deletes_the_file(tmp_path: Path) -> None:
    import hashlib

    (tmp_path / "ew-exec-a.py").write_bytes(b"print(1)\n")
    assert _take(tmp_path, "ew-exec-a.py", hashlib.sha256(b"print(1)\n").hexdigest()) == (
        b"print(1)\n"
    )
    assert list(tmp_path.iterdir()) == []


def test_take_refuses_a_rewritten_script_and_still_deletes_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import hashlib

    (tmp_path / "ew-exec-a.py").write_bytes(b"import evil\n")
    with pytest.raises(SystemExit) as exc:
        _take(tmp_path, "ew-exec-a.py", hashlib.sha256(b"print(1)\n").hexdigest())
    assert exc.value.code == 98
    assert "does not match its sha256" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_take_never_follows_a_symlink_or_blocks_on_a_fifo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = tmp_path / "secret"
    secret.write_text("s")
    box = tmp_path / "tmp"
    box.mkdir()
    (box / "ew-exec-a.py").symlink_to(secret)
    with pytest.raises(SystemExit) as exc:
        _take(box, "ew-exec-a.py", "")
    assert exc.value.code == 98
    assert "exec script unreadable" in capsys.readouterr().err
    assert secret.read_text() == "s"

    (box / "ew-exec-a.py").unlink()
    os.mkfifo(box / "ew-exec-b.py")
    with pytest.raises(SystemExit) as exc:
        _take(box, "ew-exec-b.py", "")
    assert exc.value.code == 98
    assert "not a regular file" in capsys.readouterr().err
    assert list(box.iterdir()) == []


def test_prune_removes_only_stale_copies_of_other_conversations(tmp_path: Path) -> None:
    """review M2 —— 各对话那份目录闲置超过 ``STALE_S`` 才删(先改名再删);本对话的、最近
    用过的不动;改名到一半留下的 ``.trash-*`` 过期同样删掉。"""
    import time

    ns = _helper_functions("prune")
    ns["STALE_S"] = 3600
    ns["time"] = time
    ns["shutil"] = __import__("shutil")
    old = time.time() - 7200
    for name in ("own", "stale", "fresh", ".trash-x-1"):
        (tmp_path / name / "sub").mkdir(parents=True)
        (tmp_path / name / "sub" / "f").write_text("x")
    for name in ("own", "stale", ".trash-x-1"):
        os.utime(tmp_path / name, (old, old))
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        ns["prune"](fd, "own")  # type: ignore[operator]
    finally:
        os.close(fd)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fresh", "own"]
    assert (tmp_path / "own" / "sub" / "f").read_text() == "x"


def test_rehearsal_early_exits_delete_the_outer_script(tmp_path: Path) -> None:
    """review L5 —— 97 / 98(对话 id 不合法)/ 挂载阶段失败都要把命名空间外的代码文件删掉;
    名字本身不合法时不碰任何文件。"""
    outer = tmp_path / "outer"
    outer.mkdir()
    for case in ("lacks-agent", "bad-conv", "helper-fails", "bad-name"):
        (tmp_path / case).mkdir()
        r = _Rehearsal(tmp_path / case)
        r.outer = outer
        name = "ew-exec-abc.py" if case != "bad-name" else ".ew-exec-abc.py"
        (outer / name).write_text("print(1)")
        conv = "a/b" if case == "bad-conv" else str(_CONV)
        if case == "lacks-agent":
            r.agent.rmdir()
        if case == "helper-fails":
            r.extra_env = {"EW_PY_RC": "98"}
        proc = r.run_raw(r.root, conv, conv, name, _SUM, "sh", "-c", "echo ran")
        assert proc.returncode in (97, 98), (case, proc.stderr)
        assert (outer / name).exists() is (case == "bad-name"), case
