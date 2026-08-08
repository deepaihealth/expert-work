"""共享 gid 的三方漂移闸 —— 常量 / 沙箱镜像 / k8s manifest 必须同值。

gid 10000 的**事实源**是沙箱镜像里 ``agent`` 用户的主组(``useradd -u 10000``
默认建同名同 id 主组),编排进程运行时读不到它,control-plane 的 Pod
``securityContext`` 里又必须写一份字面量 —— 于是同一个数字有三份副本。手法照
``test_image_env_matches_dockerfile``:刻意不打 ``@pytest.mark.integration``、
也刻意不 skip(漂移闸跳过时等于不存在),文件在仓库 checkout 里必然存在。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from expert_work.persistence import WORKSPACE_SHARED_GID

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SANDBOX_DOCKERFILE = _REPO_ROOT / "infra" / "sandbox-image" / "Dockerfile"
_CP_DEPLOYMENT = _REPO_ROOT / "infra" / "k8s" / "base" / "control-plane" / "deployment.yaml"


def test_shared_gid_matches_the_sandbox_image_agent_uid() -> None:
    """镜像里 ``agent`` 的 uid(= 其主组 gid)就是我们共享的那个 gid。

    改了 Dockerfile 的 ``useradd -u`` 而没改常量 → control-plane 会 chgrp 到
    一个沙箱里不存在的组,跨 uid 读写当场全断。
    """
    text = _SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"useradd\s+-u\s+(\d+)\s+.*\bagent\b", text)
    assert match is not None, "沙箱 Dockerfile 里找不到 `useradd -u <uid> ... agent` 行"
    assert int(match.group(1)) == WORKSPACE_SHARED_GID


def test_control_plane_deployment_declares_the_shared_gid() -> None:
    """control-plane Pod 必须把共享 gid 列进 ``supplementalGroups``。

    漏了这一项 = 进程根本不在那个组里,``0o640`` 的文件一律读不了 —— 而且
    症状与本次修复之前一模一样(下载 404),极难归因。
    """
    doc = yaml.safe_load(_CP_DEPLOYMENT.read_text(encoding="utf-8"))
    groups = doc["spec"]["template"]["spec"]["securityContext"]["supplementalGroups"]
    assert groups == [WORKSPACE_SHARED_GID]
