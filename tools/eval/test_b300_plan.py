"""B-300 报价:单价出处、调用次数、§8.6 的删减顺序、¥400 上限、报价编号。离线。"""

from __future__ import annotations

from typing import Any

import b300_plan as plan
import pytest
from b301_plan import Price

CARD = {
    "qwen3.8-max-0902": Price(12.0, 36.0, 1.5, "rate card"),
    "doubao-seed-2-1-pro-260915": Price(6.0, 30.0, 1.2, "rate card"),
}


def _prices() -> dict[str, Price]:
    return plan.prices_for(plan.DEFAULT_VENDORS, accept_documented=True, card=CARD)


def _quote(**kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "already_spent": 0.0,
        "single_vendor": None,
        "stop_line": 400.0,
        "scenario_digest": "d",
    }
    args.update(kw)
    return plan.quote(plan.DEFAULT_VENDORS, _prices(), **args)


def test_documented_prices_need_the_flag() -> None:
    with pytest.raises(plan.PlanError, match="accept-documented-prices"):
        plan.prices_for(plan.DEFAULT_VENDORS, accept_documented=False, card=CARD)
    prices = _prices()
    assert prices["qwen3.8-max-0902"].source == "rate card"
    assert "kb_agent_cost.py" in prices["glm-5.3"].source
    assert prices["glm-5.3-flash"].input == 0.8


def test_real_rate_card_is_parsed() -> None:
    prices = plan.prices_for(plan.DEFAULT_VENDORS, accept_documented=True)
    assert prices["doubao-seed-2-1-pro-260915"].output == 30.0
    assert "0187" in prices["qwen3.8-max-0902"].source


def test_parse_vendor() -> None:
    assert plan.parse_vendor("glm=glm-5.3:glm-5.3-flash") == plan.Vendor(
        "glm", "glm-5.3", "glm-5.3-flash"
    )
    assert plan.parse_vendor("qwen=q").memory == "q"
    with pytest.raises(plan.PlanError):
        plan.parse_vendor("qwen")


def _calls(
    lines: list[plan.Line], group: str, kind: str, vendor: str | None = None
) -> tuple[float, float]:
    rows = [
        ln
        for ln in lines
        if ln.group == group and ln.kind == kind and (vendor is None or ln.vendor == vendor)
    ]
    return sum(ln.low for ln in rows), sum(ln.high for ln in rows)


def test_counts_follow_the_design() -> None:
    lines = plan.lines_for(plan.DEFAULT_VENDORS)
    # A:11 类各 30 次写回 + A12 30 次 x 5 轮 = 480 次写回 / 家,每次 2 次提炼 + 0~1 次去重合并。
    assert _calls(lines, "A", "extract", "glm") == (960, 960)
    assert _calls(lines, "A", "reconcile", "glm") == (0, 480)
    # B:7 类 x 10 x 3 = 210 段 / 家,加两成补跑;每段 1.5~2 次主模型。
    assert _calls(lines, "B", "main", "glm") == pytest.approx((378, 504))
    assert _calls(lines, "B", "judge", "glm") == pytest.approx((756, 756))
    # C5 ①:整理只在 glm-5.3-flash 上(§12 ②),15 次加两成。
    c5 = [ln for ln in lines if ln.group == "C-C5" and ln.kind == "consolidate"]
    assert [ln.model for ln in c5] == [plan.CONSOLIDATION_MODEL]
    assert (c5[0].low, c5[0].high) == pytest.approx((18, 54))


def test_cuts_two_and_three_keep_one_vendor() -> None:
    lines = plan.lines_for(plan.DEFAULT_VENDORS, cuts=(1, 2, 3), single_vendor="doubao")
    assert {ln.vendor for ln in lines if ln.group == "B-screen"} == {"doubao"}
    assert {ln.vendor for ln in lines if ln.group in {"C-C1", "C-C3"}} == {"doubao"}
    assert {ln.vendor for ln in lines if ln.group == "C-C2"} == {
        "glm",
        "deepseek",
        "qwen",
        "doubao",
    }
    assert not [ln for ln in lines if ln.group == "B-alt"]
    baseline = {ln.vendor for ln in lines if ln.group == "C-baseline"}
    assert baseline == {"doubao", "consolidation"}


