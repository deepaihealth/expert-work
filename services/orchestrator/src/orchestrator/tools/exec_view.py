"""每次 exec 一个私有的 ``/workspace``(B-60 spec §4.3)—— 两个后端共用的命令串单源。

沙箱里 ``/mnt/workspace`` 挂的是整个用户根(热沙箱按 ``(tenant, user)`` 复用,挂载
点没法按 agent 分)。每次 exec 起一个新的 user + mount(B-242 起还有 IPC)namespace(``unshare -Urmi``,
无特权 mount 的前提是当前 uid 在新 user ns 里映射成 root),在里面把 agent 目录 bind
成 ``/workspace``、``shared/`` 只读 bind 进来、再用 tmpfs 把 ``/mnt/workspace`` 整个
盖掉 —— 于是 exec 里的任何代码(含子进程)写 ``/workspace/x`` 落的就是
``agents/<key>/x``,NAS 上别的 agent(B-236 起还有别的对话)的目录不在这个视图里。命名
空间按进程树,同一沙箱里两个 agent 的 exec 并发互不影响。

B-242 —— **沙箱本地状态也按对话分。** 热沙箱按 ``(tenant, user)`` 复用,同一个用户所有
agent、所有对话的 exec 共用同一个容器 / microVM 的本地盘。uid 10000 在命名空间外写得进的
地方(镜像实测:``/tmp``、``/home/agent``、``/dev/shm``、``/var/tmp``、``/run/lock``、
``/opt/agents``、``/opt/skills``)在视图里一律换掉:

* ``/tmp``、``/home/agent``、``/var/tmp``、``/run/lock`` bind 成本对话自己的那一份
  ``<目录>/.ew-conv/<本对话>``(同一对话的下一次 exec 还在,别的对话看不到);没有对话(``$3``
  为空)时是本次 exec 的空 tmpfs(256 MB 上限)。老视图也按 ``$3``(本对话线程 id)分,与新
  布局同一个口径。``/dev/shm`` 每次 exec 一块新的 64 MB tmpfs;``-i`` 另起 IPC namespace。
* 每次 exec 先刷新自己那份的时间戳,再删掉闲置超过 2 小时的别的对话的那份(先改名、再看
  这期间有没有人用过,没有才删),沙箱本地盘不会被攒满。
* ``/opt/agents`` 盖空 tmpfs,只把 ``/opt/agents/<key>``(``PYTHONUSERBASE``)bind 成
  ``/opt/agents/.ew-conv/<本对话>``;``/opt/skills`` 盖空 tmpfs,只把 ``/opt/skills/<key>``
  只读 bind 回来(源挂载带 ``noexec`` 时保留,锁住的旗标去不掉)。
* exec 代码文件(云上 ``files.write`` 到命名空间外的 ``/tmp``):先删、再核对 ``$5`` 给的
  sha256,放进本次 exec 独有的只读 tmpfs ``/tmp/.ew-exec/``;命名空间里的进程全退出,它
  就跟着没了(超时被杀也一样)。
* 载荷的 ``HOME`` / ``MPLCONFIGDIR`` / ``TMPDIR`` 由脚本设回镜像里的值 —— 云上外层
  ``bash -l`` 的 ``HOME`` 被换成了 ``/``(:data:`EXEC_SHELL_ENV`),不让它读到写得进的
  ``~/.bash_profile``。
* 兜底:上面的名单是对着镜像数的。挂载阶段最后把命名空间里**不是本脚本挂的**每个挂载都
  remount 成只读(``/proc`` 除外,内层 ``unshare`` 要写 ``uid_map``)—— 名单外写得进的路径
  只读,不会变成跨对话的通道。本脚本挂的经 ``attach`` 记下 mount id;被盖住、按路径够不
  到的跳过。

进程互相看得见(``ps``、同 uid 能发信号)仍在,是 B-242 P2。

脚本体是**字面量**,参数走位置参数(``$1`` = bind 源,余下 = python argv),不做字符串
拼接。``infra/sandbox-image/runner.py`` 是镜像代码、不能 import 仓库,自带一份逐字相同
的字面量,``test_exec_view_script_matches_the_sandbox_image`` 用 ``ast`` 钉两份相等。

失败即失败:``set -e`` 加上内嵌 python 任一步挂载失败都非零退出,exec 报错,**不回落**到
用户根。

B-236 —— 按对话分目录。``$2`` 是对话 id(空 = 老视图,发布前已开的对话),``$3`` 是
本对话的线程 id(老视图里用它把自己那一格 ``threads/<id>/`` 留出来;对话视图里与
``$2`` 相同),``$4`` / ``$5`` 是代码文件名与 sha256(B-242,空 = 没有代码文件),再往后
是 python argv。对话视图:

* ``/agent`` = agent 根(该 agent 所有对话共用);``/workspace`` = 本对话自己的
  ``threads/<conv>/work``。
* ``/agent/threads`` 被一块空 tmpfs 盖住,只把**本对话**那一格 bind 回来(状态投影写
  ``threads/<conv>/PLAN.md`` 要用)。老视图同理:``/workspace/threads`` 与
  ``/agent/threads`` 都盖住,只留自己那一格;线程 id 为空就整个 ``threads/`` 都不给看。
* 平台目录(``uploads`` / ``inputs`` / ``.tool_results``)仍在 agent 根,读写 bind 进
  ``/workspace``,平台发给模型的相对路径照旧可用。
* agent 根下原有的基础目录(风格锚、模板……)不在这里挂:对话开场时控制面在宿主侧把它们
  复制进本对话的 ``work/``(:mod:`orchestrator.graph_builder.conversation_seed`),exec 看到的
  就是本对话自己的那份。

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
  平台目录不合格 → 写一行 stderr、这次不挂,exec 照常跑(一个被种下的普通
  文件不该让这个 agent 之后的每一次 exec 都失败);用户根的 ``shared`` 不是真目录同样跳过。
* 第一次碰 NAS 之后不再按 ``PATH`` 找任何命令:python 与最后的 ``unshare`` 都写绝对路径,
  脚本开头另把 ``PATH`` 钉成系统目录(纵深防御),交给载荷之前还原成调用方给的值。B-242 起
  外层的 ``unshare`` / ``sh`` 也写绝对路径。
  内嵌 python 只用 ASCII、不含单引号:它是 sh 单引号串,而且 ``-c`` 的源码按 locale 解码。

**载荷跑在嵌套的 user namespace 里。** 脚本最后 ``exec /usr/bin/unshare -Ur -- "$@"``:载荷所在的
user namespace 不拥有这个 mount namespace,于是它 ``umount`` 不掉盖住 ``/mnt/workspace`` /
``threads/`` 的那几层(B-60 spec §五 记过的「能挪开 tmpfs」那一条由此关上;B-242 的各层
覆盖同理);它即便自己再 ``unshare -m``,复制过去的挂载也是锁住的(``MNT_LOCKED``),同样
卸不掉。载荷里的 uid 仍是 0(映射回外层的 0,即沙箱用户),文件属主不变。云上 ACS 沙箱
不挂 seccomp,本地后端的 seccomp profile 已为外层 ``unshare`` 放行同一组系统调用
(``open_tree`` / ``move_mount`` / ``fsopen`` / ``fsconfig`` / ``fsmount`` 在那条无条件放行的
规则里),内层不需要新的放行。

镜像里没有 ``/agent`` 时以 97 退出(没钉上新镜像);参数或 bind 源不合法、代码文件的
sha256 对不上以 98 退出。
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from uuid import UUID

from orchestrator.tools.sandbox_image_contract import SANDBOX_PYTHON_FLAGS
from orchestrator.tools.workspace_paths import agent_nas_root

#: 逐条见 spec §4.3。``REMOUNT_RO``(remount,bind,ro,nosuid,nodev,noexec):userns 里 remount 不能
#: **去掉**源挂载上锁住的 flag,但可以**加**,三个都写上永远合法。``size=1k`` 让盖住
#: 用户根的 tmpfs 没法当可写盘。**不挂用户根的 uploads**:它在 ``agents/<key>/uploads``
#: (对话视图里 bind 进来的是 agent 根的那一份)。
EXEC_VIEW_SCRIPT = """\
set -eu
ew_path="${PATH-}"
PATH=/usr/sbin:/usr/bin:/sbin:/bin
root="$1"; conv="$2"; own="$3"; code="$4"; sum="$5"; shift 5
drop() {
  case "$code" in
    ""|*/*|.*) ;;
    *) /bin/rm -f -- "/tmp/$code" ;;
  esac
}
if [ ! -d /agent ]; then
  echo "ew-exec-view: image lacks /agent" >&2
  drop
  exit 97
fi
for id in "$conv" "$own"; do
  case "$id" in
    */*|.|..) echo "ew-exec-view: bad conversation id" >&2; drop; exit 98 ;;
  esac
