"""B-302 PR 2 A/B —— 指标与及格线(设计 §5.4,先写死,纯函数)。

``docs/superpowers/specs/2026-11-11-b302-runaway-reverify-design.md`` §5.4:

* 每次运行(一轮)记的量 → :class:`Turn` / :class:`Step`;每份文档汇总成 :class:`DocResult`。
* 「重抄步」:标准答案每页全文与每步的思考 + 回复都 NFKC 规整、去空白,切字符 6-gram;某步覆盖了
  本轮此前已取过的某页 6-gram 的 ≥ 15% 记一次;每次取页之后的第一步不算(:func:`recopy_flags`)。
* 「失控」按症状定义(:func:`runaway_reasons`),判据 0~9 与每个模型的结论(:func:`model_verdict`),
  结论怎么落到配置(:func:`landing`)。

全部与内容和语言无关:按码点 / 6-gram 比,不切词。
"""

from __future__ import annotations

import itertools
import re
import statistics
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

NGRAM = 6
RECOPY_COVERAGE = 0.15
RECOPY_SHARE_RUNAWAY = 0.30
CALLS_AFTER_DELIVERABLE_RUNAWAY = 10
FIELD_FLIPS_RUNAWAY = 3
#: 失控第 4 条只认时限与步数;token 提醒 / 封顶受页图分量影响,单独报告(复审 R3-M1)。
LIMIT_SIGNALS = frozenset({"deadline", "max_steps"})
#: glm-5.3-flashx 与 glm-5.3-flash 是同一个模型(``model_catalog.py`` 的注释),继承它的结论。
INHERITS = {"glm-5.3-flashx": "glm-5.3-flash"}


@dataclass(frozen=True)
class Call:
    name: str
    args: Mapping[str, Any]


@dataclass(frozen=True)
class Step:
    """一次模型调用:思考(厂商不返回时为空)、回复、工具调用、思考 token。"""

    index: int
    thinking: str = ""
    reply: str = ""
    calls: tuple[Call, ...] = ()
    reasoning_tokens: int | None = None

    @property
    def text(self) -> str:
        return f"{self.thinking}\n{self.reply}"


@dataclass(frozen=True)
class Field:
    tag: str
    label: str
    value: str


@dataclass(frozen=True)
class Turn:
    """一轮(= 一次运行)。``first_deliverable_step``:工作区里第一次出现所要类型文件时的那一步。"""

    doc_id: str
    steps: tuple[Step, ...]
    exit_reason: str | None
    first_deliverable_step: int | None = None
    warnings: tuple[str, ...] = ()


# --------------------------------------------------------------------------- text


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def count_correct(deliverable: str, fields: Iterable[Field]) -> int:
    """交付件里关键字段对了几个:NFKC 规整、去空白后逐字找标准答案。"""
    haystack = normalize(deliverable)
    return sum(1 for f in fields if normalize(f.value) and normalize(f.value) in haystack)


def _has_cjk(text: str) -> bool:
    return any("㐀" <= c <= "鿿" for c in text)


def _cut(raw: str, gold: str) -> str:
    """从分隔符之后的原文里截出「这一次写成了什么」。"""
    stops = ["\n", ";", "\uff1b", "|", "\uff0c", "\u3002"]
    if ", " not in gold:
        stops.append(", ")
    if not re.search(r"\s", gold) and not _has_cjk(gold):
        stops.extend([" ", "\t"])
    end = min((i for i in (raw.find(s) for s in stops) if i >= 0), default=len(raw))
    text = raw[: min(end, len(gold) * 2 + 6)]
    return normalize(text).rstrip(",.;:\u3002\uff0c\uff1b\uff1a")


def readings(texts: Iterable[str], fld: Field) -> list[str]:
    """轨迹里每一次提到这个字段时写成的样子(按出现次序);先认标签,文字里没有标签再认字段名。"""
    tag = re.compile(
        re.escape(fld.tag) + r"\]?\s*(?:" + re.escape(fld.label) + r")?\s*[:\uff1a]?\s*(.*)"
    )
    label = re.compile(re.escape(fld.label) + r"\s*[:\uff1a]\s*(.*)")
    out: list[str] = []
    for text in texts:
        matches = list(tag.finditer(text)) or list(label.finditer(text))
        for match in matches:
            reading = _cut(match.group(1), fld.value)
            if reading:
                out.append(reading)
    return out


def changes(values: Sequence[str]) -> int:
    return sum(1 for a, b in itertools.pairwise(values) if a != b)


def variants(values: Sequence[str]) -> int:
    return len(set(values))


