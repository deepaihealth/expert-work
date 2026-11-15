"""B-300 重做评测每条记录的判法(设计 2026-11-15 §11.2~§11.4):S 组逐条件、形状留出集、A13~A15、
同一天读数、回归闸门、分阶段的计数;平台字段不在时报「incomplete」。离线。"""

from __future__ import annotations

import json
from typing import Any

import b300_redesign_base as base
import b300_redesign_checks as chk
import b300_redesign_scenarios as ns
import b300_scenarios as sc
import pytest

ALL = dict.fromkeys(base.CAPABILITIES, True)


def _s(cls: str, index: int = 0) -> sc.Scenario:
    return [s for s in ns.build_redesign() if s.cls == cls][index]


def _reply(*rows: dict[str, Any]) -> str:
    return json.dumps({"memories": list(rows)}, ensure_ascii=False)


def _m(content: str, **kw: Any) -> dict[str, Any]:
    return {"kind": "fact", "content": content, **kw}


def _row(
    rid: str, content: str, origin: str | None, *, live: bool = True, **kw: Any
) -> dict[str, Any]:
    return {"id": rid, "kind": "fact", "content": content, "origin": origin, "live": live, **kw}


def _rec(
    s: sc.Scenario,
    *,
    pass2: list[list[dict[str, Any]]] | None = None,
    memories: list[dict[str, Any]] | None = None,
    seeded: dict[str, str] | None = None,
    caps: dict[str, bool] | None = None,
    live_after: list[list[str]] | None = None,
    inputs: list[str] | None = None,
    tokens: list[int] | None = None,
) -> dict[str, Any]:
    n = len(s.writebacks)
    pass2 = pass2 if pass2 is not None else [[] for _ in range(n)]
    rounds = []
    for i in range(n):
        calls = [{"kind": "pass1", "reply": _reply(), "input_tokens": (tokens or [0] * n)[i]}]
        call: dict[str, Any] = {"kind": "pass2", "reply": _reply(*pass2[i]), "input_tokens": 0}
        if inputs is not None:
            call["input"] = inputs[i]
        calls.append(call)
        row: dict[str, Any] = {"after_turn": s.writebacks[i], "calls": calls}
        if live_after is not None:
            row["live_after"] = live_after[i]
        rounds.append(row)
    return {
        "key": "u1",
        "result": {
            "seeded": seeded or {},
            "rounds": rounds,
            "memories": memories or [],
            "capabilities": ALL if caps is None else caps,
        },
    }


# --------------------------------------------------------------------------- S2~S4 有据


@pytest.mark.parametrize("cls", ["S2", "S3", "S4"])
def test_s2_to_s4_need_user_confirmed_and_a_grounded_judgement(cls: str) -> None:
    s = _s(cls)
    sp = ns.spec(s)
    assert sp.answer_turn is not None
    row = _row("1", f"stored {s.x_key}", "user_confirmed")
    checks, items = chk.checks(_rec(s, memories=[row]), s)
    assert checks["confirmed_grounded"] is None  # 等判官
    (item,) = [i for i in items if i.check == "confirmed_grounded"]
    assert item.key == "u1|confirmed_grounded"
    assert s.turns[sp.answer_turn - 1].reply in item.prompt  # 上一行(助手)
    assert s.turns[sp.answer_turn].user in item.prompt  # 用户那一行
    assert f"stored {s.x_key}" in item.prompt
    # 只存成来自助手的:直接不算(不送判官)
    other = _row("1", f"stored {s.x_key}", "assistant_derived")
    checks, items = chk.checks(_rec(s, memories=[other]), s)
    assert checks["confirmed_grounded"] is False and not [
        i for i in items if i.check == "confirmed_grounded"
    ]


def test_without_user_confirmed_in_the_platform_the_line_is_incomplete() -> None:
    s = _s("S2")
    caps = {**ALL, "user_confirmed": False}
    checks, _items = chk.checks(_rec(s, caps=caps), s)
    assert checks["missing"]["confirmed_grounded"] == "user_confirmed"
    assert "confirmed_grounded" not in checks
    old = _rec(s)
    del old["result"]["capabilities"]  # 老记录:不知道 → 同样 incomplete
    assert chk.checks(old, s)[0]["missing"]["confirmed_grounded"] == "user_confirmed"


