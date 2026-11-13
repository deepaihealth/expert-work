"""B-300 评测脚本第二轮审查的修复(HIGH、M-a、M-b、M-c 与 LOW):离线。"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import b300_eval as cli
import b300_inpod as inpod
import b300_live as live
import b300_phases as phases
import b300_plan as plan
import b300_runs as runs
import b300_scenarios as sc
import b300_spend as spend
import pytest
from b301_plan import Price

FLASH = {"glm-5.3-flash": Price(0.8, 2.8, 0.23, "t"), "glm-5.3": Price(8.0, 28.0, 2.0, "t")}


def _quote(tmp_path: Path, *extra: str) -> dict[str, Any]:
    out: list[str] = []
    code = cli.main(
        ["quote", "--accept-documented-prices", "--state-dir", str(tmp_path / "state"), *extra],
        emit=out.append,
    )
    assert code == 0, out
    return json.loads(out[0])


# --------------------------------------------------------------------------- HIGH


def test_run_start_times_are_kept_and_the_earliest_wins(tmp_path: Path) -> None:
    phase = phases.PHASES["c4"]
    (tmp_path / "c4").mkdir()
    base = {"release_tag": "t", "script_commit": "c"}
    for step, at in (
        (1, "2026-11-13T08:00:00+00:00"),
        (2, "2026-11-13T09:00:00+00:00"),
        (3, "2026-11-13T10:00:00+00:00"),
    ):
        phases.write_meta(tmp_path, phase, {**base, "recorded_at": at}, step=step)
    meta = json.loads((tmp_path / "c4" / "meta.json").read_text())
    assert [s["step"] for s in meta["starts"]] == [1, 2, 3]
    assert meta["recorded_at"] == "2026-11-13T08:00:00+00:00"  # 不被后面的 run 覆盖
    assert phases.earliest_start(tmp_path, "c4") == "2026-11-13T08:00:00+00:00"


@pytest.mark.asyncio
async def test_inpod_reports_writeback_job_status_per_run() -> None:
    from expert_work.persistence import InMemoryTenantUserStore
    from expert_work.persistence.token_usage_store import InMemoryTokenUsageStore

    statuses = {
        "00000000-0000-4000-8000-000000000001": "done",
        "00000000-0000-4000-8000-000000000002": "running",
    }

    class _Jobs:
        async def get_by_run(self, *, tenant_id: Any, run_id: Any) -> Any:
            status = statuses.get(str(run_id))
            return SimpleNamespace(status=status) if status else None

    deps = inpod.Deps(
        memory=None,
        users=InMemoryTenantUserStore(),
        run_events=None,
        embedder=None,
        consolidator_factory=lambda n: (None, None),
        consolidation_model="m",
        token_usage=InMemoryTokenUsageStore(),
        writeback_jobs=_Jobs(),
    )
    out: list[str] = []
    run_ids = [*statuses, "00000000-0000-4000-8000-000000000003"]
    await inpod.dispatch(
        deps,
        {
            "op": "writeback_usage",
            "since": "2026-11-13T00:00:00+00:00",
            "users": [
                {
                    "key": "u",
                    "tenant_id": "11111111-1111-4111-8111-111111111111",
                    "user": "b300-u",
                    "agent_name": "eval-b300-glm-wb",
                    "run_ids": run_ids,
                }
            ],
        },
        out.append,
    )
    (row,) = [json.loads(x) for x in out]
    assert row["jobs"] == {run_ids[0]: "done", run_ids[1]: "running", run_ids[2]: None}
    assert row["models"] == []


def _records_with_reservations(
    out: Path, guard: spend.Guard, run_ids: list[str | None]
) -> list[str]:
    (out / "c4").mkdir(parents=True, exist_ok=True)
    reservations = []
    for n, run_id in enumerate(run_ids):
        call = live.reserve_writeback(guard, item=f"k|c{n}", model="glm-5.3-flash", phase="c4")
        reservations.append({"call_id": call, "conv": n, "run_id": run_id})
    row = {"key": "k", "user": "u", "writeback_reservations": reservations, "convs": []}
    (out / "c4" / "records.jsonl").write_text(json.dumps(row) + "\n")
    return [r["call_id"] for r in reservations]


_USAGE = [{"model": "glm-5.3-flash", "input_tokens": 1_000_000, "output_tokens": 0}]


@pytest.mark.parametrize(
    ("jobs", "models", "settles"),
    [
        ({"r1": "done", "r2": "done"}, _USAGE, True),
        ({"r1": "done", "r2": "running"}, _USAGE, False),  # 步 2 的写回还没跑完
        ({"r1": "done", "r2": "pending"}, _USAGE, False),
        ({"r1": "done"}, _USAGE, False),  # 找不到步 2 的写回任务
        ({"r1": "done", "r2": "done"}, [], False),  # 用量读不到:不按 0 结
    ],
)
def test_writeback_settles_only_when_every_job_finished_and_usage_is_there(
    tmp_path: Path, jobs: dict[str, str], models: list[Any], settles: bool
) -> None:
    guard = spend.Guard.resume(
        stop_line=100.0, prices=FLASH, state_dir=tmp_path / "s", quote_id="q"
    )
    calls = _records_with_reservations(tmp_path, guard, ["r1", "r2"])
    report = {"op": "writeback_usage", "key": "u", "jobs": jobs, "models": models}
    result = runs.settle_writeback_usage(tmp_path, "c4", [report], guard)
    cap = live.writeback_cap_yuan(FLASH["glm-5.3-flash"])
    if settles:
        assert result["settled_users"] == 1 and guard.spent == pytest.approx(0.8)
    else:
        assert result["waiting_users"] == 1 and guard.settled == 0
        assert guard.spent == pytest.approx(2 * cap) and all(guard.open_cap(c) > 0 for c in calls)


def test_a_broken_conversation_keeps_its_writeback_reservation_unsettled(tmp_path: Path) -> None:
    guard = spend.Guard.resume(
        stop_line=100.0, prices=FLASH, state_dir=tmp_path / "s", quote_id="q"
    )
    (call,) = _records_with_reservations(tmp_path, guard, [None])
    report = {"op": "writeback_usage", "key": "u", "jobs": {}, "models": _USAGE}
    runs.settle_writeback_usage(tmp_path, "c4", [report], guard)
    assert guard.open_cap(call) > 0 and guard.settled == 0  # 不知道建没建 run:留着,按上限算


# --------------------------------------------------------------------------- M-a


def _c4_setup(tmp_path: Path) -> tuple[dict[str, Any], Any]:
    s = next(x for x in sc.build_group("C") if x.cls == "C4")
    unit = {
        "phase": "c4",
        "scenario_id": s.scenario_id,
        "cls": "C4",
        "domain": s.domain,
        "vendor": "glm",
        "repeat": 0,
        "attempt": 0,
        "user": "u",
    }
    (tmp_path / "c4").mkdir(parents=True, exist_ok=True)
    return unit, plan.Vendor("glm", "glm-5.3", "glm-5.3-flash")


def _run_c4(tmp_path: Path, guard: spend.Guard, unit: dict[str, Any], vendor: Any) -> list[str]:
    emitted: list[str] = []
    asyncio.run(
        runs.run_conversation_phase(
            phases.PHASES["c4"],
            [unit],
            out=tmp_path,
            guard=guard,
            vendors={"glm": vendor},
            base_url="https://cp",
            api_key="k",
            step=1,
            emit=emitted.append,
        )
    )
    return emitted


def test_writeback_is_reserved_before_the_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M-a:写回的预留被拒 → 这段对话根本不开(没花钱、不留半截)。"""
    unit, vendor = _c4_setup(tmp_path)
    started: list[str] = []

    async def fake_conversation(*_a: Any, **_k: Any) -> dict[str, Any]:
        started.append("conv")
        return {"run_id": "r1", "session_id": "s1", "final_text": "", "usage_by_model": []}

    monkeypatch.setattr(live, "run_conversation", fake_conversation)
    cap = live.writeback_cap_yuan(FLASH["glm-5.3-flash"])
    guard = spend.Guard(stop_line=cap / 2, prices=FLASH)  # 连写回的预留都放不下
    _run_c4(tmp_path, guard, unit, vendor)
    assert started == []
    assert not (tmp_path / "c4" / "records.jsonl").exists()