# --------------------------------------------------------------------------- reads


def read_events(steps: Sequence[Step]) -> list[tuple[int, str, tuple[int, ...]]]:
    """``(step, 文件名, units)``:每次 ``read_page``(整页或局部)。"""
    events: list[tuple[int, str, tuple[int, ...]]] = []
    for step in steps:
        for call in step.calls:
            if call.name != "read_page":
                continue
            units = call.args.get("units")
            unit_list = (
                tuple(u for u in units if isinstance(u, int)) if isinstance(units, list) else ()
            )
            events.append(
                (step.index, PurePosixPath(str(call.args.get("path", ""))).name, unit_list)
            )
    return events


def _grams(text: str) -> set[str]:
    norm = normalize(text)
    return {norm[i : i + NGRAM] for i in range(max(0, len(norm) - NGRAM + 1))}


def recopy_flags(steps: Sequence[Step], pages: Mapping[tuple[str, int], str]) -> list[bool]:
    """每一步是不是「重抄步」。``pages`` 的键是 ``(文件名, unit)``。"""
    page_grams = {key: _grams(text) for key, text in pages.items()}
    fetched: set[tuple[str, int]] = set()
    first_after_fetch: set[int] = set()
    events = read_events(steps)
    flags: list[bool] = []
    for step in steps:
        for idx, name, units in events:
            if idx < step.index:
                fetched.update((name, u) for u in units)
                first_after_fetch.add(idx + 1)
        if step.index in first_after_fetch:
            flags.append(False)
            continue
        grams = _grams(step.text)
        flags.append(
            any(
                page_grams.get(key)
                and len(page_grams[key] & grams) / len(page_grams[key]) >= RECOPY_COVERAGE
                for key in fetched
            )
        )
    return flags


def refetches_after_retire(
    steps: Sequence[Step], *, view_steps: int, read_earlier: Iterable[str] = ()
) -> int:
    """页退役之后对同一文档的重取次数。第 j 步取的页从 j+1 步起可见,第 j+1+N 步起过期;
    上一轮读过、本轮第一次再读的也算(跨轮一律过期)。"""
    last: dict[str, int] = {}
    earlier = {PurePosixPath(p).name for p in read_earlier}
    count = 0
    for idx, name, _units in read_events(steps):
        if (name in last and idx >= last[name] + 1 + view_steps) or (
            name not in last and name in earlier
        ):
            count += 1
        last[name] = idx
    return count


def rotation(calls: Sequence[Sequence[int]], *, pages: int) -> bool:
    """B-268 那种轮转:一页被重看时,自它上次被读以来别的每一页都读过了(它早被挤出窗口)。"""
    order = [u for call in calls for u in call]
    others = set(range(1, pages + 1))
    for i, unit in enumerate(order):
        earlier = [j for j in range(i) if order[j] == unit]
        if earlier and (others - {unit}) <= set(order[earlier[-1] + 1 : i]):
            return True
    return False


# --------------------------------------------------------------------------- runaway


def runaway_reasons(
    turn: Turn, fields: Sequence[Field], pages: Mapping[tuple[str, int], str]
) -> list[str]:
    """第 2 轮的「失控」症状(任一条即算):交付件产出后又 ≥ 10 次调用、重抄步 ≥ 30%、任一关键字段
    相邻两次变了 ≥ 3 次、收到时限 / 步数提醒或撞了时限 / 步数封顶。"""
    reasons: list[str] = []
    if (
        turn.first_deliverable_step is not None
        and len(turn.steps) - 1 - turn.first_deliverable_step >= CALLS_AFTER_DELIVERABLE_RUNAWAY
    ):
        reasons.append("calls_after_deliverable")
    if (
        turn.steps
        and sum(recopy_flags(turn.steps, pages)) / len(turn.steps) >= RECOPY_SHARE_RUNAWAY
    ):
        reasons.append("recopy")
    texts = [s.text for s in turn.steps]
    if any(changes(readings(texts, f)) >= FIELD_FLIPS_RUNAWAY for f in fields):
        reasons.append("field_flips")
    if LIMIT_SIGNALS & set(turn.warnings) or turn.exit_reason in LIMIT_SIGNALS:
        reasons.append("limit")
    return reasons


# --------------------------------------------------------------------------- verdicts


@dataclass(frozen=True)
class DocResult:
    """一个模型、一组、一份文档的汇总(第 2 轮为主,第 3 轮只给 ``correction_applied``)。"""

    doc_id: str
    category: str
    runaway: bool
    correct: int
    flipped_fields: int
    recopy_share: float
    calls_after_deliverable: int | None
    thinking_median: float | None
    refetches: int
    no_progress_exit: bool
    wall_s: float
    tokens: int
    correction_applied: bool | None = None
    template_ok: bool | None = None
    structure: int | None = None


