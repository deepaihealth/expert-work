"""B-300 命令行的离线测试:报价编号核对、¥400 总账、单元、版本记录、判分材料与报告串起来。
不连网、不调模型(用到的价目全是测试里给的)。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import b300_eval as cli
import b300_plan as plan
import b300_scenarios as sc
import b300_spend as spend
import pytest
import yaml


def _main(*argv: str) -> tuple[int, list[str]]:
    out: list[str] = []
    code = cli.main(list(argv), emit=out.append)
    return code, out


def _quote(tmp_path: Path, *extra: str) -> dict[str, Any]:
    code, out = _main(
        "quote", "--accept-documented-prices", "--state-dir", str(tmp_path / "state"), *extra
    )
    assert code == 0, out
    return json.loads(out[0])


def test_scenarios_command(tmp_path: Path) -> None:
    code, out = _main("scenarios", "--out", str(tmp_path / "s.json"))
    assert code == 0
    summary = json.loads(out[0])
    assert summary["scenarios"] == 235 and summary["problems"] == []
    assert len(json.loads((tmp_path / "s.json").read_text())) == 235


def test_quote_needs_documented_price_flag(tmp_path: Path) -> None:
    code, out = _main("quote", "--state-dir", str(tmp_path))
    assert code == 2 and "accept-documented-prices" in out[0]


def test_quote_has_groups_and_cut_status(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--single-vendor", "glm", "--stop-line", "350")
    assert q["single_vendor"] == "glm" and q["stop_line_yuan"] == 350.0
    assert q["status"] in {"within_cap", "stop_and_ask"}
    assert {"A", "B", "C-C4"} <= set(q["planned"]["groups"])


def _prepare(tmp_path: Path, phase: str, q: dict[str, Any], *extra: str) -> tuple[int, list[str]]:
    return _main(
        "prepare",
        "--phase",
        phase,
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
    )


def test_prepare_checks_the_quote_id(tmp_path: Path) -> None:
    code, out = _main(
        "prepare",
        "--phase",
        "b",
        "--out",
        str(tmp_path),
        "--tenant-id",
        "t",
        "--approved-quote",
        "nope",
        "--accept-documented-prices",
        "--state-dir",
        str(tmp_path),
    )
    assert code == 2 and "does not match" in out[0]


def test_prepare_b_writes_units_and_a_seed_bundle(tmp_path: Path) -> None:
    q = _quote(tmp_path)
    code, out = _prepare(tmp_path, "b", q)
    assert code == 0, out
    units = json.loads((tmp_path / "out" / "b" / "units.json").read_text())
    assert len(units) == 7 * 10 * 3 * 4
    assert len({u["user"] for u in units}) == len(units)  # 每次一个新用户,种子互不串
    bundle = (tmp_path / "out" / "b" / "bundle-seed.py").read_text()
    assert "EMBEDDED_INPUT: Mapping[str, Any] | None = json.loads(" in bundle
    code, out = _prepare(tmp_path, "b", q)
    assert code == 2 and "--reruns" in out[0]


def test_single_vendor_phases_follow_the_cuts(tmp_path: Path) -> None:
    q = _quote(tmp_path, "--single-vendor", "doubao")
    cuts = {c["step"] for c in q["cuts_applied"]}
    units = cli.build_units(cli.PHASES["b-screen"], q)
    vendors = {u["vendor"] for u in units}
    assert vendors == ({"doubao"} if 2 in cuts else {"glm", "deepseek", "qwen", "doubao"})
    c_units = cli.build_units(cli.PHASES["c"], q)
    c13 = {u["vendor"] for u in c_units if u["cls"] in {"C1", "C3"}}
    assert c13 == ({"doubao"} if 3 in cuts else {"glm", "deepseek", "qwen", "doubao"})
    assert {u["vendor"] for u in cli.build_units(cli.PHASES["c5-baseline"], q)} == {"doubao"}


def test_c5_users_are_shared_across_vendors(tmp_path: Path) -> None:
    q = _quote(tmp_path)
    units = cli.build_units(cli.PHASES["c5"], q)
    assert len(units) == 5 * 3 * 4 and len({u["user"] for u in units}) == 15


def _cheap_quote_args(tmp_path: Path, q_extra: tuple[str, ...] = ()) -> list[str]:
    return ["--accept-documented-prices", "--state-dir", str(tmp_path / "state"), *q_extra]


def test_run_refuses_stop_and_ask_without_the_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plan, "CAP_YUAN", 1.0)  # 让报价一定是 stop_and_ask
    q = _quote(tmp_path, "--stop-line", "0.5")
    assert q["status"] == "stop_and_ask"
    args = [
        "run",
        "--phase",
        "a",
        "--out",
        str(tmp_path / "out"),
        "--approved-quote",
        q["quote_id"],
        "--stop-line",
        "0.5",
        *_cheap_quote_args(tmp_path),
    ]
    code, out = _main(*args)
    assert code == 2 and "stop_and_ask" in out[0]


def test_stop_line_cannot_cross_the_cap_with_history(tmp_path: Path) -> None:
    state = tmp_path / "state"
    old = spend.Guard.resume(
        stop_line=400.0, prices={"m": plan.Price(1, 1, 1, "t")}, state_dir=state, quote_id="old"
    )
    call = old.reserve(item="x", model="m", cap=350.0, phase="p")
    old.settle(call, item="x", model="m", phase="p", cost=350.0)
    q = _quote(tmp_path, "--stop-line", "100")
    assert q["already_spent_yuan"] == 350.0 and q["status"] == "stop_and_ask"
    code, out = _main(
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
        "go",
        *_cheap_quote_args(tmp_path),
    )
    assert code == 2 and "cross the ¥400 cap" in out[0]


def test_stop_line_argument_is_bounded() -> None:
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["quote", "--stop-line", "401"])


def test_release_meta() -> None:
    with pytest.raises(cli.EvalError, match="release-tag"):
        cli.release_meta(cli.PHASES["b"], None)
    meta = cli.release_meta(cli.PHASES["b-screen"], "f2a21c6b2-test")
    assert meta["release_commit"] == "f2a21c6b2" and meta["baseline"] is True
    assert cli.release_meta(cli.PHASES["a"], None)["release_tag"] is None


def test_write_meta_refuses_a_different_release(tmp_path: Path) -> None:
    phase = cli.PHASES["b"]
    (tmp_path / "b").mkdir()
    cli.write_meta(tmp_path, phase, {"release_tag": "aaa", "script_commit": "c"})
    cli.write_meta(tmp_path, phase, {"release_tag": "aaa", "script_commit": "c"})
    with pytest.raises(cli.EvalError):
        cli.write_meta(tmp_path, phase, {"release_tag": "bbb", "script_commit": "c"})


def test_manifests_validate_against_the_protocol(tmp_path: Path) -> None:
    from expert_work.protocol.agent_spec import AgentSpec

    paths = cli.write_manifests(tmp_path, plan.DEFAULT_VENDORS)
    assert len(paths) == 8
    for path in paths:
        spec = AgentSpec.model_validate(yaml.safe_load(path.read_text()))
        long_term = spec.spec.memory.long_term
        assert long_term is not None and long_term.write_back is path.stem.endswith("-wb")


def test_calibration_labels_shape() -> None:
    good = [{"domain": d, "label": i % 2 == 0, "prompt": "p"} for d in sc.DOMAINS for i in range(4)]
    cli.check_labels(good)
    with pytest.raises(cli.EvalError):
        cli.check_labels(good[:-1])
    with pytest.raises(cli.EvalError):
        cli.check_labels([{**r, "label": True} for r in good])


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def test_conversation_judge_items_and_report(tmp_path: Path) -> None:
    """B1 一条:召回到了 → 要判;没召回到 → 不计入。判完进报告的 B1 线。"""
    out = tmp_path
    s = next(x for x in sc.build_group("B") if x.cls == "B1")
    base = {
        "phase": "b",
        "scenario_id": s.scenario_id,
        "cls": "B1",
        "domain": s.domain,
        "vendor": "glm",
        "repeat": 0,
        "attempt": 0,
        "user": "u0",
        "seed_ids": {"x": "m1"},
    }
    records = [
        {**base, "key": "k0", "convs": [{"run_id": "r0", "final_text": "answer"}]},
        {
            **base,
            "key": "k1",
            "repeat": 1,
            "user": "u1",
            "convs": [{"run_id": "r1", "final_text": "answer"}],
        },
    ]
    _write(out / "b" / "records.jsonl", records)
    _write(
        out / "b" / "inpod-recalled.jsonl",
        [{"run_id": "r0", "memory_ids": ["m1"]}, {"run_id": "r1", "memory_ids": []}],
    )
    items, facts = cli.judge_items(out, cli.PHASES["b"])
    assert [i.key for i in items] == ["k0|answer"]
    assert facts["k1"]["recall_ok"] is False
    _write(
        out / "b" / "verdicts.jsonl", [{"key": "k0|answer", "verdict": False, "votes": [False] * 3}]
    )
    body = cli.report(out, None)
    b1 = body["lines"]["B1.violation"]["glm"]
    assert b1["n"] == 1 and b1["verdict"] == "incomplete"
    assert body["counts"]["b"]["not_recalled_excluded"] == 1


def test_bundle_payloads(tmp_path: Path) -> None:
    q = _quote(tmp_path)
    units = cli.build_units(cli.PHASES["c5"], q)
    seed = cli.seed_payload(cli.PHASES["c5"], units, "t")
    assert len(seed["entries"]) == 15 and seed["entries"][0]["seeds"][0]["superseded_by"] == "y"
    state = cli.users_payload("state", units, "t")
    assert len(state["users"]) == 15 and state["users"][0]["x_key"]


def _c5_records(out: Path, phase: str, *, summary: str | None) -> None:
    s = next(x for x in sc.build_group("C") if x.cls == "C5")
    seed_ids = {"x": "mx", "y": "my", "near1": "n1", "near2": "n2"}
    rows = [
        {
            "phase": phase,
            "scenario_id": s.scenario_id,
            "cls": "C5",
            "domain": s.domain,
            "vendor": v,
            "repeat": 0,
            "attempt": 0,
            "user": "u5",
            "key": f"k-{v}",
            "seed_ids": seed_ids,
            "convs": [{"run_id": f"r-{v}", "final_text": "Y"}],
        }
        for v in ("glm", "qwen")
    ]
    _write(out / phase / "records.jsonl", rows)
    _write(
        out / phase / "inpod-recalled.jsonl",
        [{"run_id": f"r-{v}", "memory_ids": ["my"]} for v in ("glm", "qwen")],
    )
    _write(
        out / phase / "inpod-candidates.jsonl", [{"key": "u5", "clusters": [["mx", "n1", "n2"]]}]
    )
    state = [{"id": "s", "summary": True, "content": summary}] if summary else []
    _write(out / phase / "inpod-state.jsonl", [{"key": "u5", "rows": state}])


def test_c5_summary_is_judged_once_per_user(tmp_path: Path) -> None:
    _c5_records(tmp_path, "c5", summary="It changed from X to Y.")
    items, facts = cli.judge_items(tmp_path, cli.PHASES["c5"])
    assert sorted(i.key for i in items) == ["k-glm|answer", "k-qwen|answer", "u5|summary"]
    assert facts["k-glm"]["x_in_candidates"] is True
    assert facts["k-glm"]["region_clustered"] is True
    verdicts = [
        {"key": "u5|summary", "verdict": True},
        {"key": "k-glm|answer", "verdict": False},
        {"key": "k-qwen|answer", "verdict": False},
    ]
    _write(tmp_path / "c5" / "verdicts.jsonl", verdicts)
    body = cli.report(tmp_path, None)
    summary = body["lines"]["C5.summary_violation"]["consolidation"]
    assert summary["n"] == 1 and summary["bad"] == 1
    assert "待用户确认" in body["lines"]["C5.violation"]["glm"]["line"]["status"]
    assert body["counts"]["c5"]["x_in_candidates"] == 1


def test_c5_without_summary_rows_is_not_a_violation(tmp_path: Path) -> None:
    _c5_records(tmp_path, "c5-baseline", summary=None)
    items, _facts = cli.judge_items(tmp_path, cli.PHASES["c5-baseline"])
    assert not [i for i in items if i.check == "summary"]
    body = cli.report(tmp_path, None)
    red = body["baseline_prove_red"]["c5-baseline"]["summary"]
    assert red["replace"] and not red["keep"]  # 基线一次都没红:先看前提 / 换场景(§8.7)


def test_c4_is_judged_only_when_the_precondition_holds(tmp_path: Path) -> None:
    s = next(x for x in sc.build_group("C") if x.cls == "C4")
    convs = [
        {"run_id": "a", "session_id": "t1"},
        {"run_id": "b", "session_id": "t2"},
        {"run_id": "c", "final_text": "Y"},
    ]
    rows = [
        {
            "phase": "c4",
            "scenario_id": s.scenario_id,
            "cls": "C4",
            "domain": s.domain,
            "vendor": "glm",
            "repeat": i,
            "attempt": 0,
            "user": f"u{i}",
            "key": f"k{i}",
            "seed_ids": {},
            "convs": convs,
        }
        for i in range(2)
    ]
    _write(tmp_path / "c4" / "records.jsonl", rows)
    wrote_x = [{"source_thread_id": "t1", "has_x": True}]
    _write(
        tmp_path / "c4" / "inpod-state-step1.jsonl",
        [{"key": "u0", "rows": wrote_x}, {"key": "u1", "rows": wrote_x}],
    )
    _write(
        tmp_path / "c4" / "inpod-state-step2.jsonl",
        [
            {"key": "u0", "rows": [{"source_thread_id": "t2", "has_y": True}]},
            {"key": "u1", "rows": wrote_x},  # X 没被取代:前提不成立
        ],
    )
    items, facts = cli.judge_items(tmp_path, cli.PHASES["c4"])
    assert [i.key for i in items] == ["k0|answer"]
    assert facts["k1"]["precondition"] is False
