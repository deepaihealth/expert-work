"""The HTTP client for a credential's custom request address — B-308 §4.5.2.

A platform credential may carry a request address an admin typed in. It is
validated when saved (public https only), but a host name can answer "public"
to that check and "internal" later. So every request to a custom address goes
through a client whose **connection layer** repeats the check each time it
opens a TCP connection:

1. resolve the host (on the event loop's resolver);
2. refuse the whole name if *any* address is not public;
3. connect to the checked addresses themselves, in resolver order, taking the
   first that connects — the name is never resolved a second time for the
   connection, so there is no gap between what was checked and what is dialed.

TLS is untouched: httpcore upgrades the stream with the original host name, so
the certificate is still verified against the name the admin configured. A
pooled connection is not re-checked — it is the connection to the address that
passed.

A refusal is an :class:`httpcore.ConnectError` (httpx: ``ConnectError``), which
provider clients already turn into a network error; routing treats it like any
other unreachable endpoint.

Requests to official addresses never come here (see
:func:`orchestrator.llm.providers._http.client_for`).
"""

from __future__ import annotations

import http.cookiejar
import logging
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import httpcore
import httpx

from expert_work.common.observability import expert_work_counter
from expert_work.common.observability.log import install_httpx_url_filter
from expert_work.common.url_validation import PublicEndpointError, resolve_public_addresses

logger = logging.getLogger(__name__)

# Covers processes that never call ``init_logging`` (eval scripts, CLIs): once
# this module is loaded, httpx request logs carry the origin only.
install_httpx_url_filter()

_connections_total = expert_work_counter(
    "expert_work_custom_endpoint_connections_total",
    "New connections to a credential's custom request address, by outcome.",
    ("provider", "outcome"),
)

#: The provider a custom-address request is for — a label for the log line and
#: the counter only (set by ``client_for`` around the request).
_REQUEST_PROVIDER: ContextVar[str] = ContextVar(
    "expert_work_custom_endpoint_provider", default="unknown"
)

#: Budget for one connection attempt when the request sets no connect timeout
#: (a lookup that never answers must not hold a request forever).
_DEFAULT_CONNECT_BUDGET_S = 30.0

#: Same pool shape as the process-level shared client (control-plane lifespan):
#: bounded keep-alive, unbounded total, 55 s expiry.
_LIMITS = httpx.Limits(max_keepalive_connections=64, max_connections=None, keepalive_expiry=55.0)


@contextmanager
def request_provider(provider: str | None) -> Iterator[None]:
    """Label connections opened inside the block with ``provider``."""
    if provider is None:
        yield
        return
    token = _REQUEST_PROVIDER.set(provider)
    try:
        yield
    finally:
        try:
            _REQUEST_PROVIDER.reset(token)
        except ValueError:
            # A streaming generator closed from another task: the label lived
            # in that generator's context and goes away with it.
            pass


class CheckedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Opens TCP connections only to addresses that passed the public-address check."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._inner: httpcore.AsyncNetworkBackend = (
            inner if inner is not None else httpcore.AnyIOBackend()
        )

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        provider = _REQUEST_PROVIDER.get()
        # One budget for the whole attempt: the lookup and every address tried.
        budget = timeout if timeout is not None else _DEFAULT_CONNECT_BUDGET_S
        deadline = time.monotonic() + budget
        try:
            addresses = await resolve_public_addresses(host, port, timeout_s=budget)
        except PublicEndpointError as exc:
            _connections_total.labels(provider=provider, outcome=exc.reason).inc()
            # Host only: a custom address may carry a token in its path.
            logger.warning(
                "provider_endpoint.blocked provider=%s host=%s reason=%s",
                provider,
                host,
                exc.reason,
            )
            msg = "model service address is not available"
            raise httpcore.ConnectError(msg) from exc
        last_error: Exception | None = None
        for address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _connections_total.labels(provider=provider, outcome="connect_error").inc()
                msg = "model service address did not connect in time"
                raise httpcore.ConnectTimeout(msg) from last_error
            try:
                stream = await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout, OSError) as exc:
                last_error = exc
                continue
            _connections_total.labels(provider=provider, outcome="ok").inc()
            return stream
        _connections_total.labels(provider=provider, outcome="connect_error").inc()
        msg = "model service address is not reachable"
        raise httpcore.ConnectError(msg) from last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        msg = "unix sockets are not a public endpoint"
        raise httpcore.ConnectError(msg)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class CheckedEndpointTransport(httpx.AsyncHTTPTransport):
    """``httpx`` transport whose pool dials through :class:`CheckedNetworkBackend`.

    httpx's own transport does not expose httpcore's ``network_backend``, so the
    pool is built here with the same settings httpx would use (default TLS
    verification, HTTP/1.1, no proxy, no environment lookups).
    """

    def __init__(
        self,
        *,
        limits: httpx.Limits = _LIMITS,
        inner_backend: httpcore.AsyncNetworkBackend | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> None:
        super().__init__(trust_env=False, limits=limits)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=True,
            http2=False,
            local_address=local_address,
            socket_options=socket_options,
            network_backend=CheckedNetworkBackend(inner_backend),
        )


def make_checked_client() -> httpx.AsyncClient:
    """A client that only ever connects to checked public addresses.

    Mirrors the process-level shared client: no client-level timeout (every
    call passes its own), the same pool limits, and a cookie jar that stores
    nothing. On top of that it ignores proxy / CA environment variables (a
    proxy would be what gets checked instead of the endpoint) and never
    follows a redirect (the target would be an address nobody checked).
    """
    client = httpx.AsyncClient(
        transport=CheckedEndpointTransport(),
        timeout=None,  # noqa: S113 — deliberate, as on the shared client
        trust_env=False,
        follow_redirects=False,
    )
    client.cookies.jar.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
    return client
