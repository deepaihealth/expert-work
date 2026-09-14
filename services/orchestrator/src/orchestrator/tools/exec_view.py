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
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence

from orchestrator.tools.sandbox_image_contract import SANDBOX_PYTHON_FLAGS
from orchestrator.tools.workspace_paths import agent_nas_root

#: 逐条见 spec §4.3。``remount,bind,ro,nosuid,nodev,noexec``:userns 里 remount 不能
#: **去掉**源挂载上锁住的 flag,但可以**加**,三个都写上永远合法。``size=1k`` 让盖住
#: 用户根的 tmpfs 没法当可写盘。**不挂 uploads**:它在 ``agents/<key>/uploads``。
EXEC_VIEW_SCRIPT = """\
set -eu
root="$1"; shift
if [ -n "$root" ]; then
  mkdir -p "$root"
  mount --bind "$root" /workspace
  if [ -d /mnt/workspace/shared ]; then
    if [ -L /workspace/shared ] || { [ -e /workspace/shared ] && [ ! -d /workspace/shared ]; }; then
      echo "ew-exec-view: /workspace/shared is not a directory; shared/ not mounted" >&2
    else
      mkdir -p /workspace/shared
      mount --bind /mnt/workspace/shared /workspace/shared
      mount -o remount,bind,ro,nosuid,nodev,noexec /workspace/shared
    fi
  fi
  mount -t tmpfs -o size=1k none /mnt/workspace
else
  mount --bind /mnt/workspace /workspace
fi
cd /workspace
exec "$@"
"""

#: ``sh -c <script> <$0> <$1> <python argv…>``;``$0`` 只是报错时的名字。
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


def exec_view_argv(agent_key: str, python_argv: Sequence[str]) -> list[str]:
    """完整 argv:前缀 + ``$1``(绑了 agent 是 NAS 上的真实目录,未绑是空串)+ python。"""
    root = agent_nas_root(agent_key) if agent_key else ""
    return [*EXEC_VIEW_ARGV_PREFIX, root, *python_argv]


def build_exec_command(agent_key: str, script_path: str) -> str:
    """ACS 后端 ``commands.run``(``/bin/bash -l -c``)用的命令串。

    ``umask 077`` 在最前:命名空间里 ``mkdir -p "$root"`` 建出来的 agent 目录也要落
    ``0o700``(与 ``NasWorkspaceStore._DIR_MODE`` 同一个数字)。其余全部 ``shlex.join``,
    脚本路径不进任何拼接。
    """
    argv = exec_view_argv(agent_key, ["python", *SANDBOX_PYTHON_FLAGS, script_path])
    return "umask 077 && " + shlex.join(argv)
