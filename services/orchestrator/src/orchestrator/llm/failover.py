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
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from expert_work.common.observability import expert_work_counter
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

logger = logging.getLogger(__name__)

_llm_failover_total = expert_work_counter(
    "expert_work_llm_failover_total",
    "LLM calls that moved from one provider group to another (B-297). "
    "reason=sticky: the run had already failed over, the call started there.",
    ("from_group", "to_group", "reason"),
)

#: Reason vocabulary (bounded label values), most specific class first.
_REASONS: tuple[tuple[type[BaseException], str], ...] = (
    (CircuitOpenError, "circuit_open"),
    (LLMKeyUnavailableError, "key_unavailable"),
    (LLMAuthError, "auth"),
    (LLMUnauthorizedError, "auth"),
    (LLMRateLimitError, "rate_limit"),
    (LLMStreamStaleError, "first_token_timeout"),
    (LLMNetworkError, "network"),
    (LLMServerError, "server_error"),
)

#: Idle lifetime of a run's sticky entry — refreshed on every read/write, so
#: a long run keeps it; a finished run's entry ages out.
STICKY_TTL_S = 2 * 3600.0
#: Hard cap on remembered runs per process (LRU eviction beyond it).
STICKY_MAX_ENTRIES = 4096


def failover_reason(exc: BaseException) -> str:
    for cls, reason in _REASONS:
        if isinstance(exc, cls):
            return reason
    return "other"


def record_failover(from_group: str, to_group: str, reason: str) -> None:
    _llm_failover_total.labels(from_group=from_group, to_group=to_group, reason=reason).inc()


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
