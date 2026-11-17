"""B-308 §4.5.2 / §7.1 (4, 10) — the client every custom-address request uses.

Each new TCP connection resolves the host, refuses the whole name if any
address is not public, and connects to the checked addresses themselves (never
to the name again), so a name cannot answer "public" at save time and
"internal" at call time. TLS still verifies the original host name.
"""

from __future__ import annotations

import logging
import socket
import ssl
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import Any

import httpcore
import httpx
import pytest
from langchain_core.messages import HumanMessage

from expert_work.common import url_validation
from expert_work.common.observability.log import HttpxUrlFilter
from expert_work.protocol import ModelSpec
from expert_work.runtime.middleware import LLMNetworkError
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import build_llm_router
from orchestrator.llm.endpoints import ProviderKey
from orchestrator.llm.providers import _checked_endpoint, _http
from orchestrator.llm.providers._checked_endpoint import (
    CheckedEndpointTransport,
    CheckedNetworkBackend,
    make_checked_client,
)
from orchestrator.llm.providers._http import client_for
from orchestrator.llm.router import AllProvidersExhaustedError

_MARKER = "TOKEN-IN-PATH-7f3a"
_HOST = "proxy.example.com"
_URL = f"https://{_HOST}/{_MARKER}/v1/chat/completions"
_PUBLIC_A = "93.184.216.34"
_PUBLIC_B = "2606:2800:220:1:248:1893:25c8:1946"
_INTERNAL = "10.0.0.7"

_HTTP_OK = [
    b"HTTP/1.1 200 OK\r\n",
    b"content-type: application/json\r\n",
    b"content-length: 2\r\n",
    b"\r\n",
    b"{}",
]


class _Dns:
    """Scripted resolver: each lookup of a host pops its next answer (the last repeats)."""

    def __init__(self, answers: dict[str, list[list[str]]]) -> None:
        self._answers = answers
        self.lookups: list[str] = []

    async def __call__(self, host: str, port: int) -> list[tuple[Any, ...]]:
        self.lookups.append(host)
        script = self._answers[host]
        answer = script.pop(0) if len(script) > 1 else script[0]
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, port),
            )
            for address in answer
        ]


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> _Dns:
    resolver = _Dns({})
    monkeypatch.setattr(url_validation, "_getaddrinfo", resolver)
    return resolver


class _Stream(httpcore.AsyncMockStream):
    """A canned HTTP/1.1 reply; records the TLS server name it was upgraded with."""

    def __init__(self, tls_names: list[str | None]) -> None:
        super().__init__(list(_HTTP_OK))
        self._tls_names = tls_names

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self._tls_names.append(server_hostname)
        return self


class _Inner(httpcore.AsyncNetworkBackend):
    """Stands in for the real socket layer: records what it was asked to dial."""

    def __init__(self, refuse: Iterable[str] = ()) -> None:
        self.dialed: list[dict[str, Any]] = []
        self.tls_names: list[str | None] = []
        self._refuse = set(refuse)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.dialed.append(
            {
                "host": host,
                "port": port,
                "local_address": local_address,
                "socket_options": socket_options,
            }
        )
        if host in self._refuse:
            raise httpcore.ConnectError("connection refused")
        return _Stream(self.tls_names)

    async def sleep(self, seconds: float) -> None:
        return None


def _client(inner: _Inner, **transport_kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=CheckedEndpointTransport(inner_backend=inner, **transport_kwargs),
        trust_env=False,
    )


# --- the connection layer ---------------------------------------------------


@pytest.mark.asyncio
async def test_connects_to_the_checked_address_and_keeps_the_host_name_for_tls(dns: _Dns) -> None:
    dns._answers[_HOST] = [[_PUBLIC_A]]
    inner = _Inner()
    async with _client(inner) as client:
        response = await client.post(_URL, json={})

    assert response.status_code == 200
    assert [d["host"] for d in inner.dialed] == [_PUBLIC_A]  # the IP, not the name
    assert inner.dialed[0]["port"] == 443
    assert inner.tls_names == [_HOST]  # certificate is still checked against the name


@pytest.mark.asyncio
async def test_rebinding_name_is_refused_on_the_next_new_connection(dns: _Dns) -> None:
    dns._answers[_HOST] = [[_PUBLIC_A], [_INTERNAL]]
    inner = _Inner()
    async with _client(inner) as first:
        assert (await first.post(_URL, json={})).status_code == 200
    async with _client(inner) as second:  # a fresh pool: a new connection, a new lookup
        with pytest.raises(httpx.ConnectError) as excinfo:
            await second.post(_URL, json={})

    assert isinstance(excinfo.value.__cause__, httpcore.ConnectError)
    assert [d["host"] for d in inner.dialed] == [_PUBLIC_A]  # the internal address is never dialed
    assert _MARKER not in str(excinfo.value)
    assert _INTERNAL not in str(excinfo.value)