def _pairs(a: Sequence[DocResult], x: Sequence[DocResult]) -> list[tuple[DocResult, DocResult]]:
    by_id = {d.doc_id: d for d in x}
    return [(d, by_id[d.doc_id]) for d in a if d.doc_id in by_id]


def reproduced(a: Sequence[DocResult]) -> bool:
    """判据 0:A 组 12 份里至少 3 份失控。"""
    return sum(d.runaway for d in a) >= 3


def fewer_runaways(a: Sequence[DocResult], x: Sequence[DocResult]) -> bool:
    """判据 1:「A 失控、X 不失控」≥ 3 份,「A 不失控、X 失控」≤ 1 份。"""
    pairs = _pairs(a, x)
    fixed = sum(p.runaway and not q.runaway for p, q in pairs)
    broke = sum(not p.runaway and q.runaway for p, q in pairs)
    return fixed >= 3 and broke <= 1


def layer2_adds(b: Sequence[DocResult], c: Sequence[DocResult]) -> bool:
    """判据 1':C 比 B 多出来的用处 —— 失控净少 ≥ 2 份,或「交付后调用次数」/「每步思考 token
    中位数」C 严格少于 B 的文档 ≥ 10 份(相等、或任一边没有交付件,算 C 没赢)。"""
    pairs = _pairs(b, c)
    fixed = sum(p.runaway and not q.runaway for p, q in pairs)
    broke = sum(not p.runaway and q.runaway for p, q in pairs)
    calls = sum(
        p.calls_after_deliverable is not None
        and q.calls_after_deliverable is not None
        and q.calls_after_deliverable < p.calls_after_deliverable
        for p, q in pairs
    )
    thinking = sum(
        p.thinking_median is not None
        and q.thinking_median is not None
        and q.thinking_median < p.thinking_median
        for p, q in pairs
    )
    return fixed - broke >= 2 or calls >= 10 or thinking >= 10


def accuracy_held(a: Sequence[DocResult], x: Sequence[DocResult]) -> bool:
    """判据 2:关键字段总对数 ≥ A - 3;任何一类不比 A 少对 3 个以上。"""
    if sum(d.correct for d in x) < sum(d.correct for d in a) - 3:
        return False
    categories = {d.category for d in a}
    for cat in categories:
        a_cat = sum(d.correct for d in a if d.category == cat)
        x_cat = sum(d.correct for d in x if d.category == cat)
        if x_cat < a_cat - 3:
            return False
    return True


def flips_halved(a: Sequence[DocResult], x: Sequence[DocResult]) -> bool:
    """判据 3:「变了 ≥ 3 次」的字段数,A ≥ 2 时 ≤ A 的一半;A 为 0 或 1 时 ≤ A + 1。"""
    a_n = sum(d.flipped_fields for d in a)
    x_n = sum(d.flipped_fields for d in x)
    return x_n <= a_n / 2 if a_n >= 2 else x_n <= a_n + 1


def recopy_reduced(a: Sequence[DocResult], b: Sequence[DocResult]) -> bool:
    """判据 4(只判 B 对 A):重抄步占比 B < A 的文档数 ≥ B > A 的文档数的 2 倍。"""
    pairs = _pairs(a, b)
    lower = sum(q.recopy_share < p.recopy_share for p, q in pairs)
    higher = sum(q.recopy_share > p.recopy_share for p, q in pairs)
    return lower >= 2 * higher


def refetch_gate(a: Sequence[DocResult], c: Sequence[DocResult]) -> bool:
    """判据 5(上线闸门):C 每次运行的过期后重取中位数 ≤ 1、最多 ≤ 5;因熔断以「无进展」收尾的
    运行 C ≤ A + 1。"""
    counts = [d.refetches for d in c]
    if not counts or statistics.median(counts) > 1 or max(counts) > 5:
        return False
    return sum(d.no_progress_exit for d in c) <= sum(d.no_progress_exit for d in a) + 1


def not_slower(a: Sequence[DocResult], x: Sequence[DocResult]) -> bool | None:
    """判据 6:两边都没失控的文档上,X ÷ A 的墙钟与总 token 中位数都 ≤ 1.1。没有这样的文档 → None。"""
    pairs = [(p, q) for p, q in _pairs(a, x) if not p.runaway and not q.runaway]
    if not pairs:
        return None
    wall = statistics.median(q.wall_s / p.wall_s for p, q in pairs if p.wall_s > 0)
    tokens = statistics.median(q.tokens / p.tokens for p, q in pairs if p.tokens > 0)
    return wall <= 1.1 and tokens <= 1.1


