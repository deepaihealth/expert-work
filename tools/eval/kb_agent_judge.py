"""知识库智能体层评测的判分(B-206 11-09 尺子修正,从 ``kb_agent_eval.py`` 拆出)。

为什么这样判(详见 B-206 设计 §8.2 的 11-09 段):

* **按结论判**:判分模型不再整体拍板「编造没有」,而是把回答拆成一条条结论,逐条标依据
  (``stated`` 原文直接写了 / ``inferred_labeled`` 标明了的推断 / ``general_labeled`` 标明了
  不是来自资料的常识 / ``inferred_unlabeled`` 没标明的推断 / ``unsupported`` 资料里没有)。
  「编造」由代码算:有一条 ``inferred_unlabeled`` 或 ``unsupported`` 就是编造。11-05 的抖动
  (0006 同一回答 7 次判 2 次编造 / 5 次其它)出在旧口径「依据段落能直接推出的不算」—— 推断
  算不算「直接推出」每次判法不一;新口径把「推断」和「标没标」拆开,判分模型只需答这两件事。
* **原文核对**:``stated`` 要附一句原文;按省略号切段,每段(≥ 6 字)去空白与标点后都在智能体
  看到的资料里才算找到。找不到的(``quote_misses``)、空或太短核对不了的(``quote_unchecked``)
  不算列出了相关事实,统计里按标签列题号给人看。资料没写的题上只有两种按编造记:引文对上难负例
  被删掉的那句,或回答没说「资料没写」又拿找不到的引文当依据(见 :func:`category_for`)。
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
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).parent))

from kb_dataset import KbCase  # type: ignore[import-not-found]
from kb_metrics import anchor_key  # type: ignore[import-not-found]

#: 默认每题判几次(多数票)。
DEFAULT_JUDGE_VOTES = 3
#: 原文核对时引文归一后至少几个字才算数。中文一个字约一个词,四五个字(「30分钟」「间隔30」)
#: 在任何资料里都找得到,证明不了出处;6 个字约一个短语,判分照要求抄的是一整句(≤40 字),
#: 不会因此漏配。
MIN_QUOTE_KEY_CHARS = 6

SUPPORTS = frozenset(
    {"stated", "inferred_labeled", "general_labeled", "inferred_unlabeled", "unsupported"}
)
FABRICATING_SUPPORTS = frozenset({"inferred_unlabeled", "unsupported"})

FAILED = "判分失败"
DISAGREE = "判分分歧"
#: 每种标签的判定类别(统计按这个顺序列)。
CATEGORIES: dict[str, tuple[str, ...]] = {
    "no_answer": ("正确说没写", "编造", "其它", FAILED, DISAGREE),
    "answerable": ("答对", "误拒", "编造", "答偏", FAILED, DISAGREE),
    "partial": ("B式作答", "只说没写", "编造", "其它", FAILED, DISAGREE),
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
1. 把回答拆成「结论」:回答里每一条关于问题对象的事实陈述或判断(含「可以 / 不可以」「有 / 没有」「会 / 不会」「是什么关系」)。礼貌语、「建议咨询医生」这类泛泛建议、「资料里没写 X」这类说明不算结论。
2. 每条结论只按上面的资料原文定 support(不看你自己的知识,也不看参考信息):
   - "stated":资料原文直接写了这条。quote 填资料里支持它的一句原文,逐字照抄,不超过 40 字。
   - "inferred_labeled":资料没直接写,是从资料推出来的,且回答在这条旁边明确标明了是推断(如「资料没直接写,按……推测」)。
   - "general_labeled":不是来自资料的常识或建议,且回答明确说了不是来自资料。
   - "inferred_unlabeled":从资料里的相关内容推出来的,但回答当作事实说、没有标明是推断(例:资料只列了禁忌人群,回答说「不在禁忌里所以可以吃」;凭文件名、同一张表推出两家公司的关系)。
   - "unsupported":资料里没有、也推不出,却当事实说(含把「资料里没找到」说成「没有 / 不存在 / 不能」,或把另一个产品 / 对象的资料安到本题对象上)。
3. "refused":布尔。回答是否明确说了资料没有写(或没有直接写)这个问题要的具体信息 / 结论。
4. "matches_reference":"yes" / "partial" / "no" / "n/a"。回答是否给出了参考答案的要点;参考信息说本题资料里没有答案、或只写了相关事实时填 "n/a"。
5. "reason":一句话中文理由,点出关键依据。

只输出 JSON,格式:{{"claims": [{{"claim": "...", "support": "...", "quote": "..."}}], "refused": true, "matches_reference": "n/a", "reason": "..."}}
回答只说资料没写、没有任何结论时 claims 为空列表。
"""  # noqa: E501

