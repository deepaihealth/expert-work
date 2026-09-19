"""B-81 —— 两个 overlay 都真的配了沙箱 pip 索引源。

代码侧默认是**关**的(不配就直连 pypi.org,与本条之前逐字节一致),所以「能配」
和「配了」是两件事。本仓库反复栽在第二件上:`persistentContents` 配好了但回收
路径从不 pause、沙箱镜像钉子 34 天没人动 —— 都是「配置到位、没人接」。这条把
「配了」钉死。

而且**测试和生产必须一起有**:两个集群都在阿里云杭州,只配一边等于生产上线那天
才发现 pip 还是 16 KiB/s。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_OVERLAYS = Path(__file__).resolve().parents[2] / "infra" / "k8s" / "overlays"

#: 注入进沙箱的三个变量分别由哪个平台变量供给(与
#: ``orchestrator.tools.sandbox._PIP_ENV_SOURCES`` 同源;改一边这里会红)。
_INDEX = "EXPERT_WORK_SANDBOX_PIP_INDEX_URL"
_EXTRA = "EXPERT_WORK_SANDBOX_PIP_EXTRA_INDEX_URL"
_TRUSTED = "EXPERT_WORK_SANDBOX_PIP_TRUSTED_HOST"


def _configmap(env: str) -> dict[str, str]:
    doc = yaml.safe_load((_OVERLAYS / env / "configmap-patch.yaml").read_text(encoding="utf-8"))
    return dict(doc["data"])


@pytest.mark.parametrize("env", ["test", "prod"])
def test_pip_index_is_configured(env: str) -> None:
    data = _configmap(env)
    assert data.get(_INDEX), f"{env} 没配 pip 索引 —— 沙箱里 pip 会直连 pypi.org,实测 16 KiB/s"


@pytest.mark.parametrize("env", ["test", "prod"])
def test_insecure_index_hosts_are_all_trusted(env: str) -> None:
    """走 http 的索引必须出现在 ``PIP_TRUSTED_HOST`` 里,否则 pip 直接拒绝。

    这不是「加个开关」——不给 trusted-host,配了内网镜像的效果是**一个包都装不了**,
    比不配更糟。
    """
    data = _configmap(env)
    trusted = set(data.get(_TRUSTED, "").split())
    for key in (_INDEX, _EXTRA):
        url = data.get(key, "")
        if url.startswith("http://"):
            host = url.split("://", 1)[1].split("/", 1)[0]
            assert host in trusted, f"{env}: {key} 走 http 但 {host} 不在 PIP_TRUSTED_HOST 里"


@pytest.mark.parametrize("env", ["test", "prod"])
def test_trusted_hosts_are_only_the_hosts_we_configured(env: str) -> None:
    """``PIP_TRUSTED_HOST`` 只放我们自己配的那几个索引主机。

    每一个 trusted host 都是一处放弃了 TLS 校验的地方,名单必须可解释:多出来的
    条目没有对应索引,就是没人记得为什么在那。
    """
    data = _configmap(env)
    configured = {
        url.split("://", 1)[1].split("/", 1)[0]
        for url in (data.get(_INDEX, ""), data.get(_EXTRA, ""))
        if "://" in url
    }
    extra = set(data.get(_TRUSTED, "").split()) - configured
    assert not extra, f"{env}: PIP_TRUSTED_HOST 里有对不上任何索引的主机 {sorted(extra)}"
