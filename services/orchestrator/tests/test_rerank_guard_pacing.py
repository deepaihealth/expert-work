"""B-206 K-7a — the shape of the rerank pacing bucket.

The 2026-10-22 probe (qwen3-vl-rerank, 600 RPM): 10 of 60 simultaneous
requests passed, a request 0.5 s after that burst was still refused, and one
request every 100 ms passed 20 of 20. A bucket of 10 tokens refilled at 10 a
second admits 19 in its first second; so the guard spaces requests evenly —
one every ``60 / rpm`` seconds with no burst — and without Redis each replica
takes one every ``60 * replicas / rpm`` seconds.

These tests pin the buckets' actual parameters (a mutant with a 60 s period
passed every other test) and, once, the real spacing in time."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import pytest
from aiolimiter import AsyncLimiter

from orchestrator.llm.rate_limit_redis import RedisRpmLimiter
from orchestrator.llm.rerank_guard import (
    RerankGuards,
    local_pacing_buckets,
    pacing_spacing_s,
    redis_pacing_buckets,
)

_KEY = "rl:llm:rerank:qwen:qwen3-vl-rerank:secret://x"


@pytest.mark.parametrize(
    ("rpm", "spacing"), [(600, 0.1), (5_400, 60 / 5_400), (3_000, 0.02), (60, 1.0), (30, 2.0)]
)
def test_requests_are_spaced_sixty_over_the_rpm_seconds_apart(rpm: int, spacing: float) -> None:
    assert pacing_spacing_s(rpm) == pytest.approx(spacing)


@pytest.mark.parametrize(
    ("replicas", "rpm", "period"),
    [(1, 600, 0.1), (3, 600, 0.3), (1, 30, 2.0), (4, 30, 8.0)],
)
def test_the_local_bucket_holds_one_token_per_replica_spacing(
    monkeypatch: pytest.MonkeyPatch, replicas: int, rpm: int, period: float
) -> None:
    """M3 — no Redis: the minute rate is divided by the replicas before it is
    turned into a spacing, rpm under 60 included."""
    monkeypatch.setenv("EXPERT_WORK_REPLICA_COUNT", str(replicas))
    bucket = local_pacing_buckets(bucket=_KEY, rpm=rpm)
    assert isinstance(bucket, AsyncLimiter)
    assert bucket.max_rate == 1
    assert bucket.time_period == pytest.approx(period)


@pytest.mark.parametrize(("replicas", "fallback_period"), [(1, 0.1), (3, 0.3)])
def test_the_redis_bucket_holds_one_token_refilled_every_spacing(
    monkeypatch: pytest.MonkeyPatch, replicas: int, fallback_period: float
) -> None:
    """One token, refilled ten times a second at 600 RPM, shared by every
    replica; its Redis-outage fallback takes only this replica's share."""
    monkeypatch.setenv("EXPERT_WORK_REPLICA_COUNT", str(replicas))
    bucket = redis_pacing_buckets(object())(bucket=_KEY, rpm=600)  # type: ignore[arg-type]
    assert isinstance(bucket, RedisRpmLimiter)
    assert bucket.bucket_key == _KEY
    assert bucket._capacity == 1
    assert bucket._refill_per_s == pytest.approx(10.0)
    fallback = bucket._local
    assert fallback.max_rate == 1
    assert fallback.time_period == pytest.approx(fallback_period)


class _Stamps:
    """A rerank client that records when each request reached it."""

    def __init__(self) -> None:
        self.at: list[float] = []

    async def rerank(self, **_kwargs: Any) -> Mapping[str, Any]:
        self.at.append(time.monotonic())
        return {"output": {"results": []}}


async def test_two_requests_through_the_guard_are_a_spacing_apart_in_real_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one real-time check: at 600 RPM the second request waits about
    100 ms for its token (a burst bucket would let it through at once, a
    60 s period would hold it for seconds)."""
    monkeypatch.setenv("EXPERT_WORK_REPLICA_COUNT", "1")
    stamps = _Stamps()
    client = RerankGuards().wrap(
        stamps, provider="qwen", model="qwen3-vl-rerank", secret_ref="secret://x"
    )
    for _ in range(2):
        await client.rerank(model="qwen3-vl-rerank", query="q", documents=["a"], top_n=1)
    gap = stamps.at[1] - stamps.at[0]
    assert 0.07 <= gap <= 0.5
