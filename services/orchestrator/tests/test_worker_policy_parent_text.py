"""B-122 —— 父侧文案与 worker 实际边界一致;阀关逐字节回到现状。"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from orchestrator.agent_factory import _WORKER_DELEGATION_BLOCK, _worker_delegation_block
from orchestrator.tools.spawn_worker import SpawnWorkerTool
from orchestrator.tools.worker_policy import WORKER_POLICY_ENV


def _desc() -> str:
    return SpawnWorkerTool(builder=Mock(), child_depth=1).spec.description


def test_parent_block_tells_it_to_verify_register_and_take_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(WORKER_POLICY_ENV, raising=False)
    block = _worker_delegation_block()
    assert block.startswith(_WORKER_DELEGATION_BLOCK)
    assert "not deliverables until you register them yourself" in block
    assert "do that part yourself" in block


def test_parent_block_valve_off_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WORKER_POLICY_ENV, "off")
    assert _worker_delegation_block() == _WORKER_DELEGATION_BLOCK


def test_spawn_worker_description_matches_worker_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WORKER_POLICY_ENV, raising=False)
    desc = _desc()
    assert "same tool set as you" not in desc
    assert "read-only MCP tools" in desc
    monkeypatch.setenv(WORKER_POLICY_ENV, "off")
    assert (
        "Workers carry the same tool set as you — including MCP tools — and can "
        "fetch data on their own."
    ) in _desc()
