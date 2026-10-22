"""B-206 K-7a (M3) — one replica's share of a rate, when there is no global bucket.

PROD-12 gives each replica ``ceil(per_period / replicas)`` per period. That
stays as it was while it is at least one request; below it (a one-token,
evenly spaced rerank bucket) the replica gets one request every
``period * replicas / per_period`` seconds, so N replicas together never
exceed the configured rate."""

from __future__ import annotations

import pytest

from orchestrator.llm.rate_limit import effective_rpm, replica_share
from orchestrator.llm.rate_limit_redis import RedisRpmLimiter


@pytest.mark.parametrize(
    ("replicas", "per_period", "period_s", "expected"),
    [
        # one replica: the rate itself
        (1, 1, 0.1, (1, 0.1)),
        (1, 60, 60.0, (60, 60.0)),
        # PROD-12 unchanged while the share is at least one request
        (3, 60, 60.0, (20, 60.0)),
        (3, 4, 60.0, (2, 60.0)),
        (3, 3, 1.0, (1, 1.0)),
        # below one request: the spacing stretches instead (600 RPM rerank = 1 per 0.1 s)
        (3, 1, 0.1, (1, pytest.approx(0.3))),
        (4, 1, 2.0, (1, pytest.approx(8.0))),  # 30 RPM rerank on 4 replicas
        (3, 2, 60.0, (1, pytest.approx(90.0))),
    ],
)
def test_replica_share(
    monkeypatch: pytest.MonkeyPatch,
    replicas: int,
    per_period: int,
    period_s: float,
    expected: tuple[int, float],
) -> None:
    monkeypatch.setenv("EXPERT_WORK_REPLICA_COUNT", str(replicas))
    assert replica_share(per_period, period_s) == expected


@pytest.mark.parametrize("replicas", [1, 2, 3, 7])
@pytest.mark.parametrize("rpm", [1, 4, 60, 600])
def test_the_share_matches_prod12_wherever_prod12_gives_at_least_one(
    monkeypatch: pytest.MonkeyPatch, replicas: int, rpm: int
) -> None:
    monkeypatch.setenv("EXPERT_WORK_REPLICA_COUNT", str(replicas))
    if rpm >= replicas:
        assert replica_share(rpm, 60.0) == (effective_rpm(rpm), 60.0)


@pytest.mark.parametrize(
    ("per_period", "period_s", "expected_rate", "expected_period"),
    [(1, 0.1, 1, 0.3), (60, 60.0, 20, 60.0)],
)
def test_the_redis_outage_fallback_takes_only_this_replicas_share(
    monkeypatch: pytest.MonkeyPatch,
    per_period: int,
    period_s: float,
    expected_rate: int,
    expected_period: float,
) -> None:
    """Redis down → each replica's local bucket. A one-token 100 ms rerank bucket
    on three replicas used to stay one token per 100 ms each — three times the
    vendor's rate."""
    monkeypatch.setenv("EXPERT_WORK_REPLICA_COUNT", "3")
    limiter = RedisRpmLimiter(
        redis_client=object(),  # type: ignore[arg-type]
        bucket_key="rl:llm:k",
        rate_limit_rpm=per_period,
        time_period_s=period_s,
    )
    local = limiter._local
    assert local.max_rate == expected_rate
    assert local.time_period == pytest.approx(expected_period)
