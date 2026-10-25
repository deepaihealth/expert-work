"""``smoke.sh`` 的沙箱网络隔离判据(B-217)。

ACS 上沙箱 Pod 默认和平台服务进同一个集群安全组,沙箱里的代码因此能直连公网和全部
内网服务(数据库、Redis、监控……),智能体的「出网关」只是没给代理变量。隔离靠沙箱
池模板指定的独立安全组;它丢了(有人直接 apply 了不带注解的池定义、或注解指回集群
安全组)不会有任何报错,沙箱照常开机。判据只看 Pod 上 ACS 实际分到的安全组:池里在跑的
每一台(待命的和用户正在用的都算 —— 改模板不会把已经在用的沙箱挪组)都必须正好是模板写的
那组,且模板那组不许和 control-plane 有交集。

第二个函数管安全组规则里写死的托管地址(集群域名解析、k8s 接口只放行到 /32,否则沙箱
之间能经这两个端口互通,10-25 实测):阿里云换了地址,沙箱就解析不了域名 / 开不了机。
池定义上记着放行了哪些地址,和集群里的实际端点比,不一致就提醒。

函数是纯的(输入全是 kubectl 的输出),从真文件里抽出来直接驱动。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_SMOKE_SH = Path(__file__).resolve().parent / "smoke.sh"
_CLUSTER_SG = "sg-cluster"
_SANDBOX_SG = "sg-sandbox"


def _verdict(*, template: str, pool: list[str], platform: list[str]) -> str:
    text = _SMOKE_SH.read_text(encoding="utf-8")
    match = re.search(r"^sandbox_network_verdict\(\) \{$.*?^\}$", text, re.MULTILINE | re.DOTALL)
    assert match is not None, "smoke.sh 里找不到函数 sandbox_network_verdict"
    script = f'set -euo pipefail\n{match.group(0)}\nsandbox_network_verdict "$1" "$2" "$3"\n'
    done = subprocess.run(  # noqa: S603 — fixed argv, no shell, test harness
        ["bash", "-c", script, "verdict", template, "\n".join(pool), "\n".join(platform)],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def test_pool_on_its_own_security_group_is_ok() -> None:
    out = _verdict(template=_SANDBOX_SG, pool=[_SANDBOX_SG, _SANDBOX_SG], platform=[_CLUSTER_SG])
    assert out.startswith(f"OK   sandbox pool is on its own security group ({_SANDBOX_SG})")


def test_template_without_a_security_group_warns() -> None:
    """池模板没写注解:沙箱进集群安全组,和平台服务互通。"""
    out = _verdict(template="", pool=[_CLUSTER_SG], platform=[_CLUSTER_SG])
    assert out.startswith("WARN") and "B-217" in out and "security-group-ids" in out


def test_a_pool_pod_sharing_the_platform_group_warns() -> None:
    """注解在,但 ACS 实际分给某台沙箱的是平台那组(注解写错 / 写成集群安全组):
    判据看 Pod 上真分到的值,不看模板上写的。"""
    out = _verdict(template=_CLUSTER_SG, pool=[_CLUSTER_SG], platform=[_CLUSTER_SG])
    assert out.startswith("WARN") and _CLUSTER_SG in out


def test_one_bad_pod_among_good_ones_warns() -> None:
    """改注解后旧 Pod 还没换完:有一台在平台那组就不算隔离好了。"""
    out = _verdict(template=_SANDBOX_SG, pool=[_SANDBOX_SG, _CLUSTER_SG], platform=[_CLUSTER_SG])
    assert out.startswith("WARN") and _CLUSTER_SG in out


def test_no_pool_pod_to_look_at_warns() -> None:
    """没有待命沙箱就看不到实际分组:说看不到,不报 OK。"""
    out = _verdict(template=_SANDBOX_SG, pool=[], platform=[_CLUSTER_SG])
    assert out.startswith("WARN") and "看不到" in out


def test_unknown_platform_group_warns() -> None:
    """读不到 control-plane 的安全组就没法比:同样说看不到。"""
    out = _verdict(template=_SANDBOX_SG, pool=[_SANDBOX_SG], platform=[])
    assert out.startswith("WARN") and "看不到" in out


def test_a_claimed_pod_left_on_the_platform_group_warns() -> None:
    """改模板后,用户正在用的沙箱不会换组:它也得算进来(审查 finding 4)。"""
    out = _verdict(template=_SANDBOX_SG, pool=[_SANDBOX_SG, _CLUSTER_SG], platform=[_CLUSTER_SG])
    assert out.startswith("WARN") and _CLUSTER_SG in out


def test_a_pod_on_some_other_group_warns() -> None:
    """不在平台那组也不够:必须正好是模板写的那组(比如被指到了出网代理的组)。"""
    out = _verdict(template=_SANDBOX_SG, pool=["sg-credproxy"], platform=[_CLUSTER_SG])
    assert out.startswith("WARN") and "sg-credproxy" in out


def test_comma_separated_groups_are_compared_as_sets() -> None:
    """一个 Pod 可以挂多个组(逗号分隔):混进平台那组要能看出来。"""
    out = _verdict(
        template=f"{_SANDBOX_SG},{_CLUSTER_SG}",
        pool=[f"{_SANDBOX_SG},{_CLUSTER_SG}"],
        platform=[_CLUSTER_SG],
    )
    assert out.startswith("WARN") and _CLUSTER_SG in out


def _peers(*, template: str, dns_peers: str, dns: list[str], api_peers: str, api: list[str]) -> str:
    text = _SMOKE_SH.read_text(encoding="utf-8")
    match = re.search(r"^sandbox_peer_drift_verdict\(\) \{$.*?^\}$", text, re.MULTILINE | re.DOTALL)
    assert match is not None, "smoke.sh 里找不到函数 sandbox_peer_drift_verdict"
    call = 'sandbox_peer_drift_verdict "$1" "$2" "$3" "$4" "$5"'
    script = f"set -euo pipefail\n{match.group(0)}\n{call}\n"
    args = [template, dns_peers, "\n".join(dns), api_peers, "\n".join(api)]
    done = subprocess.run(  # noqa: S603 — fixed argv, no shell, test harness
        ["bash", "-c", script, "verdict", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def test_peers_matching_the_cluster_is_ok() -> None:
    out = _peers(
        template=_SANDBOX_SG,
        dns_peers="10.0.0.3,10.0.0.4",
        dns=["10.0.0.4", "10.0.0.3"],
        api_peers="10.0.0.9,10.0.0.10",
        api=["10.0.0.10"],
    )
    assert out.startswith("OK")


def test_a_new_dns_endpoint_warns() -> None:
    """托管的域名解析换了地址:沙箱的安全组还只放行旧的,新沙箱解析不了域名。"""
    out = _peers(
        template=_SANDBOX_SG,
        dns_peers="10.0.0.3,10.0.0.4",
        dns=["10.0.0.3", "10.0.0.5"],
        api_peers="10.0.0.9",
        api=["10.0.0.9"],
    )
    assert out.startswith("WARN") and "10.0.0.5" in out


def test_a_new_api_server_endpoint_warns() -> None:
    out = _peers(
        template=_SANDBOX_SG,
        dns_peers="10.0.0.3",
        dns=["10.0.0.3"],
        api_peers="10.0.0.9",
        api=["10.0.0.11"],
    )
    assert out.startswith("WARN") and "10.0.0.11" in out


def test_an_isolated_pool_without_recorded_peers_warns() -> None:
    out = _peers(
        template=_SANDBOX_SG, dns_peers="", dns=["10.0.0.3"], api_peers="", api=["10.0.0.9"]
    )
    assert out.startswith("WARN")


def test_a_pool_without_its_own_group_skips_the_peer_check() -> None:
    """还没隔离的环境(生产在建好安全组之前)不比:上一条检查已经在 WARN 了。"""
    out = _peers(template="", dns_peers="", dns=["10.0.0.3"], api_peers="", api=["10.0.0.9"])
    assert out == ""
