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
  refused until the probe resolves. The E.4 middleware resolves a streaming
  probe at its first progress delta (the upstream answers — no need to hold
  the slot for a whole generation); a probe that neither progresses nor ends
  within its lease (``probe_lease_s`` = 60 s, or the attempt's own first-token
  / non-streaming deadline if longer) counts as a failed probe and re-opens
  the breaker;
* **fresh successes close** the breaker — the probe's, or a single-model
  caller's let through the open breaker (its answer is evidence the upstream
  works). The late success of a call admitted *before* the breaker opened is
  ignored while it is not CLOSED.
* every transition to OPEN increments ``expert_work_llm_breaker_open_total``
  and logs ``llm_breaker.open`` (here, so lease-expiry opens count too).

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
from dataclasses import dataclass
from typing import Literal

from expert_work.common.observability.metrics import expert_work_counter

logger = logging.getLogger(__name__)

# One increment per transition to OPEN. Labelled by the router's
# ``provider_key`` (``provider:model[#idx]``) — never the breaker key, whose
# credential fingerprint would make the label set grow with tenants.
_breaker_open_total = expert_work_counter(
    "expert_work_llm_breaker_open_total",
    "LLM upstream circuit breakers that opened (B-297).",
    ("provider_key",),
)

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
DEFAULT_PROBE_LEASE_S = 60.0


@dataclass(frozen=True)
class Permit:
    """One admitted (or refused) attempt. ``probe_id`` identifies a half-open
    probe, so the outcome of a probe whose lease already expired cannot
    resolve — or free the slot of — the probe that replaced it."""

    admission: Admission
    probe_id: int = 0

    @property
    def is_probe(self) -> bool:
        return self.admission == "probe"


