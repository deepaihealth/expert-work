"""Unit tests for :class:`RedisRpmLimiter` — 波 2 线 A (global provider RPM).

The real Lua bucket runs only against Redis (``test_rate_limit_redis_
integration.py``). These tests pin the Python side with a fake client that
keeps **per-key bucket state** (so two limiter instances on one key share a
bucket, like two replicas would) and can be scripted to fail:

* sharing: two "replicas" on one key admit exactly ``rpm`` calls at t=0, the
  next one sleeps for the Lua-reported refill time;
* isolation: different bucket keys never touch each other;
* degradation: any Redis failure flips the bucket to the local PROD-12
  division bucket, logs ONCE, does not hammer Redis while degraded, and
  re-probes after the cooldown;
* units: the rate crosses the wire as requests / second, unscaled (B-32).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import NoScriptError

from orchestrator.llm.rate_limit import RateLimitedProvider
from orchestrator.llm.rate_limit_redis import (
    RedisRpmLimiter,
    bucket_key,
    make_redis_rate_limiter_factory,
)
from orchestrator.tools.registry import ToolSpec

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeBucketRedis:
    """Fake async Redis whose ``evalsha`` re-implements the bucket arithmetic
    in Python over per-key state. Same units as the Lua: ARGV = (capacity,
    refill_per_s, now_ms, ttl_ms); reply = ``[allowed, retry_ms]``."""

    def __init__(self) -> None:
        self.buckets: dict[str, tuple[float, int]] = {}
        self.calls: list[tuple[str, list[str]]] = []
        self.script_loads = 0
        self.fail_with: BaseException | None = None
        self.hang = False
        self.reply_override: Any = None

    async def script_load(self, source: str) -> str:
        self.script_loads += 1
        return "fake-sha"

    async def evalsha(self, sha: str, numkeys: int, key: str, *argv: str) -> Any:
        if self.fail_with is not None:
            raise self.fail_with
        if self.hang:
            await asyncio.Event().wait()
        self.calls.append((key, [str(a) for a in argv]))
        if self.reply_override is not None:
            return self.reply_override
        cap, rate, now_ms = float(argv[0]), float(argv[1]), int(argv[2])
        tokens, last_ms = self.buckets.get(key, (cap, now_ms))
        tokens = min(cap, tokens + max(0, now_ms - last_ms) * rate / 1000)
        if tokens < 1:
            self.buckets[key] = (tokens, now_ms)
            return [0, math.ceil((1 - tokens) * 1000 / rate)]
        self.buckets[key] = (tokens - 1, now_ms)
        return [1, 0]


class _FakeClock:
    """Injectable wall clock + sleep: ``sleep`` advances the clock instead of
    waiting, and records every duration the limiter asked for."""

    def __init__(self) -> None:
        self.now = 1_700_000_000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _limiter(
    redis: _FakeBucketRedis,
    clock: _FakeClock,
    *,
    key: str = "rl:llm:anthropic:claude:abc",
    rpm: int = 4,
    time_period_s: float = 60.0,
) -> RedisRpmLimiter:
    return RedisRpmLimiter(
        redis_client=redis,  # type: ignore[arg-type]
        bucket_key=key,
        rate_limit_rpm=rpm,
        time_period_s=time_period_s,
        clock=clock.time,
        sleep=clock.sleep,
    )


# ---------------------------------------------------------------------------
# Sharing + isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_replicas_on_one_key_share_the_bucket_total_admits_equals_rpm() -> None:
    """Two limiter instances (= two replicas) on the same bucket key: exactly
    ``rpm`` calls go through at t=0 in total, the next one waits one refill
    interval (60 s / rpm) as reported by the bucket."""
    redis = _FakeBucketRedis()
    clock = _FakeClock()
    replica_a = _limiter(redis, clock, rpm=4)
    replica_b = _limiter(redis, clock, rpm=4)

    for i in range(4):
        await (replica_a if i % 2 == 0 else replica_b).acquire()
    assert clock.sleeps == [], "4 calls fit a 4-rpm bucket shared by both replicas"

    await replica_b.acquire()
    assert clock.sleeps == [pytest.approx(15.0, abs=0.01)], (
        "5th call across the two replicas must wait one refill interval (60 s / 4)"
    )
    # 4 admits + 1 deny + 1 admit after the sleep.
    assert [c[0] for c in redis.calls] == ["rl:llm:anthropic:claude:abc"] * 6


@pytest.mark.asyncio
async def test_different_bucket_keys_are_independent() -> None:
    redis = _FakeBucketRedis()
    clock = _FakeClock()
    primary = _limiter(redis, clock, key="rl:llm:anthropic:claude:primary", rpm=1)
    fallback = _limiter(redis, clock, key="rl:llm:anthropic:claude:fallback", rpm=1)

    await primary.acquire()
    await fallback.acquire()

    assert clock.sleeps == [], "each key has its own 1-token bucket"
    assert set(redis.buckets) == {
        "rl:llm:anthropic:claude:primary",
        "rl:llm:anthropic:claude:fallback",
    }


@pytest.mark.asyncio
async def test_rate_limited_provider_admits_through_redis_limiter() -> None:
    """The limiter satisfies :class:`RateLimitedProvider`'s ``async with``
    contract — one token per ``complete``."""

    class _Inner:
        async def complete(
            self, *, messages: Sequence[BaseMessage], tools: Sequence[ToolSpec]
        ) -> AIMessage:
            return AIMessage(content="ok")

    redis = _FakeBucketRedis()
    clock = _FakeClock()
    wrapped = RateLimitedProvider(inner=_Inner(), limiter=_limiter(redis, clock, rpm=10))

    out = await wrapped.complete(messages=[HumanMessage(content="hi")], tools=[])

    assert out.content == "ok"
    assert len(redis.calls) == 1


# ---------------------------------------------------------------------------
# Units — Python sends requests / second, unscaled (B-32 lesson)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_argv_carries_capacity_in_requests_and_rate_in_requests_per_second() -> None:
    redis = _FakeBucketRedis()
    clock = _FakeClock()

    await _limiter(redis, clock, rpm=60, time_period_s=60.0).acquire()
    await _limiter(redis, clock, rpm=30, time_period_s=10.0).acquire()

    (_, argv_60), (_, argv_30) = redis.calls
    assert argv_60[0] == "60" and argv_60[1] == "1.0", "60 rpm over 60 s = 1 req/s"
    assert argv_30[0] == "30" and argv_30[1] == "3.0", "30 per 10 s = 3 req/s"
    assert argv_60[2] == str(int(clock.now * 1000)), "now travels in milliseconds"


def test_bucket_key_is_handle_key_plus_credential_ref() -> None:
    key_a = bucket_key(
        key="anthropic:claude-sonnet-4-6#1", secret_ref="secret://tenant-a/anthropic"
    )
    key_b = bucket_key(
        key="anthropic:claude-sonnet-4-6#1", secret_ref="secret://tenant-b/anthropic"
    )

    assert key_a == "rl:llm:anthropic:claude-sonnet-4-6#1:secret://tenant-a/anthropic"
    assert key_a != key_b, "two credentials for one model are two upstream limits"
    assert (
        bucket_key(key="openai:gpt-4o", secret_ref="secret://odd name\nwith\tspace")
        == "rl:llm:openai:gpt-4o:secret://odd_name_with_space"
    ), "only whitespace is escaped; the ref stays readable in the key"


def test_factory_builds_a_redis_limiter_per_handle() -> None:
    factory = make_redis_rate_limiter_factory(_FakeBucketRedis())  # type: ignore[arg-type]
    limiter = factory(key="openai:gpt-4o", secret_ref="secret://x", rate_limit_rpm=7)
    assert isinstance(limiter, RedisRpmLimiter)
    assert limiter.bucket_key == bucket_key(key="openai:gpt-4o", secret_ref="secret://x")


def test_rejects_non_positive_rpm_and_period() -> None:
    redis = _FakeBucketRedis()
    clock = _FakeClock()
    with pytest.raises(ValueError, match="rate_limit_rpm"):
        _limiter(redis, clock, rpm=0)
    with pytest.raises(ValueError, match="time_period_s"):
        _limiter(redis, clock, rpm=1, time_period_s=0.0)


# ---------------------------------------------------------------------------
# NOSCRIPT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noscript_reloads_and_retries_once_without_degrading() -> None:
    class _EvictedOnce(_FakeBucketRedis):
        def __init__(self) -> None:
            super().__init__()
            self._evicted = True

        async def evalsha(self, sha: str, numkeys: int, key: str, *argv: str) -> Any:
            if self._evicted:
                self._evicted = False
                raise NoScriptError("NOSCRIPT")
            return await super().evalsha(sha, numkeys, key, *argv)

    redis = _EvictedOnce()
    clock = _FakeClock()
    limiter = _limiter(redis, clock)

    await limiter.acquire()

    assert redis.script_loads == 2, "lazy first load + reload after NOSCRIPT"
    assert len(redis.calls) == 1
    assert limiter.degraded is False


# ---------------------------------------------------------------------------
# Degradation — Redis failure → local PROD-12 bucket, log once, re-probe later
# ---------------------------------------------------------------------------


def _degrade_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("rate_limit.redis_degraded")]


def _recover_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("rate_limit.redis_recovered")]


@pytest.mark.asyncio
async def test_redis_error_degrades_to_local_division_bucket_and_logs_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Connection error → the bucket falls back to the pre-波2 behaviour: a
    per-process bucket at ``ceil(rpm / EXPERT_WORK_REPLICA_COUNT)``. rpm=4 on
    2 replicas = 2 local tokens, so the 3rd call waits a real refill; without
    the division all 4 would pass instantly. Exactly one warning."""
    monkeypatch.setenv("EXPERT_WORK_REPLICA_COUNT", "2")
    caplog.set_level(logging.WARNING, logger="orchestrator.llm.rate_limit_redis")
    redis = _FakeBucketRedis()
    redis.fail_with = RedisConnectionError("connection refused")
    clock = _FakeClock()
    window_s = 0.4
    limiter = _limiter(redis, clock, rpm=4, time_period_s=window_s)

    start = time.monotonic()
    await limiter.acquire()
    await limiter.acquire()
    first_two = time.monotonic() - start
    await limiter.acquire()
    elapsed = time.monotonic() - start

    assert limiter.degraded is True
    assert limiter.last_degrade_error == "ConnectionError"
    assert first_two < 0.1, "2 local tokens (4 rpm / 2 replicas) dispatch instantly"
    assert elapsed >= window_s / 2 * 0.8, (
        f"3rd call must wait a local refill (division applied); got {elapsed:.3f}s"
    )
    degraded = _degrade_records(caplog)
    assert len(degraded) == 1, "one warning per outage, not one per call"
    assert degraded[0].levelno == logging.WARNING
    assert "ConnectionError" in degraded[0].getMessage()
    assert redis.calls == [], "no tenant data and no successful eval"


