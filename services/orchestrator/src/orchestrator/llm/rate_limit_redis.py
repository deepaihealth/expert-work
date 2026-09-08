"""Redis-backed global provider RPM bucket — 波 2 线 A.

PROD-12 made N replicas honour one vendor ceiling by giving each an
in-process bucket of ``ceil(rpm / EXPERT_WORK_REPLICA_COUNT)``. That ties
the limit to a static env value — scaling out means editing it — and an
idle replica's share is wasted. This module keeps **one bucket per upstream
credential in Redis**: every replica draws from the same tokens, the
configured ``rate_limit_rpm`` is the global ceiling, and elastic scaling
needs no config change.

Same admission contract as the local bucket (see the module docstring of
:mod:`orchestrator.llm.rate_limit`): an over-limit call **awaits** the
refill, it never raises — a vendor 429 would count against the E.4 breaker.

Bucket identity ``rl:llm:{handle_key}:{secret_ref}``: the handle key
(``provider:model[#idx]``) keeps primary / fallback apart as before; the
credential *reference* (a ``secret://`` name, never the value) keeps two
tenants' own keys for one model apart — a vendor's limit is per API key.
``rl:`` is the control-plane's rate-limit prefix, so the keys sort next to
the gateway / tenant buckets in the same db.

Units (B-32 — the quota bucket shipped with a 1000x refill error that its
own retry formula agreed with, so write them down): capacity and tokens are
**requests**, ``refill_per_s`` is **requests / second**, clocks are
**milliseconds**. ``elapsed_ms * refill_per_s / 1000`` is requests;
``(1 - tokens) * 1000 / refill_per_s`` is milliseconds. Nothing is
pre-scaled on the Python side. ``test_rate_limit_redis_integration.py``
runs the script against a clock to pin this.

Degradation: any Redis failure — connection, timeout, malformed reply —
flips **this bucket** to the local PROD-12 bucket and logs one warning;
Redis is re-probed after :attr:`RedisRpmLimiter.REPROBE_S` and recovery
logs once. An LLM call never fails because Redis is down: the worst case is
exactly the pre-波2 behaviour.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, ClassVar

import redis.asyncio as redis_async
from aiolimiter import AsyncLimiter
from redis.exceptions import NoScriptError

from orchestrator.llm.rate_limit import (
    DEFAULT_TIME_PERIOD_S,
    AdmissionLimiter,
    RateLimiterFactory,
    effective_rpm,
)

logger = logging.getLogger(__name__)

_KEY_PREFIX = "rl:llm:"
_WHITESPACE = re.compile(r"\s")

#: Idle buckets age out (same horizon as the control-plane ``rl:`` buckets).
_BUCKET_TTL_MS = 30 * 86_400 * 1_000

# KEYS[1] = bucket key
# ARGV: 1=capacity (requests) 2=refill_per_s (requests/s) 3=now_ms 4=ttl_ms
# Returns {allowed (0/1), retry_after_ms}. Cost is always one request.
_LUA_ACQUIRE_SOURCE = """\
-- expert-work provider RPM bucket (波 2 线 A)
local b = redis.call('HMGET', KEYS[1], 'tokens', 'last_ms')
local cap = tonumber(ARGV[1])
local refill_per_s = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])
local tokens = tonumber(b[1]) or cap
local last_ms = tonumber(b[2]) or now_ms
local elapsed_ms = math.max(0, now_ms - last_ms)
tokens = math.min(cap, tokens + elapsed_ms * refill_per_s / 1000)
if tokens < 1 then
  local retry_ms = math.ceil((1 - tokens) * 1000 / refill_per_s)
  redis.call('HSET', KEYS[1], 'tokens', tokens, 'last_ms', now_ms)
  redis.call('PEXPIRE', KEYS[1], ttl_ms)
  return {0, retry_ms}
