"""B-302 PR 3 —— ``edit_file`` 失败时说清楚(spec §4.3)。

两半:

* :data:`EDIT_DIAGNOSTICS_SOURCE` —— 拼进 ``file_ops._EDIT_MAIN`` 的沙箱代码段(只能用标准库):
  最像的 3 行与列号(§4.3.2)、「已经改过了」的 6 个条件(§4.3.3)、本轮自己改掉的旧文字
  (§4.3.3 末段)。它只算、只回 envelope 字段,一个字节都不写。
* 宿主侧把 envelope 渲成模型读到的那一段文字(§4.3.1 / §4.3.4 / §4.3.6):
  :func:`edit_failure_text`、:func:`already_applied_text`、:func:`streak_text`。

**不做任何按相似度的自动替换** —— 相似行只用来**说明**哪里不同;「已改过」只认原文里
逐字存在的新文字。所有判据与语言无关:码点、整行、相似度,对任何文字都成立。

平台文字只用通用词(edit / file / line / text),**刻意避开 "not found" 这几个字** ——
那一组词会被 ``error_classifier`` 的文本扫描认成「资源不存在」(专用异常已让分类不再扫
``edit_file`` 的这几种失败,但别的路径仍可能读到这段文字;测试钉着)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: 错误文字的上限(spec §4.3.6):最坏约 1,000~1,300 字符,超过统一的 500。只给
#: ``edit_file`` 的专用异常用,其它工具的截断不变。
EDIT_ERROR_MAX_CHARS = 1500

#: 计入「同一文件连续失败」的失败种类(spec §4.3.4)。其它 ``edit_file`` 失败(不在工作区、
#: 文件不在、读不了)不计、也不清零。
COUNTED_EDIT_FAILURES = frozenset({"no_match", "ambiguous", "stale"})

#: 第几次连续失败起在错误文字最前面加那句要求(spec §4.3.4)。
STREAK_NOTICE_FROM = 3

#: 沙箱代码段。原样拼进 ``_EDIT_MAIN``(在 ``_main`` 之前),所以**模块级只有定义**,
#: 不碰 ``_P``。用 raw 字符串:里面的反斜杠转义(换行、码点)是给沙箱里的 Python 解释的。
EDIT_DIAGNOSTICS_SOURCE = r"""

import unicodedata

#: §4.3.2 —— 相似度门槛(同 openclaw)、最多几行、超过多少行先粗筛。
_SIM_CUTOFF = 0.45
_SIM_TOP = 3
#: 相似度只比每行(和 probe)的前这么多码点;过了两道上界粗筛、真正去算 ratio 的行,整个文件
#: 合计最多这么多码点(之后的行不再比)—— 大文件 / 超长行的最坏情况(几千行、每行两千字符)
#: 要远低于沙箱的执行时限。列号与片段仍按整行算(_describe)。
_SIM_LINE_CAP = 200
_SIM_BUDGET = 4000000
#: 片段:差异处前后各约 8 个码点;为了不切断字形,每侧最多再向外扩 8 个。
_SNIP_RADIUS = 8
_SNIP_STRETCH = 8
#: 「本轮自己改掉」那一行最多引用多少码点。
_OWN_SNIP = 60
#: §4.3.3 条件 1 —— 新文字去首尾空白后至少几个码点(hermes 的门槛)。
_ALREADY_MIN = 8


def _extends(ch):
    # 这个码点贴在前一个上、不能单独成一个字形:组合附加符(类别 M)、零宽连接符、
    # 变体选择符、肤色修饰符、emoji 标签。
    cp = ord(ch)
    return (
        unicodedata.category(ch).startswith("M")
        or cp == 0x200D
        or 0xFE00 <= cp <= 0xFE0F
        or 0xE0100 <= cp <= 0xE01EF
        or 0x1F3FB <= cp <= 0x1F3FF
        or 0xE0020 <= cp <= 0xE007F
    )


def _cut_ok(s, p):
    # 在 s[p-1] 与 s[p] 之间切开会不会切断一个字形:两侧都不是附加码点才行。
    if p <= 0 or p >= len(s):
        return True
    return not (_extends(s[p]) or _extends(s[p - 1]))


