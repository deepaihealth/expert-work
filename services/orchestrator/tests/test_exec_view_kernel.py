"""B-236 review 3 —— exec 视图脚本在真镜像、真内核上的挂载语义。

``test_exec_view.py`` 只演练得了 sh 那一层(本机没有非特权 user namespace,挂载阶段又
全在内嵌 python 里直接发系统调用)。这里每条用例起一个 ``--privileged`` 容器(沙箱镜像),
照云上的样子摆好:``/mnt/workspace`` 是平台建的符号链接、指向 NAS 上的用户根;脚本以
uid 10000、``umask 077`` 经 ``EXEC_VIEW_ARGV_PREFIX`` 原样跑(与 ``build_exec_command``
同一条 argv)。``--privileged`` 只给测试驱动用(布置目录、seccomp 监听),脚本本身照旧在
非特权用户的 user namespace 里跑。

回归用例 :func:`test_mount_target_swapped_for_a_symlink_at_mount_time` 用 seccomp
user-notification 把脚本的 ``mount`` / ``move_mount`` 系统调用(旧脚本则是 ``mount`` 命令的
``execve``)挂住,趁它还没执行把挂载桩
换成指向 ``/usr/local/bin`` 的符号链接再放行 —— 这正是 review 3 PoC 里竞态窗口的确定性版本(PoC 是
在检查与 mount 之间换):按路径名挂载的旧脚本必中,按 fd 挂的不中。(fanotify 的权限事件
更直接,但 Docker Desktop 的 linuxkit 内核没开 ``CONFIG_FANOTIFY_ACCESS_PERMISSIONS``。)
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from expert_work.testing import PREBUILT_SANDBOX_IMAGE_ENV, sandbox_image_plan
from orchestrator.tools.exec_view import EXEC_SHELL_ENV, EXEC_VIEW_ARGV_PREFIX, build_exec_command
from orchestrator.tools.sandbox_image_contract import SANDBOX_IMAGE_ENV

pytestmark = pytest.mark.integration

_IMAGE = "expert-work-sandbox:exec-view-kernel"
_IMAGE_CONTEXT = Path(__file__).resolve().parents[3] / "infra" / "sandbox-image"
_CONV = "11111111-2222-3333-4444-555555555555"
_OTHER = "99999999-8888-7777-6666-555555555555"

#: 容器里以 root 跑的驱动:布置 NAS、提供 ``ex()`` 跑一次真 exec,其后接每条用例自己的
#: 场景片段;场景把要断言的东西放进 ``result``,驱动最后一行打印成 JSON。
#: 一条场景:场景片段 → 驱动打印的 ``result``。
Scenario = Callable[..., dict[str, Any]]

_DRIVER = r'''
import ctypes, fcntl, json, os, platform, select, socket, struct, subprocess, tempfile

PREFIX = json.load(open("/t/prefix.json"))
U = "/nas/u"
AG = U + "/agents/a"
os.makedirs(U)
os.symlink(U, "/mnt/workspace")
os.makedirs("/workspace", exist_ok=True)
MOUNTS = "awk '{print $5, $6}' /proc/self/mountinfo"


def put(rel, text=""):
    path = os.path.join(U, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def mkd(rel):
    os.makedirs(os.path.join(U, rel), exist_ok=True)


def link(rel, target):
    path = os.path.join(U, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.symlink(target, path)


def chown_all():
    for base, dirs, files in os.walk(U):
        for name in [*dirs, *files]:
            os.lchown(os.path.join(base, name), 10000, 10000)
    os.chown(U, 10000, 10000)


def argv_of(code, conv="", own=None, key="a"):
    chown_all()
    root = "/mnt/workspace/agents/" + key if key else ""
    own = conv if own is None else own
    return [
        "setpriv", "--reuid", "10000", "--regid", "10000", "--clear-groups",
        "sh", "-c", 'umask 077 && exec "$@"', "x",
        *PREFIX, root, conv, own, "", "", "sh", "-c", code,
    ]


ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/agent"}


def ex(code, env=None, **kw):
    p = subprocess.run(
        argv_of(code, **kw), capture_output=True, text=True, env={**ENV, **(env or {})}, timeout=60
    )
    return {"rc": p.returncode, "out": p.stdout, "err": p.stderr}


def ex_shell(command, env, script=None):
    """The cloud path: envd writes the script into /tmp as uid 10000, then runs
    ``/bin/bash -l -c <command>`` as uid 10000 with cwd /mnt/workspace."""
    if script is not None:
        name, text = script
        with open("/tmp/" + name, "w") as f:
            f.write(text)
        os.chown("/tmp/" + name, 10000, 10000)
    chown_all()
    p = subprocess.run(
        ["setpriv", "--reuid", "10000", "--regid", "10000", "--clear-groups",
         "/bin/bash", "-l", "-c", command],
        capture_output=True, text=True, env=env, cwd="/mnt/workspace", timeout=60,
    )
    return {"rc": p.returncode, "out": p.stdout, "err": p.stderr}


def plant(path, text, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)
    os.chown(path, 10000, 10000)


def mounts_of(run):
    """``MOUNTS`` 的输出(载荷打印的若干行)里挂载点 -> 选项。"""
    table = {}
    for line in run["out"].splitlines():
        parts = line.split(" ")
        if len(parts) == 2 and parts[0].startswith("/"):
            table[parts[0]] = parts[1]
    return table


LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.syscall.restype = ctypes.c_long
ARM = platform.machine() in ("aarch64", "arm64")
NR_SECCOMP, NR_MOUNT, NR_MOVE_MOUNT, NR_EXECVE = (277, 40, 429, 221) if ARM else (317, 165, 429, 59)


class SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_char_p)]


def install_mount_trap():
    """当前进程(及其后代)的 ``mount`` / ``move_mount`` / ``execve`` 交给 user-notification
    监听者;返回监听 fd。"""
    ins = [
        struct.pack("=HBBI", 0x20, 0, 0, 0),  # ld [nr]
        struct.pack("=HBBI", 0x15, 3, 0, NR_MOUNT),
        struct.pack("=HBBI", 0x15, 2, 0, NR_MOVE_MOUNT),
        struct.pack("=HBBI", 0x15, 1, 0, NR_EXECVE),
        struct.pack("=HBBI", 0x06, 0, 0, 0x7FFF0000),  # ALLOW
        struct.pack("=HBBI", 0x06, 0, 0, 0x7FC00000),  # USER_NOTIF
    ]
    prog = SockFprog(len(ins), b"".join(ins))
    fd = LIBC.syscall(
        ctypes.c_long(NR_SECCOMP), ctypes.c_long(1), ctypes.c_long(8), ctypes.byref(prog)
    )
    assert fd >= 0, os.strerror(ctypes.get_errno())
    return fd


def read_str(pid, addr):
    with open(f"/proc/{pid}/mem", "rb") as mem:
        mem.seek(addr)
        return mem.read(4096).split(b"\0", 1)[0].decode()


def mount_target(pid, nr, args):
    """这次挂载的目标:``mount`` / ``move_mount`` 的路径参数,或(空路径时)目标 fd 指向的
    路径;``execve`` 只认 ``mount`` 命令(按路径名挂载的那种实现),取它 argv 的最后一个。"""
    if nr == NR_EXECVE:
        if os.path.basename(read_str(pid, args[0])) != "mount":
            return ""
        with open(f"/proc/{pid}/mem", "rb") as mem:
            mem.seek(args[1])
            ptrs = []
            while not ptrs or ptrs[-1]:
                ptrs.append(struct.unpack("=Q", mem.read(8))[0])
        return read_str(pid, ptrs[-2])
    if nr == NR_MOUNT:
        return read_str(pid, args[1])
    path = read_str(pid, args[3]) if args[3] else ""
    return path or os.readlink(f"/proc/{pid}/fd/{ctypes.c_int(args[2]).value}")


def ex_racing(code, name, path, swap_name, target, **kw):
    """跑一次 exec;它第一次要把东西挂到名为 ``name`` 的目标上时(挂载的系统调用、或按路径
    名挂载的 ``mount`` 命令的 ``execve`` 已发出、还没执行),把 NAS 上的 ``path`` 与一个指向
    ``target`` 的符号链接互换名字,再放它执行。"""
    lnk = os.path.join(os.path.dirname(path), swap_name)
    os.symlink(target, lnk)
    argv = argv_of(code, **kw)
    os.lchown(lnk, 10000, 10000)
    parent, child = socket.socketpair()
    out, err = tempfile.TemporaryFile(), tempfile.TemporaryFile()
    pid = os.fork()
    if pid == 0:
        try:
            os.dup2(out.fileno(), 1)
            os.dup2(err.fileno(), 2)
            fd = install_mount_trap()
            socket.send_fds(child, [b"x"], [fd])
            os.execvpe(argv[0], argv, ENV)
        finally:
            os._exit(127)
    _, fds, _, _ = socket.recv_fds(parent, 1, 1)
    listener = fds[0]
    fired = []
    poller = select.poll()
    poller.register(listener, select.POLLIN)
    done = None
    while done is None:
        if poller.poll(100):
            buf = bytearray(80)
            try:
                fcntl.ioctl(listener, 0xC0502100, buf, True)  # SECCOMP_IOCTL_NOTIF_RECV
            except OSError:
                buf = None
            if buf is not None:
                nid, npid, _, nr, _, _, *args = struct.unpack("=QIIiIQ6Q", buf)
                try:
                    if not fired and mount_target(npid, nr, args).endswith("/" + name):
                        fired.append(LIBC.renameat2(-100, path.encode(), -100, lnk.encode(), 2))
                except OSError:
                    pass
                resp = struct.pack("=QqiI", nid, 0, 0, 1)  # SECCOMP_USER_NOTIF_FLAG_CONTINUE
                try:
                    fcntl.ioctl(listener, 0xC0182101, resp)  # SECCOMP_IOCTL_NOTIF_SEND
                except OSError:
                    pass
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited:
            done = os.waitstatus_to_exitcode(status)
    out.seek(0)
    err.seek(0)
    return {"rc": done, "out": out.read().decode(), "err": err.read().decode(), "fired": fired}


result = {}
'''


@pytest.fixture(scope="module")
def image() -> str:
    try:
        probe = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("docker CLI unavailable")
    else:
        if probe.returncode != 0:
            pytest.skip("docker daemon unreachable")
    plan = sandbox_image_plan(_IMAGE, _IMAGE_CONTEXT)
    built = subprocess.run(  # noqa: S603
        ["docker", *plan.argv],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    if built.returncode != 0:
        if plan.prebuilt is not None:
            pytest.fail(
                f"{PREBUILT_SANDBOX_IMAGE_ENV}={plan.prebuilt} 不在本机:{built.stderr[-400:]}"
            )
        pytest.skip(f"sandbox image build failed: {built.stderr[-400:]}")
    return _IMAGE


@pytest.fixture
def scenario(image: str, tmp_path: Path) -> Scenario:
    def run(body: str, data: object = None, files: dict[str, str] | None = None) -> dict[str, Any]:
        (tmp_path / "prefix.json").write_text(json.dumps(list(EXEC_VIEW_ARGV_PREFIX)))
        (tmp_path / "data.json").write_text(json.dumps(data))
        for name, text in (files or {}).items():
            (tmp_path / name).write_text(text)
            (tmp_path / name).chmod(0o644)
        # pytest 建的 tmp_path 是 0700(属主是跑测试的账号):Linux CI 上容器里 uid 10000 进
        # 不去,exec 里读 ``/t/<files>`` 就是 EACCES。Docker Desktop 的共享目录不报这个错。
        tmp_path.chmod(0o755)
        (tmp_path / "driver.py").write_text(
            _DRIVER + textwrap.dedent(body) + "\nprint(json.dumps(result))\n"
        )
        proc = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "docker",
                "run",
                "--rm",
                "--privileged",
                "-v",
                f"{tmp_path}:/t:ro",
                "--entrypoint",
                "python3",
                image,
                "-I",
                "/t/driver.py",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr[-3000:]
        out: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
        return out

    return run


def test_conversation_view_mounts(scenario: Scenario) -> None:
    r = scenario(f"""
        put("agents/a/uploads/u.txt", "up\\n")
        put("agents/a/threads/{_OTHER}/work/secret.txt", "B")
        put("shared/legacy.md", "LEGACY\\n")
        run = ex(
            "echo x > out.txt; echo y > /agent/a.txt; cat uploads/u.txt; ls /agent/threads; "
            "ls -A /mnt/workspace | wc -l; cat shared/legacy.md; "
            "touch shared/w 2>/dev/null || echo shared-ro; pwd; " + MOUNTS,
            conv="{_CONV}",
        )
        result["run"] = run
        result["mounts"] = mounts_of(run)
        result["out_txt"] = open(AG + "/threads/{_CONV}/work/out.txt").read()
        result["a_txt"] = open(AG + "/a.txt").read()
    """)
    run = r["run"]
    assert run["rc"] == 0, run["err"]
    assert run["out"].splitlines()[:6] == ["up", _CONV, "0", "LEGACY", "shared-ro", "/workspace"]
    assert (r["out_txt"], r["a_txt"]) == ("x\n", "y\n")
    mounts = r["mounts"]
    for point in ("/agent", "/workspace", "/agent/threads", f"/agent/threads/{_CONV}"):
        assert point in mounts, mounts
    for d in ("uploads", "inputs", ".tool_results"):
        assert mounts[f"/workspace/{d}"].startswith("rw"), mounts
    assert mounts["/workspace/shared"].startswith("ro,nosuid,nodev,noexec"), mounts


def test_legacy_and_unbound_views(scenario: Scenario) -> None:
    r = scenario(f"""
        put("agents/a/threads/{_OTHER}/work/secret.txt", "B")
        result["own"] = ex(
            "ls threads; ls /agent/threads; echo x > out.txt", own="{_CONV}"
        )
        result["none"] = ex("ls -A threads /agent/threads", own="")
        result["unbound"] = ex("ls /workspace /agent", key="")
        result["out_txt"] = open(AG + "/out.txt").read()
    """)
    assert r["own"] == {"rc": 0, "out": f"{_CONV}\n{_CONV}\n", "err": ""}
    assert r["out_txt"] == "x\n"
    none = r["none"]
    assert none["rc"] == 0, none["err"]
    assert _OTHER not in none["out"]
    unbound = r["unbound"]
    assert unbound["out"].split() == ["/agent:", "agents", "/workspace:", "agents"]


def test_conversation_dirs_that_are_not_plain_dirs_fail_closed(scenario: Scenario) -> None:
    """``threads`` / 对话目录 / 工作目录被换成符号链接(指向别的对话)或普通文件:98 退出,
    代码一行都不跑。"""
    r = scenario(f"""
        import shutil
        other = AG + "/threads/{_OTHER}/work"
        for which in ("threads", "conversation", "work"):
            for plant in ("symlink", "file"):
                shutil.rmtree(AG, ignore_errors=True)
                put("agents/a/threads/{_OTHER}/work/secret.txt", "B")
                target = {{
                    "threads": AG + "/threads",
                    "conversation": AG + "/threads/{_CONV}",
                    "work": AG + "/threads/{_CONV}/work",
                }}[which]
                if which == "threads":
                    os.rename(target, AG + "/threads-real")
                    source = AG + "/threads-real"
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    source = other if which == "work" else os.path.dirname(other)
                if plant == "symlink":
                    os.symlink(source, target)
                else:
                    open(target, "w").write("planted")
                result[which + "/" + plant] = ex("cat secret.txt; echo ran", conv="{_CONV}")
    """)
    assert len(r) == 6
    for case, run in r.items():
        assert isinstance(run, dict)
        assert run["rc"] == 98, (case, run)
        assert "not a plain directory" in run["err"], (case, run)
        assert run["out"] == "", (case, run)


def test_planted_symlinks_are_skipped_or_refused(scenario: Scenario) -> None:
    """平台目录的源(agent 根下)与目标(本对话 ``work/`` 里的挂载桩)、用户根的 ``shared``
    被换成指向别处的符号链接:这一格不挂、写一行 stderr、exec 照跑,链接指向的地方上
    什么都没挂;agent 根本身是符号链接 → 98。"""
    r = scenario(f"""
        os.makedirs("/srv/outside")
        put("agents/a/threads/{_OTHER}/work/secret.txt", "B")
        link("agents/a/uploads", "threads")
        mkd("agents/a/inputs")
        link("agents/a/threads/{_CONV}/work/inputs", "/srv/outside")
        mkd("shared-real")
        put("shared-real/x.md", "S")
        link("shared", "agents/a/threads")
        result["skip"] = ex(
            "ls -A uploads inputs/ shared/ 2>&1; ls -A /srv/outside; "
            "grep -c ' /srv/outside ' /proc/self/mountinfo; echo ran",
            conv="{_CONV}",
        )
        link("agents/k3", "a")
        result["root"] = ex("echo ran", conv="{_CONV}", key="k3")
    """)
    skip = r["skip"]
    assert skip["rc"] == 0, skip["err"]
    assert "uploads is not a plain directory in the agent root" in skip["err"]
    assert "/workspace/inputs is not a directory; inputs/ not mounted" in skip["err"]
    assert "shared is not a plain directory in the user root" in skip["err"]
    assert _OTHER not in skip["out"] and "x.md" not in skip["out"]
    assert skip["out"].splitlines()[-2:] == ["0", "ran"]
    root = r["root"]
    assert root["rc"] == 98 and root["out"] == "", root


def test_mount_target_swapped_for_a_symlink_at_mount_time(scenario: Scenario) -> None:
    """review 3 CRITICAL 的回归用例(PoC 的确定性版):脚本要把平台目录 ``uploads``(agent 根
    下、载荷可写,里面种了一个假的 ``stat``)挂到本对话 ``work/uploads`` 这个挂载桩上 —— 挂载
    已经开始、还没执行时,把挂载桩换成指向 ``/usr/local/bin`` 的符号链接。按路径名挂载会跟着
    链接把 ``uploads`` 盖到 ``/usr/local/bin`` 上(旧脚本随后按 ``PATH`` 执行的 ``stat`` 就是
    种下的那个,它还握着这个 mount ns 的全部能力);按 fd 挂,挂载落在原来那个目录上(此刻它
    叫 ``.lnk``),``/usr/local/bin`` 原封不动,载荷照常跑完。"""
    r = scenario(f"""
        put("agents/a/uploads/s.md", "anchor\\n")
        put("agents/a/uploads/stat", "#!/bin/sh" + chr(10) + "echo evil-stat >&2" + chr(10)
            + "exec /usr/bin/stat -L " + chr(34) + "$@" + chr(34) + chr(10))
        os.chmod(AG + "/uploads/stat", 0o755)
        mkd("agents/a/threads/{_CONV}/work/uploads")
        result["run"] = ex_racing(
            "grep -c ' /usr/local/bin ' /proc/self/mountinfo; "
            "test -x /usr/local/bin/python3 && echo python-ok; cat .lnk/s.md; echo ran",
            "uploads",
            AG + "/threads/{_CONV}/work/uploads",
            ".lnk",
            "/usr/local/bin",
            conv="{_CONV}",
        )
    """)
    run = r["run"]
    assert run["fired"] == [0], ("没拦到那次挂载 —— 用例没有真的制造出竞态", run)
    assert run["rc"] == 0, run["err"]
    assert "evil-stat" not in run["err"]
    assert run["out"].splitlines() == ["0", "python-ok", "anchor", "ran"], run


# ---------------------------------------------------------------------------
# B-242 —— 外层登录 shell、代码文件、沙箱本地状态(真镜像真内核)。
# ---------------------------------------------------------------------------

_B = "33333333-4444-5555-6666-777777777777"
_KEPT = ("/tmp", "/home/agent", "/var/tmp", "/run/lock")  # noqa: S108 — 沙箱里的路径


def _shell_env(*, fixed: bool = True) -> dict[str, str]:
    """envd 给外层 ``bash -l`` 的环境:建沙箱时的镜像那套 + 每次 exec 的;``fixed=False`` 是
    B-242 之前(没有 ``EXEC_SHELL_ENV``)。"""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        **SANDBOX_IMAGE_ENV,
        "PYTHONUSERBASE": "/opt/agents/a",
    }
    return {**env, **EXEC_SHELL_ENV} if fixed else env


def _shell_call(code: str, conv: str, n: int, *, digest: str | None = None) -> dict[str, str]:
    """云上那条路的一次 exec:``files.write`` 的名字与内容 + ``build_exec_command`` 的命令串。"""
    name = f"ew-exec-{n:032x}.py"
    sha = digest or hashlib.sha256(code.encode()).hexdigest()
    command = build_exec_command("a", name, sha, conv_id=UUID(conv), thread_id=UUID(conv))
    return {"command": command, "name": name, "code": code}


_B_PROBE = """\
import os
print("hi")
print(os.path.exists(os.path.expanduser("~/.bash_profile")))
for line in open("/proc/self/mountinfo"):
    f = line.split()
    if f[4] in {kept!r}:
        print(f[4], f[3])
