"""B-206 多轮指代评测:``coref`` 判据的归类、外发参数信号、汇总(全部离线,合成记录)。"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from behavior_checks import evaluate
from behavior_coref import classify, parse_detail, summarize
from behavior_schema import (
    AGENTS_DIR,
    CASES_DIR,
    FIXTURES_DIR,
    Case,
    CaseResult,
    Coref,
    RunRecord,
    ToolCall,
    TurnRecord,
    load_cases,
)
from pydantic import ValidationError

_PRODUCT = Coref(
    type="coref",
    referent="product",
    turn=2,
    target=["26 个月"],
    others=["18 个月"],
    names=["KX-305"],
)
_AMBIGUOUS = Coref(
    type="coref", referent="product", turn=2, target=["1299"], others=["899"], ambiguous=True
)


def _record(final: str, *, query: str | None = "KX-305 保修期", error: bool = False) -> RunRecord:
    calls = (
        [ToolCall(turn=2, name="knowledge_search", args={"query": query}, error=error)]
        if query is not None
        else []
    )
    return RunRecord(
        case_id="r00-x",
        rep=1,
        user_id="u",
        turns=[
            TurnRecord(index=1, final_text="KX-305 是除湿机,KX-217 是空气净化器。"),
            TurnRecord(index=2, final_text=final, tool_calls=calls),
        ],
    )


@pytest.mark.parametrize(
    ("final", "outcome", "passed"),
    [
        ("KX-305 整机保修 26 个月。", "right", True),
        ("整机保修 18 个月。", "confident_wrong", False),
        ("您是问 KX-217 吗?它保修 18 个月。", "wrong_hedged", False),
        # 结尾一句客套的问句不是反问「指哪个」
        ("整机保修 18 个月。还有别的问题吗?", "confident_wrong", False),
        ("KX-305 保修 26 个月,KX-217 是 18 个月。", "mixed", True),
        ("您指的是哪一款?", "over_clarified", False),
        ("没有查到。", "missed", False),
    ],
)
def test_unambiguous_outcomes(final: str, outcome: str, passed: bool) -> None:
    got = classify(_PRODUCT, _record(final))
    assert (got.outcome, got.passed) == (outcome, passed)


@pytest.mark.parametrize(
    ("final", "outcome", "passed"),
    [
        ("KX-305 售价 1299 元,KX-217 售价 899 元。", "answered_both", True),
        ("您问的是 KX-305 还是 KX-217?", "clarified", True),
        ("如果是 KX-305,售价 1299 元;您指的是哪一款?", "hedged_pick", True),
        ("售价 1,299 元。", "confident_pick", False),  # 两个候选只答一个且不问 = 猜
        ("没有查到。", "missed", False),
    ],
)
def test_ambiguous_outcomes(final: str, outcome: str, passed: bool) -> None:
    got = classify(_AMBIGUOUS, _record(final, query=None))
    assert (got.outcome, got.passed) == (outcome, passed)


def test_plural_needs_every_target() -> None:
    plural = Coref(
        type="coref", referent="plural", turn=2, target=["26 个月", "18 个月"], others=["15 个月"]
    )
    assert classify(plural, _record("分别是 26 个月和 18 个月。")).outcome == "right"
    assert classify(plural, _record("KX-305 是 26 个月。")).outcome == "missed"


def test_facts_match_across_spacing_commas_and_hyphens() -> None:
    check = Coref(type="coref", referent="list_item", turn=2, target=["0512-6633-4071"])
    assert classify(check, _record("电话 0512 66334071。")).outcome == "right"


def test_outgoing_query_signals() -> None:
    named = classify(_PRODUCT, _record("26 个月", query="KX-305 的保修期"))
    assert (named.named, named.query_named, named.query_demonstrative) == (False, True, False)
    vague = classify(_PRODUCT, _record("KX-305 保修 26 个月", query="它的保修期多久"))
    assert (vague.named, vague.query_named, vague.query_demonstrative) == (True, False, True)
    # 被拒的调用不算;这一轮没调工具就记「不适用」
    refused = classify(_PRODUCT, _record("26 个月", query="KX-305", error=True))
    assert (refused.query_named, refused.query_demonstrative) == (None, None)


def test_the_verdict_always_carries_its_detail() -> None:
    case = Case(
        id="r00-x",
        agent="eval-coref",
        shape="t",
        turns=[{"prompt": "a"}, {"prompt": "b"}],  # type: ignore[list-item]
        checks=[_PRODUCT],
    )
    [verdict] = evaluate(case, _record("KX-305 整机保修 26 个月。"), {}, fixtures_dir=None)  # type: ignore[arg-type]
    assert verdict.passed is True
    assert parse_detail(verdict.detail) == {
        "referent": "product",
        "ambiguous": "0",
        "outcome": "right",
        "named": "1",
        "query_named": "1",
        "query_demonstrative": "0",
    }


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"ambiguous": True, "others": []}, "ambiguous"),
        ({"target": ["26 个月"], "others": ["26 个月"]}, "both"),
        ({"referent": "pet"}, "referent"),
    ],
)
def test_coref_check_rules(fields: dict[str, Any], message: str) -> None:
    base = {"type": "coref", "referent": "product", "turn": 2, "target": ["26 个月"]}
    with pytest.raises(ValidationError, match=message):
        Coref.model_validate({**base, **fields})


def test_the_coref_turn_must_exist() -> None:
    with pytest.raises(ValidationError, match="turn 3"):
        Case(
            id="r00-x",
            agent="eval-coref",
            shape="t",
            turns=[{"prompt": "a"}, {"prompt": "b"}],  # type: ignore[list-item]
            checks=[{**_PRODUCT.model_dump(), "turn": 3}],  # type: ignore[list-item]
        )


def _result(case_id: str, detail: str, passed: bool) -> CaseResult:
    verdict = {"type": "coref", "passed": passed, "detail": detail}
    return CaseResult(case_id=case_id, rep=1, passed=passed, verdicts=[verdict])  # type: ignore[list-item]


def test_summary_reports_the_three_numbers_and_a_row_per_referent_type() -> None:
    d = "referent={} ambiguous={} outcome={} named=1 query_named={} query_demonstrative={}"
    results = [
        _result("r01", d.format("person", 0, "right", 1, 0), True),
        _result("r01", d.format("person", 0, "confident_wrong", 0, 1), False),
        _result("r03", d.format("customer", 0, "wrong_hedged", "-", "-"), False),
        _result("r02", d.format("person", 1, "clarified", "-", "-"), True),
        _result("r05", d.format("product", 1, "confident_pick", "-", "-"), False),
        _result("r05", d.format("product", 1, "answered_both", "-", "-"), True),
        CaseResult(case_id="r09", rep=1, passed=None, infra_error="boom"),
    ]
    text = summarize(results)
    assert "指代对象明确 n=3:指错对象 66.7%(2/3),其中很笃定地答错对象 1" in text
    assert "有两个以上候选 n=3:追问 33.3%(1/3),追问或两个都答 66.7%(2/3),只猜一个 1" in text
    assert "外发检索参数:点名 50.0%(1/2),残留指示词 1" in text
    assert "  person n=3 通过 2" in text
    assert "  customer n=1 通过 0" in text
    assert "(另有 1 次没跑成,不计)" in text


# ---------------------------------------------------------------------------
# 用例集 / fixtures 自检
# ---------------------------------------------------------------------------


def _fixtures_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mk", FIXTURES_DIR / "make_fixtures.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _norm(text: str) -> str:
    return re.sub("[\\s,\uff0c\\-]", "", text)


def _coref_cases() -> list[Case]:
    return [c for c in load_cases(CASES_DIR) if c.id.startswith("r")]


def _corpus() -> str:
    module = _fixtures_module()
    texts = [
        (FIXTURES_DIR / "kb" / base / doc).read_text(encoding="utf-8")
        for base, docs in module.COREF_DOCS.items()
        for doc in docs
    ]
    texts += [
        (FIXTURES_DIR / name).read_text(encoding="utf-8") for name in module.COREF_ATTACHMENTS
    ]
    return "\n".join(texts)


def test_coref_cases_cover_every_referent_type_with_ambiguous_ones() -> None:
    cases = _coref_cases()
    assert len(cases) >= 12
    checks = [c for case in cases for c in case.checks if isinstance(c, Coref)]
    assert all(case.agent == "eval-coref" for case in cases)
    assert all(sum(isinstance(c, Coref) for c in case.checks) == 1 for case in cases)
    assert {c.referent for c in checks} == set(Coref.model_fields["referent"].annotation.__args__)  # type: ignore[union-attr]
    ambiguous = {c.referent for c in checks if c.ambiguous}
    assert len(ambiguous) >= 3  # 不只商品:人 / 客户 / 文件也有两个候选的
    assert "product" in ambiguous and "person" in ambiguous


def test_every_coref_fact_appears_exactly_once_in_the_synthetic_corpus() -> None:
    """回复里出现了谁的事实就是指了谁:前提是每条事实只属于一个对象。"""
    corpus = _norm(_corpus())
    for case in _coref_cases():
        for check in case.checks:
            assert isinstance(check, Coref)
            for fact in [*check.target, *check.others]:
                key = _norm(fact)
                assert corpus.count(key) == 1, (case.id, fact)
            for name in check.names:
                assert _norm(name) in corpus, (case.id, name)


def test_coref_turn_attachments_are_the_generated_ones() -> None:
    module = _fixtures_module()
    used = {n for case in _coref_cases() for t in case.turns for n in t.fixtures}
    assert used == set(module.COREF_ATTACHMENTS)


def test_eval_coref_binds_only_the_coref_base_and_the_readme_lists_it() -> None:
    module = _fixtures_module()
    manifest = yaml.safe_load((AGENTS_DIR / "eval-coref.yaml").read_text(encoding="utf-8"))
    assert manifest["spec"]["knowledge"]["knowledge_base_refs"] == list(module.COREF_BASES)
    readme = (Path(__file__).parent / "README.md").read_text(encoding="utf-8")
    for base, description in module.COREF_BASES.items():
        assert f"`{base}`「{description}」" in readme, base
    assert sorted(p.name for p in (FIXTURES_DIR / "kb" / "b140-kb-coref").iterdir()) == sorted(
        module.COREF_DOCS["b140-kb-coref"]
    )
