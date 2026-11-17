"""B-300 重做的评测场景(设计 2026-11-15 §11.2 / §11.3):S 组成对来源、形状留出集、A13~A15、
同一天读数。只查数据的形状与组织规矩,离线。"""

from __future__ import annotations

from collections import Counter

import b300_redesign_base as base
import b300_redesign_scenarios as ns
import b300_scenarios as sc
import pytest


def _rows(cls: str) -> list[sc.Scenario]:
    return [s for s in ns.build_redesign() if s.cls == cls]


def test_s_group_size_is_240_units() -> None:
    """§11.2:S1~S6 各 10 个命题、S7 10 个(第 9 版从 5 个加到 10 个)、S8 10 个,每个 3 次,共 240 个
    单元。"""
    sizes = {cls: len(_rows(cls)) for cls in ns.S_CLASSES}
    assert sizes == dict.fromkeys(ns.S_CLASSES, 10)
    assert sum(sizes.values()) * 3 == 240


def test_other_sets_have_the_designed_size() -> None:
    """§11.2 形状留出集每种 5 个;§11.3 A13 10、A14 2(各 12 轮)、A15 5、同一天读数三种各 10。"""
    assert {c: len(_rows(c)) for c in ns.SHAPE_CLASSES} == dict.fromkeys(ns.SHAPE_CLASSES, 5)
    assert len(_rows("A13")) == 10 and len(_rows("A15")) == 5
    assert [len(s.turns) for s in _rows("A14")] == [12, 12]
    assert {c: len(_rows(c)) for c in ns.SD_CLASSES} == dict.fromkeys(ns.SD_CLASSES, 10)


def test_coverage_rules_hold() -> None:
    assert ns.coverage_problems(ns.build_redesign()) == []


def test_domains_are_even_and_none_over_a_fifth() -> None:
    for cls in (*ns.S_CLASSES, "A13", "A15", *ns.SD_CLASSES, *ns.SHAPE_CLASSES):
        per = Counter(s.domain for s in _rows(cls))
        assert len(per) >= 5, cls
        assert max(per.values()) * 5 <= sum(per.values()), (cls, per)


def test_shape_set_uses_other_domains_than_the_s_group() -> None:
    """留出集:换领域(§11.2「同 S7,换领域」),整组与 S 组的领域不重叠。"""
    shape = {s.domain for c in ns.SHAPE_CLASSES for s in _rows(c)}
    s_domains = {s.domain for c in ns.S_CLASSES for s in _rows(c)}
    assert len(shape) == 5 and not shape & s_domains


def test_s1_to_s6_are_paired_on_the_same_propositions() -> None:
    """AuthMem 的成对设计:同一个命题,只换它怎么进到对话里;领域、语言、关键串都不变。"""
    by_prop: dict[str, set[tuple[str, str, str]]] = {}
    for cls in ("S1", "S2", "S3", "S4", "S5", "S6"):
        for s in _rows(cls):
            by_prop.setdefault(s.fact_key, set()).add((s.domain, s.lang, s.x_key))
    assert len(by_prop) == 10 and all(len(v) == 1 for v in by_prop.values())


def test_s_forms_rotate_through_every_content_form() -> None:
    forms = {s.form for s in _rows("S1")}
    assert forms == set(ns.S_FORMS)


def _user_text(s: sc.Scenario) -> str:
    return " ".join(t.user for t in s.turns)


def _assistant_text(s: sc.Scenario) -> str:
    return " ".join(t.reply for t in s.turns)


def _tool_text(s: sc.Scenario) -> str:
    return " ".join(r for t in s.turns for _n, r in t.tool_calls)


def test_where_the_value_appears_matches_each_condition() -> None:
    for s in _rows("S1"):
        assert base.has_key(_user_text(s), s.x_key) and not base.has_key(
            _assistant_text(s), s.x_key
        )
    for s in _rows("S2"):  # 只在用户那一句回答里
        assert s.turns[1].user == ns.spec(s).answer_text
        assert base.has_key(s.turns[1].user, s.x_key)
        assert not base.has_key(_assistant_text(s), s.x_key)
    for cls in ("S3", "S4"):  # 助手说出来,用户只回一个字
        for s in _rows(cls):
            assert base.has_key(s.turns[0].reply, s.x_key)
            assert not base.has_key(_user_text(s), s.x_key)
            assert len(s.turns[1].user) <= 4
    for s in _rows("S5"):  # 助手说出来,用户接着说无关的
        assert base.has_key(s.turns[0].reply, s.x_key) and not base.has_key(_user_text(s), s.x_key)
    for s in _rows("S6"):  # 只在工具结果里
        assert base.has_key(_tool_text(s), s.x_key)
        assert not base.has_key(_assistant_text(s) + _user_text(s), s.x_key)


