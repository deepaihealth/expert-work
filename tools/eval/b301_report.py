"""B-301 PR 2 体检的判分与报告(设计 §9.2 各线)。离线,只读结果文件与花费账本。

关键字段两种判法并列报告:

* **严格**(按类结论「有收益 / 无收益 / 变差」用它):规范化后与关键字段相等;或关键字段作为整体
  出现在回答里,并且
  - 关键字段带数字时:回答里没有别的数 / 编号(「可能是 69.1,也可能是 1.69」算错;「87.5(第4周)」
    这种带了别的数的对答案也算错 —— 所以报告把严格与宽松不一致的回答逐条列出,供人工看);
  - 关键字段不带数字时:回答没有给备选(``or`` / ``maybe`` / ``possibly`` / ``perhaps`` / 或 /
    也可能 / 可能是),也没有比「关键字段 + 一个短标签」长出很多。
* **宽松**:关键字段作为整体出现在回答里就算对。

规范化:NFKC(全角转半角)、小写、去空白 / 千分位逗号 / 引号括号 / markdown 记号、去句末标点。
只有种类是数(``number``,含时刻)的关键字段把数统一写法(``36.80`` = ``36.8``,``08:47`` =
``8:47``);编号、日期按原样比(``7731-0428`` 不等于 ``7731-428``)。
"""

from __future__ import annotations

import math
import re
import statistics
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

import b301_images as images_mod
import b301_spend as spend
from b301_health import (
    L1_MIN,
    L1_PER_CLASS_MIN,
    L1P_UNDETERMINED_MAX,
    L2_CONTROL_HIGH,
    L2_CONTROL_LOW,
    L2_MIN,
    L2_PREMISE_MIN,
    L6_MAX_RATE,
    WORSE_BY,
)

from orchestrator.tools.image_orientation import (
    ROTATED_NOTE,
    TURNS,
    UNDETERMINED_NOTE,
    first_conclusion,
    turned_conclusion,
)
from orchestrator.tools.region_zoom import REGION_HINT

#: 弯引号写成转义:ruff 的模糊字符规则会把全角标点「修」成半角。空白另行处理(见 :func:`_norm`)。
_DROP = re.compile(
    "[,\"'\\u201c\\u201d\\u2018\\u2019`*()\\[\\]{}<>\\u300a\\u300b\\u300c\\u300d\\u3010\\u3011]"
)
_SPACE = re.compile(r"\s+")
_TRAILING = re.compile(r"[。.!?;]+$")
_NUMBER = re.compile(r"\d+(?:[.:]\d+)*")
_PART = re.compile(r"([.:])")
_DIGIT = re.compile(r"\d")
#: 数统一写法只用在这些种类的关键字段上。
NUMERIC_KINDS = frozenset({"number", "time"})
#: 不带数字的关键字段:回答里出现这些就是给了备选(在去掉关键字段之后的剩余部分里找)。
_HEDGE = re.compile(r"\b(?:or|maybe|possibly|perhaps)\b|或|也可能|可能是.*也")
#: 不带数字的关键字段:回答比关键字段长出这么多字符(规范化后、去空白)以上,就不是「只写字段」。
STRICT_EXTRA_CHARS = 16


def _canon_number(match: re.Match[str]) -> str:
    """一个数的统一写法:各段去掉前导 0;小数点后的那段去掉末尾 0(全是 0 就连小数点一起去)。"""
    pieces = _PART.split(match.group(0))
    out = [pieces[0].lstrip("0") or "0"]
    for sep, part in zip(pieces[1::2], pieces[2::2], strict=True):
        if sep == ".":
            part = part.rstrip("0")
            if part:
                out.append("." + part)
        else:
            out.append(sep + (part.lstrip("0") or "0"))
    return "".join(out)


def _norm(text: str, *, keep_space: bool, numeric: bool) -> str:
    folded = unicodedata.normalize("NFKC", text).lower()
    folded = _TRAILING.sub("", _DROP.sub("", _SPACE.sub(" " if keep_space else "", folded)).strip())
    return _NUMBER.sub(_canon_number, folded) if numeric else folded


def normalize(text: str, *, numeric: bool = False) -> str:
    """见模块说明(去掉空白)。保留 ``. - / : % _``:它们在日期、编号、读数里有意义。"""
    return _norm(text, keep_space=False, numeric=numeric)


