"""Vendor-neutral pacing, concurrency, retry and time budget for rerank — B-206 K-7.

The seam: a vendor rerank client (:class:`~orchestrator.llm.rerank.RerankClient`
— DashScope's today, a Cohere-style one next) only *classifies* a failed request,
raising :class:`RerankCallError` with a :data:`RerankFailure` and, when the
vendor said, a ``retry_after_s``. :meth:`RerankGuards.wrap` puts any such client
behind the same guard, which decides everything else from the model catalog's
:class:`~expert_work.protocol.model_catalog.RerankCaps` alone:

* **pacing** — every HTTP request (retries included) takes the one token of a
  bucket per (model, credential) refilled every ``60 / rpm`` seconds: requests
  evenly spaced, no burst (:func:`pacing_spacing_s`). With Redis the bucket is
  B-44's global one (:class:`~orchestrator.llm.rate_limit_redis.RedisRpmLimiter`,
  shared by every replica); without it, or while Redis is down, each replica
  takes its share (one request every ``60 * replicas / rpm`` seconds). A model
  the catalog gives no ``rpm`` is not paced (warned once): it keeps the
  concurrency cap and the 429 retry;
* **concurrency** — at most ``max_concurrency`` requests per model in flight in
  this process, across searches and both views of one;
* **retry** — ``rate_limited`` / ``transient`` failures back off ``0.5 s x 2^n``
  (at least 1 s after a ``rate_limited`` one; + up to 25 % jitter, cap 8 s, the
  vendor's ``Retry-After`` when longer) at most three times; ``rejected`` /
  ``too_large`` go straight back to the caller;
* **budget** — everything one search's rerank requests wait for or retry fits
  in its :func:`~orchestrator.tools.knowledge_rerank.rerank_budget`, and no
  request or backoff starts unless a request (:data:`MIN_REQUEST_S`) still fits
  after it; otherwise, or when retries run out, the request raises
  :class:`~orchestrator.tools.knowledge_rerank.RerankUnavailableError`.

2026-10-22 probe (``qwen3-vl-rerank``, catalog 600 RPM, native endpoint, minimal
requests): of 60 simultaneous requests 10 passed and 50 got HTTP 429 with body
``{"code": "Throttling.RateQuota", "message": "Requests rate limit exceeded,
please try again later. ..."}`` in 10-170 ms; no ``Retry-After`` or rate-limit
header. A request 0.5 s after that burst was still refused, one 1.05 s after
passed. 20 requests one per 100 ms all passed; one per 80 ms, 1 of 20 was
refused. The vendor's window is not a refilling bucket: a bucket of ``rpm / 60``
tokens refilled at ``rpm / 60`` a second admits nearly twice that in its first
second (10 + 10 at 600 RPM), which the vendor refuses. Even spacing at the
measured-safe 100 ms never bursts.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from aiolimiter import AsyncLimiter

from expert_work.protocol.model_catalog import catalog_entry
from orchestrator.llm.rate_limit import AdmissionLimiter, replica_share
from orchestrator.llm.rate_limit_redis import RedisRpmLimiter, bucket_key
from orchestrator.tools.knowledge_rerank import (
    RERANK_BUDGET_S,
    RerankBudget,
    RerankError,
    RerankOutcome,
    RerankUnavailableError,
    current_rerank_budget,
)

if TYPE_CHECKING:
    import redis.asyncio as redis_async

logger = logging.getLogger(__name__)


@runtime_checkable
class RerankClient(Protocol):
    """The one rerank endpoint we use — sized so tests fake it without httpx.

    ``instruction`` (B-206 K-14) is passed only when there is one, so a client
    written before it existed keeps working. A failed request raises
    :class:`~orchestrator.llm.rerank_guard.RerankCallError` saying what kind of
    failure it was (B-206 K-7) — the one thing
    :class:`~orchestrator.llm.rerank_guard.RerankGuards` needs from a vendor
    client to pace, retry and give up for it."""

    async def rerank(
        self,
        *,
        model: str,
        query: str,
        documents: Sequence[str],
        top_n: int,
        instruction: str | None = None,
    ) -> Mapping[str, Any]:
        """POST the rerank endpoint; return the parsed JSON body."""


#: How a rerank request failed, as the vendor client judged it:
#: ``rate_limited`` the vendor asked us to slow down (HTTP 429);
#: ``transient`` a failure that may clear on its own (HTTP 408 / 5xx, a timeout,
#: a refused or reset connection); ``rejected`` any other refusal — it would be
#: refused again; ``too_large`` refused for its size — the reranker may send less.
RerankFailure = Literal["rate_limited", "transient", "rejected", "too_large"]
_RETRYABLE: frozenset[RerankFailure] = frozenset({"rate_limited", "transient"})

#: Requests in flight per rerank model in one process when the catalog does not
#: say. Above what the catalogued 600 RPM model can use (10 per second x about
#: 0.6 s each, measured 2026-10-22), so pacing stays the binding limit there;
#: low enough that a burst of searches cannot open dozens of connections to a
#: self-hosted reranker.
DEFAULT_RERANK_CONCURRENCY = 8
MAX_RETRIES = 3
BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 8.0
#: The shortest wait after an HTTP 429: the vendor's window is one second (probe:
#: refused again 0.5 s later, passed 1.05 s later).
RATE_LIMITED_MIN_BACKOFF_S = 1.0
#: The time a rerank request needs; a request or backoff that would leave less
#: than this before the budget's deadline is not started. Measured 2026-10-22:
#: 0.47-0.69 s for 100 passages of 600 characters, 0.36-0.38 s for 20; 1 s
#: covers the slowest with about 45 % to spare. A request cut off mid-flight has
#: still been paid for and still fails the search.
MIN_REQUEST_S = 1.0
#: A backoff is lengthened by up to this fraction, drawn per retry, so requests
#: refused together do not come back together.
_JITTER = 0.25
_SECONDS_PER_MINUTE = 60


class RerankCallError(RerankError):
    """A rerank request the vendor refused or that could not reach it, as the
    vendor client classifies it (see :data:`RerankFailure`). ``status`` is the
    HTTP status when there was one; ``retry_after_s`` how long the vendor asked
    us to wait, when it said. As a :class:`RerankError` it reports
    ``rate_limited`` for a ``rate_limited`` failure and ``model_error``
    otherwise, guarded or not."""

    def __init__(
        self,
        message: str,
        *,
        failure: RerankFailure,
        status: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(
            message, outcome="rate_limited" if failure == "rate_limited" else "model_error"
        )
        self.failure: RerankFailure = failure
        self.status = status
        self.retry_after_s = retry_after_s


def status_failure(status_code: int) -> RerankFailure:
    """The failure an HTTP error status means for any rerank vendor; a vendor
    client refines it from its own error body (DashScope: see
    :func:`orchestrator.llm.rerank._failure`)."""
    if status_code == 429:
        return "rate_limited"
    if status_code == 408 or status_code >= 500:
        return "transient"
    return "rejected"


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """A ``Retry-After`` header's delay in seconds; ``None`` when absent or not
    a non-negative number (the HTTP-date form is not worth parsing here: the
    backoff applies instead)."""
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    return seconds if seconds >= 0 and math.isfinite(seconds) else None


def pacing_spacing_s(rpm: int) -> float:
    """Seconds between two requests to a model allowed ``rpm`` a minute."""
    return _SECONDS_PER_MINUTE / rpm


def backoff_s(retry: int, failure: RerankFailure, *, rand: Callable[[], float]) -> float:
    """The wait before retry number ``retry`` (0-based) after ``failure``:
    ``0.5 s x 2^retry``, at least :data:`RATE_LIMITED_MIN_BACKOFF_S` after a
    ``rate_limited`` one, capped at 8 s, lengthened by up to 25 % jitter."""
    base = BACKOFF_BASE_S * 2.0**retry
    if failure == "rate_limited":
        base = max(base, RATE_LIMITED_MIN_BACKOFF_S)
    return min(BACKOFF_CAP_S, base) * (1 + _JITTER * rand())


class PacingBuckets(Protocol):
    """Builds the pacing bucket for one (model, credential) allowed ``rpm`` a
    minute: one token, refilled every :func:`pacing_spacing_s` seconds."""

    def __call__(self, *, bucket: str, rpm: int) -> AdmissionLimiter:
        """Return the bucket ``bucket`` (a key name, see
        :func:`~orchestrator.llm.rate_limit_redis.bucket_key`)."""


def local_pacing_buckets(*, bucket: str, rpm: int) -> AdmissionLimiter:
    """Per-process buckets — no Redis: this replica's share of the rate, one
    request every ``60 * replicas / rpm`` seconds
    (:func:`~orchestrator.llm.rate_limit.replica_share`)."""
    del bucket
    per_period, period_s = replica_share(1, pacing_spacing_s(rpm))
    return AsyncLimiter(max_rate=per_period, time_period=period_s)


def redis_pacing_buckets(redis_client: redis_async.Redis) -> PacingBuckets:
    """Global buckets in Redis, shared by every replica (B-44); while Redis is
    down each falls back to this replica's share, as :func:`local_pacing_buckets`."""

    def _build(*, bucket: str, rpm: int) -> AdmissionLimiter:
        return RedisRpmLimiter(
            redis_client=redis_client,
            bucket_key=bucket,
            rate_limit_rpm=1,
            time_period_s=pacing_spacing_s(rpm),
        )

    return _build