@pytest.mark.asyncio
async def test_degrade_log_carries_the_exception_class_but_never_its_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """redis-py error text can embed the connection URL (password included);
    the warning names the exception class only."""
    caplog.set_level(logging.WARNING, logger="orchestrator.llm.rate_limit_redis")
    redis = _FakeBucketRedis()
    redis.fail_with = RedisConnectionError(
        "Error connecting to redis://default:hunter2-s3cret@redis.internal:6379/0"
    )
    clock = _FakeClock()

    await _limiter(redis, clock, rpm=100).acquire()

    (record,) = _degrade_records(caplog)
    assert "ConnectionError" in record.getMessage()
    assert "hunter2-s3cret" not in caplog.text
    assert "redis.internal" not in caplog.text


@pytest.mark.asyncio
async def test_degraded_bucket_does_not_touch_redis_until_the_reprobe_cooldown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="orchestrator.llm.rate_limit_redis")
    redis = _FakeBucketRedis()
    redis.fail_with = RedisConnectionError("down")
    clock = _FakeClock()
    limiter = _limiter(redis, clock, rpm=100)
    await limiter.acquire()  # → degraded
    redis.fail_with = None  # Redis is back, but the cooldown hasn't elapsed

    clock.now += 1.0
    await limiter.acquire()
    assert redis.calls == [], "degraded bucket serves locally; Redis not re-probed yet"
    assert limiter.degraded is True

    clock.now += RedisRpmLimiter.REPROBE_S
    await limiter.acquire()
    assert len(redis.calls) == 1, "cooldown elapsed → re-probe hits Redis"
    assert limiter.degraded is False
    assert limiter.last_degrade_error is None, "recovery clears the last error"
    assert len(_recover_records(caplog)) == 1


