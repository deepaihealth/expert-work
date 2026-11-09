#!/usr/bin/env python
"""知识库智能体层评测的免费报价(B-206 11-09):**不调任何模型**,只读本机的历史结果文件与
判分缓存,估新尺子跑一轮要多少调用、多少钱。

估法(假设都打印在输出里):

* 答题、重排:照搬历史结果文件里每题的实花(答题模型与产品代码不变,题的难度相近);
* 判分:历史每次判分的实花,乘新旧提示词的长度比(逐题按「模板 + 问题 + 资料 + 回答」的
  字数算),加上按结论判多出来的输出(旧回复平均字数,乘 :data:`CLAIMS_OUTPUT_FACTOR` 减 1,
  乘 :data:`TOKENS_PER_CHAR`,乘判分模型输出价),再乘每题票数;
* 嵌入:难负例轮删过句的文档整篇重新嵌入(上限;其余文档走缓存),字数取自 spec;
* 改动后一轮 = 改动前基线乘 (1 + ``--margin``):B 式作答更长、可能多一次收尾提醒,
  代码还没写,只能给余量。

用法::

    uv run --no-sync python tools/eval/kb_agent_estimate.py \\
        --dataset tools/eval/datasets/kb/huada_v1.yaml \\
        --chat-dataset tools/eval/datasets/kb/huada_v1.chat.yaml \\
        --history eval-out/kb/agent-k2-read.jsonl \\
        --hard-negatives eval-out/kb/hard-negatives.yaml
"""

from __future__ import annotations

import argparse
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
from kb_agent_judge import DEFAULT_JUDGE_VOTES, JUDGE_TEMPLATE  # type: ignore[import-not-found]
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


@dataclass(frozen=True)
class History:
    questions: int
    answer_cny_per_q: float
    answer_calls_per_q: float
    judge_cny_per_call: float
    rerank_cny_per_q: float
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
        answer_calls_per_q=sum(int(r["calls"]["answer"]) for r in runs) / n,
        judge_cny_per_call=sum(r["cost_cny"]["judge"] for r in rows) / judge_calls,
        rerank_cny_per_q=sum(float(r["spend_cny"]["rerank"]) for r in runs) / n,
        input_ratio=statistics.fmean(ratios),
        answer_model=str(rows[0].get("answer_model")),
        judge_model=str(rows[0].get("judge_model")),
    )


def average_reply_chars(cache_db: Path) -> float | None:
    """判分缓存里旧回复的平均字数;缓存不在就 None。"""
    if not cache_db.exists():
        return None
    with sqlite3.connect(cache_db) as con:
        value = con.execute("SELECT AVG(LENGTH(v)) FROM j").fetchone()[0]
    return float(value) if value is not None else None


def judge_call_cny(h: History, *, reply_chars: float, price: Price) -> float:
    extra_tokens = (CLAIMS_OUTPUT_FACTOR - 1) * reply_chars * TOKENS_PER_CHAR
    return float(h.judge_cny_per_call * h.input_ratio + extra_tokens * price.output / 1e6)


@dataclass(frozen=True)
class RunEstimate:
    name: str
    questions: int
    answer_calls: float
    judge_calls: int
    answer_cny: float
    judge_cny: float
    rerank_cny: float
    embed_cny: float

    @property
    def total(self) -> float:
        return self.answer_cny + self.judge_cny + self.rerank_cny + self.embed_cny


def estimate_run(
    name: str,
    questions: int,
    h: History,
    *,
    votes: int,
    judge_call: float,
    embed_cny: float = 0.0,
) -> RunEstimate:
    return RunEstimate(
        name=name,
        questions=questions,
        answer_calls=questions * h.answer_calls_per_q,
        judge_calls=questions * votes,
        answer_cny=questions * h.answer_cny_per_q,
        judge_cny=questions * votes * judge_call,
        rerank_cny=questions * h.rerank_cny_per_q,
        embed_cny=embed_cny,
    )


def _row(r: RunEstimate) -> str:
    return (
        f"  {r.name} | {r.questions} 题 | 答题 {r.answer_calls:.0f} 次 / 判分 {r.judge_calls} 次 | "
        f"答题 ¥{r.answer_cny:.2f} 判分 ¥{r.judge_cny:.2f} 重排 ¥{r.rerank_cny:.2f} "
        f"嵌入 ¥{r.embed_cny:.2f} | 合计 ¥{r.total:.2f}"
    )


def format_estimate(
    runs: Sequence[RunEstimate], h: History, *, judge_call: float, margin: float, notes: list[str]
) -> str:
    base = sum(r.total for r in runs)
    judge_share = sum(r.judge_cny for r in runs)
    lines = [
        "知识库智能体层评测报价(不调任何模型:只读历史结果与判分缓存)",
        f"  历史:{h.questions} 题,答题 {h.answer_model} ¥{h.answer_cny_per_q:.3f}/题、"
        f"{h.answer_calls_per_q:.2f} 次/题;判分 {h.judge_model} ¥{h.judge_cny_per_call:.3f}/次;"
        f"重排 ¥{h.rerank_cny_per_q:.3f}/题",
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


def _count_runs(args: argparse.Namespace) -> tuple[int, int, int, float, list[str]]:
    """``(标准轮题数, 其中干扰题, 难负例轮题数, 难负例删句文档字数, 说明)``。"""
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
    return len(picked) + distractors, distractors, controls + hard, chars, notes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--chat-dataset", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True, help="一份历史结果 JSONL")
    parser.add_argument("--judge-cache", type=Path, help="判分缓存(估旧回复长度)")
    parser.add_argument("--hard-negatives", type=Path, help="kb_hard_sets.py 生成的 spec")
    parser.add_argument("--distractors", type=int, default=15)
    parser.add_argument("--judge-votes", type=int, default=DEFAULT_JUDGE_VOTES)
    parser.add_argument("--judge-price", help="判分模型不在价目表时:input,cached,output")
    parser.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    args = parser.parse_args(argv)

    h = history_from(args.history)
    price = resolve_price(h.judge_model, args.judge_price)
    cache = args.judge_cache
    if cache is None:
        from kb_agent_eval import JUDGE_CACHE_DB  # type: ignore[import-not-found]

        cache = JUDGE_CACHE_DB
    reply = average_reply_chars(cache)
    notes = [] if reply is not None else ["没有判分缓存:旧回复按 150 字估"]
    judge_call = judge_call_cny(h, reply_chars=reply or 150.0, price=price)
    standard, _distractors, hard_run, chars, more = _count_runs(args)
    embed_price = float(os.environ.get(EMBED_PRICE_ENV, DEFAULT_EMBED_PRICE_CNY_PER_1K))
    embed = chars * TOKENS_PER_CHAR / 1000 * embed_price
    runs = [
        estimate_run("标准轮", standard, h, votes=args.judge_votes, judge_call=judge_call),
        estimate_run(
            "难负例轮", hard_run, h, votes=args.judge_votes, judge_call=judge_call, embed_cny=embed
        ),
    ]
    print(format_estimate(runs, h, judge_call=judge_call, margin=args.margin, notes=notes + more))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