class _Unpaced:
    """The admission limiter of a model the catalog gives no rate: admits at once."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


@dataclass(eq=False)
class RerankGuards:
    """The process's rerank guard state: one concurrency cap per model and one
    pacing bucket per (model, credential), built on first use and kept for the
    process — hold one instance per process (the control plane builds it at
    startup) so every reranker shares them. Compared and hashed by identity."""

    buckets: PacingBuckets = local_pacing_buckets
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic
    rand: Callable[[], float] = random.random
    min_request_s: float = MIN_REQUEST_S
    _slots: dict[tuple[str, str], asyncio.Semaphore] = field(default_factory=dict, init=False)
    _paces: dict[tuple[str, str, str], AdmissionLimiter] = field(default_factory=dict, init=False)
    _unpaced_warned: set[tuple[str, str]] = field(default_factory=set, init=False)

    def wrap(
        self, client: RerankClient, *, provider: str, model: str, secret_ref: str
    ) -> GuardedRerankClient:
        """``client`` behind this process's guard for ``provider/model`` called
        with the credential ``secret_ref`` (a reference, never the key)."""
        entry = catalog_entry(provider, model)
        caps = entry.rerank_caps if entry is not None else None
        slots = self._slots.get((provider, model))
        if slots is None:
            limit = caps.max_concurrency if caps is not None else None
            slots = asyncio.Semaphore(limit or DEFAULT_RERANK_CONCURRENCY)
            self._slots[(provider, model)] = slots
        rpm = caps.rpm if caps is not None else None
        return GuardedRerankClient(
            inner=client,
            slots=slots,
            pace=self._pace(provider, model, secret_ref, rpm),
            guards=self,
        )

    def _pace(
        self, provider: str, model: str, secret_ref: str, rpm: int | None
    ) -> AdmissionLimiter:
        if rpm is None:
            if (provider, model) not in self._unpaced_warned:
                self._unpaced_warned.add((provider, model))
                logger.warning(
                    "rerank.unpaced model=%s/%s — the model catalog gives no rpm: requests "
                    "are not paced, only capped in concurrency and retried on HTTP 429",
                    provider,
                    model,
                )
            return _Unpaced()
        pace = self._paces.get((provider, model, secret_ref))
        if pace is None:
            pace = self.buckets(
                bucket=bucket_key(key=f"rerank:{provider}:{model}", secret_ref=secret_ref),
                rpm=rpm,
            )
            self._paces[(provider, model, secret_ref)] = pace
        return pace


@dataclass(frozen=True)
class GuardedRerankClient:
    """A :class:`~orchestrator.llm.rerank.RerankClient` behind its model's
    guard (see the module docstring and :meth:`RerankGuards.wrap`)."""

    inner: RerankClient
    slots: asyncio.Semaphore
    pace: AdmissionLimiter
    guards: RerankGuards

    async def rerank(
        self,
        *,
        model: str,
        query: str,
        documents: Sequence[str],
        top_n: int,
        instruction: str | None = None,
    ) -> Mapping[str, Any]:
        clock = self.guards.clock
        need = self.guards.min_request_s
        budget = current_rerank_budget() or RerankBudget(deadline=clock() + RERANK_BUDGET_S)
        extra: dict[str, Any] = {} if instruction is None else {"instruction": instruction}
        last: RerankFailure | None = None
        retry = 0
        while True:
            remaining = budget.deadline - clock()
            if remaining <= 0 or remaining < need:
                raise _gave_up(model, last, "too little of its time budget is left")
            phase = "slot"
            cut = asyncio.timeout(remaining)
            try:
                async with cut, self.slots:
                    phase = "pace"
                    async with self.pace:
                        phase = "call"
                        budget.attempts += 1
                        return await self.inner.rerank(
                            model=model, query=query, documents=documents, top_n=top_n, **extra
                        )
            except TimeoutError as exc:
                if not cut.expired():
                    raise
                # Waiting for a pacing token → the rate kept it out; for a
                # concurrency slot or mid-request → too slow, not rate-limited.
                failure: RerankFailure = "rate_limited" if phase == "pace" else "transient"
                raise _gave_up(model, failure, f"the time budget ran out ({phase})") from exc
            except RerankCallError as exc:
                if exc.failure not in _RETRYABLE:
                    raise
                last = exc.failure
                if retry >= MAX_RETRIES:
                    raise _gave_up(model, last, f"{MAX_RETRIES} retries failed") from exc
                delay = backoff_s(retry, last, rand=self.guards.rand)
                if exc.retry_after_s is not None:
                    delay = max(delay, exc.retry_after_s)
                if delay + need > budget.deadline - clock():
                    raise _gave_up(model, last, "no request would fit after its backoff") from exc
                logger.info(
                    "rerank.retry model=%s failure=%s status=%s retry=%d delay_s=%.2f",
                    model,
                    last,
                    exc.status,
                    retry + 1,
                    delay,
                )
                await self.guards.sleep(delay)
                retry += 1


def _gave_up(model: str, failure: RerankFailure | None, why: str) -> RerankUnavailableError:
    """The error a guarded request ends with: ``rate_limited`` only when the
    rate is shown to be the cause (an HTTP 429, or waiting for a pacing token),
    ``model_error`` otherwise."""
    outcome: RerankOutcome = "rate_limited" if failure == "rate_limited" else "model_error"
    return RerankUnavailableError(f"rerank {model} gave up: {why}", outcome=outcome)