def test_cut_order_and_stop() -> None:
    prices = _prices()
    totals = {}
    for cuts in ((), (1,), (1, 2), (1, 2, 3)):
        lines = plan.lines_for(plan.DEFAULT_VENDORS, cuts=cuts, single_vendor="qwen")
        totals[cuts] = plan.summarize(lines, prices)["total"]["yuan_high"]
    assert totals[()] > totals[(1,)] > totals[(1, 2)] > totals[(1, 2, 3)]

    def chosen(cap: float) -> tuple[list[int], str]:
        cuts, _s, status = plan.choose_cuts(
            plan.DEFAULT_VENDORS, prices, already_spent=0.0, single_vendor="qwen", cap=cap
        )
        return cuts, status

    assert chosen(totals[()] + 1) == ([], "within_cap")
    assert chosen(totals[(1,)] + 0.01) == ([1], "within_cap")
    assert chosen(totals[(1, 2)] + 0.01) == ([1, 2], "within_cap")
    assert chosen(totals[(1, 2, 3)] + 0.01) == ([1, 2, 3], "within_cap")
    assert chosen(totals[(1, 2, 3)] - 1) == ([1, 2, 3], "stop_and_ask")


def test_already_spent_counts_toward_the_cap() -> None:
    q = _quote(cap=1e9)
    assert q["status"] == "within_cap" and q["cuts_applied"] == []
    q = _quote(cap=1e9, already_spent=1e9 - 1)
    assert q["status"] == "stop_and_ask"


def test_stop_line_over_remaining_cap_is_stop_and_ask() -> None:
    q = _quote(cap=1e9, already_spent=1e9 - 300, stop_line=400.0)
    assert q["status"] == "stop_and_ask"


def test_quote_id_changes_with_inputs() -> None:
    a = _quote()
    assert a["quote_id"] == _quote()["quote_id"]
    assert a["quote_id"] != _quote(stop_line=300.0)["quote_id"]
    assert a["quote_id"] != _quote(single_vendor="glm")["quote_id"]
    assert a["quote_id"] != _quote(scenario_digest="other")["quote_id"]
    assert a["single_vendor"] == "qwen" and a["single_vendor_chosen"] is False


def test_quote_reports_groups_vendors_and_assumptions() -> None:
    q = _quote(cap=1e9)
    groups = q["planned"]["groups"]
    assert {"A", "B", "B-screen", "C-C1", "C-C4", "C-C5", "C-baseline", "calibration"} <= set(
        groups
    )
    assert set(groups["B"]["by_vendor"]) == {"glm", "deepseek", "qwen", "doubao"}
    low, high = q["planned"]["total"]["yuan_low"], q["planned"]["total"]["yuan_high"]
    assert 0 < low < high
    assert q["token_assumptions"]["consolidate"]["source"].startswith("measured")
    assert q["token_assumptions"]["main"]["source"] == "assumption, not measured"


def test_token_overrides() -> None:
    table = plan.load_token_overrides({"main": {"low": [1000, 100], "high": [2000, 200]}})
    assert table["main"] == ((1000, 100), (2000, 200))
    with pytest.raises(plan.PlanError):
        plan.load_token_overrides({"nope": {"low": [1, 1], "high": [2, 2]}})
    with pytest.raises(plan.PlanError):
        plan.load_token_overrides({"main": {"low": [3000, 100], "high": [2000, 200]}})
    cheaper = _quote(cap=1e9, tokens=table)
    assert (
        cheaper["planned"]["total"]["yuan_high"] < _quote(cap=1e9)["planned"]["total"]["yuan_high"]
    )