def _clip(s, a, b):
    # s[a:b],两端向外扩到不切断字形为止(每侧最多 _SNIP_STRETCH 个码点)。
    a = max(0, a)
    b = min(len(s), b)
    lo = a
    while lo > 0 and a - lo < _SNIP_STRETCH and not _cut_ok(s, lo):
        lo -= 1
    hi = b
    while hi < len(s) and hi - b < _SNIP_STRETCH and not _cut_ok(s, hi):
        hi += 1
    return s[lo:hi]


def _probe(text, old):
    # §4.3.2 —— old_string 里第一条在文件里找不到的行(去首尾空白后比),返回 (它在
    # old_string 里是第几行, 去空白后的内容);每一行都找得到时返回 None。
    # 首尾两行可以只是文件某一行的一段(old_string 可以从行中间开始、到行中间结束);
    # 中间的行必须与文件里某一整行相同 —— 否则「漏抄了行尾注释」的中间行会被当成找得到。
    # 不含换行的串是 text 的子串,当且仅当它是某一行的子串。
    numbered = [(j + 1, raw.strip()) for j, raw in enumerate(old.split("\n")) if raw.strip()]
    whole = None
    for pos, (number, body) in enumerate(numbered):
        if pos in (0, len(numbered) - 1):
            if body not in text:
                return number, body
            continue
        if whole is None:
            whole = {line.strip() for line in text.split("\n")}
        if body not in whole:
            return number, body
    return None


def _similar(lines, probe):
    # 与 probe 最像的至多 _SIM_TOP 行的下标(0 起),按相似度从高到低、同分按行序。
    # real_quick_ratio / quick_ratio 是 ratio 的上界,先用它们筛掉不可能过门槛的行(结果不变);
    # 只比前 _SIM_LINE_CAP 个码点;去算 ratio 的行合计最多 _SIM_BUDGET 个码点(之后的行不再比)。
    import difflib

    matcher = difflib.SequenceMatcher(autojunk=False)
    matcher.set_seq2(probe[:_SIM_LINE_CAP])
    budget = _SIM_BUDGET
    scored = []
    for idx, raw in enumerate(lines):
        body = raw.strip()[:_SIM_LINE_CAP]
        if not body:
            continue
        matcher.set_seq1(body)
        if matcher.real_quick_ratio() < _SIM_CUTOFF or matcher.quick_ratio() < _SIM_CUTOFF:
            continue
        budget -= len(body)
        if budget < 0:
            break
        ratio = matcher.ratio()
        if ratio >= _SIM_CUTOFF:
            scored.append((-ratio, idx))
    scored.sort()
    return [idx for _neg, idx in scored[:_SIM_TOP]]


def _near(text, old):
    # (probe, 最像的行下标)。probe 为 None 时下标为空。
    probe = _probe(text, old)
    if probe is None:
        return None, []
    return probe, _similar(text.split("\n"), probe[1])


