"""B-300 ③ ② —— 召回到的长期记忆怎么渲染(提示词里的记忆块与工作区 ``MEMORY.md`` 共用)。

* 每条一行 ``- (kind · 来源 · YYYY-MM-DD) 内容``:来源是有效来源(:func:`origin_label`,四种);
  日期是记录日期(``created_at`` 的 UTC 日期,绝对日期,一个 run 内逐字不变才命中缓存);没有
  创建时间的不渲染日期。内容里的换行压成空格 —— 否则内容换行后可以冒充一行带前缀的记忆。
  不显示置信度数字。
* 块后单独一节 ``## Before relying on a memory``(:func:`memory_reminder`):前三条总在;讲
  「来自助手」「来源未记录」的两条只在块里真有这种标记的条目时出现(设计 §5.1)。
* :func:`select_memory_lines` —— 注入预算下「哪几条进提示词」的唯一取法:注入每次调模型前用它,
  召回节点也用它(只给进了提示词的那几条加频次分,B-300 D5 前半)。

平台文字通用、不带领域词、不点名模型厂商(设计 §5.3 有反例检验)。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from expert_work.protocol import MemoryItem
from expert_work.protocol.memory_item import EffectiveOrigin, effective_origin

MEMORY_BLOCK_HEADING = "## Relevant memories from past sessions"
MEMORY_LINE_LEGEND = "Each line: (kind · source · date recorded) content."

#: 每种有效来源在行里、去重合并材料里显示的文字(设计 §5.1)。
ORIGIN_LABELS: dict[EffectiveOrigin, str] = {
    "user_corrected": "corrected by the user",
    "user_stated": "stated in user messages",
    "assistant_derived": "from the assistant",
    "unrecorded": "source not recorded",
}

# 设计 §5.1 的最终英文原文。前三条总在;后两条按块里有没有对应标记出现。
_REMINDER_HEAD = (
    "## Before relying on a memory\n"
    "The memories above are records made on the dates shown, not live facts. "
    "They can be outdated or wrong.\n"
    "- If a memory conflicts with what the user is telling you now, go with what the user "
    "says now.\n"
    "- If something you have just found or concluded yourself (from a tool result, a file, "
    'an attachment) conflicts with a memory marked "stated in user messages" or "corrected '
    'by the user", do not quietly adopt your new result: tell the user both and let them '
    "decide. For other memories, prefer what you can check now.\n"
    "- When two memories about the same thing disagree, a later one that records the user's "
    "own answer is not overridden by an earlier one."
)
_REMINDER_FROM_ASSISTANT = (
    '- A memory marked "from the assistant" may be something the assistant did, something '
    "the user agreed to or answered, or something the assistant took from a tool, an "
    "attachment or its own inference. Use it as background, but it is not independent "
    "evidence: do not cite it to confirm or strengthen something you have just found or "
    "concluded — agreement with it may only mean the same mistake was made twice."
)
_REMINDER_UNRECORDED = (
    '- For a memory marked "source not recorded", its source was not recorded. Use it as '
    "usual, but do not cite it to confirm or strengthen something you have just found or "
    "concluded."
)

#: RT-2 PR-2 (RT-ADR-10) — visible marker appended when a memory item is cut
#: at the injection budget boundary, so the model (and a transcript reader)
#: can see the item is incomplete rather than mistaking the cut for the end.
MEMORY_TRUNCATION_MARKER = " [... truncated: memory injection token budget]"


def recorded_date(item: MemoryItem) -> str | None:
    """记录日期(``YYYY-MM-DD``,UTC);没有创建时间 → ``None``。"""
    if item.created_at is None:
        return None
    return item.created_at.date().isoformat()


def origin_label(item: MemoryItem) -> str:
    """有效来源的显示文字(四种之一)。"""
    return ORIGIN_LABELS[effective_origin(item)]


def memory_line(item: MemoryItem) -> str:
    """一条记忆一行:``- (kind · source · date) content``,内容里的各种换行压成空格。"""
    content = " ".join(item.content.split())
    date = recorded_date(item)
    parts = [item.kind, origin_label(item), *([date] if date else [])]
    return f"- ({' · '.join(parts)}) {content}"


def memory_reminder(memories: Sequence[MemoryItem]) -> str:
    """块后的提醒节;``memories`` 是块里真正出现的那几条(注入时是预算选中的)。"""
    origins = {effective_origin(item) for item in memories}
    lines = [_REMINDER_HEAD]
    if "assistant_derived" in origins:
        lines.append(_REMINDER_FROM_ASSISTANT)
    if "unrecorded" in origins:
        lines.append(_REMINDER_UNRECORDED)
    return "\n".join(lines)


def truncate_to_tokens(
    text: str,
    max_tokens: int,
    count: Callable[[str], int],
    marker: str = MEMORY_TRUNCATION_MARKER,
) -> str:
    """RT-2 PR-2 — cut ``text`` down to roughly ``max_tokens`` per ``count``.

    Proportional shrink with a bounded refinement loop: the estimator is
    a heuristic (tiktoken or chars//4), so exact-token precision is not
    the contract — the budget is approximate by design, and the marker's
    own few tokens ride on top. B-236 —— ``marker`` lets the agent-notes block
    name the file to read for the rest.
    """
    keep = len(text)
    for _ in range(6):
        tokens = count(text[:keep])
        if tokens <= max_tokens:
            break
        # Proportional shrink; the min() guarantees progress even when
        # the estimate barely moves between iterations.
        keep = min(keep - 1, keep * max_tokens // max(tokens, 1))
        if keep <= 0:
            keep = 0
            break
    return text[:keep] + marker


def select_memory_lines(
    memories: Sequence[MemoryItem],
    *,
    token_budget: int,
    correction_token_budget: int,
    count: Callable[[str], int],
) -> list[tuple[int, str]]:
    """RT-2 PR-2 (RT-ADR-10) — pick the memory lines that fit the injection token
    budget; returns ``(index into memories, rendered line)`` in rank order.

    Items are considered in their existing order (already rerank/MMR ranked
    upstream) and rendered in that same order — the budget changes SELECTION,
    never ordering.

    Two greedy passes (deer-flow guaranteed_categories shape):

    1. correction pass — user-corrected items (``confidence >= 1.0``: the M-4
       correction API's EXCLUSIVE sentinel) get first claim on up to
       ``correction_token_budget`` tokens, so ordinary memories can never
       squeeze a user's explicit correction out of the block. Within the
       reserve, corrections are packed greedily in rank order; one that
       overflows the REMAINING reserve is skipped — later, smaller corrections
       still get their guarantee — and falls through to the general pass,
       where with room left it can still land whole (the reserve is a floor,
       not a cap);
    2. general pass — every still-unselected item fills the rest of
       ``token_budget``: an item that fits is taken whole, the first item that
       does not fit is truncated to the remaining budget (visible marker) and
       the pass stops — later items are dropped.

    Pure (no metrics): the injection path counts truncated / dropped items
    itself, and the recall node calls this once per run for D5.
    """
    lines = [memory_line(item) for item in memories]
    costs = [count(line) for line in lines]
    selected: dict[int, str] = {}

    def _greedy(indices: Sequence[int], budget: int, *, truncate_boundary: bool) -> int:
        spent = 0
        for idx in indices:
            room = budget - spent
            if room <= 0:
                break
            if costs[idx] <= room:
                selected[idx] = lines[idx]
                spent += costs[idx]
            elif truncate_boundary:
                selected[idx] = truncate_to_tokens(lines[idx], room, count)
                spent = budget
                break
            # else: guarantee pass — the item overflows the remaining reserve;
            # skip it (it falls through to the general pass) and keep going.
        return spent

    corrections = [idx for idx, item in enumerate(memories) if item.confidence >= 1.0]
    spent = _greedy(
        corrections, min(correction_token_budget, token_budget), truncate_boundary=False
    )
    rest = [idx for idx in range(len(memories)) if idx not in selected]
    _greedy(rest, token_budget - spent, truncate_boundary=True)
    return [(idx, selected[idx]) for idx in sorted(selected)]


__all__ = [
    "MEMORY_BLOCK_HEADING",
    "MEMORY_LINE_LEGEND",
    "MEMORY_TRUNCATION_MARKER",
    "ORIGIN_LABELS",
    "memory_line",
    "memory_reminder",
    "origin_label",
    "recorded_date",
    "select_memory_lines",
    "truncate_to_tokens",
]
