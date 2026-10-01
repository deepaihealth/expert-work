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
    assert "not deliverables until they are registered as such" in block
    assert "if you do not have that tool" in block
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
        "Workers carry the same tool set as you — including MCP tools, but no "
        "knowledge-base search — and can fetch data on their own."
    ) in _desc()


@pytest.mark.parametrize("policy", [None, "off"])
def test_spawn_worker_description_is_truthful(
    monkeypatch: pytest.MonkeyPatch, policy: str | None
) -> None:
    """Cost / tools / result size / parallelism claims match the code
    (subagent_runtime worker synth, overflow.EXTERNALIZE_MIN_CHARS, workspace lock)."""
    if policy is None:
        monkeypatch.delenv(WORKER_POLICY_ENV, raising=False)
    else:
        monkeypatch.setenv(WORKER_POLICY_ENV, policy)
    desc = _desc()
    # False claims are gone.
    assert "lightweight" not in desc
    assert "cheap" not in desc
    assert "fast," not in desc
    assert "lands in this conversation in full" not in desc
    # True clauses are present.
    assert "full, separate agent run" in desc
    assert "its own token cost" in desc
    assert "re-sent on every later step" in desc
    assert "about as much" not in desc
    assert "no knowledge-base search" in desc
    assert "longer than about 12,000 characters" in desc
    assert "head/tail preview plus a path" in desc
    assert "file writes / bash commands take turns" in desc