def _glued(neighbour: str, edge: str) -> bool:
    """紧挨着的是同一类(数字 / 小数点挨数字,字母挨字母)才算粘连:``169.1`` 里没有 ``69.1``,
    ``69.1kg`` 里有;空格两边不粘连。"""

    def kind(ch: str) -> str:
        if ch.isascii() and (ch.isdigit() or ch == "."):
            return "number"
        if ch.isascii() and ch.isalpha():
            return "letter"
        return ""

    return bool(neighbour) and kind(neighbour) != "" and kind(neighbour) == kind(edge)


def _occurrences(key: str, answer: str, *, numeric: bool) -> tuple[str, list[tuple[int, int]]]:
    """回答(保留单个空格的规范化形态)和关键字段在其中作为整体出现的位置。关键字段各字符之间
    允许多一个空格(``4:30pm`` 与 ``4:30 pm`` 一样);两侧按 :func:`_glued` 判断边界。"""
    want = normalize(key, numeric=numeric)
    spaced = _norm(answer, keep_space=True, numeric=numeric)
    if not want:
        return spaced, []
    pattern = re.compile(" ?".join(re.escape(ch) for ch in want))
    spans: list[tuple[int, int]] = []
    for match in pattern.finditer(spaced):
        start, end = match.span()
        before = spaced[start - 1] if start > 0 else ""
        after = spaced[end] if end < len(spaced) else ""
        if not _glued(before, want[0]) and not _glued(after, want[-1]):
            spans.append((start, end))
    return spaced, spans


def field_matches(key: str, answer: str, *, numeric: bool = False) -> bool:
    """宽松:规范化后相等,或作为整体出现在回答里。"""
    want = normalize(key, numeric=numeric)
    if want and normalize(answer, numeric=numeric) == want:
        return True
    return bool(_occurrences(key, answer, numeric=numeric)[1])


def field_matches_strict(key: str, answer: str, *, numeric: bool = False) -> bool:
    """严格:相等;或宽松算对、且回答没有给别的候选(见模块说明)。"""
    want = normalize(key, numeric=numeric)
    if want and normalize(answer, numeric=numeric) == want:
        return True
    spaced, spans = _occurrences(key, answer, numeric=numeric)
    if not spans:
        return False
    pieces, cursor = [], 0
    for start, end in spans:
        pieces.append(spaced[cursor:start])
        cursor = end
    pieces.append(spaced[cursor:])
    rest = " ".join(pieces)
    if _DIGIT.search(want):
        return not _DIGIT.search(rest)
    if _HEDGE.search(rest):
        return False
    return len(normalize(answer, numeric=numeric)) <= len(want) + STRICT_EXTRA_CHARS


def strip_notes(content: str) -> str:
    """去掉工具追加在回答后面的说明(转过 / 判不出 / 建议用 region),只留看图模型的回答。"""
    for note in (UNDETERMINED_NOTE, REGION_HINT, *(ROTATED_NOTE.format(degrees=d) for d in TURNS)):
        content = content.replace(note, "")
    return content.strip()


def orientation_of(record: Mapping[str, Any]) -> tuple[str, int, str | None]:
    """(结论, 角度, 第 1 步的回答)。工具结果里有 meta 就用它;工具出错没有 meta 时按调用记录
    用同一套规则推出来(第 2 步三个角度靠副本路径里的 ``r<角度>`` 分辨)。"""
    meta = record.get("orientation")
    if isinstance(meta, Mapping):
        first_answer = (meta.get("answers") or {}).get("0")
        return str(meta["conclusion"]), int(meta["degrees"]), first_answer
    answers: dict[int, Any] = {}
    for call in record.get("calls", []):
        if call.get("kind") == "orientation":
            answers[int(call.get("turn", 0))] = call.get("answer")
    first = answers.get(0)
    if first is None:
        return "not_asked", 0, None
    conclusion = first_conclusion(first)
    if conclusion is not None:
        return conclusion, 0, first
    if not all(turn in answers for turn in TURNS):
        return "not_asked", 0, first
    turned, degrees = turned_conclusion({turn: answers[turn] for turn in TURNS})
    return turned, degrees, first


