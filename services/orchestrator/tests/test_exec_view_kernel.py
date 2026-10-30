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

import json
import subprocess
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from expert_work.testing import PREBUILT_SANDBOX_IMAGE_ENV, sandbox_image_plan
from orchestrator.tools.exec_view import EXEC_VIEW_ARGV_PREFIX

pytestmark = pytest.mark.integration

_IMAGE = "expert-work-sandbox:exec-view-kernel"
_IMAGE_CONTEXT = Path(__file__).resolve().parents[3] / "infra" / "sandbox-image"
_CONV = "11111111-2222-3333-4444-555555555555"
_OTHER = "99999999-8888-7777-6666-555555555555"

#: 容器里以 root 跑的驱动:布置 NAS、提供 ``ex()`` 跑一次真 exec,其后接每条用例自己的
#: 场景片段;场景把要断言的东西放进 ``result``,驱动最后一行打印成 JSON。
#: 一条场景:场景片段 → 驱动打印的 ``result``。
Scenario = Callable[[str], dict[str, Any]]

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


def argv_of(code, conv="", own=None, compat=(), key="a"):
    chown_all()
    root = "/mnt/workspace/agents/" + key if key else ""
    own = conv if own is None else own
    return [
        "setpriv", "--reuid", "10000", "--regid", "10000", "--clear-groups",
        "sh", "-c", 'umask 077 && exec "$@"', "x",
        *PREFIX, root, conv, own, str(len(compat)), *compat, "sh", "-c", code,
    ]


ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/agent"}


def ex(code, **kw):
    p = subprocess.run(argv_of(code, **kw), capture_output=True, text=True, env=ENV, timeout=60)
    return {"rc": p.returncode, "out": p.stdout, "err": p.stderr}


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
    def run(body: str) -> dict[str, Any]:
        (tmp_path / "prefix.json").write_text(json.dumps(list(EXEC_VIEW_ARGV_PREFIX)))
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


def test_compat_dirs_map_read_only_and_only_when_the_conversation_has_none(
    scenario: Scenario,
) -> None:
    """B-236 M4 —— 过渡期目录只读映射:老锚点读得到,写一律 EROFS(两个对话不能再经由
    agent 根的同一个老目录互相覆盖);本对话自己有内容就不映射;根下的单个文件、指向别处
    的符号链接从不映射。"""
    r = scenario(f"""
        put("agents/a/style/s.md", "anchor\\n")
        mkd("agents/a/own")
        mkd("agents/a/stub")
        put("agents/a/notes.md", "root file")
        mkd("agents/a/threads/{_OTHER}")
        link("agents/a/linked", "threads/{_OTHER}")
        put("agents/a/threads/{_CONV}/work/own/x", "mine")
        mkd("agents/a/threads/{_CONV}/work/stub")
        run = ex(
            "cat style/s.md; (echo w > style/a) 2>&1 | grep -c 'Read-only file system'; "
            "echo w > own/y; " + MOUNTS,
            conv="{_CONV}",
            compat=("style", "own", "stub", "notes.md", "absent", "linked"),
        )
        result["run"] = run
        result["mounts"] = mounts_of(run)
        result["style_a"] = os.path.exists(AG + "/style/a")
        result["legacy"] = ex("ls -A /workspace | grep -c style", own="", compat=("style",))
    """)
    run = r["run"]
    assert run["rc"] == 0, run["err"]
    assert run["out"].splitlines()[:2] == ["anchor", "1"]
    assert r["style_a"] is False  # 经 style/ 的写没有落到 agents/<key>/style/
    mounts = r["mounts"]
    assert mounts["/workspace/style"].startswith("ro,nosuid,nodev,noexec"), mounts
    assert mounts["/workspace/stub"].startswith("ro,nosuid,nodev,noexec"), mounts
    for d in ("own", "notes.md", "absent", "linked"):
        assert f"/workspace/{d}" not in mounts, (d, mounts)
    legacy = r["legacy"]
    assert legacy["rc"] == 0, legacy["err"]
    assert legacy["out"].strip() == "1"  # 老视图:/workspace 就是 agent 根,style 原样在,不挂


