"""第二轮审查(PR E):A14 只量新段 + 上下文行数单独查;闸门 3 票时校准也用 3 票;秘密值按数字
边界比;留出集的写法收紧;H2 的说明;多个秘密值;连续限流只让这一项失败。离线、不花钱。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import b300_eval as cli
import b300_live as live
import b300_plan as plan
import b300_redesign_base as base
import b300_redesign_checks as chk
import b300_redesign_scenarios as ns
import b300_report as rep
import b300_runs as runs
import b300_scenarios as sc
import b300_spend as spend
import httpx
import pytest
from b301_plan import Price

ALL = dict.fromkeys(base.CAPABILITIES, True)
GLM = ["--vendor", "glm=glm-5.3:glm-5.3-flash", "--single-vendor", "glm"]


def _a14() -> sc.Scenario:
    return next(s for s in ns.build_redesign() if s.cls == "A14")


def _numbered(context: list[str], new: list[str]) -> str:
    rows = [f"[{i}] {line}" for i, line in enumerate([*context, *new], start=1)]
    marker = [base.NEW_MARKER] if context else []
    return "\n".join([*rows[: len(context)], *marker, *rows[len(context) :]])


def _a14_record(
    inputs: list[tuple[str, str]], caps: dict[str, bool] | None = None
) -> dict[str, Any]:
    """每轮 (第一次的输入, 第二次的输入)。"""
    rounds = [
        {
            "after_turn": i,
            "calls": [
                {"kind": "pass1", "reply": '{"memories": []}', "input": p1},
                {"kind": "pass2", "reply": '{"memories": []}', "input": p2},
            ],
        }
        for i, (p1, p2) in enumerate(inputs)
    ]
    return {"key": "u", "result": {"rounds": rounds, "memories": [], "capabilities": caps or ALL}}


def _sliced(rounds: int = 12, p1_ctx: int = 2, p2_ctx: int = 4) -> list[tuple[str, str]]:
    """切片正确:每轮新段是这一轮的一条用户输入 + 一条回答;上下文从 0 行慢慢涨到上限(热身)。"""
    out = []
    history: list[str] = []
    users: list[str] = []
    for k in range(rounds):
        user, reply = f"[user] [2026-11-15] item {k} " + "x" * 30, f"[assistant] ok {k}"
        p1 = _numbered(users[-p1_ctx:] if p1_ctx else [], [user])
        p2 = _numbered(history[-p2_ctx:] if p2_ctx else [], [user, reply])
        out.append((p1, p2))
        history += [user, reply]
        users.append(user)
    return out


def test_a14_correct_slicing_with_context_warm_up_passes() -> None:
    """审查二 HIGH:上下文 4 / 2 行的热身让前 3 轮天然偏短;只量新段就与热身无关。"""
    checks = chk.checks(_a14_record(_sliced()), _a14())[0]
    assert checks["input_growth"] is False and checks["context_over_cap"] is False


def test_a14_no_slicing_still_fails() -> None:
    history: list[str] = []
    inputs = []
    for k in range(12):
        history += [f"[user] item {k} " + "x" * 30, f"[assistant] ok {k}"]
        whole = "\n".join(f"[{i}] {line}" for i, line in enumerate(history, start=1))
        inputs.append((whole, whole))  # 没有切片:没有分隔行,整段都是新的
    assert chk.checks(_a14_record(inputs), _a14())[0]["input_growth"] is True


def test_a14_context_over_its_cap_is_its_own_failure() -> None:
    checks = chk.checks(_a14_record(_sliced(p2_ctx=6)), _a14())[0]
    assert checks["context_over_cap"] is True and checks["input_growth"] is False
    checks = chk.checks(_a14_record(_sliced(p1_ctx=3)), _a14())[0]
    assert checks["context_over_cap"] is True


def test_a14_context_line_is_a_line_and_the_measure_is_disclosed(tmp_path: Path) -> None:
    line = next(ln for ln in rep.LINES if (ln.cls, ln.metric) == ("A14", "context_over_cap"))
    assert line.op == "max" and line.total == 0 and line.expected_n == 6
    out = tmp_path / "out"
    (out / "a14").mkdir(parents=True)
    (out / "a14" / "records.jsonl").write_text("")
    text = " ".join(cli.report(out, None)["disclosures"])
    assert "新段" in text and "前 3 轮" in text


# --------------------------------------------------------------------------- MEDIUM


def _labels() -> list[dict[str, Any]]:
    kinds = [base.grounded_prompt("a", "b", ["c"]), base.event_date_prompt("c", "2026-11-15")]
    return [
        {"domain": d, "label": i % 2 == 0, "prompt": kinds[i % 2]}
        for i, d in enumerate(d for d in sc.DOMAINS for _ in range(4))
    ]


def test_gate_calibration_runs_at_the_gate_votes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审查二 MEDIUM:``--gate-judge-votes 3`` 时闸门的校准也按 3 票(报价就按 3 票算的)。"""
    args = [
        *GLM,
        "--phases",
        "g",
        "--gate-judge-votes",
        "3",
        "--stop-line",
        "20",
        "--accept-documented-prices",
        "--state-dir",
        str(tmp_path / "state"),
    ]
    out: list[str] = []
    assert cli.main(["quote", *args], emit=out.append) == 0, out
    quote_id = json.loads(out[0])["quote_id"]
    labels = tmp_path / "labels.jsonl"
    labels.write_text("".join(json.dumps(r) + "\n" for r in _labels()))
    used: list[int] = []

    async def fake_run_judge(items: Any, *, votes: int, **_: Any) -> None:
        used.append(votes)

    monkeypatch.setattr(runs, "run_judge", fake_run_judge)
    msgs: list[str] = []
    code = cli.main(
        [
            "calibrate",
            "--set",
            "a",
            "--labels",
            str(labels),
            "--out",
            str(tmp_path / "o"),
            *args,
            "--approved-quote",
            quote_id,
        ],
        emit=msgs.append,
    )
    assert code == 0, msgs
    assert used == [3]
    saved = json.loads((tmp_path / "o" / "calibration-a.json").read_text())
    assert saved["votes"] == 3


