"""B-300 第 9 版 K 组每条记录的读库断言(设计 2026-11-15 §11.2a / §11.3):每类的线读哪几列、
近邻、写的保证、判官题。记录是手拼的,不调模型。"""

from __future__ import annotations

import json
from typing import Any

import b300_k_checks as kchk
import b300_k_scenarios as ks
import b300_scenarios as sc
import pytest

K = ks.build_k()
CAPS = {"retire_split": True, "summary_rewrite": True}
LABELS = {
    "stated in user messages": "user_stated",
    "confirmed by the user": "user_confirmed",
    "from the assistant": "assistant_derived",
}


def _s(cls: str, variant: str | None = None, index: int = 0) -> sc.Scenario:
    rows = [s for s in K if s.cls == cls and (variant is None or s.variant == variant)]
    return rows[index]


def _row(rid: str, content: str, **kw: Any) -> dict[str, Any]:
    return {
        "id": rid,
        "kind": "fact",
        "content": content,
        "origin": "user_stated",
        "confidence": 0.9,
        "live": True,
        "superseded_by": None,
        "supersedes": None,
        "consolidated_from": [],
        "review_flagged_at": None,
        **kw,
    }


def _record(
    s: sc.Scenario,
    new: list[dict[str, Any]] | None = None,
    *,
    retire: dict[str, str | None] | None = None,
    calls: list[dict[str, Any]] | None = None,
    rewrite: dict[str, Any] | None = None,
    caps: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """种子照场景种下;``retire`` = 种子键 → 取代它的那一行的 id(``None`` = 只是作废)。"""
    rows, seeded = [], {}
    for seed in s.seeds:
        rid = f"seed-{seed.key}"
        seeded[seed.key] = rid
        members = [f"seed-{k}" for k in getattr(seed, "summary_of", ())]
        rows.append(
            _row(
                rid,
                seed.content,
                origin=None if members else seed.origin,
                confidence=seed.confidence,
                consolidated_from=members,
            )
        )
    for key, successor in (retire or {}).items():
        row = next(r for r in rows if r["id"] == seeded[key])
        row.update(live=False, superseded_by=successor)
    rnd: dict[str, Any] = {"calls": calls or []}
    if rewrite is not None:
        rnd["rewrite"] = rewrite
    result = {
        "seeded": seeded,
        "memories": [*rows, *(new or [])],
        "rounds": [rnd],
        "capabilities": CAPS if caps is None else caps,
        "origin_labels": LABELS,
    }
    return {"key": "u1", "result": result}


def _check(s: sc.Scenario, *args: Any, **kw: Any) -> dict[str, Any]:
    return kchk.checks(_record(s, *args, **kw), s)[0]


def _new(s: sc.Scenario, key_text: str, rid: str = "n1", **kw: Any) -> dict[str, Any]:
    return _row(rid, f"note {key_text}", **kw)


def _y(s: sc.Scenario) -> str:
    return ks.item_of(s).y.memory


def _z(s: sc.Scenario) -> str:
    return ks.item_of(s).z.memory


# --------------------------------------------------------------------------- 逐类


def test_k_a_needs_the_old_row_retired_and_linked_to_the_new_valid_row() -> None:
    """K-a:新值有效且旧值作废、``superseded_by`` 连到新值;新值没存下 = 漏记。"""
    s = _s("K-a")
    good = _check(s, [_row("n1", _y(s))], retire={"x": "n1"})
    assert good["retired_to_new"] is True and good["missed"] is False
    assert _check(s, [_row("n1", _y(s))])["retired_to_new"] is False  # 旧值仍有效
    assert _check(s, [_row("n1", _y(s))], retire={"x": None})["retired_to_new"] is False  # 没连线
    lost = _check(s, [_row("n1", _y(s), live=False)], retire={"x": "n1"})
    assert lost["retired_to_new"] is False  # 连到一条已作废的行不算
    assert _check(s, [])["missed"] is True
    assert _check(s, [], retire={"x": "seed-near1"})["retired_to_new"] is False  # 连到近邻不算


def test_k_a_dated_scenarios_stay_out_of_the_line_and_are_only_counted() -> None:
    """用户 11-16:带事件日期的 2 个场景不进 K-a 的线,单独报数、不设线。"""
    s = _s("K-a", "dated")
    coexist = _check(s, [_row("n1", _y(s))])
    assert "retired_to_new" not in coexist and "missed" not in coexist
    counts = coexist["counts"]
    assert (counts["dated_units"], counts["dated_coexists"], counts["dated_retired"]) == (1, 1, 0)
    done = _check(s, [_row("n1", _y(s))], retire={"x": "n1"})["counts"]
    assert (done["dated_retired"], done["dated_retired_to_new"]) == (1, 1)
    assert _check(s, [])["counts"]["dated_missed"] == 1
    plain = _check(_s("K-a", ""), [_row("n1", _y(s))])
    assert "dated_units" not in plain["counts"] and plain["retired_to_new"] is False
    assert "neighbour_retired" in coexist  # 近邻照样量


def test_k_a_prime_only_lines_the_new_value_and_counts_the_rest() -> None:
    """K-a′(默认规则):新值有效进线;旧值仍有效、新行连着旧行只报数。"""  # noqa: RUF002
    s = _s("K-ap")
    out = _check(s, [_row("n1", _y(s), origin="user_confirmed", supersedes="seed-x")])
    assert out["new_valid"] is True
    assert (out["counts"]["x_still_valid"], out["counts"]["linked_to_x"]) == (1, 1)
    assert "x_retired" not in {k for k, v in out.items() if isinstance(v, bool)}
    assert _check(s, [])["new_valid"] is False
    assert _check(s, [_row("n1", _y(s))])["counts"]["linked_to_x"] == 0


@pytest.mark.parametrize("cls", ["K-b", "K-b2"])
def test_k_b_keeps_x_and_asks_the_judge_whether_the_doubt_stayed(cls: str) -> None:
    s = _s(cls)
    record = _record(s, [_row("n1", ks.item_of(s).hedge_memory)])
    out, items = kchk.checks(record, s)
    assert out["x_still_valid"] is True and out["hedge_kept"] is None
    assert [i.key for i in items] == ["u1|hedge_kept"]
    assert kchk.HEDGE_WORDING in items[0].prompt and ks.item_of(s).hedge_memory in items[0].prompt
    assert s.turns[-1].user in items[0].prompt
    lost, none = kchk.checks(_record(s, [], retire={"x": None}), s)
    assert lost["x_still_valid"] is False and lost["hedge_kept"] is False and none == []


def test_k_b2_shows_the_judge_the_question_and_the_short_answer() -> None:
    s = _s("K-b2")
    _out, items = kchk.checks(_record(s, [_row("n1", _z(s), origin="user_confirmed")]), s)
    assert f"[assistant] {s.turns[0].reply}" in items[0].prompt
    assert f"[user] {s.turns[1].user}" in items[0].prompt


def test_k_b_prime_wants_the_settled_value_stored_and_x_retired() -> None:
    s = _s("K-bp")
    assert _check(s, [_row("n1", _z(s))], retire={"x": "n1"})["settled"] is True
    assert _check(s, [_row("n1", _z(s))])["settled"] is False
    assert _check(s, [], retire={"x": None})["settled"] is False  # 只有种下的那句犹豫的,不算
    both = _check(s, [_row("n1", _z(s))], retire={"x": "n1", "hedge": "n1"})
    assert both["counts"]["hedge_retired"] == 1


def test_k_c_never_retires_x_and_never_labels_the_finding_user_side() -> None:
    s = _s("K-c")
    ok = _check(s, [_row("n1", _y(s), origin="assistant_derived")])
    assert ok["x_retired"] is False and ok["mislabelled"] is False
    assert ok["counts"]["assistant_rows"] == 1
    assert _check(s, [], retire={"x": None})["x_retired"] is True
    assert _check(s, [_row("n1", _y(s), origin="user_confirmed")])["mislabelled"] is True


def test_k_c_prime_reports_the_two_seed_origins_apart() -> None:
    for variant in ks.HALVES["K-cp"]:
        s = _s("K-cp", variant)
        out = _check(s, [_row("n1", _y(s), origin="user_confirmed")], retire={"x": "n1"})
        assert out["x_retired"] is True
        assert out["counts"][f"x_retired_{variant}"] == 1 and out["counts"][f"units_{variant}"] == 1
        assert out["counts"]["conclusion_confirmed"] == 1
        assert _check(s, [])["x_retired"] is False


@pytest.mark.parametrize("cls", ["K-n1", "K-n2"])
def test_k_n_never_retires_x_and_reports_each_shape(cls: str) -> None:
    for shape in ks.N_SHAPES:
        s = _s(cls, shape)
        bad = _check(s, [_row("n1", _z(s))], retire={"x": "n1"})
        assert bad["x_retired"] is True and bad["counts"][f"x_retired_{shape}"] == 1
        assert bad["counts"][f"user_side_stored_{shape}"] == 1
        fine = _check(s, [_row("n1", _z(s), origin="assistant_derived")])
        assert fine["x_retired"] is False and fine["counts"]["user_side_stored"] == 0


def test_k_e1_retires_x_onto_the_seeded_y_without_a_new_row() -> None:
    s = _s("K-e1")
    good = _check(s, [], retire={"x": "seed-y"})
    assert good["retired_to_new"] is True and good["duplicate_added"] is False
    assert _check(s, [])["retired_to_new"] is False
    dup = _check(s, [_row("n1", _y(s) + " ")], retire={"x": "n1"})
    assert dup["duplicate_added"] is True and dup["retired_to_new"] is False  # 没连到原来的 Y


@pytest.mark.parametrize("cls", ["K-e2", "K-w"])
def test_k_e2_and_k_w_want_a_valid_user_stated_y(cls: str) -> None:
    """逐字相同的一半与换说法的一半各自一条线;X 作废另一条;弱的那条 Y 作废只报数。"""
    for variant in ("verbatim", "reworded"):
        s = _s(cls, variant)
        good = _check(s, [_row("n1", "said " + _y(s))], retire={"x": "n1", "y": "n1"})
        assert good[f"user_stated_y_{variant}"] is True and good["x_retired_ok"] is True
        assert good["counts"]["weaker_y_retired"] == 1
        assert good["counts"]["weaker_y_coexists_linked"] == 0
        side = _check(s, [_row("n1", "said " + _y(s), supersedes="seed-y")], retire={"x": "n1"})
        assert side[f"user_stated_y_{variant}"] is True  # 并存连线:线照样过
        assert side["counts"]["weaker_y_coexists_linked"] == 1
        assert side["counts"]["weaker_y_retired"] == 0
        loose = _check(s, [_row("n1", "said " + _y(s))], retire={"x": "n1"})
        assert loose["counts"]["weaker_y_coexists_linked"] == 0  # 连线记在 X 上时
        assert loose["counts"]["weaker_y_coexists"] == side["counts"]["weaker_y_coexists"] == 1
        assert good["counts"]["weaker_y_coexists"] == 0
        other = "reworded" if variant == "verbatim" else "verbatim"
        assert f"user_stated_y_{other}" not in good
        swallowed = _check(s, [])  # 用户的话被弱的那条吞掉:库里只有种下的用户确认 / 来自助手
        assert swallowed[f"user_stated_y_{variant}"] is False and swallowed["x_retired_ok"] is False
        weak = _check(s, [_row("n1", "said " + _y(s), origin="user_confirmed")])
        assert weak[f"user_stated_y_{variant}"] is False


def test_verbatim_half_reports_whether_the_extraction_really_matched() -> None:
    s = _s("K-w", "verbatim")
    reply = json.dumps({"memories": [{"kind": "fact", "content": _y(s)}]}, ensure_ascii=False)
    hit = _check(s, [], calls=[{"kind": "pass1", "reply": reply}])
    assert hit["counts"]["verbatim_extracted"] == 1
    other = json.dumps({"memories": [{"kind": "fact", "content": _y(s) + " x"}]})
    assert (
        _check(s, [], calls=[{"kind": "pass1", "reply": other}])["counts"].get(
            "verbatim_extracted", 0
        )
        == 0
    )


def test_k_f_stores_the_statement_and_links_x_to_it() -> None:
    s = _s("K-f")
    said = _row("n1", "the user says it is gone")
    good = _check(s, [said], retire={"x": "n1"})
    assert good["retired_to_new"] is True and good["stored_new"] is True
    assert _check(s, [], retire={"x": None})["retired_to_new"] is False  # 只是过期、没有连线
    assert _check(s, [said], retire={"x": "seed-near1"})["retired_to_new"] is False  # 连到近邻不算
    assert _check(s, [])["stored_new"] is False
    assistant = _check(s, [_row("n1", "gone", origin="assistant_derived")])
    assert assistant["stored_new"] is False


def test_k_g_wants_at_least_one_of_the_two_rows_retired() -> None:
    s = _s("K-g")
    assert _check(s, [])["any_retired"] is False
    one = _check(s, [], retire={"x2": None})
    assert one["any_retired"] is True and one["counts"]["both_retired"] == 0
    assert _check(s, [], retire={"x": None, "x2": None})["counts"]["both_retired"] == 1


def test_k_p_only_asks_the_judge_when_the_compound_row_was_retired() -> None:
    """审查 H-3:那条复合的记忆没被退 → 没被否认的那一半还在(读库就够);被退了才问判官。"""
    s = _s("K-p")
    c = ks.item_of(s).compound
    kept, none = kchk.checks(_record(s, [_row("n1", c.deny_b)]), s)
    assert kept["half_kept"] is True and kept["stored_new"] is True and none == []
    out, items = kchk.checks(_record(s, [_row("n1", c.deny_b)], retire={"x": "n1"}), s)
    assert out["half_kept"] is None and out["counts"]["compound_retired"] == 1
    assert [i.key for i in items] == ["u1|half_kept"]
    prompt = items[0].prompt
    assert kchk.HALF_WORDING in prompt and c.memory in prompt and c.deny_b in prompt
    assert f"- {c.deny_b}" in prompt and f"- {c.memory}" not in prompt  # 只列有效行


def test_k_t_never_retires_the_corrected_row() -> None:
    s = _s("K-t")
    ok = _check(s, [_row("n1", _y(s))])
    assert ok["x_retired"] is False and ok["stored_new"] is True
    bad = _check(s, [], retire={"x": None})
    assert bad["x_retired"] is True and bad["stored_new"] is False


def test_k_late_fuller_half_is_settled_by_the_key_or_sent_to_the_judge() -> None:
    s = _s("K-late", "fuller")
    late = ks.item_of(s).late
    kept, none = kchk.checks(_record(s, [_row("n1", f"deliver to {late.first}")]), s)
    assert kept["fuller_kept"] is True and none == []
    out, items = kchk.checks(_record(s, [_row("n1", late.second)]), s)
    assert out["fuller_kept"] is None and [i.key for i in items] == ["u1|fuller_kept"]
    assert kchk.FULLER_WORDING in items[0].prompt and late.first in items[0].prompt
    history = [_row("n1", f"deliver to {late.first}", live=False, superseded_by="n2")]
    gone, asked = kchk.checks(_record(s, [*history, _row("n2", late.second)]), s)
    assert gone["fuller_kept"] is None and len(asked) == 1  # 写成历史行 = 不在有效行里


def test_k_late_switch_half_wants_the_later_value_valid_and_counts_the_earlier() -> None:
    s = _s("K-late", "switch")
    late = ks.item_of(s).late
    early = late.early_key.split("|")[0]
    later = late.late_key.split("|")[0]
    hist = _check(
        s, [_row("n1", f"set {early}", live=False, superseded_by="n2"), _row("n2", f"set {later}")]
    )
    assert hist["later_valid"] is True and hist["counts"]["earlier_history"] == 1
    both = _check(s, [_row("n1", f"set {early}"), _row("n2", f"set {later}")])
    assert both["counts"]["earlier_coexists"] == 1 and both["counts"]["earlier_history"] == 0
    assert _check(s, [_row("n1", f"set {early}")])["later_valid"] is False
    assert _check(s, [_row("n2", f"set {later}")])["counts"]["earlier_not_stored"] == 1


# --------------------------------------------------------------------------- K-s 汇总行


def _before(live: bool = True, flagged: bool = False) -> dict[str, Any]:
    return {
        "ran": True,
        "outcomes": [],
        "before": {"summary_live": live, "summary_flagged": flagged},
    }


def test_k_s_denial_never_retires_the_summary_before_the_rewrite() -> None:
    s = _s("K-s", "deny")
    said = _row("n1", ks.item_of(s).trio.deny_b, supersedes="seed-summary")
    ok = _check(s, [said], rewrite=_before(flagged=True))
    assert ok["summary_retired_directly"] is False and ok["new_stored"] is True
    assert ok["counts"]["marked"] == 1 and ok["counts"]["rewrite_not_run"] == 1
    direct = _check(s, [said], retire={"summary": "n1"}, rewrite=_before(live=False))
    assert direct["summary_retired_directly"] is True
    assert _check(s, [], rewrite=_before())["new_stored"] is False


def test_k_s_denial_sends_the_current_summary_to_the_judge_twice() -> None:
    s = _s("K-s", "deny")
    trio = ks.item_of(s).trio
    said = _row("n1", trio.deny_b)
    fresh = _row("n2", "rewritten summary", origin=None, consolidated_from=["seed-a", "seed-c"])
    rewrite = {**_before(flagged=True), "outcomes": ["rewritten"]}
    out, items = kchk.checks(
        _record(s, [said, fresh], retire={"summary": "n2"}, rewrite=rewrite), s
    )
    assert out["b_still_in_summary"] is None and out["ac_lost"] is None
    assert out["counts"]["rewrite_rewritten"] == 1 and out["counts"]["rewrite_not_run"] == 0
    assert [i.key for i in items] == ["u1|b_still_in_summary", "u1|ac_lost"]
    for item in items:
        assert kchk.SUMMARY_WORDING in item.prompt and "- rewritten summary" in item.prompt
        assert trio.summary in item.prompt and trio.a.memory in item.prompt
    assert items[0].prompt != items[1].prompt


def test_k_s_denial_with_no_summary_left_lost_a_and_c() -> None:
    s = _s("K-s", "deny")
    rewrite = {**_before(flagged=True), "outcomes": ["emptied"]}
    out, items = kchk.checks(
        _record(s, [_row("n1", "said")], retire={"summary": "n1"}, rewrite=rewrite), s
    )
    assert (out["b_still_in_summary"], out["ac_lost"], items) == (False, True, [])


def test_k_s_rewrite_lines_are_incomplete_without_the_rewrite_entry() -> None:
    s = _s("K-s", "deny")
    out, items = kchk.checks(
        _record(s, [], rewrite=_before(), caps={"retire_split": True, "summary_rewrite": False}), s
    )
    assert out["missing"]["b_still_in_summary"] == out["missing"]["ac_lost"] == "summary_rewrite"
    assert "b_still_in_summary" not in out and items == []
    assert out["summary_retired_directly"] is False  # 这一条不靠重写,照量


def test_k_s_restatement_must_not_mark_or_change_the_summary() -> None:
    """第三轮审查 M-A:把汇总过的事再说一遍 → 不打标、汇总行不动;多出一行同义的另计。"""
    s = _s("K-s", "restate")
    quiet = _check(s, [], rewrite=_before())
    assert (quiet["rewrite_marked"], quiet["summary_changed"], quiet["synonym_added"]) == (
        False,
        False,
        False,
    )
    assert _check(s, [], rewrite=_before(flagged=True))["rewrite_marked"] is True
    assert _check(s, [], retire={"summary": None}, rewrite=_before())["summary_changed"] is True
    assert _check(s, [_row("n1", "again")], rewrite=_before())["synonym_added"] is True
    assert "summary_retired_directly" not in quiet and "new_stored" not in quiet


# --------------------------------------------------------------------------- 近邻


def _reconcile(
    index: int, content: str, existing: list[str], decision: dict[str, Any]
) -> dict[str, Any]:
    material = {
        "candidates": [
            {
                "index": index,
                "content": content,
                "source": "stated in user messages",
                "existing": [{"id": f"e{n}", "content": text} for n, text in enumerate(existing)],
            }
        ]
    }
    reply = {"decisions": [{"index": index, **decision}]}
    return {
        "kind": "reconcile",
        "input": json.dumps(material, ensure_ascii=False),
        "reply": json.dumps(reply, ensure_ascii=False),
    }


def test_a_retired_neighbour_is_bad_for_every_class_but_k_late() -> None:
    """审查 M-2:近邻旁观记忆被作废 = 坏(读库,不靠判官)。"""
    s = _s("K-a")
    assert _check(s, [])["neighbour_retired"] is False
    hit = _check(s, [], retire={"near2": None})
    assert hit["neighbour_retired"] is True and hit["counts"]["neighbours_seeded"] == 2
    assert "neighbour_retired" not in _check(_s("K-late", "switch"), [])


def test_listed_but_still_valid_rows_are_counted_and_split_by_neighbour() -> None:
    """「模型列了、被程序挡下」= 回复里列了、那一行写回后仍有效;其中落在近邻上的另数。"""
    s = _s("K-a")
    x, near = s.seeds[0].content, s.seeds[1].content
    call = _reconcile(0, _y(s), [x, near], {"op": "ADD", "replaces": ["e0", "e1", "e9"]})
    blocked = _check(s, [_row("n1", _y(s))], calls=[call])["counts"]
    assert (blocked["listed"], blocked["listed_blocked"]) == (2, 2)  # e9 不认识:丢掉
    assert blocked["listed_blocked_on_neighbour"] == 1
    done = _check(s, [_row("n1", _y(s))], retire={"x": "n1"}, calls=[call])["counts"]
    assert (done["listed_blocked"], done["listed_blocked_on_neighbour"]) == (1, 1)


def test_legacy_ops_are_counted() -> None:
    s = _s("K-a")
    call = _reconcile(0, _y(s), [s.seeds[0].content], {"op": "UPDATE", "target_id": "e0"})
    assert _check(s, [], calls=[call])["counts"]["legacy_op"] == 1
    fine = _reconcile(0, _y(s), [s.seeds[0].content], {"op": "ADD"})
    assert _check(s, [], calls=[fine])["counts"]["legacy_op"] == 0


# --------------------------------------------------------------------------- 写的保证


def _guarantee(
    s: sc.Scenario, new: list[dict[str, Any]], decision: dict[str, Any], **kw: Any
) -> Any:
    existing = [x.content for x in s.seeds]
    call = _reconcile(0, _y(s), existing, decision)
    return _check(s, new, calls=[call], **kw)


def test_write_guarantee_holds_when_the_candidate_is_a_valid_row() -> None:
    s = _s("K-a")
    out = _guarantee(s, [_row("n1", _y(s))], {"op": "ADD"})
    assert out["write_guarantee_broken"] is False and out["counts"]["guarantee_candidates"] == 1


def test_write_guarantee_breaks_when_a_user_side_candidate_is_nowhere() -> None:
    """§7.3:候选既不是有效行、也没有层级不低于它的有效行说着同样的事、也不是历史行 = 代码错。"""
    s = _s("K-a")
    out = _guarantee(s, [], {"op": "ADD"})
    assert out["write_guarantee_broken"] is True
    assert out["counts"]["guarantee_broken_candidates"] == 1


def test_write_guarantee_rejects_a_duplicate_judged_against_a_weaker_row() -> None:
    """审查 H-1:模型判它与一条来自助手的行重复 → 用户的话没写 = 不成立;判到同层的行才成立。"""
    s = _s("K-w", "reworded")
    existing = [x.content for x in s.seeds]
    y_at = next(n for n, x in enumerate(s.seeds) if x.key == "y")
    noop = {"op": "NOOP", "target_id": f"e{y_at}"}
    call = _reconcile(0, "the user says Y in other words", existing, noop)
    assert _check(s, [], calls=[call])["write_guarantee_broken"] is True
    e1 = _s("K-e1")
    existing = [x.content for x in e1.seeds]
    y_at = next(n for n, x in enumerate(e1.seeds) if x.key == "y")
    call = _reconcile(0, "Y said again", existing, {"op": "NOOP", "target_id": f"e{y_at}"})
    assert _check(e1, [], calls=[call])["write_guarantee_broken"] is False


def test_write_guarantee_accepts_identical_content_only_at_the_same_or_higher_tier() -> None:
    s = _s("K-a")
    same_weaker = _row("n1", _y(s), origin="assistant_derived")
    assert _guarantee(s, [same_weaker], {"op": "ADD"})["write_guarantee_broken"] is True
    corrected = _row("n1", _y(s), origin=None, confidence=1.0)
    assert _guarantee(s, [corrected], {"op": "ADD"})["write_guarantee_broken"] is False


def test_write_guarantee_accepts_a_history_row_linked_to_a_valid_row() -> None:
    s = _s("K-a")
    history = _row("n1", _y(s), live=False, superseded_by="n2")
    assert (
        _guarantee(s, [history, _row("n2", "later")], {"op": "ADD"})["write_guarantee_broken"]
        is False
    )
    dangling = _row("n1", _y(s), live=False, superseded_by="n2")
    dead = _row("n2", "later", live=False)
    assert _guarantee(s, [dangling, dead], {"op": "ADD"})["write_guarantee_broken"] is True


def test_write_guarantee_skips_assistant_candidates_and_old_code() -> None:
    s = _s("K-a")
    call = _reconcile(0, _y(s), [], {"op": "ADD"})
    call["input"] = call["input"].replace("stated in user messages", "from the assistant")
    out = _check(s, [], calls=[call])
    assert out["write_guarantee_broken"] is False and "guarantee_candidates" not in out["counts"]
    old = _check(s, [], calls=[call], caps={"retire_split": False, "summary_rewrite": False})
    assert "write_guarantee_broken" not in old
    assert old["missing"]["write_guarantee_broken"] == "retire_split"


def test_summary_rows_take_the_tier_of_their_members() -> None:
    """§6.3:汇总行按成员算层级 —— 成员里有用户说的,它就不比用户说的候选弱。"""
    s = _s("K-s", "restate")
    record = _record(s, [])
    rows = kchk.Rows(record["result"])
    assert rows.tier(rows.seed("summary")) == 2 and rows.seed("summary")["origin"] is None
    existing = [x.content for x in s.seeds]
    at = next(n for n, x in enumerate(s.seeds) if x.key == "summary")
    call = _reconcile(0, "B said again", existing, {"op": "NOOP", "target_id": f"e{at}"})
    out = _check(s, [], calls=[call], rewrite=_before())
    assert out["write_guarantee_broken"] is False


# --------------------------------------------------------------------------- 闸门与边角


def test_gate_checks_drop_the_judge_items() -> None:
    """闸门的 K 段只看读库的线:K-b 的「犹豫是否留着」不送判官。"""
    s = _s("K-b")
    out, items = kchk.gate_checks(_record(s, [_row("n1", ks.item_of(s).hedge_memory)]), s)
    assert items == [] and "hedge_kept" not in out and out["x_still_valid"] is True


def test_checks_refuse_other_classes() -> None:
    other = sc.build_group("A")[0]
    with pytest.raises(ValueError, match="not a K class"):
        kchk.checks(_record(_s("K-a")), other)


@pytest.mark.parametrize("cls", ks.K_CLASSES)
def test_every_class_yields_only_known_metrics(cls: str) -> None:
    """每类产出的指标都有线(或是跨类合计的);不会有指标悄悄没人看。"""
    import b300_k_report as krep

    lined = {(c, m) for c, m, *_rest in krep.K_LINES}
    for s in (x for x in K if x.cls == cls):
        out = _check(s, [_row("n1", "anything")], rewrite=_before())
        metrics = {k for k in out if k not in kchk.INFO_KEYS} | set(out["missing"])
        for metric in metrics:
            target = kchk.AGGREGATE.get(metric, cls)
            assert (target, metric) in lined, (cls, metric)


# --------------------------------------------------------------------------- K-s:卡在哪一步


def _summary_call(s: sc.Scenario, listed: bool) -> dict[str, Any]:
    existing = [x.content for x in s.seeds]
    at = next(n for n, x in enumerate(s.seeds) if x.key == "summary")
    return _reconcile(
        0, "denial", existing, {"op": "ADD", "replaces": [f"e{at}"] if listed else []}
    )


def test_k_s_denial_tells_where_each_unit_stopped() -> None:
    """用户 11-16:K-s 的不达标要分得出「被名次等规则挡下」与「重写本身错」。"""
    s = _s("K-s", "deny")
    said = _row("n1", "denial")

    def cause(listed: bool, flagged: bool, outcomes: list[str]) -> str:
        rewrite = {**_before(flagged=flagged), "outcomes": outcomes}
        out = _check(s, [said], calls=[_summary_call(s, listed)], rewrite=rewrite)
        assert out["counts"][f"deny_{out['info']['cause']}"] == 1
        return str(out["info"]["cause"])

    assert cause(False, False, []) == "not_listed"
    assert cause(True, False, []) == "listed_blocked"  # 模型列了、程序没让打标(名次等)
    assert cause(True, True, []) == "marked_not_rewritten"
    assert cause(True, True, ["failed"]) == "marked_not_rewritten"
    assert cause(True, True, ["unchanged"]) == "marked_not_rewritten"
    assert cause(True, True, ["rewritten"]) == "rewritten"
    assert cause(True, True, ["emptied"]) == "rewritten"
    assert {cause(True, True, ["rewritten"]), "not_listed", "listed_blocked"} <= set(
        kchk.SUMMARY_CAUSES
    )
    assert "info" not in _check(_s("K-s", "restate"), [], rewrite=_before())


# --------------------------------------------------------------------------- K-s 撞上成员原文


def test_k_s_restatement_matching_a_member_verbatim_is_counted_apart() -> None:
    """用户 11-16:新行与某条成员原文归一后相同、那条成员被作废并连到新行 → 单独计数,
    不算同义多行。"""
    s = _s("K-s", "restate")
    member = ks.item_of(s).trio.b.memory
    same = _row("n1", f"  {member.upper()}  ")  # 归一后相同:大小写、首尾空白
    out = _check(s, [same], retire={"b": "n1"}, rewrite=_before())
    assert out["synonym_added"] is False and out["counts"]["restate_member_verbatim"] == 1
    trailing = _row("n1", member.rstrip("。.") + "。")
    assert _check(s, [trailing], retire={"b": "n1"}, rewrite=_before())["synonym_added"] is False


def test_k_s_restatement_counts_only_when_both_conditions_hold() -> None:
    s = _s("K-s", "restate")
    member = ks.item_of(s).trio.b.memory
    # 内容相同,但成员没被作废(没撞上):照样是同义多行
    kept = _check(s, [_row("n1", member)], rewrite=_before())
    assert kept["synonym_added"] is True and kept["counts"].get("restate_member_verbatim", 0) == 0
    # 成员作废了,但连到的不是这一行
    other = _check(s, [_row("n1", member)], retire={"b": "elsewhere"}, rewrite=_before())
    assert other["synonym_added"] is True
    # 成员连到这一行,但内容不同(句中改了一个字)
    changed = _check(s, [_row("n1", member + " again")], retire={"b": "n1"}, rewrite=_before())
    assert changed["synonym_added"] is True
    # 撞上成员的一行与另一条同义行同时出现:另一条照样算
    both = _check(
        s, [_row("n1", member), _row("n2", "said again")], retire={"b": "n1"}, rewrite=_before()
    )
    assert both["synonym_added"] is True and both["counts"]["restate_member_verbatim"] == 1


def test_content_norm_matches_the_platform_rule() -> None:
    """与平台 ``normalized_memory_text`` 同一口径(同一组输入逐个比)。"""
    from orchestrator.graph_builder.memory import normalized_memory_text

    wide = "".join(chr(ord(c) + 0xFEE0) for c in "Full width!")  # 全角字母与感叹号
    samples = [
        f"{wide}\u3002",
        "abc!!",
        "x;y;",
        "用户 的 文具\u3002\u3002",
        "H\u2082O \u2460",  # 下标、带圈数字:不折
        "Hello?",
        "  tabs\tand  spaces ",
        "x\u2026",
        f"{wide}\uff1b",
    ]
    for text in samples:
        assert kchk._content_norm(text) == normalized_memory_text(text), text
