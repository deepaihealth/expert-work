"""每次 exec 一个私有的 ``/workspace``(B-60 spec §4.3)—— 两个后端共用的命令串单源。

沙箱里 ``/mnt/workspace`` 挂的是整个用户根(热沙箱按 ``(tenant, user)`` 复用,挂载
点没法按 agent 分)。每次 exec 起一个新的 user + mount namespace(``unshare -Urm``,
无特权 mount 的前提是当前 uid 在新 user ns 里映射成 root),在里面把 agent 目录 bind
成 ``/workspace``、``shared/`` 只读 bind 进来、再用 tmpfs 把 ``/mnt/workspace`` 整个
盖掉 —— 于是 exec 里的任何代码(含子进程)写 ``/workspace/x`` 落的就是
``agents/<key>/x``,而且看不见别的 agent。命名空间按进程树,同一沙箱里两个 agent 的
exec 并发互不影响。

脚本体是**字面量**,参数走位置参数(``$1`` = bind 源,余下 = python argv),不做字符串
拼接。``infra/sandbox-image/runner.py`` 是镜像代码、不能 import 仓库,自带一份逐字相同
的字面量,``test_exec_view_script_matches_the_sandbox_image`` 用 ``ast`` 钉两份相等。

失败即失败:``set -e`` 加上内嵌 python 任一步挂载失败都非零退出,exec 报错,**不回落**到
用户根。

B-236 —— 按对话分目录。``$2`` 是对话 id(空 = 老视图,发布前已开的对话),``$3`` 是
本对话的线程 id(老视图里用它把自己那一格 ``threads/<id>/`` 留出来;对话视图里与
``$2`` 相同),``$4`` 是过渡期映射目录的个数 ``n``,其后 ``n`` 个目录名,再往后才是
python argv。对话视图:

* ``/agent`` = agent 根(该 agent 所有对话共用);``/workspace`` = 本对话自己的
  ``threads/<conv>/work``。
* ``/agent/threads`` 被一块空 tmpfs 盖住,只把**本对话**那一格 bind 回来(状态投影写
  ``threads/<conv>/PLAN.md`` 要用)。老视图同理:``/workspace/threads`` 与
  ``/agent/threads`` 都盖住,只留自己那一格;线程 id 为空就整个 ``threads/`` 都不给看。
* 平台目录(``uploads`` / ``inputs`` / ``.tool_results``)仍在 agent 根,读写 bind 进
  ``/workspace``,平台发给模型的相对路径照旧可用。
* 过渡期目录:agent 根下已有的同名目录,本对话里没有(或只有一个空的挂载桩)时
  **只读** bind 进 ``/workspace``(与 ``shared/`` 同一组 flag:ro,nosuid,nodev,noexec)——
  老锚点照读,但两个对话不能再经由 agent 根的同一个老目录(如 ``output/``)互相覆盖;
  要写就写本对话自己的目录,要共享就写 ``/agent/<目录>/``。只映射目录,根下的单个
  文件从不映射。宿主侧的判据
  (:func:`orchestrator.tools.workspace_compat.discover_compat_dirs`)是同一个,宿主侧
  文件工具对过渡期路径的写同样拒绝。

**NAS 上的路径一律按 fd 挂,不按路径名挂。** agent 根以下的任何路径(含本对话
``work/`` 里的挂载桩)都可能被上一次 exec 留下的后台进程随时换成符号链接或普通文件
(它对这些目录有写权限)。按路径名「先检查、再 mount」中间永远有窗口,mount 会跟着
换上来的符号链接挂到别处 —— review 3 的 PoC 就是在这个窗口里把可写的 ``uploads`` 挂到
``/usr/local/bin`` 上、让脚本随后执行的 ``stat`` 变成攻击者的程序(此时它还握着这个
mount ns 的全部能力,``/mnt/workspace`` 也还没盖住)。所以整个挂载阶段由脚本里内嵌的
一段 python(``/usr/local/bin/python3 -I -S``,只用 ctypes 直接发系统调用,不起任何
外部命令)完成:

* 源与目标都从可信的起点 fd(``/mnt/workspace``、``/agent``、``/workspace``、刚挂上的
  那棵树的根)逐段 ``openat(O_PATH|O_NOFOLLOW|O_DIRECTORY)`` 拿到;缺的挂载桩在已钉住的
  父 fd 上 ``mkdirat``,再 ``O_NOFOLLOW`` 重开并核对是目录。名字之后再被换掉,手里的 fd
  指的还是原来那个目录。
* bind = ``open_tree(OPEN_TREE_CLONE)`` + ``move_mount``(两端都是 fd);盖 ``threads/``
  与 ``/mnt/workspace`` 的 tmpfs 用 ``fsopen`` / ``fsmount`` 建好再 ``move_mount``,本对话那
  一格的挂载桩在 tmpfs 挂上之前就在它里面建好。``shared/`` 的只读 remount 走
  ``/proc/self/fd/<那棵树>``(内核 5.10 还没有 ``mount_setattr``)。
* agent 根:``$1`` 的最后两段(``agents`` 与 key)不许是符号链接,其余前缀(NAS 挂载点,
  云上是平台建的符号链接)照常解析。``threads`` / 本对话目录 / 工作目录不合格 → 98 退出;
  平台目录与过渡期目录不合格 → 写一行 stderr、这次不挂,exec 照常跑(一个被种下的普通
  文件不该让这个 agent 之后的每一次 exec 都失败);用户根的 ``shared`` 不是真目录同样跳过。
* 第一次碰 NAS 之后不再按 ``PATH`` 找任何命令:python 与最后的 ``unshare`` 都写绝对路径,
  脚本开头另把 ``PATH`` 钉成系统目录(纵深防御),交给载荷之前还原成调用方给的值。
  内嵌 python 只用 ASCII、不含单引号:它是 sh 单引号串,而且 ``-c`` 的源码按 locale 解码。

**载荷跑在嵌套的 user namespace 里。** 脚本最后 ``exec /usr/bin/unshare -Ur -- "$@"``:载荷所在的
user namespace 不拥有这个 mount namespace,于是它 ``umount`` 不掉盖住 ``/mnt/workspace`` /
``threads/`` 的那几层(B-60 spec §五 记过的「能挪开 tmpfs」那条残留就此关上);它即便自己再
``unshare -m``,复制过去的挂载也是锁住的(``MNT_LOCKED``),同样卸不掉。载荷里的 uid 仍是
0(映射回外层的 0,即沙箱用户),文件属主不变。云上 ACS 沙箱不挂 seccomp,本地后端的
seccomp profile 已为外层 ``unshare`` 放行同一组系统调用(``open_tree`` / ``move_mount`` /
``fsopen`` / ``fsconfig`` / ``fsmount`` 在那条无条件放行的规则里),内层不需要新的放行。

镜像里没有 ``/agent`` 时以 97 退出(没钉上新镜像);参数或 bind 源不合法以 98 退出。
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from uuid import UUID

from orchestrator.tools.sandbox_image_contract import SANDBOX_PYTHON_FLAGS
from orchestrator.tools.workspace_paths import agent_nas_root, require_compat_dir_name

#: 逐条见 spec §4.3。``REMOUNT_RO``(remount,bind,ro,nosuid,nodev,noexec):userns 里 remount 不能
#: **去掉**源挂载上锁住的 flag,但可以**加**,三个都写上永远合法。``size=1k`` 让盖住
#: 用户根的 tmpfs 没法当可写盘。**不挂用户根的 uploads**:它在 ``agents/<key>/uploads``
#: (对话视图里 bind 进来的是 agent 根的那一份)。
EXEC_VIEW_SCRIPT = """\
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

