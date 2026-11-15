"""B-300 重做对 ``b300_report`` 的改动(设计 2026-11-15 §11.4 末「评测脚本要改的」、§11.1 回归闸门、
§11.3 的线):标签表加用户确认;P6 / P7 / P9 的来源读字段、判官只判内容有据;A10 仍只认用户说的、
另断言原来那条被取代;新线;字段不在时报「incomplete」。离线。"""

from __future__ import annotations

import json
from typing import Any

import b300_redesign_base as base
import b300_report as rep
import b300_scenarios as sc
import pytest

ALL = dict.fromkeys(base.CAPABILITIES, True)


def _scenario(cls: str, fact_key: str = "budget") -> sc.Scenario:
    return next(s for s in sc.build_group("A") if s.cls == cls and s.fact_key == fact_key)


def _reply(*contents: str, correction: bool = False) -> str:
    rows = [{"kind": "fact", "content": c, "is_correction": correction} for c in contents]
    return json.dumps({"memories": rows}, ensure_ascii=False)


def _record(
    calls: list[tuple[str, str]],
    memories: list[dict[str, Any]] | None = None,
    *,
    seeded: dict[str, str] | None = None,
    caps: dict[str, bool] | None = None,
) -> dict[str, Any]:
    return {
        "key": "k",
        "result": {
            "seeded": seeded or {},
            "rounds": [{"after_turn": 1, "calls": [{"kind": k, "reply": r} for k, r in calls]}],
            "memories": memories or [],
            "capabilities": ALL if caps is None else caps,
        },
    }


def _row(content: str, origin: str | None, *, live: bool = True, rid: str = "1") -> dict[str, Any]:
    return {"id": rid, "kind": "fact", "content": content, "origin": origin, "live": live}


def test_label_table_has_user_confirmed() -> None:
    assert rep._LABEL["user_confirmed"] == "confirmed by the user"
    seed = sc.Seed("x", "fact", "c", "user_confirmed", 0.8, 3)
    s = sc.Scenario("id", "B", "B4", "support", "number", "en", "budget", "x", "y", seeds=(seed,))
    assert "confirmed by the user" in rep._seed_lines(s)


def test_user_confirmed_is_user_side_for_a12() -> None:
    a12 = _scenario("A12")
    x = sc.fact("budget").texts[a12.lang].x
    rows = [_row(x, "user_confirmed", rid=str(i)) for i in range(2)]
    assert rep.a_checks(_record([("pass1", _reply())], rows), a12)[0]["user_side_duplicate"] is True


@pytest.mark.parametrize("cls", ["P6", "P7"])
def test_p6_p7_read_origin_from_the_field_and_judge_only_the_content(cls: str) -> None:
    s = _scenario(cls)
    x = sc.fact("budget").texts[s.lang].x
    calls = [("pass1", _reply()), ("pass2", _reply(x))]
    checks, items = rep.a_checks(_record(calls, [_row(f"v {x}", "user_confirmed")]), s)
    assert "attributed" not in checks and checks["confirmed_grounded"] is None
    (item,) = items
    assert item.key == "k|confirmed_grounded" and base.GROUNDED_WORDING in item.prompt
    assert s.turns[0].reply in item.prompt and s.turns[1].user in item.prompt
    assert f"v {x}" in item.prompt
    # 内容里写了「用户回答」但来源字段是来自助手:不算(来源只读字段)
    worded = [_row(f"the user answered {x}", "assistant_derived")]
    checks, items = rep.a_checks(_record(calls, worded), s)
    assert checks["confirmed_grounded"] is False and items == []
    assert checks["pass2_empty"] is False
    checks, _ = rep.a_checks(_record([("pass1", _reply()), ("pass2", _reply())]), s)
    assert checks["pass2_empty"] is True


def test_p6_without_the_field_is_incomplete_not_zero() -> None:
    s = _scenario("P6")
    caps = {**ALL, "user_confirmed": False}
    checks, items = rep.a_checks(_record([("pass1", _reply()), ("pass2", _reply())], caps=caps), s)
    assert checks["missing"] == {"confirmed_grounded": "user_confirmed"} and items == []


def test_p9_correction_reads_the_field_and_the_judge_checks_content() -> None:
    s = _scenario("P9")
    y = sc.fact("budget").texts[s.lang].y
    rows = [_row(f"now {y}", "user_confirmed")]
    calls = [("pass1", _reply()), ("pass2", _reply(f"now {y}", correction=True))]
    checks, items = rep.a_checks(_record(calls, rows), s)
    assert checks["confirmed_correction"] is None and checks["y_valid"] is True
    (item,) = items
    assert item.check == "confirmed_correction" and base.GROUNDED_WORDING in item.prompt
    assert s.turns[0].reply in item.prompt and s.turns[1].user in item.prompt
    not_correction = [("pass1", _reply()), ("pass2", _reply(f"now {y}"))]
    assert rep.a_checks(_record(not_correction, rows), s)[0]["confirmed_correction"] is False


