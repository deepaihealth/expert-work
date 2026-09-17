"""Stream CM-2 — working-memory sliding window (cheap pre-compressor gate).

The agent_node preflight currently has exactly one compression gate: the
LLM-backed :class:`~orchestrator.context.compressor.ContextCompressor`,
which summarises the middle of an overflowing conversation. Every light
overflow therefore pays for a summariser LLM call. This module adds a
**cheap, LLM-free** first gate that runs *before* the compressor: when the
estimated prompt is over threshold it trims the conversation down to the
first turn plus the most-recent ``max_recent_turns`` user turns. Most light
overflows are resolved here, so the compressor (the second gate) only runs
on what is still too large after trimming. Maps the framework report's A2
(OpenClaw ``limitHistoryTurns``).

Design anchors (STREAM-CM-DESIGN §4):

* **CM-C1 — token-gated.** Trimming only happens when the estimate is at or
  over threshold; under threshold the window is a no-op, so a conversation
  that is not overflowing sees an unchanged prompt (zero behaviour change).
* **CM-C2 — cut on HumanMessage boundaries.** A ``HumanMessage`` always
  starts a fresh turn and never sits between an ``AIMessage`` tool_use and
  its ``ToolMessage`` tool_result, so slicing the suffix from a
  ``HumanMessage`` index can never split a tool-call pair (the OpenClaw
  #1084 hard constraint). No pair-repair logic is needed — cutting at the
  right boundary is the guarantee.
* **CM-C3 — keep the first turn.** Mirroring the compressor's head/tail
  philosophy, the original task turn is a cheap goal anchor kept alongside
  the recent window (complementing, not depending on, CM-0 recitation).
* **CM-C4 — prompt-view only.** Like the compressor, trimming only shapes
  the message list handed to *this* LLM call; ``agent_node`` never rewrites
  the checkpointed history, so dropped middle turns are not lost — the next
  turn reloads the full history from the checkpoint and trims afresh.
* **CM-C6 — reuse the compressor's estimator.** The token gate reuses
  :func:`~orchestrator.context.compressor.estimate_tokens` and the
  leading-SystemMessage split, keeping one estimation basis and never
  touching ``AgentState`` / ``ToolResult`` / ``ToolSpec``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from expert_work.common.conversation_channel import is_hidden
from expert_work.runtime.tokens import TokenEstimator
from orchestrator.context.compressor import estimate_tokens


@dataclass(frozen=True)
class TrimResult:
    """Outcome of a window pass — the (possibly trimmed) prompt view plus
    how many user turns were dropped (``0`` ⇒ no-op)."""

    messages: list[BaseMessage]
    dropped_turns: int


def _split_leading_systems(
    messages: Sequence[BaseMessage],
) -> tuple[list[BaseMessage], list[BaseMessage]]:
    """Split off the frozen leading :class:`SystemMessage` prefix.

    Mirrors :func:`orchestrator.context.compressor._split`'s handling: the
    L1 prompt-cache invariant requires the leading system block to stay
    byte-stable, so the window never trims it.
    """
    cursor = 0
    while cursor < len(messages) and isinstance(messages[cursor], SystemMessage):
        cursor += 1
    return list(messages[:cursor]), list(messages[cursor:])


def trim_to_recent_turns(
    messages: Sequence[BaseMessage],
    *,
    max_recent_turns: int,
    keep_first_turn: bool,
) -> TrimResult:
    """Trim ``messages`` to the first turn plus the most-recent N turns.

    Token-unaware: callers gate on size via :meth:`WorkingWindow.should_trim`.
    A "turn" is the span a non-hidden ``HumanMessage`` opens. Cutting only on
    ``HumanMessage`` indices preserves every ToolCall↔ToolResult pair
    (CM-C2). Returns a new list — never mutates the input.

    Hidden ``HumanMessage`` s (B-67 inputs block right after the user message,
    recovery advisories, delegation nudges) are scaffolding inside the turn
    they sit in: they never open a turn, so a cut never separates a user
    message from the block after it, and only user turns count toward
    ``max_recent_turns`` / ``dropped_turns``.

    No-ops (``dropped_turns == 0``, original list returned) when there is no
    turn boundary to cut on (no non-hidden ``HumanMessage``) or the
    conversation already fits within the budget.
    """
    msgs = list(messages)
    leading, remainder = _split_leading_systems(msgs)
    human_idxs = [
        i for i, m in enumerate(remainder) if isinstance(m, HumanMessage) and not is_hidden(m)
    ]
    total_turns = len(human_idxs)
    # No turn boundary, or already within budget → nothing safe/needed to cut.
    if total_turns <= max_recent_turns:
        return TrimResult(messages=msgs, dropped_turns=0)

    # Index in ``remainder`` where the most-recent-N-turns window begins.
    window_start = human_idxs[total_turns - max_recent_turns]
    first_turn_start = human_idxs[0]

    if not keep_first_turn:
        kept = remainder[window_start:]
        return TrimResult(
            messages=[*leading, *kept],
            dropped_turns=total_turns - max_recent_turns,
        )

    # keep_first_turn: the first turn spans [first_turn_start, second_human).
    first_turn_end = human_idxs[1]
    if first_turn_end >= window_start:
        # The recent window already reaches back into (or up to) the first
        # turn — keeping from the first turn onward keeps every turn, so
        # there is nothing to drop and no separate splice to make.
        return TrimResult(messages=[*leading, *remainder[first_turn_start:]], dropped_turns=0)

    first_turn = remainder[first_turn_start:first_turn_end]
    window = remainder[window_start:]
    # First turn kept + last N kept ⇒ the turns strictly between are dropped.
    return TrimResult(
        messages=[*leading, *first_turn, *window],
        dropped_turns=total_turns - max_recent_turns - 1,
    )


@dataclass(frozen=True)
class WorkingWindow:
    """Token-gated sliding window over the working memory (conversation).

    Build one per agent (the factory wires
    ``policies.working_memory`` into this) and pass to
    :func:`~orchestrator.graph_builder.builder.build_react_graph` so
    ``agent_node`` can call :meth:`apply` right after loading the history,
    *before* plan/memory/advisory injection and the compressor preflight.
    """

    context_window: int
    threshold_pct: float = 0.7
    max_recent_turns: int = 20
    keep_first_turn: bool = True
    #: Stream HX-1 (Mini-ADR HX-A1) — injected token estimator. ``None``
    #: keeps the legacy ``chars // 4`` heuristic; the factory injects the
    #: shared tiktoken-backed estimator (same instance as the compressor
    #: so the two gates keep one estimation basis — CM-C6).
    estimator: TokenEstimator | None = None

    @property
    def threshold_tokens(self) -> int:
        """Token threshold at/above which a pass trims. Same shape as the
        compressor's threshold so the two gates share one estimation basis."""
        return int(self.context_window * self.threshold_pct)

    def should_trim(self, messages: Sequence[BaseMessage]) -> bool:
        """Cheap preflight — ``True`` when the estimate meets/exceeds the
        threshold. Reuses the compressor's estimator basis (CM-C6)."""
        return estimate_tokens(messages, estimator=self.estimator) >= self.threshold_tokens

    def apply(self, messages: Sequence[BaseMessage]) -> TrimResult:
        """Trim to the recent window when over threshold, else no-op.

        Returns a :class:`TrimResult`; ``dropped_turns == 0`` means the
        prompt was left untouched (under threshold, no turn boundary, or
        already within budget).
        """
        msgs = list(messages)
        if not self.should_trim(msgs):
            return TrimResult(messages=msgs, dropped_turns=0)
        return trim_to_recent_turns(
            msgs,
            max_recent_turns=self.max_recent_turns,
            keep_first_turn=self.keep_first_turn,
        )
