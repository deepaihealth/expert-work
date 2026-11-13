"""B-300 评测脚本第三轮审查的修复(两条 MEDIUM 与 LOW):离线。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import b300_eval as cli
import b300_live as live
import b300_phases as phases
import b300_plan as plan
import b300_runs as runs
import b300_scenarios as sc
import b300_spend as spend
import pytest
from b301_plan import Price

PRICES = {"m": Price(1.0, 1.0, 1.0, "t")}
FLASH = {"glm-5.3-flash": Price(0.8, 2.8, 0.23, "t")}


def _quote(tmp_path: Path, *extra: str) -> dict[str, Any]:
    out: list[str] = []
    code = cli.main(
        ["quote", "--accept-documented-prices", "--state-dir", str(tmp_path / "state"), *extra],
        emit=out.append,
    )
    assert code == 0, out
    return json.loads(out[0])


# --------------------------------------------------------------------------- MEDIUM 1


def test_approved_cap_message_names_both_parts(tmp_path: Path) -> None:
    state = tmp_path / "state"
    old = spend.Guard.resume(stop_line=400.0, prices=PRICES, state_dir=state, quote_id="old")
    call = old.reserve(item="x", model="m", cap=350.0, phase="b")
    old.settle(call, item="x", model="m", phase="b", cost=350.0)
    q = _quote(tmp_path, "--stop-line", "100")
    out: list[str] = []
    code = cli.main(
        [
            "run",
            "--phase",
            "a",
            "--out",
            str(tmp_path / "out"),
            "--approved-quote",
            q["quote_id"],
            "--stop-line",
            "100",
            "--user-said",
            "go ahead with it",
            "--accept-documented-prices",
            "--state-dir",
            str(state),
        ],
        emit=out.append,
    )
    assert code == 2 and "other quotes have spent ¥350" in out[-1] and "this quote" in out[-1]


# --------------------------------------------------------------------------- MEDIUM 2


def _finish_phase(out: Path, q: dict[str, Any], name: str) -> int:
    units = phases.build_units(phases.PHASES[name], q)
    phases.save_units(out, name, units, q)
    scenarios = phases.all_scenarios()
    rows = [
        {
            **u,
            "key": phases.unit_key(u),
            "convs": [{}] * len(scenarios[u["scenario_id"]].conversations),
        }
        for u in units
    ]
    (out / name / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return len(units)


# --------------------------------------------------------------------------- LOW


@pytest.mark.parametrize("raw", ["b=nan", "b=-5", "b=inf"])
def test_allocation_values_are_validated(raw: str) -> None:
    with pytest.raises(plan.PlanError):
        plan.parse_allocation(raw)


@pytest.mark.parametrize("share", [1.5, -0.1, math.nan, math.inf])
def test_progress_ratios_are_validated(share: float) -> None:
    with pytest.raises(plan.PlanError):
        plan.check_remaining({"B": share})


def _ledger_with_reservations(
    tmp_path: Path, rows: list[dict[str, Any]]
) -> tuple[spend.Guard, list[str]]:
    guard = spend.Guard.resume(
        stop_line=100.0, prices=FLASH, state_dir=tmp_path / "s", quote_id="q"
    )
    (tmp_path / "c4").mkdir(parents=True, exist_ok=True)
    reservations = []
    for n, extra in enumerate(rows):
        call = live.reserve_writeback(guard, item=f"k|c{n}", model="glm-5.3-flash", phase="c4")
        reservations.append({"call_id": call, "conv": n, **extra})
    record = {"key": "k", "user": "u", "writeback_reservations": reservations, "convs": []}
    (tmp_path / "c4" / "records.jsonl").write_text(json.dumps(record) + "\n")
    return guard, [r["call_id"] for r in reservations]


USAGE = [{"model": "glm-5.3-flash", "input_tokens": 100_000, "output_tokens": 0}]  # ¥0.08


def test_a_run_that_never_started_settles_at_zero_without_blocking_the_rest(tmp_path: Path) -> None:
    guard, calls = _ledger_with_reservations(
        tmp_path, [{"run_id": "r1"}, {"run_id": None, "run_started": False}]
    )
    report = {"op": "writeback_usage", "key": "u", "jobs": {"r1": "done"}, "models": USAGE}
    runs.settle_writeback_usage(tmp_path, "c4", [report], guard)
    assert guard.open_cap(calls[0]) == 0 and guard.open_cap(calls[1]) == 0
    assert guard.spent == pytest.approx(0.08)


def test_an_unknown_run_stays_reserved_but_does_not_block_the_rest(tmp_path: Path) -> None:
    guard, calls = _ledger_with_reservations(tmp_path, [{"run_id": "r1"}, {"run_id": None}])
    report = {"op": "writeback_usage", "key": "u", "jobs": {"r1": "done"}, "models": USAGE}
    runs.settle_writeback_usage(tmp_path, "c4", [report], guard)
    assert guard.open_cap(calls[0]) == 0  # r1 结掉了
    assert guard.open_cap(calls[1]) > 0  # 不知道建没建 run 的那笔留着(按上限算)


def test_stepwise_ingest_does_not_count_a_run_twice(tmp_path: Path) -> None:
    guard, _calls = _ledger_with_reservations(tmp_path, [{"run_id": "r1"}])
    step1 = {"op": "writeback_usage", "key": "u", "jobs": {"r1": "done"}, "models": USAGE}
    runs.settle_writeback_usage(tmp_path, "c4", [step1], guard)
    assert guard.spent == pytest.approx(0.08)
    # 第二步:又一次写回;用量窗口还是从最早那次算起,所以报回来的是两次的合计
    record = json.loads((tmp_path / "c4" / "records.jsonl").read_text())
    call2 = live.reserve_writeback(guard, item="k|c1", model="glm-5.3-flash", phase="c4")
    record["writeback_reservations"].append({"call_id": call2, "conv": 1, "run_id": "r2"})
    (tmp_path / "c4" / "records.jsonl").write_text(json.dumps(record) + "\n")
    both = [{"model": "glm-5.3-flash", "input_tokens": 150_000, "output_tokens": 0}]
    step2 = {
        "op": "writeback_usage",
        "key": "u",
        "jobs": {"r1": "done", "r2": "done"},
        "models": both,
    }
    runs.settle_writeback_usage(tmp_path, "c4", [step2], guard)
    assert guard.spent == pytest.approx(0.12)  # 0.08 + 0.04,不是 0.08 + 0.12
    assert guard.blocked is None
    runs.settle_writeback_usage(tmp_path, "c4", [step2], guard)  # 再收一次:什么都不变
    assert guard.spent == pytest.approx(0.12)


def test_settle_only_ingest_uses_the_ledger_quote_not_a_recomputed_one(tmp_path: Path) -> None:
    """旧报价的账在新报价出现之后仍能结:结算只认账本里的报价编号。"""
    state = tmp_path / "state"
    q1 = _quote(tmp_path, "--stop-line", "100")
    guard = spend.Guard.resume(
        stop_line=100.0, prices=FLASH, state_dir=state, quote_id=q1["quote_id"]
    )
    guard.reserve(item="k|c0|writeback", model="glm-5.3-flash", cap=1.0, phase="c4")
    other = spend.Guard.resume(stop_line=50.0, prices=PRICES, state_dir=state, quote_id="newer")
    c = other.reserve(item="x", model="m", cap=5.0, phase="b")
    other.settle(c, item="x", model="m", phase="b", cost=5.0)  # 新报价花了钱 → q1 重算后编号会变
    args = cli.parser().parse_args(
        [
            "ingest",
            "--phase",
            "c4",
            "--op",
            "writeback_usage",
            "--file",
            "f",
            "--out",
            str(tmp_path / "out"),
            "--approved-quote",
            q1["quote_id"],
            "--stop-line",
            "100",
            "--accept-documented-prices",
            "--state-dir",
            str(state),
        ]
    )
    from b300_approve import settle_guard

    with spend.state_lock(state):
        settler = settle_guard(args)
    assert settler.spent == pytest.approx(1.0)
    args.approved_quote = "never-seen"
    with pytest.raises(phases.EvalError, match="no ledger"):
        with spend.state_lock(state):
            settle_guard(args)


def test_run_longmem_needs_an_explicit_user_text_choice() -> None:
    import run_longmem

    base = ["--benchmark", "fixture_locomo", "--tier", "endtoend", "--llm-model", "m"]
    for extra in ([], ["--expect-user-text", "--pre-b300"]):
        with pytest.raises(SystemExit, match="exactly one"):
            run_longmem.check_user_text_choice(
                run_longmem.build_parser().parse_args([*base, *extra])
            )
    run_longmem.check_user_text_choice(run_longmem.build_parser().parse_args([*base, "--pre-b300"]))
    script = (Path(run_longmem.__file__).parent / "run_baseline_full.sh").read_text()
    assert script.count("$USER_TEXT_FLAG") >= 3


_ = sc
