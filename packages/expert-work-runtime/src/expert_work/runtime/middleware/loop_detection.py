"""Loop-detection middleware — Stream E.10.5.

Registered to ``after_llm_call``. Walks the recent message history
and abort-rewrites the LLM's response when it has emitted the **same
tool call** three turns in a row — the classic "I tried, got an
error, let me try the exact same thing again" failure mode.

Complementary to :class:`AgentState.max_steps`: the step cap is a
runaway-guard ceiling (20 by default); loop detection is the **early
abort** that prevents 20 wasted iterations of the same buggy call
burning tokens. Modelled after deer-flow's
``loop_detection_middleware.py``.

On detection:

1. The most recent ``AIMessage`` is **rewritten with empty
   ``tool_calls``** (preserving its id so LangGraph's ``add_messages``
   reducer replaces in-place — no duplicate ladder accumulating in
   the checkpoint).
2. A ``<system-reminder>`` :class:`HumanMessage` is appended telling
   the LLM the loop was detected so it switches to a final answer or
   different parameters.

Both rewrites go into ``ctx.payload["messages"]``; the orchestrator's
post-chain integration (E.11 LLMRouter) merges them back into the
LangGraph state.

See [STREAM-E-DESIGN Mini-ADR E-11](../../../../../../../docs/streams/STREAM-E-DESIGN.md)
for the choice of normalised-args + sha256 fingerprint over raw
``json.dumps`` comparison — LLMs frequently retry with whitespace /
case / key-order differences that wouldn't match string-equal.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from expert_work.runtime.middleware.base import CallNext, MiddlewareContext

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_SIZE = 3
DEFAULT_REMINDER_TEXT = (
    "<system-reminder>Loop detected: the same tool call has been "
    "emitted 3 times in a row. Give a final answer or call a "
    "different tool / arguments instead.</system-reminder>"
)
_FINGERPRINT_BYTES = 16


def normalize_args(value: object) -> object:
    """Recursively normalise tool-call args for fingerprint comparison.

    - ``dict``: sort keys, normalise each value.
    - ``list`` / ``tuple``: preserve order (semantically meaningful —
      ``["a", "b"]`` is a different call from ``["b", "a"]`` for most
      tools), but normalise each element.
    - ``str``: ``.strip()`` then ``.lower()`` so the LLM retrying with
      ``"/Tmp"`` vs ``"/tmp"`` or ``"foo"`` vs ``" foo "`` is treated
      as the same call.
    - other scalars: pass through.

    Trade-off (Mini-ADR E-11): collapsing case + whitespace can
    over-match on legitimately case-sensitive args (e.g. Linux paths
    with different case). The LLM-retry-loop scenario is the more
    common failure mode in dogfood, so we accept the collision risk
    in exchange for catching it.
    """
    if isinstance(value, Mapping):
        return {str(k): normalize_args(v) for k, v in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [normalize_args(item) for item in value]
    if isinstance(value, str):
        return value.strip().lower()
    return value


def fingerprint_tool_calls(tool_calls: Sequence[Mapping[str, Any]]) -> str:
    """Return a stable 16-byte hex fingerprint of a ``tool_calls`` list.

    Empty list → empty string (caller treats as "no fingerprint, no
    loop"). Multi-tool-call AIMessages are still fingerprinted; the
    set of (name, normalised-args) is sorted by name so call order
    within one message doesn't matter.
    """
    if not tool_calls:
        return ""
    normalised: list[tuple[str, object]] = []
    for tc in tool_calls:
        name = str(tc.get("name", ""))
        args = tc.get("args") or {}
        normalised.append((name, normalize_args(args)))
    normalised.sort(key=lambda pair: pair[0])
    blob = json.dumps(normalised, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[: _FINGERPRINT_BYTES * 2]


def clone_ai_message_with_tool_calls(
    message: AIMessage,
    tool_calls: Sequence[Mapping[str, Any]],
) -> AIMessage:
    """Return a clone of ``message`` with ``tool_calls`` replaced, preserving id.

    Following deer-flow's ``clone_ai_message_with_tool_calls`` pattern:
    rewriting the SAME-id ``AIMessage`` lets LangGraph's
    ``add_messages`` reducer **replace** the original in the
    checkpoint rather than appending. Without id preservation a
    cleared-tool-calls clone would stack on top of the original, and
    the LLM would see both — defeating the point.

    Also clears any raw-provider ``tool_calls`` payload that some
    LangChain providers stash in ``additional_kwargs`` so the two
    stay in sync (a half-cleared AIMessage confuses some providers'
    "is this still a tool-calling turn?" check).
    """
    additional_kwargs = dict(getattr(message, "additional_kwargs", {}) or {})
    if "tool_calls" in additional_kwargs:
        additional_kwargs["tool_calls"] = []
    return message.model_copy(
        update={
            "tool_calls": list(tool_calls),
            "additional_kwargs": additional_kwargs,
        }
    )


@dataclass
class LoopDetectionMiddleware:
    """Detect identical tool-call loops and abort with a system reminder."""

    window_size: int = DEFAULT_WINDOW_SIZE
    reminder_text: str = DEFAULT_REMINDER_TEXT

    name: str = "loop_detection"
    anchor: str = "after_llm_call"
    after: tuple[str, ...] = field(default_factory=tuple)
    before: tuple[str, ...] = field(default_factory=tuple)

    async def __call__(self, ctx: MiddlewareContext, call_next: CallNext) -> None:
        # Let the rest of the after_llm_call chain run first (langfuse
        # span recording etc.); we only need to inspect the final state.
        await call_next(ctx)

        raw_messages = ctx.payload.get("messages")
        if not isinstance(raw_messages, list):
            return

        recent = self._recent_ai_with_tool_calls(raw_messages)
        if len(recent) < self.window_size:
            return

        first_fingerprint = fingerprint_tool_calls(recent[0].tool_calls)
        if not first_fingerprint:
            return
        if any(fingerprint_tool_calls(m.tool_calls) != first_fingerprint for m in recent[1:]):
            return

        logger.warning(
            "loop_detection.tripped fingerprint=%s window=%d",
            first_fingerprint,
            self.window_size,
        )
        latest = recent[0]
        cleaned = clone_ai_message_with_tool_calls(latest, tool_calls=[])
        # Not a real user turn — without this the system-reminder renders as a
        # user chat bubble, gets forwarded to third parties reading the
        # transcript, and throws off buildHistoryTurns' user/assistant
        # order-pairing (spec 2026-07-30, Important#2).
        reminder = HumanMessage(
            content=self.reminder_text,
            additional_kwargs={"expert_work_hide_from_ui": True},
        )
        # Same-id ``cleaned`` → replace in-place via add_messages; the
        # reminder has a fresh auto-id → append.
        ctx.payload["messages"] = [cleaned, reminder]
        # Stream CM-9 (Mini-ADR CM-J5) — flag the trip for the caller so
        # the next agent step can escalate to the higher-effort caller.
        ctx.payload["loop_detected"] = True

    def _recent_ai_with_tool_calls(
        self,
        messages: Sequence[BaseMessage],
    ) -> list[AIMessage]:
        """Return up to ``window_size`` most-recent AIMessages with non-empty
        ``tool_calls``, newest first."""
        collected: list[AIMessage] = []
        for message in reversed(messages):
            if isinstance(message, AIMessage) and message.tool_calls:
                collected.append(message)
                if len(collected) >= self.window_size:
                    break
        return collected
