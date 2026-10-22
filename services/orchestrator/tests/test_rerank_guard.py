"""B-206 K-7 — the vendor-neutral rerank guard: pacing by the catalog's rate,
a per-model concurrency cap, retry with backoff, and one search's time budget.

No network and no real sleeping: the guard's clock, sleep and jitter are
injected; the budget-cut tests wait on a never-set event for a few
milliseconds of a real deadline. The bucket's shape (even spacing, replica
share) is pinned in ``test_rerank_guard_pacing.py``."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Any

import pytest

from expert_work.protocol.model_catalog import ModelEntry, RerankCaps
from orchestrator.llm.rerank_guard import (
    BACKOFF_BASE_S,
    DEFAULT_RERANK_CONCURRENCY,
    MAX_RETRIES,
    MIN_REQUEST_S,
    RATE_LIMITED_MIN_BACKOFF_S,
    RerankCallError,
    RerankGuards,
    retry_after_seconds,
    status_failure,
)
from orchestrator.tools.knowledge_rerank import (
    RerankError,
    RerankUnavailableError,
    rerank_budget,
)

_MODEL = "qwen3-vl-rerank"
_OK: Mapping[str, Any] = {"output": {"results": [{"index": 0, "relevance_score": 0.9}]}}


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _Sleeps:
    """Records each backoff and moves the fake clock past it."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.clock.now += delay


class _Bucket:
    """An admission limiter that counts the tokens taken."""

    def __init__(self, *, blocked: bool = False) -> None:
        self.taken = 0
        self._open = asyncio.Event()
        if not blocked:
            self._open.set()

    async def __aenter__(self) -> None:
        await self._open.wait()
        self.taken += 1

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _BucketFactory:
    def __init__(self, bucket: _Bucket | None = None) -> None:
        self.bucket = bucket or _Bucket()
        self.built: list[tuple[str, int]] = []

    def __call__(self, *, bucket: str, rpm: int) -> _Bucket:
        self.built.append((bucket, rpm))
        return self.bucket


