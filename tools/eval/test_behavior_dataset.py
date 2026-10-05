"""B-140 数据集自检:用例都能加载、fixtures 与智能体都在、fixtures 与生成脚本一致。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import yaml
from behavior_schema import AGENTS_DIR, CASES_DIR, FIXTURES_DIR, load_cases


def _make_fixtures_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "make_fixtures", FIXTURES_DIR / "make_fixtures.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_all_cases_load_and_reference_existing_files() -> None:
    cases = load_cases(CASES_DIR)
    assert {c.id for c in cases} >= {"g02-edit-three-places", "g08-group-mean", "h01-word-plan"}
    for case in cases:
        for name in case.fixtures:
            assert (FIXTURES_DIR / name).is_file(), (case.id, name)
        for check in case.checks:
            fixture = getattr(check, "fixture", None)
            if fixture:
                assert (FIXTURES_DIR / fixture).is_file(), (case.id, fixture)


def test_agent_manifests_match_codes() -> None:
    for code in ("eval-general", "eval-ahp"):
        manifest = yaml.safe_load((AGENTS_DIR / f"{code}.yaml").read_text(encoding="utf-8"))
        assert manifest["metadata"]["name"] == code
        assert manifest["spec"]["sandbox"]["filesystem"]["persistent_workspace"] is True
        assert manifest["spec"]["policies"]["token_budget"] > 0


def test_committed_fixtures_match_generator() -> None:
    built = _make_fixtures_module().build()
    for name, content in built.items():
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        assert (FIXTURES_DIR / name).read_bytes() == data, name


def test_fixture_facts_the_cases_rely_on() -> None:
    from behavior_extract import document_text

    module = _make_fixtures_module()
    regulation = (FIXTURES_DIR / "regulation-60k.md").read_text(encoding="utf-8")
    assert len(regulation) > 50_000 and regulation.rstrip().endswith("B140-Z9Q7。")
    notes = (FIXTURES_DIR / "product-notes.md").read_text(encoding="utf-8").splitlines()
    assert [
        i + 1 for i, line in enumerate(notes) if "AlphaDesk" in line
    ] == module.G03_RENAMED_LINES
    script = (FIXTURES_DIR / "buggy-script.txt").read_text(encoding="utf-8").splitlines()
    assert "range(1, limit)" in script[module.G04_BUG_LINE - 1]
    report = document_text(
        "checkup-report.docx", (FIXTURES_DIR / "checkup-report.docx").read_bytes()
    )
    assert len(report) > 30_000 and report.splitlines()[-1].startswith("尿酸")
    plan = document_text("previous-plan.docx", (FIXTURES_DIR / "previous-plan.docx").read_bytes())
    assert "每周快走3次" in plan and "23:30前入睡" in plan


def test_g02_fixture_has_the_three_outdated_lines_where_the_case_says() -> None:
    lines = (FIXTURES_DIR / "report-300-lines.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 300
    assert "2026 年 6 月" in lines[11] and "987 人" in lines[139] and "张工" in lines[287]


def test_no_coach_wording_in_cases() -> None:
    for path in Path(CASES_DIR).glob("*.yaml"):
        assert "教练" not in path.read_text(encoding="utf-8").replace("any: [教练]", ""), path.name


def test_every_fixture_used_by_a_case_is_an_allowed_upload_type() -> None:
    from behavior_client import content_type_for

    for case in load_cases(CASES_DIR):
        for name in case.fixtures:
            content_type_for(name)  # raises ValueError for types the upload API rejects
