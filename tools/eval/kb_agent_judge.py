"""知识库智能体层评测的判分(B-206 11-09 尺子修正,11-10 改成分级;从 ``kb_agent_eval.py`` 拆出)。

为什么这样判(详见 B-206 设计 §8.2 的 11-09 段):

* **按结论判**:判分模型不整体拍板「编造没有」,而是把回答拆成一条条结论,逐条标依据
  (``stated`` 原文写了,含复述、概括、只用原文数字的简单算术 / ``inferred_labeled`` 标明了的推断 /
  ``general_labeled`` 标明了不是来自资料的常识 / ``no_source_needed`` 通用安全提示、免责、寒暄 /
  ``inferred_unlabeled`` 没标明的推断 / ``unsupported`` 资料里没有 / ``contradicted`` 与资料矛盾 /
  ``misattributed`` 别的对象的事实安到被问的对象上或推广到同类),再标 ``core``(是不是结论本身)。
* **分级,不再一票否决**(11-10,参考 Google FACTS Grounding 对不需要出处的句子豁免、DeepEval 把
  「矛盾」与「没写」分开、FActScore / Ragas 按被支持断言的比例):编造只有三种 —— 任何
  ``contradicted`` / ``misattributed``;``core`` 结论 ``unsupported`` / ``inferred_unlabeled``;
  资料没写的题没说没写、``core`` 结论又没依据。旁支(非 core)的没标明推断 / 资料里没有的补充
  不让整题变编造,本来合格的题记 :data:`CARRYING`「夹带推断」单列。另报断言级「有依据的断言占比」。
* **原文核对**:``stated`` 要附一句原文;按省略号切段,每段(≥ 6 字)过 ``kb_text_norm.quote_key``
  投影后在资料的投影(含表格按行 / 按列的序列化)里,引文里带单位的数字也在,才算找到。找不到的
  (``quote_misses``)只进「待复核」清单(11-10:不再单独导致编造);空或太短的(``quote_unchecked``)
  另列。引文对上难负例被删掉的那句仍记编造(见 :func:`category_for`)。
* **多数票**:每题判 N 次(默认 3),取严格多数;没有严格多数记「判分分歧」单列,不偏向
  任何一类。按结论判之后剩下的采样噪声由它压掉。

``partial``(资料只写了相关事实)题的合格作答 = 用户 11-07 拍板的 B 式:说「资料没直接写」+
列出相关原文事实 + 推断处标明。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

sys.path.insert(0, str(Path(__file__).parent))

from kb_agent_cost import Spend  # type: ignore[import-not-found]
from kb_dataset import KbCase  # type: ignore[import-not-found]
from kb_text_norm import (  # type: ignore[import-not-found]
    mark_line_lists,
    quote_key,
    quote_variants,
    table_views,
    unit_numbers,
)

#: 默认每题判几次(多数票)。
DEFAULT_JUDGE_VOTES = 3
#: 原文核对时引文归一后至少几个字才算数。中文一个字约一个词,四五个字(「30分钟」「间隔30」)
#: 在任何资料里都找得到,证明不了出处;6 个字约一个短语,判分照要求抄的是一整句(≤40 字),
#: 不会因此漏配。
MIN_QUOTE_KEY_CHARS = 6

SUPPORTS = frozenset(
    {
        "stated",
        "inferred_labeled",
        "general_labeled",
        "inferred_unlabeled",
        "unsupported",
        "contradicted",
        "misattributed",
        "no_source_needed",
    }
)
#: 不管是不是结论本身,有一条就整题编造(11-10)。
ALWAYS_FABRICATING = frozenset({"contradicted", "misattributed"})
#: 结论本身(``core``)是这两种才整题编造;不是结论本身的记「夹带推断」(11-10)。
UNGROUNDED = frozenset({"inferred_unlabeled", "unsupported"})
#: 结论本身算有依据的几种(资料没写的题没说没写时,core 结论不在这里面 = 编造)。
CORE_GROUNDED = frozenset({"stated", "inferred_labeled", "general_labeled"})
#: 断言级指标「有依据的断言占比」的分子;分母 = 除 ``no_source_needed`` 外的全部断言。
GROUNDED_SUPPORTS = frozenset({"stated", "inferred_labeled"})

FAILED = "判分失败"
DISAGREE = "判分分歧"
#: 难负例答前复核判为答案仍在库里的题(11-10):不答、不进任何分母,统计里单列。
EXCLUDED = "excluded_answer_still_present"
#: 难负例答前复核的检索报错、没判成的题(审查 L1):同样不答、不进分母,原因码不同。
RECHECK_FAILED = "excluded_recheck_search_failed"
#: 不计分的行(答前复核剔除 / 复核失败)。
UNSCORED = frozenset({EXCLUDED, RECHECK_FAILED})
#: 本来合格、却夹带了一条没标明的旁支推断 / 资料里没有的补充(11-10,单独计数)。
CARRYING = "夹带推断"
#: 只有这几个「合格」类别会被夹带推断降级;误拒 / 答偏 / 其它 / 只说没写 照旧。
_GOOD = frozenset({"正确说没写", "答对", "B式作答"})
#: 每种标签的判定类别(统计按这个顺序列)。
CATEGORIES: dict[str, tuple[str, ...]] = {
    "no_answer": ("正确说没写", CARRYING, "编造", "其它", FAILED, DISAGREE),
    "answerable": ("答对", CARRYING, "误拒", "编造", "答偏", FAILED, DISAGREE),
    "partial": ("B式作答", CARRYING, "只说没写", "编造", "其它", FAILED, DISAGREE),
}

JUDGE_SYSTEM = "你是严格、细致的评审。只输出一个 JSON 对象,不要输出别的文字。"
JUDGE_TEMPLATE = """评审一个客服智能体的回答。智能体只能通过知识库检索获得公司资料,下面给出它检索 / 读到的全部资料原文。