class _Scripted:
    """A rerank client answering from a script: an exception is raised, a
    mapping returned; the last entry repeats."""

    def __init__(self, *script: Exception | Mapping[str, Any]) -> None:
        self.script = list(script) or [_OK]
        self.calls: list[dict[str, Any]] = []

    async def rerank(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(kwargs)
        step = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(step, Exception):
            raise step
        return step


def _guards(
    factory: _BucketFactory | None = None,
    *,
    clock: _Clock | None = None,
    rand: float = 0.0,
    min_request_s: float = MIN_REQUEST_S,
) -> tuple[RerankGuards, _Sleeps, _BucketFactory]:
    clock = clock or _Clock()
    sleeps = _Sleeps(clock)
    factory = factory or _BucketFactory()
    guards = RerankGuards(
        buckets=factory,
        sleep=sleeps,
        clock=clock,
        rand=lambda: rand,
        min_request_s=min_request_s,
    )
    return guards, sleeps, factory


async def _call(client: Any, documents: Sequence[str] = ("a",)) -> Mapping[str, Any]:
    body: Mapping[str, Any] = await client.rerank(
        model=_MODEL, query="q", documents=list(documents), top_n=1
    )
    return body


def _err(failure: str, **kwargs: Any) -> RerankCallError:
    return RerankCallError(f"{failure} failure", failure=failure, **kwargs)  # type: ignore[arg-type]


# --- pacing -----------------------------------------------------------------


async def test_every_request_and_every_retry_takes_one_pacing_token() -> None:
    guards, _sleeps, factory = _guards()
    inner = _Scripted(_err("rate_limited"), _OK)
    client = guards.wrap(inner, provider="qwen", model=_MODEL, secret_ref="secret://k")
    await _call(client)
    assert factory.bucket.taken == 2  # the first try and its retry
    await _call(client)
    assert factory.bucket.taken == 3


async def test_the_bucket_is_sized_from_the_catalog_and_keyed_by_model_and_credential() -> None:
    guards, _sleeps, factory = _guards()
    guards.wrap(_Scripted(), provider="qwen", model=_MODEL, secret_ref="secret://tenant-a")
    guards.wrap(_Scripted(), provider="qwen", model=_MODEL, secret_ref="secret://tenant-a")
    guards.wrap(_Scripted(), provider="qwen", model=_MODEL, secret_ref="secret://tenant-b")
    # catalog: qwen3-vl-rerank 600 RPM; one bucket per credential, reused
    assert factory.built == [
        ("rl:llm:rerank:qwen:qwen3-vl-rerank:secret://tenant-a", 600),
        ("rl:llm:rerank:qwen:qwen3-vl-rerank:secret://tenant-b", 600),
    ]


async def test_a_model_the_catalog_gives_no_rate_is_not_paced_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """B-206 K-7a (M4) — an uncatalogued rerank model was unthrottled before K-7;
    a made-up default rate would hold it to one request a second platform-wide.
    It keeps the concurrency cap and the 429 retry, and the log says so once."""
    guards, sleeps, factory = _guards()
    inner = _Scripted(_err("rate_limited"), _OK)
    with caplog.at_level(logging.WARNING, logger="orchestrator.llm.rerank_guard"):
        for _ in range(2):
            client = guards.wrap(inner, provider="qwen", model="gte-rerank-v2", secret_ref="s")
            await _call(client)
    assert factory.built == []  # no bucket
    assert factory.bucket.taken == 0
    assert sleeps.delays == [RATE_LIMITED_MIN_BACKOFF_S]  # the 429 was still retried
    warnings = [r.getMessage() for r in caplog.records if "rerank.unpaced" in r.getMessage()]
    assert len(warnings) == 1
    assert "gte-rerank-v2" in warnings[0]


def test_the_guard_state_is_hashable_so_rerankers_holding_it_are() -> None:
    """B-206 K-7a (L5) — a frozen reranker dataclass hashes its fields."""
    assert len({RerankGuards(), RerankGuards()}) == 2


# --- concurrency --------------------------------------------------------------


class _Gate:
    """A rerank client that holds every request until released and records
    how many were in flight at once."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.in_flight = 0
        self.peak = 0

    async def rerank(self, **_kwargs: Any) -> Mapping[str, Any]:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await self.release.wait()
        finally:
            self.in_flight -= 1
        return _OK


async def _peak_in_flight(guards: RerankGuards, model: str, requests: int) -> int:
    gate = _Gate()
    # Two wraps of one model (two searches, or a search's two views) share the cap.
    clients = [
        guards.wrap(gate, provider="qwen", model=model, secret_ref=f"secret://{i % 2}")
        for i in range(requests)
    ]
    tasks = [asyncio.create_task(_call(client)) for client in clients]
    for _ in range(5):
        await asyncio.sleep(0)
    peak = gate.peak
    gate.release.set()
    await asyncio.gather(*tasks)
    return peak


async def test_requests_in_flight_never_exceed_the_default_cap() -> None:
    guards, _sleeps, _factory = _guards()
    peak = await _peak_in_flight(guards, _MODEL, DEFAULT_RERANK_CONCURRENCY + 4)
    assert peak == DEFAULT_RERANK_CONCURRENCY


async def test_the_catalog_concurrency_and_rate_win_over_the_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = ModelEntry(
        name="capped-rerank",
        rerank=True,
        rerank_caps=RerankCaps(rerank_api="cohere_style", max_concurrency=2, rpm=1_200),
    )
    monkeypatch.setattr(
        "orchestrator.llm.rerank_guard.catalog_entry",
        lambda provider, model: entry if model == "capped-rerank" else None,
    )
    guards, _sleeps, factory = _guards()
    assert await _peak_in_flight(guards, "capped-rerank", 5) == 2
    assert {rpm for _key, rpm in factory.built} == {1_200}


# --- retry ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "schedule"),
    [
        ("transient", [0.5, 1.0, 2.0]),  # 0.5 s x 2^n, jitter drawn at 0
        # B-206 K-7a (M1) — the vendor's window is one second: a request 0.5 s
        # after a refusal was refused again, one 1.05 s after passed (probe).
        ("rate_limited", [1.0, 1.0, 2.0]),
    ],
)
async def test_a_retryable_failure_is_retried_on_the_backoff_schedule(
    failure: str, schedule: list[float]
) -> None:
    guards, sleeps, _factory = _guards()
    inner = _Scripted(_err(failure), _err(failure), _err(failure), _OK)
    body = await _call(guards.wrap(inner, provider="qwen", model=_MODEL, secret_ref="s"))
    assert body == _OK
    assert len(inner.calls) == 4
    assert sleeps.delays == schedule


async def test_jitter_only_lengthens_a_backoff_by_up_to_a_quarter() -> None:
    guards, sleeps, _factory = _guards(rand=0.999)
    inner = _Scripted(_err("transient"), _OK)
    await _call(guards.wrap(inner, provider="qwen", model=_MODEL, secret_ref="s"))
    (delay,) = sleeps.delays
    assert BACKOFF_BASE_S < delay < BACKOFF_BASE_S * 1.25


@pytest.mark.parametrize(
    ("failure", "outcome"), [("rate_limited", "rate_limited"), ("transient", "model_error")]
)
async def test_retries_run_out_after_three(failure: str, outcome: str) -> None:
    guards, sleeps, _factory = _guards()
    inner = _Scripted(_err(failure))
    with pytest.raises(RerankUnavailableError) as raised:
        await _call(guards.wrap(inner, provider="qwen", model=_MODEL, secret_ref="s"))
    assert raised.value.outcome == outcome
    assert len(inner.calls) == 1 + MAX_RETRIES
    assert len(sleeps.delays) == MAX_RETRIES


@pytest.mark.parametrize("failure", ["rejected", "too_large"])
async def test_a_refusal_is_not_retried(failure: str) -> None:
    """A refusal would be refused again; ``too_large`` goes back to the reranker,
    which knows how to send less."""
    guards, sleeps, _factory = _guards()
    inner = _Scripted(_err(failure))
    with pytest.raises(RerankCallError) as raised:
        await _call(guards.wrap(inner, provider="qwen", model=_MODEL, secret_ref="s"))
    assert raised.value.failure == failure
    assert len(inner.calls) == 1
    assert sleeps.delays == []


@pytest.mark.parametrize(
    ("retry_after", "expected"), [(3.0, 3.0), (0.1, RATE_LIMITED_MIN_BACKOFF_S)]
)
async def test_a_vendor_retry_after_is_honoured_when_longer_than_the_backoff(
    retry_after: float, expected: float
) -> None:
    guards, sleeps, _factory = _guards()
    inner = _Scripted(_err("rate_limited", retry_after_s=retry_after), _OK)
    await _call(guards.wrap(inner, provider="qwen", model=_MODEL, secret_ref="s"))
    assert sleeps.delays == [expected]


@pytest.mark.parametrize(
    ("status", "failure"),
    [
        (429, "rate_limited"),
        (408, "transient"),
        (500, "transient"),
        (502, "transient"),
        (503, "transient"),
        (504, "transient"),
        (400, "rejected"),
        (401, "rejected"),
        (403, "rejected"),
        (404, "rejected"),
    ],
)
def test_http_status_classification(status: int, failure: str) -> None:
    assert status_failure(status) == failure


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({"retry-after": "2"}, 2.0), ({"retry-after": "1.5"}, 1.5), ({}, None)],
)
def test_retry_after_reads_delta_seconds(headers: dict[str, str], expected: float | None) -> None:
    assert retry_after_seconds(headers) == expected


@pytest.mark.parametrize("value", ["Wed, 21 Oct 2026 07:28:00 GMT", "soon", "-3"])
def test_retry_after_ignores_what_it_cannot_use(value: str) -> None:
    assert retry_after_seconds({"retry-after": value}) is None


# --- one search's time budget -----------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "budget_s", "delays", "outcome"),
    [
        # 0.5 + 1 s fits 3 s; then 1 + 1 s fits the 2.5 s left; 2 + 1 s does not fit 1.5 s
        ("transient", 3.0, [0.5, 1.0], "model_error"),
        # a 429 waits at least 1 s: 1 + 1 s fits 2.5 s, the next 1 + 1 s does not fit 1.5 s
        ("rate_limited", 2.5, [1.0], "rate_limited"),
    ],
)
async def test_a_backoff_that_leaves_no_time_for_the_request_gives_up_at_once(
    failure: str, budget_s: float, delays: list[float], outcome: str
) -> None:
    """B-206 K-7a (M2) — a backoff is only worth sleeping when a request still
    fits after it (:data:`MIN_REQUEST_S`)."""
    clock = _Clock()
    guards, sleeps, _factory = _guards(clock=clock)
    inner = _Scripted(_err(failure))
    with rerank_budget(budget_s, clock=clock), pytest.raises(RerankUnavailableError) as raised:
        await _call(guards.wrap(inner, provider="qwen", model=_MODEL, secret_ref="s"))
    assert raised.value.outcome == outcome
    assert sleeps.delays == delays
    assert len(inner.calls) == len(delays) + 1


async def test_both_views_of_a_search_share_one_budget() -> None:
    """The second request of a search gets what the first left, not a fresh
    allowance — the dual view must not double the worst-case wait. Here the
    first used 2.1 of 3 s (one backoff plus its time in flight), so the second
    has 0.9 s: less than a request needs, and it is not sent at all."""
    clock = _Clock()
    guards, sleeps, factory = _guards(clock=clock)
    first = guards.wrap(
        _Scripted(_err("transient"), _OK), provider="qwen", model=_MODEL, secret_ref="s"
    )
    second_inner = _Scripted()
    second = guards.wrap(second_inner, provider="qwen", model=_MODEL, secret_ref="s")
    with rerank_budget(3.0, clock=clock) as budget:
        await _call(first)  # one 0.5 s backoff
        clock.now += 1.6  # its two requests' time in flight
        with pytest.raises(RerankUnavailableError):
            await _call(second)
    assert sleeps.delays == [0.5]
    assert second_inner.calls == []
    assert budget.attempts == 2 == factory.bucket.taken


async def test_a_request_that_cannot_finish_in_time_is_not_started() -> None:
    """B-206 K-7a (M2) — under :data:`MIN_REQUEST_S` left, the guard gives up
    without a token or a paid call; nothing showed the rate was the cause."""
    clock = _Clock()
    guards, _sleeps, factory = _guards(clock=clock)
    inner = _Scripted()
    with rerank_budget(3.0, clock=clock):
        clock.now += 3.0 - MIN_REQUEST_S + 0.01
        with pytest.raises(RerankUnavailableError) as raised:
            await _call(guards.wrap(inner, provider="qwen", model=_MODEL, secret_ref="s"))
    assert raised.value.outcome == "model_error"
    assert inner.calls == []
    assert factory.bucket.taken == 0


async def test_waiting_for_a_pacing_token_past_the_budget_is_rate_limited() -> None:
    guards = RerankGuards(buckets=_BucketFactory(_Bucket(blocked=True)), min_request_s=0.0)
    inner = _Scripted()
    with rerank_budget(0.02), pytest.raises(RerankUnavailableError) as raised:
        await _call(guards.wrap(inner, provider="qwen", model=_MODEL, secret_ref="s"))
    assert raised.value.outcome == "rate_limited"
    assert inner.calls == []


async def test_waiting_for_a_concurrency_slot_past_the_budget_is_a_model_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-206 K-7a (L4) — a full concurrency cap means requests are slow, not
    that the vendor's rate was hit."""
    entry = ModelEntry(
        name="one-at-a-time",
        rerank=True,
        rerank_caps=RerankCaps(rerank_api="cohere_style", max_concurrency=1, rpm=6_000),
    )
    monkeypatch.setattr("orchestrator.llm.rerank_guard.catalog_entry", lambda p, m: entry)
    guards = RerankGuards(buckets=_BucketFactory(), min_request_s=0.0)
    gate = _Gate()
    busy = asyncio.create_task(
        _call(guards.wrap(gate, provider="x", model="one-at-a-time", secret_ref="s"))
    )
    await asyncio.sleep(0)
    inner = _Scripted()
    with rerank_budget(0.02), pytest.raises(RerankUnavailableError) as raised:
        await _call(guards.wrap(inner, provider="x", model="one-at-a-time", secret_ref="s"))
    gate.release.set()
    await asyncio.wait([busy])
    assert raised.value.outcome == "model_error"
    assert inner.calls == []


async def test_a_request_still_running_at_the_deadline_is_a_model_error() -> None:
    guards = RerankGuards(buckets=_BucketFactory(), min_request_s=0.0)
    with rerank_budget(0.02), pytest.raises(RerankUnavailableError) as raised:
        await _call(guards.wrap(_Gate(), provider="qwen", model=_MODEL, secret_ref="s"))
    assert raised.value.outcome == "model_error"


@pytest.mark.parametrize(
    ("failure", "outcome"), [("rate_limited", "rate_limited"), ("rejected", "model_error")]
)
def test_a_vendor_failure_says_how_a_search_reports_it(failure: str, outcome: str) -> None:
    """B-206 K-7a (L3) — a classified failure from a client nobody guards still
    maps to the right search outcome."""
    error = _err(failure)
    assert isinstance(error, RerankError)
    assert error.outcome == outcome


async def test_the_instruction_is_passed_only_when_there_is_one() -> None:
    guards, _sleeps, _factory = _guards()
    inner = _Scripted()
    client = guards.wrap(inner, provider="qwen", model=_MODEL, secret_ref="s")
    await client.rerank(model=_MODEL, query="q", documents=["a"], top_n=1, instruction="judge")
    await _call(client)
    assert inner.calls[0]["instruction"] == "judge"
    assert "instruction" not in inner.calls[1]  # a client written before K-14 keeps working