def _latency_stats(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"median_s": None, "p95_s": None}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]
    return {"median_s": round(statistics.median(ordered), 2), "p95_s": round(p95, 2)}


def _read_right(
    record: Mapping[str, Any] | None, image: images_mod.SetImage, *, strict: bool
) -> bool:
    """这一条读出了关键字段(工具出错的一律算没读出)。"""
    if record is None or record.get("status") != "ok" or image.key is None:
        return False
    answer, numeric = str(record.get("answer", "")), image.kind in NUMERIC_KINDS
    match = field_matches_strict if strict else field_matches
    return match(image.key, answer, numeric=numeric)


#: 报告里每条「严格与宽松不一致」的回答最多显示多少字。
DISAGREEMENT_CHARS = 120


def _classify(upright_ok: int, on_ok: int, control_ok: int) -> str:
    if upright_ok < L2_PREMISE_MIN:
        return "模型本来就读不好(不计入)"
    if on_ok <= control_ok - WORSE_BY:
        return "变差"
    if control_ok >= L2_CONTROL_HIGH:
        return "无收益"
    if control_ok <= L2_CONTROL_LOW and on_ok >= L2_MIN:
        return "有收益"
    return "介于之间"


def _l1(
    on: Mapping[str, Mapping[str, Any]], rotated: Sequence[images_mod.SetImage]
) -> dict[str, Any]:
    ok, by_class = 0, dict[str, int]()
    for image in rotated:
        record = on.get(image.image_id)
        hit = record is not None and orientation_of(record)[:2] == ("rotated", image.rotation)
        ok += hit
        by_class[image.image_class] = by_class.get(image.image_class, 0) + hit
    passed = ok >= L1_MIN and all(v >= L1_PER_CLASS_MIN for v in by_class.values())
    return {"ok": ok, "of": len(rotated), "by_class": by_class, "pass": passed}


def _l1_prime(on: Mapping[str, Mapping[str, Any]], upright_ids: Sequence[str]) -> dict[str, Any]:
    turned: list[str] = []
    undetermined: list[str] = []
    first_no = 0
    for image_id in upright_ids:
        record = on.get(image_id)
        if record is None:
            continue
        conclusion, _degrees, first = orientation_of(record)
        turned += [image_id] if conclusion == "rotated" else []
        undetermined += [image_id] if conclusion == "undetermined" else []
        first_no += first == "no"
    passed = not turned and len(undetermined) <= L1P_UNDETERMINED_MAX
    return {
        "turned": turned,
        "undetermined": undetermined,
        "first_step_no": first_no,
        "of": len(upright_ids),
        "pass": passed,
    }


def _l2(
    on: Mapping[str, Mapping[str, Any]],
    control: Mapping[str, Mapping[str, Any]],
    image_set: Sequence[images_mod.SetImage],
) -> dict[str, dict[str, Any]]:
    classes: dict[str, dict[str, Any]] = {}
    for image in image_set:
        if image.key is None:
            continue
        c = classes.setdefault(
            image.image_class,
            {
                "upright": 0,
                "on": 0,
                "control": 0,
                "lenient": {"upright": 0, "on": 0, "control": 0},
                "disagreements": [],
            },
        )
        for mode, record in (
            ("on", on.get(image.image_id)),
            ("control", control.get(image.image_id)),
        ):
            strict = _read_right(record, image, strict=True)
            lenient = _read_right(record, image, strict=False)
            slot = mode if mode == "control" or not image.upright else "upright"
            c[slot] += strict
            c["lenient"][slot] += lenient
            if strict != lenient and record is not None:
                c["disagreements"].append(
                    {
                        "image_id": image.image_id,
                        "mode": mode,
                        "key": image.key,
                        "answer": str(record.get("answer", ""))[:DISAGREEMENT_CHARS],
                    }
                )
    for c in classes.values():
        c["l2_pass"] = c["upright"] < L2_PREMISE_MIN or c["on"] >= L2_MIN
        c["result"] = _classify(c["upright"], c["on"], c["control"])
    return classes