class CircuitBreaker:
    """Three-state breaker for one upstream (see module docstring).

    Callers bracket every upstream attempt with :meth:`admit` and exactly
    one of :meth:`record_success` / :meth:`record_failure` / :meth:`release`,
    passing the :class:`Permit` back. (:meth:`acquire` and ``probe=True``
    are the permit-less shorthand: "the probe currently in flight".)
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
        probe_lease_s: float = DEFAULT_PROBE_LEASE_S,
        clock: Callable[[], float] | None = None,
        key: str = "",
        label: str = "",
    ) -> None:
        #: ``key`` (breaker identity, may carry a fingerprint) and ``label``
        #: (bounded metric label, the router's ``provider:model[#idx]``) are
        #: only used for the open log / ``expert_work_llm_breaker_open_total``.
        self.key = key
        self.label = label
        self._failure_threshold = failure_threshold
        self._failure_rate = failure_rate
        self._min_attempts = min_attempts
        self._window_s = window_s
        self._cooldown_s = cooldown_s
        self._max_cooldown_s = max_cooldown_s
        self._probe_lease_s = probe_lease_s
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._state: BreakerState = "CLOSED"
        self._consecutive_failures = 0
        self._outcomes: deque[tuple[float, bool]] = deque()
        self._open_until = 0.0
        self._open_streak = 0
        self._probe_in_flight = False
        self._probe_id = 0
        self._probe_started = 0.0
        self._probe_lease_current = probe_lease_s

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

    async def admit(self, *, probe_lease_s: float | None = None) -> Permit:
        """Admit or refuse one upstream attempt.

        ``probe_lease_s`` — how long this attempt may legitimately go without
        progress (the router's first-token / non-streaming deadline); a probe's
        lease is never shorter than it, so a healthy slow probe is not declared
        hung."""
        with self._lock:
            now = self._clock()
            self._maybe_half_open(now)
            if self._state == "CLOSED":
                return Permit("allowed")
            if self._state == "HALF_OPEN" and not self._probe_in_flight:
                self._probe_in_flight = True
                self._probe_id += 1
                self._probe_started = now
                self._probe_lease_current = max(self._probe_lease_s, probe_lease_s or 0.0)
                logger.info("circuit_breaker.probe")
                return Permit("probe", self._probe_id)
            return Permit("rejected")

    async def acquire(self) -> Admission:
        """:meth:`admit` without the permit (tests / simple callers)."""
        return (await self.admit()).admission

    # -- outcomes ------------------------------------------------------------

    async def record_success(self, *, probe: bool = False, permit: Permit | None = None) -> None:
        """Count one success. A success closes a non-CLOSED breaker when it is
        fresh evidence the upstream answers — a probe, or a single-model caller
        let through the open breaker (a ``"rejected"`` permit). The success of
        a call admitted (``"allowed"``) before the breaker opened is stale and
        ignored there: long streams finishing during an overload outage would
        otherwise keep re-closing it and the cooldown would never grow."""
        with self._lock:
            now = self._clock()
            if self._is_current_probe(probe, permit):
                self._probe_in_flight = False
            if self._state != "CLOSED" and permit is not None and permit.admission == "allowed":
                return
            self._consecutive_failures = 0
            if self._state != "CLOSED":
                self._state = "CLOSED"
                self._open_streak = 0
                self._outcomes.clear()
                logger.info("circuit_breaker.closed")
                return
            self._push(now, ok=True)

    async def record_failure(
        self,
        *,
        probe: bool = False,
        permit: Permit | None = None,
        retry_after_s: float | None = None,
    ) -> bool:
        """Count one failed attempt. Returns ``True`` when this failure opened
        the breaker (callers emit the open metric / stop retrying)."""
        with self._lock:
            now = self._clock()
            self._consecutive_failures += 1
            if self._is_current_probe(probe, permit):
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

    async def release(self, *, probe: bool = False, permit: Permit | None = None) -> None:
        """Neutral outcome (4xx client error, cancellation): frees the probe
        slot without counting a success or a failure."""
        with self._lock:
            if self._is_current_probe(probe, permit):
                self._probe_in_flight = False

    # -- internals (lock held) ----------------------------------------------

    def _is_current_probe(self, probe: bool, permit: Permit | None) -> bool:
        if permit is None:
            return probe
        return permit.is_probe and permit.probe_id == self._probe_id and self._probe_in_flight

    def _maybe_half_open(self, now: float) -> None:
        if self._state == "OPEN" and now >= self._open_until:
            self._state = "HALF_OPEN"
            self._probe_in_flight = False
            logger.info("circuit_breaker.half_open")
        elif (
            self._state == "HALF_OPEN"
            and self._probe_in_flight
            and now - self._probe_started >= self._probe_lease_current
        ):
            # The probe neither progressed nor finished within its lease:
            # the upstream is not answering — a failed probe.
            self._probe_in_flight = False
            logger.warning("circuit_breaker.probe_lease_expired")
            self._open(now, None)

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
        # Every transition to OPEN — failure threshold, failed probe, expired
        # probe lease — is counted and logged here, in one place.
        _breaker_open_total.labels(provider_key=self.label or "unlabelled").inc()
        logger.warning(
            "circuit_breaker.open consecutive_failures=%d cooldown_s=%.1f",
            self._consecutive_failures,
            cooldown,
        )
        logger.warning(
            "llm_breaker.open provider_key=%s breaker_key=%s consecutive_failures=%d",
            self.label,
            self.key,
            self._consecutive_failures,
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

    async def get(self, key: str, *, label: str = "") -> CircuitBreaker:
        """The breaker for ``key``; ``label`` (bounded, e.g. ``provider:model``)
        names it in the open metric / log."""
        with self._lock:
            breaker = self._breakers.get(key)
            if breaker is None:
                breaker = CircuitBreaker(
                    failure_threshold=self._failure_threshold,
                    cooldown_s=self._cooldown_s,
                    clock=self._clock,
                    key=key,
                    label=label,
                )
                self._breakers[key] = breaker
            elif label and not breaker.label:
                breaker.label = label
            return breaker
