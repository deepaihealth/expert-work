#!/usr/bin/env python
"""知识库智能体层评测的免费报价(B-206 11-09):**不调任何模型**,只读本机的历史结果文件与
判分缓存,估新尺子跑一轮要多少调用、多少钱。

估法(假设都打印在输出里):

* 答题、重排:照搬历史结果文件里每题的实花(答题模型与产品代码不变,题的难度相近);
* 判分:历史每次判分的实花,乘新旧提示词的长度比(逐题按「模板 + 问题 + 资料 + 回答」的
  字数算),加上按结论判多出来的输出(旧回复平均字数,乘 :data:`CLAIMS_OUTPUT_FACTOR` 减 1,
 乘:data:`TOKENS_PER_CHAR`,乘判分模型输出价),再乘每题票数;
* 嵌入:难负例轮删过句的文档整篇重新嵌入(上限;其余文档走缓存),字数取自 spec;
* 改动后一轮 = 改动前基线乘 (1 + ``--margin``):B 式作答更长、可能多一次收尾提醒,
  代码还没写,只能给余量。
* 难负例答前复核(11-10,``--no-recheck`` = 不做):难负例题数乘票数乘一次复核的钱(复核提示词 +
  一次检索结果的字数上限 :data:`SEARCH_TEXT_BUDGET` + 参考答案,按判分模型价目表价算)。

**重判已有结果**(``--rejudge a.jsonl b.jsonl …``,不需要 ``--dataset`` / ``--history``):
逐行按新判分模板拼出来的提示词字数(模板 + 问题 + 资料 + 回答 + 参考信息;旧行没记 ``gold`` 时,
有答案题的含答案段落按 :data:`GOLD_PASSAGE_CHARS`乘2 估)乘 票数估输入,输出按判分缓存里按结论判的
回复平均字数乘:data:`CORE_FIELD_FACTOR` 估;报全价、同一提示词第 2 票起走提示词缓存的价、闲时半价。

用法::

    uv run --no-sync python tools/eval/kb_agent_estimate.py \\
        --dataset tools/eval/datasets/kb/huada_v1.yaml \\
        --chat-dataset tools/eval/datasets/kb/huada_v1.chat.yaml \\
        --history eval-out/kb/agent-k2-read.jsonl \\
        --hard-negatives eval-out/kb/hard-negatives.yaml

    uv run --no-sync python tools/eval/kb_agent_estimate.py --rejudge eval-out/kb/agent-x.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from kb_agent_cost import Price, resolve_price  # type: ignore[import-not-found]
from kb_agent_judge import (  # type: ignore[import-not-found]
    DEFAULT_JUDGE_VOTES,
    EXCLUDED,
    JUDGE_TEMPLATE,
    seen_passages,
)
from kb_agent_recheck import RECHECK_TEMPLATE  # type: ignore[import-not-found]
from kb_agent_tally import read_results  # type: ignore[import-not-found]
from kb_dataset import load_dataset  # type: ignore[import-not-found]

#: 中文为主的文本,每字约多少 token(估算假设,DeepSeek / GLM 的分词器量级)。
TOKENS_PER_CHAR = 0.7
#: 按结论判的回复比旧回复长几倍(列出 3~4 条结论 + 引文;估算假设)。
CLAIMS_OUTPUT_FACTOR = 3.0
#: 旧判分模板(``ca1c9f0f8`` 的 ``JUDGE_TEMPLATE``)去掉四个占位符后的字数。
OLD_TEMPLATE_CHARS = 472
NEW_TEMPLATE_CHARS = len(JUDGE_TEMPLATE.format(question="", passages="", reference="", answer=""))
DEFAULT_MARGIN = 0.2
#: 同 ``kb_eval.DEFAULT_EMBED_PRICE_CNY_PER_1K`` / ``EMBED_PRICE_ENV``(不 import kb_eval:太重)。
EMBED_PRICE_ENV = "EXPERT_WORK_EVAL_EMBED_PRICE_CNY_PER_1K"
DEFAULT_EMBED_PRICE_CNY_PER_1K = 0.0005
#: 难负例 spec 不给时按这么多道估。
DEFAULT_HARD_NEGATIVES = 30
#: 一次 ``knowledge_search`` 结果的字数上限(``orchestrator.tools.knowledge_results
#: .RESULTS_TEXT_BUDGET``;不 import:太重)。答前复核取前 20 段也只有这么多字。
SEARCH_TEXT_BUDGET = 10_000
#: 复核的参考答案(原题文字答案 + 被删原文)、问题、回复各约多少字(估算假设)。
RECHECK_REFERENCE_CHARS = 200
RECHECK_QUESTION_CHARS = 30
RECHECK_REPLY_CHARS = 80
#: 旧结果行没记 ``gold`` 时,一段「含答案的完整段落」按多少字估(10 月切块导出的平均块长约 290 字)。
GOLD_PASSAGE_CHARS = 300
#: 部分写了 / 资料没写的参考信息约多少字;派生题的补充说明另加多少字(估算假设)。
PARTIAL_REFERENCE_CHARS = 300
NO_ANSWER_REFERENCE_CHARS = 40
NOTE_CHARS = 150
#: 11-10 每条结论多一个 ``core`` 字段:按结论判的回复比 11-09 的长约一成(估算假设)。
CORE_FIELD_FACTOR = 1.1
#: 11-09 尺子(``2026-11-09.3+902042cf``,3b074f291)的判分模板去掉四个占位符后的字数。
YARDSTICK_1109_TEMPLATE_CHARS = 1003
#: 判分缓存里没有按结论判的回复时,回复按多少字估。
DEFAULT_CLAIMS_REPLY_CHARS = 800


@dataclass(frozen=True)
class History:
    questions: int
    answer_cny_per_q: float
    #: 历史文件没有 run 行(中途停了)时不知道:None。
    answer_calls_per_q: float | None
    judge_cny_per_call: float
    rerank_cny_per_q: float | None
    input_ratio: float
    answer_model: str
    judge_model: str


def history_from(path: Path) -> History:
    rows, runs = read_results(path)
    if not rows:
        raise SystemExit(f"{path}: no result rows to estimate from")
    n = len(rows)
    judge_calls = sum(int(r["calls"]["judge"]) for r in runs) or n

    def rest(row: dict[str, Any]) -> int:
        seen = [*row["searches"], *row.get("reads", ())]
        passages = sum(len(s.get("content") or "") for s in seen)
        return len(row.get("question") or "") + len(row.get("answer") or "") + passages

    ratios = [(NEW_TEMPLATE_CHARS + rest(r)) / (OLD_TEMPLATE_CHARS + rest(r)) for r in rows]
    return History(
        questions=n,
        answer_cny_per_q=sum(r["cost_cny"]["answer"] for r in rows) / n,
        answer_calls_per_q=sum(int(r["calls"]["answer"]) for r in runs) / n if runs else None,
        judge_cny_per_call=sum(r["cost_cny"]["judge"] for r in rows) / judge_calls,
        rerank_cny_per_q=sum(float(r["spend_cny"]["rerank"]) for r in runs) / n if runs else None,
        input_ratio=statistics.fmean(ratios),
        answer_model=str(rows[0].get("answer_model")),
        judge_model=str(rows[0].get("judge_model")),
    )


def average_reply_chars(cache_db: Path) -> float | None:
    """判分缓存里**旧尺子**回复(JSON 里没有 ``claims``)的平均字数;缓存不在或一条旧回复都没有
    就 None。新尺子的回复已经是结论列表,再乘倍数会重复计。"""
    if not cache_db.exists():
        return None
    with sqlite3.connect(cache_db) as con:
        values = [str(v) for (v,) in con.execute("SELECT v FROM j")]
    old = [len(v) for v in values if _old_style(v)]
    return statistics.fmean(old) if old else None


def _old_style(raw: str) -> bool:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return False
    return isinstance(value, dict) and "claims" not in value


def judge_call_cny(h: History, *, reply_chars: float, price: Price) -> float:
    extra_tokens = (CLAIMS_OUTPUT_FACTOR - 1) * reply_chars * TOKENS_PER_CHAR
    return float(h.judge_cny_per_call * h.input_ratio + extra_tokens * price.output / 1e6)


@dataclass(frozen=True)
class RunEstimate:
    name: str
    questions: int
    answer_calls: float | None
    judge_calls: int
    answer_cny: float
    judge_cny: float
    rerank_cny: float
    embed_cny: float
    #: 难负例答前复核(标准轮没有:None)。
    recheck_calls: int | None = None
    recheck_cny: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.answer_cny + self.judge_cny + self.rerank_cny + self.embed_cny + self.recheck_cny
        )


def estimate_run(
    name: str,
    questions: int,
    h: History,
    *,
    votes: int,
    judge_call: float,
    embed_cny: float = 0.0,
    recheck: tuple[int, float] | None = None,
) -> RunEstimate:
    """``recheck`` = ``(复核次数, 一次的钱)``,难负例轮才有。"""
    return RunEstimate(
        recheck_calls=None if recheck is None else recheck[0],
        recheck_cny=0.0 if recheck is None else recheck[0] * recheck[1],
        name=name,
        questions=questions,
        answer_calls=None if h.answer_calls_per_q is None else questions * h.answer_calls_per_q,
        judge_calls=questions * votes,
        answer_cny=questions * h.answer_cny_per_q,
        judge_cny=questions * votes * judge_call,
        rerank_cny=questions * (h.rerank_cny_per_q or 0.0),
        embed_cny=embed_cny,
    )


def _calls(value: float | None, *, digits: int = 0) -> str:
    return "?" if value is None else f"{value:.{digits}f}"


def _row(r: RunEstimate) -> str:
    recheck = (
        "" if r.recheck_calls is None else f"答前复核 {r.recheck_calls} 次 ¥{r.recheck_cny:.2f} "
    )
    return (
        f"  {r.name} | {r.questions} 题 | "
        f"答题 {_calls(r.answer_calls)} 次 / 判分 {r.judge_calls} 次 | "
        f"答题 ¥{r.answer_cny:.2f} 判分 ¥{r.judge_cny:.2f} 重排 ¥{r.rerank_cny:.2f} "
        f"嵌入 ¥{r.embed_cny:.2f} {recheck}| 合计 ¥{r.total:.2f}"
    )


def recheck_call_cny(price: Price) -> float:
    """一次答前复核的钱(价目表价,不算闲时半价)。"""
    template = len(RECHECK_TEMPLATE.format(question="", reference="", passages=""))
    chars = template + RECHECK_QUESTION_CHARS + RECHECK_REFERENCE_CHARS + SEARCH_TEXT_BUDGET
    tokens_in, tokens_out = chars * TOKENS_PER_CHAR, RECHECK_REPLY_CHARS * TOKENS_PER_CHAR
    return float((tokens_in * price.input + tokens_out * price.output) / 1e6)


# ---------------------------------------------------------------------------
# 重判已有结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RejudgeEstimate:
    name: str
    questions: int
    calls: int
    input_tokens: float
    output_tokens: float
    full_cny: float
    #: 同一提示词第 2 票起输入走提示词缓存价。
    cached_cny: float
    #: 按原文件每行的判分实花推:实花乘新旧提示词长度比、乘票数比(含看不见的思考 token、
    #: 原来那次的忙闲时比例;原来走了缓存的行实花为 0,会偏低)。
    history_cny: float


def _reference_chars(row: dict[str, Any]) -> int:
    if "gold" in row:
        gold = sum(len(g) for g in row["gold"])
    else:
        gold = 2 * GOLD_PASSAGE_CHARS if row.get("label") == "answerable" else 0
    by_label = {"partial": PARTIAL_REFERENCE_CHARS, "no_answer": NO_ANSWER_REFERENCE_CHARS}
    note = NOTE_CHARS if row.get("set") in ("hard_negative", "distractor") else 0
    return gold + by_label.get(str(row.get("label")), 0) + note


def rejudge_estimate(
    path: Path, *, price: Price, reply_chars: float, votes: int | None = None
) -> RejudgeEstimate:
    """一份结果文件用新尺子重判要多少调用、多少钱(不调模型);复核剔除的行不重判。"""
    rows = [r for r in read_results(path)[0] if r.get("category") != EXCLUDED]
    calls = 0
    tokens_in = tokens_out = full = cached = history = 0.0
    out_per_call = reply_chars * TOKENS_PER_CHAR
    for row in rows:
        n = votes or int(row.get("judge_votes") or DEFAULT_JUDGE_VOTES)
        run = {"searches": row.get("searches") or [], "reads": row.get("reads") or []}
        chars = (
            NEW_TEMPLATE_CHARS
            + len(row.get("question") or "")
            + len(seen_passages(run))
            + len(row.get("answer") or "")
            + _reference_chars(row)
        )
        per_in = chars * TOKENS_PER_CHAR
        rest = chars - NEW_TEMPLATE_CHARS
        ratio = chars / (YARDSTICK_1109_TEMPLATE_CHARS + rest)
        old_votes = int(row.get("judge_votes") or n)
        spent = float((row.get("cost_cny") or {}).get("judge") or 0.0)
        history += spent * ratio * n / old_votes
        calls += n
        tokens_in += n * per_in
        tokens_out += n * out_per_call
        full += n * (per_in * price.input + out_per_call * price.output) / 1e6
        cached += (
            per_in * price.input + (n - 1) * per_in * price.cached + n * out_per_call * price.output
        ) / 1e6
    return RejudgeEstimate(
        name=path.name,
        questions=len(rows),
        calls=calls,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        full_cny=full,
        cached_cny=cached,
        history_cny=history,
    )


def average_claims_reply_chars(cache_db: Path) -> float | None:
    """判分缓存里按结论判(JSON 里有 ``claims``)的回复的平均字数;没有就 None。"""
    if not cache_db.exists():
        return None
    with sqlite3.connect(cache_db) as con:
        values = [str(v) for (v,) in con.execute("SELECT v FROM j")]
    lengths = [len(v) for v in values if not _old_style(v) and '"claims"' in v]
    return statistics.fmean(lengths) if lengths else None


def format_rejudge(estimates: Sequence[RejudgeEstimate], *, model: str, reply: str) -> str:
    lines = [
        f"重判报价(不调任何模型:只读结果文件与判分缓存;判分 {model},价目表价)",
        f"  输出按 {reply};一字约 {TOKENS_PER_CHAR} token",
    ]
    for e in estimates:
        lines.append(
            f"  {e.name} | {e.questions} 题 | 判分 {e.calls} 次 | 输入约 {e.input_tokens:,.0f} "
            f"token、输出约 {e.output_tokens:,.0f} token | 全价 ¥{e.full_cny:.2f},"
            f"第 2 票起走提示词缓存 ¥{e.cached_cny:.2f},再遇闲时半价 ¥{e.cached_cny / 2:.2f} | "
            f"按原文件判分实花推 ¥{e.history_cny:.2f}"
        )
    full = sum(e.full_cny for e in estimates)
    cached = sum(e.cached_cny for e in estimates)
    history = sum(e.history_cny for e in estimates)
    lines.append(
        f"合计 {sum(e.questions for e in estimates)} 题 / {sum(e.calls for e in estimates)} 次:"
        f"全价 ¥{full:.2f},走提示词缓存 ¥{cached:.2f},闲时 ¥{cached / 2:.2f};"
        f"按原文件判分实花推 ¥{history:.2f}"
    )
    lines.append(
        "  按 token 估的不含判分模型看不见的思考 token,会偏低;按原文件实花推的已含,"
        "但也带着原来那次的忙闲时比例"
    )
    return "\n".join(lines)


def _main_rejudge(args: argparse.Namespace) -> int:
    rows = read_results(args.rejudge[0])[0]
    model = args.judge_model or str(rows[0].get("judge_model") if rows else "deepseek-v4-pro")
    price = resolve_price(model, args.judge_price)
    cache = args.judge_cache
    if cache is None:
        from kb_agent_eval import JUDGE_CACHE_DB  # type: ignore[import-not-found]

        cache = JUDGE_CACHE_DB
    measured = average_claims_reply_chars(cache)
    if measured is None:
        reply_chars = float(DEFAULT_CLAIMS_REPLY_CHARS)
        reply = f"判分缓存里没有按结论判的回复,按 {DEFAULT_CLAIMS_REPLY_CHARS} 字估"
    else:
        reply_chars = measured * CORE_FIELD_FACTOR
        reply = f"判分缓存里按结论判的回复平均 {measured:.0f} 字乘{CORE_FIELD_FACTOR}(core 字段)"
    estimates = [
        rejudge_estimate(path, price=price, reply_chars=reply_chars, votes=args.rejudge_votes)
        for path in args.rejudge
    ]
    print(format_rejudge(estimates, model=model, reply=reply))
    return 0


def format_estimate(
    runs: Sequence[RunEstimate], h: History, *, judge_call: float, margin: float, notes: list[str]
) -> str:
    base = sum(r.total for r in runs)
    judge_share = sum(r.judge_cny for r in runs)
    lines = [
        "知识库智能体层评测报价(不调任何模型:只读历史结果与判分缓存)",
        f"  历史:{h.questions} 题,答题 {h.answer_model} ¥{h.answer_cny_per_q:.3f}/题、"
        f"{_calls(h.answer_calls_per_q, digits=2)} 次/题;判分 {h.judge_model} "
        f"¥{h.judge_cny_per_call:.3f}/次;重排 ¥{h.rerank_cny_per_q or 0.0:.3f}/题",
        f"  新判分:提示词约 {h.input_ratio:.3f} 倍、多出结论列表的输出 → ¥{judge_call:.3f}/次",
        *(f"  {n}" for n in notes),
        "一轮:",
        *(_row(r) for r in runs),
        f"改动前基线(当前 main):¥{base:.2f}",
        f"改动后一轮(+{margin:.0%} 余量):¥{base * (1 + margin):.2f}",
        f"两次合计:¥{base * (2 + margin):.2f}",
        f"  DeepSeek 判分闲时半价:基线可省约 ¥{judge_share / 2:.2f}(历史单价已混了忙闲时)",
    ]
    return "\n".join(lines)


def _count_runs(args: argparse.Namespace) -> tuple[int, int, int, int, float, list[str]]:
    """``(标准轮题数, 其中干扰题, 难负例轮题数, 其中难负例, 难负例删句文档字数, 说明)``。"""
    from kb_agent_cases import plan_run, select_cases  # type: ignore[import-not-found]

    main_set, chat_set = load_dataset(args.dataset), load_dataset(args.chat_dataset)
    picked = select_cases(main_set, chat_set)
    controls = sum(1 for _label, c in picked if c.expected)
    notes: list[str] = []
    try:
        ns = SimpleNamespace(
            hard_negatives=None, distractors=args.distractors, corpus=None, dataset=args.dataset
        )
        distractors = plan_run(ns, main_set, chat_set).distractors
    except (SystemExit, OSError):
        distractors = args.distractors
        notes.append(f"读不到语料清单,干扰题按上限 {distractors} 道估")
    hard, chars = DEFAULT_HARD_NEGATIVES, 0.0
    if args.hard_negatives is not None:
        spec = yaml.safe_load(args.hard_negatives.read_text(encoding="utf-8"))
        hard = len(spec.get("cases") or [])
        chars = float((spec.get("touched") or {}).get("chars") or 0)
    else:
        notes.append(f"没给难负例 spec:按 {hard} 道、嵌入 ¥0 估")
    notes.append(f"标准轮含近名干扰题 {distractors} 道;难负例轮含有答案对照题 {controls} 道")
    return len(picked) + distractors, distractors, controls + hard, hard, chars, notes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--chat-dataset", type=Path)
    parser.add_argument("--history", type=Path, help="一份历史结果 JSONL")
    parser.add_argument(
        "--rejudge",
        type=Path,
        nargs="+",
        help="只估这些结果文件用新尺子重判(kb_agent_rejudge.py)的钱;不需要 --dataset / --history",
    )
    parser.add_argument("--rejudge-votes", type=int, help="重判每题几票(默认与原文件相同)")
    parser.add_argument("--judge-model", help="重判用的判分模型(默认与原文件相同)")
    parser.add_argument(
        "--no-recheck", action="store_true", help="难负例轮不做答前复核(同 kb_agent_eval)"
    )
    parser.add_argument("--judge-cache", type=Path, help="判分缓存(估旧回复长度)")
    parser.add_argument("--hard-negatives", type=Path, help="kb_hard_sets.py 生成的 spec")
    parser.add_argument("--distractors", type=int, default=15)
    parser.add_argument("--judge-votes", type=int, default=DEFAULT_JUDGE_VOTES)
    parser.add_argument("--judge-price", help="判分模型不在价目表时:input,cached,output")
    parser.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    args = parser.parse_args(argv)
    if args.rejudge:
        return _main_rejudge(args)
    for name in ("dataset", "chat_dataset", "history"):
        if getattr(args, name) is None:
            parser.error(f"--{name.replace('_', '-')} is required (unless --rejudge)")

    h = history_from(args.history)
    price = resolve_price(h.judge_model, args.judge_price)
    cache = args.judge_cache
    if cache is None:
        from kb_agent_eval import JUDGE_CACHE_DB  # type: ignore[import-not-found]

        cache = JUDGE_CACHE_DB
    reply = average_reply_chars(cache)
    notes = [] if reply is not None else ["判分缓存里没有旧尺子的回复:旧回复按 150 字估"]
    if h.answer_calls_per_q is None:
        notes.append("历史文件没有 run 行(中途停了):答题次数未知、重排按 ¥0 估")
    judge_call = judge_call_cny(h, reply_chars=reply or 150.0, price=price)
    standard, _distractors, hard_run, hard, chars, more = _count_runs(args)
    recheck = (0 if args.no_recheck else hard * args.judge_votes, recheck_call_cny(price))
    embed_price = float(os.environ.get(EMBED_PRICE_ENV, DEFAULT_EMBED_PRICE_CNY_PER_1K))
    embed = chars * TOKENS_PER_CHAR / 1000 * embed_price
    runs = [
        estimate_run("标准轮", standard, h, votes=args.judge_votes, judge_call=judge_call),
        estimate_run(
            "难负例轮",
            hard_run,
            h,
            votes=args.judge_votes,
            judge_call=judge_call,
            embed_cny=embed,
            recheck=recheck,
        ),
    ]
    print(format_estimate(runs, h, judge_call=judge_call, margin=args.margin, notes=notes + more))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