def _l6(on: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """问成率。厂商返回超时以外的错误会让整个 run 停下、那一条作废,不在结果里 —— 它的次数从
    账本单独报(``error_stops``)。"""
    calls = [
        call
        for r in on.values()
        for call in r.get("calls", [])
        if call.get("kind") == "orientation"
    ]
    timeouts = sum(call.get("outcome") == "timeout" for call in calls)
    off_topic = sum(
        call.get("outcome") == "ok" and call.get("answer") == "off_topic" for call in calls
    )
    bad_rate = (timeouts + off_topic) / len(calls) if calls else 1.0
    latency = _latency_stats([c["latency_s"] for c in calls if c.get("outcome") == "ok"])
    return {
        "calls": len(calls),
        "timeouts": timeouts,
        "off_topic": off_topic,
        "bad_rate": round(bad_rate, 4),
        **latency,
        "pass": bool(calls) and bad_rate <= L6_MAX_RATE,
    }


def model_report(
    model: str,
    records: Sequence[Mapping[str, Any]],
    image_set: Sequence[images_mod.SetImage],
    ledger_rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """一个体检模型的 §9.2 各线与结论。通过 = 数据齐 + L1' 过 + L6 过。"""
    on = {r["image_id"]: r for r in records if r["model"] == model and r["mode"] == "on"}
    control = {r["image_id"]: r for r in records if r["model"] == model and r["mode"] == "control"}
    upright_ids = [i.image_id for i in image_set if i.upright]
    rotated = [i for i in image_set if not i.upright]
    missing = len(upright_ids) + len(rotated) - len(on) + len(rotated) - len(control)
    l1, l1p, classes, l6 = (
        _l1(on, rotated),
        _l1_prime(on, upright_ids),
        _l2(on, control, image_set),
        _l6(on),
    )
    why: list[str] = []
    if missing:
        why.append(f"数据不齐:还差 {missing} 条")
    if l1p["turned"]:
        why.append(f"L1' 正放的图被转了 {len(l1p['turned'])} 张:{', '.join(l1p['turned'])}")
    if len(l1p["undetermined"]) > L1P_UNDETERMINED_MAX:
        why.append(f"L1' 「判不出」{len(l1p['undetermined'])} 张 > {L1P_UNDETERMINED_MAX}")
    if not l6["pass"]:
        why.append(f"L6 问方向没问成的比例 {l6['bad_rate']:.1%} > {L6_MAX_RATE:.0%}")
    verdict = "不完整" if missing else ("通过" if l1p["pass"] and l6["pass"] else "不通过")
    worse = [name for name, c in classes.items() if c["result"] == "变差"]
    own_ledger = [row for row in ledger_rows if row.get("model") == model]
    return {
        "model": model,
        "verdict": verdict,
        "why": why,
        "needs_user": [f"L2「变差」:{', '.join(worse)}"] if worse else [],
        "L1": l1,
        "L1'": l1p,
        "L2": classes,
        "L6": l6,
        "error_stops": spend.error_stops(own_ledger).get(model, 0),
        "spent_records_yuan": round(
            sum(float(r.get("cost_yuan", 0.0)) for r in records if r["model"] == model), 4
        ),
        "spent_ledger_yuan": round(spend.ledger_total(own_ledger), 4),
    }


def probe_report(
    model: str,
    records: Sequence[Mapping[str, Any]],
    image_set: Sequence[images_mod.SetImage],
    ledger_rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """§8.1:正放答 yes(或无文字答 none)、转过的答 no 的张数;没答成的另计。"""
    rotation = {i.image_id: (i.rotation, i.image_class) for i in image_set}
    rows = [r for r in records if r["model"] == model and r["mode"] == "probe"]
    counts = dict.fromkeys(
        ("upright_yes", "upright", "rotated_no", "rotated", "no_text_none", "no_text"), 0
    )
    unanswered = 0
    for r in rows:
        degrees, image_class = rotation[r["image_id"]]
        answer = r.get("probe_answer")
        unanswered += answer not in ("yes", "no", "none")
        if image_class == images_mod.NO_TEXT_CLASS:
            counts["no_text"] += 1
            counts["no_text_none"] += answer == "none"
        elif degrees == 0:
            counts["upright"] += 1
            counts["upright_yes"] += answer == "yes"
        else:
            counts["rotated"] += 1
            counts["rotated_no"] += answer == "no"
    latency = _latency_stats(
        [c["latency_s"] for r in rows for c in r.get("calls", []) if c.get("outcome") == "ok"]
    )
    own_ledger = [row for row in ledger_rows if row.get("model") == model]
    return {
        "model": model,
        **counts,
        "unanswered": unanswered,
        **latency,
        "error_stops": spend.error_stops(own_ledger).get(model, 0),
    }


def report(
    records: Sequence[Mapping[str, Any]],
    image_set: Sequence[images_mod.SetImage] | None = None,
    ledger_rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    image_set = images_mod.build_set() if image_set is None else image_set
    health = sorted({r["model"] for r in records if r["mode"] in ("on", "control")})
    probe = sorted({r["model"] for r in records if r["mode"] == "probe"})
    return {
        "health": [model_report(m, records, image_set, ledger_rows) for m in health],
        "probe": [probe_report(m, records, image_set, ledger_rows) for m in probe],
        "spent_records_yuan": round(sum(float(r.get("cost_yuan", 0.0)) for r in records), 4),
        "spent_ledger_yuan": round(spend.ledger_total(ledger_rows), 4),
    }


def _mark(passed: bool) -> str:
    return "过" if passed else "没过"


def _render_model(m: Mapping[str, Any]) -> list[str]:
    l1, l1p, l6 = m["L1"], m["L1'"], m["L6"]
    lines = [f"== {m['model']}:{m['verdict']}"]
    lines.extend(f"   - {why}" for why in m["why"])
    lines.extend(f"   - 交用户定:{item}" for item in m["needs_user"])
    lines.append(
        f"   L1  转过的 {l1['ok']}/{l1['of']}"
        f"(及格 ≥{L1_MIN},每类 ≥{L1_PER_CLASS_MIN}/9):{_mark(l1['pass'])}"
    )
    lines.append("       每类:" + ", ".join(f"{k} {v}/9" for k, v in l1["by_class"].items()))
    lines.append(
        f"   L1' 正放 + 无文字 {l1p['of']} 张:被转 {len(l1p['turned'])}、"
        f"判不出 {len(l1p['undetermined'])}、第 1 步答 no {l1p['first_step_no']}:"
        f"{_mark(l1p['pass'])}"
    )
    lines.append("   L2  每类 严格(宽松):正放 /3、开 /9、对照 /9 → 结论(按严格)")
    for name, c in m["L2"].items():
        loose = c["lenient"]
        lines.append(
            f"       {name}: {c['upright']}({loose['upright']})/3, {c['on']}({loose['on']})/9, "
            f"{c['control']}({loose['control']})/9 → {c['result']}"
        )
        for d in c["disagreements"]:  # 严格算错、宽松算对:人工看一眼是不是其实读对了
            lines.append(
                f"         严格≠宽松 {d['image_id']} ({d['mode']}) 关键字段 {d['key']!r}:"
                f"{d['answer']!r}"
            )
    lines.append(
        f"   L6  问方向 {l6['calls']} 次:超时 {l6['timeouts']}、答非所问 {l6['off_topic']}"
        f"({l6['bad_rate']:.1%});耗时中位 {l6['median_s']} s、p95 {l6['p95_s']} s:"
        f"{_mark(l6['pass'])}"
    )
    lines.append(f"   厂商出错而停(账本):{m['error_stops']} 次")
    lines.append(
        f"   花费:账本 ¥{m['spent_ledger_yuan']}(结果里跑完的条目 ¥{m['spent_records_yuan']})"
    )
    return lines


def render_report(result: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for m in result["health"]:
        lines.extend(_render_model(m))
    for p in result["probe"]:
        lines.append(
            f"== §8.1 {p['model']}:正放答 yes {p['upright_yes']}/{p['upright']}、"
            f"转过答 no {p['rotated_no']}/{p['rotated']}、"
            f"无文字答 none {p['no_text_none']}/{p['no_text']}、没答成 {p['unanswered']}、"
            f"厂商出错而停 {p['error_stops']} 次;耗时中位 {p['median_s']} s"
        )
    lines.append(
        f"合计花费:账本 ¥{result['spent_ledger_yuan']}"
        f"(结果里跑完的条目 ¥{result['spent_records_yuan']})"
    )
    return "\n".join(lines)
