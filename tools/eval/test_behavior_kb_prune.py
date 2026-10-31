"""B-206 track D (k06 / k07) — the ``eval-kb-prune`` agent and its synthetic manual.

The cases test what an agent does once the cross-turn cleanup (B-126) has
collapsed an earlier turn's ``knowledge_search`` result to a one-line reference,
so the cleanup must run at the start of turn 3 for certain. It runs when the
earlier turns hold at least ``min_context_tokens`` and collapsing saves at least
``min_reclaim_tokens`` (o200k: the estimator the agent factory injects). The
sizes are computed from the fixture: the manual is cut into parents the way an
ingest cuts them (structure and size alone; topic shifts only split children),
and with a base's default settings (no score threshold) a search returns the
best ``limit`` parents within the result text budget.
"""

from __future__ import annotations

import importlib.util
import re
from types import ModuleType
from typing import Any

import pytest
import yaml
from behavior_schema import AGENTS_DIR, CASES_DIR, FIXTURES_DIR, load_cases

from control_plane.knowledge.chunking import chunk_markdown_parented
from expert_work.protocol import knowledge_document_title
from expert_work.protocol.agent_spec import AgentSpec
from expert_work.protocol.knowledge import (
    DEFAULT_CHUNK_MAX_TOKENS,
    DEFAULT_CHUNK_OVERLAP_TOKENS,
)
from orchestrator.tools import knowledge
from orchestrator.tools.knowledge_results import RESULTS_TEXT_BUDGET

#: What one collapsed search costs once it is a reference stub (the line is
#: under 700 characters; o200k counts Chinese and ASCII alike at most ~1/char,
#: and the line is mostly ASCII), rounded up.
_STUB_TOKENS = 300


def _make_fixtures() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "make_fixtures", FIXTURES_DIR / "make_fixtures.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(code: str) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((AGENTS_DIR / f"{code}.yaml").read_text(encoding="utf-8"))
    return raw


def _manual() -> str:
    module = _make_fixtures()
    path = FIXTURES_DIR / "kb" / module.KB_MANUAL_BASE / module.KB_MANUAL_DOC
    return path.read_text(encoding="utf-8")


def _chapter(text: str, heading: str) -> str:
    start = text.index(f"## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def _count(text: str, number: str) -> int:
    return len(re.findall(rf"(?<![0-9.]){re.escape(number)}(?![0-9])", text))


def test_eval_kb_prune_is_eval_kb_with_only_the_base_and_prune_knobs_changed() -> None:
    kb, prune = _manifest("eval-kb"), _manifest("eval-kb-prune")
    spec = AgentSpec.model_validate(prune).spec
    assert prune["metadata"]["name"] == "eval-kb-prune"
    assert spec.knowledge is not None
    assert spec.knowledge.knowledge_base_refs == [_make_fixtures().KB_MANUAL_BASE]
    policy = spec.policies.tool_result_prune
    assert policy.enabled and policy.cross_turn
    assert (policy.min_context_tokens, policy.min_reclaim_tokens) == (3000, 500)
    assert policy.recent_tool_results_kept == 0
    assert spec.sandbox.filesystem.persistent_workspace is False
    for key, value in kb["spec"].items():
        if key not in ("description", "knowledge", "policies"):
            assert prune["spec"][key] == value, key
    assert set(prune["spec"]) == set(kb["spec"])
    assert prune["spec"]["policies"] == {
        **kb["spec"]["policies"],
        "tool_result_prune": {
            "min_context_tokens": 3000,
            "min_reclaim_tokens": 500,
            "recent_tool_results_kept": 0,
        },
    }
    assert {c.id for c in load_cases(CASES_DIR) if c.agent == "eval-kb-prune"} == {
        "k06-recover-pruned-search",
        "k07-pruned-search-not-stated",
    }


def test_the_manual_on_disk_is_what_make_fixtures_writes() -> None:
    module = _make_fixtures()
    name = f"kb/{module.KB_MANUAL_BASE}/{module.KB_MANUAL_DOC}"
    assert _manual() == module.build()[name]
    base_dir = FIXTURES_DIR / "kb" / module.KB_MANUAL_BASE
    assert [p.name for p in base_dir.iterdir()] == [module.KB_MANUAL_DOC]


def test_the_manual_facts_k06_and_k07_rely_on() -> None:
    """k06: VT-330's full-charge time is in its own chapter, its neighbours' differ.
    k07: the noise level is stated for VT-220 and VT-440 only. Every number a
    check reads appears once in the manual."""
    text = _manual()
    vt330 = _chapter(text, "VT-330")
    assert "电池容量:1036 Wh" in vt330 and "交流满充时间:137 分钟" in vt330
    assert "交流满充时间:112 分钟" in _chapter(text, "VT-220")
    assert "交流满充时间:163 分钟" in _chapter(text, "VT-440")
    assert "分贝" not in vt330
    assert "约 41 分贝" in _chapter(text, "VT-220") and "约 47 分贝" in _chapter(text, "VT-440")
    assert text.count("分贝") == 2
    assert "充到六成" in _chapter(text, "存放与保养")
    for number in ("1036", "137", "112", "163", "41", "47"):
        assert _count(text, number) == 1, number


def _parents() -> list[str]:
    module = _make_fixtures()
    return [
        parent.body
        for parent in chunk_markdown_parented(
            _manual(),
            max_tokens=DEFAULT_CHUNK_MAX_TOKENS,
            overlap_tokens=DEFAULT_CHUNK_OVERLAP_TOKENS,
            doc_title=knowledge_document_title(module.KB_MANUAL_DOC),
        )
    ]


def test_a_search_returns_the_whole_manual_and_vt330_shares_a_passage_with_vt440() -> None:
    """Every passage fits one search's default limit and text budget, so each
    search hands the model the whole manual; VT-330's passage also holds VT-440's
    chapter, so reading around VT-330 shows a neighbour's noise level (k07)."""
    parents = _parents()
    assert len(parents) <= knowledge._DEFAULT_LIMIT
    assert sum(len(body) for body in parents) <= RESULTS_TEXT_BUDGET
    [holding] = [body for body in parents if "电池容量:1036 Wh" in body]
    assert "约 47 分贝" in holding


def _encoding():  # type: ignore[no-untyped-def]
    tiktoken = pytest.importorskip("tiktoken")
    try:
        return tiktoken.get_encoding("o200k_base")
    except Exception as exc:  # offline: skipped like the runtime's tiktoken tests
        raise pytest.skip.Exception("o200k_base BPE unavailable (offline)") from exc


def test_two_earlier_searches_clear_the_cleanup_thresholds() -> None:
    """Turns 1 and 2 search once each at least; the system prompt is left out,
    so the earlier context is only larger than this."""
    enc = _encoding()
    policy = AgentSpec.model_validate(_manifest("eval-kb-prune")).spec.policies.tool_result_prune
    per_search = len(enc.encode(_manual()))
    assert 2 * per_search >= policy.min_context_tokens
    # One collapsed search alone already saves enough.
    assert per_search - _STUB_TOKENS >= policy.min_reclaim_tokens
