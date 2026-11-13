"""B-300 评测脚本审查后的其余加固(M3~M5 与 LOW):离线。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import b300_eval as cli
import b300_phases as phases
import b300_plan as plan
import b300_runs as runs
import b300_scenarios as sc
import b300_spend as spend
import b300_steps as steps
import pytest
from b301_plan import Price


def _quote(tmp_path: Path, *extra: str) -> dict[str, Any]:
    out: list[str] = []
    code = cli.main(
        ["quote", "--accept-documented-prices", "--state-dir", str(tmp_path / "state"), *extra],
        emit=out.append,
    )
    assert code == 0, out
    return json.loads(out[0])


# --------------------------------------------------------------------------- M3


def test_a_judge_votes_and_a_calibration_are_priced(tmp_path: Path) -> None:
    one = _quote(tmp_path)
    three = _quote(tmp_path, "--a-judge-votes", "3")
    assert one["quote_id"] != three["quote_id"]
    assert one["judge"]["a_group_votes"] == 1 and three["judge"]["a_group_votes"] == 3
    cal_one = one["without_cuts"]["groups"]["calibration-a"]["calls_high"]
    cal_three = three["without_cuts"]["groups"]["calibration-a"]["calls_high"]
    assert (cal_one, cal_three) == (20, 60)
    assert three["planned"]["groups"]["A"]["yuan_high"] > one["planned"]["groups"]["A"]["yuan_high"]


def test_a_judge_needs_its_own_calibration(tmp_path: Path) -> None:
    q = {"pilot": False, "judge": {"a_group_votes": 1}}
    with pytest.raises(cli.EvalError, match="calibrate --set a"):
        cli.judge_votes(tmp_path, q, "extraction")
    phases.calibration_path(tmp_path, "a").write_text(
        json.dumps({"judge_model": plan.JUDGE_MODEL, "votes": 1, "agree": 15, "of": 20})
    )
    with pytest.raises(cli.EvalError, match="--a-judge-votes 3"):
        cli.judge_votes(tmp_path, q, "extraction")
    phases.calibration_path(tmp_path, "a").write_text(
        json.dumps({"judge_model": plan.JUDGE_MODEL, "votes": 1, "agree": 19, "of": 20})
    )
    assert cli.judge_votes(tmp_path, q, "extraction") == 1
    # B / C 组的校准与 A 组分开:A 组校过了不等于 B 组校过了
    with pytest.raises(cli.EvalError, match="not calibrated"):
        cli.judge_votes(tmp_path, q, "conversation")


# --------------------------------------------------------------------------- M4


def test_phase_budgets_go_b_then_c_then_a() -> None:
    planned = {
        "groups": {
            "A": {"yuan_high": 50.0},
            "B": {"yuan_high": 300.0},
            "C-C1": {"yuan_high": 80.0},
            "calibration": {"yuan_high": 2.0},
        }
    }
    budgets = plan.phase_budgets(planned, 350.0)
    assert budgets["B"] == {"high_estimate_yuan": 302.0, "budget_yuan": 302.0, "short_by_yuan": 0.0}
    assert budgets["C"]["budget_yuan"] == 48.0 and budgets["C"]["short_by_yuan"] == 32.0
    assert budgets["A"]["budget_yuan"] == 0.0


def test_guard_enforces_the_sub_budget() -> None:
    guard = spend.Guard(
        stop_line=100.0,
        prices={"m": Price(1, 1, 1, "t")},
        phase_caps={"C": 5.0},
        budget_of=lambda p: phases.budget_of(p),
    )
    guard.reserve(item="x", model="m", cap=4.0, phase="c4")
    guard.reserve(item="y", model="m", cap=50.0, phase="b")  # B 没有分预算上限
    with pytest.raises(spend.SpendStopError, match="C sub-budget"):
        guard.reserve(item="z", model="m", cap=2.0, phase="c5")


def test_pilot_quote_and_units(tmp_path: Path) -> None:
    pilot = _quote(tmp_path, "--pilot", "--single-vendor", "glm", "--stop-line", "30")
    full = _quote(tmp_path, "--single-vendor", "glm", "--stop-line", "30")
    assert pilot["pilot"] is True and pilot["quote_id"] != full["quote_id"]
    assert pilot["planned"]["total"]["yuan_high"] < 30
    assert all(g.startswith("pilot-") for g in pilot["planned"]["groups"])
    assert pilot["status"] == "within_cap"
    units = phases.build_units(phases.PHASES["b"], pilot)
    assert len(units) == plan.PILOT_UNITS_PER_PHASE and {u["vendor"] for u in units} == {"glm"}
    assert len({u["cls"] for u in units}) == 2  # 不同的类
    assert len(phases.build_units(phases.PHASES["c4"], pilot)) == plan.PILOT_UNITS_PER_PHASE


def test_pilot_tokens_feed_the_token_table(tmp_path: Path) -> None:
    out = tmp_path / "pilot"
    (out / "a").mkdir(parents=True)
    a_rec = {
        "key": "k",
        "result": {
            "rounds": [
                {
                    "calls": [
                        {"kind": "pass1", "input_tokens": 900, "output_tokens": 100},
                        {"kind": "pass2", "input_tokens": 1500, "output_tokens": 300},
                        {"kind": "reconcile", "input_tokens": 700, "output_tokens": 50},
                    ]
                }
            ]
        },
    }
    (out / "a" / "records.jsonl").write_text(json.dumps(a_rec) + "\n")
    (out / "b").mkdir()
    b_rec = {
        "key": "k",
        "convs": [
            {
                "steps": 2,
                "step_input_tokens": [5000, 7000],
                "usage_by_model": [{"model": "glm-5.3", "output_tokens": 600}],
            }
        ],
    }
    (out / "b" / "records.jsonl").write_text(json.dumps(b_rec) + "\n")
    (out / "b" / "verdicts.jsonl").write_text(
        json.dumps({"key": "v", "usage": [{"input_tokens": 1200, "output_tokens": 90}]}) + "\n"
    )
    table = runs.pilot_tokens(out)
    assert table["main"] == {"low": [6000, 300], "high": [7000, 300]}
    assert table["extract"]["high"] == [1500, 300] and table["reconcile"]["low"] == [700, 50]
    assert table["judge"] == {"low": [1200, 90], "high": [1200, 90]}
    loaded = plan.load_token_overrides(table)  # 直接能喂给 --tokens
    assert loaded["main"] == ((6000, 300), (7000, 300))


# --------------------------------------------------------------------------- M5


def test_every_seed_is_well_inside_the_30_day_windows() -> None:
    ages = [seed.days_ago for s in sc.build_all() for seed in s.seeds]
    assert max(ages) <= sc.MAX_SEED_DAYS_AGO <= 20


@pytest.mark.asyncio
async def test_preflight_reports_switches_only() -> None:
    import b300_inpod as inpod

    class _Cfg:
        memory_purge_enabled = False
        memory_purge_min_age_days = 30
        memory_predictive_review_enabled = False

    class _Svc:
        async def get(self, *, tenant_id: Any) -> Any:
            return _Cfg()

    deps = inpod.Deps(
        memory=None,
        users=None,
        run_events=None,
        embedder=None,
        consolidator_factory=lambda n: (None, None),
        consolidation_model="m",
        tenant_config=_Svc(),
        consolidation_interval_s=14400.0,
    )
    out: list[str] = []
    await inpod.dispatch(
        deps, {"op": "preflight", "tenant_id": "11111111-1111-4111-8111-111111111111"}, out.append
    )
    (row,) = [json.loads(x) for x in out]
    assert row["ok"] is True and row["consolidation_switch_per_tenant"] is False
    assert row["consolidation_interval_s"] == 14400.0
    _Cfg.memory_purge_enabled = True
    out.clear()
    await inpod.dispatch(
        deps, {"op": "preflight", "tenant_id": "11111111-1111-4111-8111-111111111111"}, out.append
    )
    assert json.loads(out[0])["ok"] is False


def test_conversation_runs_need_a_passing_preflight(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--stop-line", "100")
    out: list[str] = []
    code = cli.main(
        [
            "run",
            "--phase",
            "b",
            "--out",
            str(tmp_path / "out"),
            "--approved-quote",
            q["quote_id"],
            "--stop-line",
            "100",
            "--accept-documented-prices",
            "--state-dir",
            str(tmp_path / "state"),
        ],
        emit=out.append,
    )
    assert code == 2 and "preflight" in out[-1]


def test_c5_consolidated_before_our_step_is_excluded(tmp_path: Path) -> None:
    s = next(x for x in sc.build_group("C") if x.cls == "C5")
    rec = {
        "phase": "c5",
        "scenario_id": s.scenario_id,
        "cls": "C5",
        "domain": s.domain,
        "vendor": "glm",
        "repeat": 0,
        "attempt": 0,
        "user": "u5",
        "key": "k",
        "seed_ids": {"y": "my"},
        "convs": [{"run_id": "r", "final_text": "Y"}],
    }
    (tmp_path / "c5").mkdir()
    (tmp_path / "c5" / "records.jsonl").write_text(json.dumps(rec) + "\n")
    (tmp_path / "c5" / "inpod-recalled.jsonl").write_text(
        json.dumps({"run_id": "r", "memory_ids": ["my"]}) + "\n"
    )
    (tmp_path / "c5" / "inpod-candidates.jsonl").write_text(
        json.dumps({"key": "u5", "clusters": [], "already_consolidated": True}) + "\n"
    )
    items, facts = cli.judge_items(tmp_path, phases.PHASES["c5"])
    assert items == [] and facts["k"]["precondition"] is False


# --------------------------------------------------------------------------- LOW


def test_errored_units_resume_in_place_not_as_new_attempts() -> None:
    units = [
        {
            "phase": "b",
            "scenario_id": "S",
            "cls": "B1",
            "domain": "legal",
            "vendor": "glm",
            "repeat": 0,
            "attempt": 0,
            "user": "u",
        }
    ]
    records = {
        "k": {**units[0], "key": "k", "error": "boom"},
        "k2": {**units[0], "key": "k2", "repeat": 1},
    }
    facts = {"k2": {"recall_ok": False}}
    added = steps.rerun_units(units, records, facts, quote_id="q" * 12, shared_user=False)
    assert [(a["repeat"], a["attempt"]) for a in added] == [(1, 1)]
    again = steps.rerun_units(
        [*units, *added], records, facts, quote_id="q" * 12, shared_user=False
    )
    assert again == []  # 已经加过的不再加


def test_user_said_is_recorded_for_every_spending_command(tmp_path: Path) -> None:
    phases.record_approval(tmp_path, cmd="judge b", quote_id="q", user_said="go ahead")
    phases.record_approval(tmp_path, cmd="calibrate a", quote_id="q", user_said=None)
    rows = [json.loads(x) for x in (tmp_path / "approvals.jsonl").read_text().splitlines()]
    assert [r["cmd"] for r in rows] == ["judge b"] and rows[0]["user_said"] == "go ahead"


def test_consolidation_bundle_run_twice_is_charged_and_stops(tmp_path: Path) -> None:
    (tmp_path / "c5").mkdir()
    guard = spend.Guard.resume(
        stop_line=50.0,
        prices={"glm-5.3-flash": Price(0.8, 2.8, 0.23, "t")},
        state_dir=tmp_path / "state",
        quote_id="q",
    )
    call = guard.reserve(item="c5|consolidate", model="glm-5.3-flash", cap=5.0, phase="c5")
    (tmp_path / "c5" / "consolidate-reserve.jsonl").write_text(
        json.dumps({"nonce": "n1", "call_id": call, "cap": 5.0, "max_calls": 3}) + "\n"
    )
    total = {
        "op": "consolidate_total",
        "nonce": "n1",
        "calls": 2,
        "unparsed_calls": 1,
        "input_tokens": 1_000_000,
        "output_tokens": 0,
    }
    first = steps.settle_consolidation(tmp_path, "c5", [total], guard)
    retry_cap = 2 * steps._consolidation_call_cap(guard.prices["glm-5.3-flash"])
    assert first == pytest.approx(0.8 + retry_cap)  # 没过校验的那次按 2 次重试的上限补记
    assert guard.blocked is None
    steps.settle_consolidation(tmp_path, "c5", [total], guard)  # 同一个 nonce 又来一次
    assert "ran more than once" in (guard.blocked or "")


def test_quote_shows_language_mix_embeddings_and_unpriced(tmp_path: Path) -> None:
    q = _quote(tmp_path)
    assert q["languages"]["cycle"] == list(sc.LANG_CYCLE)
    assert sum(q["languages"]["scenarios_by_language"].values()) == 235
    assert q["embeddings"]["in_ledger"] is False and "not priced" in q["embeddings"]["yuan"]
    priced = _quote(tmp_path, "--embed-price", "0.5")
    assert isinstance(priced["embeddings"]["yuan"], list)
    assert any("sandbox" in n for n in q["not_priced"])
