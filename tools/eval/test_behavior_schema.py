"""B-140 behavior_schema 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from behavior_schema import (
    CaseResult,
    CheckVerdict,
    ResultHeader,
    ToolArgContains,
    ToolUsed,
    TurnRecord,
    WorkspaceFileContains,
    WorkspaceFileLineCount,
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


def test_case_set_hash_ignores_caches_and_dotfiles(tmp_path: Path) -> None:
    cases, fixtures = tmp_path / "cases", tmp_path / "fixtures"
    _write(cases, "g99-sample.yaml", _CASE)
    _write(fixtures, "a.md", "旧\n")
    before = case_set_hash(cases, fixtures)
    (fixtures / "__pycache__").mkdir()
    (fixtures / "__pycache__" / "make_fixtures.cpython-313.pyc").write_bytes(b"\x00pyc")
    (fixtures / ".DS_Store").write_bytes(b"mac")
    assert case_set_hash(cases, fixtures) == before


@pytest.mark.parametrize("check_type", ["final_text_regex", "final_text_not_regex"])
def test_final_text_regex_must_compile(tmp_path: Path, check_type: str) -> None:
    bad = _CASE.replace("{type: completed}", f"{{type: {check_type}, pattern: '(unclosed'}}")
    with pytest.raises(ValidationError, match="invalid regex"):
        load_case(_write(tmp_path, "g99-sample.yaml", bad))


def test_load_cases_rejects_non_yaml_files(tmp_path: Path) -> None:
    _write(tmp_path, "g99-sample.yaml", _CASE)
    _write(tmp_path, "g98-other.yml", _CASE.replace("g99-sample", "g98-other"))
    with pytest.raises(ValueError, match=r"g98-other\.yml"):
        load_cases(tmp_path)


@pytest.mark.parametrize("check_type", ["artifact_contains", "artifact_not_contains"])
def test_content_checks_on_pdf_or_xlsx_are_rejected_at_load(
    tmp_path: Path, check_type: str
) -> None:
    key = "all" if check_type == "artifact_contains" else "any"
    bad = _CASE.replace("{type: completed}", f"{{type: {check_type}, name: '*.pdf', {key}: [x]}}")
    with pytest.raises(ValidationError, match="text"):
        load_case(_write(tmp_path, "g99-sample.yaml", bad))


def test_tool_checks_accept_a_turn(tmp_path: Path) -> None:
    case = load_case(
        _write(
            tmp_path,
            "g99-sample.yaml",
            _CASE.replace("tool: edit_file}", "tool: edit_file, turn: 2}"),
        )
    )
    assert isinstance(case.checks[1], ToolUsed) and case.checks[1].turn == 2


def test_compression_cases_take_a_c_id_and_the_compress_agent(tmp_path: Path) -> None:
    text = _CASE.replace("g99-sample", "c99-sample").replace("eval-general", "eval-compress")
    case = load_case(_write(tmp_path, "c99-sample.yaml", text + "requires_compaction: true\n"))
    assert case.agent == "eval-compress" and case.requires_compaction is True
    assert load_case(_write(tmp_path, "g99-sample.yaml", _CASE)).requires_compaction is False


def test_recovery_cases_can_require_a_tool_error(tmp_path: Path) -> None:
    case = load_case(_write(tmp_path, "g99-sample.yaml", _CASE + "requires_tool_error: true\n"))
    assert case.requires_tool_error is True
    assert load_case(_write(tmp_path, "g99-sample.yaml", _CASE)).requires_tool_error is False


@pytest.mark.parametrize(
    "check",
    [
        "{type: workspace_file_line_count, path: r.md, pattern: '(unclosed', count: 1}",
        "{type: workspace_file_line_count, path: r.md, pattern: '^a', count: -1}",
        "{type: workspace_file_line_count, path: r.md, pattern: '^a', count: 1, extra: 1}",
        "{type: workspace_file_line_count, path: r.md, count: 1}",
    ],
)
def test_line_count_check_is_strict(tmp_path: Path, check: str) -> None:
    bad = _CASE.replace("{type: completed}", check)
    with pytest.raises(ValidationError):
        load_case(_write(tmp_path, "g99-sample.yaml", bad))


def test_indeterminate_result_round_trips(tmp_path: Path) -> None:
    out = tmp_path / "r.jsonl"
    header = ResultHeader(
        label="b",
        started_at="t",
        base_url="http://localhost",
        case_set_hash="a" * 12,
        repeats=1,
        case_ids=["c99-sample"],
    )
    result = CaseResult(
        case_id="c99-sample", rep=1, passed=None, indeterminate="no compaction in any turn"
    )
    append_line(out, header)
    append_line(out, result)
    assert read_results(out)[1] == [result]


def test_turns_may_bring_their_own_fixtures(tmp_path: Path) -> None:
    text = _CASE.replace(
        "  - prompt: 改一下\n", "  - prompt: 改一下\n  - prompt: 再改\n    fixtures: [b.md]\n"
    )
    case = load_case(_write(tmp_path, "g99-sample.yaml", text))
    assert case.turns[0].fixtures == [] and case.turns[1].fixtures == ["b.md"]


def test_knowledge_cases_take_a_k_id_and_the_kb_agent(tmp_path: Path) -> None:
    text = (
        _CASE.replace("g99-sample", "k99-sample")
        .replace("eval-general", "eval-kb")
        .replace(
            "{type: completed}",
            "{type: tool_arg_contains, tool: knowledge_search, arg: bases, any_of: [K2, specs]}",
        )
    )
    case = load_case(_write(tmp_path, "k99-sample.yaml", text))
    assert case.agent == "eval-kb"
    check = case.checks[0]
    assert isinstance(check, ToolArgContains)
    assert check.any_of == ["K2", "specs"] and check.call == "first" and check.turn is None


@pytest.mark.parametrize(
    "check",
    [
        "{type: tool_arg_contains, tool: knowledge_search, arg: bases, any_of: []}",
        "{type: tool_arg_contains, tool: knowledge_search, arg: bases, any_of: [K2], call: last}",
        "{type: tool_arg_contains, tool: knowledge_search, any_of: [K2]}",
        "{type: tool_arg_contains, tool: knowledge_search, arg: bases, any_of: [K2], turn: 0}",
    ],
)
def test_tool_arg_check_is_strict(tmp_path: Path, check: str) -> None:
    bad = _CASE.replace("{type: completed}", check)
    with pytest.raises(ValidationError):
        load_case(_write(tmp_path, "g99-sample.yaml", bad))


@pytest.mark.parametrize(
    ("any_of", "none_of", "ok"),
    [
        ("[K2]", "[k2]", False),  # 句柄不分大小写:同一个库
        ("[specs, K2]", "[specs]", False),
        ("[specs]", "[SPECS]", True),  # 库名区分大小写:不是同一个库
        ("[K2]", "[K1, K3]", True),
    ],
)
def test_tool_arg_check_rejects_overlapping_lists(
    tmp_path: Path, any_of: str, none_of: str, ok: bool
) -> None:
    """同一个值既在 any_of 又在 none_of:判据永远不过,加载时就报错。"""
    check = (
        "{type: tool_arg_contains, tool: knowledge_search, arg: bases, "
        f"any_of: {any_of}, none_of: {none_of}}}"
    )
    text = _CASE.replace("{type: completed}", check)
    if ok:
        load_case(_write(tmp_path, "g99-sample.yaml", text))
        return
    with pytest.raises(ValidationError, match="both any_of and none_of"):
        load_case(_write(tmp_path, "g99-sample.yaml", text))


def test_line_count_check_loads(tmp_path: Path) -> None:
    ok = _CASE.replace(
        "{type: completed}",
        "{type: workspace_file_line_count, path: r.md, pattern: '^a', count: 0}",
    )
    check = load_case(_write(tmp_path, "g99-sample.yaml", ok)).checks[0]
    assert isinstance(check, WorkspaceFileLineCount) and check.count == 0


def test_tight_budget_cases_take_a_t_id_and_can_require_an_exit_reason(tmp_path: Path) -> None:
    """B-226 —— 撞步数上限的用例跑在 ``eval-tight``;这次没撞上(``exit_reason`` 不同)就不可判。"""
    text = _CASE.replace("g99-sample", "t99-sample").replace("eval-general", "eval-tight")
    case = load_case(
        _write(tmp_path, "t99-sample.yaml", text + "requires_exit_reason: max_steps\n")
    )
    assert case.agent == "eval-tight" and case.requires_exit_reason == "max_steps"
    assert load_case(_write(tmp_path, "g99-sample.yaml", _CASE)).requires_exit_reason is None


def test_loop_cases_can_require_a_loop_trip(tmp_path: Path) -> None:
    case = load_case(_write(tmp_path, "g99-sample.yaml", _CASE + "requires_loop_trip: true\n"))
    assert case.requires_loop_trip is True
    assert load_case(_write(tmp_path, "g99-sample.yaml", _CASE)).requires_loop_trip is False


def test_unsaved_cases_can_require_that_no_artifact_matches(tmp_path: Path) -> None:
    text = _CASE + "requires_no_artifact: 'totals*'\n"
    assert load_case(_write(tmp_path, "g99-sample.yaml", text)).requires_no_artifact == "totals*"
    assert load_case(_write(tmp_path, "g99-sample.yaml", _CASE)).requires_no_artifact is None


def test_turn_record_loop_trips_default_to_zero() -> None:
    assert TurnRecord(index=1).loop_trips == 0
    assert TurnRecord(index=1).step_warning_maxes == []


def test_step_notice_cases_can_require_a_step_warning(tmp_path: Path) -> None:
    text = _CASE.replace("g99-sample", "t99-sample").replace("eval-general", "eval-tight-24")
    case = load_case(_write(tmp_path, "t99-sample.yaml", text + "requires_step_warning: true\n"))
    assert case.agent == "eval-tight-24" and case.requires_step_warning is True
    assert load_case(_write(tmp_path, "g99-sample.yaml", _CASE)).requires_step_warning is False


def test_checks_take_unless_exit_reason(tmp_path: Path) -> None:
    ok = _CASE.replace("{type: completed}", "{type: completed, unless_exit_reason: max_steps}")
    check = load_case(_write(tmp_path, "g99-sample.yaml", ok)).checks[0]
    assert getattr(check, "unless_exit_reason", None) == ["max_steps"]  # 单个值收成列表
    both = _CASE.replace(
        "{type: completed}", "{type: completed, unless_exit_reason: [max_steps, token_budget]}"
    )
    check = load_case(_write(tmp_path, "g99-sample.yaml", both)).checks[0]
    assert getattr(check, "unless_exit_reason", None) == ["max_steps", "token_budget"]
