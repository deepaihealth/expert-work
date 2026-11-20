"""B-326 M1 —— client IP behind a trusted reverse proxy.

``X-Forwarded-For`` is believed only when the TCP peer is a configured trusted proxy,
walked right to left past trusted hops; empty config = trust nobody.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from control_plane.client_ip import client_ip, parse_trusted_proxies
from control_plane.middleware import RateLimitMiddleware
from control_plane.ratelimit import RateLimitDecision
from control_plane.settings import Settings


def _request(peer: str, xff: str | None = None, *more_lines: str) -> Request:
    lines = ([xff] if xff is not None else []) + list(more_lines)
    headers = [(b"x-forwarded-for", line.encode()) for line in lines]
    return Request({"type": "http", "client": (peer, 1234), "headers": headers, "path": "/"})


_ALB = parse_trusted_proxies(["10.0.0.0/8"])


@pytest.mark.parametrize(
    ("peer", "xff", "trusted", "expected"),
    [
        # Default: trust nobody — the header is ignored entirely.
        ("10.1.2.3", "203.0.113.9", (), "10.1.2.3"),
        # Untrusted peer: a client-supplied header never picks the address.
        ("198.51.100.7", "203.0.113.9", _ALB, "198.51.100.7"),
        # Trusted peer: the right-most untrusted hop is the client.
        ("10.1.2.3", "203.0.113.9", _ALB, "203.0.113.9"),
        # A forged left part is skipped: only what the proxy appended counts.
        ("10.1.2.3", "1.1.1.1, 203.0.113.9", _ALB, "203.0.113.9"),
        # Chained trusted proxies are walked past.
        ("10.1.2.3", "203.0.113.9, 10.9.9.9", _ALB, "203.0.113.9"),
        # Trusted peer, no header → the peer.
        ("10.1.2.3", None, _ALB, "10.1.2.3"),
        # Malformed hop stops at the last trusted address.
        ("10.1.2.3", "not-an-ip", _ALB, "10.1.2.3"),
        ("10.1.2.3", "203.0.113.9, garbage, 10.9.9.9", _ALB, "10.9.9.9"),
    ],
)
def test_client_ip(peer: str, xff: str | None, trusted: tuple[object, ...], expected: str) -> None:
    assert client_ip(_request(peer, xff), trusted) == expected  # type: ignore[arg-type]


def test_multiple_header_lines_are_one_list() -> None:
    """M-d —— a client-sent line plus the proxy-appended line: the proxy's entry wins."""
    request = _request("10.1.2.3", "6.6.6.6", "1.2.3.4")
    assert client_ip(request, _ALB) == "1.2.3.4"


def test_setting_defaults_to_trust_nobody_and_rejects_garbage() -> None:
    assert Settings().trusted_proxy_cidrs == []
    assert Settings(trusted_proxy_cidrs=["10.0.0.0/8", " 192.168.1.1 "]).trusted_proxy_cidrs == [
        "10.0.0.0/8",
        "192.168.1.1",
    ]
    with pytest.raises(ValueError):
        Settings(trusted_proxy_cidrs=["10.0.0.0/33"])


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def acquire(
        self,
        *,
        dimension: str,
        key: str,
        capacity: int | None = None,
        refill_per_sec: float | None = None,
    ) -> RateLimitDecision:
        del capacity, refill_per_sec
        self.calls.append((dimension, key))
        return RateLimitDecision(allowed=True, retry_after_s=0.0, remaining=1.0)


async def _buckets(trusted: list[str], xffs: list[str]) -> list[tuple[str, str]]:
    limiter = _Recorder()
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware, limiter=limiter, trusted_proxies=parse_trusted_proxies(trusted)
    )

    @app.get("/probe")
    async def probe() -> JSONResponse:
        return JSONResponse({"ok": True})

    transport = ASGITransport(app=app, client=("10.0.0.5", 4321))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for xff in xffs:
            await client.get("/probe", headers={"X-Forwarded-For": xff})
    return limiter.calls


@pytest.mark.asyncio
async def test_distinct_clients_behind_a_trusted_proxy_get_distinct_buckets() -> None:
    calls = await _buckets(["10.0.0.0/8"], ["203.0.113.1", "203.0.113.2"])
    assert calls == [("ip", "203.0.113.1"), ("ip", "203.0.113.2")]


@pytest.mark.asyncio
async def test_forwarded_for_from_an_untrusted_peer_is_ignored() -> None:
    calls = await _buckets(["192.168.0.0/16"], ["203.0.113.1", "203.0.113.2"])
    assert calls == [("ip", "10.0.0.5"), ("ip", "10.0.0.5")]
    assert await _buckets([], ["203.0.113.1"]) == [("ip", "10.0.0.5")]
