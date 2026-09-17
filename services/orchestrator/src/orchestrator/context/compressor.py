"""Stream L.L2 — :class:`ContextCompressor` + token preflight.

Implements Hermes ``context_compressor.py:454-600``'s "summarise the
middle" pattern, scoped down to a single-pass-per-turn API. The
:func:`agent_node` preflight calls :func:`ContextCompressor.compress`
when the estimated outbound prompt size exceeds
``context_window * threshold_pct``; the compressor preserves the
first ``head_keep`` and last ``tail_keep`` non-system messages and
collapses the middle into a single ``<context-summary>`` system
message generated via an LLM call.

Mini-ADR L-2 highlights:

* **One-shot per turn** — we don't keep a running summary across
  compressions (Hermes "iterative summary preservation"). Each
  compression starts fresh from the current message list; if the
  conversation grows large enough to need compression repeatedly the
  individual passes are cheap, and the result is easier to reason
  about than a self-feeding summary.
* **Independent summariser LLM call** — the compressor takes its own
  :class:`LLMCaller`. The agent's main router may go through the
  same caller, but the contract is "summarise this, return one
  message" rather than "act as the agent" so a future hop to a
  dedicated cheaper model is a one-field change.
* **Hard cap via ``max_passes``** — if successive summarisations
  cannot bring the estimated size below threshold the compressor
  raises :class:`ContextOverflowError`. Hiding the failure behind a
  silent fallback would let the run keep ballooning until the
  upstream rejects it; the explicit signal lets the orchestrator log
  a clean run-failed audit.
* **Transient summariser failure skips, not fails (RT-ADR-6)** — a
  summariser LLM exception no longer fails the run on the spot: the
  round returns the messages uncompressed (metric + warning log) and
  the next turn retries. Only ``_MAX_CONSECUTIVE_SUMMARY_FAILURES``
  consecutive failed rounds of the SAME conversation (the streak is
  keyed by the caller-supplied ``streak_key`` — thread_id — because a
  compressor instance is shared across conversations) escalate to
  :class:`ContextOverflowError`; an empty middle that is still over
  threshold stays fail-hard (nothing left to summarise).
* **Summariser prompt budget (RT-ADR-10)** — the transcript handed to
  the summariser is itself bounded: a per-message char cap plus a
  total hard budget (halved between PREVIOUS SUMMARY and NEW EVENTS
  in update mode), so a single oversized tool dump cannot blow up the
  summariser call. Truncation keeps the head (2/3) and tail of each
  slice around a visible elision marker (deer-flow #3887
  ``_bound_text`` shape).
* **Skill reads become references (RT-ADR-7)** — a successful
  lazy-skill read (``ToolMessage(name="skill_view")``) in the middle
  enters the summariser transcript as a one-line skill reference
  (name + source path, no tool-call syntax — the summariser prompt
  forbids it in the output) instead of its up-to-20k body: the
  summary keeps the skill NAME while the input budget is spent on
  the actual conversation; the re-read handle itself is provided by
  the available-skills list in the system prompt and the CM-12
  pruner stub.
* **Rough char-based estimator** — ``estimate_tokens`` returns
  ``total_chars // 4``. Cheaper than tiktoken (no dependency, no
  per-message tokeniser call) and Hermes uses the same rule of
  thumb. The 4-chars-per-token heuristic is conservative for English
  / code, slightly aggressive for CJK; the threshold ratio gives us
  enough headroom to absorb the difference without ratcheting up
  cost.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from expert_work.common.conversation_channel import is_hidden
from expert_work.common.observability import (
    ExpertWorkComponent,
    expert_work_counter,
    expert_work_span,
)
from expert_work.runtime.cancellation import RunCancelledError
from expert_work.runtime.tokens import TokenEstimator, estimate_messages, flatten_message
from orchestrator.context.skill_reference import skill_view_reference
from orchestrator.llm.caller import LLMCaller

logger = logging.getLogger(__name__)

# Stream RT-2 PR-1 — compressor observability: passes completed,
# summariser failures, and rounds skipped after a transient failure
# (RT-ADR-6). Full COMPACTION eventing lands in RT-2 PR-4; these
# counters make the failure semantics observable now.
_cm_compressor_pass_total = expert_work_counter(
    "expert_work_cm_compressor_pass_total",
    "Summarise-the-middle compressor passes completed (Stream L.L2 / RT-2).",
)
_cm_compressor_summary_failure_total = expert_work_counter(
    "expert_work_cm_compressor_summary_failure_total",
    "Summariser LLM failures during a compression round (RT-ADR-6).",
)
_cm_compressor_skipped_total = expert_work_counter(
    "expert_work_cm_compressor_skipped_total",
    "Compression rounds skipped after a transient summariser failure (RT-ADR-6).",
)
# Stream RT-2 PR-4 — compaction magnitude. The pass/skip counters say WHEN
# compaction fires; this counter says HOW MUCH it reclaims (summed estimated
# tokens the middle-summarisation removed from the prompt), so a dashboard can
# quantify the context/cost savings across all runs. A histogram would be the
# natural shape, but ``expert_work_histogram`` requires the ``_seconds`` suffix
# (duration-only by contract), so a monotonic counter of tokens saved is the
# name-rule-compatible choice.
_cm_compressor_tokens_saved_total = expert_work_counter(
    "expert_work_cm_compressor_tokens_saved_total",
    "Estimated prompt tokens reclaimed by middle-summarisation (Stream RT-2).",
)

#: Stream CM-3 — pre-compaction hook. Awaited with the middle slice that is
#: about to be summarised away, *before* it is discarded, so an upper layer
#: can flush its salient points to durable storage (long-term memory). The
#: compressor stays pure — it owns no store/embedder; the hook is injected.
PreCompactionHook = Callable[[Sequence[BaseMessage]], Awaitable[None]]


@dataclass(frozen=True)
class CompactionStats:
    """Stream RT-2 PR-4 — one compaction's observable summary.

    Handed to the :data:`OnCompacted` hook once per :meth:`compress` call
    that actually produced a summary (a skip-only round or an
    already-under-threshold entry emits nothing). ``tokens_before`` /
    ``tokens_after`` are the compressor's own estimate (chars//4 or the
    injected tiktoken estimator) at entry and final return, so the
    reclaimed amount is ``max(0, tokens_before - tokens_after)``.
    ``passes`` is the number of summarise-the-middle passes that ran;
    ``summary_chars`` is the length of the final ``<context-summary>``
    block.
    """

    passes: int
    tokens_before: int
    tokens_after: int
    summary_chars: int


#: Stream RT-2 PR-4 — compaction observability hook. Awaited once per
#: :meth:`ContextCompressor.compress` call that produced a summary, with the
#: run's :class:`CompactionStats`. Symmetric to :data:`PreCompactionHook`:
#: the compressor stays free of any SSE/bridge dependency — the caller
#: (``agent_node``) injects a hook that publishes the COMPACTION event and
#: swallows its own non-cancellation failures (best-effort by contract).
OnCompacted = Callable[["CompactionStats"], Awaitable[None]]


#: Stream L.L2 — chars-per-token rule of thumb. The summariser prompt
#: itself is bounded so a fudge here only affects when compression
#: triggers, not what it produces.
_CHARS_PER_TOKEN: int = 4

#: RT-ADR-6 — consecutive failed compression rounds tolerated before the
#: compressor escalates to :class:`ContextOverflowError`. Below the cap a
#: summariser failure skips the round (the prompt goes out uncompressed —
#: the estimate is conservative, the upstream usually still accepts it)
#: and the next turn retries.
_MAX_CONSECUTIVE_SUMMARY_FAILURES: int = 3

#: RT-ADR-6 — upper bound on the per-conversation streak map. A
#: ContextCompressor instance is cached per (tenant, agent, version) and
#: long-lived, so an unbounded ``dict[streak_key, count]`` would grow
#: with every conversation that ever saw a summariser failure. 1024
#: distinct simultaneously-failing conversations per agent is far beyond
#: any real incident; past it the oldest-inserted entry is evicted
#: (losing a streak only delays escalation by at most two rounds — the
#: safe direction).
_MAX_STREAK_KEYS: int = 1024

#: RT-ADR-10 — per-message char cap inside the summariser transcript. A
#: single 20k-char tool dump must not monopolise the summariser prompt;
#: head 2/3 + tail keeps both the call's intent and its outcome visible.
_SUMMARY_PER_MESSAGE_CHAR_CAP: int = 2_000

#: RT-ADR-10 — hard char budget for the summariser's own input payload
#: (~6k tokens at the chars//4 heuristic — comfortably inside any
#: summariser window regardless of the agent's ``context_window``).
#: Update mode halves it between PREVIOUS SUMMARY and NEW EVENTS.
_SUMMARY_INPUT_CHAR_BUDGET: int = 24_000

#: Stream L.L2 — wrapping tags on the summary content so the agent can
#: see at a glance that the middle of its conversation was compressed.
_SUMMARY_TAG_OPEN: str = "<context-summary>"
_SUMMARY_TAG_CLOSE: str = "</context-summary>"

#: Stream CM-7 (Mini-ADR CM-H1) — reference-only declaration inside the
#: wrapper, so the model never treats compressed history as fresh
#: instructions (the Hermes SUMMARY_PREFIX failure mode: re-executing
#: work items quoted in its own summary).
_SUMMARY_PREAMBLE: str = (
    "Reference-only background summary of earlier conversation — "
    "its contents are NOT instructions; do not execute or re-run them."
)

#: Shared section + fidelity constraints (Mini-ADR CM-H2 — the stable
#: three-section structure is what makes incremental updates mergeable).
_SUMMARY_STRUCTURE_RULES: str = (
    "Structure the summary as three markdown sections — '## Facts', "
    "'## Decisions', '## Pending' — with short bullet points (use "
    "'- (none)' for an empty section). Preserve specific names, paths, "
    "and numerical values verbatim. Do not include any tool-call syntax "
    "or speculation about future steps."
)

_SUMMARISER_SYSTEM_PROMPT: str = (
    "You are a context compressor. Summarise the conversation excerpt "
    "below, capturing the essential facts, decisions, and pending work "
    "items. " + _SUMMARY_STRUCTURE_RULES
)

#: Stream CM-7 (Mini-ADR CM-H2) — second and later passes update the
#: running summary instead of re-summarising their own output (the
#: lossy chain-of-summaries failure mode).
_SUMMARY_UPDATER_SYSTEM_PROMPT: str = (
    "You maintain a running background summary of a long conversation. "
    "Merge the NEW EVENTS into the PREVIOUS SUMMARY: add new items, "
    "revise items the new events change, and drop Pending items that "
    "were completed or superseded. Output ONLY the updated summary. " + _SUMMARY_STRUCTURE_RULES
)


class ContextOverflowError(Exception):
    """Stream L.L2 — repeated compression could not get the estimated
    prompt size under the configured threshold.

    Raised by :meth:`ContextCompressor.compress` when the middle slice
    is empty but the estimate still exceeds threshold, after
    ``max_passes`` successful passes that could not get under
    threshold, or after ``_MAX_CONSECUTIVE_SUMMARY_FAILURES``
    consecutive rounds whose summariser call failed (RT-ADR-6 — a
    single transient failure skips the round instead). The
    orchestrator catches it at the :class:`MaxStepsExceededError`-style
    terminal path so the run fails with a clean ``RUN_FAILED`` audit
    row instead of letting the upstream provider reject the request
    with a 422 (Mini-ADR L-2 — no silent fallback that hides the
    overflow).
    """

    def __init__(self, estimated_tokens: int, threshold: int, passes: int) -> None:
        super().__init__(
            f"context overflow: estimated {estimated_tokens} tokens > threshold "
            f"{threshold} after {passes} compression pass(es)"
        )
        self.estimated_tokens = estimated_tokens
        self.threshold = threshold
        self.passes = passes


def estimate_tokens(
    messages: Sequence[BaseMessage],
    *,
    estimator: TokenEstimator | None = None,
) -> int:
    """Token estimate for ``messages``.

    Without an ``estimator`` this is the legacy rough estimate —
    ``total_chars // 4`` (matches Hermes ``estimate_request_tokens_rough``;
    cheap, no dependency, a heavy underestimate for CJK). With one
    (Stream HX-1 — the factory injects the tiktoken-backed
    :func:`~expert_work.runtime.tokens.default_estimator`) it is a
    per-message real count. Upstream stays authoritative on the actual
    number either way.
    """
    if estimator is not None:
        return estimate_messages(messages, estimator)
    total = 0
    for msg in messages:
        total += len(_message_to_text(msg))
    return total // _CHARS_PER_TOKEN


#: Stream HX-1 — the flattening moved to ``expert_work.runtime.tokens``
#: (shared with the middleware layer); the local name stays because the
#: summary formatting below uses it too.
_message_to_text = flatten_message


@dataclass(frozen=True)
class _SplitMessages:
    """Slice of the message list — head/middle/tail by index, plus
    the leading SystemMessages kept verbatim."""

    leading_systems: list[BaseMessage]
    head: list[BaseMessage]
    middle: list[BaseMessage]
    tail: list[BaseMessage]


def _split(messages: Sequence[BaseMessage], *, head_keep: int, tail_keep: int) -> _SplitMessages:
    """Split ``messages`` into (leading systems, head, middle, tail).

    Leading :class:`SystemMessage` instances stay outside the
    head/tail accounting — the L1 invariant requires the system
    prompt block to stay byte-stable, so the compressor never touches
    it. Head / tail are slices of the *non-system* tail of the list.
    """
    leading_systems: list[BaseMessage] = []
    cursor = 0
    while cursor < len(messages) and isinstance(messages[cursor], SystemMessage):
        leading_systems.append(messages[cursor])
        cursor += 1
    remainder = list(messages[cursor:])
    head = remainder[:head_keep]
    tail = remainder[-tail_keep:] if tail_keep else []
    # Compute middle by index to handle overlap between head/tail when
    # the list is short (head_keep + tail_keep > len(remainder)).
    middle_start = head_keep
    middle_end = len(remainder) - tail_keep if tail_keep else len(remainder)
    if middle_end <= middle_start:
        middle: list[BaseMessage] = []
    else:
        middle = remainder[middle_start:middle_end]
    return _SplitMessages(
        leading_systems=leading_systems,
        head=head,
        middle=middle,
        tail=tail,
    )


def _extract_prior_summary(
    middle: Sequence[BaseMessage],
) -> tuple[str | None, list[BaseMessage]]:
    """Pull the most recent running summary out of the middle slice (CM-7).

    Returns ``(prior_body, remaining_middle)``. The LAST
    ``<context-summary>`` SystemMessage is the running summary to update;
    any earlier ones (degenerate multi-summary histories) stay in the
    remainder and get folded into the new-events transcript, so the chain
    converges to a single running summary. ``(None, middle)`` when no
    summary is present (fresh mode).
    """
    prior_idx: int | None = None
    prior_content: str | None = None
    for idx, msg in enumerate(middle):
        if (
            isinstance(msg, SystemMessage)
            and isinstance(msg.content, str)
            and msg.content.startswith(_SUMMARY_TAG_OPEN)
        ):
            prior_idx, prior_content = idx, msg.content
    if prior_idx is None or prior_content is None:
        return None, list(middle)
    body = prior_content.removeprefix(_SUMMARY_TAG_OPEN).removesuffix(_SUMMARY_TAG_CLOSE).strip()
    # Strip the reference-only preamble when present (pre-CM-7 summaries
    # in old checkpoints carry none) — it is re-added by the wrapper.
    body = body.removeprefix(_SUMMARY_PREAMBLE).strip()
    rest = [msg for idx, msg in enumerate(middle) if idx != prior_idx]
    return body, rest


def _bound_text(text: str, max_chars: int) -> str:
    """RT-ADR-10 — bound ``text`` to roughly ``max_chars`` chars.

    Keeps the head (2/3 of the budget) and the tail (the remainder)
    around a visible elision marker (deer-flow #3887 ``_bound_text``
    shape): the head carries the setup / intent, the tail the most
    recent state. Output may exceed the budget by the marker length
    only.
    """
    if len(text) <= max_chars:
        return text
    head = (max_chars * 2) // 3
    tail = max_chars - head
    dropped = len(text) - max_chars
    return f"{text[:head]}\n[... {dropped} chars truncated ...]\n{text[-tail:]}"


def _format_middle_for_summary(
    middle: Sequence[BaseMessage],
    *,
    char_budget: int = _SUMMARY_INPUT_CHAR_BUDGET,
) -> str:
    """Render the middle slice as a flat transcript the summariser
    LLM consumes. The format is intentionally simple — role: text —
    so the summariser doesn't get sidetracked by JSON wire format.

    RT-ADR-10: each message is bounded to
    ``_SUMMARY_PER_MESSAGE_CHAR_CAP`` (one oversized tool dump must not
    monopolise the transcript) and the joined transcript is bounded to
    ``char_budget`` so the summariser call itself can never overflow
    the summariser's window.
    """
    lines: list[str] = []
    for msg in middle:
        # B-67 — hidden HumanMessages (inputs block, advisories) are platform
        # scaffolding; the summary is durable text, so they stay out of it.
        if is_hidden(msg):
            continue
        role = _role_label(msg)
        # RT-2 PR-3 (RT-ADR-7) — a successful lazy-skill read is fed to the
        # summariser as its one-line reference (name + path, no tool-call
        # syntax), never its body: the summary keeps the skill identity and
        # the input budget goes to the actual conversation. ``None`` (not a
        # skill_view message, non-success result, or no skill_name) keeps
        # the default path byte-identical.
        reference = skill_view_reference(msg)
        if reference is not None:
            lines.append(f"{role}: {reference}")
            continue
        text = _message_to_text(msg).strip()
        if text:
            lines.append(f"{role}: {_bound_text(text, _SUMMARY_PER_MESSAGE_CHAR_CAP)}")
    return _bound_text("\n\n".join(lines), char_budget)


def _role_label(msg: BaseMessage) -> str:
    if isinstance(msg, SystemMessage):
        return "system"
    if isinstance(msg, HumanMessage):
        return "user"
    if isinstance(msg, AIMessage):
        return "assistant"
    if isinstance(msg, ToolMessage):
        return "tool"
    return type(msg).__name__


def _summary_chars(messages: Sequence[BaseMessage]) -> int:
    """Length of the ``<context-summary>`` block in ``messages`` (0 if none).

    Stream RT-2 PR-4 — a compressed message list carries exactly one
    ``<context-summary>`` SystemMessage (the last pass's running summary);
    its char length is a cheap proxy for how much the middle was condensed
    into, surfaced in the COMPACTION event payload.
    """
    for msg in reversed(messages):
        if (
            isinstance(msg, SystemMessage)
            and isinstance(msg.content, str)
            and msg.content.startswith(_SUMMARY_TAG_OPEN)
        ):
            return len(msg.content)
    return 0


def floor_head_keep_for_injection(head_keep: int, *, per_session_memory_active: bool) -> int:
    """Stream RT-2 PR-4 (§8.4) — floor ``head_keep`` to 1 when per_session
    memory injection is active.

    per_session recall (Mini-ADR U-8) lands the cache-anchor + memory
    guidance block at ``messages[1]``; ``head_keep=0`` would let
    :func:`_split` fold that block into the summarised middle, silently
    dropping both the Anthropic cache anchor and the recalled memories
    (the risk RT-ADR-8's combo test pinned). ``head_keep=0`` stays legit
    for agents WITHOUT per_session injection — this only raises the floor
    when the anchor is actually present, so a valid non-memory config is
    never bricked.
    """
    if per_session_memory_active and head_keep < 1:
        return 1
    return head_keep


@dataclass
class _FailureStreaks:
    """RT-ADR-6 — per-conversation consecutive-failure counters.

    Keyed by the caller-supplied ``streak_key`` (the LangGraph
    ``thread_id`` in production — see ``builder.agent_node``). The
    keying matters: a :class:`ContextCompressor` hangs off a BuiltAgent
    that ``AgentRuntime._cache`` shares per ``(tenant, agent, version)``
    across every thread / run / user of that agent, so a single shared
    counter would let conversation A's failures escalate conversation
    C's first wobble into a false :class:`ContextOverflowError` — and,
    in the other direction, healthy conversations' resets would mask a
    genuinely broken summariser.

    Lives in its own mutable object so the frozen
    :class:`ContextCompressor` (whose configuration stays immutable)
    can still track cross-round summariser health on the instance.
    """

    counts: dict[str, int] = field(default_factory=dict)

    def bump(self, key: str) -> int:
        """Increment and return ``key``'s streak, evicting the
        oldest-inserted entry when the map is full (see
        ``_MAX_STREAK_KEYS`` — approximate LRU is good enough here)."""
        if key not in self.counts and len(self.counts) >= _MAX_STREAK_KEYS:
            self.counts.pop(next(iter(self.counts)))
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def clear(self, key: str) -> None:
        """Drop ``key``'s streak after a successful pass."""
        self.counts.pop(key, None)


@dataclass(frozen=True)
class ContextCompressor:
    """Summarise the middle of a conversation when its estimated size
    exceeds the model's context window threshold.

    Build one per agent (the factory wires the manifest's
    ``policies.context_compression`` policy into this). Pass to
    :func:`build_react_graph` so ``agent_node`` can call
    :meth:`compress` at its entry preflight.
    """

    llm_caller: LLMCaller
    context_window: int
    threshold_pct: float = 0.7
    head_keep: int = 4
    tail_keep: int = 6
    max_passes: int = 3
    #: Stream HX-1 (Mini-ADR HX-A1) — injected token estimator. ``None``
    #: keeps the legacy ``chars // 4`` heuristic (direct construction /
    #: unit tests stay network-free); the factory injects the shared
    #: tiktoken-backed estimator.
    estimator: TokenEstimator | None = None
    #: RT-ADR-6 — per-conversation consecutive-failure streaks, keyed by
    #: the ``streak_key`` handed to :meth:`compress` (thread_id in
    #: production; this instance is shared across conversations). A
    #: key's streak resets on any successful pass; reaching
    #: ``_MAX_CONSECUTIVE_SUMMARY_FAILURES`` escalates that
    #: conversation's failure to :class:`ContextOverflowError`.
    _summary_failures: _FailureStreaks = field(
        default_factory=_FailureStreaks, init=False, repr=False, compare=False
    )

    @property
    def threshold_tokens(self) -> int:
        """Token threshold above which a preflight should trigger a
        compression. The agent_node preflight uses ``>=`` against this
        value to decide whether to call :meth:`compress`."""
        return int(self.context_window * self.threshold_pct)

    def _estimate(self, messages: Sequence[BaseMessage]) -> int:
        return estimate_tokens(messages, estimator=self.estimator)

    def should_compress(self, messages: Sequence[BaseMessage]) -> bool:
        """Cheap preflight — returns ``True`` if the estimated prompt
        size meets or exceeds the threshold."""
        return self._estimate(messages) >= self.threshold_tokens

    async def compress(
        self,
        messages: Sequence[BaseMessage],
        *,
        on_pre_compaction: PreCompactionHook | None = None,
        on_compacted: OnCompacted | None = None,
        streak_key: str | None = None,
    ) -> list[BaseMessage]:
        """Compress the message list until it fits under the threshold.

        Returns a new list — never mutates the input.

        Failure semantics (Mini-ADR L-2 as revised by RT-ADR-6):

        - an empty middle that is still over threshold raises
          :class:`ContextOverflowError` immediately (nothing left to
          summarise — the only knobs are manifest-level);
        - ``max_passes`` successful passes that cannot get under
          threshold raise :class:`ContextOverflowError`;
        - a transient summariser failure SKIPS compression for this
          round — the current messages are returned uncompressed
          (metric + warning log) and the next turn retries. Only
          ``_MAX_CONSECUTIVE_SUMMARY_FAILURES`` consecutive failed
          rounds *of the same conversation* escalate to
          :class:`ContextOverflowError`, so a persistently broken
          summariser still fails loudly instead of silently ballooning
          forever.

        ``streak_key`` scopes the consecutive-failure count — pass the
        conversation identity (the LangGraph ``thread_id``): this
        compressor instance is shared per (tenant, agent, version)
        across all conversations, so an unscoped count would mix
        unrelated runs' failures (see :class:`_FailureStreaks`). With
        ``streak_key=None`` a transient failure keeps the skip-once
        semantics but is NOT counted toward escalation — without a
        conversation identity, counting on the shared instance would
        reintroduce exactly the cross-conversation false-failure the
        key exists to prevent. The empty-middle / max_passes fail-hard
        paths are unaffected by the key.

        Stream CM-3 — ``on_pre_compaction`` (when supplied) is awaited with
        the middle slice each pass is about to discard, *before* it is
        summarised away, so the caller can flush it to durable memory. It
        is best-effort by contract: the caller must swallow its own
        non-cancellation failures; a :class:`RunCancelledError` raised by
        the hook propagates out and aborts the run.

        Stream RT-2 PR-4 — ``on_compacted`` (when supplied) is awaited ONCE
        per call that actually produced a summary, with the run's
        :class:`CompactionStats` (a skip-only round or an entry already
        under threshold emits nothing). Best-effort by the same contract as
        ``on_pre_compaction``: the caller swallows its own failures.
        """
        current: list[BaseMessage] = list(messages)
        tokens_before = self._estimate(current)
        passes_done = 0
        for pass_idx in range(self.max_passes):
            if self._estimate(current) < self.threshold_tokens:
                if pass_idx > 0:
                    logger.info(
                        "context_compressor.compressed passes=%d final_tokens=%d",
                        pass_idx,
                        self._estimate(current),
                    )
                return await self._finish_compaction(
                    current, tokens_before, passes_done, on_compacted
                )
            try:
                current = await self._compress_once(current, on_pre_compaction=on_pre_compaction)
            except ContextOverflowError:
                raise
            except RunCancelledError:
                # A cancelled run must abort, never be mistaken for a
                # transient summariser failure (CM-3 hook contract).
                raise
            except Exception as exc:
                # RT-ADR-6 — transient summariser failure: skip this
                # round, count it per conversation, retry next turn.
                # Only an unbroken streak of failed rounds for the SAME
                # streak_key escalates to fail-hard. Without a key the
                # failure still skips but is not counted (see the
                # docstring — counting on the shared instance would mix
                # unrelated conversations).
                _cm_compressor_summary_failure_total.inc()
                streak = self._summary_failures.bump(streak_key) if streak_key else 0
                if streak >= _MAX_CONSECUTIVE_SUMMARY_FAILURES:
                    logger.exception(
                        "context_compressor.summariser_failed_hard consecutive=%d",
                        streak,
                    )
                    raise ContextOverflowError(
                        estimated_tokens=self._estimate(current),
                        threshold=self.threshold_tokens,
                        passes=pass_idx,
                    ) from exc
                _cm_compressor_skipped_total.inc()
                logger.warning(
                    "context_compressor.summariser_failed_skipping consecutive=%d "
                    "estimated_tokens=%d threshold=%d error=%s",
                    streak,
                    self._estimate(current),
                    self.threshold_tokens,
                    exc,
                )
                # A prior pass this call may already have produced a summary
                # (passes_done > 0) — fire the hook for what did land before
                # this round's transient skip; passes_done == 0 emits nothing.
                return await self._finish_compaction(
                    current, tokens_before, passes_done, on_compacted
                )
            if streak_key:
                self._summary_failures.clear(streak_key)
            _cm_compressor_pass_total.inc()
            passes_done += 1
        if self._estimate(current) >= self.threshold_tokens:
            raise ContextOverflowError(
                estimated_tokens=self._estimate(current),
                threshold=self.threshold_tokens,
                passes=self.max_passes,
            )
        return await self._finish_compaction(current, tokens_before, passes_done, on_compacted)

    async def _finish_compaction(
        self,
        result: list[BaseMessage],
        tokens_before: int,
        passes_done: int,
        on_compacted: OnCompacted | None,
    ) -> list[BaseMessage]:
        """Stream RT-2 PR-4 — fire the tokens-saved counter + ``on_compacted``
        hook once, only when at least one pass produced a summary.

        Called from every non-raising exit of :meth:`compress` so the
        magnitude counter and the COMPACTION event fire exactly once per
        call (not per pass); ``passes_done == 0`` (skip-only / already under
        threshold) is a no-op.
        """
        if passes_done <= 0:
            return result
        tokens_after = self._estimate(result)
        _cm_compressor_tokens_saved_total.inc(max(0, tokens_before - tokens_after))
        if on_compacted is not None:
            await on_compacted(
                CompactionStats(
                    passes=passes_done,
                    tokens_before=tokens_before,
                    tokens_after=tokens_after,
                    summary_chars=_summary_chars(result),
                )
            )
        return result

    async def _compress_once(
        self,
        messages: list[BaseMessage],
        *,
        on_pre_compaction: PreCompactionHook | None = None,
    ) -> list[BaseMessage]:
        """One summarise-the-middle pass."""
        split = _split(messages, head_keep=self.head_keep, tail_keep=self.tail_keep)
        if not split.middle:
            # Nothing summarisable — head+tail already span the
            # remainder. Surfacing this as an overflow is the right
            # signal because the only knobs left to turn are the
            # head/tail-keep counts (manifest-level) or a bigger
            # window.
            raise ContextOverflowError(
                estimated_tokens=self._estimate(messages),
                threshold=self.threshold_tokens,
                passes=0,
            )
        # Stream CM-3 — flush the middle to durable memory BEFORE it is
        # summarised away (and before the summariser LLM call, so the
        # salient points survive even if summarisation then fails).
        if on_pre_compaction is not None:
            await on_pre_compaction(split.middle)
        # Stream CM-7 (Mini-ADR CM-H2) — when the middle carries an
        # earlier compression's summary, UPDATE it with the new events
        # instead of re-summarising its own output (lossy chain).
        prior, fresh_middle = _extract_prior_summary(split.middle)
        if prior is not None:
            # RT-ADR-10 — update mode splits the input budget evenly:
            # PREVIOUS SUMMARY and NEW EVENTS each get half, so neither
            # side can starve the other out of the summariser prompt.
            summary_text = await self._summarise_update(
                _bound_text(prior, _SUMMARY_INPUT_CHAR_BUDGET // 2),
                _format_middle_for_summary(
                    fresh_middle, char_budget=_SUMMARY_INPUT_CHAR_BUDGET // 2
                ),
            )
        else:
            summary_text = await self._summarise(_format_middle_for_summary(split.middle))
        logger.info(
            "context_compressor.summary mode=%s middle=%d",
            "update" if prior is not None else "fresh",
            len(split.middle),
        )
        wrapped = SystemMessage(
            content=(
                f"{_SUMMARY_TAG_OPEN}\n{_SUMMARY_PREAMBLE}\n\n{summary_text}\n{_SUMMARY_TAG_CLOSE}"
            )
        )
        return [*split.leading_systems, *split.head, wrapped, *split.tail]

    async def _summarise(self, transcript: str) -> str:
        """Invoke the summariser LLM and return the summary body."""
        prompt = [
            SystemMessage(content=_SUMMARISER_SYSTEM_PROMPT),
            HumanMessage(content=transcript),
        ]
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "compress"):
            response = await self.llm_caller(messages=prompt, tools=[])
        return _message_to_text(response).strip() or "(no summary produced)"

    async def _summarise_update(self, prior: str, transcript: str) -> str:
        """Merge new events into the previous running summary (CM-7)."""
        prompt = [
            SystemMessage(content=_SUMMARY_UPDATER_SYSTEM_PROMPT),
            HumanMessage(content=f"PREVIOUS SUMMARY:\n{prior}\n\nNEW EVENTS:\n{transcript}"),
        ]
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "compress"):
            response = await self.llm_caller(messages=prompt, tools=[])
        return _message_to_text(response).strip() or "(no summary produced)"
