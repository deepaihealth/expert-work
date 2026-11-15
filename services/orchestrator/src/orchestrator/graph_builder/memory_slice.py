"""B-300 重做 PR S —— 增量切片(B-176 M2-1,设计 §8.1、§8.2):一次写回提炼哪些轮次、给几行上下文、
超长时丢哪些、重试时怎么分块。

输入是整段对话按轮切好、每行已编好整段对话里的消息序号的 :class:`Turn`(在
``graph_builder.memory._extraction_inputs`` 里建)—— 先整段编号、再按起点过滤,证据指针的序号才对。
这里只做选择,不调模型。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from orchestrator.graph_builder.memory_lines import ASSISTANT_TAG, ExtractionLine, render_lines

#: 第二次的上下文:新段之前最近几行(不够时往前补到含一条 ``[assistant]``)。
CONVERSATION_CONTEXT_LINES = 4
#: 第一次的上下文:新段之前最近几条用户输入。
USER_CONTEXT_LINES = 2


@dataclass(frozen=True)
class SegmentStart:
    """本次写回从哪里提炼起(§8.1)。

    * ``message_index``:开轮消息在整段对话里的序号 ≥ 它的轮是新段 —— 后台任务取同一对话前一个任务的
      ``message_count``;本轮内写回取本轮的开轮消息。一轮从它之前开始、在它之后结束(起点落在一轮
      中间)→ 那一轮不进新段、计数(它属于前一个任务)。
    * ``opened_since``:没有更早的任务 —— 开轮消息的写入时间(``expert_work_created_at``)不早于它的
      轮是新段;没有这个时间戳的算更早的。

    两样都没有 = 整段都是新段(内联写回模式,逃生口,§8.1 末)。"""

    message_index: int | None = None
    opened_since: datetime | None = None


@dataclass(frozen=True)
class Turn:
    """一轮在两次提炼里的行(编号前),以及它在整段对话里的位置与开轮时间。"""

    user: tuple[ExtractionLine, ...]
    conversation: tuple[ExtractionLine, ...]
    has_reply: bool
    first_index: int
    last_index: int
    opened_at: datetime | None
    #: 被截到每条上限的消息数(两次各自的行)。
    user_truncated: int = 0
    conversation_truncated: int = 0


@dataclass(frozen=True)
class Chunk:
    """一次提炼调用的输入:上下文行(``context=True``)在前,本块的轮次在后。"""

    user: tuple[ExtractionLine, ...]
    conversation: tuple[ExtractionLine, ...]
    turns: tuple[Turn, ...]

    @property
    def has_new_user_text(self) -> bool:
        return any(not line.context for line in self.user)

    @property
    def has_reply(self) -> bool:
        return any(turn.has_reply for turn in self.turns)


@dataclass(frozen=True)
class SlicePlan:
    """切片的结果与要计数的事。``chunks`` 为空 = 本段为空,不调模型。"""

    chunks: tuple[Chunk, ...]
    straddling: int = 0
    dropped_over_cap: int = 0
    halved: bool = False


def _is_new(turn: Turn, start: SegmentStart) -> bool:
    if start.message_index is not None:
        return turn.first_index >= start.message_index
    if start.opened_since is not None:
        return turn.opened_at is not None and turn.opened_at >= start.opened_since
    return True


def _straddles(turn: Turn, start: SegmentStart) -> bool:
    index = start.message_index
    return index is not None and turn.first_index < index <= turn.last_index


def _cap(turns: Sequence[Turn], new: Sequence[Turn], cap: int) -> list[Turn]:
    """§8.2 第 4 步:按整轮从最近的往前取,第二次提炼的完整输入(上下文 + 这些轮次)不超过 ``cap``;
    最近的一轮总是留下(单独一轮不会超:每条消息各截到 1,000 字)。"""
    kept: list[Turn] = []
    for turn in reversed(new):
        candidate = [turn, *kept]
        if kept and len(render_lines(_chunk(turns, candidate).conversation)) > cap:
            break
        kept = candidate
    return kept


def _as_context(lines: Sequence[ExtractionLine]) -> tuple[ExtractionLine, ...]:
    return tuple(replace(line, context=True) for line in lines)


def _context(
    earlier: Sequence[Turn],
) -> tuple[tuple[ExtractionLine, ...], tuple[ExtractionLine, ...]]:
    """§8.2 第 3 步:第二次给最近 4 行,其中没有 ``[assistant]`` 就往前补到最近的一条
    ``[assistant]``(短回答的问题常在上一轮末尾);第一次给最近 2 条用户输入(只有用户的字)。"""
    conversation = [line for turn in earlier for line in turn.conversation]
    taken = conversation[-CONVERSATION_CONTEXT_LINES:]
    if not any(line.tag == ASSISTANT_TAG for line in taken):
        replies = [i for i, line in enumerate(conversation) if line.tag == ASSISTANT_TAG]
        if replies:
            taken = conversation[replies[-1] :]
    users = [line for turn in earlier for line in turn.user][-USER_CONTEXT_LINES:]
    return _as_context(users), _as_context(taken)


def _chunk(turns: Sequence[Turn], new: Sequence[Turn]) -> Chunk:
    position = next(i for i, turn in enumerate(turns) if turn is new[0])
    user_context, conversation_context = _context(turns[:position])
    return Chunk(
        user=user_context + tuple(line for turn in new for line in turn.user),
        conversation=conversation_context
        + tuple(line for turn in new for line in turn.conversation),
        turns=tuple(new),
    )


def plan_slice(
    turns: Sequence[Turn], start: SegmentStart | None, *, cap: int, halves: bool
) -> SlicePlan:
    """新段 → 上限 → (第 2 次起的尝试)按整轮对半切成至多两块,每块带自己的上下文。"""
    start = start or SegmentStart()
    new = [turn for turn in turns if _is_new(turn, start)]
    straddling = sum(1 for turn in turns if _straddles(turn, start))
    kept = _cap(turns, new, cap)
    dropped = len(new) - len(kept)
    if not kept:
        return SlicePlan(chunks=(), straddling=straddling)
    split = halves and len(kept) > 1
    middle = (len(kept) + 1) // 2
    parts = [kept[:middle], kept[middle:]] if split else [kept]
    return SlicePlan(
        chunks=tuple(_chunk(turns, part) for part in parts),
        straddling=straddling,
        dropped_over_cap=dropped,
        halved=split,
    )


__all__ = ["Chunk", "SegmentStart", "SlicePlan", "Turn", "plan_slice"]
