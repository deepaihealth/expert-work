"""B-300 评测的判分材料与报告汇总(从 ``b300_eval`` 拆出,不花钱):每条记录的程序检查与前提、
要送判官的项、按类 x 家 x 领域的结果、基线(证明会红)、§8.5 对比。"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import b300_plan as plan  # noqa: E402
import b300_redesign_base as base  # noqa: E402
import b300_redesign_checks as chk  # noqa: E402
import b300_redesign_scenarios as ns  # noqa: E402
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
            checks, more = _extraction_checks(phase, r, s)
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
            info["candidates_known"] = cand is not None
            info.update(
                rep.c5_region_in_candidates(r.get("seed_ids", {}), cand["clusters"]) if cand else {}
            )
            state_rows = (final_state.get(r["user"]) or {}).get("rows")
            summaries = [row["content"] for row in state_rows or [] if row.get("summary")]
            info["summaries"] = len(summaries)
            info["state_known"] = state_rows is not None
            # 送判官的汇总行:我们这一步整理出来的(进过线);平台在我们之前整理出来的也判,但另列、
            # 不进过线(第六轮)。整理前没凑成簇 / 候选没记录的:不判(未测到)。
            platform = bool(info.get("consolidated_before"))
            if (
                r["user"] not in summaries_done
                and state_rows is not None
                and summaries
                and (platform or info.get("region_clustered"))
            ):
                summaries_done.add(r["user"])
                check = "platform_summary" if platform else "summary"
                items.append(
                    rep.JudgeItem(f"{r['user']}|{check}", check, rep.summary_prompt(s, summaries))
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


def _extraction_checks(
    phase: Phase, r: Mapping[str, Any], s: Any
) -> tuple[dict[str, Any], list[rep.JudgeItem]]:
    """A 组走 ``a_checks``;回归闸门与重做的各段走 ``b300_redesign_checks``;另外每条带事件日期的
    记忆送判官判是不是一次性的事(§11.3,全部提炼段,不含对比与基线)。"""
    if phase.classes == ("D",):
        return {}, []
    if phase.name == GATE_PHASE:
        checks, items = chk.gate_checks(r, s)
    elif s.cls in ns.REDESIGN_CLASSES:
        checks, items = chk.checks(r, s)
    else:
        checks, items = rep.a_checks(r, s)
    if not phase.baseline:
        items = [*items, *chk.event_date_items(r)]
    return checks, items


#: 回归闸门的段名;它的线带 ``G-`` 前缀,与正式一轮 A 组的 P6 / P7 分开(§11.1 第 2 步)。
GATE_PHASE = "gate"


# --------------------------------------------------------------------------- 报告


def _unjudged(verdicts: Mapping[str, Any], key: str) -> str:
    """没有判分可用的原因:还没判(``judge_missing``)/ 判了但没有多数(``judge_split``)。"""
    return "judge_missing" if verdicts.get(key) is None else "judge_split"


def _bad(verdicts: Mapping[str, Any], key: str) -> bool | None:
    row = verdicts.get(key)
    return None if row is None else row.get("verdict")


Missing = dict[tuple[str, str, str], dict[str, int]]


def collect_outcomes(
    out: Path,
) -> tuple[
    dict[tuple[str, str, str], list[tuple[str, bool]]], dict[str, Any], dict[str, Any], Missing
]:
    outcomes: dict[tuple[str, str, str], list[tuple[str, bool]]] = defaultdict(list)
    counts: dict[str, Any] = defaultdict(lambda: defaultdict(int))
    baseline: dict[str, Any] = {}
    missing: Missing = defaultdict(lambda: defaultdict(int))
    for name, phase in PHASES.items():
        if not (out / name).exists() or phase.classes == ("D",):
            continue
        _items, facts = judge_items(out, phase)
        verdicts = latest(read_jsonl(out / name / "verdicts.jsonl"), "key")
        if phase.kind == "extraction":  # 审查 M3:措辞对不上的判分不算,计数报出、要重判
            digest = rep.a_judge_wording_digest()
            stale = {k for k, v in verdicts.items() if v.get("wording_digest") != digest}
            if stale:
                counts[name]["verdicts_stale_wording_excluded"] += len(stale)
                verdicts = {k: v for k, v in verdicts.items() if k not in stale}
        records = latest_records(out, name)
        red_rows: list[tuple[str, bool | None]] = []
        for key, info in facts.items():
            r = records[key]
            cls, vendor, domain = r["cls"], r["vendor"], r["domain"]
            if phase.kind == "extraction":
                label = f"G-{cls}" if name == GATE_PHASE else cls
                _a_outcomes(outcomes, missing, counts[name], r, info["checks"], verdicts, label)
                if not phase.baseline:
                    _event_date_outcomes(outcomes, missing, r, verdicts)
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
                counts[name][_unjudged(verdicts, f"{key}|answer")] += 1
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
    return (
        outcomes,
        {k: dict(v) for k, v in counts.items()},
        baseline,
        {k: dict(v) for k, v in missing.items()},
    )


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
        seen.add(r["user"])
        # 先后有讲究(第六轮):先看平台是不是在我们之前就整理过,再看候选有没有记录,最后才是
        # 「没凑成簇」—— 三种各记各的原因,都不进过线。
        if info.get("consolidated_before"):
            counts["c5_consolidated_before_our_step"] += 1
            verdict = (
                _bad(verdicts, f"{r['user']}|platform_summary") if info.get("summaries") else False
            )
            if verdict is not None:  # 平台整理出来的汇总行的判分:另列
                counts[
                    "c5_platform_summary_x_as_current" if verdict else "c5_platform_summary_ok"
                ] += 1
            continue
        if not info.get("candidates_known"):
            counts["c5_candidates_not_recorded"] += 1  # 候选没记录
            continue
        if not info.get("region_clustered"):
            # 整理前这一片没凑成候选簇(审查第五轮 M2):这一次 C5 ① 什么都没量到 —— 记「未测到」,
            # 不算过线,也不算违规;基线上同样不进「证明会红」。
            counts["c5_not_measured"] += 1
            continue
        counts["x_in_candidates"] += bool(info.get("x_in_candidates"))
        clustered = True  # 没凑成簇的上面已经记成「未测到」
        bad = _bad(verdicts, f"{r['user']}|summary") if info.get("summaries") else False
        if bad is None:
            counts[_unjudged(verdicts, f"{r['user']}|summary")] += 1
            continue
        rows.append((r["scenario_id"], r["domain"], bool(bad), clustered))
    return rows


#: 检查里只报数、不进线的键(另有专门的计数)。
_INFO_KEYS = frozenset(
    {
        "two_pass",
        "missing",
        "counts",
        "is_correction",
        "user_version",
        "original_superseded",
        "user_side_rows",
        "user_side_max_per_kind",
        "origin_unknown",
        "assistant_restatements",
    }
)
#: 待判项的检查名与指标名不同的(P8)。
_ITEM_CHECK = {"pass1_statement": "statement"}
#: 跨全部提炼段合计的指标(§11.3「误伤」)。
_AGGREGATE = frozenset({"collateral"})


def _a_outcomes(
    outcomes: dict[tuple[str, str, str], list[tuple[str, bool]]],
    missing: Missing,
    counts: Any,
    r: Mapping[str, Any],
    checks: Mapping[str, Any],
    verdicts: Mapping[str, Any],
    cls: str,
) -> None:
    """一条提炼记录 → 各线的(领域, 坏不坏)。「坏」类指标 ``True`` = 坏;「好」类
    (``chk.GOOD_METRICS``、``y_valid``)``True`` = 做到了;``None`` = 看判官(``<键>|<检查名>``)。
    平台没有字段而量不了的,记进 ``missing``(报告判 incomplete、写明缺哪个字段)。"""
    vendor, domain, key = r["vendor"], r["domain"], r["key"]
    if "two_pass" in checks and not checks.get("two_pass"):
        counts["pre_pr1_or_no_two_pass"] += 1
        return
    good_metrics = chk.GOOD_METRICS | {"y_valid"}
    for metric, value in checks.items():
        if metric in _INFO_KEYS:
            continue
        if value is None:
            row = verdicts.get(f"{key}|{_ITEM_CHECK.get(metric, metric)}")
            verdict = None if row is None else row.get("verdict")
            if verdict is None:
                counts["judge_missing" if row is None else "judge_split"] += 1
                continue
            bad = not verdict if metric in good_metrics else bool(verdict)
        else:
            bad = not value if metric in good_metrics else bool(value)
        outcomes[("ALL" if metric in _AGGREGATE else cls, metric, vendor)].append((domain, bad))
    for metric, field in (checks.get("missing") or {}).items():
        missing[(cls, metric, vendor)][field] += 1
    for name, n in (checks.get("counts") or {}).items():
        counts[f"{cls}.{name}"] += int(n)
    if cls == "A10":
        counts["a10_user_version"] += bool(checks.get("user_version"))
        counts["a10_original_superseded"] += bool(checks.get("original_superseded"))  # 也在线里
    if cls == "A12":  # 用户 11-14:来自助手的复述允许,计数报出
        counts["a12_assistant_restatements"] += int(checks.get("assistant_restatements", 0))
        counts["a12_user_side_zero"] += checks.get("user_side_rows") == 0  # 不算违规,单列
        counts["a12_origin_unknown"] += int(checks.get("origin_unknown", 0))


def _event_date_outcomes(
    outcomes: dict[tuple[str, str, str], list[tuple[str, bool]]],
    missing: Missing,
    r: Mapping[str, Any],
    verdicts: Mapping[str, Any],
) -> None:
    """§11.3「不是事件却被标了事件日期」:每条带事件日期的记忆一行(判官 yes = 不是一次性的事)。"""
    if not base.capability(r["result"], "event_date"):
        missing[("ALL", "event_date_not_event", r["vendor"])]["event_date"] += 1
        return
    prefix = f"{r['key']}|event_date|"
    for vkey, row in verdicts.items():
        if vkey.startswith(prefix) and row.get("verdict") is not None:
            outcomes[("ALL", "event_date_not_event", r["vendor"])].append(
                (r["domain"], bool(row["verdict"]))
            )


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


REDESIGN_PHASES = ("gate", "s", "shape", "a13", "a14", "a15", "sd")
#: 重做设计 §11.3 末、§11.2 的照实说明。
REDESIGN_DISCLOSURES = (
    "统计力(§11.3):30 次里 0 次只能说明真实比例大致低于 10%,15 次里 0 次约 20%;这些线挡得住"
    "明显的问题,挡不住低比例的偶发。",
    "可比性(§11.3):11-15 及以前的 P6 / P7、A12 是在「每轮整段重抽」下量的,本项之后是切片 + "
    "新指令,不能直接比;以回归闸门那一次为新基线,旧数只说明问题存在。",
    "回归闸门(§11.1 第 2 步)按原文只设总数线(空列表各 ≤ 2 / 30、用户确认且有据各 ≥ 24 / 30),"
    "不设逐领域;它用 A 组原样的 P6 / P7 单元(只在最后一轮写回一次)。",
    "形状留出集只报数;它由另一位没看过提示词例子的人写(20 个,4 种形状 x 5 个领域,中英各 10),"
    "脚本只转成数据结构、给期望值补了几种写法,并把一个卡号的首位改成 0。",
    "误伤只在种了旁观记忆的 S 组上量(每个 S 场景一条同领域、无关的用户说的记忆);A 组原样不动,"
    "没有旁观记忆。",
    "切片起点按参数名 start_message_count 传给写回入口;被测代码不收这个参数时 A13、A14 报 "
    "incomplete,"
    "其余各段照整段重抽量。",
)


def votes_disclosures(out: Path) -> list[str]:
    """审查 M5:报告写明每段判官实际用了几票(按判分行里的票数数,不按假设)。"""
    lines = []
    for name in PHASES:
        rows = read_jsonl(out / name / "verdicts.jsonl")
        per = Counter(len(r.get("votes") or []) for r in rows if r.get("votes"))
        if per:
            shown = "、".join(f"{v} 票 {n} 条" for v, n in sorted(per.items()))
            lines.append(f"{name}:判官每条 {shown}(B / C 组按拍板 3 票;A 组与闸门按报价)。")
    return lines


def report(out: Path, q: Mapping[str, Any] | None) -> dict[str, Any]:
    outcomes, counts, baseline, missing = collect_outcomes(out)
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
        missing=missing,
    )
    body["disclosures"] += votes_disclosures(out)
    if any(name in REDESIGN_PHASES for name in meta) or any(
        (out / name).exists() for name in REDESIGN_PHASES
    ):
        body["disclosures"] += REDESIGN_DISCLOSURES
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