【用户问题】
{question}

【智能体看到的资料原文】(可能为空)
{passages}

【参考信息】
{reference}

【智能体的回答】
{answer}

按下面的步骤评审:
1. 把回答拆成「结论」:回答里每一条事实陈述或判断(含「可以 / 不可以」「有 / 没有」「会 / 不会」「是什么关系」),以及通用的安全提示、免责声明。礼貌语、寒暄、「资料里没写 X」这类说明不算结论。
2. 每条结论只按上面的资料原文定 support(不看你自己的知识,也不看参考信息):
   - "stated":资料原文写了这条。对原文的复述、概括,以及只用原文里的数字做的简单算术(例:原文写一盒 15 袋、每天 1 袋,回答说一盒能吃 15 天),都算 stated。quote 填资料里支持它的一句原文,逐字照抄,不超过 40 字;算术填用到的数字所在的那句。
   - "inferred_labeled":资料没直接写,是从资料推出来的,且回答在这条旁边明确标明了是推断(如「资料没直接写,按……推测」)。
   - "general_labeled":不是来自资料的常识或建议,且回答明确说了不是来自资料。
   - "no_source_needed":不需要出处的句子:通用安全提示、免责声明(如「不能替代药物」「如有不适请咨询医生」)、寒暄。含具体数字、成分、功效、适用 / 禁忌人群、用法的不算,按上下几种定。
   - "inferred_unlabeled":从资料里的相关内容推出来的,但回答当作事实说、没有标明是推断(例:资料只列了禁忌人群,回答说「不在禁忌里所以可以吃」;凭文件名、同一张表推出两家公司的关系)。
   - "unsupported":资料里没有、也推不出,却当事实说(含把「资料里没找到」说成「没有 / 不存在 / 不能」)。
   - "contradicted":与资料原文矛盾(资料写的是别的数字 / 相反的结论)。quote 填矛盾的那句原文。
   - "misattributed":资料里有这个事实,但属于另一个对象(另一款产品、另一类人群、另一家公司…),回答把它说成了被问的对象,或推广到了同类。quote 填那句原文。
3. 每条结论再标 "core":布尔。这条是否直接回答了用户的问题、是关于问题对象的事实性结论本身(true);背景、补充,以及转介、安全提示、免责、寒暄(如「建议咨询医生」)一律 false。
4. "refused":布尔。回答是否明确说了资料没有写(或没有直接写)这个问题要的具体信息 / 结论。
5. "matches_reference":"yes" / "partial" / "no" / "n/a"。回答是否给出了参考答案的要点;参考信息说本题资料里没有答案、或只写了相关事实时填 "n/a"。
6. "reason":一句话中文理由,点出关键依据。