print({a!r} in open("/proc/self/mountinfo").read())
"""


def test_profile_planted_by_one_conversation_never_runs_in_another(scenario: Scenario) -> None:
    """B-242 P0(10-31 测试环境实测复现的那一条):A 往 ``~/.bash_profile`` 追加打印标记、
    定义同名 ``unshare`` / ``sh`` 函数;B 跑 ``print("hi")``。

    * A 写进的是它自己那份 HOME,B 的视图里根本没有这个文件;
    * 就算命名空间外的 ``/home/agent/.bash_profile`` 被种上(B-242 之前留下的,或任何别的
      途径),外层 ``bash -l`` 的 ``HOME=/`` 也让它读不到;
    * 去掉 ``EXEC_SHELL_ENV``(B-242 之前):标记打印出来了(种子是活的),但绝对路径的
      ``unshare`` / ``sh`` 截不到 —— 两层各自成立;
    * B 的 mountinfo 里本地目录全是 ``.ew-conv/<B>``,一处 A 都没有。"""
    evil = (
        "echo MARKER >&2\n"
        "unshare() { echo HIJACK; }\n"
        "sh() { echo HIJACK; }\n"
        "export PATH=/home/agent/bin:$PATH\n"
    )
    a_code = (
        "import os\n"
        "with open(os.path.expanduser('~/.bash_profile'), 'a') as f:\n"
        f"    f.write({evil!r})\n"
        "print(os.path.expanduser('~'))\n"
    )
    data = {
        "a": _shell_call(a_code, _CONV, 1),
        "b": [_shell_call(_B_PROBE.format(kept=_KEPT, a=_CONV), _B, n) for n in (2, 3, 4)],
        "fixed": _shell_env(),
        "before": _shell_env(fixed=False),
    }
    r = scenario(
        """
        D = json.load(open("/t/data.json"))
        def call(c, env):
            return ex_shell(c["command"], env, (c["name"], c["code"]))
        result["a"] = call(D["a"], D["fixed"])
        result["b"] = call(D["b"][0], D["fixed"])
        plant("/home/agent/.bash_profile", open("/t/evil").read())
        for name in ("unshare", "sh"):
            plant("/home/agent/bin/" + name, "#!/bin/sh" + chr(10) + "echo HIJACK" + chr(10), 0o755)
        result["outer"] = call(D["b"][1], D["fixed"])
        result["before"] = call(D["b"][2], D["before"])
        result["writable"] = ex_shell(
            "for f in / /etc/profile /etc/profile.d /etc/bash.bashrc; do "
            "[ -w $f ] && echo $f; done; true",
            D["fixed"],
        )
        """,
        data,
        files={"evil": evil},
    )
    a = r["a"]
    assert a == {"rc": 0, "out": "/home/agent\n", "err": ""}, a
    for case in ("b", "outer"):
        run = r[case]
        assert run["rc"] == 0, (case, run)
        assert "MARKER" not in run["err"] and "HIJACK" not in run["out"] + run["err"], (case, run)
        lines = run["out"].splitlines()
        assert lines[:2] == ["hi", "False"], (case, run)
        assert lines[-1] == "False", ("A 出现在 B 的 mountinfo 里", case, run)
        roots = dict(line.split(" ") for line in lines[2:-1])
        assert sorted(roots) == sorted(_KEPT), (case, roots)
        for point, root in roots.items():
            assert root.endswith(f"/.ew-conv/{_B}"), (case, point, root)
    before = r["before"]
    assert "MARKER" in before["err"], ("没修时外层 bash -l 读了种下的 profile", before)
    assert "HIJACK" not in before["out"] + before["err"], before
    assert before["out"].splitlines()[0] == "hi", before
    assert r["writable"] == {"rc": 0, "out": "", "err": ""}, (
        "外层 shell 读的系统 profile 必须写不进"
    )


def test_exec_script_is_checked_private_and_gone_afterwards(scenario: Scenario) -> None:
    """B-242 P0 —— 代码文件:载荷在本次 exec 自己的只读 tmpfs 里看到它,命名空间外的那份在
    跑之前就删了;内容被换过(哈希对不上)→ 98、一行都不跑;被 SIGKILL(超时同理)之后什么
    都不剩,下一次 exec 的 ``/tmp/.ew-exec`` 里只有它自己。"""
    probe = (
        "import os\n"
        "print(__file__)\n"
        "print(sorted(os.listdir('/tmp/.ew-exec')))\n"
        "try:\n"
        "    open(__file__, 'a')\n"
        "except OSError as e:\n"
        "    print(e.errno)\n"
        "print(sorted(n for n in os.listdir('/tmp') if n.startswith('ew-exec')))\n"
    )
    good = _shell_call(probe, _CONV, 1)
    tampered = _shell_call("print('evil')\n", _CONV, 2, digest=hashlib.sha256(b"x").hexdigest())
    killed = _shell_call("import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n", _CONV, 3)
    after = _shell_call(probe, _CONV, 4)
    r = scenario(
        """
        D = json.load(open("/t/data.json"))
        for key in ("good", "tampered", "killed", "after"):
            c = D[key]
            result[key] = ex_shell(c["command"], D["env"], (c["name"], c["code"]))
            result[key]["left"] = os.path.exists("/tmp/" + c["name"])
        """,
        {
            "good": good,
            "tampered": tampered,
            "killed": killed,
            "after": after,
            "env": _shell_env(),
        },
    )
    for key in ("good", "tampered", "killed", "after"):
        assert r[key]["left"] is False, ("命名空间外的代码文件还在", key, r[key])
    for key, name in (("good", good["name"]), ("after", after["name"])):
        run = r[key]
        assert run["rc"] == 0, run
        expected = [f"/tmp/.ew-exec/{name}", str([name]), "30", "[]"]  # noqa: S108
        assert run["out"].splitlines() == expected, run
    tampered_run = r["tampered"]
    assert tampered_run["rc"] == 98, tampered_run
    assert "does not match its sha256" in tampered_run["err"], tampered_run
    assert tampered_run["out"] == ""
    assert r["killed"]["rc"] != 0, r["killed"]


_WRITE = (
    "for d in /tmp ~ /var/tmp /run/lock /dev/shm; do echo A > $d/a-mark; done; "
    "site=$(python3 -m site --user-site) && mkdir -p $site && echo X=1 > $site/moda.py; "
    "echo done"
)
#: ``pip`` 与 exec_python(``-E -P``)都认 ``PYTHONUSERBASE``:``site`` 直接读 ``os.environ``,
#: ``-E`` 挡不住它(镜像里实测),所以两种解释器旗标都要看得到 / 看不到。
#: ``/dev/shm`` 是每次 exec 一块新的 tmpfs:同一对话的下一次也看不到(review M2)。
_READ = (
    "for d in /tmp ~ /var/tmp /run/lock /dev/shm; do [ -e $d/a-mark ] && echo $d; done; "
    "python3 -c 'import moda' 2>/dev/null && echo moda; "
    "python3 -E -P -c 'import moda' 2>/dev/null && echo moda-E; echo end"
)
_ALL_OF_A = [*_KEPT, "moda", "moda-E", "end"]


def test_local_state_is_private_to_the_conversation(scenario: Scenario) -> None:
    """B-242 P1 —— A 在 ``/tmp`` / HOME / ``/var/tmp`` / ``/run/lock`` 留的文件、
    pip 装进 ``PYTHONUSERBASE``(``/opt/agents/a``)的模块:A 的下一次 exec 还在,B 一样都
    看不到。老视图按线程 id 走同一份;没有对话的 exec 每次一份新的。"""
    pip = {"PYTHONUSERBASE": "/opt/agents/a"}
    r = scenario(
        f"""
        P = {pip!r}
        result["a1"] = ex({_WRITE!r}, env=P, conv="{_CONV}")
        result["a2"] = ex({_READ!r}, env=P, conv="{_CONV}")
        result["b"] = ex({_READ!r}, env=P, conv="{_B}")
        result["legacy_a"] = ex({_READ!r}, env=P, own="{_CONV}")
        result["legacy_other"] = ex({_READ!r}, env=P, own="{_OTHER}")
        result["none1"] = ex({_WRITE!r}, env=P, own="")
        result["none2"] = ex({_READ!r}, env=P, own="")
        result["outer"] = sorted(os.listdir("/tmp"))
        """
    )
    assert r["a1"]["out"] == "done\n", r["a1"]
    assert r["a2"]["out"].splitlines() == _ALL_OF_A, r["a2"]
    assert r["legacy_a"]["out"].splitlines() == _ALL_OF_A, r["legacy_a"]
    for case in ("b", "legacy_other", "none2"):
        assert r[case] == {"rc": 0, "out": "end\n", "err": ""}, (case, r[case])
    assert r["none1"]["out"] == "done\n", r["none1"]
    assert r["outer"] == [".ew-conv"], "命名空间外的 /tmp 只多一个存放各对话那份的目录"


def test_skills_are_only_the_own_key_and_read_only(scenario: Scenario) -> None:
    """B-242 P1 —— ``/opt/skills`` 只剩自己 key 那一格、只读(``EROFS``),技能脚本照样能
    执行;``/opt/agents`` 只剩自己 key;载荷的 HOME / TMPDIR 是镜像里的值。源挂载本身带
    ``noexec``(本地后端的 tmpfs)时只读 remount 必须保留它,否则锁住的旗标去不掉、98。"""
    probe = (
        "ls -A /opt/skills; ls -A /opt/agents; cat /opt/skills/a/s/SKILL.md; "
        "/opt/skills/a/s/run.sh 2>&1; "
        "touch /opt/skills/a/x 2>&1 | grep -c 'Read-only file system'; "
        "awk '$5 == \"/opt/skills/a\" {print $6}' /proc/self/mountinfo; echo $HOME $TMPDIR"
    )
    r = scenario(
        f"""
        plant("/opt/skills/a/s/SKILL.md", "skill" + chr(10))
        plant("/opt/skills/a/s/run.sh", "#!/bin/sh" + chr(10) + "echo skill-ran" + chr(10), 0o755)
        plant("/opt/skills/other/secret.md", "other" + chr(10))
        result["own"] = ex({probe!r}, conv="{_B}")
        result["unbound"] = ex("ls -A /opt/skills | wc -l; ls -A /opt/agents | wc -l", key="")
        subprocess.run(
            ["mount", "-t", "tmpfs", "-o", "noexec,nosuid,nodev,mode=1777", "tmpfs", "/opt/skills"],
            check=True,
        )
        plant("/opt/skills/a/s/SKILL.md", "skill" + chr(10))
        result["noexec"] = ex(
            "cat /opt/skills/a/s/SKILL.md; "
            "awk '$5 == \\"/opt/skills/a\\" {{print $6}}' /proc/self/mountinfo",
            conv="{_B}",
        )
        """
    )
    own = r["own"]
    assert own["rc"] == 0, own
    lines = own["out"].splitlines()
    assert lines[:5] == ["a", "a", "skill", "skill-ran", "1"], own
    assert lines[5].split(",")[0] == "ro" and "noexec" not in lines[5], own
    assert lines[6] == "/home/agent /tmp", own
    assert r["unbound"]["out"].split() == ["0", "0"], ("没绑 agent:两处都是空的", r["unbound"])
    noexec = r["noexec"]
    assert noexec["rc"] == 0, noexec
    skill, options = noexec["out"].splitlines()
    assert skill == "skill"
    assert options.split(",")[0] == "ro" and "noexec" in options.split(","), noexec


# ---------------------------------------------------------------------------
# B-242 安全审查 —— 只读收尾(M1)、过期清理与大小上限(M2)、/proc 跨 exec(L1)、IPC(L2)。
# ---------------------------------------------------------------------------

_FROZEN_PROBE = (
    "for d in /srv/open /srv/m /dev/mqueue; do "
    "touch $d/x 2>&1 | grep -q 'Read-only file system' && echo \"RO $d\"; done; "
    "for d in /tmp ~ /var/tmp /run/lock /dev/shm /workspace /agent /opt/agents/a; do "
    'touch $d/w && echo "W $d"; done; '
    "echo x > /dev/null && echo devnull; "
    'awk \'$5 == "/" || $5 == "/srv/m" {print $5, substr($6, 1, 2)}\' /proc/self/mountinfo'
)


def test_unlisted_writable_paths_are_read_only_in_the_view(scenario: Scenario) -> None:
    """review M1 —— 名单是对着镜像数出来的,云上 pod 还会有别的挂载。名单之外、uid 10000
    写得进的路径(这里造两处:根文件系统上一个 0777 目录、一块 1777 的 tmpfs;镜像本身的
    ``/dev/mqueue`` 也是)在视图里一律只读,不再是跨对话的通道;名单里的、本对话的工作区
    照常可写,``/dev/null`` 照常可写。"""
    r = scenario(
        f"""
        os.makedirs("/srv/open")
        os.chmod("/srv/open", 0o777)
        os.makedirs("/srv/m")
        subprocess.run(["mount", "-t", "tmpfs", "-o", "mode=1777", "tmpfs", "/srv/m"], check=True)
        result["run"] = ex({_FROZEN_PROBE!r}, conv="{_CONV}")
        result["landed"] = sorted(os.listdir("/srv/open")) + sorted(os.listdir("/srv/m"))
        """
    )
    run = r["run"]
    assert run["rc"] == 0, run
    lines = run["out"].splitlines()
    assert lines[:3] == ["RO /srv/open", "RO /srv/m", "RO /dev/mqueue"], run
    writable = [*_KEPT, "/dev/shm", "/workspace"]  # noqa: S108
    assert lines[3:12] == [f"W {d}" for d in [*writable, "/agent", "/opt/agents/a"]] + [
        "devnull"
    ], run
    assert sorted(lines[12:]) == ["/ ro", "/srv/m ro"], run
    assert r["landed"] == []


def test_stale_copies_are_pruned_and_ephemeral_mounts_are_bounded(scenario: Scenario) -> None:
    """review M2 —— 闲置超过两小时的对话副本由下一次(别的对话的)exec 删掉,本对话自己的
    永远不删(它这次就要用);没有对话时的一次性 tmpfs、``/dev/shm``、代码文件那块都有上限。"""
    age = (
        "import time\n"
        "old = time.time() - 3 * 3600\n"
        "for b in ('/tmp', '/home/agent', '/var/tmp', '/run/lock', '/opt/agents'):\n"
        f"    os.utime(b + '/.ew-conv/{_CONV}', (old, old))\n"
    )
    sizes = "df -k --output=target,size /tmp /home/agent /dev/shm | tail -n +2 | tr -s ' '"
    r = scenario(
        f"""
        P = {{"PYTHONUSERBASE": "/opt/agents/a"}}
        result["a1"] = ex("echo k > /tmp/keep; echo done", env=P, conv="{_CONV}")
        exec({age!r})
        result["a2"] = ex("cat /tmp/keep", env=P, conv="{_CONV}")
        exec({age!r})
        result["b"] = ex("echo b", env=P, conv="{_B}")
        result["stores"] = {{
            b: sorted(os.listdir(b + "/.ew-conv"))
            for b in ("/tmp", "/home/agent", "/var/tmp", "/run/lock", "/opt/agents")
        }}
        result["none"] = ex({sizes!r}, own="")
        result["conv"] = ex({sizes!r}, conv="{_B}")
        """
    )
    assert r["a1"]["out"] == "done\n", r["a1"]
    assert r["a2"]["out"] == "k\n", ("自己的副本过期了也不许删", r["a2"])
    assert r["b"]["out"] == "b\n", r["b"]
    for base, names in r["stores"].items():
        assert names == [_B], (base, names)
    assert r["none"]["out"].split() == [
        "/tmp",  # noqa: S108
        "262144",
        "/home/agent",
        "262144",
        "/dev/shm",  # noqa: S108
        "65536",
    ]
    conv = r["conv"]["out"].split()
    assert conv[-2:] == ["/dev/shm", "65536"], r["conv"]  # noqa: S108


_WATCH = """\
import json, os, time
me = os.getpid()
mine = os.readlink("/proc/self/ns/mnt")
log = {}
end = time.time() + 8
while time.time() < end:
    for p in os.listdir("/proc"):
        if not p.isdigit() or int(p) == me or p in log:
            continue
        res = {}
        try:
            if os.readlink("/proc/%s/ns/mnt" % p) == mine:
                continue
            res["ns"] = "ok"
        except OSError as e:
            res["ns"] = e.errno
        try:
            os.listdir("/proc/%s/root/" % p)
            res["root"] = "ok"
        except OSError as e:
            res["root"] = e.errno
        try:
            d = "/proc/%s/root/opt/agents/a/lib/python3.12/site-packages" % p
            os.makedirs(d, exist_ok=True)
            open(d + "/zz.pth", "w").write("import os")
            res["pth"] = "ok"
        except OSError as e:
            res["pth"] = e.errno
        log[p] = res
    time.sleep(0.01)
