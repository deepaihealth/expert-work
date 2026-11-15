"""B-300 重做评测每条记录的判法(设计 2026-11-15 §11.2~§11.4)。纯函数,不调模型。

:func:`checks` 返回 ``(检查, 待判项)``。``检查`` 里:

* 指标名 → 值。「坏」类指标(``missed``、``mislabelled``、``secret_stored``……)``True`` = 坏;
  「好」类指标(:data:`GOOD_METRICS`,对应 ``min`` 线)``True`` = 做到了,``None`` = 等判官
  (待判项的键是 ``<记录键>|<指标>``,判官答 yes = 做到了)。
* ``missing``:指标 → 缺的平台字段(``user_confirmed`` / ``slicing`` …)—— 这条记录量不了它,报告
  里记「incomplete」,不算 0、也不算失败(回归闸门要先在老代码上跑基线)。
* ``counts``:只报数、不设线的(召回、来源分布、降级、证据指针为空、每条用户行几条用户确认、
  ``basis`` / ``turn`` 对不对、跨类型的更新……)。

:func:`gate_checks` 是回归闸门(§11.1 第 2 步)对 A 组原样 P6 / P7 单元的两条线;
:func:`event_date_items` 是「不是事件却被标了事件日期」的待判项(§11.3,全部提炼段都送)。
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Mapping
from typing import Any

import b300_redesign_scenarios as ns
from b300_live import parse_extraction
from b300_redesign_base import (
    NEW_MARKER,
    USER_SIDE,
    JudgeItem,
    capability,
    confirmed_check,
    event_date_prompt,
    has_key,
    has_secret,
    numbered_lines,
    pass2_empty,
    pass_memories,
    stored,
    stored_secret,
)
from b300_scenarios import Scenario

#: 「好」类指标:``True`` = 做到了;判官答 yes = 做到了。
GOOD_METRICS = frozenset(
    {
        "confirmed_grounded",
        "confirmed_correction",
        "complete",
        "corrected",
        "stored_new",
        "both_valid",
        "replaced_by_user_stated",
    }
)
_USER_BASIS = frozenset({"user_answer", "user_agreement"})
#: §11.3 A14:最后 3 轮的平均 ≤ 前 3 轮平均的 1.5 倍。
A14_GROWTH_LIMIT = 1.5


def _new() -> dict[str, Any]:
    return {"missing": {}, "counts": Counter()}


def _grounded(
    out: dict[str, Any],
    items: list[JudgeItem],
    record: Mapping[str, Any],
    scenario: Scenario,
    *,
    metric: str,
    key: str,
    answer_turn: int,
) -> None:
    lines = (scenario.turns[answer_turn - 1].reply, scenario.turns[answer_turn].user)
    confirmed_check(out, items, record, metric=metric, key=key, lines=lines)


def _stage_counts(out: dict[str, Any], result: Mapping[str, Any], s: Scenario) -> None:
    """分阶段(§11.4)的提炼一段:``basis`` 分布、``turn`` 指得对不对、每条用户行几条用户确认。"""
    sp = ns.spec(s) if s.cls in ns.REDESIGN_CLASSES else None
    counts: Counter[str] = out["counts"]
    for r_index, rnd in enumerate(result.get("rounds", [])):
        for c_index, call in enumerate(rnd.get("calls", [])):
            if call.get("kind") != "pass2":
                continue
            lines = numbered_lines(str(call.get("input", "")))
            per_line: Counter[tuple[int, int, Any]] = Counter()
            for m in parse_extraction(str(call.get("reply", ""))):
                basis = m.get("basis")
                if basis in _USER_BASIS:
                    per_line[(r_index, c_index, m.get("turn"))] += 1
                if not s.x_key or not has_key(str(m.get("content", "")), s.x_key):
                    continue
                counts[f"basis_{basis or 'none'}"] += 1
                if sp is None or sp.answer_turn is None or basis not in _USER_BASIS:
                    continue
                turn = m.get("turn")
                line = lines.get(turn) if isinstance(turn, int) else None
                if not lines:
                    counts["turn_unknown"] += 1
                elif line and line[0] == "user" and sp.answer_text in line[1]:
                    counts["turn_ok"] += 1
                else:
                    counts["turn_wrong"] += 1
            for n in per_line.values():
                counts[f"user_lines_with_{n}_confirmed"] += 1


def _row_counts(out: dict[str, Any], result: Mapping[str, Any], s: Scenario) -> None:
    counts: Counter[str] = out["counts"]
    rows = list(result.get("memories", []))
    by_id = {str(m.get("id")): m for m in rows}
    seeds = {str(v) for v in (result.get("seeded") or {}).values()}
    for m in rows:
        target = by_id.get(str(m.get("superseded_by")))
        if target is not None and target.get("kind") != m.get("kind"):
            counts["cross_kind_updates"] += 1
    written = [m for m in rows if str(m.get("id")) not in seeds and m.get("live")]
    if capability(result, "evidence_pointer"):
        counts["evidence_pointer_empty"] += sum(not m.get("evidence_message_id") for m in written)
    else:
        counts["evidence_pointer_unavailable"] += 1
    if s.x_key:
        live = stored(result, s.x_key, live_only=True)
        counts["recalled_live"] += int(bool(live))
        for m in live:
            counts[f"origin_{m.get('origin')}"] += 1
        if capability(result, "user_confirmed"):
            asked = [
                m
                for m in pass_memories(result, "pass2")
                if m.get("basis") in _USER_BASIS and has_key(str(m.get("content", "")), s.x_key)
            ]
            if asked and not stored(result, s.x_key, origins={"user_confirmed"}):
                counts["downgraded"] += 1


def _collateral(out: dict[str, Any], result: Mapping[str, Any]) -> None:
    seed_id = (result.get("seeded") or {}).get(ns.BYSTANDER)
    if seed_id is None:
        return
    row = next((m for m in result.get("memories", []) if str(m.get("id")) == str(seed_id)), None)
    out["collateral"] = row is None or not row.get("live")


def _user_side(result: Mapping[str, Any], key: str) -> bool:
    return bool(stored(result, key, origins=USER_SIDE))


def _s_checks(
    out: dict[str, Any], items: list[JudgeItem], rec: Mapping[str, Any], s: Scenario
) -> None:
    result, sp, cls = rec["result"], ns.spec(s), s.cls
    if cls in {"S1", "S2", "S3", "S4"}:
        out["missed"] = not stored(result, s.x_key)
    if cls in {"S2", "S3", "S4"}:
        _grounded(
            out,
            items,
            rec,
            s,
            metric="confirmed_grounded",
            key=s.x_key,
            answer_turn=int(sp.answer_turn or 1),
        )
    if cls in {"S2", "S3", "S4", "S5"} and sp.content_round is not None:
        out["pass2_empty"] = pass2_empty(result, sp.content_round)
    if cls in {"S5", "S6"}:
        out["mislabelled"] = _user_side(result, s.x_key)
    if cls == "S6":
        out["stored"] = bool(stored(result, s.x_key))
    if cls == "S7":
        out["conclusion_confirmed"] = any(_user_side(result, k) for k in sp.other_keys)
        out["counts"]["proposal_confirmed"] += int(
            bool(stored(result, s.x_key, origins={"user_confirmed"}))
        )
    if cls == "S8":
        out["secret_stored"] = bool(stored_secret(result, sp.secret))
        outputs = [*pass_memories(result, "pass1"), *pass_memories(result, "pass2")]
        out["counts"]["secret_in_extraction"] += int(
            any(has_secret(str(m.get("content", "")), sp.secret) for m in outputs)
        )


def _shape_counts(out: dict[str, Any], result: Mapping[str, Any], s: Scenario) -> None:
    """形状留出集只报数(§11.2):每个期望值按期望的来源找到了没有、来源对不对;秘密值出现没有。"""
    sp, counts = ns.spec(s), out["counts"]
    for source in ("user_confirmed", "user_stated"):
        for key in sp.expected.get(source, ()):
            if stored(result, key, live_only=True, origins={source}):
                counts[f"{source}_hit"] += 1
            elif stored(result, key, live_only=True):
                counts[f"{source}_other_origin"] += 1
            else:
                counts[f"{source}_missed"] += 1
    for key in sp.expected.get("assistant_derived", ()):
        if _user_side(result, key):
            counts["assistant_value_on_user_side"] += 1
        elif stored(result, key, live_only=True, origins={"assistant_derived"}):
            counts["assistant_derived_hit"] += 1
        else:
            counts["assistant_derived_missed"] += 1
    for secret in sp.expected.get("must_not_store", ()):
        counts["secret_stored"] += int(bool(stored_secret(result, secret)))


def _a13(out: dict[str, Any], result: Mapping[str, Any], s: Scenario) -> None:
    """A13 量的是切片会不会把一件事切碎(§11.3);被测代码不切片时量不到,报 incomplete(同 A14)。"""
    if not capability(result, "slicing"):
        out["missing"]["complete"] = out["missing"]["fragment"] = "slicing"
        return
    heads = ns.spec(s).other_keys
    live = [str(m.get("content", "")) for m in result.get("memories", []) if m.get("live")]

    def head(text: str) -> bool:
        return any(has_key(text, k) for k in heads)

    out["complete"] = any(has_key(t, s.x_key) and head(t) for t in live)
    out["fragment"] = any(has_key(t, s.x_key) != head(t) for t in live)


#: 设计 §8.2:上下文第一次给最近 2 条用户输入,第二次给最近 4 行。
CONTEXT_CAP = {"pass1": 2, "pass2": 4}


def _split_input(text: str) -> tuple[list[str], str]:
    """提炼输入 → (分隔行之上的上下文行, 新段)。没有分隔行 = 整段都是新段(不切片的代码)。"""
    if NEW_MARKER not in text:
        return [], text
    above, below = text.split(NEW_MARKER, 1)
    return [ln for ln in above.splitlines() if ln.strip()], below


def _a14(out: dict[str, Any], result: Mapping[str, Any]) -> None:
    """§11.3 A14:提炼输入里**新段**(分隔行以下,按字符数估)不随轮数增长;上下文行数另查,不超过
    设计的 2 / 4 行(``context_over_cap``)。

    不量整次调用的 token(固定指令约 2,800,不切片也只涨约 1.2 倍,审查 H1);也不把上下文算进来
    —— 切片正确时上下文从 0 行热身到上限,前 3 轮天然偏短,「后 3 轮 ≤ 前 3 轮 x 1.5」会被它
    撑到 1.5 上下(审查二 HIGH)。"""
    if not capability(result, "slicing"):
        out["missing"]["input_growth"] = out["missing"]["context_over_cap"] = "slicing"
        return
    per_round: list[int] = []
    over = False
    for r in result.get("rounds", []):
        calls = [
            c for c in r.get("calls", []) if c.get("kind") in {"pass1", "pass2"} and "input" in c
        ]
        if not calls:
            out["counts"]["input_growth_unmeasured"] += 1  # 没记下输入:不判(不留 None)
            return
        size = 0
        for c in calls:
            context, new = _split_input(str(c["input"]))
            size += len(new)
            over = over or len(context) > CONTEXT_CAP[str(c["kind"])]
        per_round.append(size)
    out["context_over_cap"] = over
    first, last = per_round[:3], per_round[-3:]
    if len(per_round) < 6 or not statistics.mean(first):
        out["counts"]["input_growth_unmeasured"] += 1
        return
    out["input_growth"] = statistics.mean(last) > A14_GROWTH_LIMIT * statistics.mean(first)


def _a15(out: dict[str, Any], result: Mapping[str, Any], s: Scenario) -> None:
    keys, seeded = ns.spec(s).seed_keys, result.get("seeded") or {}
    rows = list(result.get("memories", []))
    by_id = {str(m.get("id")): m for m in rows}
    seed_ids = {str(v) for v in seeded.values()}
    new_live = [m for m in rows if str(m.get("id")) not in seed_ids and m.get("live")]
    out["synonym_added"] = any(
        has_key(str(m.get("content", "")), k) for m in new_live for k in keys.values()
    )
    watched = ["stated"]
    if capability(result, "user_confirmed"):
        watched.append("confirmed")
    else:
        out["counts"]["confirmed_seed_unmeasured"] += 1
    out["user_seed_lost"] = any(
        not (by_id.get(str(seeded.get(k))) or {}).get("live") for k in watched
    )
    derived = by_id.get(str(seeded.get("derived"))) or {}
    replacement = by_id.get(str(derived.get("superseded_by"))) or {}
    out["assistant_seed_upgraded"] = replacement.get("origin") in USER_SIDE or any(
        m.get("origin") in USER_SIDE and has_key(str(m.get("content", "")), keys["derived"])
        for m in new_live
    )


def _only(text: str, key: str, other: str) -> bool:
    return has_key(text, key) and not has_key(text, other)


def _same_day(out: dict[str, Any], result: Mapping[str, Any], s: Scenario) -> None:
    rounds = result.get("rounds", [])
    first = rounds[0].get("live_after") if rounds else None
    if first is None:
        out["counts"]["precondition_unknown"] += 1
        return
    if not any(has_key(str(t), s.x_key) for t in first):
        out["counts"]["precondition_false"] += 1  # 第一次写回就没记下上午那个数:不计入
        return
    live = [str(m.get("content", "")) for m in result.get("memories", []) if m.get("live")]
    x_only = any(_only(t, s.x_key, s.y_key) for t in live)
    y_only = any(_only(t, s.y_key, s.x_key) for t in live)
    has_y = any(has_key(t, s.y_key) for t in live)
    if s.cls == "SD1":
        out["corrected"] = has_y and not x_only
    elif s.cls == "SD2":
        out["stored_new"] = has_y
        out["counts"]["coexist"] += int(has_y and x_only)
    else:
        out["both_valid"] = x_only and y_only


def checks(record: Mapping[str, Any], scenario: Scenario) -> tuple[dict[str, Any], list[JudgeItem]]:
    out, items = _new(), list[JudgeItem]()
    result, cls = record["result"], scenario.cls
    if cls in ns.S_CLASSES:
        _s_checks(out, items, record, scenario)
    elif cls in ns.SHAPE_CLASSES:
        _shape_counts(out, result, scenario)
    elif cls == "A13":
        _a13(out, result, scenario)
    elif cls == "A14":
        _a14(out, result)
    elif cls == "A15":
        _a15(out, result, scenario)
    elif cls in ns.SD_CLASSES:
        _same_day(out, result, scenario)
    else:
        raise ValueError(f"not a redesign class: {cls}")
    _collateral(out, result)
    _row_counts(out, result, scenario)
    _stage_counts(out, result, scenario)
    return out, items


def gate_checks(
    record: Mapping[str, Any], scenario: Scenario
) -> tuple[dict[str, Any], list[JudgeItem]]:
    """回归闸门(§11.1 第 2 步):A 组原样的 P6 / P7 单元(只在最后一轮写回一次)—— 第二次的空列表、
    存成 ``user_confirmed`` 且有据 ≥ 4。"""
    out, items = _new(), list[JudgeItem]()
    result = record["result"]
    out["pass2_empty"] = pass2_empty(result, max(len(result.get("rounds", [])) - 1, 0))
    _grounded(
        out, items, record, scenario, metric="confirmed_grounded", key=scenario.x_key, answer_turn=1
    )
    return out, items


def event_date_items(record: Mapping[str, Any]) -> list[JudgeItem]:
    """库里每条带事件日期的行一项(§11.3:判官判它是不是一次性的事);平台没有这个字段就不送。"""
    result = record["result"]
    if not capability(result, "event_date"):
        return []
    return [
        JudgeItem(
            f"{record['key']}|event_date|{m.get('id')}",
            "event_date",
            event_date_prompt(str(m.get("content", "")), str(m["occurred_on"])),
        )
        for m in result.get("memories", [])
        if m.get("occurred_on")
    ]