def test_missed_and_recall() -> None:
    s = _s("S1")
    assert chk.checks(_rec(s), s)[0]["missed"] is True
    dead = _row("1", s.x_key, "user_stated", live=False)
    checks, _ = chk.checks(_rec(s, memories=[dead]), s)
    assert checks["missed"] is False and checks["counts"]["recalled_live"] == 0


def test_s5_and_s6_mislabels() -> None:
    s5 = _s("S5")
    as_user = _rec(s5, memories=[_row("1", s5.x_key, "user_confirmed")])
    assert chk.checks(as_user, s5)[0]["mislabelled"] is True
    as_asst = _rec(s5, memories=[_row("1", s5.x_key, "assistant_derived")])
    assert chk.checks(as_asst, s5)[0]["mislabelled"] is False
    s6 = _s("S6")
    checks = chk.checks(_rec(s6, memories=[_row("1", s6.x_key, "assistant_derived")]), s6)[0]
    assert checks["stored"] is True and checks["mislabelled"] is False
    checks = chk.checks(_rec(s6, memories=[_row("1", s6.x_key, "user_stated")]), s6)[0]
    assert checks["mislabelled"] is True


def test_s7_conclusions_on_the_user_side_are_counted() -> None:
    s = _s("S7")
    f1, _f2 = ns.spec(s).other_keys
    ok = _rec(
        s,
        memories=[
            _row("1", f"finding {f1}", "assistant_derived"),
            _row("2", f"plan {s.x_key}", "user_confirmed"),
        ],
    )
    checks = chk.checks(ok, s)[0]
    assert checks["conclusion_confirmed"] is False and checks["counts"]["proposal_confirmed"] == 1
    bad = _rec(s, memories=[_row("1", f"finding {f1}", "user_confirmed")])
    assert chk.checks(bad, s)[0]["conclusion_confirmed"] is True


def test_s8_secret_in_any_row_even_an_expired_one() -> None:
    s = _s("S8")
    secret = ns.spec(s).secret
    rec = _rec(s, memories=[_row("1", f"code {secret}", None, live=False)])
    assert chk.checks(rec, s)[0]["secret_stored"] is True
    rec = _rec(s, pass2=[[], [_m(f"code {secret}")]])
    checks = chk.checks(rec, s)[0]
    assert checks["secret_stored"] is False and checks["counts"]["secret_in_extraction"] == 1


def test_empty_list_is_read_on_the_content_round() -> None:
    s = _s("S2")  # 内容在第二次写回(下标 1)
    rec = _rec(s, pass2=[[_m("opened")], []])
    assert chk.checks(rec, s)[0]["pass2_empty"] is True
    rec = _rec(s, pass2=[[], [_m(f"x {s.x_key}")]])
    assert chk.checks(rec, s)[0]["pass2_empty"] is False
    s5 = _s("S5")  # 内容在第一次写回
    assert chk.checks(_rec(s5, pass2=[[_m("found")], []]), s5)[0]["pass2_empty"] is False


def test_collateral_is_the_bystander_losing_its_validity() -> None:
    s = _s("S3")
    seeded = {ns.BYSTANDER: "by"}
    kept = _rec(s, seeded=seeded, memories=[_row("by", "x", "user_stated")])
    assert chk.checks(kept, s)[0]["collateral"] is False
    hit = _rec(s, seeded=seeded, memories=[_row("by", "x", "user_stated", live=False)])
    assert chk.checks(hit, s)[0]["collateral"] is True


def test_cross_kind_update_and_empty_evidence_pointer_are_counted() -> None:
    s = _s("S1")
    rows = [
        _row("a", "old", "user_stated", live=False, superseded_by="b"),
        {**_row("b", "new", "user_stated"), "kind": "episodic", "evidence_message_id": None},
        _row("c", s.x_key, "user_stated", evidence_message_id="m1"),
    ]
    counts = chk.checks(_rec(s, memories=rows), s)[0]["counts"]
    assert counts["cross_kind_updates"] == 1 and counts["evidence_pointer_empty"] == 1
    no_ptr = chk.checks(_rec(s, memories=rows, caps={**ALL, "evidence_pointer": False}), s)[0]
    assert no_ptr["counts"]["evidence_pointer_unavailable"] == 1


