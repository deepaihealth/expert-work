"""B-300 第 9 版评测接进现有跑法(设计 2026-11-15 §11.1 第 2 步、§11.3、§11.7、§12.5 K-E):新段落、
报价的单元形状、报告里的线与统计力、判官题的种类、本机写回记下读库断言要的东西。离线、不花钱。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import b300_eval as cli
import b300_k_checks as kchk
import b300_k_items as ki
import b300_k_platform as kp
import b300_k_report as krep
import b300_k_scenarios as ks
import b300_live as live
import b300_phases as phases
import b300_plan as plan
import b300_redesign_base as base
import b300_redesign_scenarios as ns
import b300_report as rep
import b300_scenarios as sc
import b300_spend as spend
import httpx
import pytest
from b301_plan import Price
from longmem.embedders import KeywordEmbedder

GLM = ["--vendor", "glm=glm-5.3:glm-5.3-flash", "--single-vendor", "glm"]
K = ks.build_k()


def _quote(tmp_path: Path, *extra: str) -> dict[str, Any]:
    out: list[str] = []
    code = cli.main(
        ["quote", "--accept-documented-prices", "--state-dir", str(tmp_path / "state"), *extra],
        emit=out.append,
    )
    assert code == 0, out
    body: dict[str, Any] = json.loads(out[0])
    return body


# --------------------------------------------------------------------------- 段落与单元


def test_v9_phases_and_their_budgets() -> None:
    k, gate, use = (phases.PHASES[n] for n in ("k", "gate-k", "b4x"))
    assert k.classes == ks.K_CLASSES and k.kind == "extraction" and not k.baseline
    assert gate.classes == ks.GATE_K_CLASSES and gate.repeats == 1
    assert use.classes == ks.USE_CLASSES and use.kind == "conversation"
    assert [phases.budget_of(n) for n in ("k", "gate-k", "b4x")] == ["A", "G", "B"]
    assert phases.GATE_PHASES == ("gate", "gate-k")


def test_k_phase_has_540_units_and_the_gate_takes_40(tmp_path: Path) -> None:
    """§11.2a:540 个单元;§11.1 第 2 步 ①:K-a、K-b、K-n2、K-c′ 各 10 个单元(每个场景 1 次)。"""  # noqa: RUF002
    q = _quote(tmp_path, *GLM, "--phases", "a,g", "--stop-line", "100")
    units = phases.build_units(phases.PHASES["k"], q)
    assert len(units) == 540 and {u["repeat"] for u in units} == {0, 1, 2}
    gate = phases.build_units(phases.PHASES["gate-k"], q)
    assert len(gate) == 40 and {u["repeat"] for u in gate} == {0}
    assert {u["cls"] for u in gate} == set(ks.GATE_K_CLASSES)
    assert len(phases.build_units(phases.PHASES["s"], q)) == 240  # S7 加到 10 个命题


def test_use_side_units_and_their_seed_payload(tmp_path: Path) -> None:
    q = _quote(tmp_path, *GLM, "--phases", "b", "--stop-line", "300")
    units = phases.build_units(phases.PHASES["b4x"], q)
    assert len(units) == 60 and {u["cls"] for u in units} == {"B4c", "B4d"}
    payload = phases.seed_payload(phases.PHASES["b4x"], units, "t-1")
    b4d = next(e for e in payload["entries"] if "b4d" in e["user"])
    summary = next(s for s in b4d["seeds"] if s["key"] == "summary")
    assert summary["summary_of"] == ("a", "x", "c")
    assert next(s for s in b4d["seeds"] if s["key"] == "y")["linked_to"] == "summary"


def test_all_scenarios_know_the_k_and_use_classes() -> None:
    known = phases.all_scenarios()
    assert all(s.scenario_id in known for s in [*K, *ks.build_use()])


# --------------------------------------------------------------------------- 报价的单元形状


def test_k_unit_shape_counts_what_the_design_lists() -> None:
    """§11.7 第 4 步的表:K 组 540 个单元;判官 K-b、K-b2、K-p、K-late、K-s(两问)各一票。"""
    n = plan.k_unit_shape()
    assert n["units"] == 540 and n["gate_units"] == 40
    assert n["writebacks"] == sum(len(s.writebacks) for s in K) * 3 == 690
    assert n["judge_high"] == 60 + 30 + 15 + 2 * 15
    assert n["judge_low"] == 60 * plan.GROUNDED_SHARE[0]
    assert (n["rewrite_low"], n["rewrite_high"]) == (15, 30)
    assert n["dated_low"] == 6
    assert n["use_conversations"] == 20 * 3 * plan.RERUN_FACTOR


def test_k_group_is_priced_in_the_a_budget(tmp_path: Path) -> None:
    q = _quote(tmp_path, *GLM, "--phases", "a", "--stop-line", "100", "--no-compare")
    kinds = q["planned"]["groups"]["K"]["by_vendor"]["glm"]["calls_by_kind"]
    assert kinds["extract@glm-5.3-flash"] == [1380.0, 1380.0]  # 690 次写回 x 两次提炼
    assert kinds["reconcile@glm-5.3-flash"] == [0.0, 1380.0]
    assert kinds["consolidate@glm-5.3-flash"] == [15.0, 30.0]  # K-s 的重写调用
    assert kinds[f"judge@{plan.JUDGE_MODEL}"] == [48.0, 135.0]
    assert plan.budget_of_group("K") == "A" and "gate-k" not in q["planned"]["groups"]
    assert "B-K" not in q["planned"]["groups"]


def test_gate_quote_carries_the_k_units(tmp_path: Path) -> None:
    q = _quote(tmp_path, *GLM, "--phases", "g", "--stop-line", "20")
    kinds = q["planned"]["groups"]["gate-k"]["by_vendor"]["glm"]["calls_by_kind"]
    assert kinds["extract@glm-5.3-flash"] == [100.0, 100.0]  # 50 次写回 x 两次提炼
    assert kinds[f"judge_event@{plan.JUDGE_MODEL}"] == [0.0, 40.0]
    assert f"judge@{plan.JUDGE_MODEL}" not in kinds  # 闸门的 K 段不送 K 组的判官题
    assert plan.budget_of_group("gate-k") == "G"


def test_use_side_classes_are_priced_like_a_b_class(tmp_path: Path) -> None:
    """§11.7 第 4 步:B4c、B4d 各按 B 组一类的量。"""
    q = _quote(tmp_path, *GLM, "--phases", "b", "--stop-line", "300")
    groups = q["planned"]["groups"]
    per_b_class = groups["B"]["yuan_high"] / len(sc.B_CLASSES)
    assert groups["B-K"]["yuan_high"] == pytest.approx(2 * per_b_class, rel=0.01)
    assert plan.budget_of_group("B-K") == "B"


def test_group_phases_let_a_requote_shrink_the_k_groups() -> None:
    assert plan.GROUP_PHASES["K"] == ("k",) and plan.GROUP_PHASES["gate-k"] == ("gate-k",)
    assert plan.GROUP_PHASES["B-K"] == ("b4x",)
    assert plan.group_share("K", {"k": 0.25}) == 0.25


def test_quote_digest_moves_when_a_k_scenario_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _quote(tmp_path, *GLM, "--phases", "a", "--stop-line", "100")["scenario_digest"]
    assert before.endswith(ks.scenario_digest_k())
    monkeypatch.setattr(ks, "build_use", lambda: [])
    after = _quote(tmp_path, *GLM, "--phases", "a", "--stop-line", "100")["scenario_digest"]
    assert after != before


# --------------------------------------------------------------------------- 线


def _line(cls: str, metric: str) -> rep.PassLine:
    return next(ln for ln in rep.LINES if (ln.cls, ln.metric) == (cls, metric))


def test_k_lines_match_the_design() -> None:
    """§11.3 第 9 版各线、§11.1 第 2 步闸门多看的三样:(比法, 线, 逐领域, 应有单元数)。"""
    expect = {
        ("G-K-a", "retired_to_new"): ("min", 8, None, 10),
        ("G-K-b", "x_still_valid"): ("min", 9, None, 10),
        ("G-K-n2", "x_retired"): ("max", 0, None, 10),
        ("G-K-cp", "x_retired"): ("max", 0, None, 10),
        ("G-ALL", "write_guarantee_broken"): ("max", 0, None, 1),
        ("G-ALL", "reconcile_reply_error"): ("max", 0, None, 40),
        ("K-a", "retired_to_new"): ("min", 24, 4, 30),
        ("K-a", "missed"): ("max", 3, None, 30),
        ("K-ap", "new_valid"): ("min", 24, None, 30),
        ("K-b", "x_still_valid"): ("min", 27, None, 30),
        ("K-b", "hedge_kept"): ("min", 24, None, 30),
        ("K-b2", "x_still_valid"): ("min", 27, None, 30),
        ("K-b2", "hedge_kept"): ("min", 24, None, 30),
        ("K-bp", "settled"): ("min", 24, None, 30),
        ("K-c", "x_retired"): ("max", 0, None, 30),
        ("K-c", "mislabelled"): ("max", 1, None, 30),
        ("K-cp", "x_retired"): ("max", 0, None, 30),
        ("K-n1", "x_retired"): ("max", 0, None, 30),
        ("K-n2", "x_retired"): ("max", 0, None, 30),
        ("K-e1", "retired_to_new"): ("min", 24, None, 30),
        ("K-e1", "duplicate_added"): ("max", 0, None, 30),
        ("K-e2", "user_stated_y_verbatim"): ("min", 15, None, 15),
        ("K-e2", "user_stated_y_reworded"): ("min", 12, None, 15),
        ("K-e2", "x_retired_ok"): ("min", 24, None, 30),
        ("K-f", "retired_to_new"): ("min", 24, None, 30),
        ("K-f", "stored_new"): ("min", 27, None, 30),
        ("K-g", "any_retired"): ("min", 27, None, 30),
        ("K-p", "half_kept"): ("min", 29, None, 30),
        ("K-p", "stored_new"): ("min", 27, None, 30),
        ("K-w", "user_stated_y_verbatim"): ("min", 15, None, 15),
        ("K-w", "user_stated_y_reworded"): ("min", 12, None, 15),
        ("K-w", "x_retired_ok"): ("min", 24, None, 30),
        ("K-t", "x_retired"): ("max", 0, None, 30),
        ("K-t", "stored_new"): ("min", 27, None, 30),
        ("K-late", "fuller_kept"): ("min", 15, None, 15),
        ("K-late", "later_valid"): ("min", 15, None, 15),
        ("K-s", "new_stored"): ("min", 13, None, 15),
        ("K-s", "summary_retired_directly"): ("max", 0, None, 15),
        ("K-s", "b_still_in_summary"): ("max", 2, None, 15),
        ("K-s", "ac_lost"): ("max", 0, None, 15),
        ("K-s", "rewrite_marked"): ("max", 0, None, 15),
        ("K-s", "summary_changed"): ("max", 0, None, 15),
        ("K-s", "synonym_added"): ("max", 2, None, 15),
        ("ALL-K", "neighbour_retired"): ("max", 0, None, 510),
        ("ALL", "write_guarantee_broken"): ("max", 0, None, 1),
        ("B4c", "violation"): ("max", 0, None, 30),
        ("B4d", "violation"): ("max", 0, None, 30),
    }
    assert {(c, m) for c, m, *_ in krep.K_LINES} == set(expect)
    for (cls, metric), (op, total, per_domain, n) in expect.items():
        line = _line(cls, metric)
        assert (line.op, line.total, line.per_domain, line.expected_n) == (op, total, per_domain, n)
        assert line.status == krep.APPROVED_V9, (cls, metric)
    assert _line("S7", "conclusion_confirmed").expected_n == 30  # 审查 M-1:15 次加到 30 次


def test_good_metrics_are_the_min_lines() -> None:
    """「好」类指标正好是 ``min`` 线的那些:方向标反了,过线会被读成不过线。"""
    mins = {m for c, m, op, *_ in krep.K_LINES if op == "min"}
    maxes = {m for c, m, op, *_ in krep.K_LINES if op == "max" and m != "violation"}
    assert mins == set(kchk.GOOD_METRICS)
    assert not maxes & set(kchk.GOOD_METRICS)


def test_neighbour_line_counts_every_unit_that_seeds_neighbours() -> None:
    seeded = sum(any(x.key in ks.NEAR_KEYS for x in s.seeds) for s in K) * plan.REPEATS
    assert _line("ALL-K", "neighbour_retired").expected_n == seeded == 510


# --------------------------------------------------------------------------- 报告汇总


def _mem(rid: str, content: str, **kw: Any) -> dict[str, Any]:
    return {
        "id": rid,
        "kind": "fact",
        "content": content,
        "origin": "user_stated",
        "confidence": 0.9,
        "live": True,
        "superseded_by": None,
        "supersedes": None,
        "consolidated_from": [],
        **kw,
    }


def _record(
    s: sc.Scenario,
    key: str,
    new: list[dict[str, Any]],
    *,
    retire: dict[str, str | None] | None = None,
    errors: list[str] | None = None,
    caps: dict[str, bool] | None = None,
) -> dict[str, Any]:
    rows = [_mem(f"{key}-{x.key}", x.content, origin=x.origin) for x in s.seeds]
    for seed_key, successor in (retire or {}).items():
        row = next(r for r in rows if r["id"] == f"{key}-{seed_key}")
        row.update(live=False, superseded_by=successor)
    return {
        "key": key,
        "scenario_id": s.scenario_id,
        "cls": s.cls,
        "vendor": "glm",
        "domain": s.domain,
        "result": {
            "seeded": {x.key: f"{key}-{x.key}" for x in s.seeds},
            "rounds": [{"calls": [], "failed": bool(errors), "errors": errors or []}],
            "memories": [*rows, *new],
            "capabilities": caps or dict.fromkeys(base.CAPABILITIES, True),
            "origin_labels": {},
        },
    }


def _write(out: Path, phase: str, records: list[dict[str, Any]], verdicts: list[Any] = ()) -> None:  # type: ignore[assignment]
    (out / phase).mkdir(parents=True, exist_ok=True)
    (out / phase / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    (out / phase / "verdicts.jsonl").write_text("".join(json.dumps(v) + "\n" for v in verdicts))


def _k(cls: str, n: int = 0) -> sc.Scenario:
    return [s for s in K if s.cls == cls][n]


def test_report_collects_k_lines_neighbours_and_the_guarantee(tmp_path: Path) -> None:
    out = tmp_path / "out"
    a = _k("K-a")
    y = ks.item_of(a).y.memory
    good = _record(a, "k1", [_mem("n1", y)], retire={"x": "n1"})
    bad = _record(a, "k2", [], retire={"near1": None})
    _write(out, "k", [good, bad])
    body = cli.report(out, None)
    line = body["lines"]["K-a.retired_to_new"]["glm"]
    assert (line["good"], line["n"]) == (1, 2) and line["verdict"] == "incomplete"
    assert body["lines"]["K-a.missed"]["glm"]["bad"] == 1
    near = body["lines"]["ALL-K.neighbour_retired"]["glm"]
    assert (near["bad"], near["n"]) == (1, 2)
    assert body["lines"]["ALL.write_guarantee_broken"]["glm"]["bad"] == 0
    assert body["counts"]["k"]["K-a.neighbours_seeded"] == 4


def test_report_puts_the_gate_k_lines_under_g(tmp_path: Path) -> None:
    """闸门的 K 段:线带 ``G-``;写的保证、回复解析失败各有一条 ``G-ALL`` 的线。"""
    out = tmp_path / "out"
    n2 = _k("K-n2")
    fine = _record(n2, "g1", [])
    broken = _record(
        n2, "g2", [], retire={"x": None}, errors=["ReconcileFailedError", "ReconcileReplyError"]
    )
    _write(out, "gate-k", [fine, broken])
    lines = cli.report(out, None)["lines"]
    assert lines["G-K-n2.x_retired"]["glm"]["bad"] == 1 and "K-n2.x_retired" not in lines
    errors = lines["G-ALL.reconcile_reply_error"]["glm"]
    assert (errors["bad"], errors["n"], errors["verdict"]) == (1, 2, "incomplete")
    assert lines["G-ALL.write_guarantee_broken"]["glm"]["n"] == 2
    assert "ALL.write_guarantee_broken" not in lines


def test_reply_errors_are_counted_in_every_extraction_phase(tmp_path: Path) -> None:
    out = tmp_path / "out"
    a = _k("K-a")
    _write(out, "k", [_record(a, "k1", [], errors=["ReconcileReplyError"]), _record(a, "k2", [])])
    counts = cli.report(out, None)["counts"]["k"]
    assert counts["reconcile_reply_errors"] == 1 and counts["writeback_failed"] == 1


def test_gate_k_sends_nothing_to_the_k_judge(tmp_path: Path) -> None:
    out = tmp_path / "out"
    b = _k("K-b")
    hedge = _record(b, "g1", [_mem("n1", ks.item_of(b).hedge_memory)])
    _write(out, "gate-k", [hedge])
    items, _facts = cli.judge_items(out, phases.PHASES["gate-k"])
    assert items == []
    _write(out, "k", [hedge])
    assert [i.check for i in cli.judge_items(out, phases.PHASES["k"])[0]] == ["hedge_kept"]


def test_judged_k_metric_reads_the_verdict(tmp_path: Path) -> None:
    out = tmp_path / "out"
    b = _k("K-b")
    hedge = _record(b, "k1", [_mem("n1", ks.item_of(b).hedge_memory)])
    digest = rep.a_judge_wording_digest()
    _write(out, "k", [hedge], [{"key": "k1|hedge_kept", "verdict": True, "wording_digest": digest}])
    assert cli.report(out, None)["lines"]["K-b.hedge_kept"]["glm"]["good"] == 1
    _write(
        out, "k", [hedge], [{"key": "k1|hedge_kept", "verdict": False, "wording_digest": digest}]
    )
    assert cli.report(out, None)["lines"]["K-b.hedge_kept"]["glm"]["good"] == 0


def test_old_code_reports_the_guarantee_incomplete_for_s_records_too(tmp_path: Path) -> None:
    """§11.3「写的保证(K、A、S 组全部)」:S 组的记录也核对;被测代码不是第 9 版 → incomplete。"""
    out = tmp_path / "out"
    s1 = next(s for s in ns.build_redesign() if s.cls == "S1")
    caps = {**dict.fromkeys(base.CAPABILITIES, True), "retire_split": False}
    rec = _record(s1, "s1", [_mem("n1", s1.x_key)], caps=caps)
    _write(out, "s", [rec])
    line = cli.report(out, None)["lines"]["ALL.write_guarantee_broken"]["glm"]
    assert line["verdict"].startswith("incomplete") and line["missing"] == {"retire_split": 1}
    new = _record(s1, "s1", [_mem("n1", s1.x_key)])
    _write(out, "s", [new])
    assert cli.report(out, None)["lines"]["ALL.write_guarantee_broken"]["glm"]["verdict"] == "pass"


def test_report_carries_the_power_table_and_the_assumptions(tmp_path: Path) -> None:
    """§11.3「统计力,照实写」与对代码侧接口的假设:有第 9 版的输出时进报告。"""
    out = tmp_path / "out"
    _write(out, "k", [_record(_k("K-a"), "k1", [])])
    body = cli.report(out, None)
    shapes = {row["shape"]: row for row in body["statistical_power"]}
    assert shapes["0 / 30"]["one_sided"] == "约 10%" and shapes["0 / 10"]["one_sided"] == "约 26%"
    assert shapes["0 / 15"]["two_sided"] == "约 22%" and len(shapes) == 7
    text = "\n".join(body["disclosures"])
    assert all(a in text for a in kp.ASSUMPTIONS)
    assert "K-b" in text and "K-n2" in text and "K-p" in text  # 没有程序兜底的三处
    assert krep.APPROVED_V9 in body["line_status_legend"]
    plain = tmp_path / "plain"
    s1 = next(s for s in ns.build_redesign() if s.cls == "S1")
    _write(plain, "s", [_record(s1, "s1", [])])
    assert "statistical_power" not in cli.report(plain, None)


# --------------------------------------------------------------------------- 判官题的种类


def test_k_judge_prompts_are_recognised_and_change_the_wording_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts = {
        "hedge": kchk.hedge_prompt(["[user] x"], ["m"]),
        "half": kchk.half_prompt("old", "deny", ["m"]),
        "fuller": kchk.fuller_prompt("q", "a", "later", ["m"]),
        "summary": kchk.summary_prompt("old", "deny", ["o"], ["cur"], "b"),
    }
    for name, prompt in prompts.items():
        assert rep.label_check(prompt) == name and rep.wording_in_prompt(prompt)
    assert rep.label_check(kchk.summary_prompt("old", "deny", ["o"], ["cur"], "ac")) == "summary"
    before = rep.a_judge_wording_digest()
    monkeypatch.setattr(kchk, "HEDGE_WORDING", kchk.HEDGE_WORDING + " changed")
    assert rep.a_judge_wording_digest() != before


def test_calibration_must_cover_the_k_judge_kinds() -> None:
    assert {"hedge", "half", "fuller", "summary"} <= cli.CALIBRATION_CHECKS["calibration-a"]
    assert not {"hedge", "half"} & cli.CALIBRATION_CHECKS["calibration-g"]


def test_calibration_labels_may_come_from_the_k_domains() -> None:
    def labels(domains: list[str]) -> list[dict[str, Any]]:
        return [{"domain": d, "label": i % 2 == 0, "prompt": "p"} for i, d in enumerate(domains)]

    phases.check_labels(labels([d for d in sc.DOMAINS for _ in range(4)]))
    mixed = [
        *(d for d in sc.DOMAINS for _ in range(2)),
        *(d for d in ki.K_DOMAINS for _ in range(2)),
    ]
    phases.check_labels(labels(mixed))
    with pytest.raises(phases.EvalError):
        phases.check_labels(labels(["support"] * 8 + ["legal"] * 12))
    with pytest.raises(phases.EvalError):
        phases.check_labels(labels(["unknown"] * 4 + [d for d in sc.DOMAINS[1:] for _ in range(4)]))
    lopsided = ["support"] * 8 + ["legal"] * 4 + ["coding"] * 4 + ["sales"] * 2 + ["health"] * 2
    with pytest.raises(phases.EvalError):  # 5 个领域都有,但一个领域 8 条
        phases.check_labels(labels(lopsided))


def test_the_k_gate_is_judged_with_the_gate_votes(tmp_path: Path) -> None:
    """闸门的 K 段与原来的闸门一样用报价里 G 段的票数(带事件日期的记忆仍要送判官)。"""
    q = {"judge": {"gate_votes": 3, "a_group_votes": 1}, "pilot": False}
    calibration = {
        "judge_model": plan.JUDGE_MODEL,
        "votes": 3,
        "agree": plan.CALIBRATION_ITEMS,
        "wording_digest": rep.a_judge_wording_digest(),
    }
    phases.calibration_path(tmp_path, "a").write_text(json.dumps(calibration))
    assert cli.judge_votes(tmp_path, q, "extraction", "gate-k") == 3
    assert cli.judge_votes(tmp_path, q, "extraction", "gate") == 3
    with pytest.raises(phases.EvalError):
        cli.judge_votes(tmp_path, q, "extraction", "k")  # 正式一轮按 A 段的票数(这里是 1 票)


def test_use_side_judge_prompt_states_the_rule_and_both_values() -> None:
    for cls in ks.USE_CLASSES:
        s = ks.build_use_class(cls)[0]
        text = sc.fact(s.fact_key).texts[s.lang]
        prompt = rep.conversation_prompt(s, "the answer")
        assert "goes by the earlier value X alone" in prompt
        assert f'X = "{text.x}"' in prompt and f'Y = "{text.y}"' in prompt
    b4d = rep.conversation_prompt(ks.build_use_class("B4d")[0], "a")
    assert b4d.count("summary of earlier memories") == 1 and b4d.count("days ago") == 2
    b4c = rep.conversation_prompt(ks.build_use_class("B4c")[0], "a")
    assert "corrected by the user" in b4c and "stated in user messages" in b4c


def test_use_side_recall_needs_both_rows() -> None:
    b4c, b4d = (ks.build_use_class(c)[0] for c in ks.USE_CLASSES)
    assert rep.recall_ok(b4c, {"x": "1", "y": "2"}, ["1", "2"]) is True
    assert rep.recall_ok(b4c, {"x": "1", "y": "2"}, ["1"]) is False
    assert rep.recall_ok(b4d, {"summary": "5", "y": "2", "x": "1"}, ["5", "2"]) is True
    assert rep.recall_ok(b4d, {"summary": "5", "y": "2", "x": "1"}, ["1", "2"]) is False


# --------------------------------------------------------------------------- 本机写回


def _chat(reply_for: Any) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = "\n".join(m["content"] for m in body["messages"] if m["role"] == "system")
        content = reply_for(live.classify_call(system), body)
        usage = {"prompt_tokens": 10, "completion_tokens": 5}
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content}}], "usage": usage}
        )

    return httpx.MockTransport(handler)


def _run(s: sc.Scenario, reply_for: Any) -> dict[str, Any]:
    guard = spend.Guard(stop_line=5.0, prices={"mem": Price(1, 1, 1, "t")})

    async def main() -> dict[str, Any]:
        async with httpx.AsyncClient(transport=_chat(reply_for)) as http:
            t = live.ChatTransport("mem", "https://v", "k", guard, http, 500, provider="deepseek")
            return await live.run_extraction(
                s, transport=t, embedder=KeywordEmbedder(), item="u", phase="k"
            )

    return asyncio.run(main())


def _nothing(_kind: str, _body: Any) -> str:
    return '{"memories": []}'


def test_run_extraction_seeds_k_scenarios_and_dumps_the_columns_the_checks_read() -> None:
    result = _run(_k("K-s"), _nothing)
    rows = {m["id"]: m for m in result["memories"]}
    summary = rows[result["seeded"]["summary"]]
    assert summary["status"] == "consolidated" and len(summary["consolidated_from"]) == 3
    assert rows[result["seeded"]["b"]]["consolidated_into"] == result["seeded"]["summary"]
    assert set(base.CAPABILITIES) == set(result["capabilities"])
    assert result["origin_labels"]["stated in user messages"] == "user_stated"
    rewrite = result["rounds"][0]["rewrite"]
    assert rewrite["before"] == {"summary_live": True, "summary_flagged": False}
    assert rewrite["calls"] == [] and result["rounds"][0]["errors"] == []
    out, _items = kchk.checks({"key": "u", "result": result}, _k("K-s"))
    assert out["summary_retired_directly"] is False and out["neighbour_retired"] is False


def test_only_k_s_runs_the_rewrite_step() -> None:
    assert "rewrite" not in _run(_k("K-a"), _nothing)["rounds"][0]


def test_k_late_is_one_writeback_over_all_its_turns() -> None:
    seen: list[str] = []

    def reply(kind: str, body: Any) -> str:
        if kind == "pass1":
            seen.append("\n".join(str(m["content"]) for m in body["messages"][1:]))
        return '{"memories": []}'

    s = _k("K-late")
    result = _run(s, reply)
    assert [r["after_turn"] for r in result["rounds"]] == [2] and len(seen) == 1
    assert s.turns[1].user in seen[0] and s.turns[2].user in seen[0]
    assert result["seeded"] == {}


def test_reconcile_material_and_swallowed_errors_are_recorded() -> None:
    """去重合并的材料记下(写的保证、「模型列了」都要按它对回去);写回入口吞掉的错只记异常名。"""

    def reply(kind: str, _body: Any) -> str:
        if kind == "pass1":
            memory = {
                "kind": "fact",
                "content": "The user changed it.",
                "importance": 0.8,
                "turn": 1,
            }
            return json.dumps({"memories": [memory]})
        return "not json" if kind == "reconcile" else '{"memories": []}'

    result = _run(_k("K-a"), reply)
    call = next(c for c in result["rounds"][0]["calls"] if c["kind"] == "reconcile")
    material = json.loads(call["input"])
    assert material["candidates"][0]["content"] == "The user changed it."
    assert kp.RECONCILE_REPLY_ERROR in result["rounds"][0]["errors"]
    assert result["rounds"][0]["failed"] is True


# --------------------------------------------------------------------------- 第 9 版代码(假模型)


def _user_line(body: Any) -> int:
    """第一次提炼的输入里最后一行的行号(第一次只看用户的文字,每行 ``[n] [日期] 文字``)。"""
    text = "\n".join(str(m["content"]) for m in body["messages"] if m["role"] != "system")
    numbers = [x[1 : x.index("]")] for x in text.splitlines() if x.startswith("[") and "]" in x]
    return int([n for n in numbers if n.isdigit()][-1])


def _scripted(content: str, listed: str, rewritten: str = "") -> Any:
    """假模型:第一次提炼把 ``content`` 记成用户说的(带 ``turn``);去重合并 ``ADD`` 并把正文是
    ``listed`` 的那条已有记忆列进 ``replaces``;重写回 ``rewritten``。"""

    def reply(kind: str, body: Any) -> str:
        if kind == "pass1":
            turn = _user_line(body)
            memory = {"kind": "fact", "content": content, "importance": 0.8, "turn": turn}
            return json.dumps({"memories": [memory]}, ensure_ascii=False)
        if kind == "reconcile":
            material = json.loads(body["messages"][-1]["content"])
            ops = []
            for cand in material["candidates"]:
                ids = [e["id"] for e in cand["existing"] if e["content"] == listed]
                ops.append({"index": cand["index"], "op": "ADD", "replaces": ids, "restates": []})
            return json.dumps({"ops": ops})
        if kind == "other":
            return json.dumps({"summary": rewritten}, ensure_ascii=False)
        return '{"memories": []}'

    return reply


def test_on_v9_code_a_full_sentence_change_retires_the_old_row_and_keeps_the_guarantee() -> None:
    """代码侧第 9 版合进来之后:能力探测为真,K-a 的线与「写的保证」算得出来(不再 incomplete)。"""
    s = _k("K-a")
    it = ks.item_of(s)
    result = _run(s, _scripted(it.y.memory, it.x.memory))
    assert result["capabilities"]["retire_split"] is True
    assert result["capabilities"]["summary_rewrite"] is True
    out, _items = kchk.checks({"key": "u", "result": result}, s)
    assert out["retired_to_new"] is True and out["missed"] is False
    assert out["write_guarantee_broken"] is False and out["missing"] == {}
    assert out["counts"]["guarantee_candidates"] == 1 and out["counts"]["listed"] == 1
    assert out["counts"].get("listed_blocked", 0) == 0 and out["neighbour_retired"] is False
    rows = {m["id"]: m for m in result["memories"]}
    old = rows[result["seeded"]["x"]]
    assert not old["live"] and rows[old["superseded_by"]]["origin"] == "user_stated"


def test_on_v9_code_a_listed_neighbour_that_the_program_keeps_is_counted() -> None:
    """控制台更正过的行模型列了也退不掉(K-t):新说法照写、旧行仍有效,数进「被程序挡下」。"""
    s = _k("K-t")
    it = ks.item_of(s)
    result = _run(s, _scripted(it.y.memory, it.x.memory))
    out, _items = kchk.checks({"key": "u", "result": result}, s)
    assert out["x_retired"] is False and out["stored_new"] is True
    assert out["counts"]["listed_blocked"] == 1 and out["write_guarantee_broken"] is False


def test_on_v9_code_a_denied_summary_is_marked_then_rewritten_in_place() -> None:
    """K-s 否认的那一半:写回不直接退汇总行、只打标;当场重写之后旧汇总行连到新汇总行,
    重写后的两条线送判官(不再 incomplete)。"""
    s = next(x for x in K if x.cls == "K-s" and x.variant == "deny")
    trio = ks.item_of(s).trio
    rewritten = f"{trio.a.memory} {trio.c.memory}"
    # 假嵌入按字面相近排名次:让假模型写出的那句贴着汇总句,汇总行才进同格前 2 名(§7.1 第 6 行);
    # 真跑时名次由真嵌入定,排不进前 2 名就只是不退、不打标(counts 的 listed_blocked 看得到)。
    said = f"{trio.summary} {trio.deny_b}"
    result = _run(s, _scripted(said, trio.summary, rewritten))
    rewrite = result["rounds"][0]["rewrite"]
    assert rewrite["before"] == {"summary_live": True, "summary_flagged": True}
    assert rewrite["ran"] is True and rewrite["outcomes"] == ["rewritten"]
    assert [c["kind"] for c in rewrite["calls"]] == ["rewrite"]
    rows = {m["id"]: m for m in result["memories"]}
    old = rows[result["seeded"]["summary"]]
    fresh = rows[old["superseded_by"]]
    assert not old["live"] and fresh["live"] and fresh["content"] == rewritten
    assert fresh["consolidated_from"] == old["consolidated_from"]
    out, items = kchk.checks({"key": "u", "result": result}, s)
    assert out["summary_retired_directly"] is False and out["new_stored"] is True
    assert out["missing"] == {} and out["counts"]["rewrite_rewritten"] == 1
    assert [i.check for i in items] == ["b_still_in_summary", "ac_lost"]
    assert f"- {rewritten}" in items[0].prompt
    assert out["neighbour_retired"] is False and out["write_guarantee_broken"] is False