@pytest.mark.asyncio
async def test_each_outage_episode_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="orchestrator.llm.rate_limit_redis")
    redis = _FakeBucketRedis()
    clock = _FakeClock()
    limiter = _limiter(redis, clock, rpm=100)

    redis.fail_with = RedisConnectionError("down")
    for _ in range(3):
        await limiter.acquire()
    assert len(_degrade_records(caplog)) == 1

    # Still failing at the re-probe → stays degraded, no new warning.
    clock.now += RedisRpmLimiter.REPROBE_S
    await limiter.acquire()
    assert len(_degrade_records(caplog)) == 1

    # Recovers, then fails again → second episode, second warning.
    redis.fail_with = None
    clock.now += RedisRpmLimiter.REPROBE_S
    await limiter.acquire()
    assert limiter.degraded is False
    redis.fail_with = RedisConnectionError("down again")
    await limiter.acquire()
    assert len(_degrade_records(caplog)) == 2


@pytest.mark.asyncio
async def test_hung_redis_degrades_after_the_op_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Redis that never answers must not stall the LLM call: the eval is
    bounded by the op timeout, then the bucket degrades."""
    monkeypatch.setattr(RedisRpmLimiter, "OP_TIMEOUT_S", 0.05)
    redis = _FakeBucketRedis()
    redis.hang = True
    clock = _FakeClock()
    limiter = _limiter(redis, clock, rpm=100)

    start = time.monotonic()
    await asyncio.wait_for(limiter.acquire(), timeout=2.0)

    assert time.monotonic() - start < 1.0
    assert limiter.degraded is True


@pytest.mark.asyncio
async def test_malformed_reply_degrades_instead_of_raising() -> None:
    redis = _FakeBucketRedis()
    redis.reply_override = "not-a-pair"
    clock = _FakeClock()
    limiter = _limiter(redis, clock, rpm=100)

    await limiter.acquire()

    assert limiter.degraded is True


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed_as_degradation() -> None:
    """``CancelledError`` (run cancel / deadline) must propagate — it is not a
    Redis failure and must not flip the bucket to degraded."""
    redis = _FakeBucketRedis()
    redis.hang = True
    clock = _FakeClock()
    limiter = _limiter(redis, clock, rpm=100)

    task = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await task

    assert limiter.degraded is False
