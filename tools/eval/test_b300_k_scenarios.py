"""B-300 第 9 版 K 组的场景(设计 2026-11-15 §11.2a):18 类 x 10、近邻、种子的形状、分两半的类、
回归闸门的 K 段、使用侧 B4c / B4d。纯数据,不调模型。"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, replace

import b300_k_items as ki
import b300_k_scenarios as ks
import b300_redesign_scenarios as ns
import b300_scenarios as sc
import pytest
from b300_redesign_base import has_key

K = ks.build_k()


def _rows(cls: str) -> list[sc.Scenario]:
    return [s for s in K if s.cls == cls]


def _seed(s: sc.Scenario, key: str) -> ks.KSeed:
    seed = next(x for x in s.seeds if x.key == key)
    assert isinstance(seed, ks.KSeed)
    return seed


def test_k_group_is_18_classes_of_10_scenarios_540_units() -> None:
    """§11.2a:18 类,每类 10 个场景 x 3 次 = 30 个单元,共 540。"""
    assert len(ks.K_CLASSES) == 18
    assert Counter(s.cls for s in K) == dict.fromkeys(ks.K_CLASSES, 10)
    assert len(K) * 3 == 540
    assert ks.coverage_problems(K) == []


def test_every_class_covers_five_domains_two_languages_six_forms() -> None:
    for cls in ks.K_CLASSES:
        rows = _rows(cls)
        assert Counter(s.domain for s in rows) == dict.fromkeys(ki.K_DOMAINS, 2), cls
        assert Counter(s.lang for s in rows) == {"zh": 5, "en": 5}, cls
        assert {s.form for s in rows} == set(ki.K_FORMS), cls


def test_k_domains_do_not_overlap_the_other_groups() -> None:
    """K 组另用 5 个领域:与 A / S 组、形状留出集都不重叠。"""
    assert not set(ki.K_DOMAINS) & {*sc.DOMAINS, *ns.SHAPE_DOMAINS}


def test_coverage_problems_catches_a_missing_scenario_and_a_missing_neighbour() -> None:
    assert any("K-a: 9 scenarios" in p for p in ks.coverage_problems(K[1:]))
    s = K[0]
    stripped = replace(s, seeds=tuple(x for x in s.seeds if x.key != "near2"))
    assert any("1 neighbours" in p for p in ks.coverage_problems([stripped, *K[1:]]))


def test_every_scenario_but_k_late_seeds_two_neighbours() -> None:
    """审查 M-2:每个场景两条近邻(同一话题、另一个人 / 另一样东西 / 另一个场合);K-late 不种。"""
    for s in K:
        nears = [x for x in s.seeds if x.key in ks.NEAR_KEYS]
        if s.cls == "K-late":
            assert s.seeds == ()
            continue
        assert len(nears) == 2, s.scenario_id
        others = {x.content for x in s.seeds if x.key not in ks.NEAR_KEYS}
        assert all(n.content not in others for n in nears), s.scenario_id
        assert all(n.origin == "user_stated" for n in nears)


def test_the_first_neighbour_shares_the_old_value_but_is_about_someone_or_something_else() -> None:
    """近邻要够近:第一条与旧值 X 是同一个值(换了人 / 换了东西),模型才有可能误伤它。"""
    for it in ki.ITEMS:
        assert has_key(it.nears[0], it.x.key), it.key
        assert it.nears[0] != it.x.memory


def test_keys_find_their_own_text_and_not_the_other_value() -> None:
    for it in ki.ITEMS:
        for val, others in ((it.x, (it.y, it.z)), (it.y, (it.x, it.z)), (it.z, (it.x, it.y))):
            assert has_key(val.memory, val.key), (it.key, val.key)
            assert not any(has_key(val.memory, o.key) for o in others), (it.key, val.key)
        assert has_key(it.change_full, it.y.key) and not has_key(it.change_full, it.x.key)
        assert has_key(it.state_y, it.y.key) and has_key(it.state_y_alt, it.y.key)
        assert has_key(it.hedge_full, it.z.key) and has_key(it.hedge_memory, it.z.key)
        assert has_key(it.state_z, it.z.key)
        assert has_key(it.compound.memory, it.compound.a_key)
        assert has_key(it.compound.memory, it.compound.b_key)
        assert has_key(it.compound.deny_b, it.compound.deny_key), it.key
        for member in (it.trio.a, it.trio.b, it.trio.c):
            assert has_key(member.memory, member.key), (it.key, member.key)
            assert has_key(it.trio.summary, member.key), (it.key, member.key)
        assert has_key(it.trio.deny_b, it.trio.deny_key), it.key
        assert has_key(it.gone, it.gone_key) and has_key(it.both_gone, it.both_key), it.key


def test_hedge_sentences_carry_their_doubt_words() -> None:
    """K-b:犹豫要写在那句话里(设计 §7.1 第 15 行:犹豫留在正文里)。"""
    for it in ki.ITEMS:
        assert has_key(it.hedge_full, it.hedge_words), it.key
        assert has_key(it.hedge_memory, it.hedge_words), it.key
        assert not has_key(it.state_z, it.hedge_words), it.key


def test_writebacks_follow_every_turn_except_k_late() -> None:
    """K-late:一个写回覆盖两轮(只在最后写回一次);其余与 S 组一样每轮结束各写回一次。"""
    for s in K:
        if s.cls == "K-late":
            assert len(s.turns) == 3 and s.writebacks == (2,), s.scenario_id
        else:
            assert s.writebacks == tuple(range(len(s.turns))), s.scenario_id


def test_seed_shapes_per_class() -> None:
    """§11.2a 表「种下什么」:并存、置信度 1.0、连线、汇总行与成员。"""
    for s in _rows("K-t"):
        assert (_seed(s, "x").confidence, _seed(s, "x").origin) == (1.0, "user_stated")
    for s in _rows("K-e1"):
        x, y = _seed(s, "x"), _seed(s, "y")
        assert (x.origin, y.origin) == ("user_stated", "user_stated")
        assert x.superseded_by is None and y.linked_to is None  # 两条都有效、之间没有连线
        assert y.days_ago < x.days_ago
    for s in _rows("K-e2"):
        y = _seed(s, "y")
        assert (y.origin, y.confidence, y.linked_to) == ("user_confirmed", 0.8, "x")
    for s in _rows("K-w"):
        y = _seed(s, "y")
        assert (y.origin, y.confidence, y.linked_to) == ("assistant_derived", 0.6, None)
    for s in _rows("K-bp"):
        assert has_key(_seed(s, "hedge").content, ks.item_of(s).hedge_words)
    for s in _rows("K-g"):
        assert {x.key for x in s.seeds} == {"x", "x2", *ks.NEAR_KEYS}
    for s in _rows("K-p"):
        assert _seed(s, "x").content == ks.item_of(s).compound.memory
    for s in _rows("K-s"):
        summary = _seed(s, "summary")
        assert summary.summary_of == ("a", "b", "c")
        assert all(_seed(s, k).origin == "user_stated" for k in summary.summary_of)
    assert all(x.confidence < 1.0 for s in K if s.cls != "K-t" for x in s.seeds)


def test_halves_are_five_scenarios_one_per_domain() -> None:
    """分开报的半边:K-c′ 种子的来源、K-e2 / K-w 逐字与换说法、K-late、K-s 各 5 个场景。"""  # noqa: RUF002
    for cls, variants in ks.HALVES.items():
        for variant in variants:
            half = [s for s in _rows(cls) if s.variant == variant]
            assert len(half) == 5 and {s.domain for s in half} == set(ki.K_DOMAINS), (cls, variant)
            assert {s.lang for s in half} == {"zh", "en"}, (cls, variant)
    for s in _rows("K-cp"):
        assert s.variant == f"seed_{_seed(s, 'x').origin}"
    for s in _rows("K-e2"):
        said = s.turns[0].user
        it = ks.item_of(s)
        assert said == (it.state_y if s.variant == "verbatim" else it.state_y_alt)
    for s in _rows("K-s"):
        t = ks.item_of(s).trio
        assert s.turns[0].user == (t.deny_b if s.variant == "deny" else t.restate_b)


def test_k_n_rotates_the_four_shapes_short_and_full() -> None:
    """K-n1 是短回答(上一行是助手的提问),K-n2 是整句(一轮);四种形状轮换。"""
    for cls in ("K-n1", "K-n2"):
        assert {s.variant for s in _rows(cls)} == set(ks.N_SHAPES)
    for s in _rows("K-n1"):
        assert len(s.turns) == 2 and s.turns[0].reply
        assert has_key(s.turns[1].user, ks.item_of(s).z.key)
    for s in _rows("K-n2"):
        assert len(s.turns) == 1 and has_key(s.turns[0].user, ks.item_of(s).z.key)


def test_k_c_prime_has_a_finding_a_proposal_and_a_rotating_vague_reply() -> None:
    replies: Counter[int] = Counter()
    for s in _rows("K-cp"):
        it = ks.item_of(s)
        first, second = s.turns
        assert has_key(first.reply, it.y.key) and it.proposal in first.reply
        assert first.tool_calls and has_key(first.tool_calls[0][1], it.y.key)
        assert second.user in ki.VAGUE[s.lang]
        replies[ki.VAGUE[s.lang].index(second.user)] += 1
    assert set(replies) == {0, 1, 2, 3}  # 「好」「嗯」「谢谢」「继续」都出现


def test_k_a_carries_the_two_dated_scenarios() -> None:
    """§11.2a 末:「带日期的碰上不带日期的」并进 K-a 的 2 个场景(只报数)。"""
    dated = [s for s in _rows("K-a") if s.variant == "dated"]
    assert len(dated) == 2
    assert all(_seed(s, "x").occurred_days_ago is not None for s in dated)
    plain = [s for s in _rows("K-a") if s.variant != "dated"]
    assert all(_seed(s, "x").occurred_days_ago is None for s in plain)


def test_k_late_keys_sit_in_the_right_turn() -> None:
    for s in _rows("K-late"):
        late = ks.item_of(s).late
        assert s.variant == late.kind
        if late.kind == "fuller":
            assert has_key(late.first, late.early_key) and not has_key(late.second, late.early_key)
        else:
            assert has_key(late.assistant, late.early_key) and has_key(late.second, late.late_key)


def test_content_avoids_the_design_examples_and_taken_domains() -> None:
    """场景不用设计规则章节里顺带出现的例子,也不碰附录 A 的领域(``b300_redesign_facts`` 文件头)。"""
    taboo = ("花生", "过敏", "周会", "松风", "南枝", "养猫", "接孩子", "验证码", "allerg", "peanut")
    text = " ".join(
        " ".join([t.user, t.reply, *(r for _n, r in t.tool_calls)]) for s in K for t in s.turns
    ) + " ".join(x.content for s in K for x in s.seeds)
    assert not [w for w in taboo if w in text.lower()]


def test_k_seed_leaves_the_existing_scenario_digests_alone() -> None:
    """``KSeed`` 是 ``Seed`` 的子类:原有场景的种子还是 ``Seed``,``asdict`` 不多出字段,摘要不变。"""
    old = [*sc.build_all(), *ns.build_redesign()]
    assert all(type(x) is sc.Seed for s in old for x in s.seeds)
    assert "linked_to" not in asdict(next(x for s in old for x in s.seeds))
    assert "linked_to" in asdict(K[0].seeds[0])


def test_gate_takes_ten_units_of_four_classes() -> None:
    """§11.1 第 2 步 ①:K-a、K-b、K-n2、K-c′ 各取 10 个单元(共 40)。"""  # noqa: RUF002
    assert ks.GATE_K_CLASSES == ("K-a", "K-b", "K-n2", "K-cp")
    assert sum(s.cls in ks.GATE_K_CLASSES for s in K) == 40


def test_design_names_map_the_ascii_class_ids() -> None:
    assert set(ks.DESIGN_NAME) == {"K-ap", "K-bp", "K-cp"} <= set(ks.K_CLASSES)
    assert all(c.isascii() for c in ks.K_CLASSES)


# --------------------------------------------------------------------------- 使用侧 B4c / B4d


def test_use_side_adds_two_classes_of_ten() -> None:
    use = ks.build_use()
    assert Counter(s.cls for s in use) == {"B4c": 10, "B4d": 10}
    for cls in ks.USE_CLASSES:
        rows = [s for s in use if s.cls == cls]
        assert Counter(s.domain for s in rows) == dict.fromkeys(sc.DOMAINS, 2)
        assert Counter(s.lang for s in rows) == {"zh": 5, "en": 5}
    assert all(len(s.conversations) == 1 and s.group == "B" for s in use)


def test_b4c_seeds_a_corrected_x_and_a_later_user_stated_y() -> None:
    """审查 M-5:控制台更正过的 X(置信度 1.0)+ 日期更晚的、用户说的 Y,两条都有效。"""
    for s in ks.build_use_class("B4c"):
        x, y = _seed(s, "x"), _seed(s, "y")
        assert x.confidence == 1.0 and (y.origin, y.confidence) == ("user_stated", 0.9)
        assert y.days_ago < x.days_ago and x.superseded_by is None
        assert has_key(x.content, s.x_key) and has_key(y.content, s.y_key)


def test_b4d_seeds_a_summary_holding_x_and_a_later_y_linked_to_it() -> None:
    """用户 11-16:汇总行(含 X)+ 更晚的、用户说的 Y(否认 X),汇总行还没重写。"""
    for s in ks.build_use_class("B4d"):
        summary, y = _seed(s, "summary"), _seed(s, "y")
        assert summary.summary_of == ("a", "x", "c") and has_key(summary.content, s.x_key)
        assert not has_key(summary.content, s.y_key)
        assert y.linked_to == "summary" and y.days_ago < summary.days_ago
        assert all(_seed(s, k).days_ago > summary.days_ago for k in summary.summary_of)


@pytest.mark.parametrize("cls", ks.K_CLASSES)
def test_scenario_ids_are_unique_and_ascii(cls: str) -> None:
    ids = [s.scenario_id for s in _rows(cls)]
    assert len(set(ids)) == 10 and all(i.isascii() for i in ids)
