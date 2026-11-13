"""B-300 评测的计划与报价(设计 §8.6 花费、§8.6 末「守住 ¥400」的删减顺序、§8.7 C 组规模)。

纯计算,不发任何调用。报价按「调用种类 x 次数 x 每次 token 假设 x 单价」算,给每组、每家的
调用次数与 ¥ 区间(低 / 高),再按 §8.6 的顺序删减到 ¥400 以内:

① B 组备选位置(提醒节挪进系统提示词)不自动跑;② B1 / B2 在旧提交上的筛选从四家减到一家;
③ C1、C3 两类对照只跑一家(报告里要写明是哪一家、其余三家没测);④ 三步都做完仍会超,就**停下问
用户**,不自行再删。删减顺序是设计「本文安排」,用户可以改。

**单价出处**(元 / 百万 token:输入, 输出, 缓存读):

* ``qwen3.8-max-0902``、``doubao-seed-2-1-pro-260915``:价目表迁移
  ``packages/expert-work-persistence/migrations/versions/0187_rate_card_dated_model_ids.py`` 的
  ``_PRICES``(直接解析那份迁移,与 ``b301_plan.rate_card_rows`` 同一个函数)。
* ``glm-5.3`` 8 / 28 / 2、``deepseek-v4-pro`` 9 / 27 / 0.3:``tools/eval/kb_agent_cost.py:33-36``
  的 ``PRICES``(B-206 用的价目表价)。
* ``glm-5.3-flash`` 0.8 / 2.8 / 0.23:``tools/eval/b301_plan.py`` 的 ``DOCUMENTED_PRICES``,
  即设计 §12 ⑥ 引的 ``docs/runbooks/2026-09-24-prod-release-checklist.md:748``。
* 后三项不在迁移里,只有带 ``--accept-documented-prices`` 才认(同 B-301 的规矩)。

**每次调用的 token 是假设,未实测**(:data:`CALL_TOKENS`);缓存读不打折、DeepSeek 闲时半价不算,
所以偏贵。整理调用用 §12 ⑥ 实测的平均数。开跑前按实际单价重报(已拍板 ⑦)。
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import b300_scenarios as sc  # noqa: E402
from b301_plan import Price, rate_card_rows  # noqa: E402

#: 用户拍板的评测总上限(11-11 ⑦ / 11-13「守住 ¥400」),跨全部报价累计。
CAP_YUAN = 400.0
REPEATS = 3
#: 「补跑按两成算」(§8.6)。
RERUN_FACTOR = 1.2
#: B-206 尺子:判官 deepseek-v4-pro、三票取多数(``kb_agent_eval.py:156``、``kb_agent_judge.py``)。
JUDGE_MODEL = "deepseek-v4-pro"
JUDGE_VOTES = 3
#: A 组(提炼)的判官每条只判一票:设计 §8.6 没给 A 组算判官钱,这里本脚本安排一票,报告里写明。
A_JUDGE_VOTES = 1
#: 整理只在部署设置的那一个型号上跑(§8.1、§12 ②)。
CONSOLIDATION_MODEL = "glm-5.3-flash"
#: 判官校准(§8.3:20 条,一致 ≥ 18 才开跑)。
CALIBRATION_ITEMS, CALIBRATION_MIN_AGREE = 20, 18
#: §8.5 对比:每段对话改前 / 改后各写回一次。
COMPARE_COMMITS = 2

DOCUMENTED_PRICES: dict[str, tuple[tuple[float, float, float], str]] = {
    "glm-5.3": ((8.0, 28.0, 2.0), "tools/eval/kb_agent_cost.py:34 PRICES"),
    "deepseek-v4-pro": ((9.0, 27.0, 0.3), "tools/eval/kb_agent_cost.py:35 PRICES"),
    "glm-5.3-flash": (
        (0.8, 2.8, 0.23),
        "tools/eval/b301_plan.py DOCUMENTED_PRICES (design §12 ⑥: 2026-09-24 prod checklist:748)",
    ),
}

#: 每种调用每次的 (输入, 输出) token,(低, 高)。**本脚本的假设,未实测**,整理那一行除外。
CALL_TOKENS: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    # 一次提炼:指令 ~500 + 对话;输出含思考段(glm-5.3-flash 始终带思考,§12 ②)。
    "extract": ((1_500, 300), (4_000, 1_200)),
    "reconcile": ((800, 100), (2_000, 600)),
    # 主模型一步:系统提示词 + 工具定义 + 记忆块 + 附件文字(§8.6「并不短」)。
    "main": ((6_000, 300), (15_000, 1_500)),
    "review": ((800, 50), (2_000, 600)),  # 读时复核
    "judge": ((1_500, 200), (3_000, 800)),
    # §12 ⑥ 实测:测试 136,488 / 217 ≈ 629 入、573 出;生产 76,669 / 94 ≈ 816 入、1,128 出。
    "consolidate": ((629, 573), (816, 1_128)),
}
#: 每段对话的调用次数(低, 高)(§8.6:每次约 1.5~2 次主模型 + 1 次读时复核)。
MAIN_PER_CONV = (1.5, 2.0)
REVIEW_PER_CONV = (1.0, 1.0)
#: 每次写回:2 次提炼 + 0~1 次去重合并(§8.6)。改前的代码只有 1 次提炼(§8.5 对比用)。
EXTRACT_PER_WRITEBACK, OLD_EXTRACT_PER_WRITEBACK = 2, 1
RECONCILE_PER_WRITEBACK = (0.0, 1.0)
#: C5 每次整理 1~3 次整理调用(§8.6)。
CONSOLIDATE_PER_SWEEP = (1.0, 3.0)
#: C4 前两段造数对话第一段没有记忆可召回,复核 0~1 次。
CUTS = {
    1: "B alternate position (reminder in the system prompt) is not run automatically",
    2: "B1 / B2 screening on the old commit: four vendors -> one",
    3: "C1 and C3 (controls) run on one vendor only; the report must say which",
}


class PlanError(RuntimeError):
    """报价 / 计划的前提不成立 —— 停下,不猜。"""


@dataclass(frozen=True)
class Vendor:
    """一家:主模型(B / C 组对话)与默认记忆模型(A 组提炼、读时复核、写回)。

    记忆模型按 ``orchestrator.agent_factory.memory_model``:主模型在 ``_CHEAP_SIBLING`` 里就换成
    同一家的便宜型号(glm-5.3 → glm-5.3-flash),否则就是主模型本身。"""

    provider: str
    main: str
    memory: str


#: 四家(§8.1:智谱 / DeepSeek / 通义 / 豆包,与知识库各设计的口径一致)。型号是本脚本的默认,
#: 开跑前用 ``--vendor provider=main[:memory]`` 按当时平台目录确认。
DEFAULT_VENDORS = (
    Vendor("glm", "glm-5.3", "glm-5.3-flash"),
    Vendor("deepseek", "deepseek-v4-pro", "deepseek-v4-pro"),
    Vendor("qwen", "qwen3.8-max-0902", "qwen3.8-max-0902"),
    Vendor("doubao", "doubao-seed-2-1-pro-260915", "doubao-seed-2-1-pro-260915"),
)


def parse_vendor(raw: str) -> Vendor:
    """``glm=glm-5.3:glm-5.3-flash`` 或 ``qwen=qwen3.8-max-0902``(记忆模型 = 主模型)。"""
    provider, sep, rest = raw.partition("=")
    if not sep or not provider or not rest:
        raise PlanError(f"--vendor wants provider=main[:memory], got {raw!r}")
    main, _, memory = rest.partition(":")
    return Vendor(provider.strip(), main.strip(), (memory or main).strip())


def price_of(model: str, *, accept_documented: bool, card: Mapping[str, Price]) -> Price:
    if model in card:
        return card[model]
    if model in DOCUMENTED_PRICES:
        (inp, out, read), source = DOCUMENTED_PRICES[model]
        if not accept_documented:
            raise PlanError(
                f"{model} has no rate card row in a migration; its price comes from {source}. "
                "Re-run with --accept-documented-prices only if that price was confirmed."
            )
        return Price(inp, out, read, f"documented, not in a migration: {source}")
    raise PlanError(f"{model}: no validated price")


def prices_for(
    vendors: Sequence[Vendor], *, accept_documented: bool, card: Mapping[str, Price] | None = None
) -> dict[str, Price]:
    rows = rate_card_rows() if card is None else card
    models = {JUDGE_MODEL, CONSOLIDATION_MODEL}
    for v in vendors:
        models |= {v.main, v.memory}
    return {m: price_of(m, accept_documented=accept_documented, card=rows) for m in sorted(models)}


# --------------------------------------------------------------------------- 调用次数


@dataclass(frozen=True)
class Line:
    """一组里、一个型号上、一种调用的次数(低, 高)。"""

    group: str
    vendor: str
    model: str
    kind: str
    low: float
    high: float


def _conversations(
    group: str, v: Vendor, n: float, *, votes: int = JUDGE_VOTES, judged: bool = True
) -> list[Line]:
    """``n`` 段被判分的对话(已含补跑)。"""
    lines = [
        Line(group, v.provider, v.main, "main", n * MAIN_PER_CONV[0], n * MAIN_PER_CONV[1]),
        Line(group, v.provider, v.memory, "review", n * REVIEW_PER_CONV[0], n * REVIEW_PER_CONV[1]),
    ]
    if judged:
        lines.append(Line(group, v.provider, JUDGE_MODEL, "judge", n * votes, n * votes))
    return lines


def _writebacks(
    group: str, v: Vendor, n: float, *, extract: int = EXTRACT_PER_WRITEBACK
) -> list[Line]:
    return [
        Line(group, v.provider, v.memory, "extract", n * extract, n * extract),
        Line(
            group,
            v.provider,
            v.memory,
            "reconcile",
            n * RECONCILE_PER_WRITEBACK[0],
            n * RECONCILE_PER_WRITEBACK[1],
        ),
    ]


def _a_writebacks_per_vendor() -> int:
    per_class = sc.SCENARIOS_PER_CLASS["A"] * REPEATS
    return sum(per_class * (sc.A12_ROUNDS if cls == "A12" else 1) for cls in sc.A_CLASSES)


#: A 组要判官判的类:P2~P5 第二次是否写成不带出处的陈述;P6 / P7 / P9 是否写明出处;P8 只在程序
#: 粗查到第一次产出里有该内容时才判(低档按 0 算)。
A_JUDGED = ("P2", "P3", "P4", "P5", "P6", "P7", "P9")
A_PREFILTERED = ("P8",)


def lines_for(
    vendors: Sequence[Vendor],
    *,
    cuts: Iterable[int] = (),
    single_vendor: str | None = None,
    include_compare: bool = True,
) -> list[Line]:
    """全部调用。``cuts`` 是 §8.6 的删减编号;②③ 只跑一家时用 ``single_vendor``(没定就按最贵的
    那家算,偏保守)。"""
    cut = set(cuts)
    one = _single(vendors, single_vendor)
    a_items = len(A_JUDGED) * sc.SCENARIOS_PER_CLASS["A"] * REPEATS
    p8_items = len(A_PREFILTERED) * sc.SCENARIOS_PER_CLASS["A"] * REPEATS
    b_convs = len(sc.B_CLASSES) * sc.SCENARIOS_PER_CLASS["B"] * REPEATS * RERUN_FACTOR
    screen_convs = 2 * sc.SCENARIOS_PER_CLASS["B"] * REPEATS
    c_n = sc.SCENARIOS_PER_CLASS["C"] * REPEATS
    lines: list[Line] = []
    for v in vendors:
        # A 组:提炼质量(§8.2)。
        lines += _writebacks("A", v, _a_writebacks_per_vendor())
        lines.append(
            Line(
                "A",
                v.provider,
                JUDGE_MODEL,
                "judge",
                a_items * A_JUDGE_VOTES,
                a_items * A_JUDGE_VOTES,
            )
        )
        lines.append(Line("A", v.provider, JUDGE_MODEL, "judge_p8", 0, p8_items * A_JUDGE_VOTES))
        # B 组(§8.3)。
        lines += _conversations("B", v, b_convs)
        if 1 not in cut:
            lines += _conversations("B-alt", v, b_convs)
        if 2 not in cut or v.provider == one.provider:
            lines += _conversations("B-screen", v, screen_convs * RERUN_FACTOR)
        # C 组(§8.7)。C1 / C3 是对照;C2 也在这一组。
        for cls in ("C1", "C2", "C3"):
            if cls in {"C1", "C3"} and 3 in cut and v.provider != one.provider:
                continue
            lines += _conversations(f"C-{cls}", v, c_n * RERUN_FACTOR)
        # C4:两段造数对话(带写回)+ 一段提问(§8.6:每条约 15 次调用)。
        chains = c_n * RERUN_FACTOR
        lines += [
            Line("C-C4", v.provider, v.main, "main", chains * 2 * 1.0, chains * 2 * 2.0),
            Line("C-C4", v.provider, v.memory, "review", chains * 1.0, chains * 2.0),
        ]
        lines += _writebacks("C-C4", v, chains * 2)
        lines += _conversations("C-C4", v, chains)
        # C5 ②:提问(整理只在 glm-5.3-flash 上,见下)。
        lines += _conversations("C-C5", v, c_n * RERUN_FACTOR)
        if include_compare:
            n = 2 * len(sc.FACTS)  # 20 段跨领域带工具的对话(§8.5:15~20 段)
            lines += _writebacks("D-compare", v, n, extract=EXTRACT_PER_WRITEBACK)
            lines += _writebacks("D-compare", v, n, extract=OLD_EXTRACT_PER_WRITEBACK)
    # C5 ①:整理只在一个型号上(15 次)+ 判官判汇总行。
    sweeps = c_n * RERUN_FACTOR
    lines += [
        Line(
            "C-C5",
            "consolidation",
            CONSOLIDATION_MODEL,
            "consolidate",
            sweeps * CONSOLIDATE_PER_SWEEP[0],
            sweeps * CONSOLIDATE_PER_SWEEP[1],
        ),
        Line(
            "C-C5",
            "consolidation",
            JUDGE_MODEL,
            "judge",
            sweeps * JUDGE_VOTES,
            sweeps * JUDGE_VOTES,
        ),
    ]
    # 在旧提交上证明会红:只在一家上做(§8.6)—— C2、C4、C5(C1 / C3 是对照,不要求先红)。
    lines += _conversations("C-baseline", one, c_n)  # C2
    lines += [
        Line("C-baseline", one.provider, one.main, "main", c_n * 2 * 1.0, c_n * 2 * 2.0),
        Line("C-baseline", one.provider, one.memory, "review", c_n * 1.0, c_n * 2.0),
    ]
    lines += _writebacks("C-baseline", one, c_n * 2)
    lines += _conversations("C-baseline", one, c_n)  # C4 提问
    lines += [
        Line(
            "C-baseline",
            "consolidation",
            CONSOLIDATION_MODEL,
            "consolidate",
            c_n * CONSOLIDATE_PER_SWEEP[0],
            c_n * CONSOLIDATE_PER_SWEEP[1],
        ),
        Line(
            "C-baseline",
            "consolidation",
            JUDGE_MODEL,
            "judge",
            c_n * JUDGE_VOTES,
            c_n * JUDGE_VOTES,
        ),
    ]
    lines += _conversations("C-baseline", one, c_n)  # C5 提问
    lines.append(
        Line(
            "calibration",
            "judge",
            JUDGE_MODEL,
            "judge",
            CALIBRATION_ITEMS * JUDGE_VOTES,
            CALIBRATION_ITEMS * JUDGE_VOTES,
        )
    )
    return lines


def _single(vendors: Sequence[Vendor], single_vendor: str | None) -> Vendor:
    if single_vendor is None:
        return vendors[0]
    for v in vendors:
        if v.provider == single_vendor:
            return v
    raise PlanError(f"--single-vendor {single_vendor!r} is not one of the vendors")


def most_expensive(vendors: Sequence[Vendor], prices: Mapping[str, Price]) -> str:
    """没定「只跑一家」是哪一家时,按主模型输出价最高的那家报(偏保守)。"""
    return max(vendors, key=lambda v: (prices[v.main].output, prices[v.main].input)).provider


TokenTable = Mapping[str, tuple[tuple[int, int], tuple[int, int]]]


def load_token_overrides(
    raw: Mapping[str, Any],
) -> dict[str, tuple[tuple[int, int], tuple[int, int]]]:
    """``--tokens FILE``:试点实测后换掉 :data:`CALL_TOKENS` 的若干行。格式
    ``{"main": {"low": [in, out], "high": [in, out]}, ...}``。"""
    table = dict(CALL_TOKENS)
    for kind, row in raw.items():
        if kind not in CALL_TOKENS:
            raise PlanError(f"unknown call kind {kind!r} in token overrides")
        low, high = row["low"], row["high"]
        pairs = [int(low[0]), int(low[1]), int(high[0]), int(high[1])]
        if any(n < 0 for n in pairs) or pairs[0] > pairs[2] or pairs[1] > pairs[3]:
            raise PlanError(f"bad token override for {kind!r}: {row}")
        table[kind] = ((pairs[0], pairs[1]), (pairs[2], pairs[3]))
    return table


def line_cost(line: Line, price: Price, tokens: TokenTable = CALL_TOKENS) -> tuple[float, float]:
    (lin, lout), (hin, hout) = tokens["judge" if line.kind == "judge_p8" else line.kind]
    low = line.low * (lin * price.input + lout * price.output) / 1e6
    high = line.high * (hin * price.input + hout * price.output) / 1e6
    return low, high


def summarize(
    lines: Sequence[Line], prices: Mapping[str, Price], tokens: TokenTable = CALL_TOKENS
) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    total = [0.0, 0.0, 0.0, 0.0]
    for line in lines:
        low, high = line_cost(line, prices[line.model], tokens)
        g = groups.setdefault(
            line.group,
            {
                "calls_low": 0.0,
                "calls_high": 0.0,
                "yuan_low": 0.0,
                "yuan_high": 0.0,
                "by_vendor": {},
            },
        )
        v = g["by_vendor"].setdefault(
            line.vendor,
            {
                "calls_low": 0.0,
                "calls_high": 0.0,
                "yuan_low": 0.0,
                "yuan_high": 0.0,
                "calls_by_kind": {},
            },
        )
        for bucket in (g, v):
            bucket["calls_low"] += line.low
            bucket["calls_high"] += line.high
            bucket["yuan_low"] += low
            bucket["yuan_high"] += high
        kinds = v["calls_by_kind"].setdefault(f"{line.kind}@{line.model}", [0.0, 0.0])
        kinds[0] += line.low
        kinds[1] += line.high
        total[0] += line.low
        total[1] += line.high
        total[2] += low
        total[3] += high
    return {
        "groups": _rounded(groups),
        "total": {
            "calls_low": round(total[0]),
            "calls_high": round(total[1]),
            "yuan_low": round(total[2], 2),
            "yuan_high": round(total[3], 2),
        },
    }


def _rounded(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_rounded(v) for v in value]
    if isinstance(value, float):
        return round(value, 2)
    return value


def choose_cuts(
    vendors: Sequence[Vendor],
    prices: Mapping[str, Price],
    *,
    already_spent: float,
    single_vendor: str | None,
    include_compare: bool = True,
    cap: float | None = None,
    tokens: TokenTable = CALL_TOKENS,
) -> tuple[list[int], dict[str, Any], str]:
    """按 §8.6 的顺序删,直到「已花 + 本次高档」≤ 上限;三步都删完仍超 → ``stop_and_ask``。"""
    cap = CAP_YUAN if cap is None else cap
    cuts: list[int] = []
    one = single_vendor or most_expensive(vendors, prices)
    while True:
        summary = summarize(
            lines_for(vendors, cuts=cuts, single_vendor=one, include_compare=include_compare),
            prices,
            tokens,
        )
        if already_spent + summary["total"]["yuan_high"] <= cap:
            return cuts, summary, "within_cap"
        if len(cuts) == len(CUTS):
            return cuts, summary, "stop_and_ask"
        cuts.append(len(cuts) + 1)


def quote(
    vendors: Sequence[Vendor],
    prices: Mapping[str, Price],
    *,
    already_spent: float,
    single_vendor: str | None,
    stop_line: float,
    scenario_digest: str,
    include_compare: bool = True,
    cap: float | None = None,
    tokens: TokenTable = CALL_TOKENS,
) -> dict[str, Any]:
    """报价:不删时的全量、按顺序删后的计划、状态、止损线、报价编号(内容哈希)。"""
    cap = CAP_YUAN if cap is None else cap
    one = single_vendor or most_expensive(vendors, prices)
    full = summarize(
        lines_for(vendors, single_vendor=one, include_compare=include_compare), prices, tokens
    )
    cuts, planned, status = choose_cuts(
        vendors,
        prices,
        already_spent=already_spent,
        single_vendor=one,
        include_compare=include_compare,
        cap=cap,
        tokens=tokens,
    )
    if stop_line <= 0:
        raise PlanError("stop line must be positive")
    if already_spent + stop_line > cap:
        status = "stop_and_ask" if status == "within_cap" else status
    body: dict[str, Any] = {
        "cap_yuan": cap,
        "already_spent_yuan": round(already_spent, 2),
        "stop_line_yuan": float(stop_line),
        "status": status,
        "single_vendor": one,
        "single_vendor_chosen": single_vendor is not None,
        "vendors": [asdict(v) for v in vendors],
        "prices": {
            m: {
                "input": p.input,
                "output": p.output,
                "cache_read": p.cache_read,
                "source": p.source,
            }
            for m, p in prices.items()
        },
        "token_assumptions": {
            k: {"low": v[0], "high": v[1], "source": _token_source(k, tokens)}
            for k, v in tokens.items()
        },
        "judge": {"model": JUDGE_MODEL, "votes": JUDGE_VOTES, "a_group_votes": A_JUDGE_VOTES},
        "scenario_digest": scenario_digest,
        "without_cuts": full,
        "cuts_applied": [{"step": c, "what": CUTS[c]} for c in cuts],
        "planned": planned,
    }
    if status == "stop_and_ask":
        body["note"] = (
            "after cuts 1-3 the high estimate still crosses the cap, or the stop line would: "
            "stop and ask the user (design §8.6 step 4); do not cut further on your own"
        )
    digest = json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
    return {**body, "quote_id": hashlib.sha256(digest).hexdigest()[:12]}


def _token_source(kind: str, tokens: TokenTable) -> str:
    if tokens[kind] != CALL_TOKENS[kind]:
        return "override (--tokens)"
    if kind == "consolidate":
        return "measured: design §12 ⑥ averages"
    return "assumption, not measured"


def planned_groups(q: Mapping[str, Any]) -> set[str]:
    return set(q["planned"]["groups"])
