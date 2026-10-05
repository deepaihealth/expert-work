"""B-140 behavior_schema 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from behavior_schema import (
    CaseResult,
    CheckVerdict,
    ResultHeader,
    ToolUsed,
    WorkspaceFileContains,
    append_line,
    case_set_hash,
    load_case,
    load_cases,
    read_results,
)
from pydantic import ValidationError

_CASE = """\
id: g99-sample
agent: eval-general
shape: B-137
fixtures: [a.md]
turns:
  - prompt: 改一下
checks:
  - {type: completed}
  - {type: tool_used, tool: edit_file}
  - {type: workspace_file_contains, path: a.md, all: [新]}
"""


def _write(dir_: Path, name: str, text: str) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / name
    p.write_text(text, encoding="utf-8")
    return p


def test_load_case_parses_discriminated_checks(tmp_path: Path) -> None:
    case = load_case(_write(tmp_path, "g99-sample.yaml", _CASE))
    assert case.agent == "eval-general"
    assert isinstance(case.checks[1], ToolUsed)
    assert isinstance(case.checks[2], WorkspaceFileContains)
    assert case.checks[2].path == "a.md"


def test_load_case_rejects_stem_mismatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="file stem"):
        load_case(_write(tmp_path, "g99-other.yaml", _CASE))


def test_load_case_rejects_unknown_check_type(tmp_path: Path) -> None:
    bad = _CASE.replace("{type: completed}", "{type: vibes_ok}")
    with pytest.raises(ValidationError):
        load_case(_write(tmp_path, "g99-sample.yaml", bad))


def test_load_cases_only_filters_and_rejects_unknown(tmp_path: Path) -> None:
    _write(tmp_path, "g99-sample.yaml", _CASE)
    _write(tmp_path, "g98-other.yaml", _CASE.replace("g99-sample", "g98-other"))
    assert [c.id for c in load_cases(tmp_path)] == ["g98-other", "g99-sample"]
    assert [c.id for c in load_cases(tmp_path, only=["g99-sample"])] == ["g99-sample"]
    with pytest.raises(ValueError, match="unknown case ids"):
        load_cases(tmp_path, only=["g01-nope"])


def test_load_cases_only_rejects_unknown(tmp_path: Path) -> None:
    _write(tmp_path, "g99-sample.yaml", _CASE)
    with pytest.raises(ValueError, match="g00-missing"):
        load_cases(tmp_path, only=["g00-missing"])


def test_case_set_hash_changes_with_fixture_bytes(tmp_path: Path) -> None:
    cases, fixtures = tmp_path / "cases", tmp_path / "fixtures"
    _write(cases, "g99-sample.yaml", _CASE)
    _write(fixtures, "a.md", "旧\n")
    before = case_set_hash(cases, fixtures)
    _write(fixtures, "a.md", "新\n")
    assert case_set_hash(cases, fixtures) != before
    assert len(before) == 12


def test_results_round_trip(tmp_path: Path) -> None:
    out = tmp_path / "r.jsonl"
    header = ResultHeader(
        label="base",
        started_at="2026-10-05T00:00:00+00:00",
        base_url="http://localhost",
        case_set_hash="abc123abc123",
        repeats=3,
        case_ids=["g99-sample"],
    )
    result = CaseResult(
        case_id="g99-sample",
        rep=1,
        passed=False,
        verdicts=[CheckVerdict(type="completed", passed=False, detail="turns not completed: [1]")],
        metrics={"tool_calls": 2.0},
    )
    append_line(out, header)
    append_line(out, result)
    got_header, got_results = read_results(out)
    assert got_header == header
    assert got_results == [result]


def test_read_results_requires_header(tmp_path: Path) -> None:
    out = tmp_path / "r.jsonl"
    append_line(out, CaseResult(case_id="g99-sample", rep=1, passed=True))
    with pytest.raises(ValueError, match="header"):
        read_results(out)
