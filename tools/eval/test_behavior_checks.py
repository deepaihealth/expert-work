"""B-140 behavior_checks 单元测试:每种判据一条「好记录过」、一条「坏记录不过」。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from behavior_checks import changed_lines, evaluate, path_matches, required_files
from behavior_schema import Case, RunRecord, ToolCall, TurnRecord


def _case(checks: list[dict[str, Any]]) -> Case:
    return Case.model_validate(
        {
            "id": "g99-x",
            "agent": "eval-general",
            "shape": "test",
            "turns": [{"prompt": "p"}],
            "checks": checks,
        }
    )


def _turn(index: int = 1, **kw: Any) -> TurnRecord:
    base: dict[str, Any] = {
        "index": index,
        "status": "success",
        "completed": True,
        "exit_reason": "text_response",
        "final_text": "A=104.5, B=209.0",
        "input_tokens": 1000,
        "output_tokens": 50,
    }
    base.update(kw)
    return TurnRecord.model_validate(base)


def _record(*turns: TurnRecord) -> RunRecord:
    return RunRecord(case_id="g99-x", rep=1, user_id="u", turns=list(turns))


def _one(
    check: dict[str, Any],
    record: RunRecord,
    files: dict | None = None,
    fixtures: Path | None = None,
) -> tuple[bool, str]:
    [v] = evaluate(_case([check]), record, files or {}, fixtures or Path("."))
    return v.passed, v.detail


def test_completed_requires_every_turn() -> None:
    assert _one({"type": "completed"}, _record(_turn(1), _turn(2)))[0]
    ok, detail = _one({"type": "completed"}, _record(_turn(1), _turn(2, completed=False)))
    assert not ok and "[2]" in detail
    assert not _one({"type": "completed"}, _record(_turn(1, exit_reason="max_steps")))[0]


def test_not_completed_needs_an_explicit_false_on_the_last_turn() -> None:
    assert _one({"type": "not_completed"}, _record(_turn(completed=False)))[0]
    assert not _one({"type": "not_completed"}, _record(_turn(completed=True)))[0]
    # 缺记录不等于 false:不能把「没数据」当成「账还在」
    ok, detail = _one({"type": "not_completed"}, _record(_turn(completed=None)))
    assert not ok and "None" in detail
    assert not _one({"type": "not_completed"}, _record())[0]


def test_exit_reason_reads_last_turn() -> None:
    assert _one({"type": "exit_reason", "value": "text_response"}, _record(_turn()))[0]
    assert not _one(
        {"type": "exit_reason", "value": "text_response"}, _record(_turn(exit_reason="no_progress"))
    )[0]


def test_final_text_checks() -> None:
    rec = _record(_turn(final_text="结果:A=104.5"))
    assert _one({"type": "final_text_contains", "all": ["A=104.5"]}, rec)[0]
    assert not _one({"type": "final_text_contains", "all": ["B=209.0"]}, rec)[0]
    assert _one({"type": "final_text_not_contains", "any": ["教练"]}, rec)[0]
    assert not _one({"type": "final_text_not_contains", "any": ["A="]}, rec)[0]
    assert _one({"type": "final_text_regex", "pattern": r"A=\d+\.\d"}, rec)[0]
    assert not _one({"type": "final_text_regex", "pattern": r"C=\d"}, rec)[0]


def test_artifact_exists_glob_across_turns() -> None:
    rec = _record(_turn(1), _turn(2, artifacts=[{"name": "王小雨-健康方案.docx", "version": 1}]))
    assert _one({"type": "artifact_exists", "name": "*.docx"}, rec)[0]
    assert not _one({"type": "artifact_exists", "name": "*.pptx"}, rec)[0]


def test_artifact_and_workspace_contains() -> None:
    files = {
        ("artifact", "*.md"): ("plan.md", "饮食\n运动\n".encode()),
        ("workspace", "report.md"): ("report.md", "数据截至 2026 年 9 月\n".encode()),
    }
    rec = _record(_turn())
    assert _one({"type": "artifact_contains", "name": "*.md", "all": ["饮食", "运动"]}, rec, files)[
        0
    ]
    ok, detail = _one({"type": "artifact_contains", "name": "*.md", "all": ["睡眠"]}, rec, files)
    assert not ok and "睡眠" in detail
    assert _one({"type": "artifact_not_contains", "name": "*.md", "any": ["教练"]}, rec, files)[0]
    assert not _one({"type": "artifact_not_contains", "name": "*.md", "any": ["饮食"]}, rec, files)[
        0
    ]
    assert _one(
        {"type": "workspace_file_contains", "path": "report.md", "all": ["2026 年 9 月"]},
        rec,
        files,
    )[0]


def test_missing_or_unreadable_file_fails_with_reason() -> None:
    rec = _record(_turn())
    ok, detail = _one(
        {"type": "artifact_contains", "name": "*.docx", "all": ["x"]},
        rec,
        {("artifact", "*.docx"): None},
    )
    assert not ok and "not found" in detail
    ok, detail = _one(
        {"type": "artifact_contains", "name": "*.docx", "all": ["x"]},
        rec,
        {("artifact", "*.docx"): ("a.docx", b"not a zip")},
    )
    assert not ok and "unreadable" in detail


def test_unchanged_lines(tmp_path: Path) -> None:
    (tmp_path / "f.md").write_text("一\n二\n三\n四\n", encoding="utf-8")
    rec = _record(_turn())
    check = {
        "type": "workspace_file_unchanged_lines",
        "path": "f.md",
        "fixture": "f.md",
        "except_lines": [2],
    }
    good = {("workspace", "f.md"): ("f.md", "一\n贰\n三\n四\n".encode())}
    bad = {("workspace", "f.md"): ("f.md", "一\n贰\n叁\n四\n".encode())}
    assert _one(check, rec, good, tmp_path)[0]
    ok, detail = _one(check, rec, bad, tmp_path)
    assert not ok and "[3]" in detail


def test_changed_lines_counts_insertions_and_deletions() -> None:
    assert changed_lines("a\nb\nc\n", "a\nb\nc\n") == set()
    assert changed_lines("a\nb\nc\n", "a\nX\nc\n") == {2}
    assert changed_lines("a\nb\nc\n", "a\nb\nNEW\nc\n") == {2}
    assert changed_lines("a\nb\nc\n", "a\nc\n") == {2}
    assert changed_lines("a\n", "NEW\na\n") == {1}


def test_tool_checks() -> None:
    rec = _record(
        _turn(1, tool_calls=[ToolCall(turn=1, name="bash", args={"command": "cp a b"})]),
        _turn(
            2,
            tool_calls=[ToolCall(turn=2, name="edit_file", args={"path": "/workspace/report.md"})],
        ),
    )
    assert _one({"type": "tool_used", "tool": "edit_file"}, rec)[0]
    assert not _one({"type": "tool_used", "tool": "write_file"}, rec)[0]
    assert _one({"type": "tool_not_used", "tool": "write_file"}, rec)[0]
    assert not _one({"type": "tool_not_used", "tool": "bash"}, rec)[0]
    assert _one({"type": "tool_count_max", "max": 2}, rec)[0]
    assert not _one({"type": "tool_count_max", "max": 1}, rec)[0]
    assert _one({"type": "tool_count_max", "max": 1, "tool": "bash"}, rec)[0]
    assert _one({"type": "tool_not_used_on", "tool": "write_file", "path": "report.md"}, rec)[0]
    ok, detail = _one({"type": "tool_not_used_on", "tool": "edit_file", "path": "report.md"}, rec)
    assert not ok and "[2]" in detail


def test_path_matches_normalizes_workspace_prefixes() -> None:
    assert path_matches("/workspace/report.md", "report.md")
    assert path_matches("./uploads/a.md", "uploads/a.md")
    assert path_matches("/workspace/agent-key/report.md", "report.md")
    assert not path_matches("myreport.md", "report.md")


def test_turn_tokens_max() -> None:
    rec = _record(_turn(1, input_tokens=900), _turn(2, input_tokens=5000))
    assert _one({"type": "turn_tokens_max", "turn": 1, "max_input_tokens": 1000}, rec)[0]
    ok, detail = _one({"type": "turn_tokens_max", "turn": 2, "max_input_tokens": 1000}, rec)
    assert not ok and "5000" in detail
    assert not _one({"type": "turn_tokens_max", "turn": 3, "max_input_tokens": 1000}, rec)[0]


def test_turn_tokens_max_without_usage_fails() -> None:
    ok, detail = _one(
        {"type": "turn_tokens_max", "turn": 1, "max_input_tokens": 10},
        _record(_turn(input_tokens=None)),
    )
    assert not ok and "no usage" in detail


def test_required_files_dedupes_in_order() -> None:
    case = _case(
        [
            {"type": "artifact_contains", "name": "*.docx", "all": ["a"]},
            {"type": "artifact_not_contains", "name": "*.docx", "any": ["b"]},
            {"type": "workspace_file_contains", "path": "r.md", "all": ["c"]},
            {"type": "completed"},
        ]
    )
    assert required_files(case) == [("artifact", "*.docx"), ("workspace", "r.md")]


@pytest.mark.parametrize(
    "check_type",
    [
        "completed",
        "not_completed",
        "exit_reason",
        "final_text_contains",
        "final_text_not_contains",
        "final_text_regex",
        "artifact_exists",
        "artifact_contains",
        "artifact_not_contains",
        "artifact_unchanged_lines",
        "workspace_file_contains",
        "workspace_file_not_contains",
        "workspace_file_unchanged_lines",
        "workspace_file_line_count",
        "tool_used",
        "tool_not_used",
        "tool_count_max",
        "tool_not_used_on",
        "turn_tokens_max",
    ],
)
def test_every_check_type_is_documented_in_spec(check_type: str) -> None:
    spec = (
        Path(__file__).resolve().parents[2]
        / "docs/superpowers/specs/2026-10-05-b140-behavior-regression-eval-design.md"
    )
    assert f"`{check_type}`" in spec.read_text(encoding="utf-8")


def test_workspace_file_not_contains() -> None:
    files = {("workspace", "r.md"): ("r.md", b"BetaDesk\n")}
    rec = _record(_turn())
    assert _one(
        {"type": "workspace_file_not_contains", "path": "r.md", "any": ["AlphaDesk"]}, rec, files
    )[0]
    ok, detail = _one(
        {"type": "workspace_file_not_contains", "path": "r.md", "any": ["Beta"]}, rec, files
    )
    assert not ok and "Beta" in detail


def test_tool_checks_scoped_to_a_turn() -> None:
    rec = _record(
        _turn(1, tool_calls=[ToolCall(turn=1, name="write_file", args={"path": "a.md"})]),
        _turn(2, tool_calls=[ToolCall(turn=2, name="save_artifact", args={"name": "poem.md"})]),
    )
    assert _one({"type": "tool_not_used_on", "tool": "write_file", "path": "a.md", "turn": 2}, rec)[
        0
    ]
    assert not _one({"type": "tool_not_used_on", "tool": "write_file", "path": "a.md"}, rec)[0]
    assert _one({"type": "tool_used", "tool": "save_artifact", "turn": 2}, rec)[0]
    assert not _one({"type": "tool_used", "tool": "save_artifact", "turn": 1}, rec)[0]
    assert _one({"type": "tool_not_used", "tool": "write_file", "turn": 2}, rec)[0]
    assert _one({"type": "tool_count_max", "max": 1, "turn": 1}, rec)[0]


def test_completed_reports_missing_record_separately() -> None:
    ok, detail = _one({"type": "completed"}, _record(_turn(completed=None, exit_reason=None)))
    assert not ok and "no completion record" in detail


def _line_count(pattern: str, count: int) -> dict[str, Any]:
    return {"type": "workspace_file_line_count", "path": "r.md", "pattern": pattern, "count": count}


def test_workspace_file_line_count() -> None:
    files = {("workspace", "r.md"): ("r.md", "# 结果\na,1\nb,2\na,1\n".encode())}
    rec = _record(_turn())
    assert _one(_line_count(r"^[ab],\d$", 3), rec, files)[0]
    ok, detail = _one(_line_count(r"^a,1$", 1), rec, files)
    assert not ok and "2 lines" in detail
    assert _one(_line_count(r"^c,", 0), rec, files)[0]
    assert not _one(_line_count(r"^b,2$", 2), rec, files)[0]
    ok, detail = _one(_line_count(r"^a,", 2), rec, {("workspace", "r.md"): None})
    assert not ok and "not found" in detail
    assert required_files(_case([_line_count("x", 1)])) == [("workspace", "r.md")]
