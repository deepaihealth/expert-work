"""Per-upstream LLM circuit breaker — Stream E.4, reworked in B-297.

The E.4 breaker counted one failure per *exhausted call* (≈ 5 calls x 4
attempts before it tripped), reset on any 4xx, ignored first-token timeouts
and let every caller through once half-open — in a one-hour vendor 5xx
outage it never opened. This module follows the common industry defaults
(Envoy outlier detection, resilience4j, LiteLLM cooldowns):

* **every failed upstream attempt counts** — each retry, first-token
  timeouts, post-first-token stream failures, 5xx, network, 429; a 4xx
  client error is *neutral* (neither a failure nor a reset);
* **OPEN** after ``failure_threshold`` (3) consecutive failures, or a
  failure rate ≥ ``failure_rate`` (50 %) over ≥ ``min_attempts`` (5)
  attempts inside a ``window_s`` (60 s) sliding window;
* **cooldown** ``cooldown_s`` (30 s), doubling on each consecutive re-open
  up to ``max_cooldown_s`` (300 s), back to 30 s after a successful probe;
  a vendor ``Retry-After`` longer than the cooldown stretches it (same cap);
* **HALF_OPEN** admits exactly one in-flight probe; everyone else is
  refused until the probe resolves.

State is per process (one :class:`BreakerRegistry` per control-plane
process, shared by every agent / sub-agent / run); replicas do not share it.
That is deliberate: each replica learns an outage from its own first 3
failures, which costs a few seconds, and needs no Redis round trip on the
hot path.

All methods are ``async`` for API compatibility but never await while
holding the lock — a :class:`threading.Lock` keeps one registry safe to
share across event loops and threads.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Literal

logger = logging.getLogger(__name__)

BreakerState = Literal["CLOSED", "OPEN", "HALF_OPEN"]
#: ``allowed`` — normal call; ``probe`` — the single half-open trial call;
#: ``rejected`` — breaker open (or a probe already in flight).
Admission = Literal["allowed", "probe", "rejected"]

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_FAILURE_RATE = 0.5
DEFAULT_MIN_ATTEMPTS = 5
DEFAULT_WINDOW_S = 60.0
DEFAULT_COOLDOWN_S = 30.0
DEFAULT_MAX_COOLDOWN_S = 300.0


class CircuitBreaker:
    """Three-state breaker for one upstream (see module docstring).

    Callers bracket every upstream attempt with :meth:`acquire` and exactly
    one of :meth:`record_success` / :meth:`record_failure` / :meth:`release`,
    passing ``probe=True`` when :meth:`acquire` returned ``"probe"``.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        failure_rate: float = DEFAULT_FAILURE_RATE,
        min_attempts: int = DEFAULT_MIN_ATTEMPTS,
        window_s: float = DEFAULT_WINDOW_S,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        max_cooldown_s: float = DEFAULT_MAX_COOLDOWN_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._failure_rate = failure_rate
        self._min_attempts = min_attempts
        self._window_s = window_s
        self._cooldown_s = cooldown_s
        self._max_cooldown_s = max_cooldown_s
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._state: BreakerState = "CLOSED"
        self._consecutive_failures = 0
        self._outcomes: deque[tuple[float, bool]] = deque()
        self._open_until = 0.0
        self._open_streak = 0
        self._probe_in_flight = False

    # -- reads ---------------------------------------------------------------

    async def check_state(self) -> BreakerState:
        """Current state; an elapsed OPEN reads as HALF_OPEN (no probe claimed)."""
        with self._lock:
            self._maybe_half_open(self._clock())
            return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    # -- admission -----------------------------------------------------------

    async def acquire(self) -> Admission:
        """Admit or refuse one upstream attempt."""
        with self._lock:
            self._maybe_half_open(self._clock())
            if self._state == "CLOSED":
                return "allowed"
            if self._state == "HALF_OPEN" and not self._probe_in_flight:
                self._probe_in_flight = True
                logger.info("circuit_breaker.probe")
                return "probe"
            return "rejected"

    # -- outcomes ------------------------------------------------------------

    async def record_success(self, *, probe: bool = False) -> None:
        with self._lock:
            now = self._clock()
            self._consecutive_failures = 0
            if probe:
                self._probe_in_flight = False
            if probe or self._state == "HALF_OPEN":
                self._state = "CLOSED"
                self._open_streak = 0
                self._outcomes.clear()
                logger.info("circuit_breaker.closed")
                return
            if self._state == "CLOSED":
                self._push(now, ok=True)

    async def record_failure(
        self, *, probe: bool = False, retry_after_s: float | None = None
    ) -> bool:
        """Count one failed attempt. Returns ``True`` when this failure opened
        the breaker (callers emit the open metric / stop retrying)."""
        with self._lock:
            now = self._clock()
            self._consecutive_failures += 1
            if probe:
                self._probe_in_flight = False
                if self._state == "HALF_OPEN":
                    self._open(now, retry_after_s)
                    return True
            if self._state != "CLOSED":
                # Stale evidence from a call admitted before the breaker
                # opened — only the probe decides a half-open breaker.
                return False
            self._push(now, ok=False)
            if self._consecutive_failures >= self._failure_threshold or self._rate_tripped():
                self._open(now, retry_after_s)
                return True
            return False

    async def release(self, *, probe: bool) -> None:
        """Neutral outcome (4xx client error, cancellation): frees the probe
        slot without counting a success or a failure."""
        if probe:
            with self._lock:
                self._probe_in_flight = False

    # -- internals (lock held) ----------------------------------------------

    def _maybe_half_open(self, now: float) -> None:
        if self._state == "OPEN" and now >= self._open_until:
            self._state = "HALF_OPEN"
            self._probe_in_flight = False
            logger.info("circuit_breaker.half_open")

    def _push(self, now: float, *, ok: bool) -> None:
        self._outcomes.append((now, ok))
        horizon = now - self._window_s
        while self._outcomes and self._outcomes[0][0] < horizon:
            self._outcomes.popleft()

    def _rate_tripped(self) -> bool:
        total = len(self._outcomes)
        if total < self._min_attempts:
            return False
        failures = sum(1 for _, ok in self._outcomes if not ok)
        return failures / total >= self._failure_rate

    def _open(self, now: float, retry_after_s: float | None) -> None:
        self._open_streak += 1
        cooldown = min(self._max_cooldown_s, self._cooldown_s * 2 ** (self._open_streak - 1))
        if retry_after_s is not None and retry_after_s > cooldown:
            cooldown = min(self._max_cooldown_s, retry_after_s)
        self._state = "OPEN"
        self._open_until = now + cooldown
        logger.warning(
            "circuit_breaker.open consecutive_failures=%d cooldown_s=%.1f",
            self._consecutive_failures,
            cooldown,
        )


class BreakerRegistry:
    """Per-key circuit breakers, lazily constructed.

    The key identifies one upstream health domain — the router passes
    ``provider:model[#idx]`` plus a credential/endpoint fingerprint (see
    ``ProviderHandle.breaker_key``). Cardinality is bounded by the platform's
    configured (model, credential, endpoint) triples, so it never evicts.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    async def get(self, key: str) -> CircuitBreaker:
        with self._lock:
            breaker = self._breakers.get(key)
            if breaker is None:
                breaker = CircuitBreaker(
                    failure_threshold=self._failure_threshold,
                    cooldown_s=self._cooldown_s,
                    clock=self._clock,
                )
                self._breakers[key] = breaker
            return breaker
