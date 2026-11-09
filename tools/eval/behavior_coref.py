#!/usr/bin/env python
"""B-206 多轮指代评测:``coref`` 判据的归类与汇总(跑在 B-140 行为评测上,用例 ``r*``)。

为什么放在 B-140 而不是 ``kb_agent_eval.py``:多轮指代要测的是**平台**怎么带着整段对话把
「它 / 那价格呢 / 第二个」变成外发的检索参数(没有单独的改写层,主模型看着全部历史写
``knowledge_search.query``),还有上一轮回复里点名的对象、某一轮才附上的文件 —— B-140 本来
就在测试环境真跑整段多轮会话、支持逐轮附件、判据看工具参数;``kb_agent_eval.py`` 是自己的
单轮答题循环,测不到平台这一段。

归类只看结果,不看措辞:那一轮回复里出现了**该指的对象**的事实(``target``)还是**另一个同类
候选**的事实(``others``),以及有没有反问。外加两个平台改进要看的信号:回复有没有点名
(``names``)、外发的检索参数里有没有写名字 / 还留着「它、这个、第二个」这类指示词。

汇总(三个数一起看)::

    uv run --no-sync python tools/eval/behavior_coref.py eval-out/b140/<label>.jsonl
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from behavior_schema import CaseResult, CheckVerdict, Coref, RunRecord, read_results  # noqa: E402

#: 反问 = 一个以问号结尾的句子,问「哪一个 / 是 A 还是 B / 指的是」,且点了至少一个候选的名字。
#: 只看问句本身:「您还想了解哪些信息?」「需要我帮您确认其他信息吗?」这类客套收尾不算(评审 11-09)。
_SENTENCE_END = re.compile("(?<=[\u3002\uff01!\uff1f?\n])")
_QUESTION = re.compile("[?\uff1f]")
_WHICH = re.compile("哪(?:一|个|位|款|家|份)|还是|指的是|是指|是问|问的是")
#: 不点名候选也算反问的问句:「指的是 / 是指 / 问的是」+ 指向某一类对象的「哪位 / 哪款 / 哪个 …」
#: (11-09 复审);「您还想了解哪些」「需要我帮您确认吗」这类客套问句都不含这两样。
_ASKS_WHICH_ONE = re.compile(
    "(?:指的是|是指|问的是).*哪一?(?:位|款|家|份|个|项|天|条|次|型号|客户|同事|分部)"
)
#: 外发参数里不该留下的指示词 / 序数指代(离开对话后对方看不懂)。
_DEMONSTRATIVE = re.compile(
    "(?<!其)[他它]|她|这个|那个|这款|那款|这份|那份|这家|那家|这位|那位|这两|那两|上面|刚才|"
    "那天|这天|另一个|第[一二三四五六七八九十0-9]+[个条项款份]"
)
#: 比对事实前去掉:空白、半角 / 全角逗号、连字符与全角 / 长短破折号。
_NOISE = re.compile("[\\s,\uff0c\\-\uff0d\u2013\u2014]")

#: 指代明确时只有「right」算过:把同类候选的事实一起列出来(mixed)= 没把指的那个挑出来,
#: 用户还得自己在答复里找;它单独计数,不算「指错对象」。
UNAMBIGUOUS_PASS = frozenset({"right"})
AMBIGUOUS_PASS = frozenset({"answered_both", "clarified", "hedged_pick"})
WRONG = frozenset({"confident_wrong", "wrong_hedged"})


def _norm(text: str) -> str:
    return _NOISE.sub("", text)


@dataclass(frozen=True)
class CorefOutcome:
    outcome: str
    passed: bool
    named: bool
    query_named: bool | None  # None = 这一轮没有被接受的外发调用(或判据没指定工具)
    query_demonstrative: bool | None


def _asks(text: str, candidates: Sequence[str]) -> bool:
    keys = [_norm(c) for c in candidates if _norm(c)]
    for sentence in _SENTENCE_END.split(text):
        if not (_QUESTION.search(sentence) and _WHICH.search(sentence)):
            continue
        if any(key in _norm(sentence) for key in keys) or _ASKS_WHICH_ONE.search(sentence):
            return True
    return False


def _outcome(check: Coref, text: str) -> str:
    norm = _norm(text)
    hits = [_norm(t) in norm for t in check.target]
    target = all(hits) if check.referent == "plural" else any(hits)
    other = any(_norm(o) in norm for o in check.others)
    asks = _asks(text, check.candidates or check.names)
    if check.ambiguous:
        if any(hits) and other:
            return "answered_both"
        if asks:
            return "hedged_pick" if any(hits) or other else "clarified"
        return "confident_pick" if any(hits) or other else "missed"
    if target and other:
        return "mixed"
    if target:
        return "right"
    if other:
        return "wrong_hedged" if asks else "confident_wrong"
    return "over_clarified" if asks else "missed"


def classify(check: Coref, record: RunRecord) -> CorefOutcome:
    turn = record.turns[check.turn - 1] if check.turn <= len(record.turns) else None
    text = turn.final_text if turn is not None else ""
    outcome = _outcome(check, text)
    passed = outcome in (AMBIGUOUS_PASS if check.ambiguous else UNAMBIGUOUS_PASS)
    calls = [c for c in (turn.tool_calls if turn else []) if c.name == check.tool and not c.error]
    args = " ".join(str(v) for c in calls for v in c.args.values())
    query_named = (
        any(_norm(n) in _norm(args) for n in check.names) if calls and check.names else None
    )
    return CorefOutcome(
        outcome=outcome,
        passed=passed,
        named=any(_norm(n) in _norm(text) for n in check.names),
        query_named=query_named,
        query_demonstrative=bool(_DEMONSTRATIVE.search(args)) if calls else None,
    )


def _flag(value: bool | None) -> str:
    return "-" if value is None else str(int(value))


def verdict(check: Coref, record: RunRecord) -> CheckVerdict:
    """判据结果;``detail`` 过与不过都写(汇总靠它)。"""
    got = classify(check, record)
    detail = (
        f"referent={check.referent} ambiguous={int(check.ambiguous)} outcome={got.outcome} "
        f"named={int(got.named)} query_named={_flag(got.query_named)} "
        f"query_demonstrative={_flag(got.query_demonstrative)}"
    )
    return CheckVerdict(type=check.type, passed=got.passed, detail=detail)


def parse_detail(detail: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in detail.split() if "=" in part)


def _pct(hit: int, n: int) -> str:
    return f"{hit / n:.1%}({hit}/{n})" if n else f"—({hit}/{n})"


def summarize(results: Sequence[CaseResult]) -> str:
    """三个数一起报:指错对象率(含很笃定地答错的次数;把候选全列出来的「混答」另计)、
    有歧义时追问率、外发参数点名率。"""
    rows = [
        (parse_detail(v.detail), v.passed) for r in results for v in r.verdicts if v.type == "coref"
    ]
    skipped = sum(1 for r in results if not r.verdicts)
    clear = [d for d, _ok in rows if d.get("ambiguous") == "0"]
    unclear = [d for d, _ok in rows if d.get("ambiguous") == "1"]
    out_clear = Counter(d["outcome"] for d in clear)
    out_unclear = Counter(d["outcome"] for d in unclear)
    wrong = sum(out_clear[o] for o in WRONG)
    asked = out_unclear["clarified"] + out_unclear["hedged_pick"]
    queried = [d for d in clear if d.get("query_named") in ("0", "1")]
    lines = [
        "## 多轮指代(三个数一起看)",
        f"  指代对象明确 n={len(clear)}:指错对象 {_pct(wrong, len(clear))},"
        f"其中很笃定地答错对象 {out_clear['confident_wrong']};"
        f"混答 {_pct(out_clear['mixed'], len(clear))}",
        f"  有两个以上候选 n={len(unclear)}:追问 {_pct(asked, len(unclear))},"
        f"追问或两个都答 {_pct(asked + out_unclear['answered_both'], len(unclear))},"
        f"只猜一个 {out_unclear['confident_pick']}",
        f"  外发检索参数:点名 {_pct(sum(d['query_named'] == '1' for d in queried), len(queried))},"
        f"残留指示词 {sum(d.get('query_demonstrative') == '1' for d, _ok in rows)}",
        f"  回复点名该指的对象 {_pct(sum(d.get('named') == '1' for d in clear), len(clear))}",
        "## 按指代对象类别",
    ]
    by_type: dict[str, list[bool]] = {}
    for d, ok in rows:
        by_type.setdefault(d.get("referent", "?"), []).append(ok)
    for referent, oks in by_type.items():
        lines.append(f"  {referent} n={len(oks)} 通过 {sum(oks)}")
    outcomes = Counter(d["outcome"] for d, _ok in rows)
    lines.append("归类 " + ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items())))
    if skipped:
        lines.append(f"(另有 {skipped} 次没跑成,不计)")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    paths = [Path(p) for p in (sys.argv[1:] if argv is None else argv)]
    if not paths:
        raise SystemExit("usage: behavior_coref.py <result.jsonl> [...]")
    results = [r for p in paths for r in read_results(p)[1]]
    print(summarize(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