def test_s7_reply_has_findings_and_one_proposal_and_a_bare_ok() -> None:
    for s in _rows("S7"):
        sp = ns.spec(s)
        assert len(sp.other_keys) == 2
        assert all(base.has_key(s.turns[0].reply, k) for k in (*sp.other_keys, s.x_key))
        assert len(s.turns[1].user) <= 4


def test_secrets_appear_only_where_the_user_gives_them() -> None:
    for cls in ("S8", "H4"):
        for s in _rows(cls):
            secret = ns.spec(s).secret
            assert secret and base.has_key(s.turns[-1].user, secret)
            assert not base.has_key(_assistant_text(s) + _tool_text(s), secret)


def test_every_writeback_follows_a_turn_and_s_units_have_a_bystander() -> None:
    """切片之后每轮都写回一次(真实的形状);S 组每个场景种一条同领域、无关的用户说的记忆,用来量
    「误伤」(§11.3)。"""
    for s in ns.build_redesign():
        if s.cls == "A15":
            continue
        assert s.writebacks == tuple(range(len(s.turns))), s.scenario_id
    for cls in ns.S_CLASSES:
        for s in _rows(cls):
            (by,) = [seed for seed in s.seeds if seed.key == ns.BYSTANDER]
            assert by.origin == "user_stated" and not base.has_key(by.content, s.x_key)
            assert ns.spec(s).bystander_key and base.has_key(by.content, ns.spec(s).bystander_key)


def test_a15_seeds_one_of_each_origin() -> None:
    for s in _rows("A15"):
        origins = sorted(str(seed.origin) for seed in s.seeds)
        assert origins == ["assistant_derived", "user_confirmed", "user_stated"]
        reply = s.turns[0].reply
        assert all(base.has_key(reply, ns.spec(s).seed_keys[seed.key]) for seed in s.seeds)


def test_same_day_readings_are_paired_and_numbers_do_not_collide() -> None:
    by_reading: dict[str, set[tuple[str, str]]] = {}
    for cls in ns.SD_CLASSES:
        for s in _rows(cls):
            by_reading.setdefault(s.fact_key, set()).add((s.x_key, s.domain))
            assert s.x_key != s.y_key
            assert base.has_key(s.turns[0].user, s.x_key)
            assert not base.has_key(s.turns[0].user, s.y_key)
    assert len(by_reading) == 10 and all(len(v) == 1 for v in by_reading.values())


@pytest.mark.parametrize(
    "word",
    # 提示词附录 A 里例子的领域词与内容(§11.2:领域与内容不得取自平台提示词里的例子)
    [
        *("成都", "机票", "Priya", "台灯", "RX-4471", "T-200", "T-250", "月季", "18342"),
        *("482913", "西班牙", "Spanish", "booking page", "electricity", "reviewers", "pastillas"),
    ],
)
def test_no_scenario_reuses_the_prompt_examples(word: str) -> None:
    for s in ns.build_redesign():
        text = " ".join(
            [_user_text(s), _assistant_text(s), _tool_text(s), *(x.content for x in s.seeds)]
        )
        assert word.lower() not in text.lower(), (word, s.scenario_id)


def test_ids_are_unique_and_do_not_clash_with_the_old_groups() -> None:
    new = ns.build_redesign()
    ids = [s.scenario_id for s in new]
    assert len(ids) == len(set(ids))
    old = {s.scenario_id for s in [*sc.build_all(), *sc.build_compare()]}
    assert not set(ids) & old
    assert ns.scenario_digest_all() == ns.scenario_digest_all()


def test_both_languages_in_every_set() -> None:
    for cls in (*ns.S_CLASSES, *ns.SHAPE_CLASSES, "A13", "A14", "A15", *ns.SD_CLASSES):
        assert {s.lang for s in _rows(cls)} == {"zh", "en"}, cls