def test_a_failed_conversation_still_records_its_writeback_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit, vendor = _c4_setup(tmp_path)

    async def broken(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("stream broke")

    monkeypatch.setattr(live, "run_conversation", broken)
    guard = spend.Guard(stop_line=100.0, prices=FLASH)
    _run_c4(tmp_path, guard, unit, vendor)
    (row,) = [json.loads(x) for x in (tmp_path / "c4" / "records.jsonl").read_text().splitlines()]
    assert row["error"] and len(row["writeback_reservations"]) == 1
    assert guard.open_cap(row["writeback_reservations"][0]["call_id"]) > 0


# --------------------------------------------------------------------------- M-b


def test_quote_covers_only_the_named_phases(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--phases", "b")
    assert q["phases"] == ["B"] and set(q["phase_budgets"]) == {"B"}
    assert {plan.budget_of_group(g) for g in q["planned"]["groups"]} == {"B"}
    assert q["phase_budgets"]["B"]["budget_yuan"] == q["stop_line_yuan"]
    full = _quote(tmp_path)
    budgets = full["phase_budgets"]
    assert set(budgets) == {"B", "C", "A"} and all(b["budget_yuan"] > 0 for b in budgets.values())
    assert sum(b["budget_yuan"] for b in budgets.values()) == pytest.approx(
        full["stop_line_yuan"], abs=0.05
    )


def test_sub_budget_breach_stops_only_that_phase_and_is_not_persisted(tmp_path: Path) -> None:
    guard = spend.Guard.resume(
        stop_line=100.0,
        prices={"m": Price(1, 1, 1, "t")},
        state_dir=tmp_path,
        quote_id="q",
        phase_caps={"C": 5.0, "B": 50.0},
        budget_of=phases.budget_of,
    )
    guard.reserve(item="x", model="m", cap=4.0, phase="c4")
    with pytest.raises(spend.SpendStopError, match="only this phase"):
        guard.reserve(item="y", model="m", cap=2.0, phase="c5")
    assert guard.blocked is None and not guard.may_spend("c") and guard.may_spend("b")
    guard.reserve(item="z", model="m", cap=10.0, phase="b")  # 别的段照常
    rows = [json.loads(x) for x in spend.ledger_path(tmp_path, "q").read_text().splitlines()]
    assert not [r for r in rows if r["reason"] == "blocked"]
    resumed = spend.Guard.resume(
        stop_line=100.0, prices={"m": Price(1, 1, 1, "t")}, state_dir=tmp_path, quote_id="q"
    )
    assert resumed.blocked is None


def test_a_phase_outside_the_quote_is_refused(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--phases", "b", "--stop-line", "100")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "preflight.json").write_text(json.dumps({"ok": True}))
    out: list[str] = []
    code = cli.main(
        [
            "run",
            "--phase",
            "c",
            "--out",
            str(tmp_path / "out"),
            "--approved-quote",
            q["quote_id"],
            "--phases",
            "b",
            "--stop-line",
            "100",
            "--accept-documented-prices",
            "--user-said",
            "go ahead with B only",
            "--state-dir",
            str(tmp_path / "state"),
        ],
        emit=out.append,
    )
    assert code == 2 and "not covered" in out[-1]


# --------------------------------------------------------------------------- M-c


def _prepare(tmp_path: Path, q: dict[str, Any], *extra: str) -> int:
    out: list[str] = []
    return cli.main(
        [
            "prepare",
            "--phase",
            "b",
            "--out",
            str(tmp_path / "out"),
            "--tenant-id",
            "11111111-1111-4111-8111-111111111111",
            "--approved-quote",
            q["quote_id"],
            "--accept-documented-prices",
            "--state-dir",
            str(tmp_path / "state"),
            *extra,
        ],
        emit=out.append,
    )


def test_units_are_bound_to_their_quote(tmp_path: Path) -> None:
    q1 = _quote(tmp_path, "--stop-line", "100")
    assert _prepare(tmp_path, q1, "--stop-line", "100") == 0
    with pytest.raises(phases.EvalError, match="prepared under quote"):
        phases.load_units(tmp_path / "out", "b", "other-quote")
    assert phases.load_units(tmp_path / "out", "b", q1["quote_id"])


def test_pilot_stop_line_defaults_to_its_high_estimate(tmp_path: Path) -> None:
    import math

    pilot = _quote(tmp_path, "--pilot", "--single-vendor", "glm")
    assert pilot["stop_line_yuan"] == math.ceil(pilot["planned"]["total"]["yuan_high"])
    assert pilot["stop_line_yuan"] < 40
    assert _quote(tmp_path)["stop_line_yuan"] == plan.CAP_YUAN


def test_pilot_quote_cannot_run_full_units(tmp_path: Path) -> None:
    full = _quote(tmp_path, "--stop-line", "100")
    assert _prepare(tmp_path, full, "--stop-line", "100") == 0
    pilot = _quote(tmp_path, "--pilot", "--single-vendor", "glm")
    with pytest.raises(phases.EvalError, match="pilot=False"):
        phases.load_units(tmp_path / "out", "b", pilot["quote_id"])


# --------------------------------------------------------------------------- LOW


@pytest.mark.parametrize(
    "argv",
    [
        ["judge", "--phase", "b", "--stop-line", "100"],
        ["calibrate", "--labels", "LABELS", "--stop-line", "100"],
        [
            "bundle",
            "--phase",
            "c5",
            "--op",
            "consolidate",
            "--tenant-id",
            "t",
            "--stop-line",
            "100",
        ],
        ["ingest", "--phase", "c5", "--op", "consolidate", "--file", "FILE", "--stop-line", "100"],
        [
            "ingest",
            "--phase",
            "c4",
            "--op",
            "writeback_usage",
            "--file",
            "FILE",
            "--stop-line",
            "100",
        ],
    ],
)
def test_every_spending_command_refuses_while_the_lock_is_held(
    tmp_path: Path, argv: list[str]
) -> None:
    q = _quote(tmp_path, "--stop-line", "100")
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        "".join(
            json.dumps({"domain": d, "label": i % 2 == 0, "prompt": "p"}) + "\n"
            for d in sc.DOMAINS
            for i in range(4)
        )
    )
    pod_out = tmp_path / "pod.jsonl"
    pod_out.write_text(json.dumps({"op": "consolidate_total", "nonce": "n"}) + "\n")
    phases.save_units(tmp_path / "out", "c5", [], q)
    argv = [str(labels) if a == "LABELS" else str(pod_out) if a == "FILE" else a for a in argv]
    out: list[str] = []
    with spend.state_lock(tmp_path / "state"):
        code = cli.main(
            [
                *argv,
                "--out",
                str(tmp_path / "out"),
                "--approved-quote",
                q["quote_id"],
                "--accept-documented-prices",
                "--state-dir",
                str(tmp_path / "state"),
            ],
            emit=out.append,
        )
    assert code == 2 and "locked" in out[-1]


def test_consolidation_marker_refuses_a_second_run(tmp_path: Path) -> None:
    inpod.claim_run_marker("n1", tmp_path)
    with pytest.raises(SystemExit, match="already ran"):
        inpod.claim_run_marker("n1", tmp_path)
    with pytest.raises(SystemExit, match="nonce"):
        inpod.claim_run_marker("", tmp_path)


@pytest.mark.asyncio
async def test_preflight_refuses_a_tenant_with_real_users_or_agents() -> None:
    class _Cfg:
        memory_purge_enabled = False
        memory_purge_min_age_days = 30
        memory_predictive_review_enabled = False

    class _Svc:
        async def get(self, *, tenant_id: Any) -> Any:
            return _Cfg()

    class _Users:
        async def list_by_tenant(self, tenant_id: Any, *, limit: int, offset: int) -> list[Any]:
            rows = ["ext:b300-u1", "ext:customer-42", "kc-admin"]
            return [SimpleNamespace(subject_id=s) for s in rows][offset : offset + limit]

    class _Agents:
        async def list_distinct_active_by_tenant(self, *, tenant_id: Any, limit: int) -> list[Any]:
            return [SimpleNamespace(name="eval-b300-glm"), SimpleNamespace(name="health-coach")]

    deps = inpod.Deps(
        memory=None,
        users=_Users(),
        run_events=None,
        embedder=None,
        consolidator_factory=lambda n: (None, None),
        consolidation_model="m",
        tenant_config=_Svc(),
        agent_specs=_Agents(),
    )
    out: list[str] = []
    await inpod.dispatch(
        deps, {"op": "preflight", "tenant_id": "11111111-1111-4111-8111-111111111111"}, out.append
    )
    row = json.loads(out[0])
    assert row["external_users_not_eval"] == 1 and row["agents_not_eval"] == 1
    assert (
        row["console_users"] == 1 and row["ok"] is False and row["looks_like_eval_tenant"] is False
    )


def test_background_guard_catches_new_threads() -> None:
    async def scenario() -> None:
        baseline = inpod.background_now()
        stop = threading.Event()
        worker = threading.Thread(target=stop.wait, name="sneaky-worker", daemon=True)
        worker.start()
        try:
            with pytest.raises(SystemExit, match="sneaky-worker"):
                inpod.assert_no_background_tasks(baseline=baseline)
        finally:
            stop.set()
            worker.join()

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_dispatch_checks_before_and_after_each_op() -> None:
    async def noisy(deps: Any, payload: Any, emit: Any) -> None:
        asyncio.get_running_loop().create_task(asyncio.sleep(1), name="left-behind")

    deps = inpod.Deps(
        memory=None,
        users=None,
        run_events=None,
        embedder=None,
        consolidator_factory=lambda n: (None, None),
        consolidation_model="m",
        baseline=inpod.background_now(),
    )
    original = inpod._HANDLERS["state"]
    inpod._HANDLERS["state"] = noisy
    try:
        with pytest.raises(SystemExit, match="left-behind"):
            await inpod.dispatch(deps, {"op": "state", "users": []}, lambda _l: None)
    finally:
        inpod._HANDLERS["state"] = original
        for task in asyncio.all_tasks():
            if task.get_name() == "left-behind":
                task.cancel()


@pytest.mark.parametrize("words", ["ok", "y", "yes", "  go ", "好的"])
def test_user_said_and_unblock_need_real_words(tmp_path: Path, words: str) -> None:
    with pytest.raises(SystemExit):
        cli.parser().parse_args(
            [
                "run",
                "--phase",
                "a",
                "--out",
                str(tmp_path),
                "--approved-quote",
                "q",
                "--stop-line",
                "10",
                "--user-said",
                words,
            ]
        )
    with pytest.raises(SystemExit):
        cli.parser().parse_args(
            [
                "run",
                "--phase",
                "a",
                "--out",
                str(tmp_path),
                "--approved-quote",
                "q",
                "--stop-line",
                "10",
                "--unblock",
                words,
            ]
        )
    ok = cli.parser().parse_args(
        [
            "run",
            "--phase",
            "a",
            "--out",
            str(tmp_path),
            "--approved-quote",
            "q",
            "--stop-line",
            "10",
            "--user-said",
            "用户说可以先跑 B",
        ]
    )
    assert ok.user_said == "用户说可以先跑 B"