@pytest.mark.asyncio
async def test_one_internal_answer_among_public_ones_refuses_the_whole_name(dns: _Dns) -> None:
    dns._answers[_HOST] = [[_PUBLIC_A, _INTERNAL]]
    inner = _Inner()
    async with _client(inner) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post(_URL, json={})
    assert inner.dialed == []


@pytest.mark.asyncio
async def test_unresolvable_name_is_a_connect_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fail(host: str, port: int) -> list[tuple[Any, ...]]:
        raise OSError("no such host")

    monkeypatch.setattr(url_validation, "_getaddrinfo", _fail)
    inner = _Inner()
    async with _client(inner) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post(_URL, json={})
    assert inner.dialed == []


@pytest.mark.asyncio
async def test_second_checked_address_is_tried_when_the_first_does_not_connect(dns: _Dns) -> None:
    dns._answers[_HOST] = [[_PUBLIC_A, _PUBLIC_B]]
    inner = _Inner(refuse=[_PUBLIC_A])
    async with _client(inner) as client:
        response = await client.post(_URL, json={})

    assert response.status_code == 200
    assert [d["host"] for d in inner.dialed] == [_PUBLIC_A, _PUBLIC_B]
    assert inner.tls_names == [_HOST]


@pytest.mark.asyncio
async def test_all_checked_addresses_failing_is_a_connect_error(dns: _Dns) -> None:
    dns._answers[_HOST] = [[_PUBLIC_A, _PUBLIC_B]]
    inner = _Inner(refuse=[_PUBLIC_A, _PUBLIC_B])
    async with _client(inner) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post(_URL, json={})
    assert [d["host"] for d in inner.dialed] == [_PUBLIC_A, _PUBLIC_B]


@pytest.mark.asyncio
async def test_trying_several_addresses_stays_within_one_connect_timeout(dns: _Dns) -> None:
    """The connect timeout bounds the whole attempt — lookup plus every address
    tried — not each address on its own."""
    import asyncio

    dns._answers[_HOST] = [[_PUBLIC_A, _PUBLIC_B, "93.184.216.35"]]
    timeouts: list[float | None] = []

    class _SlowRefusing(_Inner):
        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Iterable[Any] | None = None,
        ) -> httpcore.AsyncNetworkStream:
            timeouts.append(timeout)
            await asyncio.sleep(0.06)
            raise httpcore.ConnectError("connection refused")

    backend = CheckedNetworkBackend(_SlowRefusing())
    with pytest.raises(httpcore.ConnectTimeout):
        await backend.connect_tcp(_HOST, 443, timeout=0.1)

    assert len(timeouts) == 2  # the third address is never tried: the budget is spent
    assert timeouts[0] is not None and timeouts[1] is not None
    assert timeouts[0] <= 0.1
    assert timeouts[1] < timeouts[0]


@pytest.mark.asyncio
async def test_a_lookup_that_hangs_ends_with_the_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import time

    async def _hang(host: str, port: int) -> list[tuple[Any, ...]]:
        await asyncio.sleep(30)
        return []

    monkeypatch.setattr(url_validation, "_getaddrinfo", _hang)
    inner = _Inner()
    started = time.monotonic()
    with pytest.raises(httpcore.ConnectError):
        await CheckedNetworkBackend(inner).connect_tcp(_HOST, 443, timeout=0.05)
    assert time.monotonic() - started < 2
    assert inner.dialed == []


@pytest.mark.asyncio
async def test_local_address_and_socket_options_are_passed_through(dns: _Dns) -> None:
    dns._answers[_HOST] = [[_PUBLIC_A]]
    inner = _Inner()
    options = [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]
    async with _client(inner, local_address="0.0.0.0", socket_options=options) as client:  # noqa: S104
        await client.post(_URL, json={})

    assert inner.dialed[0]["local_address"] == "0.0.0.0"  # noqa: S104
    assert inner.dialed[0]["socket_options"] == options


@pytest.mark.asyncio
async def test_internal_ip_literal_is_refused_without_a_lookup(dns: _Dns) -> None:
    inner = _Inner()
    async with _client(inner) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post("https://100.100.100.200/v1/chat/completions", json={})
    assert inner.dialed == []
    assert dns.lookups == []


@pytest.mark.asyncio
async def test_unix_sockets_are_refused() -> None:
    backend = CheckedNetworkBackend()
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_unix_socket("/var/run/docker.sock")


