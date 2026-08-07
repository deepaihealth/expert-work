"""build_sandbox_runtime 的后端选择 —— 波 1 Task 6 起,Task 7 补 agent_sandbox 分支。"""

from __future__ import annotations

import pytest

from control_plane.runtime import build_sandbox_runtime, build_workspace_store
from control_plane.settings import Settings
from expert_work.persistence.sandbox_instance_store import InMemorySandboxInstanceStore
from orchestrator.tools.agent_sandbox import AgentSandboxClient
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore
from orchestrator.tools.sandbox import HTTPSupervisorRuntime
from orchestrator.tools.workspace_store import SupervisorWorkspaceStore


def test_none_when_nothing_configured() -> None:
    s = Settings(sandbox_backend=None, sandbox_supervisor_url=None)
    assert build_sandbox_runtime(s) is None


def test_supervisor_backend_builds_http_runtime() -> None:
    s = Settings(sandbox_backend="supervisor", sandbox_supervisor_url="http://sup:8080")
    runtime = build_sandbox_runtime(s)
    assert isinstance(runtime, HTTPSupervisorRuntime)
    assert runtime.base_url == "http://sup:8080"


def test_supervisor_backend_without_url_is_none() -> None:
    """后端选了 supervisor 但没给 URL —— 保持现有降级语义,不炸。"""
    s = Settings(sandbox_backend="supervisor", sandbox_supervisor_url=None)
    assert build_sandbox_runtime(s) is None


def test_agent_sandbox_backend_builds_client() -> None:
    """波 1 Task 7 —— agent_sandbox 分支接上真实构造,不再 NotImplementedError。"""
    s = Settings(
        sandbox_backend="agent_sandbox",
        sandbox_e2b_domain="expert-work-sbx-test.deepaihealth.com",
        sandbox_e2b_api_key="k",
        sandbox_e2b_template="expert-work-sandbox",
        # 全分支终审 I-2 —— 故意偏离 AgentSandboxClient.egress_token_ttl_s 的
        # dataclass 默认值(24h)。两边默认值恰好相同,漏传(或整行被删掉)会
        # 让 runtime 静默落回那个 dataclass 默认值,如果这里不特意选一个不同
        # 的数,下面的断言在两种情况下都会通过,测不出接线丢了。
        sandbox_egress_token_ttl_s=7200,
    )
    runtime = build_sandbox_runtime(s)
    assert isinstance(runtime, AgentSandboxClient)
    assert runtime.domain == "expert-work-sbx-test.deepaihealth.com"
    assert runtime.api_key == "k"
    assert runtime.template == "expert-work-sandbox"
    # 没注入 store → 工厂自己兜一个可用的 in-memory 实现,不留 None 陷阱。
    assert isinstance(runtime.store, InMemorySandboxInstanceStore)
    # egress 四项走 Settings 的值(host/port/secret 三项是 Settings 的默认值,
    # 与 credential-proxy / sandbox-supervisor 的 dev 默认一致,见 settings.py
    # 里的说明;ttl 上面特意设成非默认值,见构造 s 时的注释)。
    assert runtime.egress_proxy_host == s.sandbox_egress_proxy_host
    assert runtime.egress_proxy_port == s.sandbox_egress_proxy_port
    assert runtime.egress_token_secret == s.sandbox_egress_token_secret
    assert runtime.egress_token_ttl_s == s.sandbox_egress_token_ttl_s


def test_agent_sandbox_backend_injects_given_store() -> None:
    """app.py 有真 SQL session factory 时该注入 SqlSandboxInstanceStore,
    而不是工厂默认的 in-memory 兜底 —— 这里用一个哨兵替身验证"注入的就是
    传进来的那个对象",不测 SQL 行为本身(那是 packages/expert-work-persistence
    的真容器集成测试的职责)。
    """
    s = Settings(
        sandbox_backend="agent_sandbox",
        sandbox_e2b_domain="d",
        sandbox_e2b_api_key="k",
        sandbox_e2b_template="t",
    )
    sentinel = InMemorySandboxInstanceStore()
    runtime = build_sandbox_runtime(s, sandbox_instance_store=sentinel)
    assert isinstance(runtime, AgentSandboxClient)
    assert runtime.store is sentinel


