"""B-308 §4.5.1 — "public https endpoint only" checks for a credential's request address.

``ipaddress`` answers differ between Python versions (``is_global`` for
multicast / NAT64 among them), so every listed address is also asserted against
the explicit network list on its own — the suite must hold on CI's version.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import pytest

from expert_work.common import url_validation
from expert_work.common.url_validation import (
    PublicEndpointError,
    check_public_endpoint,
    is_public_address,
    resolve_public_addresses,
)

_FULLWIDTH_DOT = chr(0xFF0E)

_BLOCKED_IPS = [
    "0.0.0.1",
    "127.0.0.1",
    "10.1.2.3",
    "172.16.41.73",
    "192.168.0.1",
    "169.254.169.254",
    "100.100.100.200",
    "198.18.0.1",
    "224.0.0.1",
    "::1",
    "fd00::1",
    "fe80::1",
    "::ffff:10.0.0.1",
    "64:ff9b::a00:1",
    "2002:a00:1::",
    "::",
    "::10.0.0.1",  # IPv4-compatible (::/96)
    "::ffff:0:10.0.0.1",  # SIIT (::ffff:0:0:0/96)
    "::ffff:0:169.254.169.254",
    "::100.100.100.200",
    "fec0::1",  # deprecated site-local
]

#: The addresses the explicit list must catch by itself, whatever ``is_global`` says.
_EXPLICITLY_LISTED = [
    "0.0.0.1",
    "127.0.0.1",
    "10.1.2.3",
    "172.16.41.73",
    "192.168.0.1",
    "169.254.169.254",
    "100.100.100.200",
    "198.18.0.1",
    "::1",
    "fd00::1",
    "fe80::1",
    "fec0::1",
]


@pytest.mark.parametrize("address", _BLOCKED_IPS)
def test_non_public_address_is_refused(address: str) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is False


@pytest.mark.parametrize("address", _EXPLICITLY_LISTED)
def test_explicit_network_list_catches_the_address_without_is_global(address: str) -> None:
    assert url_validation._in_non_public_network(ipaddress.ip_address(address)) is True


@pytest.mark.parametrize("address", ["224.0.0.1", "ff02::1", "0.0.0.0", "::"])  # noqa: S104
def test_multicast_and_unspecified_are_refused_without_is_global(
    address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Some Python versions call a multicast address global; the check must not rely on it.
    monkeypatch.setattr(url_validation, "_is_global", lambda _ip: True)
    monkeypatch.setattr(url_validation, "_in_non_public_network", lambda _ip: False)
    assert is_public_address(ipaddress.ip_address(address)) is False


@pytest.mark.parametrize(
    "address",
    ["::ffff:10.0.0.1", "64:ff9b::a00:1", "2002:a00:1::", "::10.0.0.1", "::ffff:0:10.0.0.1"],
)
def test_embedded_ipv4_is_judged_as_the_ipv4_it_carries(address: str) -> None:
    embedded = url_validation._embedded_ipv4(ipaddress.IPv6Address(address))
    assert embedded == ipaddress.IPv4Address("10.0.0.1")


@pytest.mark.parametrize(
    "address",
    [
        "8.8.8.8",
        "1.1.1.1",
        "2606:4700:4700::1111",
        "::ffff:8.8.8.8",
        "64:ff9b::808:808",
        "::ffff:0:8.8.8.8",
    ],
)
def test_public_address_is_accepted(address: str) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is True


def test_an_address_outside_every_listed_network_still_needs_is_global() -> None:
    # 192.0.2.0/24 (documentation) is in no explicit network: only ``is_global`` refuses it.
    documentation = ipaddress.ip_address("192.0.2.1")
    assert url_validation._in_non_public_network(documentation) is False
    assert is_public_address(documentation) is False


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "api",
        "x.svc",
        "x.svc.cluster.local",
        "x.cluster.local",
        "x.internal",
        "printer.local",
        "x.localhost",
        f"169{_FULLWIDTH_DOT}254{_FULLWIDTH_DOT}169{_FULLWIDTH_DOT}254",
        "2130706433",
        "0x7f000001",
        "127.1",
        "",
        *_BLOCKED_IPS,
    ],
)
def test_host_that_is_not_a_public_name_is_refused(host: str) -> None:
    with pytest.raises(PublicEndpointError) as excinfo:
        check_public_endpoint(host)
    assert excinfo.value.reason == "blocked"


def test_public_host_is_returned_in_canonical_form() -> None:
    assert check_public_endpoint("Proxy.Example.COM.") == "proxy.example.com"
    assert check_public_endpoint("8.8.8.8") == "8.8.8.8"
    assert check_public_endpoint("bücher.example.com") == "xn--bcher-kva.example.com"


def _stub_dns(monkeypatch: pytest.MonkeyPatch, answers: dict[str, list[str] | OSError]) -> None:
    async def _fake(host: str, port: int) -> list[tuple[Any, ...]]:
        answer = answers[host]
        if isinstance(answer, OSError):
            raise answer
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

    monkeypatch.setattr(url_validation, "_getaddrinfo", _fake)


@pytest.mark.asyncio
async def test_resolve_returns_every_address_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns(monkeypatch, {"proxy.example.com": ["8.8.8.8", "2606:4700:4700::1111", "8.8.8.8"]})
    assert await resolve_public_addresses("proxy.example.com", 443) == [
        "8.8.8.8",
        "2606:4700:4700::1111",
    ]


@pytest.mark.asyncio
async def test_one_private_answer_refuses_the_whole_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns(monkeypatch, {"mixed.example.com": ["8.8.8.8", "10.0.0.5"]})
    with pytest.raises(PublicEndpointError) as excinfo:
        await resolve_public_addresses("mixed.example.com", 443)
    assert excinfo.value.reason == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [OSError("nodename nor servname provided"), []])
async def test_name_that_does_not_resolve_is_unresolvable(
    monkeypatch: pytest.MonkeyPatch, answer: list[str] | OSError
) -> None:
    _stub_dns(monkeypatch, {"gone.example.com": answer})
    with pytest.raises(PublicEndpointError) as excinfo:
        await resolve_public_addresses("gone.example.com", 443)
    assert excinfo.value.reason == "unresolvable"


@pytest.mark.asyncio
async def test_ip_literal_is_checked_without_a_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns(monkeypatch, {})
    assert await resolve_public_addresses("8.8.8.8", 443) == ["8.8.8.8"]
    with pytest.raises(PublicEndpointError):
        await resolve_public_addresses("100.100.100.200", 443)


@pytest.mark.asyncio
async def test_internal_name_is_refused_before_any_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns(monkeypatch, {})  # a lookup would raise KeyError
    with pytest.raises(PublicEndpointError) as excinfo:
        await resolve_public_addresses("db.svc.cluster.local", 443)
    assert excinfo.value.reason == "blocked"


def test_existing_static_guard_is_unchanged() -> None:
    # B-308 §7.3 — the older guard (MCP / webhook) keeps its behaviour, gap included.
    assert url_validation.validate_remote_host("100.100.100.200") == "100.100.100.200"
    assert url_validation.validate_remote_host("api") == "api"


@pytest.mark.asyncio
async def test_a_lookup_that_hangs_is_unresolvable_after_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import time

    async def _hang(host: str, port: int) -> list[tuple[Any, ...]]:
        await asyncio.sleep(30)
        return []

    monkeypatch.setattr(url_validation, "_getaddrinfo", _hang)
    started = time.monotonic()
    with pytest.raises(PublicEndpointError) as excinfo:
        await resolve_public_addresses("slow.example.com", 443, timeout_s=0.05)
    assert excinfo.value.reason == "unresolvable"
    assert time.monotonic() - started < 2