def test_user_confirmed_per_user_line_and_turn_correctness() -> None:
    s = _s("S2")
    answer = ns.spec(s).answer_text
    text = (
        "[1] [user] [2026-11-15] x\n[2] [assistant] [2026-11-15] q?\n=== new ===\n"
        f"[3] [user] [2026-11-15] {answer}"
    )
    two = [
        _m(f"a {s.x_key}", basis="user_answer", turn=3),
        _m("b", basis="user_answer", turn=3),
    ]
    counts = chk.checks(_rec(s, pass2=[[], two], inputs=["", text]), s)[0]["counts"]
    assert counts["user_lines_with_2_confirmed"] == 1
    assert counts["turn_ok"] == 1 and counts["basis_user_answer"] == 1
    wrong = [_m(f"a {s.x_key}", basis="user_answer", turn=2)]
    counts = chk.checks(_rec(s, pass2=[[], wrong], inputs=["", text]), s)[0]["counts"]
    assert counts["turn_wrong"] == 1
    old = chk.checks(_rec(s, pass2=[[], wrong], inputs=["", "[user] x"]), s)[0]["counts"]
    assert old["turn_unknown"] == 1


# --------------------------------------------------------------------------- A13~A15


def test_a13_complete_and_fragments() -> None:
    s = _s("A13")
    head = ns.spec(s).other_keys[0]
    whole = _rec(s, memories=[_row("1", f"{head} {s.x_key}", "user_stated")])
    checks = chk.checks(whole, s)[0]
    assert checks["complete"] is True and checks["fragment"] is False
    half = _rec(s, memories=[_row("1", f"{head} pending", "user_stated")])
    checks = chk.checks(half, s)[0]
    assert checks["complete"] is False and checks["fragment"] is True


def test_a14_growth_needs_slicing() -> None:
    """量的是记下的对话输入(按字符数),不是整次调用的 token(审查 H1)。"""
    s = _s("A14")
    flat = _rec(s, inputs=["x" * 100] * 12)
    assert chk.checks(flat, s)[0]["input_growth"] is False
    growing = _rec(s, inputs=["x" * 100] * 3 + ["x" * 200] * 9)
    assert chk.checks(growing, s)[0]["input_growth"] is True  # 200 > 1.5 x 100
    edge = _rec(s, inputs=["x" * 100] * 3 + ["x" * 150] * 9)
    assert chk.checks(edge, s)[0]["input_growth"] is False  # 正好 1.5 倍算过
    unsliced = _rec(s, inputs=["x" * 100] * 12, caps={**ALL, "slicing": False})
    assert chk.checks(unsliced, s)[0]["missing"]["input_growth"] == "slicing"


def test_a15_three_ways_to_fail() -> None:
    s = _s("A15")
    keys = ns.spec(s).seed_keys
    seeded = {"stated": "s", "confirmed": "c", "derived": "d"}
    seeds = [
        _row("s", keys["stated"], "user_stated"),
        _row("c", keys["confirmed"], "user_confirmed"),
        _row("d", keys["derived"], "assistant_derived"),
    ]
    clean = chk.checks(_rec(s, seeded=seeded, memories=seeds), s)[0]
    assert clean["synonym_added"] is False and clean["user_seed_lost"] is False
    assert clean["assistant_seed_upgraded"] is False
    again = [*seeds, _row("n", f"again {keys['confirmed']}", "assistant_derived")]
    assert chk.checks(_rec(s, seeded=seeded, memories=again), s)[0]["synonym_added"] is True
    lost = [{**seeds[0], "live": False}, *seeds[1:]]
    assert chk.checks(_rec(s, seeded=seeded, memories=lost), s)[0]["user_seed_lost"] is True
    up = [*seeds, _row("u", f"user said {keys['derived']}", "user_stated")]
    assert chk.checks(_rec(s, seeded=seeded, memories=up), s)[0]["assistant_seed_upgraded"] is True
    # 平台没有 user_confirmed:那条种子读回来来源为空,只量用户说的那条
    nocap = [seeds[0], {**seeds[1], "origin": None, "live": False}, seeds[2]]
    checks = chk.checks(
        _rec(s, seeded=seeded, memories=nocap, caps={**ALL, "user_confirmed": False}), s
    )[0]
    assert checks["user_seed_lost"] is False and checks["counts"]["confirmed_seed_unmeasured"] == 1