def _first_diff(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _describe(raw, probe):
    # 一行候选:列号按**原行**(含缩进)算、按码点数;两边从差异处前后各取约 8 个码点并排。
    raw = raw.rstrip("\r")
    body = raw.strip()
    lead = len(raw) - len(raw.lstrip())
    i = _first_diff(probe, body)
    col = lead + i + 1
    yours = _clip(probe, i - _SNIP_RADIUS, i + _SNIP_RADIUS)
    mine = _clip(raw, lead + i - _SNIP_RADIUS, lead + i + _SNIP_RADIUS)
    return col, yours, mine


def _no_match_detail(text, old):
    probe, near = _near(text, old)
    if probe is None:
        return "every line of 'old_string' is in the file, but not together in this order"
    if not near:
        return None
    lines = text.split("\n")
    parts = []
    for idx in near:
        col, yours, mine = _describe(lines[idx], probe[1])
        parts.append(
            "near line %d, column %d: yours «%s» / file «%s»" % (idx + 1, col, yours, mine)
        )
    return (
        "line %d of 'old_string' differs; closest: " % probe[0]
        + "; ".join(parts)
        + " (columns counting Unicode code points)"
    )


def _line_spans(text):
    # 每行非空白内容的 [起, 止) 偏移;空行为 None。
    spans = []
    pos = 0
    for raw in text.split("\n"):
        body = raw.strip()
        if body:
            start = pos + len(raw) - len(raw.lstrip())
            spans.append((start, start + len(body)))
        else:
            spans.append(None)
        pos += len(raw) + 1
    return spans


def _already_applied(text, edit):
    # §4.3.3 —— 只对调用前的原文 text 判;六个条件全部满足才返回行号(1 起),否则 None。
    # 便宜的条件先判,最后才算相似行(大文件上那一步最贵)。
    if edit.get("replace_all"):  # 5
        return None
    new = edit["new"].strip()
    old = edit["old"]
    if len(new) < _ALREADY_MIN:  # 1(码点数)
        return None
    if text.count(new) != 1:  # 2
        return None
    if _old_in(text, old):  # 3
        return None
    if new in old:  # 4
        return None
    start = text.find(new)
    end = start + len(new)
    whole = []
    touched = []
    for idx, span in enumerate(_line_spans(text)):
        if span is None:
            continue
        if span[0] >= start and span[1] <= end:
            whole.append(idx)
        if span[0] < end and span[1] > start:
            touched.append(idx)
    if not whole:  # 1(覆盖至少一整行)
        return None
    near = _near(text, old)[1]
    hits = [idx for idx in touched if idx in near]  # 6(覆盖的任一行在最像的 3 行里)
    if not hits:
        return None
    return hits[0] + 1


def _own_edit(text, old, history):
    # §4.3.3 末段 —— old_string 出错的那一行,与本轮某次成功改动的 old 里的某一行相同。
    # 按行比:部分重叠也认得出。只是说明,不改判。
    probe = _probe(text, old)
    if probe is None or not history:
        return None
    for item in reversed(history):
        if not isinstance(item, dict):
            continue
        prev_old = item.get("old")
        prev_new = item.get("new")
        if not isinstance(prev_old, str) or not isinstance(prev_new, str):
            continue
        if probe[1] not in [line.strip() for line in prev_old.split("\n")]:
            continue
        note = "this text was already replaced by your own edit earlier in this run"
        where = _locate(text, prev_new, probe[1])
        if where is not None:
            note += "; that place now reads: «%s» (line %d)" % where
        return note + "."
    return None


def _locate(text, new, probe):
    # 那次改动的新文字在文件里的哪一行(取新文字里与 probe 最像的那一行)。
    import difflib

    body = new.strip()
    if not body or body not in text:
        return None
    first = text.count("\n", 0, text.find(body))
    best, best_ratio = 0, -1.0
    for j, line in enumerate(body.split("\n")):
        if not line.strip():
            continue
        ratio = difflib.SequenceMatcher(None, probe, line.strip()).ratio()
        if ratio > best_ratio:
            best, best_ratio = j, ratio
    line = text.split("\n")[first + best].strip()
    return _clip(line, 0, _OWN_SNIP), first + best + 1


def _depends_on(old, skipped, updated):
    # old_string 与某个被跳过的改动的 new_string 有共同的行(去首尾空白后相同),且这一行在
    # 当前文字 updated 里没有 → 那一处的编号。当前文字里本来就有的共同行(else:、空括号这类
    # 常见短行)不算依赖:那样的 old_string 没对上,是它自己抄错了。
    present = None
    for raw in old.split("\n"):
        body = raw.strip()
        if body and body in skipped:
            if present is None:
                present = {line.strip() for line in updated.split("\n")}
            if body not in present:
                return skipped[body]
    return None


def _old_in(text, old):
    # old_string 在 text 里对得上(原样,或去空白整块;不唯一也算)。
    return old in text or _fuzzy_line_span(text, old) is not None
"""


# ---------------------------------------------------------------------------
# Host side — the text the model reads.
# ---------------------------------------------------------------------------

_VERB = {"no_match": "did not match", "ambiguous": "matched more than one place"}


def _numbers(items: Sequence[int]) -> str:
    return ", ".join(str(k) for k in items)


def streak_text(failures: int, *, read: bool) -> str | None:
    """Spec §4.3.4 —— 同一文件第 ``failures`` 次连续失败时排在最前面的那句。

    不到第 3 次为 ``None``。"""
    if failures < STREAK_NOTICE_FROM:
        return None
    if read:
        return (
            f"This is failure {failures} of edit_file on this file in a row, even after "
            "reading it. Rewrite the whole file with write_file instead of editing it."
        )
    return (
        f"This is failure {failures} of edit_file on this file in a row. Do not send another "
        "edit built from memory: call read_file on this path and copy 'old_string' from what "
        "it returns, or rewrite the whole file with write_file."
    )


def _report_header(env: Mapping[str, Any], report: Mapping[str, Any]) -> str:
    """Spec §4.3.1 —— 多处清单失败时的第一段:哪一处、什么都没写、哪些本来能对上 / 没法判断。"""
    index, total, kind = env.get("index"), env.get("total"), str(env.get("error"))
    parts = [
        f"edit_file failed: edit {index} of {total} {_VERB.get(kind, 'failed')} ({kind}); "
        "NOTHING was written."
    ]
    failed = [(int(k), str(e)) for k, e in _pairs(report.get("failed")) if int(k) != index]
    for other_kind in sorted({e for _k, e in failed}):
        others = [k for k, e in failed if e == other_kind]
        noun = "Edits" if len(others) > 1 else "Edit"
        verb = _VERB.get(other_kind, "failed")
        parts.append(f"{noun} {_numbers(others)} also {verb} ({other_kind}).")
    applied = [int(k) for k in report.get("applied") or [] if isinstance(k, int)]
    unchecked = [(int(k), int(on)) for k, on in _pairs(report.get("unchecked"))]
    clauses = []
    if applied:
        noun = "Edits" if len(applied) > 1 else "Edit"
        clauses.append(f"{noun} {_numbers(applied)} would have applied")
    if unchecked:
        ons = sorted({on for _k, on in unchecked})
        subject = (
            f"edit {unchecked[0][0]} could not be checked because it depends"
            if len(unchecked) == 1
            else f"edits {_numbers([k for k, _on in unchecked])} could not be checked "
            "because they depend"
        )
        clauses.append(f"{subject} on edit {_numbers(ons)}")
    if clauses:
        parts.append("; ".join(clauses) + ".")
    if failed:
        parts.append("Resend them together with corrected versions of the edits that failed.")
    else:
        parts.append(f"Resend them together with a corrected edit {index}.")
    return " ".join(parts)


def _pairs(raw: object) -> list[tuple[Any, Any]]:
    if not isinstance(raw, list):
        return []
    return [
        (item[0], item[1])
        for item in raw
        if isinstance(item, list | tuple) and len(item) == 2 and isinstance(item[0], int)
    ]


def edit_failure_text(
    env: Mapping[str, Any],
    *,
    guidance: str | None,
    streak: str | None,
) -> str:
    """一次失败的 ``edit_file`` 给模型的整段文字(spec §4.3.6 的顺序)。

    「连续失败那句」(若有)排在最前面(§4.3.4「错误文字最前面」);随后是「哪一处、什么都
    没写、哪些本来能对上 / 没法判断」→ 相似行 → 本轮自己改掉的旧文字 → 原有引导。超长时被
    截掉的永远是最后的引导。

    有「连续失败那句」时它**取代**原有引导:那句本身就是下一步(读过还失败时只要求整篇
    重写,§4.3.4「不再反复说 read_file」—— 而原有引导开口就是 read_file)。

    没有 ``report``(单处改动,或旧形状的 envelope)时第一段与 B-137 逐字相同:
    ``edit_file failed: [edit i of n: ]kind (detail)``。
    """
    kind = env.get("error", "unknown")
    detail = env.get("detail")
    report = env.get("report")
    if isinstance(report, Mapping):
        head = _report_header(env, report)
        if detail:
            head = f"{head} Edit {env.get('index')}: {detail}."
    else:
        where = f"edit {env['index']} of {env['total']}: " if env.get("index") else ""
        head = f"edit_file failed: {where}{kind}" + (f" ({detail})" if detail else "")
    parts = [p for p in (streak, head, env.get("own_edit"), None if streak else guidance) if p]
    return " ".join(str(p) for p in parts)


def already_applied_text(entries: object) -> str:
    """Spec §4.3.3 —— 成功结果里每一处「已改过」的说明;没有时为空串。"""
    if not isinstance(entries, list):
        return ""
    lines = [
        f"edit {item['index']}: its new text is already in the file at line {item['line']} "
        "and its old text is not — counted as applied; do not send it again."
        for item in entries
        if isinstance(item, Mapping) and "index" in item and "line" in item
    ]
    return " ".join(lines)