def test_a10_still_only_user_stated_and_the_original_is_replaced() -> None:
    s = _scenario("A10")
    x = sc.fact("budget").texts[s.lang].x
    seeded = {"x": "seed"}
    calls = [("pass1", _reply(x))]
    replaced = [  # 审查 M6:被取代 = superseded_by 指向一条有效的 user_stated
        {**_row(x, "assistant_derived", live=False, rid="seed"), "superseded_by": "n"},
        _row(x, "user_stated", rid="n"),
    ]
    checks, _ = rep.a_checks(_record(calls, replaced, seeded=seeded), s)
    assert checks["user_version"] is True and checks["original_superseded"] is True
    confirmed = [_row(x, "assistant_derived", rid="seed"), _row(x, "user_confirmed", rid="n")]
    checks, _ = rep.a_checks(_record(calls, confirmed, seeded=seeded), s)
    assert checks["user_version"] is False and checks["original_superseded"] is False


def _line(cls: str, metric: str) -> rep.PassLine:
    return next(ln for ln in rep.LINES if (ln.cls, ln.metric) == (cls, metric))


def test_redesign_lines_match_the_design() -> None:
    """§11.1 回归闸门、§11.3 的线(用户 11-15 按建议定)。"""
    expect = {
        ("G-P6", "pass2_empty"): ("max", 2, None, 30),
        ("G-P7", "pass2_empty"): ("max", 2, None, 30),
        ("G-P6", "confirmed_grounded"): ("min", 24, None, 30),
        ("G-P7", "confirmed_grounded"): ("min", 24, None, 30),
        ("P6", "confirmed_grounded"): ("min", 24, 4, 30),
        ("P9", "confirmed_correction"): ("min", 24, None, 30),
        ("S2", "confirmed_grounded"): ("min", 24, 4, 30),
        ("S4", "confirmed_grounded"): ("min", 24, 4, 30),
        ("S1", "missed"): ("max", 3, None, 30),
        ("S4", "missed"): ("max", 3, None, 30),
        ("S5", "mislabelled"): ("max", 1, None, 30),
        ("S6", "stored"): ("max", 1, None, 30),
        ("S6", "mislabelled"): ("max", 0, None, 30),
        ("S7", "conclusion_confirmed"): ("max", 1, None, 15),
        ("S8", "secret_stored"): ("max", 0, None, 30),
        ("S5", "pass2_empty"): ("max", 2, None, 30),
        ("A13", "complete"): ("min", 24, None, 30),
        ("A13", "fragment"): ("max", 3, None, 30),
        ("A14", "input_growth"): ("max", 0, None, 6),
        ("A15", "synonym_added"): ("max", 1, None, 15),
        ("A15", "user_seed_lost"): ("max", 0, None, 15),
        ("A15", "assistant_seed_upgraded"): ("max", 0, None, 15),
        ("SD1", "corrected"): ("min", 24, None, 30),
        ("SD2", "stored_new"): ("min", 24, None, 30),
        ("SD3", "both_valid"): ("min", 24, None, 30),
        ("ALL", "collateral"): ("max", 0, None, 0),
        ("ALL", "event_date_not_event"): ("rate_max", 0.10, None, 1),
    }
    for (cls, metric), (op, total, per_domain, n) in expect.items():
        line = _line(cls, metric)
        assert (line.op, line.total, line.per_domain, line.expected_n) == (op, total, per_domain, n)
        assert line.status == rep.APPROVED_REDESIGN, (cls, metric)
    assert not [ln for ln in rep.LINES if ln.metric == "attributed"]


def test_rate_line_without_per_domain_limit_is_judged_on_the_total() -> None:
    line = _line("ALL", "event_date_not_event")
    rows = [("support", True)] + [("legal", False)] * 9  # 1 / 10 = 10%
    assert rep.judge_line(line, rows)["verdict"] == "pass"
    assert rep.judge_line(line, [*rows, ("legal", True)])["verdict"] == "fail"


def test_missing_field_turns_the_verdict_into_incomplete_with_the_reason() -> None:
    line = _line("S2", "confirmed_grounded")
    got = rep.judge_line(line, [], missing={"user_confirmed": 30})
    assert got["verdict"].startswith("incomplete") and "user_confirmed" in got["verdict"]
    report = rep.build_report(
        {},
        counts={},
        meta={},
        c_control_vendors=[],
        all_vendors=["glm"],
        cuts_applied=[],
        baseline={},
        missing={("S2", "confirmed_grounded", "glm"): {"user_confirmed": 30}},
    )
    verdict = report["lines"]["S2.confirmed_grounded"]["glm"]["verdict"]
    assert verdict.startswith("incomplete") and "user_confirmed" in verdict


def test_judge_wording_digest_covers_the_new_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    before = rep.a_judge_wording_digest()
    for name in ("GROUNDED_WORDING", "EVENT_DATE_WORDING"):
        with monkeypatch.context() as m:
            m.setattr(base, name, getattr(base, name) + " x")
            assert rep.a_judge_wording_digest() != before, name


def test_wording_in_prompt_knows_the_new_prompts() -> None:
    assert rep.wording_in_prompt(base.grounded_prompt("a", "b", ["c"]))
    assert rep.wording_in_prompt(base.event_date_prompt("c", "2026-11-15"))
    assert not rep.wording_in_prompt("some old prompt")