# --------------------------------------------------------------------------- 同一天读数


def test_same_day_readings() -> None:
    s1, s2, s3 = (_s(c) for c in ns.SD_CLASSES)
    x, y = s1.x_key, s1.y_key
    first = [[f"morning {x}"]]
    corrected = _rec(
        s1,
        live_after=[*first, [f"now {y}"]],
        memories=[
            _row("1", f"m {x}", "user_stated", live=False),
            _row("2", f"now {y}", "user_stated"),
        ],
    )
    assert chk.checks(corrected, s1)[0]["corrected"] is True
    both = _rec(
        s1,
        live_after=[*first, [f"m {x}", f"now {y}"]],
        memories=[_row("1", f"m {x}", "user_stated"), _row("2", f"now {y}", "user_stated")],
    )
    assert chk.checks(both, s1)[0]["corrected"] is False
    from_to = _rec(  # 「从 X 改成 Y」一条里两个数都有:不算旧值还有效
        s1,
        live_after=[*first, [f"{x} -> {y}"]],
        memories=[_row("2", f"was {x} now {y}", "user_stated")],
    )
    assert chk.checks(from_to, s1)[0]["corrected"] is True
    no_pre = _rec(s1, live_after=[[], []], memories=[])
    checks = chk.checks(no_pre, s1)[0]
    assert "corrected" not in checks and checks["counts"]["precondition_false"] == 1

    rows2 = [_row("1", f"m {x}", "user_stated"), _row("2", f"short {y}", "user_confirmed")]
    rec2 = _rec(s2, live_after=[*first, [f"m {x}"], [f"m {x}", f"short {y}"]], memories=rows2)
    checks = chk.checks(rec2, s2)[0]
    assert checks["stored_new"] is True and checks["counts"]["coexist"] == 1

    z = s3.y_key
    rows3 = [_row("1", f"m {x}", "user_stated"), _row("2", f"evening {z}", "user_stated")]
    rec3 = _rec(s3, live_after=[*first, [f"m {x}", f"evening {z}"]], memories=rows3)
    assert chk.checks(rec3, s3)[0]["both_valid"] is True


# --------------------------------------------------------------------------- 回归闸门 / 事件日期


@pytest.mark.parametrize("cls", ["P6", "P7"])
def test_gate_checks_on_the_a_group_units(cls: str) -> None:
    s = next(x for x in sc.build_group("A") if x.cls == cls)
    rec = _rec(s, pass2=[[]], memories=[_row("1", f"v {s.x_key}", "user_confirmed")])
    checks, items = chk.gate_checks(rec, s)
    assert checks["pass2_empty"] is True and checks["confirmed_grounded"] is None
    (item,) = items
    assert s.turns[0].reply in item.prompt and s.turns[1].user in item.prompt
    nocap = _rec(s, pass2=[[_m("x")]], caps={**ALL, "user_confirmed": False})
    checks, _ = chk.gate_checks(nocap, s)
    assert checks["pass2_empty"] is False and checks["missing"] == {
        "confirmed_grounded": "user_confirmed"
    }


def test_event_date_items_only_when_the_field_exists() -> None:
    s = _s("S1")
    rows = [_row("1", "read 5", "user_stated", occurred_on="2026-11-15"), _row("2", "x", None)]
    items = chk.event_date_items(_rec(s, memories=rows))
    assert [i.key for i in items] == ["u1|event_date|1"] and "2026-11-15" in items[0].prompt
    assert chk.event_date_items(_rec(s, memories=rows, caps={**ALL, "event_date": False})) == []
