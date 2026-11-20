"""The client's IP behind a trusted reverse proxy — B-326.

In the clusters the control plane sits behind the cloud load balancer (Aliyun ALB
→ Service → pod) and uvicorn runs without ``--forwarded-allow-ips``, so
``request.client.host`` is the proxy's address: every caller lands in one
per-IP rate-limit bucket, and an audit row's IP says nothing.

``X-Forwarded-For`` is only believed when the TCP peer is a configured trusted
proxy (``EXPERT_WORK_TRUSTED_PROXY_CIDRS``; empty by default = trust nobody, use
the peer). Then the header is walked right to left, skipping hops that are
themselves trusted proxies; the first untrusted hop is the client. Anything a
client wrote to the left of that hop is never used, so a caller cannot pick its
own bucket by sending a forged header. A malformed hop stops the walk at the last
trusted address.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network

from starlette.requests import Request

IPNetwork = IPv4Network | IPv6Network

_FORWARDED_FOR = "x-forwarded-for"


def parse_trusted_proxies(cidrs: Iterable[str]) -> tuple[IPNetwork, ...]:
    """CIDRs (or bare addresses) → networks. :raises ValueError: on a malformed entry."""
    return tuple(ip_network(c.strip(), strict=False) for c in cidrs if c.strip())


def _parse(raw: str) -> IPv4Address | IPv6Address | None:
    try:
        return ip_address(raw.strip())
    except ValueError:
        return None


def _trusted(addr: IPv4Address | IPv6Address, proxies: Sequence[IPNetwork]) -> bool:
    return any(addr.version == net.version and addr in net for net in proxies)


def client_ip(request: Request, trusted_proxies: Sequence[IPNetwork] = ()) -> str:
    """The client address for rate limiting and audit (see module docstring)."""
    peer = request.client.host if request.client else "unknown"
    if not trusted_proxies:
        return peer
    peer_addr = _parse(peer)
    if peer_addr is None or not _trusted(peer_addr, trusted_proxies):
        return peer
    # A client can send its own X-Forwarded-For line; the proxy then appends its own
    # line (or entry). All lines together, in order, are the one logical list.
    header = ",".join(request.headers.getlist(_FORWARDED_FOR))
    if not header:
        return peer
    current = peer
    for hop in reversed(header.split(",")):
        addr = _parse(hop)
        if addr is None:
            return current
        current = str(addr)
        if not _trusted(addr, trusted_proxies):
            return current
    return current
