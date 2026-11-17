"""B-300 第 9 版 K 组每条记录的**读库断言**(设计 2026-11-15 §11.2a / §11.3)。纯函数,不调模型。

判法全部读库里的字段(``b300_live._dump`` + ``b300_k_platform.dump_row``):新值有没有有效行、旧值
的 ``superseded_by`` 指向谁、旧值是否仍有效、近邻是否被动过。要判官的只有四处(各一票):K-b /
K-b2「犹豫的措辞是否留在正文里」、K-p「没被否认的那一半是否还在某条有效行里」、K-late「说得更全
的那部分是否还在某条有效行里」、K-s「B 是否已不在有效的汇总句里、A 与 C 是否还在」(两问)。

:func:`checks` 返回 ``(检查, 待判项)``,形状同 ``b300_redesign_checks.checks``:「好」类指标
(:data:`GOOD_METRICS`)``True`` = 做到了,其余 ``True`` = 坏,``None`` = 等判官;``missing`` =
被测代码没有的能力(报 incomplete);``counts`` = 只报数的。

另有两样跨类的(``b300_outcomes`` 合计成 ``ALL-K`` / ``ALL``):

* **近邻被动过**(:func:`neighbour_checks`):近邻种子被作废 = 坏;另数「模型列了、那一行却仍有效」
  (程序挡下的)与其中落在近邻上的。
* **写的保证**(:func:`write_guarantee`,§7.3 的定义):每条进了去重合并的用户一方候选,写回之后
  它自己是有效行、或有一条层级不低于它的有效行说着同样的事(内容相同 / 模型判它与之重复)、或它
  是连着一条有效行的历史行 —— 三样都不占 = 坏。
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

import b300_k_items as ki
import b300_k_scenarios as ks
from b300_k_platform import parse_reconcile
from b300_redesign_base import USER_SIDE, JudgeItem, capability, has_key, pass_memories
from b300_scenarios import Scenario

#: 「好」类指标:``True`` = 做到了;判官答 yes = 做到了。
GOOD_METRICS = frozenset(
    {
        "retired_to_new",
        "new_valid",
        "x_still_valid",
        "hedge_kept",
        "settled",
        "user_stated_y_verbatim",
        "user_stated_y_reworded",
        "x_retired_ok",
        "stored_new",
        "any_retired",
        "half_kept",
        "fuller_kept",
        "later_valid",
        "new_stored",
    }
)
#: 跨类合计的指标 → 报告里的类名。
AGGREGATE = {"neighbour_retired": "ALL-K", "write_guarantee_broken": "ALL"}
#: 层级(同平台的 ``_ORIGIN_TIERS``):更正过的 3 > 用户说的 2 > 用户确认 1 > 来自助手 = 未记录 0。
_TIER = {"user_corrected": 3, "user_stated": 2, "user_confirmed": 1}
_LEGACY_OPS = frozenset({"UPDATE", "DELETE"})

# --------------------------------------------------------------------------- 判官措辞(不带领域词)

HEDGE_WORDING = (
    "The user said something tentatively. A memory keeps the doubt if its own text says, in any "
    "wording, that the user is unsure, is guessing, or has not decided. A memory that states the "
    "value as settled does not keep the doubt, whatever label or score it carries."
)
HALF_WORDING = (
    "A stored memory said two things. The user later denied only one of them. The other thing is "
    "still remembered if some valid memory still states it, in any wording."
)
FULLER_WORDING = (
    "The user gave a detailed value in a short answer and later mentioned the same thing in a "
    "more general way. The detail is kept if some valid memory still states it, in any wording."
)
SUMMARY_WORDING = (
    "A summary memory listed several things about the user. The user then said that one of them "
    "no longer holds. The summary may have been rewritten since."
)
WORDINGS = {
    "hedge": HEDGE_WORDING,
    "half": HALF_WORDING,
    "fuller": FULLER_WORDING,
    "summary": SUMMARY_WORDING,
}


def _bullets(rows: Sequence[str]) -> str:
    return "\n".join(f"- {r}" for r in rows) or "- (none)"


def hedge_prompt(lines: Sequence[str], memories: Sequence[str]) -> str:
    said = "\n".join(lines)
    return (
        f"Conversation:\n{said}\n\n{HEDGE_WORDING} Question: does at least one memory below keep "
        f"the doubt? yes = it does.\nMemories:\n{_bullets(memories)}"
    )


def half_prompt(old: str, denial: str, memories: Sequence[str]) -> str:
    return (
        f'Earlier memory: "{old}"\nThe user then said: "{denial}"\n\n{HALF_WORDING} Question: is '
        "the part the user did not deny still stated by at least one valid memory below? yes = it "
        f"is.\nValid memories:\n{_bullets(memories)}"
    )


def fuller_prompt(asked: str, answer: str, later: str, memories: Sequence[str]) -> str:
    return (
        f"Conversation:\n[assistant] {asked}\n[user] {answer}\n[user, later] {later}\n\n"
        f"{FULLER_WORDING} Question: is the detailed value from the short answer still stated by "
        f"at least one valid memory below? yes = it is.\nValid memories:\n{_bullets(memories)}"
    )


def summary_prompt(
    old: str, denial: str, others: Sequence[str], current: Sequence[str], ask: str
) -> str:
    question = (
        "does the current summary still present the denied thing as holding? yes = it does."
        if ask == "b"
        else "is any of the other things missing from the current summary? yes = something is "
        "missing."
    )
    return (
        f'Original summary: "{old}"\nThe user then said: "{denial}"\n'
        f"The other things the summary listed:\n{_bullets(others)}\n\n{SUMMARY_WORDING} "
        f"Question: {question}\nCurrent summary:\n{_bullets(current)}"
    )


# --------------------------------------------------------------------------- 读库的小工具


def _norm(text: object) -> str:
    return " ".join(str(text).split()).strip().lower()


class Rows:
    """一条记录写回之后库里的全部行(含已作废的),按种子 / 新写的分开看。"""

    def __init__(self, result: Mapping[str, Any]) -> None:
        self.all: list[dict[str, Any]] = list(result.get("memories", []))
        self.by_id: dict[str, dict[str, Any]] = {str(m.get("id")): m for m in self.all}
        self.seeded = {k: str(v) for k, v in (result.get("seeded") or {}).items()}
        seed_ids = set(self.seeded.values())
        self.new = [m for m in self.all if str(m.get("id")) not in seed_ids]
        self.new_live = [m for m in self.new if m.get("live")]
        self.live = [m for m in self.all if m.get("live")]

    def seed(self, key: str) -> Mapping[str, Any]:
        return self.by_id.get(self.seeded.get(key, ""), {})

    def seed_live(self, key: str) -> bool:
        return bool(self.seed(key).get("live"))

    def successor(self, row: Mapping[str, Any]) -> Mapping[str, Any]:
        """``superseded_by`` 指向的那一行(没有 = 空)。"""
        return self.by_id.get(str(row.get("superseded_by")), {})

    def new_with(
        self, key: str, *, origins: frozenset[str] | set[str] | None = None
    ) -> list[dict[str, Any]]:
        return [
            m
            for m in self.new_live
            if has_key(str(m.get("content", "")), key)
            and (origins is None or m.get("origin") in origins)
        ]

    def tier(self, row: Mapping[str, Any]) -> int:
        """一行的层级;汇总行按成员算(§6.3)。"""
        members = [self.by_id.get(str(i), {}) for i in row.get("consolidated_from") or []]
        if members:
            return max((self.tier(m) for m in members if m), default=0)
        if float(row.get("confidence") or 0.0) >= 1.0:
            return 3
        return _TIER.get(str(row.get("origin")), 0)

    def summaries_live(self) -> list[dict[str, Any]]:
        return [m for m in self.live if m.get("consolidated_from")]


def _retired_to_new(rows: Rows, key: str) -> bool:
    """旧值那条种子已作废,而且 ``superseded_by`` 指向一条**新写的有效行**。"""
    seed = rows.seed(key)
    target = rows.successor(seed)
    return (
        bool(seed)
        and not seed.get("live")
        and bool(target.get("live"))
        and str(target.get("id")) not in set(rows.seeded.values())
    )


def _user_statement_stored(rows: Rows) -> bool:
    """用户这一句话存下了:有一条新写的、有效的、用户一方的普通行(不是汇总行)。"""
    return any(
        m.get("origin") in USER_SIDE and not m.get("consolidated_from") for m in rows.new_live
    )


# --------------------------------------------------------------------------- 跨类:近邻、写的保证


def _round_decisions(rnd: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for call in rnd.get("calls", []):
        if call.get("kind") == "reconcile" and "input" in call:
            out += parse_reconcile(str(call["input"]), str(call.get("reply", "")))
    return out


def _reconciled(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """各次写回里去重合并的决定。"""
    return [d for rnd in result.get("rounds", []) for d in _round_decisions(rnd)]


def neighbour_checks(out: dict[str, Any], result: Mapping[str, Any]) -> None:
    """§11.3 末行:近邻被作废 = 坏;「模型列了、被程序挡下」与其中落在近邻上的只报数。"""
    rows, counts = Rows(result), out["counts"]
    near_ids = {rows.seeded[k] for k in ks.NEAR_KEYS if k in rows.seeded}
    if near_ids:
        out["neighbour_retired"] = any(not rows.by_id.get(i, {}).get("live") for i in near_ids)
        counts["neighbours_seeded"] += len(near_ids)
    by_content: dict[str, list[Mapping[str, Any]]] = {}
    for m in rows.all:
        by_content.setdefault(_norm(m.get("content", "")), []).append(m)
    for decision in _reconciled(result):
        counts["legacy_op"] += int(decision["op"].upper() in _LEGACY_OPS)
        for content in (*decision["replaces"], *decision["restates"]):
            counts["listed"] += 1
            hit = by_content.get(_norm(content), [])
            if any(m.get("live") for m in hit):
                counts["listed_blocked"] += 1
                if any(str(m.get("id")) in near_ids for m in hit):
                    counts["listed_blocked_on_neighbour"] += 1


def write_guarantee(out: dict[str, Any], result: Mapping[str, Any]) -> None:
    """§7.3「写的保证」。只查进了去重合并模型调用的用户一方候选(材料里看得到的);没走模型的
    确定性「重复」按定义就是第 2 项,不在这里。被测代码不是第 9 版 → incomplete。"""
    if not capability(result, "retire_split"):
        out["missing"]["write_guarantee_broken"] = "retire_split"
        return
    labels = result.get("origin_labels") or {}
    rows, counts = Rows(result), out["counts"]
    broken = skipped = False
    for rnd in result.get("rounds", []):
        if rnd.get("errors"):
            # 写回里出了错(被吞的异常):这一次没做完,保证不适用(§7.3);只按出错跳过 ——
            # 写入扫描拦下一条也会 failed,但那一轮其实做完了(复审 LOW-1)
            counts["guarantee_rounds_skipped"] += 1
            skipped = True
            continue
        scan_blocked = {_content_norm(c) for c in rnd.get("scan_blocked") or []}
        for decision in _round_decisions(rnd):
            origin = labels.get(decision["source"])
            if origin not in USER_SIDE:
                continue
            if _content_norm(decision["content"]) in scan_blocked:
                counts["guarantee_scan_blocked"] += 1  # 写入扫描拦下的那一条另计(§7.3 的出口)
                continue
            counts["guarantee_candidates"] += 1
            tier = _TIER.get(str(origin), 0)
            if not _guaranteed(rows, decision, tier):
                broken = True
                counts["guarantee_broken_candidates"] += 1
    if broken:
        out["write_guarantee_broken"] = True
    elif skipped:
        # 有一次写回没做完、量的部分又都没坏:不当成「没坏」,报没量到(复审 LOW-2)
        out["missing"]["write_guarantee_broken"] = "writeback_error"
    else:
        out["write_guarantee_broken"] = False


def _guaranteed(rows: Rows, decision: Mapping[str, Any], tier: int) -> bool:
    text = _content_norm(decision["content"])  # 与平台 normalized_memory_text 同一口径(审查 M1)
    same = [m for m in rows.all if _content_norm(m.get("content", "")) == text]
    # ① 它自己是有效行,或 ② 有一条层级不低于它、内容相同的有效行
    if any(m.get("live") and rows.tier(m) >= tier for m in same):
        return True
    # ② 模型判它与之重复的那一条有效、层级不低于它
    if decision["op"].upper() == "NOOP" and decision["target"] is not None:
        target = _content_norm(decision["target"])
        targets = [m for m in rows.all if _content_norm(m.get("content", "")) == target]
        if any(m.get("live") and rows.tier(m) >= tier for m in targets):
            return True
    # ③ 它是历史行(或之后又被更晚的说法退了):顺着 superseded_by 走得到一条有效行
    for m in same:
        row: Mapping[str, Any] = m
        for _hop in range(10):
            row = rows.successor(row)
            if not row:
                break
            if row.get("live"):
                return True
    return False


# --------------------------------------------------------------------------- 逐类


#: 去重合并里候选的来源 → 出自哪一次提炼(§4:第一次的产出一律是用户说的;第二次的是用户确认
#: 或来自助手)。
_PASS_OF_ORIGIN = {"user_stated": "pass1", "user_confirmed": "pass2", "assistant_derived": "pass2"}


def _correction_tag(flag: object) -> str:
    if flag is True:
        return "correction"
    return "no_correction" if flag is False else "unknown"


def _candidate_tag(decision: Mapping[str, Any], labels: Mapping[str, str]) -> str:
    """去重合并的一条候选 → ``<哪一次>_<带不带更正标记>``。"""
    which = _PASS_OF_ORIGIN.get(str(labels.get(decision["source"])), "other")
    return f"{which}_{_correction_tag(decision.get('is_correction'))}"


def _how_listed(decision: Mapping[str, Any], x_text: str) -> str | None:
    """这条候选怎么列的 X(与平台 ``_decision`` 同一口径):``restates_only`` = 只列在 ``restates``
    里;``replaces`` = 其余列了 X 的 —— ``replaces``、两张都列、旧的 ``UPDATE`` / ``DELETE`` 的目标
    (第一次的候选拿这几种退用户一方都查更正标记);没列 = ``None``。"""
    if decision["op"].upper() in _LEGACY_OPS and _norm(decision["target"] or "") == x_text:
        return "replaces"
    if x_text in {_norm(c) for c in decision["replaces"]}:
        return "replaces"
    if x_text in {_norm(c) for c in decision["restates"]}:
        return "restates_only"
    return None


def _same_text_user_side(decision: Mapping[str, Any], labels: Mapping[str, str]) -> bool:
    """§7.1 第 19 行例外 ③ 的近似(平台 ``_identical_neighbors``):有一个邻居与候选内容相同(平台
    同一归一口径)、同类型、两边事件日期不冲突(都有且不同才算冲突)、来源是用户一方。材料里没有
    智能体名,同格只比到类型 —— 近似;邻居本来只取召回范围(共享的事实 + 本智能体的经历),事实不带
    智能体名,所以实际很少多算。"""
    text, kind, day = (
        _content_norm(decision["content"]),
        decision.get("kind"),
        decision.get("event_date"),
    )
    return any(
        _content_norm(n["content"]) == text
        and n.get("kind") == kind
        and not (day and n.get("event_date") and n.get("event_date") != day)
        and labels.get(n["source"]) in USER_SIDE
        for n in decision.get("neighbours", ())
    )


def _non_statement_by_pass(
    out: dict[str, Any], result: Mapping[str, Any], rows: Rows, z_key: str
) -> None:
    """§11.2a K-n1 / §11.3 K-n(第 10 版):候选出自哪一次提炼、带不带更正标记,分开报数(只报数)。

    * ``z_extracted_<次>_<标记>``:两次提炼原样的回复里含 Z 的记忆;
    * ``x_listed_by_<次>_<标记>``:把 X 列进 ``replaces`` / ``restates`` 的候选;
    * ``x_retired_by_<次>_<标记>``:X 被退时退它的那一条 —— 算给 X 的 ``superseded_by`` 指向的行
      (写下的候选,或它判重复的那一条);对不上就算第一条列了 X 的;谁都没列 = ``unknown``;
    * 上一项里第一次的候选(``pass1_*``)另分两种不查更正标记的退法(互斥,先看第一种):
      ``x_retired_restates_only_by_<次>_<标记>`` —— 它只把 X 列在 ``restates`` 里(不是两张都列,
      也不是旧的 ``UPDATE``,:func:`_how_listed`);``x_retired_same_text_by_<次>_<标记>`` —— 它旁边
      有一条内容相同的用户一方邻居,走的是 §7.1 第 19 行例外 ③(:func:`_same_text_user_side`,
      近似)。

    读法:``pass1_no_correction`` 减去 ``restates_only`` 与 ``same_text`` 两项,剩下的是代码错
    (§7.1 第 19 行该挡)。只列在 ``restates`` 的按设计不受这一条管;走例外 ③ 的是用户 11-17 接受的
    已知限制(同一个错值原样再答一次,第三轮审查 MEDIUM-B)。``pass2_*`` 退了 X(用户说的)一律是
    代码错(默认规则只看来源,怎么列、内容相同不相同都不放行),所以第二次的不分这两种。
    ``pass1_correction`` 退了 X 是指令没挡住(附录 C 的 C1~C3),没有程序兜底。"""
    counts, labels = out["counts"], result.get("origin_labels") or {}
    for kind in ("pass1", "pass2"):
        for m in pass_memories(result, kind):
            if has_key(str(m.get("content", "")), z_key):
                counts[f"z_extracted_{kind}_{_correction_tag(m.get('is_correction'))}"] += 1
    x = rows.seed("x")
    if not x:
        return
    x_text = _norm(x.get("content", ""))
    listing = [d for d in _reconciled(result) if _how_listed(d, x_text)]
    for d in listing:
        counts[f"x_listed_by_{_candidate_tag(d, labels)}"] += 1
    if x.get("live"):
        return
    into = _content_norm(rows.successor(x).get("content", ""))
    retirer = next(
        (
            d
            for d in listing
            if into and into in {_content_norm(d["content"]), _content_norm(d["target"] or "")}
        ),
        listing[0] if listing else None,
    )
    if retirer is None:
        counts["x_retired_by_unknown"] += 1
        return
    tag = _candidate_tag(retirer, labels)
    counts[f"x_retired_by_{tag}"] += 1
    if not tag.startswith("pass1_"):
        return
    if _how_listed(retirer, x_text) == "restates_only":
        counts[f"x_retired_restates_only_by_{tag}"] += 1
    elif _same_text_user_side(retirer, labels):
        counts[f"x_retired_same_text_by_{tag}"] += 1


def _x_retired(out: dict[str, Any], rows: Rows, tag: str) -> None:
    retired = not rows.seed_live("x")
    out["x_retired"] = retired
    out["counts"][f"x_retired_{tag}"] += int(retired)
    out["counts"][f"units_{tag}"] += 1


def _hedge(
    out: dict[str, Any], items: list[JudgeItem], rec: Mapping[str, Any], s: Scenario, rows: Rows
) -> None:
    it = ks.item_of(s)
    out["x_still_valid"] = rows.seed_live("x")
    stored = rows.new_with(it.z.key)
    for m in stored:
        out["counts"][f"hedge_origin_{m.get('origin')}"] += 1
    if not stored:
        out["hedge_kept"] = False
        return
    out["hedge_kept"] = None
    lines = [f"[user] {s.turns[0].user}"]
    if len(s.turns) > 1:
        lines = [f"[assistant] {s.turns[0].reply}", f"[user] {s.turns[1].user}"]
    prompt = hedge_prompt(lines, [str(m["content"]) for m in stored])
    items.append(JudgeItem(f"{rec['key']}|hedge_kept", "hedge_kept", prompt))


def _stated_y(out: dict[str, Any], result: Mapping[str, Any], s: Scenario, rows: Rows) -> None:
    """K-e2 / K-w:有一条有效的、用户说的 Y;逐字相同的那一半由程序定(必须全中)。"""
    it = ks.item_of(s)
    hit = any(
        m.get("live")
        and m.get("origin") == "user_stated"
        and has_key(str(m.get("content", "")), it.y.key)
        for m in rows.all
    )
    out[f"user_stated_y_{s.variant}"] = hit
    out["x_retired_ok"] = not rows.seed_live("x")
    out["counts"]["weaker_y_retired"] += int(not rows.seed_live("y"))
    # 用户定的规则(审查 M-2):模型判「重复」、那条弱的 Y 是用户一方(用户确认)、归一后内容不同 →
    # 两条都有效。只报数;新行的 supersedes 只记一条:同一次也退了 X 时记的是 X,所以「连着它」
    # (``_linked``)只是并存里的一部分。
    y_id = rows.seeded.get("y")
    coexists = rows.seed_live("y") and hit
    out["counts"]["weaker_y_coexists"] += int(coexists)
    out["counts"]["weaker_y_coexists_linked"] += int(
        coexists and any(m.get("supersedes") == y_id for m in rows.new_live)
    )
    if s.variant == "verbatim":
        # 前提:提炼写出的正文确实与种下的那条逐字相同(否则这一个不算「程序定」的,报数)
        extracted = {
            _norm(m.get("content", ""))
            for kind in ("pass1", "pass2")
            for m in pass_memories(result, kind)
        }
        out["counts"]["verbatim_extracted"] += int(_norm(it.y.memory) in extracted)


def _half(
    out: dict[str, Any], items: list[JudgeItem], rec: Mapping[str, Any], s: Scenario, rows: Rows
) -> None:
    c = ks.item_of(s).compound
    out["stored_new"] = _user_statement_stored(rows)
    if rows.seed_live("x"):  # 那条复合的记忆没被退:没被否认的那一半当然还在
        out["half_kept"] = True
        return
    out["counts"]["compound_retired"] += 1
    out["half_kept"] = None
    prompt = half_prompt(c.memory, c.deny_b, [str(m.get("content", "")) for m in rows.live])
    items.append(JudgeItem(f"{rec['key']}|half_kept", "half_kept", prompt))


def _late(
    out: dict[str, Any], items: list[JudgeItem], rec: Mapping[str, Any], s: Scenario, rows: Rows
) -> None:
    late = ks.item_of(s).late
    live = [str(m.get("content", "")) for m in rows.live]
    if s.variant == "fuller":
        if any(has_key(t, late.early_key) for t in live):
            out["fuller_kept"] = True
            return
        out["fuller_kept"] = None
        prompt = fuller_prompt(late.assistant, late.first, late.second, live)
        items.append(JudgeItem(f"{rec['key']}|fuller_kept", "fuller_kept", prompt))
        return
    out["later_valid"] = any(has_key(t, late.late_key) for t in live)
    earlier = [m for m in rows.all if has_key(str(m.get("content", "")), late.early_key)]
    earlier = [m for m in earlier if not has_key(str(m.get("content", "")), late.late_key)]
    out["counts"]["earlier_history"] += int(
        any(not m.get("live") and rows.successor(m).get("live") for m in earlier)
    )
    out["counts"]["earlier_coexists"] += int(any(m.get("live") for m in earlier))
    out["counts"]["earlier_not_stored"] += int(not earlier)


def _content_norm(text: object) -> str:
    """判「内容相同」的口径,与平台 ``normalized_memory_text`` 一致:NFC、折全角半角、casefold、
    空白合并、去掉末尾的句末标点(反复)。句中的标点不动。"""
    folded = "".join(_fold_width(ch) for ch in unicodedata.normalize("NFC", str(text)))
    folded = folded.casefold()
    out = " ".join(folded.split())
    while out and out[-1] in _SENTENCE_END:
        out = out[:-1].rstrip()
    return out


_SENTENCE_END = frozenset(".!;。")


def _fold_width(ch: str) -> str:
    """只折全角 / 半角(兼容分解标 ``<wide>`` / ``<narrow>`` 的);别的兼容分解不折。"""
    decomposition = unicodedata.decomposition(ch)
    if decomposition.startswith(("<wide>", "<narrow>")):
        return "".join(chr(int(code, 16)) for code in decomposition.split()[1:])
    return ch


def _summary_seed(s: Scenario) -> object:
    return next((x for x in s.seeds if x.key == "summary"), None)


def _restates_member(row: Mapping[str, Any], members: Sequence[Mapping[str, Any]]) -> bool:
    """新行与某条成员原文归一后相同,且那条成员已被作废、连到这一行(撞唯一索引时让出位置)。"""
    text = _content_norm(row.get("content", ""))
    return any(
        m
        and _content_norm(m.get("content", "")) == text
        # superseded_by 有值 = 已作废(``b300_live._live``),不用再看 live
        and m.get("superseded_by") == row.get("id")
        for m in members
    )


#: K-s 否认的那一半,每个单元走到了哪一步(``b300_outcomes`` 按它把不达标的次数分开报)。
SUMMARY_CAUSES = ("not_listed", "listed_blocked", "marked_not_rewritten", "rewritten")
_REWRITTEN = frozenset({"rewritten", "emptied", "collided"})


def _summary_cause(
    result: Mapping[str, Any], summary: str, marked: bool, rewrites: Sequence[Mapping[str, Any]]
) -> str:
    """``rewritten``:打了标、重写落了库(之后还不达标 = 重写本身错);``marked_not_rewritten``:打了
    标、重写没落库(没调、失败、原样返回);``listed_blocked``:模型把汇总行列进了清单、程序没让
    打标(没进同格前 2 名等,§7.1 第 6、18 行);``not_listed``:模型根本没列它。"""
    if marked:
        done = any(o in _REWRITTEN for r in rewrites for o in r.get("outcomes", []))
        return "rewritten" if done else "marked_not_rewritten"
    listed = any(
        _norm(c) == _norm(summary)
        for d in _reconciled(result)
        for c in (*d["replaces"], *d["restates"])
    )
    return "listed_blocked" if listed else "not_listed"


def _summary(
    out: dict[str, Any], items: list[JudgeItem], rec: Mapping[str, Any], s: Scenario, rows: Rows
) -> None:
    result, trio, counts = rec["result"], ks.item_of(s).trio, out["counts"]
    rewrites = [r["rewrite"] for r in result.get("rounds", []) if r.get("rewrite")]
    before = (rewrites[-1].get("before") or {}) if rewrites else {}
    for outcome in (o for r in rewrites for o in r.get("outcomes", [])):
        counts[f"rewrite_{outcome}"] += 1
    marked = bool(before.get("summary_flagged"))
    counts["marked"] += int(marked)
    seed = rows.seed("summary")
    if s.variant == "restate":
        out["rewrite_marked"] = marked
        out["summary_changed"] = not seed.get("live")
        # 用户 11-16:提炼出的话与某条成员原文归一后相同、撞上那条成员(它被作废并连到新行)而多出
        # 的一行,单独计数、不算进同义多行那条线;线只算其它原因多出来的。
        members = [rows.seed(k) for k in getattr(_summary_seed(s), "summary_of", ())]
        verbatim = [m for m in rows.new_live if _restates_member(m, members)]
        counts["restate_member_verbatim"] += int(bool(verbatim))
        out["synonym_added"] = any(m not in verbatim for m in rows.new_live)
        return
    out["new_stored"] = _user_statement_stored(rows)
    out["info"] = {"cause": _summary_cause(result, trio.summary, marked, rewrites)}
    counts[f"deny_{out['info']['cause']}"] += 1
    # 直接作废(没经重写):写回落完库、重写还没开始时,汇总行已经不是有效行
    out["summary_retired_directly"] = before.get("summary_live") is False
    if not capability(result, "summary_rewrite"):
        out["missing"]["b_still_in_summary"] = out["missing"]["ac_lost"] = "summary_rewrite"
        return
    counts["rewrite_not_run"] += int(marked and not any(r.get("outcomes") for r in rewrites))
    current = [str(m.get("content", "")) for m in rows.summaries_live()]
    if not current:  # 汇总行没有了(重写说什么都不剩,或被直接作废):B 不在,A、C 也丢了
        out["b_still_in_summary"], out["ac_lost"] = False, True
        return
    others = [trio.a.memory, trio.c.memory]
    for metric, ask in (("b_still_in_summary", "b"), ("ac_lost", "ac")):
        out[metric] = None
        prompt = summary_prompt(trio.summary, trio.deny_b, others, current, ask)
        items.append(JudgeItem(f"{rec['key']}|{metric}", metric, prompt))


def checks(record: Mapping[str, Any], scenario: Scenario) -> tuple[dict[str, Any], list[JudgeItem]]:
    """一条 K 组记录的读库断言 + 待判项(每类的线见 ``b300_report.K_LINES``)。"""
    out: dict[str, Any] = {"missing": {}, "counts": Counter()}
    items: list[JudgeItem] = []
    result, cls, s = record["result"], scenario.cls, scenario
    if cls not in ks.K_CLASSES:
        raise ValueError(f"not a K class: {cls}")
    it: ki.KItem = ks.item_of(s)
    rows, counts = Rows(result), out["counts"]
    if cls == "K-a":
        retired = _retired_to_new(rows, "x") and bool(rows.new_with(it.y.key))
        missed = not any(has_key(str(m.get("content", "")), it.y.key) for m in rows.new)
        if s.variant == "dated":
            # 带日期的碰上不带日期的(用户 11-16):不进 K-a 的线,单独报数、不设线
            counts["dated_units"] += 1
            counts["dated_retired_to_new"] += int(retired)
            counts["dated_retired"] += int(not rows.seed_live("x"))
            counts["dated_coexists"] += int(rows.seed_live("x") and bool(rows.new_with(it.y.key)))
            counts["dated_missed"] += int(missed)
        else:
            out["retired_to_new"], out["missed"] = retired, missed
    elif cls == "K-ap":
        new = rows.new_with(it.y.key)
        out["new_valid"] = bool(new)
        counts["x_still_valid"] += int(rows.seed_live("x"))
        counts["x_retired"] += int(not rows.seed_live("x"))
        counts["linked_to_x"] += int(any(m.get("supersedes") == rows.seeded.get("x") for m in new))
    elif cls in {"K-b", "K-b2"}:
        _hedge(out, items, record, s, rows)
    elif cls == "K-bp":
        out["settled"] = bool(rows.new_with(it.z.key)) and not rows.seed_live("x")
        counts["hedge_retired"] += int(not rows.seed_live("hedge"))
    elif cls == "K-c":
        out["x_retired"] = not rows.seed_live("x")
        out["mislabelled"] = bool(rows.new_with(it.y.key, origins=USER_SIDE))
        counts["assistant_rows"] += len(rows.new_with(it.y.key, origins={"assistant_derived"}))
    elif cls == "K-cp":
        _x_retired(out, rows, s.variant)
        counts["conclusion_confirmed"] += int(
            bool(rows.new_with(it.y.key, origins={"user_confirmed"}))
        )
    elif cls in {"K-n1", "K-n2"}:
        _x_retired(out, rows, s.variant)
        _non_statement_by_pass(out, result, rows, it.z.key)
        stored = bool(rows.new_with(it.z.key, origins=USER_SIDE))
        counts["user_side_stored"] += int(stored)
        counts[f"user_side_stored_{s.variant}"] += int(stored)
    elif cls == "K-e1":
        x = rows.seed("x")
        out["retired_to_new"] = not x.get("live") and x.get("superseded_by") == rows.seeded.get("y")
        out["duplicate_added"] = bool(rows.new_with(it.y.key))
    elif cls in {"K-e2", "K-w"}:
        _stated_y(out, result, s, rows)
    elif cls == "K-f":
        out["retired_to_new"] = _retired_to_new(rows, "x")
        out["stored_new"] = _user_statement_stored(rows)
        counts["new_key_hit"] += int(bool(rows.new_with(it.gone_key)))
    elif cls == "K-g":
        gone = [not rows.seed_live(k) for k in ("x", "x2")]
        out["any_retired"] = any(gone)
        counts["both_retired"] += int(all(gone))
    elif cls == "K-p":
        _half(out, items, record, s, rows)
    elif cls == "K-t":
        out["x_retired"] = not rows.seed_live("x")
        out["stored_new"] = bool(rows.new_with(it.y.key))
    elif cls == "K-late":
        _late(out, items, record, s, rows)
    else:
        _summary(out, items, record, s, rows)
    neighbour_checks(out, result)
    write_guarantee(out, result)
    return out, items


def gate_checks(
    record: Mapping[str, Any], scenario: Scenario
) -> tuple[dict[str, Any], list[JudgeItem]]:
    """回归闸门的 K 段(§11.1 第 2 步 ①):只看读库的那几条线,不送判官(等判官的指标去掉)。"""
    out, _items = checks(record, scenario)
    return {k: v for k, v in out.items() if v is not None}, []


#: 检查里不是指标的键(``b300_outcomes`` 不把它们当线)。
INFO_KEYS = frozenset({"missing", "counts", "info"})
