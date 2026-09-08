"""Invalidation-bus subscriber — drops the secret cache when a secret rotates (B-31 ①).

:class:`~credential_proxy.cache.SecretCache` used to be evictable early only
through ``POST /admin/cache/invalidate``, which nothing calls: after a
rotation the proxy kept injecting the old value for up to ``cache_ttl_s``.
An HTTP call would also stop scaling the day this Deployment gets a second
replica (a ClusterIP delivers it to one pod). Control-plane already
broadcasts every cache invalidation on one Redis pub/sub channel so all of
its replicas evict together — this module listens on that same channel.

The proxy is its own package and must not import ``control_plane``, so the
channel name and the payload shape are restated here as constants.
``tests/test_credential_proxy_invalidation_contract.py`` (repo root) pins
them against the real publisher — drift on either side goes red there.

Which kinds matter: only the ones control-plane publishes next to a
``SecretStore.put`` / ``delete`` that rewrites a value **under an existing
ref** — the cache is keyed by ``(tenant_id, secret_ref)``, so a ref swap
(``tenant_config``'s ``model_credentials_ref`` change) is a cache miss, not
a stale hit, and needs nothing here. Verified 2026-09-08 against every
``secret_store.put`` site in control-plane:

- ``platform_secrets`` — ``api/platform_config.py``: platform / tenant LLM
  and tool credentials, rotated in place under a canonical slot name.
- ``platform_mcp`` — ``api/mcp_catalog.py``: platform MCP bearer token,
  slot derived from the server name.
- ``tenant_mcp`` — ``api/mcp_servers.py``: tenant MCP token + custom
  headers blob, slot derived from (tenant, server name).
- ``user_mcp_oauth`` — ``api/mcp_oauth_api.py`` + ``mcp_oauth_refresh.py``:
  OAuth access / refresh tokens, slot derived from (tenant, connection).

Failure posture mirrors control-plane's bus: a bad payload is logged and
skipped, a dropped connection reconnects with capped exponential backoff,
and only cancellation (shutdown) exits the loop. Unset Redis URL → no
subscriber, one INFO line, behaviour identical to before this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import redis.asyncio as redis_async

from credential_proxy.cache import SecretCache
from credential_proxy.settings import CredentialProxySettings

logger = logging.getLogger("credential_proxy.invalidation")

#: control-plane's ``invalidation_bus.CHANNEL`` — one fan-out channel, kinds
#: are dispatched application-side.
INVALIDATION_CHANNEL = "expert_work:invalidation"

#: Event kinds whose publish sites rewrite a secret value under an existing
#: ref (see the module docstring for the audit). Everything else on the
#: channel is a config / built-agent eviction the proxy has no stake in.
SECRET_VALUE_KINDS = frozenset(
    {
        "platform_secrets",
        "platform_mcp",
        "tenant_mcp",
        "user_mcp_oauth",
    }
)


@dataclass(frozen=True)
class InvalidationEvent:
    """The wire shape control-plane's ``InvalidationBus.publish`` emits."""

    kind: str
    tenant_id: str | None = None
    user_id: str | None = None
    origin: str = ""


def parse_invalidation_event(raw: object) -> InvalidationEvent | None:
    """Decode one pub/sub payload; ``None`` (and a WARNING) for anything that
    is not the documented shape — a malformed message must never take the
    subscriber down."""
    try:
        data = json.loads(raw)  # type: ignore[arg-type]
        tenant_id = data.get("tenant_id")
        user_id = data.get("user_id")
        return InvalidationEvent(
            kind=str(data["kind"]),
            tenant_id=str(tenant_id) if tenant_id is not None else None,
            user_id=str(user_id) if user_id is not None else None,
            origin=str(data.get("origin") or ""),
        )
    except Exception:
        logger.warning("invalidation.bad_payload")
        return None


class InvalidationSubscriber:
    """Self-healing subscribe loop that evicts :class:`SecretCache` entries."""

    def __init__(
        self,
        *,
        redis_client: Any,
        cache: SecretCache,
        channel: str = INVALIDATION_CHANNEL,
        reconnect_initial_s: float = 0.5,
        reconnect_max_s: float = 30.0,
    ) -> None:
        self._redis = redis_client
        self._cache = cache
        self._channel = channel
        self._reconnect_initial_s = reconnect_initial_s
        self._reconnect_max_s = reconnect_max_s
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the subscriber task (idempotent while running)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self.run())

    async def stop(self) -> None:
        """Cancel + await the loop, then release the Redis client."""
        if self._task is not None:
            self._task.cancel()
            # ``gather`` swallows the CancelledError the cancel just raised.
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        with contextlib.suppress(Exception):
            await self._redis.aclose()

    async def run(self) -> None:
        """Subscribe / dispatch forever; reconnect with capped backoff."""
        backoff = self._reconnect_initial_s
        while True:
            pubsub = None
            try:
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(self._channel)
                logger.info("invalidation.subscribed channel=%s", self._channel)
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    backoff = self._reconnect_initial_s
                    self._apply(message.get("data"))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "invalidation.subscriber_error reconnect_in=%.1fs", backoff, exc_info=True
                )
            finally:
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._reconnect_max_s)

    def _apply(self, raw: object) -> None:
        event = parse_invalidation_event(raw)
        if event is None or event.kind not in SECRET_VALUE_KINDS:
            return
        # ``event.kind`` is a member of a constant set from here on — safe to log.
        if event.tenant_id is None:
            self._cache.invalidate_all()
            logger.info("invalidation.cache_cleared kind=%s scope=all", event.kind)
            return
        try:
            tenant_id = UUID(event.tenant_id)
        except ValueError:
            # Fail toward freshness: an unreadable scope clears everything
            # rather than leaving a possibly-rotated value in place.
            self._cache.invalidate_all()
            logger.warning("invalidation.bad_tenant_id kind=%s scope=all", event.kind)
            return
        self._cache.invalidate_tenant(tenant_id)
        logger.info("invalidation.cache_cleared kind=%s scope=tenant", event.kind)


def build_invalidation_subscriber(
    settings: CredentialProxySettings, cache: SecretCache
) -> InvalidationSubscriber | None:
    """Subscriber over ``settings.redis_url``, or ``None`` when it is unset.

    Unset is the pre-B-31 ① posture — the cache TTL is the only bound on
    staleness — and is logged once so an operator can tell the bus is off.
    The URL carries the Redis password and is never logged.
    """
    if not settings.redis_url:
        logger.info(
            "invalidation.disabled no EXPERT_WORK_CRED_PROXY_REDIS_URL; "
            "secret cache falls back to its %.0fs TTL",
            settings.cache_ttl_s,
        )
        return None
    client = redis_async.from_url(settings.redis_url, encoding="utf-8", decode_responses=True)
    return InvalidationSubscriber(redis_client=client, cache=cache)