def test_checked_client_settings_match_the_shared_client() -> None:
    client = make_checked_client()
    transport = client._transport
    assert isinstance(transport, CheckedEndpointTransport)
    pool = transport._pool
    assert isinstance(pool._network_backend, CheckedNetworkBackend)
    assert pool._max_keepalive_connections == 64
    assert pool._keepalive_expiry == 55.0
    assert pool._max_connections > 10_000  # unbounded, like the shared client
    assert client.timeout == httpx.Timeout(None)
    assert client.trust_env is False  # never through an environment proxy
    assert client.follow_redirects is False
    assert client._mounts == {}
    # No cookie is ever stored (cross-tenant replay guard, as on the shared client).
    policy = vars(client.cookies.jar)["_policy"]
    assert policy.allowed_domains() == ()


@pytest.mark.asyncio
async def test_real_socket_layer_dials_the_checked_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same path with httpcore's real socket backend (no stand-in): a local
    server plays the vendor, and the name only "resolves" through the check."""
    import asyncio

    served: list[bytes] = []

    async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        served.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\n{}")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    resolved: list[tuple[str, int]] = []

    async def _checked(
        host: str, checked_port: int, *, timeout_s: float | None = None
    ) -> list[str]:
        resolved.append((host, checked_port))
        return ["127.0.0.1"]  # stands in for "the public address that passed the check"

    monkeypatch.setattr(_checked_endpoint, "resolve_public_addresses", _checked)
    try:
        async with make_checked_client() as client:
            response = await client.get(f"http://{_HOST}:{port}/{_MARKER}/v1/models", timeout=5.0)
    finally:
        server.close()
        await server.wait_closed()

    assert response.status_code == 200
    assert resolved == [(_HOST, port)]
    assert f"host: {_HOST}:{port}".encode() in served[0].lower()  # the name, not the IP


# --- client_for decides at the bottom layer -----------------------------------


@pytest.fixture
def no_registered_client() -> Iterator[None]:
    saved = _http._checked_client
    _http._checked_client = None
    try:
        yield
    finally:
        _http._checked_client = saved


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_registered_client")
async def test_unregistered_process_still_gets_a_checking_client(dns: _Dns) -> None:
    dns._answers["rebind.example.com"] = [[_INTERNAL]]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200))
    ) as shared:
        async with client_for(shared, timeout=5.0, transport=None, custom_endpoint=True) as client:
            assert client is not shared
            assert isinstance(client._transport, CheckedEndpointTransport)
            with pytest.raises(httpx.ConnectError):
                await client.post("https://rebind.example.com/v1/chat/completions", json={})
        assert client.is_closed  # per-call client: closed on exit


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_registered_client")
async def test_registered_client_is_used_and_left_open() -> None:
    registered = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    shared = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    _http.register_checked_client(registered)
    try:
        async with client_for(shared, timeout=5.0, transport=None, custom_endpoint=True) as client:
            assert client is registered
        assert not registered.is_closed
        async with client_for(None, timeout=5.0, transport=None, custom_endpoint=True) as client:
            assert client is registered
        async with client_for(shared, timeout=5.0, transport=None) as client:
            assert client is shared  # official address: unchanged
    finally:
        _http.unregister_checked_client(registered)
        await registered.aclose()
        await shared.aclose()
    assert _http._checked_client is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_registered_client")
async def test_unregistering_another_client_does_not_drop_the_registered_one() -> None:
    first = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    second = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    try:
        _http.register_checked_client(first)
        _http.unregister_checked_client(second)
        assert _http._checked_client is first
    finally:
        _http.unregister_checked_client(first)
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_registered_client")
async def test_explicit_test_transport_wins_for_a_custom_endpoint() -> None:
    seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    async with client_for(
        None, timeout=5.0, transport=httpx.MockTransport(_handler), custom_endpoint=True
    ) as client:
        await client.post(_URL, json={})
    assert seen == [_URL]


# --- through a provider: error type, routing, nothing leaks ---------------------


class _AllLogs(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{record.name} {record.getMessage()}")


@pytest.fixture
def all_logs() -> Iterator[_AllLogs]:
    handler = _AllLogs()
    root = logging.getLogger()
    httpx_logger = logging.getLogger("httpx")
    saved = (root.level, httpx_logger.level, httpx_logger.disabled)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # Another test in the same process may have switched existing loggers off
    # (``logging.config.fileConfig`` does): these tests need httpx to log.
    httpx_logger.disabled = False
    httpx_logger.setLevel(logging.NOTSET)
    logging.getLogger(_checked_endpoint.__name__).disabled = False
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(saved[0])
        httpx_logger.setLevel(saved[1])
        httpx_logger.disabled = saved[2]


def _store() -> LocalDevSecretStore:
    return LocalDevSecretStore.from_mapping(
        {"expert-work/dev/llm/primary": "sk-primary", "expert-work/dev/llm/backup": "sk-backup"}
    )


@pytest.fixture
async def registered_checked_client(dns: _Dns) -> AsyncIterator[_Inner]:
    inner = _Inner()
    client = _client(inner)
    _http.register_checked_client(client)
    try:
        yield inner
    finally:
        _http.unregister_checked_client(client)
        await client.aclose()


@pytest.mark.asyncio
async def test_refused_connection_surfaces_as_network_error_and_skips_sibling_keys(
    dns: _Dns, registered_checked_client: _Inner, all_logs: _AllLogs
) -> None:
    dns._answers[_HOST] = [[_INTERNAL]]
    backup_calls: list[str] = []

    def _shared(request: httpx.Request) -> httpx.Response:
        backup_calls.append(str(request.url))
        return httpx.Response(200, json={})

    async def _resolve(_provider: str) -> list[ProviderKey]:
        return [
            ProviderKey("secret://expert-work/dev/llm/primary", f"https://{_HOST}/{_MARKER}/v1"),
            ProviderKey("secret://expert-work/dev/llm/backup", None),
        ]

    async with httpx.AsyncClient(transport=httpx.MockTransport(_shared)) as shared:
        router = await build_llm_router(
            ModelSpec(provider="openai", name="gpt-4o"),
            secret_store=_store(),
            provider_key_resolver=_resolve,
            http_client=shared,
        )
        with pytest.raises(AllProvidersExhaustedError) as excinfo:
            await router(messages=[HumanMessage(content="hi")], tools=[])

    # What the provider client raised (and the router classified) is a network error.
    assert isinstance(excinfo.value.last_exc, LLMNetworkError)
    assert isinstance(excinfo.value.last_exc.__cause__, httpx.ConnectError)
    assert registered_checked_client.dialed == []
    assert backup_calls == []  # today's rule: a network error skips the provider's other keys
    assert _MARKER not in str(excinfo.value)
    assert not [line for line in all_logs.lines if _MARKER in line]
    blocked = [line for line in all_logs.lines if "provider_endpoint.blocked" in line]
    assert len(blocked) == 1
    assert f"host={_HOST}" in blocked[0]
    assert "provider=openai" in blocked[0]
    assert "reason=blocked" in blocked[0]


@pytest.mark.asyncio
async def test_custom_address_request_logs_the_host_and_never_the_path(
    dns: _Dns, registered_checked_client: _Inner, all_logs: _AllLogs
) -> None:
    dns._answers[_HOST] = [[_PUBLIC_A]]
    logging.getLogger().setLevel(logging.DEBUG)  # httpcore must stay quiet even so

    async def _resolve(_provider: str) -> list[ProviderKey]:
        return [
            ProviderKey("secret://expert-work/dev/llm/primary", f"https://{_HOST}/{_MARKER}/v1")
        ]

    router = await build_llm_router(
        ModelSpec(provider="openai", name="gpt-4o"),
        secret_store=_store(),
        provider_key_resolver=_resolve,
    )
    # The canned reply is ``{}`` — an empty completion; only the request matters here.
    await router(messages=[HumanMessage(content="hi")], tools=[])

    assert registered_checked_client.dialed  # the request really went out
    assert not [line for line in all_logs.lines if _MARKER in line]
    request_lines = [line for line in all_logs.lines if "HTTP Request:" in line]
    assert request_lines
    assert f"https://{_HOST} " in request_lines[0]
    assert not [line for line in all_logs.lines if line.startswith("httpcore")]


def test_loading_the_client_module_installs_the_log_filter() -> None:
    # Eval scripts and CLIs never call init_logging; importing the module is enough.
    assert _checked_endpoint.__name__  # imported at the top of this file
    installed = [f for f in logging.getLogger("httpx").filters if isinstance(f, HttpxUrlFilter)]
    assert len(installed) == 1
    assert logging.getLogger("httpcore").level == logging.WARNING


@pytest.mark.asyncio
async def test_error_text_from_a_custom_address_is_cut_to_the_origin() -> None:
    def _raise(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"could not reach {request.url}")

    failing = httpx.AsyncClient(transport=httpx.MockTransport(_raise))
    _http.register_checked_client(failing)

    async def _resolve(_provider: str) -> list[ProviderKey]:
        return [
            ProviderKey("secret://expert-work/dev/llm/primary", f"https://{_HOST}/{_MARKER}/v1")
        ]

    try:
        for provider, model in (("openai", "gpt-4o"), ("anthropic", "claude-sonnet-4-5")):
            router = await build_llm_router(
                ModelSpec(provider=provider, name=model),  # type: ignore[arg-type]
                secret_store=_store(),
                provider_key_resolver=_resolve,
            )
            with pytest.raises(AllProvidersExhaustedError) as excinfo:
                await router(messages=[HumanMessage(content="hi")], tools=[])
            assert isinstance(excinfo.value.last_exc, LLMNetworkError)
            assert _MARKER not in str(excinfo.value)
            assert f"https://{_HOST}" in str(excinfo.value)
    finally:
        _http.unregister_checked_client(failing)
        await failing.aclose()