def test_agent_sandbox_backend_without_full_config_raises_naming_the_missing_keys() -> None:
    """backend 选了 agent_sandbox 但三项配置没配全 —— 大声失败,并点名缺哪个。

    全分支终审:这里原本静默 ``return None``,表现出来是"声明了 exec_python
    的 agent 在构建期失败",一个域名少打的字母要一路回溯到这个工厂才查得
    出来。与 supervisor 分支"没 URL 就 None"不对称是有意的:那条是本来就
    合法的降级(压根没部署 supervisor),而显式把后端设成 agent_sandbox 却
    漏配凭据只可能是配错了。
    """
    s = Settings(sandbox_backend="agent_sandbox", sandbox_e2b_domain="d")

    with pytest.raises(RuntimeError) as excinfo:
        build_sandbox_runtime(s)

    message = str(excinfo.value)
    assert "EXPERT_WORK_SANDBOX_E2B_API_KEY" in message
    assert "EXPERT_WORK_SANDBOX_E2B_TEMPLATE" in message
    # 配上了的那项不该出现在"缺失"清单里,否则等于没点名。
    assert "EXPERT_WORK_SANDBOX_E2B_DOMAIN" not in message


def test_agent_sandbox_backend_empty_string_config_also_raises() -> None:
    """空串与未设同等对待 —— k8s ConfigMap 关一项配置的写法是 ``KEY: ""``,
    不是把整行删掉(见 overlays 里 supervisor URL 的先例)。"""
    s = Settings(
        sandbox_backend="agent_sandbox",
        sandbox_e2b_domain="d",
        sandbox_e2b_api_key="k",
        sandbox_e2b_template="",
    )

    with pytest.raises(RuntimeError, match="EXPERT_WORK_SANDBOX_E2B_TEMPLATE"):
        build_sandbox_runtime(s)


def test_legacy_url_only_still_works() -> None:
    """老配置只设了 URL 没设 backend —— 视作 supervisor,不破坏现网。"""
    s = Settings(sandbox_backend=None, sandbox_supervisor_url="http://sup:8080")
    assert isinstance(build_sandbox_runtime(s), HTTPSupervisorRuntime)


def test_workspace_store_builds_from_a_real_url() -> None:
    s = Settings(sandbox_supervisor_url="http://sup:8080")
    store = build_workspace_store(s)
    assert isinstance(store, SupervisorWorkspaceStore)
    assert store.base_url == "http://sup:8080"


@pytest.mark.parametrize("url", [None, ""])
def test_workspace_store_is_none_without_a_usable_url(url: str | None) -> None:
    """全分支终审:判据要与兄弟工厂 ``build_sandbox_runtime`` 一致(真值),
    不是 ``is None``。

    ``EXPERT_WORK_SANDBOX_SUPERVISOR_URL: ""`` 是 k8s overlay 关掉 supervisor
    的实际写法(测试/生产两个 configmap-patch 都这么写)。``is None`` 下那会
    造出一个 ``base_url=""`` 的**活** store,每次请求真拨一个空 base_url 的
    HTTP:``purge_user`` 把工作区那步报成失败而不是跳过,工作区端点回传输
    错误而不是空结果。
    """
    s = Settings(sandbox_supervisor_url=url)
    assert build_workspace_store(s) is None


def test_workspace_store_builds_nas_store_when_root_is_set() -> None:
    """波 2 Task 3 —— ``workspace_nas_root`` 真值 → ``NasWorkspaceStore``。"""
    s = Settings(workspace_nas_root="/mnt/workspaces")
    store = build_workspace_store(s)
    assert isinstance(store, NasWorkspaceStore)
    assert store.root == "/mnt/workspaces"


def test_workspace_store_prefers_nas_over_supervisor_when_both_are_set() -> None:
    """两者都配了 —— NAS 直读路径是波 2 的目标终态,优先于 supervisor 代理。"""
    s = Settings(workspace_nas_root="/mnt/workspaces", sandbox_supervisor_url="http://sup:8080")
    store = build_workspace_store(s)
    assert isinstance(store, NasWorkspaceStore)


@pytest.mark.parametrize("root", [None, ""])
def test_workspace_store_falls_back_to_supervisor_without_a_usable_nas_root(
    root: str | None,
) -> None:
    s = Settings(workspace_nas_root=root, sandbox_supervisor_url="http://sup:8080")
    store = build_workspace_store(s)
    assert isinstance(store, SupervisorWorkspaceStore)
