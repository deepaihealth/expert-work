"""B-300 花费:先预留后发送、止损线、账本续算、跨报价总账。离线。"""

from __future__ import annotations

import json
from pathlib import Path

import b300_spend as spend
import pytest
from b301_plan import Price

PRICES = {"m": Price(1.0, 10.0, 0.1, "test")}


def test_reserve_refuses_when_the_cap_would_cross_the_stop_line() -> None:
    guard = spend.Guard(stop_line=1.0, prices=PRICES)
    first = guard.reserve(item="a", model="m", cap=0.6, phase="p")
    with pytest.raises(spend.SpendStopError):
        guard.reserve(item="b", model="m", cap=0.6, phase="p")  # 0.6 在飞 + 0.6 > 1.0
    assert guard.blocked is not None
    guard.settle(first, item="a", model="m", phase="p", cost=0.1)
    assert guard.spent == pytest.approx(0.1)


def test_unknown_model_blocks() -> None:
    guard = spend.Guard(stop_line=10.0, prices=PRICES)
    with pytest.raises(spend.SpendStopError, match="no validated price"):
        guard.reserve(item="a", model="other", cap=0.1, phase="p")


def test_ledger_rows_and_resume(tmp_path: Path) -> None:
    guard = spend.Guard.resume(stop_line=10.0, prices=PRICES, state_dir=tmp_path, quote_id="q1")
    done = guard.reserve(item="a", model="m", cap=0.5, phase="p")
    guard.settle(done, item="a", model="m", phase="p", cost=0.2)
    guard.reserve(item="b", model="m", cap=0.7, phase="p")  # 进程「崩了」:只有预留
    rows = [json.loads(x) for x in spend.ledger_path(tmp_path, "q1").read_text().splitlines()]
    assert [r["reason"] for r in rows] == ["reserve", "ok", "reserve"]
    again = spend.Guard.resume(stop_line=10.0, prices=PRICES, state_dir=tmp_path, quote_id="q1")
    assert again.spent == pytest.approx(0.9)  # 结算的按结算,没结算的按预留


def test_history_excludes_the_given_quote(tmp_path: Path) -> None:
    for quote_id, cost in (("q1", 1.0), ("q2", 2.5)):
        g = spend.Guard.resume(stop_line=10.0, prices=PRICES, state_dir=tmp_path, quote_id=quote_id)
        call = g.reserve(item="x", model="m", cap=cost, phase="p")
        g.settle(call, item="x", model="m", phase="p", cost=cost)
    assert spend.history_total(tmp_path) == {"q1": 1.0, "q2": 2.5}
    assert spend.history_total(tmp_path, exclude="q2") == {"q1": 1.0}
    assert spend.history_total(tmp_path / "missing") == {}


def test_settle_usage_prices_cache_reads_and_blocks_on_gaps() -> None:
    guard = spend.Guard(stop_line=10.0, prices=PRICES)
    call = guard.reserve(item="a", model="m", cap=1.0, phase="p")
    usage = [
        {
            "model": "m",
            "input_tokens": 1_000_000,
            "cache_read_tokens": 500_000,
            "output_tokens": 10_000,
        }
    ]
    cost = guard.settle_usage(call, item="a", model="m", phase="p", usage_by_model=usage)
    assert cost == pytest.approx(0.5 + 0.05 + 0.1)
    assert guard.blocked is None
    call = guard.reserve(item="b", model="m", cap=0.3, phase="p")
    assert guard.settle_usage(call, item="b", model="m", phase="p", usage_by_model=None) == 0.3
    assert "no usage" in (guard.blocked or "")


def test_settle_over_the_cap_blocks() -> None:
    guard = spend.Guard(stop_line=10.0, prices=PRICES)
    call = guard.reserve(item="a", model="m", cap=0.1, phase="p")
    guard.settle(call, item="a", model="m", phase="p", cost=0.2)
    assert "exceeded its cap" in (guard.blocked or "")


def test_unwritable_ledger_stops(tmp_path: Path) -> None:
    target = tmp_path / "dir-not-file"
    target.mkdir()
    guard = spend.Guard(stop_line=10.0, prices=PRICES, ledger=spend.SpendLedger(target, "q"))
    with pytest.raises(spend.SpendStopError):
        guard.reserve(item="a", model="m", cap=0.1, phase="p")
    assert guard.spent == 0.0  # 预留没写成就不算、也不发