#: ``sh -c <script> <$0> <$1> <$2> <$3> <$4> <compat…> <python argv…>``;``$0`` 只是报错时的名字。
EXEC_VIEW_ARGV_PREFIX: tuple[str, ...] = (
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


def exec_view_argv(
    agent_key: str,
    python_argv: Sequence[str],
    *,
    conv_id: UUID | None = None,
    thread_id: UUID | None = None,
    compat_dirs: Sequence[str] = (),
) -> list[str]:
    """完整 argv:前缀 + ``$1``(绑了 agent 是 NAS 上的真实目录,未绑是空串)+ ``$2``
    (对话 id,老视图 / 未绑为空串)+ ``$3``(本对话的线程 id,没有为空串)+ ``$4``
    (过渡期目录个数)+ 目录名 + python。

    对话视图与线程 id 只在绑了 agent 时成立;未绑时一律不发。目录名先在这里按与脚本相同的
    规则校验一遍(脚本自己还会再查,不合法就 98 退出)。
    """
    root = agent_nas_root(agent_key) if agent_key else ""
    conv = str(conv_id) if root and conv_id is not None else ""
    thread = str(thread_id) if root and thread_id is not None else ""
    compat = [require_compat_dir_name(d) for d in compat_dirs] if conv else []
    return [
        *EXEC_VIEW_ARGV_PREFIX,
        root,
        conv,
        thread,
        str(len(compat)),
        *compat,
        *python_argv,
    ]


def build_exec_command(
    agent_key: str,
    script_path: str,
    *,
    conv_id: UUID | None = None,
    thread_id: UUID | None = None,
    compat_dirs: Sequence[str] = (),
) -> str:
    """ACS 后端 ``commands.run``(``/bin/bash -l -c``)用的命令串。

    ``umask 077`` 在最前:命名空间里 ``mkdir -p "$root"`` 建出来的 agent 目录也要落
    ``0o700``(与 ``NasWorkspaceStore._DIR_MODE`` 同一个数字)。其余全部 ``shlex.join``,
    脚本路径与目录名不进任何拼接。
    """
    argv = exec_view_argv(
        agent_key,
        ["python", *SANDBOX_PYTHON_FLAGS, script_path],
        conv_id=conv_id,
        thread_id=thread_id,
        compat_dirs=compat_dirs,
    )
    return "umask 077 && " + shlex.join(argv)
