"""build_sandbox_runtime 的后端选择 —— 波 1 Task 6 起,Task 7 补 agent_sandbox 分支。"""

from __future__ import annotations

from control_plane.runtime import build_sandbox_runtime
from control_plane.settings import Settings
from expert_work.persistence.sandbox_instance_store import InMemorySandboxInstanceStore
from orchestrator.tools.agent_sandbox import AgentSandboxClient
from orchestrator.tools.sandbox import HTTPSupervisorRuntime


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
    )
    runtime = build_sandbox_runtime(s)
    assert isinstance(runtime, AgentSandboxClient)
    assert runtime.domain == "expert-work-sbx-test.deepaihealth.com"
    assert runtime.api_key == "k"
    assert runtime.template == "expert-work-sandbox"
    # 没注入 store → 工厂自己兜一个可用的 in-memory 实现,不留 None 陷阱。
    assert isinstance(runtime.store, InMemorySandboxInstanceStore)
    # egress 三项走 Settings 的默认值(与 credential-proxy / sandbox-supervisor
    # 的 dev 默认一致,见 settings.py 里的说明)。
    assert runtime.egress_proxy_host == s.sandbox_egress_proxy_host
    assert runtime.egress_proxy_port == s.sandbox_egress_proxy_port
    assert runtime.egress_token_secret == s.sandbox_egress_token_secret


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


def test_agent_sandbox_backend_without_full_config_is_none() -> None:
    """backend 选了 agent_sandbox 但三项配置没配全 —— 与 supervisor 无 URL
    同等的降级语义(不炸,返 None),声明了沙箱工具的 agent 在构建期报错。
    """
    s = Settings(sandbox_backend="agent_sandbox", sandbox_e2b_domain="d")
    assert build_sandbox_runtime(s) is None


def test_legacy_url_only_still_works() -> None:
    """老配置只设了 URL 没设 backend —— 视作 supervisor,不破坏现网。"""
    s = Settings(sandbox_backend=None, sandbox_supervisor_url="http://sup:8080")
    assert isinstance(build_sandbox_runtime(s), HTTPSupervisorRuntime)
