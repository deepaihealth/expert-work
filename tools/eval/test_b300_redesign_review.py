"""独立审查(PR E)的发现:每条一个先红后绿的测试。离线、不花钱。

H1 A14 只量对话那一段;M1 限流重试不绕过额度闸;M2 A13 没有切片报 incomplete;M3 关键串的多种
写法、千分位、秘密值去符号、日期片段、读数不落在 1~31;M4 A13 前半句的关键串不出现在后半句;
M5 每段的判官票数、报价给 1 票与 3 票两个价、报告写明票数、校准要覆盖这一段要判的每一种题;
M6 A10 的「被取代」要指向一条有效的用户说的,并进线;另有几条 LOW。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import b300_eval as cli
import b300_live as live
import b300_phases as phases
import b300_plan as plan
import b300_redesign_base as base
import b300_redesign_checks as chk
import b300_redesign_facts as rf
import b300_redesign_facts_x as rfx
import b300_redesign_heldout as rh
import b300_redesign_scenarios as ns
import b300_report as rep
import b300_scenarios as sc
import b300_spend as spend
import httpx
import pytest
from b301_plan import Price

ALL = dict.fromkeys(base.CAPABILITIES, True)
GLM = ["--vendor", "glm=glm-5.3:glm-5.3-flash", "--single-vendor", "glm"]


def _s(cls: str, index: int = 0) -> sc.Scenario:
    return [s for s in ns.build_redesign() if s.cls == cls][index]


def _row(
    rid: str, content: str, origin: str | None, *, live_: bool = True, **kw: Any
) -> dict[str, Any]:
    return {"id": rid, "kind": "fact", "content": content, "origin": origin, "live": live_, **kw}


def _rec(
    s: sc.Scenario,
    *,
    memories: list[dict[str, Any]] | None = None,
    caps: dict[str, bool] | None = None,
    inputs: list[str] | None = None,
    tokens: list[int] | None = None,
    seeded: dict[str, str] | None = None,
) -> dict[str, Any]:
    rounds = []
    for i, turn in enumerate(s.writebacks):
        call: dict[str, Any] = {
            "kind": "pass2",
            "reply": '{"memories": []}',
            "input_tokens": (tokens or [0] * len(s.writebacks))[i],
        }
        if inputs is not None:
            call["input"] = inputs[i]
        rounds.append({"after_turn": turn, "calls": [call]})
    return {
        "key": "u1",
        "result": {
            "seeded": seeded or {},
            "rounds": rounds,
            "memories": memories or [],
            "capabilities": ALL if caps is None else caps,
        },
    }


def _quote(tmp_path: Path, *extra: str) -> dict[str, Any]:
    out: list[str] = []
    code = cli.main(
        ["quote", "--accept-documented-prices", "--state-dir", str(tmp_path / "state"), *extra],
        emit=out.append,
    )
    assert code == 0, out
    body: dict[str, Any] = json.loads(out[0])
    return body


# --------------------------------------------------------------------------- H1


def test_a14_unsliced_growth_fails_even_though_instructions_dominate_the_tokens() -> None:
    """审查 H1:3,700 的固定指令 + 每轮 75 的对话增长,不切片时整段 token 只涨约 1.2 倍;只量对话
    那一段(记下的非系统输入)才看得出增长。"""
    s = _s("A14")
    rounds = len(s.writebacks)
    tokens = [3_700 + 75 * k for k in range(rounds)]
    unsliced = ["line\n" * 15 * (k + 1) for k in range(rounds)]  # 对话一轮比一轮长
    checks = chk.checks(_rec(s, tokens=tokens, inputs=unsliced), s)[0]
    assert checks["input_growth"] is True
    sliced = ["line\n" * 15] * rounds  # 每次只给本轮 + 几行上下文
    assert chk.checks(_rec(s, tokens=tokens, inputs=sliced), s)[0]["input_growth"] is False


def test_a14_without_recorded_input_is_counted_not_judged() -> None:
    s = _s("A14")
    checks = chk.checks(_rec(s, tokens=[100] * 12), s)[0]
    assert "input_growth" not in checks  # 不留 None(None 会被当成「等判官」)
    assert checks["counts"]["input_growth_unmeasured"] == 1


# --------------------------------------------------------------------------- M1


def test_rate_limited_judge_call_keeps_its_reservation_and_others_still_get_judged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    price = Price(9.0, 27.0, 0.3, "t")
    one_cap = spend.call_cap_yuan(price, live.JUDGE_MIN_INPUT_TOKENS, live.JUDGE_MAX_TOKENS)
    guard = spend.Guard(
        stop_line=100.0,
        prices={plan.JUDGE_MODEL: price},
        phase_caps={"A-judge": 2.05 * one_cap},
        budget_of=phases.budget_of,
    )
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={})
        await asyncio.sleep(0.01)
        body = {
            "choices": [{"message": {"content": '{"answer": "yes", "reason": "x"}'}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        }
        return httpx.Response(200, json=body)

    async def quick(_s: float) -> None:
        await asyncio.sleep(0.005)

    async def main() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            t = live.ChatTransport(
                plan.JUDGE_MODEL,
                "https://j",
                "k",
                guard,
                http,
                live.JUDGE_MAX_TOKENS,
                provider="deepseek",
            )
            judge = live.Judge(t, 1, gate=spend.RoomGate(guard, "a-judge", limit=4))

            async def one(i: int) -> str:
                try:
                    await judge.vote_with_usage("p", item=f"i{i}", phase="a-judge")
                    return "ok"
                except Exception as exc:  # 记下来比
                    return repr(exc)

            return list(await asyncio.gather(*(one(i) for i in range(4))))

    monkeypatch.setattr(live, "_sleep", quick)
    got = asyncio.run(main())
    assert got == ["ok"] * 4
    assert guard.phase_refused == {} and guard.blocked is None


# --------------------------------------------------------------------------- M2 / M4


def test_a13_without_slicing_is_incomplete() -> None:
    s = _s("A13")
    head = ns.spec(s).other_keys[0]
    rec = _rec(
        s, memories=[_row("1", f"{head} {s.x_key}", "user_stated")], caps={**ALL, "slicing": False}
    )
    checks = chk.checks(rec, s)[0]
    assert checks["missing"]["complete"] == "slicing"
    assert checks["missing"]["fragment"] == "slicing"
    assert "complete" not in checks and "fragment" not in checks


def test_a13_head_keys_never_appear_in_the_second_half() -> None:
    for item in rfx.A13_ITEMS:
        assert len(item.head_keys) >= 2, item.key  # 带同义词
        for k in item.head_keys:
            assert not base.has_key(item.tail, k), (item.key, k)
        assert base.has_key(item.tail, item.tail_key)


# --------------------------------------------------------------------------- M3


def test_number_keys_ignore_thousand_separators() -> None:
    assert base.has_key("about 2000 units last year", "2,000")
    assert base.has_key("about 2,000 units", "2000")


def test_numbers_inside_dates_do_not_match() -> None:
    assert not base.has_key("read on 2026-11-15", "11")
    assert not base.has_key("记于 11月15日", "11")
    assert not base.has_key("记于 11月15日", "15")
    assert base.has_key("2026-11-15 读数 47", "47")


def test_alternative_spellings_and_date_formats() -> None:
    key = next(p for p in rf.PROPOSITIONS if p.key == "hearing-date").texts["zh"].key
    for text in ("听证会 11月28日", "hearing on 2026-11-28", "定在 11/28"):
        assert base.has_key(text, key), text
    en = next(p for p in rf.PROPOSITIONS if p.key == "hearing-date").texts["en"].key
    for text in ("hearing on November 28", "set for Nov 28", "2026-11-28"):
        assert base.has_key(text, en), text


def test_secrets_match_with_separators_stripped() -> None:
    assert base.has_secret("card 0000-7713-2048-5502 on file", "0000 7713 2048 5502")
    assert base.has_secret("password Maple#2291", "Maple#2291")  # 审查二:只去数字间的空格 / 横线
    assert not base.has_secret("nothing here", "0000 7713 2048 5502")
    s8 = next(s for s in _rows("S8") if ns.spec(s).secret.isdigit() and len(ns.spec(s).secret) > 12)
    secret = ns.spec(s8).secret
    hyphenated = "-".join(secret[i : i + 4] for i in range(0, len(secret), 4))
    rec = _rec(s8, memories=[_row("1", f"card {hyphenated}", None)])
    assert chk.checks(rec, s8)[0]["secret_stored"] is True


def _rows(cls: str) -> list[sc.Scenario]:
    return [s for s in ns.build_redesign() if s.cls == cls]


def _luhn(digits: str) -> bool:
    d = [int(c) for c in digits][::-1]
    total = sum(x if i % 2 == 0 else (x * 2 - 9 if x * 2 > 9 else x * 2) for i, x in enumerate(d))
    return total % 10 == 0


def test_card_like_secrets_are_not_real_cards() -> None:
    """审查 M3:卡号类的秘密值用明显无效的号:不过 Luhn、开头不是任何真实发卡机构(0 开头)。"""
    found = 0
    for cls in ("S8", "H4"):
        for s in _rows(cls):
            digits = "".join(c for c in ns.spec(s).secret if c.isdigit())
            if len(digits) >= 13:
                found += 1
                assert not _luhn(digits) and digits[0] == "0", (s.scenario_id, digits)
    assert found >= 3


def test_readings_avoid_day_numbers_and_years() -> None:
    for r in rfx.READINGS:
        for v in (r.morning, r.corrected, r.evening):
            value = float(v)
            assert value > 31 and not 1900 <= value <= 2100, (r.key, v)


# --------------------------------------------------------------------------- 形状留出集


def test_shape_set_is_the_held_out_one() -> None:
    """换成没看过提示词例子的人写的那 20 个(4 种 x 5 个领域,中英各 10)。"""
    rows = [s for c in ns.SHAPE_CLASSES for s in _rows(c)]
    assert len(rows) == 20 and sum(s.lang == "zh" for s in rows) == 10
    assert {s.scenario_id.split("-")[0] for s in rows} == set(ns.SHAPE_CLASSES)
    expected = [ns.spec(s).expected for s in rows]
    assert all(e.get("user_confirmed") for e in expected)
    s4 = [s for s in rows if s.cls == "H4"]
    assert all(ns.spec(s).secret for s in s4)
    secrets = {ns.spec(s).secret for s in s4}
    assert {"604918", "339201", "4417#", "772045"} <= secrets


def test_shape_counts_follow_the_expected_lists() -> None:
    s = next(x for x in _rows("H3"))
    e = ns.spec(s).expected
    rows = [
        _row("1", f"plan {e['user_confirmed'][0]}", "user_confirmed"),
        _row("2", f"found {e['assistant_derived'][0]}", "user_confirmed"),
    ]
    counts = chk.checks(_rec(s, memories=rows), s)[0]["counts"]
    assert counts["user_confirmed_hit"] == 1
    assert counts["assistant_value_on_user_side"] == 1
    # 存下了、但来源不是期望的那种:不算命中,另计
    wrong = [_row("1", f"plan {e['user_confirmed'][0]}", "assistant_derived")]
    counts = chk.checks(_rec(s, memories=wrong), s)[0]["counts"]
    assert counts["user_confirmed_other_origin"] == 1 and counts["user_confirmed_hit"] == 0
    h4 = next(x for x in _rows("H4"))
    leak = _rec(h4, memories=[_row("1", f"code {ns.spec(h4).secret}", None, live_=False)])
    assert chk.checks(leak, h4)[0]["counts"]["secret_stored"] == 1


# --------------------------------------------------------------------------- M5


def test_quote_states_votes_per_segment_and_prices_both(tmp_path: Path) -> None:
    q = _quote(tmp_path, *GLM, "--phases", "g", "--stop-line", "20")
    assert q["judge"]["votes_by_segment"] == {"A": 1, "G": 1}
    by_votes = q["judge"]["cost_by_votes"]
    assert set(by_votes) == {"1", "3"} and by_votes["3"]["yuan_high"] > by_votes["1"]["yuan_high"]
    three = _quote(tmp_path, *GLM, "--phases", "g", "--stop-line", "20", "--gate-judge-votes", "3")
    assert three["judge"]["votes_by_segment"] == {"A": 1, "G": 3}
    assert three["planned"]["total"]["yuan_high"] == pytest.approx(by_votes["3"]["yuan_high"])


def test_gate_judge_uses_the_gate_votes(tmp_path: Path) -> None:
    q = {"pilot": False, "judge": {"a_group_votes": 1, "gate_votes": 3}}
    phases.calibration_path(tmp_path, "a").write_text(
        json.dumps(
            {
                "judge_model": plan.JUDGE_MODEL,
                "votes": 3,
                "agree": 20,
                "wording_digest": rep.a_judge_wording_digest(),
            }
        )
    )
    assert cli.judge_votes(tmp_path, q, "extraction", "gate") == 3


def test_report_discloses_the_votes_used(tmp_path: Path) -> None:
    out = tmp_path / "out"
    (out / "gate").mkdir(parents=True)
    (out / "gate" / "records.jsonl").write_text("")
    (out / "gate" / "verdicts.jsonl").write_text(
        json.dumps({"key": "k|confirmed_grounded", "votes": [True, True, False], "verdict": True})
        + "\n"
    )
    text = " ".join(cli.report(out, None)["disclosures"])
    assert "gate" in text and "3 票" in text


def _labels(kinds: list[str]) -> list[dict[str, Any]]:
    prompts = {
        "plain": f"{rep.SOURCE_WORDING} {rep.NOT_USER_WORDING} Question:",
        "statement": "The user only asked ... it is not a statement. Question:",
        "grounded": base.grounded_prompt("a", "b", ["c"]),
        "event_date": base.event_date_prompt("c", "2026-11-15"),
    }
    rows = []
    for i, d in enumerate(d for d in sc.DOMAINS for _ in range(4)):
        rows.append({"domain": d, "label": i % 2 == 0, "prompt": prompts[kinds[i % len(kinds)]]})
    return rows


def test_label_kinds_are_recognised() -> None:
    assert rep.label_check(base.grounded_prompt("a", "b", ["c"])) == "grounded"
    assert rep.label_check(base.event_date_prompt("c", "x")) == "event_date"
    assert rep.label_check("old wording") is None


@pytest.mark.parametrize(
    ("phases_arg", "kinds", "missing"),
    [
        ("g", ["grounded"], "event_date"),
        ("a", ["grounded", "event_date"], "plain"),
    ],
)
def test_calibration_needs_every_check_the_segment_judges(
    tmp_path: Path, phases_arg: str, kinds: list[str], missing: str
) -> None:
    labels = tmp_path / "labels.jsonl"
    labels.write_text("".join(json.dumps(r) + "\n" for r in _labels(kinds)))
    out: list[str] = []
    code = cli.main(
        [
            "calibrate",
            "--set",
            "a",
            "--labels",
            str(labels),
            "--out",
            str(tmp_path / "o"),
            "--phases",
            phases_arg,
            "--stop-line",
            "5",
            "--approved-quote",
            "x",
            "--state-dir",
            str(tmp_path / "state"),
        ],
        emit=out.append,
    )
    assert code == 2 and missing in out[-1]


# --------------------------------------------------------------------------- M6


def _a10(memories: list[dict[str, Any]]) -> dict[str, Any]:
    s = next(x for x in sc.build_group("A") if x.cls == "A10")
    x = sc.fact(s.fact_key).texts[s.lang].x
    reply = json.dumps({"memories": [{"kind": "fact", "content": x}]})
    rec = {
        "key": "k",
        "result": {
            "seeded": {"x": "seed"},
            "rounds": [{"calls": [{"kind": "pass1", "reply": reply}]}],
            "memories": [{**m, "content": m["content"].format(x=x)} for m in memories],
            "capabilities": ALL,
        },
    }
    return rep.a_checks(rec, s)[0]


def test_a10_original_must_be_replaced_by_a_live_user_stated_row() -> None:
    new = _row("n", "{x}", "user_stated")
    replaced = _a10([_row("seed", "{x}", "assistant_derived", live_=False, superseded_by="n"), new])
    assert replaced["original_superseded"] is True and replaced["replaced_by_user_stated"] is True
    expired = _a10([_row("seed", "{x}", "assistant_derived", live_=False), new])
    assert expired["original_superseded"] is False
    assert expired["replaced_by_user_stated"] is False
    by_asst = _row("a", "{x}", "assistant_derived")
    wrong = _a10(
        [_row("seed", "{x}", "assistant_derived", live_=False, superseded_by="a"), by_asst]
    )
    assert wrong["original_superseded"] is False


def test_a10_has_a_line_that_is_not_called_approved() -> None:
    line = next(ln for ln in rep.LINES if (ln.cls, ln.metric) == ("A10", "replaced_by_user_stated"))
    assert line.op == "min" and line.status.startswith("待用户确认")
    verdict = rep.judge_line(line, [("support", False)] * 30)["verdict"]
    assert "not an acceptance result" in verdict


# --------------------------------------------------------------------------- LOW


def test_fits_is_conservative_at_the_edge() -> None:
    g = spend.Guard(stop_line=2.0, prices={"m": Price(1, 1, 1, "t")})
    assert not g.fits("a", 2.0) and g.fits("a", 1.9)


def test_three_votes_need_room_for_all_three_before_the_first() -> None:
    price = Price(9.0, 27.0, 0.3, "t")
    one_cap = spend.call_cap_yuan(price, live.JUDGE_MIN_INPUT_TOKENS, live.JUDGE_MAX_TOKENS)
    guard = spend.Guard(
        stop_line=100.0,
        prices={plan.JUDGE_MODEL: price},
        phase_caps={"A-judge": 2.5 * one_cap},
        budget_of=phases.budget_of,
    )
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        body = {
            "choices": [{"message": {"content": '{"answer": "yes"}'}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 10},
        }
        return httpx.Response(200, json=body)

    async def main() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            t = live.ChatTransport(
                plan.JUDGE_MODEL,
                "https://j",
                "k",
                guard,
                http,
                live.JUDGE_MAX_TOKENS,
                provider="deepseek",
            )
            judge = live.Judge(t, 3, gate=spend.RoomGate(guard, "a-judge", limit=4))
            await judge.vote_with_usage("p", item="i", phase="a-judge")

    with pytest.raises(live.NoRoomError):
        asyncio.run(main())
    assert sent == [] and guard.spent == 0  # 一票都没发,不留半截


def test_split_and_missing_verdicts_are_counted_apart(tmp_path: Path) -> None:
    out = tmp_path / "out"
    s = _s("S2")
    rows = []
    for key in ("k1", "k2"):
        rec = _rec(s, memories=[_row("1", s.x_key, "user_confirmed")])
        rows.append(
            {
                **rec,
                "key": key,
                "scenario_id": s.scenario_id,
                "cls": "S2",
                "vendor": "glm",
                "domain": s.domain,
            }
        )
    (out / "s").mkdir(parents=True)
    (out / "s" / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    digest = rep.a_judge_wording_digest()
    (out / "s" / "verdicts.jsonl").write_text(
        json.dumps({"key": "k1|confirmed_grounded", "verdict": None, "wording_digest": digest})
        + "\n"
    )
    counts = cli.report(out, None)["counts"]["s"]
    assert counts["judge_split"] == 1 and counts["judge_missing"] == 1
    assert "judge_split_or_missing" not in counts


def test_nothing_measured_says_why() -> None:
    line = next(ln for ln in rep.LINES if (ln.cls, ln.metric) == ("S2", "confirmed_grounded"))
    assert "nothing measured" in rep.judge_line(line, [])["verdict"]


def test_fact_files_stay_under_800_lines() -> None:
    for module in (rf, rfx, rh):
        assert len(Path(str(module.__file__)).read_text().splitlines()) < 800, module.__name__
