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

失败即失败:``set -e`` 让任一条 mount 失败都非零退出,exec 报错,**不回落**到用户根。

B-236 —— 按对话分目录。``$2`` 是对话 id(空 = 老视图,发布前已开的对话),``$3`` 是
过渡期映射目录的个数 ``n``,其后 ``n`` 个目录名,再往后才是 python argv。对话视图:

* ``/agent`` = agent 根(该 agent 所有对话共用);``/workspace`` = 本对话自己的
  ``threads/<conv>/work``。
* ``/agent/threads`` 被一块空 tmpfs 盖住,只把**本对话**那一格 bind 回来(状态投影写
  ``threads/<conv>/PLAN.md`` 要用),别的对话的目录在命名空间里不存在。
* 平台目录(``uploads`` / ``inputs`` / ``.tool_results``)仍在 agent 根,读写 bind 进
  ``/workspace``,平台发给模型的相对路径照旧可用。
* 过渡期目录:agent 根下已有的同名目录,本对话里没有(或只有一个空的挂载桩)时
  读写 bind 进 ``/workspace``。只映射目录,根下的单个文件从不映射。宿主侧的判据
  (:func:`orchestrator.tools.workspace_compat.discover_compat_dirs`)是同一个。

镜像里没有 ``/agent`` 时以 97 退出(没钉上新镜像);参数不合法以 98 退出。
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from uuid import UUID

from orchestrator.tools.sandbox_image_contract import SANDBOX_PYTHON_FLAGS
from orchestrator.tools.workspace_paths import agent_nas_root, require_compat_dir_name

#: 逐条见 spec §4.3。``remount,bind,ro,nosuid,nodev,noexec``:userns 里 remount 不能
#: **去掉**源挂载上锁住的 flag,但可以**加**,三个都写上永远合法。``size=1k`` 让盖住
#: 用户根的 tmpfs 没法当可写盘。**不挂用户根的 uploads**:它在 ``agents/<key>/uploads``
#: (对话视图里 bind 进来的是 agent 根的那一份)。
EXEC_VIEW_SCRIPT = """\
set -eu
root="$1"; conv="$2"; n="$3"; shift 3
if [ ! -d /agent ]; then
  echo "ew-exec-view: image lacks /agent" >&2
  exit 97
fi
case "$n" in
  ''|*[!0-9]*) echo "ew-exec-view: bad compat count" >&2; exit 98 ;;
esac
case "$conv" in
  */*|.|..) echo "ew-exec-view: bad conversation id" >&2; exit 98 ;;
esac
if [ -n "$root" ]; then
  mkdir -p "$root"
  mount --bind "$root" /agent
  if [ -n "$conv" ]; then
    mkdir -p "$root/threads/$conv/work"
    mount --bind "$root/threads/$conv/work" /workspace
    mount -t tmpfs -o size=1k,mode=0500 none /agent/threads
    mkdir -p "/agent/threads/$conv"
    mount --bind "$root/threads/$conv" "/agent/threads/$conv"
    for d in uploads inputs .tool_results; do
      mkdir -p "$root/$d"
      if [ -L "/workspace/$d" ] || { [ -e "/workspace/$d" ] && [ ! -d "/workspace/$d" ]; }; then
        echo "ew-exec-view: /workspace/$d is not a directory; $d/ not mounted" >&2
      else
        mkdir -p "/workspace/$d"
        mount --bind "$root/$d" "/workspace/$d"
      fi
    done
  else
    mount --bind "$root" /workspace
  fi
  if [ -d /mnt/workspace/shared ]; then
    if [ -L /workspace/shared ] || { [ -e /workspace/shared ] && [ ! -d /workspace/shared ]; }; then
      echo "ew-exec-view: /workspace/shared is not a directory; shared/ not mounted" >&2
    else
      mkdir -p /workspace/shared
      mount --bind /mnt/workspace/shared /workspace/shared
      mount -o remount,bind,ro,nosuid,nodev,noexec /workspace/shared
    fi
  fi
  while [ "$n" -gt 0 ]; do
    d="$1"; shift; n=$((n - 1))
    case "$d" in
      ''|.|..|*/*|threads|uploads|inputs|.tool_results|shared|skills|agents)
        echo "ew-exec-view: bad compat dir" >&2; exit 98 ;;
    esac
    if [ -z "$conv" ] || [ -L "$root/$d" ] || [ ! -d "$root/$d" ] || [ -L "/workspace/$d" ]; then
      continue
    fi
    if [ ! -e "/workspace/$d" ]; then
      mkdir "/workspace/$d"
    elif [ ! -d "/workspace/$d" ] || [ -n "$(ls -A "/workspace/$d")" ]; then
      continue
    fi
    mount --bind "$root/$d" "/workspace/$d"
  done
  mount -t tmpfs -o size=1k none /mnt/workspace
else
  mount --bind /mnt/workspace /agent
  mount --bind /mnt/workspace /workspace
  shift "$n"
fi
cd /workspace
exec "$@"
"""

#: ``sh -c <script> <$0> <$1> <$2> <$3> <compat…> <python argv…>``;``$0`` 只是报错时的名字。
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
    compat_dirs: Sequence[str] = (),
) -> list[str]:
    """完整 argv:前缀 + ``$1``(绑了 agent 是 NAS 上的真实目录,未绑是空串)+ ``$2``
    (对话 id,老视图 / 未绑为空串)+ ``$3``(过渡期目录个数)+ 目录名 + python。

    对话视图只在绑了 agent 时成立;未绑时对话 id 与过渡期目录一律不发。目录名先在这里
    按与脚本相同的规则校验一遍(脚本自己还会再查,不合法就 98 退出)。
    """
    root = agent_nas_root(agent_key) if agent_key else ""
    conv = str(conv_id) if root and conv_id is not None else ""
    compat = [require_compat_dir_name(d) for d in compat_dirs] if conv else []
    return [*EXEC_VIEW_ARGV_PREFIX, root, conv, str(len(compat)), *compat, *python_argv]


def build_exec_command(
    agent_key: str,
    script_path: str,
    *,
    conv_id: UUID | None = None,
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
        compat_dirs=compat_dirs,
    )
    return "umask 077 && " + shlex.join(argv)