end
tokens = tokens - 1
redis.call('HSET', KEYS[1], 'tokens', tokens, 'last_ms', now_ms)
redis.call('PEXPIRE', KEYS[1], ttl_ms)
return {1, 0}
"""


def bucket_key(*, key: str, secret_ref: str) -> str:
    """``rl:llm:{handle_key}:{secret_ref}`` — see module docstring.

    The reference goes in verbatim except for whitespace (a Redis key is
    binary-safe, but a space or newline would make it unreadable in ops
    tooling and ambiguous in logs)."""
    return f"{_KEY_PREFIX}{key}:{_WHITESPACE.sub('_', secret_ref)}"


class RedisRpmLimiter:
    """:class:`AdmissionLimiter` over one Redis bucket shared by all replicas."""

    #: Bound on one Redis round-trip; a hung Redis degrades instead of
    #: stalling the LLM call.
    OP_TIMEOUT_S: ClassVar[float] = 2.0
    #: How long a degraded bucket serves locally before trying Redis again.
    REPROBE_S: ClassVar[float] = 60.0

    def __init__(
        self,
        *,
        redis_client: redis_async.Redis,
        bucket_key: str,
        rate_limit_rpm: int,
        time_period_s: float = DEFAULT_TIME_PERIOD_S,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_limit_rpm <= 0:
            raise ValueError(f"rate_limit_rpm must be positive, got {rate_limit_rpm!r}")
        if time_period_s <= 0:
            raise ValueError(f"time_period_s must be positive, got {time_period_s!r}")
        self._redis = redis_client
        self._key = bucket_key
        self._capacity = rate_limit_rpm
        self._refill_per_s = rate_limit_rpm / time_period_s
        # Fallback = the pre-波2 per-process bucket, replica division included.
        self._local = AsyncLimiter(
            max_rate=effective_rpm(rate_limit_rpm), time_period=time_period_s
        )
        self._clock = clock
        self._sleep = sleep
        self._lua_sha: str | None = None
        # Wall-clock time at which a degraded bucket re-probes Redis; ``None``
        # while healthy.
        self._reprobe_at: float | None = None

    @property
    def bucket_key(self) -> str:
        return self._key

    @property
    def degraded(self) -> bool:
        return self._reprobe_at is not None

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def acquire(self) -> None:
        """Await one token. Never raises on Redis trouble — see module docstring."""
        while True:
            if self._reprobe_at is not None and self._clock() < self._reprobe_at:
                await self._local.acquire()
                return
            try:
                allowed, retry_ms = await self._eval()
            except Exception as exc:  # any Redis failure degrades, never propagates
                self._degrade(exc)
                await self._local.acquire()
                return
            self._recover()
            if allowed:
                return
            await self._sleep(max(retry_ms, 1) / 1000.0)

    async def _eval(self) -> tuple[bool, int]:
        argv = [
            str(self._capacity),
            repr(self._refill_per_s),
            str(round(self._clock() * 1000)),
            str(_BUCKET_TTL_MS),
        ]
        result: Any
        async with asyncio.timeout(self.OP_TIMEOUT_S):
            try:
                if self._lua_sha is None:
                    self._lua_sha = str(await self._redis.script_load(_LUA_ACQUIRE_SOURCE))
                result = await self._redis.evalsha(self._lua_sha, 1, self._key, *argv)
            except NoScriptError:
                self._lua_sha = str(await self._redis.script_load(_LUA_ACQUIRE_SOURCE))
                result = await self._redis.evalsha(self._lua_sha, 1, self._key, *argv)
        allowed_raw, retry_ms_raw = result
        return bool(int(allowed_raw)), int(retry_ms_raw)

    def _degrade(self, exc: BaseException) -> None:
        first = self._reprobe_at is None
        self._reprobe_at = self._clock() + self.REPROBE_S
        if first:
            # Class name only: redis-py error text can carry the connection
            # URL, password included.
            logger.warning(
                "rate_limit.redis_degraded bucket=%s error=%s fallback_rpm=%s",
                self._key,
                type(exc).__name__,
                self._local.max_rate,
            )

    def _recover(self) -> None:
        if self._reprobe_at is None:
            return
        self._reprobe_at = None
        logger.info("rate_limit.redis_recovered bucket=%s", self._key)


def make_redis_rate_limiter_factory(
    redis_client: redis_async.Redis, *, time_period_s: float = DEFAULT_TIME_PERIOD_S
) -> RateLimiterFactory:
    """The :class:`RateLimiterFactory` the control-plane injects when it has Redis."""

    def _factory(*, key: str, secret_ref: str, rate_limit_rpm: int) -> AdmissionLimiter:
        return RedisRpmLimiter(
            redis_client=redis_client,
            bucket_key=bucket_key(key=key, secret_ref=secret_ref),
            rate_limit_rpm=rate_limit_rpm,
            time_period_s=time_period_s,
        )

    return _factory
