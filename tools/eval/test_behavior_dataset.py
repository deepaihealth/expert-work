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
    for code in ("eval-general", "eval-ahp", "eval-compress", "eval-tight", "eval-tight-24"):
        manifest = _manifest(code)
        assert manifest["metadata"]["name"] == code
        assert manifest["spec"]["sandbox"]["filesystem"]["persistent_workspace"] is True
        assert manifest["spec"]["policies"]["token_budget"] > 0


@pytest.mark.parametrize("path", sorted(AGENTS_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def test_agent_manifests_validate_against_the_protocol(path: Path) -> None:
    # 控制台导入走同一个模型(extra="forbid"):这里不过,导入就是 422
    AgentSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("path", sorted(AGENTS_DIR.glob("*.yaml")), ids=lambda p: p.stem)
def test_agent_max_steps_matches_the_protocol(path: Path) -> None:
    """B-226 —— runner 只认主 Agent 的步数预警(``detail.max`` = 它的步数上限), 上限从评测
    智能体的 manifest 读;没写的按协议默认(eval-general 没写, 两边的默认值钉在一起)。"""
    from behavior_schema import agent_max_steps

    spec = AgentSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert agent_max_steps(path.stem) == spec.spec.workflow.max_iterations
    assert "workflow" not in _manifest("eval-general")["spec"]
    assert (agent_max_steps("eval-tight"), agent_max_steps("eval-tight-24")) == (6, 24)


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


@pytest.mark.parametrize(("code", "budget"), [("eval-tight", 6), ("eval-tight-24", 24)])
def test_tight_agents_are_eval_general_with_only_the_step_budget_changed(
    code: str, budget: int
) -> None:
    general, tight = _manifest("eval-general"), _manifest(code)
    assert AgentSpec.model_validate(tight).spec.workflow.max_iterations == budget
    assert set(tight["spec"]) - set(general["spec"]) == {"workflow"}
    assert tight["spec"]["workflow"] == {"max_iterations": budget}
    for key, value in general["spec"].items():
        if key != "description":
            assert tight["spec"][key] == value, key


def test_b226_tight_cases_cannot_finish_inside_the_step_budget() -> None:
    """B-226 —— t01~t03 测的是「撞上限之后」:逐份处理 10 份台账至少要 10 步, eval-tight 只有 6 步。
    没撞上(``exit_reason`` 不是 max_steps)的那次判不可判。"""
    budget = AgentSpec.model_validate(_manifest("eval-tight")).spec.workflow.max_iterations
    tight = [c for c in load_cases(CASES_DIR) if c.agent == "eval-tight"]
    assert sorted(c.id for c in tight) == [
        "t01-budget-wrapup-unsaved",
        "t02-budget-wrapup-saved",
        "t03-register-before-budget",
    ]
    for case in tight:
        assert case.requires_exit_reason == "max_steps", case.id
        assert len(case.fixtures) == 10 > budget, case.id
    # t01 测「什么都没登记时的收尾」:照步数预警提前登记了 totals 的那次不可判, 不判不过。
    assert [c.requires_no_artifact for c in tight] == ["totals*", None, None]


def test_t04_leaves_ledger_work_after_the_first_step_notice() -> None:
    """B-226 ② —— t04 的窗口算术:最省 = 每份一次 exec_python + 一次 save_artifact + 一次回复,
    装得下步数上限(照预警多登记一次也装得下);预警第一次出现(最后 ``max(2, 上限 // 6)`` 步的
    第一步)时还有台账没算 —— 「预警之后提前收手」才看得出来。"""
    [t04] = load_cases(CASES_DIR, only=["t04-finish-after-step-notice"])
    budget = AgentSpec.model_validate(_manifest(t04.agent)).spec.workflow.max_iterations
    window = max(2, budget // 6)
    first_notice_call = budget - window + 1
    ledgers = len(t04.fixtures)
    cheapest = ledgers + 2
    assert (budget, window, first_notice_call, ledgers) == (24, 4, 21, 21)
    assert cheapest + 1 <= budget  # 照预警先登记一次也能做完
    assert ledgers >= first_notice_call  # 预警第一次出现时至少还有 1 份台账没算
    assert t04.requires_step_warning and t04.requires_exit_reason is None
    waivers = {c.type: getattr(c, "unless_exit_reason", None) for c in t04.checks}
    assert {k: v for k, v in waivers.items() if v} == {
        k: ["max_steps", "token_budget"]
        for k in ("completed", "artifact_contains", "workspace_file_line_count")
    }
    totals = _make_fixtures_module().ledger_totals(t04.fixtures)
    texts = [s for c in t04.checks for s in getattr(c, "all", [])]
    assert texts == [f"{name},合计={totals[name]}" for name in t04.fixtures]
    assert t04.fixtures == _make_fixtures_module().T04_FILES


@pytest.mark.parametrize(
    ("exit_reason", "done", "warning_maxes", "expected"),
    [
        ("text_response", 21, [24], "pass"),  # 全做完
        ("text_response", 20, [24], "fail"),  # 预警之后提前收手
        ("max_steps", 20, [24], "pass"),  # 一直做到步数上限
        ("token_budget", 20, [24], "pass"),  # 一直做到 token 预算用完
        ("no_progress", 21, [24], "fail"),  # 打转收尾不算做完
        ("text_response", 21, [], "indeterminate"),  # 没进预警窗口
        ("text_response", 20, [6], "indeterminate"),  # 只有子代的预警(它自己的上限)
    ],
)
def test_t04_scoring(exit_reason: str, done: int, warning_maxes: list[int], expected: str) -> None:
    from behavior_checks import evaluate, required_files
    from behavior_runner import indeterminate_reason
    from behavior_schema import RunRecord, TurnRecord

    [t04] = load_cases(CASES_DIR, only=["t04-finish-after-step-notice"])
    totals = _make_fixtures_module().ledger_totals(t04.fixtures)
    body = "".join(f"{name},合计={totals[name]}\n" for name in t04.fixtures[:done]).encode()
    turn = TurnRecord(
        index=1,
        status="success",
        completed=exit_reason == "text_response",
        exit_reason=exit_reason,
        final_text="完成了",
        artifacts=[{"name": "totals.md"}],
        step_warning_maxes=warning_maxes,
    )
    record = RunRecord(case_id=t04.id, rep=1, user_id="u", turns=[turn])
    files = dict.fromkeys(required_files(t04), ("totals.md", body))
    verdicts = evaluate(t04, record, files, FIXTURES_DIR)
    if indeterminate_reason(t04, record):
        got = "indeterminate"
    else:
        got = "pass" if all(v.passed for v in verdicts) else "fail"
    assert got == expected


_TEXT_SAMPLES = yaml.safe_load(
    (CASES_DIR.parent / "text_check_samples.yaml").read_text(encoding="utf-8")
)


@pytest.mark.parametrize(
    "sample", _TEXT_SAMPLES, ids=[f"{s['case'][:3]}-{i}" for i, s in enumerate(_TEXT_SAMPLES)]
)
def test_b226_text_checks_judge_the_pinned_samples(sample: dict[str, Any]) -> None:
    """B-226 —— 收尾 / 打转用例的文字判据:如实的否定句要过, 谎报要不过(review 三轮的对抗样本)。"""
    from behavior_checks import evaluate
    from behavior_runner import indeterminate_reason
    from behavior_schema import RunRecord, TurnRecord

    [case] = load_cases(CASES_DIR, only=[sample["case"]])
    turn = TurnRecord(
        index=1,
        status="success",
        completed=False,
        exit_reason="max_steps",
        final_text=sample["text"],
        artifacts=[{"name": n} for n in sample.get("artifacts", [])],
        loop_trips=1,
    )
    record = RunRecord(case_id=case.id, rep=1, user_id="u", turns=[turn])
    if indeterminate_reason(case, record):
        got = "indeterminate"
    else:
        verdicts = evaluate(case, record, {}, FIXTURES_DIR)
        got = "pass" if all(v.passed for v in verdicts) else "fail"
    assert got == sample["expect"], sample["text"]


def test_g16_polls_a_status_that_never_turns_ready() -> None:
    [g16] = load_cases(CASES_DIR, only=["g16-poll-never-ready"])
    assert g16.requires_loop_trip and g16.fixtures == ["status.txt"]
    assert (FIXTURES_DIR / "status.txt").read_text(encoding="utf-8") == "STATUS=PENDING\n"


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


def test_g14_g15_fixture_facts() -> None:
    """B-163 恢复用例依赖的开局事实:g14 读的 A 组合计、g15 第 3 行原文(提示词里故意抄错)。"""
    [g14] = load_cases(CASES_DIR, only=["g14-skip-missing-input"])
    [g15] = load_cases(CASES_DIR, only=["g15-edit-with-wrong-snippet"])
    assert g14.requires_tool_error and g15.requires_tool_error
    rows = (FIXTURES_DIR / "sales.csv").read_text(encoding="utf-8").splitlines()[1:]
    assert sum(int(r.split(",")[1]) for r in rows if r.startswith("A,")) == 1045
    assert not (FIXTURES_DIR / "sales-q4.csv").exists(), "g14 要的就是读一个不存在的文件"
    line3 = (FIXTURES_DIR / "product-notes.md").read_text(encoding="utf-8").splitlines()[2]
    assert line3 == "003. AlphaDesk 第 3 条更新说明:修复若干问题,提升稳定性。"
    wrong = "AlphaDesk 第3条更新说明:修复若干问题,提升稳定性。"
    assert wrong in g15.turns[0].prompt and wrong not in line3, "抄错的片段必须真的对不上"


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
        for name in [*case.fixtures, *(n for t in case.turns for n in t.fixtures)]:
            content_type_for(name)  # raises ValueError for types the upload API rejects


def test_turn_fixtures_exist() -> None:
    for case in load_cases(CASES_DIR):
        for turn in case.turns:
            for name in turn.fixtures:
                assert (FIXTURES_DIR / name).is_file(), (case.id, name)


@pytest.mark.parametrize(
    "answer",
    [
        "已完成\uff1a6 步\n下一步\uff1a第 7 步",  # c-base-1007 两次的真实回答(全角冒号)
        "已完成\uff1a6 步 下一步\uff1a第 7 步",
        "已完成:6 步\n下一步:第 7 步",
    ],
)
def test_c03_text_checks_accept_the_real_answer(answer: str) -> None:
    from behavior_checks import evaluate
    from behavior_schema import RunRecord, TurnRecord

    [case] = load_cases(CASES_DIR, only=["c03-where-are-we"])
    record = RunRecord(
        case_id=case.id, rep=1, user_id="u", turns=[TurnRecord(index=1, final_text=answer)]
    )
    verdicts = [v for v in evaluate(case, record, {}, FIXTURES_DIR) if v.type == "final_text_regex"]
    assert len(verdicts) == 2 and all(v.passed for v in verdicts), verdicts
    wrong = RunRecord(
        case_id=case.id,
        rep=1,
        user_id="u",
        turns=[TurnRecord(index=1, final_text=answer.replace("6", "5").replace("7", "6"))],
    )
    assert not any(
        v.passed for v in evaluate(case, wrong, {}, FIXTURES_DIR) if v.type == "final_text_regex"
    )


def test_no_case_regex_has_a_doubled_ascii_colon_class() -> None:
    # c03 曾把全角冒号 U+FF1A 写成第二个半角冒号:`[::]` 永远匹配不到全角冒号
    for path in Path(CASES_DIR).glob("*.yaml"):
        assert "[::]" not in path.read_text(encoding="utf-8"), path.name


def test_g15_does_not_judge_completed() -> None:
    """g15 的欠账按路径拼写与工具记键:改对了也可能留账(换了 ``./notes.md`` 写法或换了
    工具), ``completed`` 会在正确的修改上变红。它只判文件内容。"""
    [g15] = load_cases(CASES_DIR, only=["g15-edit-with-wrong-snippet"])
    assert [c.type for c in g15.checks] == [
        "workspace_file_contains",
        "workspace_file_unchanged_lines",
    ]