# --------------------------------------------------------------------------- LOW


def test_secret_digits_compare_on_digit_boundaries() -> None:
    assert not base.has_secret("PIN 88, 41 left", "8841")
    assert not base.has_secret("order 188415", "8841")
    assert base.has_secret("PIN 8841", "8841")
    assert base.has_secret("card 0000-7713-2048-5502", "0000 7713 2048 5502")
    assert base.has_secret("password Maple#2291 saved", "Maple#2291")


@pytest.mark.parametrize(
    ("key_part", "text", "hit"),
    [
        ("~theo", "Theo has algebra", True),
        ("~theo", "the theory of it", False),
        ("~nine", "she is nine", True),
        ("~nine", "ninety minutes", False),
    ],
)
def test_whole_word_variants(key_part: str, text: str, hit: bool) -> None:
    assert base.has_key(text, key_part) is hit


def test_held_out_spellings_are_tight() -> None:
    import b300_redesign_heldout as rh

    by_id = {c.key: c for c in rh.HELD_OUT}
    theo = by_id["s1-education-en"].user_stated[0]
    assert not base.has_key("the theory of tutoring", theo)
    nine = by_id["s2-education-zh"].user_confirmed[0]
    assert not base.has_key("ninety", nine)
    linden = by_id["s4-logistics-en"].user_confirmed[0]
    assert base.has_key("new address 88 Linden Court Suite 5", linden)


def test_h2_downgrade_is_disclosed(tmp_path: Path) -> None:
    out = tmp_path / "out"
    (out / "shape").mkdir(parents=True)
    (out / "shape" / "records.jsonl").write_text("")
    text = " ".join(cli.report(out, None)["disclosures"])
    assert "H2" in text and "§5.4" in text and "user_confirmed_other_origin" in text


def test_every_must_not_store_value_is_checked() -> None:
    s = next(x for x in ns.build_redesign() if x.cls == "H4")
    sp = ns.spec(s)
    assert sp.expected["must_not_store"]
    # 给这个场景塞两个秘密值:第二个漏进记忆也要被数到
    two = ns.Spec(
        **{**sp.__dict__, "expected": {**sp.expected, "must_not_store": ("111222", "333444")}}
    )
    monkey = dict(ns._SPECS)
    ns._SPECS[s.scenario_id] = two
    try:
        rec = {
            "key": "u",
            "result": {
                "rounds": [],
                "memories": [{"id": "1", "content": "code 333444", "origin": None, "live": False}],
                "capabilities": ALL,
            },
        }
        assert chk.checks(rec, s)[0]["counts"]["secret_stored"] == 1
    finally:
        ns._SPECS.clear()
        ns._SPECS.update(monkey)


def test_four_rate_limits_fail_the_item_not_the_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = spend.Guard(stop_line=5.0, prices={"m": Price(1, 1, 1, "t")})
    codes = iter([429, 429, 429, 429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        code = next(codes)
        if code == 429:
            return httpx.Response(429, json={})
        body = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        }
        return httpx.Response(200, json=body)

    async def quick(_s: float) -> None:
        return None

    monkeypatch.setattr(live, "_sleep", quick)

    async def main() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            t = live.ChatTransport("m", "https://v", "k", guard, http, 100, provider="deepseek")
            with pytest.raises(live.RateLimitedError):
                await t.complete([{"role": "user", "content": "x"}], item="i1", phase="a")
            text, _usage = await t.complete(
                [{"role": "user", "content": "x"}], item="i2", phase="a"
            )
            return text

    assert asyncio.run(main()) == "ok"
    assert guard.blocked is None
    assert guard.settled < 0.001  # 被限流的那一项按 0 结,不按上限
    assert not isinstance(live.RateLimitedError("x"), spend.SpendStopError)
    assert plan.JUDGE_MODEL  # 只是让 import 有用
