"""build_sandbox_runtime 的后端选择 —— 波 1 Task 6。"""

from __future__ import annotations

import pytest

from control_plane.runtime import build_sandbox_runtime
from control_plane.settings import Settings
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


def test_agent_sandbox_backend_not_wired_yet() -> None:
    """Task 7 之前 agent_sandbox 分支还没实现 —— 明确抛,不静默返 None。"""
    s = Settings(sandbox_backend="agent_sandbox")
    with pytest.raises(NotImplementedError, match="agent_sandbox"):
        build_sandbox_runtime(s)


def test_legacy_url_only_still_works() -> None:
    """老配置只设了 URL 没设 backend —— 视作 supervisor,不破坏现网。"""
    s = Settings(sandbox_backend=None, sandbox_supervisor_url="http://sup:8080")
    assert isinstance(build_sandbox_runtime(s), HTTPSupervisorRuntime)
