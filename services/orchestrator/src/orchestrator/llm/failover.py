"""B-297 — fast-failover support for :class:`~orchestrator.llm.router.LLMRouter`.

Three pieces the router composes:

* :func:`plan_walk` — the order one call walks the provider chain in. With no
  per-run memory it is the chain order. Once a run was served by a later
  provider group because an earlier one failed (**stickiness**, "一轮内不来回
  切"), later calls of that run start at that group and only revisit the
  skipped earlier groups as a last resort before giving up.
* :class:`StickyRouteStore` — that per-run memory, keyed by
  ``(run_id, chain)``. Built agents (and their routers) are cached and shared
  by concurrent runs, so the state lives outside the router instance, keyed by
  the run; bounded by an idle TTL and a size cap so it cannot leak.
* metrics / labels — ``expert_work_llm_failover_total{from_group,to_group,reason}``
  with bounded label values (groups are ``provider:model`` chain entries,
  reasons a fixed vocabulary), and :func:`breaker_key_for`, the breaker
  identity the E.4 middleware keys its shared circuit breakers by.

PR2 adds the switch *notice* (:func:`announce_failover` → a console-only
``llm_failover`` run event via :func:`failover_frames`), the discard signal for
an abandoned reasoning-only attempt (:func:`discard_streamed_attempt`), and the
time-to-first-progress histogram that validates the first-token caps above.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage

from expert_work.common.observability import expert_work_counter, expert_work_histogram
from expert_work.runtime.middleware import (
    CircuitOpenError,
    LLMAuthError,
    LLMKeyUnavailableError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
    LLMStreamStaleError,
    LLMUnauthorizedError,
)
from expert_work.runtime.tokens import TokenEstimator, estimate_messages
from orchestrator.llm.stall import LLMReasoningStallError, has_visible_output

logger = logging.getLogger(__name__)

_llm_failover_total = expert_work_counter(
    "expert_work_llm_failover_total",
    "LLM calls that moved from one provider group to another (B-297). "
    "reason=sticky: the run had already failed over, the call started there.",
    ("from_group", "to_group", "reason"),
)

#: PR2 — seconds from the start of a streaming attempt to its first progress
#: delta (reasoning counts). Validates the first-token caps below: a timeout
#: observes nothing (``expert_work_llm_stream_stale_total`` counts those).
_llm_first_progress_seconds = expert_work_histogram(
    "expert_work_llm_first_progress_seconds",
    "Streaming LLM attempts: time to the first progress delta, reasoning included (B-297).",
    ("provider_key",),
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 45, 60, 90, 120, 180),
)

#: Reason vocabulary (bounded label values), most specific class first.
_REASONS: tuple[tuple[type[BaseException], str], ...] = (
    (CircuitOpenError, "circuit_open"),
    # PR2 — a subclass of LLMStreamStaleError, so it must come before it.
    (LLMReasoningStallError, "reasoning_stall"),
    (LLMKeyUnavailableError, "key_unavailable"),
    (LLMAuthError, "auth"),
    (LLMUnauthorizedError, "auth"),
    (LLMRateLimitError, "rate_limit"),
    (LLMStreamStaleError, "first_token_timeout"),
    (LLMNetworkError, "network"),
    (LLMServerError, "server_error"),
)

#: First-token deadline while a backup provider group exists and the call
#: streams. The timer restarts on every delta and reasoning deltas count as
#: progress (``LLMDelta.has_progress``), so it only fires when the upstream
#: sends nothing at all. Prior art: Claude Code gives up after 90 s idle;
#: OpenClaw waits 120 s for cloud models and 60 s on its cron fallback path;
#: hermes-agent scales 90-240 s with context size. A hung primary with a
#: healthy backup should not cost the 180 s single-model floor.
FALLBACK_FIRST_TOKEN_TIMEOUT_S = 60.0
#: …for large prompts, whose prefill alone can take tens of seconds.
FALLBACK_FIRST_TOKEN_TIMEOUT_LARGE_CONTEXT_S = 120.0
#: Estimated prompt tokens above which the larger deadline applies.
LARGE_CONTEXT_TOKENS = 100_000

#: Idle lifetime of a run's sticky entry — refreshed on every read/write, so
#: a long run keeps it; a finished run's entry ages out.
STICKY_TTL_S = 2 * 3600.0
#: Hard cap on remembered runs per process (LRU eviction beyond it).
STICKY_MAX_ENTRIES = 4096


def fallback_first_token_timeout(
    configured: float | None,
    messages: Sequence[BaseMessage],
    estimator: TokenEstimator,
) -> float | None:
    """The first-token deadline of a *streaming* attempt that has a backup
    group: ``configured`` capped at 60 s (120 s above 100k estimated prompt
    tokens). ``None`` / ``0`` (deadline disabled in the manifest) is kept."""
    if configured is None or configured <= 0:
        return configured
    cap = FALLBACK_FIRST_TOKEN_TIMEOUT_S
    if estimate_messages(messages, estimator) > LARGE_CONTEXT_TOKENS:
        cap = FALLBACK_FIRST_TOKEN_TIMEOUT_LARGE_CONTEXT_S
    return min(configured, cap)


def failover_reason(exc: BaseException) -> str:
    for cls, reason in _REASONS:
        if isinstance(exc, cls):
            return reason
    return "other"


def record_failover(from_group: str, to_group: str, reason: str) -> None:
    _llm_failover_total.labels(from_group=from_group, to_group=to_group, reason=reason).inc()


def observe_first_progress(provider_key: str, elapsed_s: float) -> None:
    _llm_first_progress_seconds.labels(provider_key=provider_key).observe(elapsed_s)


@dataclass(frozen=True)
class FailoverNotice:
    """One actual switch of a call from one provider group to the next."""

    from_group: str
    to_group: str
    reason: str

    def frame(self, step: int) -> dict[str, Any]:
        """The console-only ``llm_failover`` run-event payload: model names
        (the ``model`` part of ``provider:model``), the reason code and the
        agent step — nothing from the conversation."""
        return {
            "step": step,
            "from_model": _model_of(self.from_group),
            "to_model": _model_of(self.to_group),
            "reason": self.reason,
        }


def _model_of(group: str) -> str:
    return group.split(":", 1)[-1]


FailoverListener = Callable[[FailoverNotice], Awaitable[None]]

#: Set by the agent node around its LLM call (:func:`failover_frames`); a
#: ContextVar for the same reason as the router's delta sink — no threading
#: through the caller protocol. Other LLM calls (planner, reflection,
#: memory) leave it unset and record only the metric.
_failover_listener: ContextVar[FailoverListener | None] = ContextVar(
    "_llm_failover_listener", default=None
)


@contextmanager
def failover_frames(
    step: int, publish: Callable[[dict[str, Any]], Awaitable[None]] | None
) -> Iterator[None]:
    """While active, every actual switch publishes ``FailoverNotice.frame(step)``
    through ``publish`` (``None`` → nothing is published)."""
    listener: FailoverListener | None = None
    if publish is not None:
        sink = publish

        async def _publish(notice: FailoverNotice) -> None:
            await sink(notice.frame(step))

        listener = _publish
    token = _failover_listener.set(listener)
    try:
        yield
    finally:
        _failover_listener.reset(token)


async def announce_failover(from_group: str, to_group: str, reason: str) -> None:
    """A call moves from ``from_group`` to ``to_group``: count it and tell the
    run's listener, if any. The notice is best-effort — a failing publisher
    is logged, never allowed to fail the call or block the switch."""
    record_failover(from_group, to_group, reason)
    listener = _failover_listener.get()
    if listener is None:
        return
    try:
        await listener(FailoverNotice(from_group=from_group, to_group=to_group, reason=reason))
    except Exception:
        logger.warning("llm_router.failover_notice_failed", exc_info=True)


async def discard_streamed_attempt(sink: object | None) -> None:
    """The router abandons a streaming attempt whose deltas already reached
    ``sink``: tell a sink that supports it (``discard_attempt``) to drop that
    attempt's buffered state before the backup's deltas arrive."""
    discard = getattr(sink, "discard_attempt", None)
    if discard is None:
        return
    try:
        await discard()
    except Exception:
        logger.warning("llm_router.sink_discard_failed", exc_info=True)


def breaker_key_for(handle_key: str, secret_ref: str, base_url: str | None) -> str:
    """Breaker identity for one handle: ``<provider:model[#idx]>@<fingerprint>``.

    Two tenants can resolve the same ``provider:model`` to different API keys
    (tenant credential overrides) and a manifest can point ``self-hosted`` /
    ``azure`` models at its own ``base_url`` — different upstream health
    domains that must not share a breaker once the registry is process-wide.
    The fingerprint hashes the credential *reference* (never the secret) and
    the endpoint, truncated: stable, short, and reveals neither.
    """
    digest = hashlib.sha256(f"{secret_ref}\n{base_url or ''}".encode()).hexdigest()
    return f"{handle_key}@{digest[:12]}"


@dataclass(frozen=True)
class WalkPlan:
    """Handle indices in walk order. ``order[:boundary]`` is the primary walk
    (from the sticky group, or the chain head, to the end); ``order[boundary:]``
    the last-resort pass over the earlier groups the sticky start skipped."""

    groups: tuple[str, ...]
    order: tuple[int, ...]
    boundary: int

    @property
    def start_group(self) -> str:
        return self.groups[self.order[0]]

    def group_at(self, pos: int) -> str:
        return self.groups[self.order[pos]]

    def has_fallback(self, pos: int) -> bool:
        """Whether the E.4 middleware at ``pos`` should use the fast budget.

        In the primary walk: a *different* group still follows (sibling keys
        of the same group do not count — a provider outage hits them all).
        In the last-resort pass: always — those groups already failed in this
        run, so they get one fast, breaker-respecting try, not a full retry
        cycle; the sticky group before them is the last *healthy* option and
        keeps the full single-model budget.
        """
        if pos >= self.boundary:
            return True
        group = self.group_at(pos)
        return any(self.group_at(q) != group for q in range(pos + 1, self.boundary))

    def has_alternative(self, pos: int) -> bool:
        """Whether *any* handle follows ``pos`` — a sibling key or another
        group. The E.4 middleware refuses an OPEN breaker (and does not retry
        a 429 in place) only when there is somewhere else to go."""
        return pos + 1 < len(self.order)

    def can_redo_elsewhere(self, pos: int) -> bool:
        """PR2 — whether a reasoning-only stall at ``pos`` may be redone on
        another group: :meth:`has_fallback` semantics, and a different group
        really is left to walk to (the last handle of a last-resort pass
        has none)."""
        return self.has_fallback(pos) and self.next_group(pos) < len(self.order)

    def next_group(self, pos: int) -> int:
        """First position after ``pos`` in a different group (skip siblings)."""
        group = self.group_at(pos)
        nxt = pos + 1
        while nxt < len(self.order) and self.group_at(nxt) == group:
            nxt += 1
        return nxt


def plan_walk(groups: Sequence[str], sticky: str | None) -> WalkPlan:
    frozen = tuple(groups)
    n = len(frozen)
    if sticky is None or sticky not in frozen or sticky == frozen[0]:
        return WalkPlan(groups=frozen, order=tuple(range(n)), boundary=n)
    start = frozen.index(sticky)
    return WalkPlan(
        groups=frozen,
        order=tuple(range(start, n)) + tuple(range(start)),
        boundary=n - start,
    )


class StickyRouteStore:
    """``(run_id, chain) → group`` with an idle TTL and an LRU size cap.

    ``chain`` is the router's group tuple, so routers with different chains
    (step routers, the vision router) keep separate entries while two router
    instances with the same chain in one run (a rebuilt agent, a sub-agent
    on the parent's run) share one. Thread-safe; never awaits.
    """

    def __init__(
        self,
        *,
        ttl_s: float = STICKY_TTL_S,
        max_entries: int = STICKY_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[tuple[UUID, tuple[str, ...]], tuple[str, float]] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, run_id: UUID, chain: tuple[str, ...]) -> str | None:
        key = (run_id, chain)
        with self._lock:
            now = self._clock()
            entry = self._entries.get(key)
            if entry is None:
                return None
            group, expires = entry
            if expires <= now:
                del self._entries[key]
                return None
            self._entries[key] = (group, now + self._ttl_s)
            self._entries.move_to_end(key)
            return group

    def set(self, run_id: UUID, chain: tuple[str, ...], group: str) -> None:
        key = (run_id, chain)
        with self._lock:
            now = self._clock()
            self._entries[key] = (group, now + self._ttl_s)
            self._entries.move_to_end(key)
            self._evict(now)

    def clear(self, run_id: UUID, chain: tuple[str, ...]) -> None:
        with self._lock:
            self._entries.pop((run_id, chain), None)

    def _evict(self, now: float) -> None:
        # Entries are in last-touched order with a sliding TTL, so expired
        # ones sit at the front.
        while self._entries:
            _, (_, expires) = next(iter(self._entries.items()))
            if expires > now and len(self._entries) <= self._max_entries:
                break
            self._entries.popitem(last=False)


_DEFAULT_STICKY_STORE = StickyRouteStore()


def default_sticky_store() -> StickyRouteStore:
    """The process-wide store every :class:`LLMRouter` uses by default."""
    return _DEFAULT_STICKY_STORE


def remember_route(
    store: StickyRouteStore | None,
    run_id: UUID | None,
    plan: WalkPlan,
    sticky: str | None,
    served: str,
) -> None:
    """Record which group served this call of the run: a later group (an
    earlier one failed) becomes the run's start; the chain head clears it."""
    if store is None or run_id is None or served == sticky:
        return
    if served == plan.groups[0]:
        if sticky is not None:
            store.clear(run_id, plan.groups)
        return
    store.set(run_id, plan.groups, served)
    logger.info("llm_router.sticky_set group=%s", served)


async def redo_reasoning_only(
    provider_key: str,
    partial: AIMessage,
    sink: object | None,
    cause: str,
    can_redo: bool,
) -> None:
    """A streaming attempt stalled / failed after its first progress delta.

    With nothing visible yet (:func:`~orchestrator.llm.stall.has_visible_output`)
    and another group to walk to (``can_redo`` —
    :meth:`WalkPlan.can_redo_elsewhere`): discard the attempt (the sink drops
    its streamed reasoning) and raise :class:`LLMReasoningStallError` so the
    router redoes the call on the next group. Otherwise return, and the caller
    keeps today's behaviour (B-186 partial / ``LLMStreamInterruptedError``).
    """
    if not can_redo or has_visible_output(partial):
        return
    logger.warning(
        "llm_router.reasoning_only_stall key=%s cause=%s (redoing on the next group)",
        provider_key,
        cause,
    )
    await discard_streamed_attempt(sink)
    raise LLMReasoningStallError(
        f"provider {provider_key!r} stopped after reasoning only ({cause}); "
        "redoing the step on the next provider group"
    )
