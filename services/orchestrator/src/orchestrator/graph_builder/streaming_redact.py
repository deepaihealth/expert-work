"""Buffered-release streaming redaction for token SSE frames (流式 epic 子项目 2).

Token deltas escape the LLM router BEFORE the node-level output guards run on the
assembled message, so a per-delta redaction pass runs here as defense in depth.
Reuses the SAME regex guards as the non-streaming path (``scan_and_redact`` for
DLP, ``screen_output`` for the credential/exfil screen), applied incrementally
with a look-back hold so a pattern split across deltas is never partially emitted.

Token frames are provisional: the authoritative ``updates`` frame (full guards on
the complete message) is the source of truth. This redactor is best-effort on the
preview — the fixed-shape DLP patterns (card/id/phone) fit fully inside the hold
window; anything longer is covered by the authoritative frame. The residual case
is email: the pattern is unbounded, so an address longer than HOLD_CHARS can still
leak a partial head in the provisional preview — the authoritative frame is the
backstop for that case.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from expert_work.common.dlp import scan_and_redact
from expert_work.common.output_screen import screen_output

if TYPE_CHECKING:
    from orchestrator.llm.providers._streaming import LLMDelta

#: Characters held back from the tail of the (redacted) buffer on each feed.
#: Two-sided invariant — HOLD_CHARS must be:
#:   1. >= every BLOCK guard's (``screen_output``) MINIMUM regex-match length,
#:      else a partial credential head could cross the emission boundary and
#:      escape before the screen latch trips.
#:   2. >= every fixed-shape DLP (``scan_and_redact``) pattern's MAXIMUM
#:      match length, so a pattern still completing is always still inside
#:      the hold and never partially redacted.
#: Holds today: screen minimums top out at 39 (Google API key `AIza…{35}`;
#: sk-…{20,}=23, AKIA…=20, xox…=15, PEM=27); DLP fixed-shape maxes top out at
#: 19 (card 19 / id 18 / phone 11 — email is unbounded, see module docstring).
#: A future guard pattern with a larger minimum-match-length would silently
#: defeat the hold.
HOLD_CHARS = 64

#: Target lag of the frozen pointer behind the buffer end (and thus the size of
#: the suffix rescanned by DLP each feed). Governs per-feed rescan cost, not
#: correctness: bounded-equivalence is upheld by the exact split-equality check
#: in ``_advance_frozen``, and the screen latch by scanning the full unfrozen
#: tail (``_buf[_frozen_raw:]``) — a credential's minimum match (<= 39) is fully
#: inside that tail the feed it completes, because HOLD_CHARS (64) > 39 keeps
#: _frozen_raw behind the credential's start until it is caught and latched.
RESCAN_LOOKBACK = 64

#: Frozen-pointer target lag: ``_advance_frozen`` tries to freeze up to
#: ``end - WINDOW`` each feed (≈ the DLP rescan / tail size in steady state).
#: Everything before a *verified-clean* frozen boundary is finalized and never
#: rescanned again — this is what makes ``feed`` O(1) amortized (O(n) over the
#: whole stream) instead of O(n) per delta. (Screen is NOT windowed — it scans
#: the full unfrozen tail; see RESCAN_LOOKBACK above.)
WINDOW = HOLD_CHARS + RESCAN_LOOKBACK


class StreamingRedactor:
    """Incremental buffered-release redactor over one content channel.

    ``feed(text)`` returns the newly-stable redacted prefix safe to emit now;
    ``flush()`` returns the redacted remainder at stream end. Only the guards
    enabled for the run (``dlp`` / ``screen``) are applied. ``screen`` is a BLOCK
    guard: once it trips, the redactor withholds everything (the authoritative
    frame carries the refusal).
    """

    def __init__(self, *, dlp: bool, screen: bool) -> None:
        self._dlp = dlp
        self._screen = screen
        self._buf = ""
        #: Redacted chars emitted so far (monotonic; = old ``_emitted_len``).
        self._emitted_out = 0
        #: Raw offset of the finalized boundary: ``_buf[:_frozen_raw]`` redaction
        #: is settled and already emitted, so it is never rescanned again.
        self._frozen_raw = 0
        #: Redacted-char count of ``_buf[:_frozen_raw]``. Invariant (upheld by
        #: the collapse guard in ``_advance_frozen``): ``_frozen_out <= _emitted_out``.
        self._frozen_out = 0
        self._blocked = False
        # 二期 P3 —— 双关时整条快路径:无 hold、无重扫、无冻结指针。
        # hold 的存在意义是 screen 的整段撤回 + dlp 的跨 chunk 模式匹配,
        # 两者都关时扣住尾部 64 字符纯粹是感知延迟损耗。
        self._passthrough = not dlp and not screen

    def _redact(self, text: str) -> str:
        return scan_and_redact(text).redacted if self._dlp else text

    def _advance_frozen(self, tail_red: str) -> None:
        # Push the frozen boundary up to ``end - WINDOW`` so the rescanned tail
        # stays bounded — but only if the buffer's redaction splits EXACTLY at
        # new_frozen: redact(head) ++ redact(retained) == the full-context
        # tail_red. This is load-bearing and NOT replaceable by a prefix test:
        # new_frozen (a function of total buffer length) drifts backward into an
        # EARLIER match as the buffer grows, and because ``[redacted]`` is a
        # fixed token, a cut that forms a DIFFERENT collapsing match (e.g. an
        # 18-char id-card's 16-digit prefix independently matches the card shape)
        # produces the same ``[redacted]`` and would fool ``startswith``. Exact
        # split-equality is the real cleanliness invariant; if it fails,
        # new_frozen straddles a match — defer advancing this feed.
        new_frozen = max(0, len(self._buf) - WINDOW)
        if new_frozen <= self._frozen_raw:
            return
        head_red = self._redact(self._buf[self._frozen_raw : new_frozen])
        retained_red = self._redact(self._buf[new_frozen:])
        if head_red + retained_red != tail_red:
            return
        added = len(head_red)
        # Collapse guard: a PII span completing just past the emission frontier
        # makes ``redact`` shrink, so a clean-split frozen count can momentarily
        # exceed what we've emitted; freezing it would drive
        # ``_frozen_out > _emitted_out`` and a later negative slice index
        # (Python wraps → tail leak). Defer until emission catches up.
        if self._frozen_out + added > self._emitted_out:
            return
        self._frozen_out += added
        self._frozen_raw = new_frozen

    def feed(self, text: str) -> str:
        if self._passthrough:
            return text
        if self._blocked:
            return ""
        self._buf += text
        if not text:
            return ""
        tail = self._buf[self._frozen_raw :]
        if self._screen and screen_output(tail).blocked:
            self._blocked = True
            return ""
        tail_red = self._redact(tail)
        full_red_len = self._frozen_out + len(tail_red)
        boundary = max(self._emitted_out, full_red_len - HOLD_CHARS)
        out = tail_red[self._emitted_out - self._frozen_out : boundary - self._frozen_out]
        self._emitted_out = boundary
        self._advance_frozen(tail_red)
        return out

    def flush(self) -> str:
        if self._passthrough:
            return ""
        if self._blocked:
            return ""
        tail = self._buf[self._frozen_raw :]
        if self._screen and screen_output(tail).blocked:
            self._blocked = True
            return ""
        tail_red = self._redact(tail)
        out = tail_red[self._emitted_out - self._frozen_out :]
        self._emitted_out = self._frozen_out + len(tail_red)
        return out


#: Async callback that ships one token frame to the SSE bridge (injected by
#: run_agent via ``TOKEN_SINK_KEY``; see graph_builder/_config.py).
TokenPublish = Callable[[dict[str, Any]], Awaitable[None]]


class TokenSink:
    """Per-run multi-channel token emitter (子项目 2 content + 3b reasoning/tool_args).

    One :class:`StreamingRedactor` per *text* channel (content, reasoning —
    independent buffered-release streams); tool-call *names* are emitted once
    per ``index`` when first seen. Each streamed ``LLMDelta`` publishes the
    newly-stable redacted text of each text channel; ``flush`` releases the
    buffered-release tails after the router returns. Tool *arguments* are NOT
    streamed — they reach the client via the authoritative ``updates`` frame
    (name-only, 子项目 3b decision), so there is no argument-redaction path.
    """

    def __init__(self, *, step: int, publish: TokenPublish, dlp: bool, screen: bool) -> None:
        self._step = step
        self._publish = publish
        self._content = StreamingRedactor(dlp=dlp, screen=screen)
        self._reasoning = StreamingRedactor(dlp=dlp, screen=screen)
        self._tool_names: dict[int, str] = {}
        #: ``time.monotonic()`` of the first non-empty delta; agent_node uses
        #: it to compute ``first_token_ms``.
        self.first_delta_at: float | None = None

    async def __call__(self, delta: LLMDelta) -> None:
        if self.first_delta_at is None and delta.has_progress:
            self.first_delta_at = time.monotonic()
        safe = self._content.feed(delta.content)
        if safe:
            await self._publish({"step": self._step, "channel": "content", "text": safe})
        rsafe = self._reasoning.feed(delta.reasoning)
        if rsafe:
            await self._publish({"step": self._step, "channel": "reasoning", "text": rsafe})
        for tc in delta.tool_calls:
            if tc.name and tc.index not in self._tool_names:
                self._tool_names[tc.index] = tc.name
                await self._publish(
                    {
                        "step": self._step,
                        "channel": "tool_args",
                        "tool_index": tc.index,
                        "name": tc.name,
                    }
                )

    async def flush(self) -> None:
        tail = self._content.flush()
        if tail:
            await self._publish({"step": self._step, "channel": "content", "text": tail})
        rtail = self._reasoning.flush()
        if rtail:
            await self._publish({"step": self._step, "channel": "reasoning", "text": rtail})


def make_token_sink(
    *,
    step: int,
    publish: TokenPublish | None,
    dlp: bool,
    screen: bool,
    judge_enabled: bool,
) -> TokenSink | None:
    """Build a :class:`TokenSink`, or ``None`` when token streaming is gated off.

    Gate: an LLM output judge (``judge_enabled``) can only decide on the complete
    message, so its runs never token-stream; and without an injected ``publish``
    sink there is nowhere to send frames.
    """
    if judge_enabled or publish is None:
        return None
    return TokenSink(step=step, publish=publish, dlp=dlp, screen=screen)
