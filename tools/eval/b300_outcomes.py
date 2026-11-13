"""B-300 评测的判分材料与报告汇总(从 ``b300_eval`` 拆出,不花钱):每条记录的程序检查与前提、
要送判官的项、按类 x 家 x 领域的结果、基线(证明会红)、§8.5 对比。"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import b300_plan as plan  # noqa: E402
import b300_report as rep  # noqa: E402
from b300_phases import (  # noqa: E402
    PHASES,
    Phase,
    all_scenarios,
    inpod_rows,
    latest,
    read_jsonl,
)

# --------------------------------------------------------------------------- 判官


def latest_records(out: Path, phase: str) -> dict[str, dict[str, Any]]:
    """同一单元留最后一条(续跑会追加)。"""
    return {r["key"]: r for r in read_jsonl(out / phase / "records.jsonl")}


def judge_items(out: Path, phase: Phase) -> tuple[list[rep.JudgeItem], dict[str, Any]]:
    """这一段要判的项,以及每条记录的程序检查 / 前提(报告用)。"""
    scenarios = all_scenarios()
    records = latest_records(out, phase.name)
    items: list[rep.JudgeItem] = []
    facts: dict[str, Any] = {}
    recalled = latest(inpod_rows(out, phase.name, "recalled"), "run_id")
    states = {step: latest(inpod_rows(out, phase.name, "state", step), "key") for step in (1, 2)}
    final_state = latest(inpod_rows(out, phase.name, "state"), "key")
    candidates = latest(inpod_rows(out, phase.name, "candidates"), "key")
    summaries_done: set[str] = set()
    for key, r in records.items():
        if r.get("error"):
            continue
        s = scenarios[r["scenario_id"]]
        if phase.kind == "extraction":
            checks, more = rep.a_checks(r, s) if phase.classes != ("D",) else ({}, [])
            facts[key] = {"checks": checks}
            items += more
            continue
        convs = r.get("convs", [])
        if len(convs) < len(s.conversations):
            continue
        answer = convs[-1]
        rec = recalled.get(str(answer.get("run_id")))
        info: dict[str, Any] = {
            "recall_ok": rep.recall_ok(
                s, r.get("seed_ids", {}), rec.get("memory_ids") if rec else None
            )
        }
        if phase.kind == "c4":
            info["precondition"] = rep.c4_precondition(
                convs[0].get("session_id"),
                (states[1].get(r["user"]) or {}).get("rows"),
                (states[2].get(r["user"]) or {}).get("rows"),
            )
        if phase.kind == "c5":
            cand = candidates.get(r["user"])
            if cand and cand.get("already_consolidated"):
                # 平台的定时整理在我们这一步之前就整理过了(没有按租户的开关,审查 M5):前提不成立。
                info["precondition"] = False
                info["consolidated_before"] = True
            info.update(
                rep.c5_region_in_candidates(r.get("seed_ids", {}), cand["clusters"]) if cand else {}
            )
            state_rows = (final_state.get(r["user"]) or {}).get("rows")
            summaries = [row["content"] for row in state_rows or [] if row.get("summary")]
            info["summaries"] = len(summaries)
            info["state_known"] = state_rows is not None
            # 没凑成簇的这一片:C5 ① 未测到,汇总行不送判官(省钱,也不进过线)
            if (
                r["user"] not in summaries_done
                and state_rows is not None
                and info.get("region_clustered")
            ):
                summaries_done.add(r["user"])
                if summaries:
                    items.append(
                        rep.JudgeItem(
                            f"{r['user']}|summary", "summary", rep.summary_prompt(s, summaries)
                        )
                    )
        facts[key] = info
        # C4 只在前提确实成立时判(没查到状态 / 不成立的单独计数,§8.7)。
        if info["recall_ok"] and info.get("precondition", True) is True:
            items.append(
                rep.JudgeItem(
                    f"{key}|answer", "answer", rep.conversation_prompt(s, answer["final_text"])
                )
            )
    return items, facts


# --------------------------------------------------------------------------- 报告


def _bad(verdicts: Mapping[str, Any], key: str) -> bool | None:
    row = verdicts.get(key)
    return None if row is None else row.get("verdict")


def collect_outcomes(
    out: Path,
) -> tuple[dict[tuple[str, str, str], list[tuple[str, bool]]], dict[str, Any], dict[str, Any]]:
    outcomes: dict[tuple[str, str, str], list[tuple[str, bool]]] = defaultdict(list)
    counts: dict[str, Any] = defaultdict(lambda: defaultdict(int))
    baseline: dict[str, Any] = {}
    for name, phase in PHASES.items():
        if not (out / name).exists() or phase.classes == ("D",):
            continue
        _items, facts = judge_items(out, phase)
        verdicts = latest(read_jsonl(out / name / "verdicts.jsonl"), "key")
        records = latest_records(out, name)
        red_rows: list[tuple[str, bool | None]] = []
        for key, info in facts.items():
            r = records[key]
            cls, vendor, domain = r["cls"], r["vendor"], r["domain"]
            if phase.kind == "extraction":
                _a_outcomes(outcomes, counts[name], r, info["checks"], verdicts)
                continue
            if not info["recall_ok"]:
                counts[name]["not_recalled_excluded"] += 1
                continue
            if info.get("precondition") is False:
                counts[name]["precondition_false"] += 1
                continue
            if info.get("precondition") is None and phase.kind == "c4":
                counts[name]["precondition_unknown"] += 1
                continue
            bad = _bad(verdicts, f"{key}|answer")
            if bad is None:
                counts[name]["judge_split_or_missing"] += 1
                continue
            if phase.baseline:
                red_rows.append((r["scenario_id"], bad))
            else:
                outcomes[(cls, "violation", vendor)].append((domain, bad))
        if phase.baseline:
            baseline[name] = {"answers": rep.prove_red(red_rows)}
        if phase.kind == "c5":
            summary_rows = _c5_summaries(records, facts, verdicts, counts[name])
            if phase.baseline:
                baseline[name]["summary"] = rep.prove_red(
                    (sid, bad) for sid, _d, bad, clustered in summary_rows if clustered
                )
            else:
                measured = [(domain, bad) for _sid, domain, bad, _c in summary_rows]
                if measured:  # 一次都没测到就不出这条线(「未测到」另列)
                    outcomes[("C5", "summary_violation", "consolidation")] += measured
    return outcomes, {k: dict(v) for k, v in counts.items()}, baseline


def _c5_summaries(
    records: Mapping[str, Mapping[str, Any]],
    facts: Mapping[str, Mapping[str, Any]],
    verdicts: Mapping[str, Any],
    counts: Any,
) -> list[tuple[str, str, bool, bool]]:
    """C5 ①,每个评测用户一次(整理只做一次,四家共用):(场景, 领域, 坏不坏, 这一片整理前成簇没有)。

    没有汇总行 = 没有把 X 说成现在的(不坏);有汇总行就看判官。整理前的候选里有已作废的 X
    单独计数(PR A 的 D1 之后应为 0)。"""
    rows: list[tuple[str, str, bool, bool]] = []
    seen: set[str] = set()
    for key, info in facts.items():
        r = records[key]
        if r["user"] in seen or not info.get("state_known"):
            continue
        if not info.get("region_clustered"):
            # 整理前这一片没凑成候选簇(审查第五轮 M2):这一次 C5 ① 什么都没量到 —— 记「未测到」,
            # 不算过线,也不算违规;基线上同样不进「证明会红」。
            seen.add(r["user"])
            counts["c5_not_measured"] += 1
            continue
        if info.get("consolidated_before"):
            seen.add(r["user"])
            counts["c5_consolidated_before_our_step"] += 1
            continue
        seen.add(r["user"])
        counts["x_in_candidates"] += bool(info.get("x_in_candidates"))
        clustered = True  # 没凑成簇的上面已经记成「未测到」
        bad = _bad(verdicts, f"{r['user']}|summary") if info.get("summaries") else False
        if bad is None:
            counts["judge_split_or_missing"] += 1
            continue
        rows.append((r["scenario_id"], r["domain"], bool(bad), clustered))
    return rows


def _a_outcomes(
    outcomes: dict[tuple[str, str, str], list[tuple[str, bool]]],
    counts: Any,
    r: Mapping[str, Any],
    checks: Mapping[str, Any],
    verdicts: Mapping[str, Any],
) -> None:
    cls, vendor, domain, key = r["cls"], r["vendor"], r["domain"], r["key"]
    if not checks.get("two_pass"):
        counts["pre_pr1_or_no_two_pass"] += 1
        return

    def add(metric: str, bad: bool | None) -> None:
        if bad is None:
            counts["judge_split_or_missing"] += 1
        else:
            outcomes[(cls, metric, vendor)].append((domain, bool(bad)))

    for metric in ("missed", "pass1_leak", "pass1_fabricate", "user_memory_lost", "over_two"):
        if metric in checks:
            add(metric, checks[metric])
    if "plain" in checks:
        add(
            "plain",
            checks["plain"] if checks["plain"] is not None else _bad(verdicts, f"{key}|plain"),
        )
    if "pass1_statement" in checks:
        value = checks["pass1_statement"]
        add("pass1_statement", value if value is not None else _bad(verdicts, f"{key}|statement"))
    if "attributed" in checks:
        good = (
            checks["attributed"]
            if checks["attributed"] is not None
            else _bad(verdicts, f"{key}|attributed")
        )
        if cls == "P9":
            add(
                "attributed_correction",
                None if good is None else not (good and checks.get("is_correction")),
            )
            add("y_valid", not checks.get("y_valid"))
        else:
            add("attributed", None if good is None else not good)
    if cls == "A10":
        counts["a10_user_version"] += bool(checks.get("user_version"))


def compare_counts(out: Path) -> dict[str, Any]:
    """§8.5:改前 / 改后写入条数、两类各几条、来源;线初定「下降不超过三分之一」(设计 §8.5)。"""
    side: dict[str, dict[str, int]] = {}
    for name in ("compare-before", "compare-after"):
        totals: dict[str, int] = defaultdict(int)
        for r in latest_records(out, name).values():
            if r.get("error"):
                continue
            for m in r["result"]["memories"]:
                if not m.get("live"):
                    continue
                totals["written"] += 1
                totals[f"kind_{m['kind']}"] += 1
                totals[f"origin_{m.get('origin')}"] += 1
        side[name] = dict(totals)
    before, after = (
        side["compare-before"].get("written", 0),
        side["compare-after"].get("written", 0),
    )
    drop = (before - after) / before if before else None
    return {
        **side,
        "drop_share": None if drop is None else round(drop, 3),
        "line": "drop ≤ 1/3 (设计 §8.5 初定)",
        "within_line": None if drop is None else drop <= 1 / 3,
    }


def report(out: Path, q: Mapping[str, Any] | None) -> dict[str, Any]:
    outcomes, counts, baseline = collect_outcomes(out)
    carried = {
        name: len(read_jsonl(out / name / "carried.jsonl"))
        for name in PHASES
        if (out / name / "carried.jsonl").exists()
    }
    meta = {
        name: json.loads((out / name / "meta.json").read_text(encoding="utf-8"))
        for name in PHASES
        if (out / name / "meta.json").exists()
    }
    c_controls = (
        {
            r["vendor"]
            for r in latest_records(out, "c").values()
            if r["cls"] in {"C1", "C3"} and not r.get("error")
        }
        if (out / "c").exists()
        else set()
    )
    vendors = (
        [v["provider"] for v in q["vendors"]] if q else [v.provider for v in plan.DEFAULT_VENDORS]
    )
    cuts = [c["step"] for c in q["cuts_applied"]] if q else []
    body = rep.build_report(
        outcomes,
        counts=counts,
        meta=meta,
        c_control_vendors=c_controls,
        all_vendors=vendors,
        cuts_applied=cuts,
        baseline=baseline,
    )
    if (out / "compare-before").exists() or (out / "compare-after").exists():
        body["compare"] = compare_counts(out)
    # 从旧报价搬过来的记录(``prepare --resume-from``)与新跑的合在一起算;这里报搬了多少。
    not_measured = sum(int((c or {}).get("c5_not_measured", 0)) for c in counts.values())
    if not_measured:
        body["disclosures"].append(
            f"C5 ① 有 {not_measured} 次整理前这一片没凑成候选簇,记为「未测到」,"
            "不算过线(审查第五轮 M2)。"
        )
    body["carried"] = carried
    return body