json.dump(log, open("/workspace/w.log", "w"))
"""


def test_lingering_process_cannot_reach_another_conversation_through_proc(
    scenario: Scenario,
) -> None:
    """review L1 —— A 留下一个后台进程,一直扫 ``/proc``;B 随后来跑。每次 exec 的载荷都在
    自己嵌套的 user namespace 里:A 对 B(以及沙箱里任何别的进程)的 ``/proc/<pid>/root``、
    ``/proc/<pid>/ns/mnt`` 都是 ``EACCES``,往 B 的 ``PYTHONUSERBASE`` 种 ``.pth`` 落不下去。
    (进程本身仍看得见,是 P2。)"""
    a_code = "setsid python3 -I /t/watch.py </dev/null >/dev/null 2>/workspace/w.err & echo started"
    b_code = (
        "echo $$ > /workspace/pid; sleep 3; "
        "ls $(python3 -m site --user-site) 2>/dev/null | grep -c pth; true"
    )
    r = scenario(
        f"""
        import time
        P = {{"PYTHONUSERBASE": "/opt/agents/a"}}
        result["a"] = ex({a_code!r}, env=P, conv="{_CONV}")
        time.sleep(1)
        result["b"] = ex({b_code!r}, env=P, conv="{_B}")
        log = AG + "/threads/{_CONV}/work/w.log"
        for _ in range(100):
            if os.path.exists(log):
                break
            time.sleep(0.2)
        if os.path.exists(log):
            result["log"] = json.load(open(log))
        else:  # 监视进程没写出结果:把它的 stderr 和进程表带出来,别只剩一个 FileNotFoundError
            err = AG + "/threads/{_CONV}/work/w.err"
            procs = {{}}
            for pid in os.listdir("/proc"):
                try:
                    raw = open("/proc/" + pid + "/cmdline", "rb").read()
                    procs[pid] = raw.replace(b"\\0", b" ").decode()
                except (OSError, ValueError):
                    pass
            result["log"] = {{
                "missing": open(err).read() if os.path.exists(err) else "no w.err",
                "procs": procs,
            }}
        result["b_pid"] = open(AG + "/threads/{_B}/work/pid").read().strip()
        """,
        files={"watch.py": _WATCH},
    )
    assert r["a"]["out"] == "started\n", r["a"]
    assert r["b"]["out"] == "0\n", ("A 的 .pth 落进了 B 的 user site", r["b"])
    log = r["log"]
    assert r["b_pid"] in log, ("监视进程没看到 B 的载荷 —— 用例没有真的制造出场景", log)
    assert log[r["b_pid"]] == {"ns": 13, "root": 13, "pth": 13}, log[r["b_pid"]]
    leaked = {pid: res for pid, res in log.items() if "ok" in res.values()}
    assert leaked == {}, leaked


def test_sysv_ipc_is_private_to_each_exec(scenario: Scenario) -> None:
    """review L2 —— 每次 exec 自己的 IPC namespace:A 建的 System V 消息队列,B 看不到,
    A 自己的下一次 exec 也看不到(平台里没有依赖跨 exec IPC 的东西)。"""
    count = "ipcs -q | grep -c '^0x'; true"
    r = scenario(
        f"""
        result["a"] = ex("ipcmk -Q >/dev/null; " + {count!r}, conv="{_CONV}")
        result["a2"] = ex({count!r}, conv="{_CONV}")
        result["b"] = ex({count!r}, conv="{_B}")
        """
    )
    assert [r[k]["out"] for k in ("a", "a2", "b")] == ["1\n", "0\n", "0\n"], r


#: 10-31 测试环境 ACS 沙箱 main 容器的挂载表里,本仓库镜像之外多出来的那几类(见手册 §81.8d):
#: 0777 的目录挂载(``/mnt/envd`` 放着 root 的 envd 二进制、``/var/opt/sandbox/agent-token``)、
#: CSI 的 tmpfs、单文件 bind(``/etc/hosts`` 一类)、``/sys/fs/cgroup`` 的可写 tmpfs 与其下只读
#: 的 cgroup 挂载,以及运行时在 ``/proc`` 下放的遮罩(``/dev/null`` 单文件 bind、只读 tmpfs、
#: 只读 proc 子树)。``--privileged`` 的测试容器没有这些遮罩,这里照样摆出来。
_ACS_SHAPE = """
def sh(cmd):
    subprocess.run(["sh", "-c", cmd], check=True)