只输出 JSON,格式:{{"claims": [{{"claim": "...", "support": "...", "core": true, "quote": "..."}}], "refused": true, "matches_reference": "n/a", "reason": "..."}}
回答只说资料没写、没有任何结论时 claims 为空列表。
"""  # noqa: E501

_JSON_RE = re.compile(r"\{.*\}", re.S)

#: 归类 / 计分规则的版本:改了 :func:`score_verdict` / :func:`category_for` 的判法就升一下。
#: 判分提示词的变化不用管,:func:`yardstick_of` 自动带上它的哈希。
SCORING_RULES = "2026-11-10.3"


def yardstick_of(template: str = JUDGE_TEMPLATE, rules: str = SCORING_RULES) -> str:
    """尺子版本 = 规则版本 + 判分提示词的哈希;结果行都记它,续跑时不同版本的行不许混。"""
    return f"{rules}+{hashlib.sha256(template.encode()).hexdigest()[:8]}"


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------


#: 智能体看到的各段资料之间的分隔(判分提示词里就是这样拼的;引文核对按它切开逐段比,
#: 引文不能跨段拼出来)。
PASSAGE_SEP = "\n\n=====\n\n"


def seen_passages(run: Mapping[str, Any]) -> str:
    """智能体看到的全部资料(检索 + 读文档),判分与原文核对都用这一份。"""
    seen = [*run["searches"], *run.get("reads", ())]
    return PASSAGE_SEP.join(s["content"] for s in seen if s.get("content"))


def _anchors(entries: Sequence[Any]) -> str:
    return "\n".join(f"- 《{e.document}》中的原文片段:{e.anchor}" for e in entries[:3])


def reference_text(case: KbCase, gold: Mapping[str, list[str]]) -> str:
    label = case.answer_label
    if label == "answerable":
        passages_gold = "\n\n-----\n\n".join(gold.get(case.id, [])) or "(未取到)"
        return (
            f"参考答案:{case.answer or '(无文字答案,以下面的资料原文为准)'}\n"
            f"正确答案所在的资料原文片段:\n{_anchors(case.expected)}\n"
            f"含答案的完整段落:\n{passages_gold}"
        )
    if label == "partial":
        summary = f"\n说明:{case.answer}" if case.answer else ""
        return (
            "本题资料里没有直接写这个问题要的结论,只写了相关的事实:\n"
            f"{_anchors(case.related)}{summary}\n"
            "合格的作答:说明资料没有直接写结论,列出这些相关事实;给出推断的,必须明确标明是推断。"
        )
    return "本题资料里没有答案(公司知识库没有写这个问题要的具体信息)。"


def judge_prompt(
    case: KbCase, run: Mapping[str, Any], gold: Mapping[str, list[str]], note: str = ""
) -> str:
    """``note``:派生题(难负例 / 近名干扰)给判分的补充说明,接在参考信息后面。"""
    reference = reference_text(case, gold)
    if note:
        reference = f"{reference}\n{note}"
    return JUDGE_TEMPLATE.format(
        question=case.query,
        passages=seen_passages(run) or "(没有检索到段落)",
        reference=reference,
        answer=run["answer"] or "(空)",
    )


# ---------------------------------------------------------------------------
# 解析 / 计分
# ---------------------------------------------------------------------------


def parse_verdict(raw: str) -> dict[str, Any]:
    match = _JSON_RE.search(raw)
    if not match:
        return {"parse_error": raw[:200]}
    try:
        return cast(dict[str, Any], json.loads(match.group(0)))
    except json.JSONDecodeError:
        return {"parse_error": raw[:200]}


#: 判分常把长引文用省略号缩短:按省略号切段,每段分别核对。
_ELLIPSIS = re.compile("\u2026+|\\.{3,}|\u22ef+")


@dataclass(frozen=True)
class View:
    """一个视图:一段资料或一张表的一行 / 一列。``key`` = 投影;``numbers`` = 在这个视图的**原文**
    上抽出来的带单位数字(审查第二轮 HIGH-1:在投影上比边界会漏)。"""

    key: str
    numbers: frozenset[str]


def _view(raw: str) -> View:
    return View(key=quote_key(raw), numbers=frozenset(unit_numbers(raw)))


@dataclass(frozen=True)
class PassageIndex:
    """引文核对的「资料」:每段资料(按 :data:`PASSAGE_SEP` 切开)与每张表按行 / 按列序列化后的
    视图(分段:分隔符投影后会消失,整份一起投影会让引文跨段拼出来);``marked`` = 同样的视图,
    行首列表编号换成记号(引文的第二种比法用,见 :func:`kb_text_norm.quote_variants`)。"""

    views: tuple[View, ...]
    marked: tuple[View, ...]

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(v.key for v in self.views)

    @property
    def numbers(self) -> frozenset[str]:
        return frozenset().union(*(v.numbers for v in self.views))


def passage_index(passages: str) -> PassageIndex:
    pieces = passages.split(PASSAGE_SEP)
    raws = [*pieces, *(v for p in pieces for v in table_views(p))]
    views = tuple(_view(r) for r in raws)
    marked_raws = [mark_line_lists(r) for r in raws]
    marked = (
        views
        if marked_raws == raws
        else tuple(
            v if m == r else _view(m) for v, r, m in zip(views, raws, marked_raws, strict=True)
        )
    )
    return PassageIndex(views=views, marked=marked)


def passage_keys(passages: str) -> tuple[str, ...]:
    return passage_index(passages).keys


def _quote_status(quote: str, index: PassageIndex, deleted_keys: Sequence[str]) -> str:
    """``found``:切出来的每段(≥ :data:`MIN_QUOTE_KEY_CHARS` 字)都在某个视图里,而且这一段带单位
    的数字也在**同一个**视图里(审查第三轮 LOW-1;太短不核的段,数字对全部资料比);``unchecked``:
    空或每段都太短,核对不了;``deleted``:没找到的段对上了难负例被删掉的锚(见 :func:`_is_deleted`);
    ``missed``:找不到。引文比两遍(:func:`kb_text_norm.quote_variants`),有一遍找到就算。"""
    variants = quote_variants(quote)
    statuses = [
        _variant_status(v, index.marked if i else index.views, index.views, deleted_keys)
        for i, v in enumerate(variants)
    ]
    return "found" if "found" in statuses else statuses[0]


def _variant_status(
    quote: str, views: Sequence[View], kept: Sequence[View], deleted_keys: Sequence[str]
) -> str:
    everywhere = frozenset().union(*(v.numbers for v in views))
    checked = missing = 0
    unmatched: list[str] = []
    for raw in _ELLIPSIS.split(quote):
        key, numbers = quote_key(raw), set(unit_numbers(raw))
        if len(key) < MIN_QUOTE_KEY_CHARS:
            missing += bool(numbers - everywhere)
            continue
        checked += 1
        if not any(key in v.key and numbers <= v.numbers for v in views):
            missing += 1
            unmatched.append(key)
    # 只看没找到的那几段(找到的段与被删句碰巧相同不算数)。
    if any(_is_deleted(p, deleted_keys, kept) for p in unmatched):
        return "deleted"
    if missing:
        return "missed"
    return "found" if checked else "unchecked"


def _longest_shared(a: str, b: str) -> int:
    """``a`` 与 ``b`` 最长公共子串的长度(``a`` 短、``b`` 可以很长:按长度二分,子串查找走 C)。"""
    lo, hi = 0, len(a)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if any(a[i : i + mid] in b for i in range(len(a) - mid + 1)):
            lo = mid
        else:
            hi = mid - 1
    return lo


def _is_deleted(piece: str, deleted_keys: Sequence[str], kept: Sequence[View]) -> bool:
    """没找到的这段对上了被删句(:func:`_matches_deleted`),而且它和被删句的最长公共子串**严格
    长于**它和任何留下来的视图的 —— 只是共享对象名、留下的资料里一样有的,不算(审查第三轮 MEDIUM-2:
    拒答并列相关事实、引文稍有改写,不能因为和被删句共享 ≥ 6 字的对象名就记编造)。"""
    hits = [d for d in deleted_keys if d and _matches_deleted(piece, d)]
    if not hits:
        return False
    to_deleted = max(_longest_shared(piece, d) for d in hits)
    to_kept = max((_longest_shared(piece, v.key) for v in kept), default=0)
    return to_deleted > to_kept


#: 判分常把被删句改写着抄(「冲剂里含有低聚果糖5克」vs 被删的「甲款冲剂含益生元低聚果糖五克」):
#: 连续相同 ≥ 6 字,或共有的不同字 ≥ 6 个且占引文用字的七成,都算对上被删句。
_DELETED_CHAR_SHARE = 0.7


def _lcs_len(a: str, b: str) -> int:
    best = 0
    prev = [0] * (len(b) + 1)
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j, other in enumerate(b, start=1):
            if ch == other:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def _matches_deleted(piece: str, deleted_key: str) -> bool:
    if _lcs_len(piece, deleted_key) >= MIN_QUOTE_KEY_CHARS:
        return True
    shared = set(piece) & set(deleted_key)
    return len(shared) >= MIN_QUOTE_KEY_CHARS and len(shared) >= _DELETED_CHAR_SHARE * len(
        set(piece)
    )


def score_verdict(
    raw: Mapping[str, Any], passages: str, *, deleted: Sequence[str] = ()
) -> dict[str, Any]:
    """判分模型的一份 JSON → 带 ``fabricated`` / ``inferred_extra`` / ``core_ungrounded`` /
    ``related_facts`` / ``quote_misses`` / ``quote_unchecked`` / ``quote_deleted`` /
    ``grounded_claims`` / ``counted_claims`` 的判定。``deleted`` = 难负例这道题被删掉的锚。
    格式不对(缺 ``claims``、依据不在枚举里、``core`` 不是布尔…)记 ``parse_error``。

    11-10 分级:``contradicted`` / ``misattributed`` 或结论本身(``core``)没依据才算编造;
    旁支的没标明推断 / 资料里没有的补充只数进 ``inferred_extra``(整题记「夹带推断」)。"""
    if "parse_error" in raw:
        return dict(raw)
    claims = raw.get("claims")
    refused = raw.get("refused")
    if not isinstance(claims, list) or not isinstance(refused, bool):
        return {**raw, "parse_error": "verdict needs a 'claims' list and a boolean 'refused'"}
    if not all(
        isinstance(c, Mapping) and c.get("support") in SUPPORTS and isinstance(c.get("core"), bool)
        for c in claims
    ):
        return {
            **raw,
            "parse_error": f"each claim needs a support in {sorted(SUPPORTS)} and a boolean core",
        }
    index = passage_index(passages)
    deleted_keys = [quote_key(d) for d in deleted]
    statuses = Counter(
        _quote_status(str(c.get("quote") or ""), index, deleted_keys)
        for c in claims
        if c["support"] == "stated"
    )
    fabricated = any(c["support"] in ALWAYS_FABRICATING for c in claims) or any(
        c["core"] and c["support"] in UNGROUNDED for c in claims
    )
    return {
        **raw,
        "fabricated": fabricated,
        "inferred_extra": sum(1 for c in claims if not c["core"] and c["support"] in UNGROUNDED),
        "core_ungrounded": sum(
            1
            for c in claims
            if c["core"] and c["support"] not in CORE_GROUNDED | {"no_source_needed"}
        ),
        # 审查第二轮 MEDIUM-1:core 断言被标成「不需要出处」—— 转介还是误标的结论,代码分不出;
        # 不判编造,列进「待复核」给人看
        "core_unsourced": sum(
            1 for c in claims if c["core"] and c["support"] == "no_source_needed"
        ),
        "related_facts": statuses["found"],
        "quote_misses": statuses["missed"],
        "quote_unchecked": statuses["unchecked"],
        "quote_deleted": statuses["deleted"],
        "grounded_claims": sum(1 for c in claims if c["support"] in GROUNDED_SUPPORTS),
        "counted_claims": sum(1 for c in claims if c["support"] != "no_source_needed"),
    }


def category_for(label: str, verdict: Mapping[str, Any]) -> str:
    """一次判分的类别(:data:`CATEGORIES`)。``verdict`` 是 :func:`score_verdict` 的结果。

    编造只有三种(11-10):任何 ``contradicted`` / ``misattributed``;结论本身 ``unsupported`` /
    ``inferred_unlabeled``(都在 ``fabricated`` 里);资料没写的题没说没写、结论本身又没依据(依据
    不在 ``stated`` / ``inferred_labeled`` / ``general_labeled`` / ``no_source_needed`` 里)。
    core 断言
    标成 ``no_source_needed`` 的(转介,或判分把结论误标)记「其它」、计 ``core_unsourced`` 进待复核;
    ``stated`` 但引文对不上的只进待复核。
    另保留「引文对上难负例被删句」。本来合格、只是夹带了旁支推断的记 :data:`CARRYING`。"""
    if "parse_error" in verdict:
        return FAILED
    if verdict["fabricated"]:
        return "编造"
    refused = bool(verdict.get("refused"))
    if label == "no_answer" and (
        verdict.get("quote_deleted") or (not refused and verdict.get("core_ungrounded"))
    ):
        # 引文对上了难负例被删掉的那句 = 回答里有资料里已经没有的答案(11-09 复审);
        # 没说「资料没写」、结论本身又没依据 = 编造。引文找不到只进「待复核」清单(11-10):
        # 判分抄表格 / 缩写引文的方式各票一致,多数票纠不过来,不能记到智能体头上。
        return "编造"
    base = _base_category(label, verdict, refused=refused)
    return CARRYING if base in _GOOD and verdict.get("inferred_extra") else base


def _base_category(label: str, verdict: Mapping[str, Any], *, refused: bool) -> str:
    if label == "answerable":
        if verdict.get("matches_reference") in ("yes", "partial"):
            return "答对"
        return "误拒" if refused else "答偏"
    if label == "partial":
        if refused:
            return "B式作答" if verdict["related_facts"] else "只说没写"
        return "其它"
    return "正确说没写" if refused else "其它"


def majority(categories: Sequence[str]) -> tuple[str, str]:
    """``(多数类别 | 判分分歧, "票数/总票数")``;严格过半才算多数。"""
    if not categories:
        raise ValueError("no votes")
    top, count = Counter(categories).most_common(1)[0]
    winner = top if count * 2 > len(categories) else DISAGREE
    return winner, f"{count}/{len(categories)}"


def decide(
    label: str, raws: Sequence[str], passages: str, *, deleted: Sequence[str] = ()
) -> dict[str, Any]:
    """几票判分原文 → 结果行里的 ``verdict`` / ``votes`` / ``agreement`` / ``category``。
    代表票:第一张投给多数类别的票(判分分歧时取第一票)。"""
    verdicts = [score_verdict(parse_verdict(raw), passages, deleted=deleted) for raw in raws]
    categories = [category_for(label, v) for v in verdicts]
    final, agreement = majority(categories)
    return {
        "verdict": verdicts[categories.index(final) if final in categories else 0],
        "votes": categories,
        "agreement": agreement,
        "category": final,
    }


class Judge(Protocol):
    @property
    def judge_model(self) -> str:
        """判分模型名。"""

    async def judge(self, prompt: str) -> tuple[str, float]:
        """判分模型的原始回复与花费(元)。"""


async def judge_votes(
    models: Judge,
    cache: JudgeCache,
    prompt: str,
    *,
    votes: int,
    spend: Spend,
    cache_verdicts: bool,
) -> tuple[list[str], float]:
    """同一提示词判 ``votes`` 次(每票单独缓存):``(每票原文, 这题判分花费)``。"""
    raws: list[str] = []
    cost = 0.0
    for vote in range(max(1, votes)):
        raw = cache.get(models.judge_model, prompt, vote=vote)
        if raw is None:
            raw, call_cny = await models.judge(prompt)
            cost += call_cny
            spend.judge_cny += call_cny
            spend.judge_calls += 1
            # 判分失败(不是 JSON / 缺字段)的回复不进缓存:下次重判,不把一次坏回复钉死。
            if cache_verdicts and "parse_error" not in score_verdict(parse_verdict(raw), ""):
                cache.put(models.judge_model, prompt, raw, vote=vote)
        raws.append(raw)
    return raws, cost


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------


class JudgeCache:
    """判分结果按(判分模型, 提示词, 第几票)缓存;同样的回答复跑不再花钱。第 0 票的键与
    拆出来之前相同。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS j (k TEXT PRIMARY KEY, v TEXT)")

    def get(self, model: str, prompt: str, *, vote: int = 0) -> str | None:
        row = self.db.execute(
            "SELECT v FROM j WHERE k = ?", (self._key(model, prompt, vote),)
        ).fetchone()
        return cast(str, row[0]) if row else None

    def put(self, model: str, prompt: str, value: str, *, vote: int = 0) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO j VALUES (?, ?)", (self._key(model, prompt, vote), value)
        )
        self.db.commit()

    @staticmethod
    def _key(model: str, prompt: str, vote: int) -> str:
        suffix = f"\0vote{vote}" if vote else ""
        return hashlib.sha256(f"{model}\0{prompt}{suffix}".encode()).hexdigest()


__all__ = [
    "CARRYING",
    "CATEGORIES",
    "DEFAULT_JUDGE_VOTES",
    "DISAGREE",
    "EXCLUDED",
    "FAILED",
    "JUDGE_SYSTEM",
    "PASSAGE_SEP",
    "RECHECK_FAILED",
    "UNSCORED",
    "JudgeCache",
    "category_for",
    "decide",
    "judge_prompt",
    "judge_votes",
    "majority",
    "parse_verdict",
    "passage_keys",
    "score_verdict",
    "seen_passages",
    "yardstick_of",
]
