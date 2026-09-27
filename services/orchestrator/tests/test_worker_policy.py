"""B-122 —— worker 工具边界的规则来源 + http 方法收窄。"""

from __future__ import annotations

from uuid import UUID

import pytest

from orchestrator.tools.http import HTTPTool
from orchestrator.tools.skill_authoring import SKILL_AUTHORING_BUILTINS
from orchestrator.tools.worker_policy import (
    READ_ONLY_HTTP_METHODS,
    WORKER_DENIED_BUILTINS,
    WORKER_POLICY_ENV,
    worker_policy_enabled,
)


async def _allow_all(_tenant: UUID | None) -> list[str]:
    return []


def test_denied_builtins_cover_delivery_and_self_modification() -> None:
    assert {"save_artifact", "ask_for_approval", "manage_task"} <= WORKER_DENIED_BUILTINS
    # B 类直接复用技能创作的集合,不另抄一份名字
    assert SKILL_AUTHORING_BUILTINS <= WORKER_DENIED_BUILTINS
    # 读类与工作区工具不在禁用之列
    assert not {"read_file", "write_file", "exec_python", "list_artifacts"} & WORKER_DENIED_BUILTINS


@pytest.mark.parametrize("raw", ["0", "false", "OFF", " no "])
def test_valve_off_values(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(WORKER_POLICY_ENV, raw)
    assert worker_policy_enabled() is False


@pytest.mark.parametrize("raw", [None, "", "1", "true", "on"])
def test_valve_defaults_on(monkeypatch: pytest.MonkeyPatch, raw: str | None) -> None:
    if raw is None:
        monkeypatch.delenv(WORKER_POLICY_ENV, raising=False)
    else:
        monkeypatch.setenv(WORKER_POLICY_ENV, raw)
    assert worker_policy_enabled() is True


def test_http_default_exposes_every_method() -> None:
    tool = HTTPTool(allowlist_provider=_allow_all)
    assert "POST" in tool.spec.parameters["properties"]["method"]["enum"]
    assert tool._require_method({"method": "post"}) == "POST"


def test_http_read_only_narrows_schema_and_rejects_writes() -> None:
    tool = HTTPTool(allowlist_provider=_allow_all, allowed_methods=READ_ONLY_HTTP_METHODS)
    assert tool.spec.parameters["properties"]["method"]["enum"] == ["GET", "HEAD", "OPTIONS"]
    assert tool._require_method({"method": "get"}) == "GET"
    for method in ("post", "PUT", "patch", "DELETE"):
        with pytest.raises(ValueError, match="worker sub-agents"):
            tool._require_method({"method": method})
