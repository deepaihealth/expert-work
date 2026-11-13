"""B-300 评测场景:组织规矩(§8.1 / §8.7)、结构性质(内容只从该来的地方来)、关键串可用。离线。"""

from __future__ import annotations

from collections import Counter

import b300_scenarios as sc
import pytest


def _norm(text: str) -> str:
    return "".join(text.split()).lower()


def test_sizes_and_coverage() -> None:
    everything = sc.build_all()
    assert sc.coverage_problems(everything) == []
    counts = Counter(s.group for s in everything)
    assert counts == {"A": 120, "B": 70, "C": 25}
    assert len(sc.build_compare()) == 20


def test_coverage_check_catches_a_missing_form() -> None:
    broken = [s for s in sc.build_all() if not (s.cls == "P1" and s.form == "negation")]
    assert any("P1" in p for p in sc.coverage_problems(broken))


def test_facts_are_generic_and_balanced() -> None:
    assert Counter(f.domain for f in sc.FACTS) == dict.fromkeys(sc.DOMAINS, 2)
    assert {f.form for f in sc.FACTS} == set(sc.FORMS)
    assert sum(f.form in sc.VALUE_FORMS for f in sc.FACTS) * 2 <= len(sc.FACTS)
    assert [f.key for f in sc.FACTS if f.incident] == ["glucose"]  # 健康 · 数值,只是其中一个


@pytest.mark.parametrize("fact", sc.FACTS, ids=lambda f: f.key)
@pytest.mark.parametrize("lang", sc.LANGS)
def test_keys_separate_x_from_y(fact: sc.Fact, lang: str) -> None:
    """程序粗查靠关键串:X 的串在 X 的句子里、不在 Y 的句子里,反之亦然;短更新里有 Y 的串。"""
    t = fact.texts[lang]
    for text in (t.state_x, t.memory_x, t.asks):
        assert _norm(t.x_key) in _norm(text)
        assert _norm(t.y_key) not in _norm(text)
    for text in (t.state_y, t.memory_y, t.short_y):
        assert _norm(t.y_key) in _norm(text)
        assert _norm(t.x_key) not in _norm(text)


def test_each_class_uses_both_languages() -> None:
    for group in ("A", "B"):
        for cls in sc.classes_of(group):
            langs = {s.lang for s in sc.build_group(group) if s.cls == cls}
            assert langs == set(sc.LANGS), cls


def _user_texts(s: sc.Scenario) -> str:
    return " ".join(t.user for t in s.turns)


@pytest.mark.parametrize("cls", ["P2", "P3", "P4", "P5", "P7"])
def test_derived_content_never_in_user_text(cls: str) -> None:
    """P2~P5、P7:该内容只出现在工具结果 / 图 / 外部内容 / 助手的话里,用户输入的文字里没有。"""
    for s in sc.build_group("A"):
        if s.cls != cls:
            continue
        assert _norm(s.x_key) not in _norm(_user_texts(s)), s.scenario_id
        carriers = " ".join(
            [t.reply for t in s.turns]
            + [r for t in s.turns for _n, r in t.tool_calls]
            + [e for t in s.turns for e in t.external]
        )
        assert _norm(s.x_key) in _norm(carriers), s.scenario_id


def test_source_paths_are_structural() -> None:
    by = {
        s.cls: s for s in sc.build_group("A") if s.domain == "support" and s.fact_key == "return-no"
    }
    assert by["P2"].turns[0].tool_calls and not by["P2"].turns[0].images
    assert by["P3"].turns[0].images == 1
    assert by["P4"].turns[0].external
    assert by["P6"].turns[1].user == sc.fact("return-no").texts[by["P6"].lang].x  # 只答一个词
    assert by["A12"].writebacks == tuple(range(sc.A12_ROUNDS))
    assert by["P1"].writebacks == (0,)


def test_p8_rotates_the_four_kinds() -> None:
    kinds = {s.variant for s in sc.build_group("A") if s.cls == "P8"}
    assert kinds == {"p8_question", "p8_guess", "p8_hypo", "p8_hearsay"}


def test_b1_incident_shape() -> None:
    (incident,) = [s for s in sc.build_group("B") if s.incident]
    assert incident.cls == "B1" and incident.domain == "health"
    (seed,) = incident.seeds
    assert seed.origin is None and seed.confidence == sc.INCIDENT_CONFIDENCE
    assert seed.content == sc.fact("glucose").texts["en"].memory_x  # 事故那条是英文


def test_b_seeds_and_documents() -> None:
    rows = {(s.cls, s.fact_key): s for s in sc.build_group("B")}
    b3 = rows[("B3", "budget")]
    lang = b3.lang
    assert b3.seeds[0].origin == "assistant_derived"
    assert sc.fact("budget").texts[lang].y in b3.conversations[0].documents[0][1]
    b5 = rows[("B5", "budget")]
    assert [s.days_ago for s in b5.seeds] == [sc.MAX_SEED_DAYS_AGO, 3]  # X 早、Y 晚
    # B4:一半种「用户说的」,一半种「用户更正」(置信度 1.0)。
    assert {s.confidence for f in sc.FACTS for s in rows[("B4", f.key)].seeds} == {0.9, 1.0}
    assert rows[("B7", "budget")].seeds[0].kind == "episodic"


def test_c_group_shapes() -> None:
    rows = sc.build_group("C")
    assert Counter(s.cls for s in rows) == dict.fromkeys(sc.C_CLASSES, 5)
    c5 = next(s for s in rows if s.cls == "C5")
    seeds = {s.key: s for s in c5.seeds}
    assert seeds["x"].superseded_by == "y"
    memory_x = sc.fact(c5.fact_key).texts[c5.lang].memory_x
    assert memory_x in seeds["near1"].content and memory_x in seeds["near2"].content
    c4 = next(s for s in rows if s.cls == "C4")
    assert len(c4.conversations) == 3 and not c4.seeds
    c1 = next(s for s in rows if s.cls == "C1")
    assert all(_norm(c1.x_key) not in _norm(seed.content) for seed in c1.seeds)


def test_no_tenant_or_company_names() -> None:
    blob = repr(sc.to_jsonable([*sc.build_all(), *sc.build_compare()]))
    for name in ("深护", "山丘", "联康", "expert-work", "expert_work"):
        assert name not in blob


def test_digest_is_stable() -> None:
    assert sc.scenario_digest(sc.build_all()) == sc.scenario_digest(sc.build_all())