def test_mount_target_swapped_for_a_symlink_at_mount_time(scenario: Scenario) -> None:
    """review 3 CRITICAL 的回归用例(PoC 的确定性版):脚本要把过渡期目录 ``style``(agent 根
    下、载荷可写,里面种了一个假的 ``stat``)挂到本对话 ``work/style`` 这个挂载桩上 —— 挂载
    已经开始、还没执行时,把挂载桩换成指向 ``/usr/local/bin`` 的符号链接。按路径名挂载会跟着
    链接把 ``style`` 盖到 ``/usr/local/bin`` 上(旧脚本随后按 ``PATH`` 执行的 ``stat`` 就是
    种下的那个,它还握着这个 mount ns 的全部能力);按 fd 挂,挂载落在原来那个目录上(此刻它
    叫 ``.lnk``),``/usr/local/bin`` 原封不动,载荷照常跑完。"""
    r = scenario(f"""
        put("agents/a/style/s.md", "anchor\\n")
        put("agents/a/style/stat", "#!/bin/sh" + chr(10) + "echo evil-stat >&2" + chr(10)
            + "exec /usr/bin/stat -L " + chr(34) + "$@" + chr(34) + chr(10))
        os.chmod(AG + "/style/stat", 0o755)
        mkd("agents/a/threads/{_CONV}/work/style")
        result["run"] = ex_racing(
            "grep -c ' /usr/local/bin ' /proc/self/mountinfo; "
            "test -x /usr/local/bin/python3 && echo python-ok; cat .lnk/s.md; echo ran",
            "style",
            AG + "/threads/{_CONV}/work/style",
            ".lnk",
            "/usr/local/bin",
            conv="{_CONV}",
            compat=("style",),
        )
    """)
    run = r["run"]
    assert run["fired"] == [0], ("没拦到那次挂载 —— 用例没有真的制造出竞态", run)
    assert run["rc"] == 0, run["err"]
    assert "evil-stat" not in run["err"]
    assert run["out"].splitlines() == ["0", "python-ok", "anchor", "ran"], run


async def test_compat_predicate_matches_the_host_side(
    scenario: Scenario,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """计划测试 10 —— 同一棵树,宿主侧 ``discover_compat_dirs`` 选中的目录与脚本真的
    bind 的目录必须是同一组(两边各写一份判据:不存在,或只是一个空的真目录)。"""
    from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
    from orchestrator.tools.workspace_compat import COMPAT_UNTIL_ENV, discover_compat_dirs
    from orchestrator.tools.workspace_paths import WorkspaceView

    tenant, user = UUID(int=7), UUID(int=8)
    host_root = tmp_path / "host"
    agent = host_root / str(tenant) / str(user) / "agents" / "plan-aaaaaaaa"
    work = agent / "threads" / _CONV / "work"
    for d in ("style", "own", "stub", "linked-target"):
        (agent / d).mkdir(parents=True)
    (agent / "linked").symlink_to(agent / "linked-target")
    (work / "own").mkdir(parents=True)
    (work / "own" / "x").write_text("mine")
    (work / "stub").mkdir()
    (work / "linked-target").symlink_to(agent / "style")
    candidates = ["linked", "linked-target", "own", "stub", "style"]

    monkeypatch.setenv(COMPAT_UNTIL_ENV, "2099-12-31")
    host = await discover_compat_dirs(
        NasWorkspaceStore(root=str(host_root)),
        tenant_id=tenant,
        user_id=user,
        view=WorkspaceView(agent_key="plan-aaaaaaaa", conv_id=UUID(_CONV)),
    )
    r = scenario(f"""
        for d in ("style", "own", "stub", "linked-target"):
            mkd("agents/a/" + d)
        link("agents/a/linked", "linked-target")
        put("agents/a/threads/{_CONV}/work/own/x", "mine")
        mkd("agents/a/threads/{_CONV}/work/stub")
        link("agents/a/threads/{_CONV}/work/linked-target", "/mnt/workspace/agents/a/style")
        run = ex(MOUNTS, conv="{_CONV}", compat={candidates!r})
        result["run"] = run
        result["bound"] = sorted(
            p.rsplit("/", 1)[-1] for p in mounts_of(run) if p.rsplit("/", 1)[-1] in {candidates!r}
        )
    """)
    assert host == ("stub", "style")
    assert tuple(r["bound"]) == host