_JSON_RE = re.compile(r"\{.*\}", re.S)

#: 归类 / 计分规则的版本:改了 :func:`score_verdict` / :func:`category_for` 的判法就升一下。
#: 判分提示词的变化不用管,:func:`yardstick_of` 自动带上它的哈希。
SCORING_RULES = "2026-11-09.2"


def yardstick_of(template: str = JUDGE_TEMPLATE, rules: str = SCORING_RULES) -> str:
    """尺子版本 = 规则版本 + 判分提示词的哈希;结果行都记它,续跑时不同版本的行不许混。"""
    return f"{rules}+{hashlib.sha256(template.encode()).hexdigest()[:8]}"


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------


def seen_passages(run: Mapping[str, Any]) -> str:
    """智能体看到的全部资料(检索 + 读文档),判分与原文核对都用这一份。"""
    seen = [*run["searches"], *run.get("reads", ())]
    return "\n\n=====\n\n".join(s["content"] for s in seen if s.get("content"))


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


def _quote_status(quote: str, passages_key: str, deleted_keys: Sequence[str]) -> str:
    """``found``:切出来的每段(≥ :data:`MIN_QUOTE_KEY_CHARS` 字)都在资料里;``unchecked``:
    空或每段都太短,核对不了;``deleted``:找不到、但对上了难负例被删掉的锚;``missed``:找不到。"""
    pieces = [anchor_key(p) for p in _ELLIPSIS.split(quote)]
    pieces = [p for p in pieces if len(p) >= MIN_QUOTE_KEY_CHARS]
    if not pieces:
        return "unchecked"
    if all(p in passages_key for p in pieces):
        return "found"
    if any(p in d or d in p for p in pieces for d in deleted_keys if d):
        return "deleted"
    return "missed"


def score_verdict(
    raw: Mapping[str, Any], passages: str, *, deleted: Sequence[str] = ()
) -> dict[str, Any]:
    """判分模型的一份 JSON → 带 ``fabricated`` / ``related_facts`` / ``quote_misses`` /
    ``quote_unchecked`` / ``quote_deleted`` 的判定。``deleted`` = 难负例这道题被删掉的锚。
    格式不对(缺 ``claims``、依据不在枚举里…)记 ``parse_error``。"""
    if "parse_error" in raw:
        return dict(raw)
    claims = raw.get("claims")
    refused = raw.get("refused")
    if not isinstance(claims, list) or not isinstance(refused, bool):
        return {**raw, "parse_error": "verdict needs a 'claims' list and a boolean 'refused'"}
    if not all(isinstance(c, Mapping) and c.get("support") in SUPPORTS for c in claims):
        return {**raw, "parse_error": f"each claim needs a support in {sorted(SUPPORTS)}"}
    passages_key = anchor_key(passages)
    deleted_keys = [anchor_key(d) for d in deleted]
    statuses = Counter(
        _quote_status(str(c.get("quote") or ""), passages_key, deleted_keys)
        for c in claims
        if c["support"] == "stated"
    )
    return {
        **raw,
        "fabricated": any(c["support"] in FABRICATING_SUPPORTS for c in claims),
        "related_facts": statuses["found"],
        "quote_misses": statuses["missed"],
        "quote_unchecked": statuses["unchecked"],
        "quote_deleted": statuses["deleted"],
    }


def category_for(label: str, verdict: Mapping[str, Any]) -> str:
    """一次判分的类别(:data:`CATEGORIES`)。``verdict`` 是 :func:`score_verdict` 的结果。"""
    if "parse_error" in verdict:
        return FAILED
    if verdict["fabricated"]:
        return "编造"
    refused = bool(verdict.get("refused"))
    if label == "no_answer" and (
        verdict.get("quote_deleted") or (not refused and verdict.get("quote_misses"))
    ):
        # 资料没写(含难负例)上只在两种情况下按引文判编造(11-09 复审):
        # - 引文对上了难负例被删掉的那句:回答里有资料里已经没有的答案;
        # - 回答没说「资料没写」,又拿一句在智能体看到的资料里找不到的「原文」当依据。
        # 说了没写、只是引文找不到 / 太短 / 为空的,只列出来给人看 —— 判分缩写引文的方式各票
        # 一致,多数票纠不过来,不能记到智能体头上。
        return "编造"
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
    "CATEGORIES",
    "DEFAULT_JUDGE_VOTES",
    "DISAGREE",
    "FAILED",
    "JUDGE_SYSTEM",
    "JudgeCache",
    "category_for",
    "judge_prompt",
    "majority",
    "parse_verdict",
    "score_verdict",
    "seen_passages",
    "yardstick_of",
]