def corrections_followed(a: Sequence[DocResult], x: Sequence[DocResult]) -> bool:
    """判据 8:第 3 轮的 6 份里交付件改成用户给的值:X ≥ A - 1,且 ≥ 5 份。"""
    a_n = sum(d.correction_applied is True for d in a)
    x_n = sum(d.correction_applied is True for d in x)
    return x_n >= a_n - 1 and x_n >= 5


def format_kept(a: Sequence[DocResult], x: Sequence[DocResult]) -> bool:
    """判据 9:A 做到模板的那几份,X 也做到;页数 / 表数 / 一级标题数与 A 相差 ≤ 1 的文档 ≥ 10 份。"""
    pairs = _pairs(a, x)
    if any(p.template_ok is True and q.template_ok is not True for p, q in pairs):
        return False
    close = sum(
        p.structure is not None and q.structure is not None and abs(p.structure - q.structure) <= 1
        for p, q in pairs
    )
    return close >= 10


@dataclass(frozen=True)
class ModelVerdict:
    reproduced: bool
    layer1: bool
    layer2: bool
    failed: dict[str, list[str]] = field(default_factory=dict)


def _check(
    ids: Sequence[str],
    a: Sequence[DocResult],
    x: Sequence[DocResult],
    *,
    b: Sequence[DocResult] | None,
    regression_ok: bool | None,
) -> list[str]:
    """``ids`` 里不过的判据。"""
    tests: dict[str, Any] = {
        "1": lambda: fewer_runaways(a, x),
        "1'": lambda: b is not None and layer2_adds(b, x),
        "1b": lambda: sum(d.runaway for d in x) <= sum(d.runaway for d in a),
        "2": lambda: accuracy_held(a, x),
        "3": lambda: flips_halved(a, x),
        "4": lambda: recopy_reduced(a, x),
        "5": lambda: refetch_gate(a, x),
        "6": lambda: not_slower(a, x) is True,
        "7": lambda: regression_ok is not False,
        "8": lambda: corrections_followed(a, x),
        "9": lambda: format_kept(a, x),
    }
    return [i for i in ids if not tests[i]()]


def model_verdict(
    arms: Mapping[str, Sequence[DocResult]], *, regression_ok: bool | None
) -> ModelVerdict:
    """§5.4「每个模型的结论」那张表。``regression_ok`` 是判据 7(只 glm 跑回归集;其余为 ``None``)。

    判据 6 没有可比的文档(两边总有一边失控)时按不过算,带着数字回来找用户。
    """
    a, c = arms["A"], arms["C"]
    b = arms.get("B")
    rep = reproduced(a)
    failed: dict[str, list[str]] = {}
    if b is None:  # D9 的豆包:只有 A、C,两层一起判
        ids = ["1", "2", "3", "5", "6", "7", "8", "9"] if rep else ["2", "5", "6", "8", "9"]
        failed["C"] = _check(ids, a, c, b=None, regression_ok=regression_ok)
        ok = not failed["C"]
        return ModelVerdict(reproduced=rep, layer1=ok, layer2=ok, failed=failed)
    b_ids = ["1b", "2", "3", "4", "6", "8", "9"] if rep else ["2", "6", "8", "9"]
    c_ids = ["1", "1'", "2", "3", "5", "6", "7", "8", "9"] if rep else ["2", "5", "6", "8", "9"]
    failed["B"] = _check(b_ids, a, b, b=None, regression_ok=regression_ok)
    failed["C"] = _check(c_ids, a, c, b=b, regression_ok=regression_ok)
    layer1 = not failed["B"]
    return ModelVerdict(
        reproduced=rep, layer1=layer1, layer2=layer1 and not failed["C"], failed=failed
    )


def verdict_model(model: str) -> str:
    return INHERITS.get(model, model)


def landing(verdicts: Mapping[str, ModelVerdict]) -> dict[str, dict[str, Any]]:
    """§5.4 末尾:每一层分别判 —— 所有测过的模型都过才改平台默认;否则默认保持关,给出「过」的型号。"""
    out: dict[str, dict[str, Any]] = {}
    for layer in ("layer1", "layer2"):
        passed = sorted(m for m, v in verdicts.items() if getattr(v, layer))
        out[layer] = {
            "platform_default_on": bool(verdicts) and len(passed) == len(verdicts),
            "models_passed": passed,
        }
    return out