for d in ("/mnt/envd", "/var/opt/sandbox/agent-token", "/srv/envd-src", "/srv/token-src"):
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o777)
sh("mount --bind /srv/envd-src /mnt/envd")
sh("mount --bind /srv/token-src /var/opt/sandbox/agent-token")
os.makedirs("/run/csi/mount-root", exist_ok=True)
sh("mount -t tmpfs -o mode=1777 tmpfs /run/csi/mount-root")
open("/srv/file", "w").write("f")
os.chmod("/srv/file", 0o666)
open("/etc/ew-file", "w").close()
sh("mount --bind /srv/file /etc/ew-file")
sh("mount -t tmpfs -o mode=1777 tmpfs /sys/fs/cgroup && mkdir /sys/fs/cgroup/pids "
   "&& mount -t tmpfs -o ro tmpfs /sys/fs/cgroup/pids")
sh("mount --bind /dev/null /proc/kcore")
masked = [d for d in ("/proc/acpi", "/proc/scsi", "/proc/driver") if os.path.isdir(d)][0]
sh("mount -t tmpfs -o ro tmpfs " + masked)
sh("mount --bind /proc/sys /proc/sys && mount -o remount,bind,ro /proc/sys")
result["masked"] = masked
"""

_ACS_PROBE = (
    "for t in /mnt/envd/x /var/opt/sandbox/agent-token/x /run/csi/mount-root/x "
    "/sys/fs/cgroup/x; do touch $t 2>&1 | grep -q 'Read-only file system' && echo \"RO $t\"; "
    "done; (echo x >> /etc/ew-file) 2>&1 | grep -q 'Read-only file system' "
    "&& echo 'RO /etc/ew-file'; cat /etc/ew-file; echo; head -c 1 /proc/kcore | wc -c; "
    "cat /etc/hosts >/dev/null && echo hosts; "
    "touch /tmp/w /workspace/w && echo W; "
    "awk '$5 ~ /^\\/(mnt\\/envd|var\\/opt|run\\/csi|etc\\/ew-file|sys\\/fs\\/cgroup)/ "
    "{print $5, substr($6, 1, 2)}' /proc/self/mountinfo"
)


def test_freeze_handles_the_acs_mount_table_shape(scenario: Scenario) -> None:
    """安全审查追问 —— ``freeze`` 在 ACS 那张挂载表的形状上不能以 98 退出:单文件 bind、
    ``/proc`` 下的遮罩(整棵 ``/proc`` 不动)、``/sys/fs/cgroup`` 的 tmpfs 与只读子挂载都要过;
    ``/mnt/envd`` / ``agent-token`` 这类 0777 目录、CSI tmpfs、可写的单文件在视图里只读。"""
    r = scenario(
        _ACS_SHAPE
        + f"""
result["run"] = ex({_ACS_PROBE!r}, conv="{_CONV}")
D = json.load(open("/t/data.json"))
result["shell"] = ex_shell(D["command"], D["env"], (D["name"], D["code"]))
""",
        _shell_call("print('ok')\n", _B, 7) | {"env": _shell_env()},
    )
    run = r["run"]
    assert run["rc"] == 0, run
    lines = run["out"].splitlines()
    assert lines[:5] == [
        "RO /mnt/envd/x",
        "RO /var/opt/sandbox/agent-token/x",
        "RO /run/csi/mount-root/x",
        "RO /sys/fs/cgroup/x",
        "RO /etc/ew-file",
    ], run
    assert lines[5:9] == ["f", "0", "hosts", "W"], ("可读的照样可读,名单里的照样可写", run)
    options = dict(line.split(" ") for line in lines[9:])
    for point in (
        "/mnt/envd",
        "/var/opt/sandbox/agent-token",
        "/run/csi/mount-root",
        "/etc/ew-file",
    ):
        assert options.get(point) == "ro", (point, options)
    assert r["masked"].startswith("/proc/"), r
    assert r["shell"]["out"] == "ok\n", ("云上那条路(bash -l + 代码文件)同样不出 98", r["shell"])