done
case "$code" in
  */*|.*) echo "ew-exec-view: bad script name" >&2; exit 98 ;;
esac
if [ -n "$conv" ]; then
  own="$conv"
fi
/usr/local/bin/python3 -I -S -c '
import ctypes, hashlib, os, shutil, stat, sys, time

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
RDONLY, NOSUID, NODEV, NOEXEC = 0x1, 0x2, 0x4, 0x8
REMOUNT_BIND = 0x20 | 0x1000  # MS_REMOUNT | MS_BIND
SEALED = RDONLY | NOSUID | NODEV | NOEXEC
DIR = os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC
PLATFORM_DIRS = ("uploads", "inputs", ".tool_results")
# Sandbox-local dirs every exec could write: each is replaced by a copy kept per conversation
# (<dir>/.ew-conv/<conv>), or a fresh tmpfs when there is no conversation.
KEPT = ("/tmp", "/home/agent", "/var/tmp", "/run/lock")
AGENTS, SKILLS = "/opt/agents", "/opt/skills"
# Per-conversation copies untouched this long are removed (the sandbox lives <= 12 h).
STALE_S = 2 * 3600
# Mounts this script made; every other mount ends up read-only (freeze).
OURS = set()


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


def sub(fd, name, make):
    if name in ("", ".", "..") or "/" in name:
        raise OSError(22, "bad name")
    try:
        got = os.open(name, DIR, dir_fd=fd)
    except FileNotFoundError:
        if not make:
            raise
        try:
            os.mkdir(name, 0o777, dir_fd=fd)
        except FileExistsError:
            pass
        got = os.open(name, DIR, dir_fd=fd)
    if not stat.S_ISDIR(os.fstat(got).st_mode):
        os.close(got)
        raise NotADirectoryError(20, "not a directory")
    return got


def mnt_id(fd):
    with open("/proc/self/fdinfo/" + str(fd), "rb") as f:
        for line in f:
            if line.startswith(b"mnt_id:"):
                return int(line.split()[1])
    die("no mnt_id for fd " + str(fd))


def attach(tree, target):
    sc(MOVE_MOUNT, tree, b"", target, b"", MOVE)
    OURS.add(mnt_id(tree))
    return tree


def bind(source, target):
    return attach(sc(OPEN_TREE, source, b"", CLONE), target)


def ro(tree, what, flags):
    # A bind remount sets the per-mount flags as a whole: the ones the source
    # mount already has are locked in a user namespace and must be kept.
    f = os.fstatvfs(tree).f_flag
    for st, ms in ((os.ST_NOSUID, NOSUID), (os.ST_NODEV, NODEV), (os.ST_NOEXEC, NOEXEC)):
        if f & st:
            flags |= ms
    fd_path = ("/proc/self/fd/" + str(tree)).encode()
    if libc.mount(None, fd_path, None, REMOUNT_BIND | flags, None) != 0:
        die(what + " could not be made read-only")


def bind_ro(source, target, what):
    ro(bind(source, target), what, SEALED)


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


def nas_view(root, conv, own):
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
    attach(tmpfs((b"size", b"1k")), nas)


def local(path):
    return os.open(path, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)


def take(tmp, name, digest):
    # The exec script, written to the outer /tmp: unlinked first (so it is gone
    # whatever happens next), then checked against the hash the caller sent.
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=tmp)
        try:
            os.unlink(name, dir_fd=tmp)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                die("exec script is not a regular file")
            chunks = []
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(fd)
    except OSError as e:
        die("exec script unreadable: " + str(e))
    data = b"".join(chunks)
    if hashlib.sha256(data).hexdigest() != digest:
        die("exec script does not match its sha256")
    return data


def prune(store, own):
    # A copy idle for STALE_S is renamed away, then removed only if nobody touched
    # it in between (an exec of that conversation touches its copy first).
    listing = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=store)
    try:
        names = os.listdir(listing)
    finally:
        os.close(listing)
    for name in names:
        try:
            if name == own or time.time() - os.lstat(name, dir_fd=store).st_mtime < STALE_S:
                continue
            gone = name
            if not name.startswith(".trash-"):
                gone = ".trash-" + name + "-" + os.urandom(4).hex()
                os.rename(name, gone, src_dir_fd=store, dst_dir_fd=store)
                if time.time() - os.lstat(gone, dir_fd=store).st_mtime < STALE_S:
                    os.rename(gone, name, src_dir_fd=store, dst_dir_fd=store)
                    continue
            shutil.rmtree(gone, ignore_errors=True, dir_fd=store)
        except OSError:
            continue


def mine(base, own):
    try:
        store = sub(base, ".ew-conv", True)
        prune(store, own)
        if not own:
            return None
        try:
            os.utime(own, dir_fd=store, follow_symlinks=False)
        except FileNotFoundError:
            pass
        return sub(store, own, True)
    except OSError:
        die("the local state of this conversation is not a plain directory")


def private(target, source):
    if source is None:
        return attach(tmpfs((b"size", b"256m"), (b"mode", b"0700")), target)
    return bind(source, target)


def only(target, key, fill):
    # An empty tmpfs over target; only target/<key> is put back, by fill.
    cover = tmpfs((b"size", b"1k"), (b"mode", b"0500"))
    slot = None
    if key and fill is not None:
        os.mkdir(key, 0o777, dir_fd=cover)
        slot = sub(cover, key, False)
    attach(cover, target)
    if slot is not None:
        fill(slot)


def local_view(tmp, key, own, name, data):
    # Every source is opened before anything is covered.
    kept = []
    for path in KEPT:
        try:
            base = tmp if path == "/tmp" else local(path)
        except FileNotFoundError:
            continue
        if os.fstatvfs(base).f_flag & os.ST_RDONLY:
            continue  # nobody can write it (local backend: --read-only rootfs)
        kept.append((path, base, mine(base, own)))
    try:
        shm = local("/dev/shm")
    except FileNotFoundError:
        shm = None
    agents, skills = local(AGENTS), local(SKILLS)
    pyuser = mine(agents, own) if key else None
    skill = None
    if key:
        try:
            skill = sub(skills, key, False)
        except FileNotFoundError:
            pass
        except OSError:
            say(SKILLS + "/" + key + " is not a plain directory; skills not mounted")
    tmp_tree = None
    for path, base, source in kept:
        tree = private(base, source)
        if path == "/tmp":
            tmp_tree = tree
    if shm is not None:
        attach(tmpfs((b"size", b"64m"), (b"mode", b"1777")), shm)
    only(agents, key, lambda slot: private(slot, pyuser))

    def skills_ro(slot):
        ro(bind(skill, slot), SKILLS + "/" + key, RDONLY | NOSUID | NODEV)

    only(skills, key, None if skill is None else skills_ro)
    if data is not None:
        if tmp_tree is None:
            die("/tmp is not writable; nowhere to put the exec script")
        box = tmpfs((b"size", str(len(data) + 65536).encode()), (b"mode", b"0700"))
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600, dir_fd=box)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        ro(attach(box, sub(tmp_tree, ".ew-exec", True)), "the exec script", SEALED)


def freeze():
    # Fail closed: every mount this namespace was copied with goes read-only, so a
    # writable path nobody listed is not shared between conversations. The /proc
    # subtree stays as it is: the nested unshare writes uid_map, and the runtime
    # masks under it (/dev/null binds, ro proc/tmpfs) hold no storage. The mounts
    # made above keep their flags; a mount hidden under one of them is skipped.
    bs = bytes([92])
    with open("/proc/self/mountinfo", "rb") as f:
        rows = [line.split(b" ") for line in f.read().splitlines()]
    for row in rows:
        point = row[4]
        for code, ch in ((b"040", b" "), (b"011", bytes([9])), (b"012", bytes([10])), (b"134", bs)):
            point = point.replace(bs + code, ch)
        if int(row[0]) in OURS or point == b"/proc" or point.startswith(b"/proc/"):
            continue
        try:
            fd = os.open(point, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            continue  # out of reach here, so out of reach of the payload too
        try:
            if mnt_id(fd) == int(row[0]):
                ro(fd, point.decode("utf-8", "replace"), RDONLY)
        finally:
            os.close(fd)


def main(root, conv, own, name, digest):
    tmp = local("/tmp")
    data = take(tmp, name, digest) if name else None
    nas_view(root, conv, own)
    local_view(tmp, root.rsplit("/", 1)[-1] if root else "", own, name, data)
    freeze()


try:
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
except OSError as e:
    die("mount failed: " + str(e))
' "$root" "$conv" "$own" "$code" "$sum" || { rc=$?; drop; exit "$rc"; }
cd /workspace
PATH="$ew_path"
HOME=/home/agent
MPLCONFIGDIR=/home/agent/.mplconfig
TMPDIR=/tmp
export HOME MPLCONFIGDIR TMPDIR
unset BASH_ENV ENV
exec /usr/bin/unshare -Ur -- "$@"
"""

#: ``/bin/sh -c <script> <$0> <$1> … <$5> <python argv…>``;``$0`` 只是报错时的名字。B-242 ——
#: ``unshare`` 与 ``sh`` 都写绝对路径:外层 shell 里同名的函数 / ``PATH`` 上的假命令都截不到;
#: ``-i`` 另起 IPC namespace,System V IPC / POSIX 消息队列也不跨 exec。
EXEC_VIEW_ARGV_PREFIX: tuple[str, ...] = (
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

#: B-242 —— exec 代码文件:``files.write`` 写在命名空间外的 ``/tmp``(任何 exec 的视图里
#: ``/tmp`` 都是本对话自己的,看不到这里);挂载阶段读出、删掉、核对 sha256,放进本次 exec
#: 独有的只读 tmpfs,载荷在这个目录里看到它。
EXEC_SCRIPT_DIR = "/tmp"  # noqa: S108 — 沙箱里命名空间外的 /tmp,不是本机
EXEC_SCRIPT_VIEW_DIR = "/tmp/.ew-exec"  # noqa: S108 — 同上,命名空间里的路径

#: B-242 —— envd 的 ``commands.run`` 固定走 ``/bin/bash -l -c``,登录 shell 会先读
#: ``$HOME/.bash_profile`` —— 那是上一次 exec 写得进的地方,而它在命名空间建好**之前**跑。
#: 外层 shell 的 ``HOME`` 换成 root 所有、uid 10000 写不进的 ``/``;``BASH_ENV`` / ``ENV``
#: 置空(envd 的 ``envs`` 只能覆盖、不能删)。真正的 ``HOME`` 由脚本在视图里设回。
EXEC_SHELL_ENV: Mapping[str, str] = MappingProxyType({"HOME": "/", "BASH_ENV": "", "ENV": ""})


def exec_view_argv(
    agent_key: str,
    python_argv: Sequence[str],
    *,
    conv_id: UUID | None = None,
    thread_id: UUID | None = None,
    script_name: str = "",
    script_sha256: str = "",
) -> list[str]:
    """完整 argv:前缀 + ``$1``(绑了 agent 是 NAS 上的真实目录,未绑是空串)+ ``$2``
    (对话 id,老视图 / 未绑为空串)+ ``$3``(本对话的线程 id,没有为空串)+ ``$4`` / ``$5``
    (B-242:``EXEC_SCRIPT_DIR`` 下代码文件的名字与 sha256,本地后端走 ``-c`` 时为空串)
    + python。

    对话视图与线程 id 只在绑了 agent 时成立;未绑时一律不发。
    """
    root = agent_nas_root(agent_key) if agent_key else ""
    conv = str(conv_id) if root and conv_id is not None else ""
    thread = str(thread_id) if root and thread_id is not None else ""
    return [*EXEC_VIEW_ARGV_PREFIX, root, conv, thread, script_name, script_sha256, *python_argv]


def build_exec_command(
    agent_key: str,
    script_name: str,
    script_sha256: str,
    *,
    conv_id: UUID | None = None,
    thread_id: UUID | None = None,
) -> str:
    """ACS 后端 ``commands.run``(``/bin/bash -l -c``)用的命令串。

    ``umask 077`` 在最前:命名空间里 ``mkdir -p "$root"`` 建出来的 agent 目录也要落
    ``0o700``(与 ``NasWorkspaceStore._DIR_MODE`` 同一个数字)。其余全部 ``shlex.join``,
    脚本名不进任何拼接。B-242 —— 代码文件要先由调用方写到 ``EXEC_SCRIPT_DIR/<script_name>``,
    载荷跑的是 ``EXEC_SCRIPT_VIEW_DIR`` 里那一份;外层 shell 要带 :data:`EXEC_SHELL_ENV`。
    """
    argv = exec_view_argv(
        agent_key,
        ["python", *SANDBOX_PYTHON_FLAGS, f"{EXEC_SCRIPT_VIEW_DIR}/{script_name}"],
        conv_id=conv_id,
        thread_id=thread_id,
        script_name=script_name,
        script_sha256=script_sha256,
    )
    return "umask 077 && " + shlex.join(argv)
