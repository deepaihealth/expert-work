"""B-140 数据集自检:用例都能加载、fixtures 与智能体都在、fixtures 与生成脚本一致。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from behavior_schema import AGENTS_DIR, CASES_DIR, FIXTURES_DIR, load_cases

from expert_work.protocol.agent_spec import AgentSpec


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


def _manifest(code: str) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((AGENTS_DIR / f"{code}.yaml").read_text(encoding="utf-8"))
    return raw


def test_agent_manifests_match_codes() -> None:
    for code in ("eval-general", "eval-ahp", "eval-compress"):
        manifest = _manifest(code)
        assert manifest["metadata"]["name"] == code
        assert manifest["spec"]["sandbox"]["filesystem"]["persistent_workspace"] is True
        assert manifest["spec"]["policies"]["token_budget"] > 0


@pytest.mark.parametrize("path", sorted(AGENTS_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def test_agent_manifests_validate_against_the_protocol(path: Path) -> None:
    # 控制台导入走同一个模型(extra="forbid"):这里不过,导入就是 422
    AgentSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def test_eval_compress_is_eval_general_with_only_the_compression_knobs_changed() -> None:
    general, compress = _manifest("eval-general"), _manifest("eval-compress")
    spec = AgentSpec.model_validate(compress).spec
    assert spec.policies.context_compression.absolute_cap_tokens == 30_000
    assert spec.policies.working_memory.enabled is False
    # 关掉它 agent_factory 就不建 ToolResultPruner,跨轮清理(cross_turn)也在它里面
    assert spec.policies.tool_result_prune.enabled is False
    for key in ("tenant_config", "model", "system_prompt", "tools", "sandbox"):
        assert compress["spec"][key] == general["spec"][key], key
    allowed = {"description", "workflow", "policies"}
    assert set(compress["spec"]) - set(general["spec"]) <= allowed
    assert {k for k in general["spec"] if compress["spec"].get(k) != general["spec"][k]} <= allowed
    assert set(compress["spec"]["policies"]) == {
        "token_budget",
        "context_compression",
        "working_memory",
        "tool_result_prune",
    }


def test_c01_checks_carry_the_generated_totals() -> None:
    totals = _make_fixtures_module().c01_totals()
    [case] = load_cases(CASES_DIR, only=["c01-ten-ledgers-one-by-one"])
    assert case.fixtures == list(totals)
    patterns = {getattr(c, "pattern", "") for c in case.checks}
    for name, total in totals.items():
        assert f"^\\s*{name.replace('.', chr(92) + '.')},合计={total}\\s*$" in patterns, name


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
