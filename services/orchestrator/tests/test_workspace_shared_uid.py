"""两份 Dockerfile 的 uid 漂移闸 —— 方向变更(共享 gid → 统一 uid)后的唯一防线。

control-plane(``services/control-plane/Dockerfile``)与沙箱镜像
(``infra/sandbox-image/Dockerfile``)现在故意用**同一个** uid(10000)跑,
两个进程在文件系统看来是同一个人,W2-BUG-1(agent 写的 ``MEMORY.md``
control-plane 读不动)从根上不存在——不需要共享组、不需要 setgid、不需要
任何 ``chown``。两个数字一旦分叉,症状就是 BUG-1 原样复发:agent 写的文件
control-plane 读不动,前端列得出、下载 404,而且不会有任何一条日志说"是
uid 不一样"。两份 Dockerfile 分属不同目录、不同发布线(沙箱镜像走
sandbox-image.yml,control-plane 走 release.sh),漂移是迟早的事。

刻意不打 ``@pytest.mark.integration``、也刻意不 skip:漂移闸在跳过时就等于
不存在,而这两个文件在仓库 checkout 里必然存在。

不把 uid 写死在断言里(照 ``test_image_env_matches_dockerfile`` 的既有手
法)——双向从两份 Dockerfile 各自提取一次,互相比对。写死数字的话只钉得住
"改了一边"这一种漂移;真正要守住的不变式是"两个数字相等",不是"两个数字
都等于 10000"。

复审 M-4 —— 上一版这里只是**提到**了另一个漂移形状(两边被同时改成同一个
新数字,比如都改成 10005,不会破坏两侧的读写能力,但会撞上
credential-proxy(10001)/supervisor(10003)已经占用的 uid 空间)却没有真的
守住它,读起来像是"已经处理"。``test_shared_uid_does_not_collide_with_
another_service`` 补上这半句缺的断言,不只是改文案绕过去。
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SANDBOX_DOCKERFILE = _REPO_ROOT / "infra" / "sandbox-image" / "Dockerfile"
_CP_DOCKERFILE = _REPO_ROOT / "services" / "control-plane" / "Dockerfile"

#: uid 已经分给别的服务,不能被这两份 Dockerfile 共享的新 uid 撞上——
#: credential-proxy 是 10001,sandbox-supervisor 是 10003(两者都不在本仓的
#: Dockerfile 漂移闸覆盖范围内,这里手写字面量;沙箱 10000/control-plane
#: 10000 是本闸真正比对的两个值,不在这个集合里)。
_RESERVED_FOR_OTHER_SERVICES = frozenset({10001, 10003})


def _sandbox_agent_uid() -> int:
    text = _SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"useradd\s+-u\s+(\d+)\s+.*\bagent\b", text)
    assert match is not None, "沙箱 Dockerfile 里找不到 `useradd -u <uid> ... agent` 行"
    return int(match.group(1))


def _control_plane_uid() -> int:
    text = _CP_DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"useradd\s+--uid\s+(\d+)\s+.*\bexpert_work\b", text)
    assert match is not None, (
        "control-plane Dockerfile 里找不到 `useradd --uid <uid> ... expert_work` 行"
    )
    return int(match.group(1))


def test_control_plane_and_sandbox_images_share_one_uid() -> None:
    """两份 Dockerfile 的 uid 必须相同 —— 这是 W2-BUG-1 的唯一防线。

    两个数字一旦分叉,症状就是 BUG-1 原样复发:agent 写的文件 control-plane
    读不动,前端列得出、下载 404,而且不会有任何一条日志说"是 uid 不一
    样"。两份 Dockerfile 分属不同目录、不同发布线(沙箱镜像走
    sandbox-image.yml,control-plane 走 release.sh),漂移是迟早的事。

    刻意不打 ``@pytest.mark.integration``、也刻意不 skip:漂移闸在跳过时就
    等于不存在,而这两个文件在仓库 checkout 里必然存在。
    """
    assert _control_plane_uid() == _sandbox_agent_uid()


def test_shared_uid_does_not_collide_with_another_service() -> None:
    """两份 Dockerfile 的 uid 相等只是必要条件,不是充分条件——两边被同时
    改成同一个新数字(比如都改成 10001)照样不会破坏 control-plane/沙箱两
    侧互相读写的能力,但会与 credential-proxy(10001)或
    sandbox-supervisor(10003)已经占用的 uid 撞车,产生一个本闸的姐妹用例
    抓不到的新故障。"""
    shared_uid = _control_plane_uid()
    assert shared_uid == _sandbox_agent_uid()
    assert shared_uid not in _RESERVED_FOR_OTHER_SERVICES, (
        f"uid {shared_uid} 已经分给了别的服务(credential-proxy=10001 / sandbox-supervisor=10003)"
    )
