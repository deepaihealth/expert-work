"""B-301 PR 3 —— ``region="auto"`` 有横条报 ``partial`` 时,工具自己把接缝再快看一次。

设计见 ``docs/superpowers/specs/2026-11-11-b301-image-orientation-design.md`` §4.2 / §4.3 / §10。
今天(:func:`~orchestrator.tools.tile_reading.decide_auto`)退回整张读,并让主模型「用这个区域
再问一次」;现在工具自己用原问题(加上与横条相同的那段 JSON 要求,好逐字比对)看一次那个区域:

* 与整张读一致(:func:`seam_agrees`,宁严勿松)→ 仍给整张读,「小字可能不可靠」原样保留,
  「再问一次」换成「已重看过、读到 "<值>"、整张读里也有它」—— **值总是写出来**,主模型自己能核;
  **不说可信**:同一个模型对同一张横图两次都读成 68.1 是实测过的(§2)。
* 不一致、或这次什么都没读到 → 两个都列出,明说不确定;不投票、不挑一个。
* 重看失败 / 超时 / run 时间不够 → 输出与今天逐字节相同(只在 meta 和计数器里留痕)。
* 报 ``partial`` 的横条不相邻 → 不是一条接缝,不看,输出与今天相同。

本模块只放纯逻辑:接缝框、比对、给主模型的文字、meta、计数器。裁图与调用看图模型在
:mod:`orchestrator.tools.vision`。平台文字一律英文、通用、零租户内容。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal

from expert_work.common.observability.metrics import expert_work_counter
from orchestrator.tools.region_zoom import REGION_SCALE, Box, Tile, band_axis
from orchestrator.tools.tile_reading import (
    _COMPARATORS,
    _FALLBACK_REASONS,
    RAW_META_CHARS,
    TileReply,
    TileResult,
    _display,
    _flat,
    _union_box,
    aggregate_tiles,
    canonical_text,
    normalize_unit,
)

SeamOutcome = Literal["agree", "disagree", "no_reading", "error", "skipped_deadline"]

#: 重看那一次的整体上限(秒),含排队等切块名额。普通快看测试环境 30 天最长 28 秒
#: (``vision.ASK_IMAGE_DEEP_TIMEOUT_S`` 的注释),与问方向同一个数。超时 = 今天的输出。
SEAM_RELOOK_TIMEOUT_S: Final = 30.0
#: run 剩的时间不到这么多秒就不重看(``skipped_deadline``,今天的输出):重看最多用掉
#: :data:`SEAM_RELOOK_TIMEOUT_S`,之后还要给主模型留至少同样多的时间用这个结果。
SEAM_RELOOK_MIN_RUN_LEFT_S: Final = 2 * SEAM_RELOOK_TIMEOUT_S
#: 重看那一块在 meta / 日志里的名字。
SEAM_LABEL: Final = "seam"

ask_image_seam_relook_total = expert_work_counter(
    "expert_work_ask_image_seam_relook_total",
    "ask_image region='auto' looks where a band saw the value cut off at its edge and the tool "
    "looked at the seam again (B-301 PR 3), by outcome: agree / disagree (with the whole-image "
    "reading), no_reading (the second look read nothing), error (it failed or timed out; the "
    "answer is as before) or skipped_deadline (too little run time left; the answer is as "
    "before).",
    ("outcome",),
)

#: 第二次看什么都没读到时,括号里说的原因(按它回的状态)。
_NOTHING_READ: Final[dict[str, str]] = {
    "not_found": "it replied not_found",
    "partial": "it saw the value cut off again",
    "unparseable": "its reply could not be read",
}


def seam_box(results: Sequence[TileResult], bands: Sequence[Tile]) -> Box | None:
    """要重看的框:报 ``partial`` 的那几条的并集(今天建议里给的那个框)。

    ``results`` 与 ``bands`` 同序。没有 ``partial``、或报 ``partial`` 的几条**不相邻**(例如第 1 条和
    第 4 条:并集差不多是整张图,也不是一条接缝)→ ``None``,不重看。只有一条报 ``partial`` 时
    并集就是它自己 —— 原样再裁一次,看到的还是被它的边切开的那几个像素(裁图文件都是同一个)。
    所以沿切的方向两头各放宽半条,让它的两条边都落在框里面。"""
    hits = [i for i, r in enumerate(results) if r.reply is not None and r.reply.status == "partial"]
    if not hits or hits != list(range(hits[0], hits[-1] + 1)):
        return None
    union = _union_box([results[i].tile.box for i in hits])
    if len(hits) > 1:
        return union
    x1, y1, x2, y2 = union
    if band_axis(tuple(bands)) == "y":
        half = (y2 - y1) // 2
        return x1, max(0, y1 - half), x2, min(REGION_SCALE, y2 + half)
    half = (x2 - x1) // 2
    return max(0, x1 - half), y1, min(REGION_SCALE, x2 + half), y2


# ---------------------------------------------------------------------------
# 两次读数一致吗(宁可判不一致:不一致会把两个都列出来,是安全的那一边)
# ---------------------------------------------------------------------------

_CANONICAL_COMPARATOR: Final[dict[str, str]] = dict(_COMPARATORS)
_COMPARATOR_ALTERNATION: Final = "|".join(re.escape(token) for token, _ in _COMPARATORS)
#: 一个数(带正负号;数字之间的 ``-`` 已被 ``canonical_text`` 换成范围号 ``~``):前面可带比较符,
#: 后面紧跟(最多隔一个空白)的一段非数字、非标点、非 Markdown 记号(``*`` ``_`` 反引号)算它的单位。
_NUMBER_TOKEN_RE: Final = re.compile(
    rf"(?:(?P<cmp>{_COMPARATOR_ALTERNATION})\s*)?(?P<num>[-+]?\d+(?:\.\d+)?)"
    r"(?:\s?(?P<unit>[^\s\d.,;:!?()\[\]{}\"'~/*_`\u3001\uff0c\u3002\uff1b\uff1a\uff01\uff1f\uff08\uff09]+))?"
)
#: 两个数之间只隔着这些(时间 ``12:30``、血压 ``120/80``、范围 ``3.9~6.1``、日期 ``2026~11~12`` /
#: ``2026年11月``、版本 ``1.2.3``)= 它们是同一个值的两段。
_JOINED_GAP_RE: Final = re.compile(r"\s*(?:[/:~.\-]|\u5e74|\u6708|\u65e5|\u65f6|\u70b9|\u5206)\s*")
#: 一段数之后、不隔数字和句读就到了冒号 = 那是一个标签(``Step 1:``、``2 values:``),不是值。
_LABEL_TAIL_RE: Final = re.compile(r"[^\d.,;!?\n]{0,40}?:")
#: 只有文字的值:按这些标点把整张读的回答切成段;某一段整段等于这个值、而且后面不是冒号才算一致。
_SEGMENT_RE: Final = re.compile(
    "[^,.;:!?\\n\"'()\\[\\]\u201c\u201d\u2018\u2019\uff0c\u3002\uff1b\uff1a\uff01\uff1f\uff08\uff09]+"
)
_COLONS: Final = frozenset(":\uff1a")
_LETTER_RE: Final = re.compile(r"[^\W\d_]")


@dataclass(frozen=True)
class _Number:
    comparator: str
    digits: str
    unit: str
    #: 在归一后的文字里的位置:整段的起止,以及数字本身的结尾(不含单位)。
    start: int
    digits_end: int
    end: int

    def same_as(self, other: _Number) -> bool:
        """比较符相同、数相同(带前导零的按原样比:编号 ``0915`` 不等于 ``915``);**两边都带**单位
        时写法必须相同 —— 只有一边写了单位藏不住一个不同的数,不算冲突。"""
        if self.comparator != other.comparator:
            return False
        if _leading_zero(self.digits) or _leading_zero(other.digits):
            if self.digits != other.digits:
                return False
        elif Decimal(self.digits) != Decimal(other.digits):
            return False
        return not self.unit or not other.unit or self.unit == other.unit


def _leading_zero(digits: str) -> bool:
    bare = digits.lstrip("+-")
    return len(bare) > 1 and bare.startswith("0") and not bare.startswith("0.")


def _numbers(text: str) -> tuple[list[_Number], str]:
    """``text`` 归一之后里面的数(按出现顺序),连同归一后的文字(位置按它算)。"""
    canonical = canonical_text(text)
    return [
        _Number(
            comparator=_CANONICAL_COMPARATOR.get(m.group("cmp") or "", ""),
            digits=m.group("num"),
            unit=normalize_unit(m.group("unit") or ""),
            start=m.start(),
            digits_end=m.end("num"),
            end=m.end(),
        )
        for m in _NUMBER_TOKEN_RE.finditer(canonical)
    ], canonical


def _bounded_in(needle: str, haystack: str) -> bool:
    """``needle`` 作为整体出现在 ``haystack`` 里(两边不是字母 / 数字),都按归一后的写法比。"""
    wanted = " ".join(canonical_text(needle).casefold().split())
    text = " ".join(canonical_text(haystack).casefold().split())
    return re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", text) is not None


def _numbers_agree(full_answer: str, seen: str, wanted: list[_Number]) -> bool:
    given, text = _numbers(full_answer)
    count = len(wanted)
    if len(given) < count:
        return False
    if not all(w.same_as(g) for w, g in zip(wanted, given, strict=False)):
        return False
    last = given[count - 1]
    if len(given) > count and _JOINED_GAP_RE.fullmatch(text[last.digits_end : given[count].start]):
        return False  # 对上的只是一个值的前半段(120 对 120/80、12 对 12:30、2026 对 2026-11-12)
    if _LABEL_TAIL_RE.match(text, last.end):
        return False  # 对上的是标签里的数(1 对「Step 1: …」、2 对「2 values: …」)
    rest = _NUMBER_TOKEN_RE.sub(" ", canonical_text(seen))
    return not _LETTER_RE.search(rest) or _bounded_in(seen, full_answer)


def _text_agrees(full_answer: str, seen: str) -> bool:
    target = _flat(seen)
    if not target:
        return False
    for segment in _SEGMENT_RE.finditer(full_answer):
        if _flat(segment.group()) != target:
            continue
        follows = full_answer[segment.end() : segment.end() + 1]
        if follows not in _COLONS:  # 后面是冒号:那是标签(「HBsAg: negative」里的 HBsAg)
            return True
    return False


def seam_agrees(full_answer: str, seen: str) -> bool:
    """重看读到的 ``seen``(值连同单位)与整张读的回答一致吗。宁严勿松 —— 拿不准就是不一致:

    * 带数的值:整张读回答里**开头的那几个数**要与它的数逐个对上 —— 比较符相同、数相同;两边都
      带单位时单位写法相同(``120/85`` 对 ``120/80``、``<5`` 对 ``5``、``68.1 lb`` 对 ``68.1 kg``、
      ``6.1`` 对「5.6 …(参考 3.9-6.1)」都不一致)。对上之后,回答里紧接着的下一个数若与它只隔
      ``/ : ~ - .`` 或日期 / 时间的字(同一个值的另一段),或者对上的那段后面不隔数字、句读就是冒号
      (标签),也不一致。去掉数和单位之后值里还有字母(编号 ``HT-2026-…``、``A12``)时,整个值
      还要作为整体出现在回答里。
    * 只有文字的值:回答按标点切段,某一段整段等于它、而且后面不是冒号(「Zhang」对
      「Name: Zhang San」、「HBsAg」对「HBsAg: negative」都不一致)。
    """
    wanted, _canonical = _numbers(seen)
    if wanted:
        return _numbers_agree(full_answer, seen, wanted)
    return _text_agrees(full_answer, seen)


# ---------------------------------------------------------------------------
# 结果、meta、给主模型的文字
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeamRelook:
    """重看一次的结果。``calls`` 是实际发出的看图调用数(0 或 1),``usage`` 是它的用量。"""

    outcome: SeamOutcome
    box: Box
    reply: TileReply | None = None
    error: str | None = None
    calls: int = 0
    usage: Mapping[str, Any] | None = None

    @property
    def looked(self) -> bool:
        """真看到了一个回答(一致 / 不一致 / 没读到)—— 只有这三种改写给主模型的文字。"""
        return self.outcome in ("agree", "disagree", "no_reading")

    @property
    def seen(self) -> str:
        """重看读到的值(连同单位),给主模型看、也拿来比对;没读到是空串。"""
        if self.reply is None or self.reply.status != "found":
            return ""
        return _display(TileResult(tile=Tile(SEAM_LABEL, self.box), reply=self.reply))

    def meta(self) -> dict[str, Any]:
        """工具 meta 里的 ``seam_relook``:结论、框,以及回答(截断)或失败原因。"""
        entry: dict[str, Any] = {"outcome": self.outcome, "region": list(self.box)}
        if self.error is not None:
            entry["error"] = self.error
        if self.reply is not None:
            entry.update(
                {
                    "status": self.reply.status,
                    "value": self.reply.value,
                    "unit": self.reply.unit,
                    "raw": self.reply.raw[:RAW_META_CHARS],
                }
            )
        return entry


def judged(
    full_answer: str, box: Box, reply: TileReply, *, usage: Mapping[str, Any] | None
) -> SeamRelook:
    """第二次看(发出了 1 次调用)的回答 → 结论(比法见 :func:`seam_agrees`)。"""
    relook = SeamRelook("no_reading", box, reply=reply, calls=1, usage=usage)
    if reply.status != "found":
        return relook
    agrees = seam_agrees(full_answer, relook.seen)
    return SeamRelook("agree" if agrees else "disagree", box, reply=reply, calls=1, usage=usage)


def seam_content(full_answer: str, results: Sequence[TileResult], relook: SeamRelook) -> str:
    """看到了回答时给主模型的文字(设计 §4.3)。开头与今天相同:整张读的回答 + 「小字可能不可靠」;
    只把「再问一次」那半句换成重看的结果(读到了就写出读到的值);横条读数照旧附在后面。"""
    region = list(relook.box)
    if relook.outcome == "agree":
        tail = (
            f"The seam between bands was looked at again (region={region}) and read "
            f'"{relook.seen}", which also appears in the whole-image reading above; two looks '
            "agreeing does not make it certain, so say the value is uncertain."
        )
    elif relook.outcome == "disagree":
        tail = (
            f'Two looks at this value disagree: the whole-image reading above / "{relook.seen}" '
            f"from looking again at the seam between bands (region={region}). Neither is "
            "preferred; say the value is uncertain."
        )
    else:
        status = relook.reply.status if relook.reply is not None else ""
        why = _NOTHING_READ.get(status, "no reply")
        tail = (
            f"The seam between bands was looked at again (region={region}) and nothing was read "
            f"there ({why}), so the two looks do not agree: say the value is uncertain."
        )
    bands = aggregate_tiles(results, seam_looked_at=relook.box).content
    return (
        f"{full_answer}\n\n(region='auto': whole-image reading; small print may be unreliable - "
        f"{_FALLBACK_REASONS['partial']}. {tail})\nBand readings: {bands}"
    )
