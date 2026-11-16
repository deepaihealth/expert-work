"""Remote-URL validation — SSRF guard for tenant-supplied MCP server URLs.

A tenant registers a remote MCP server by URL; the control plane then
*connects out* to that URL (registration probe + runtime tool calls). An
unchecked URL lets a tenant point the platform at internal services or the
cloud metadata endpoint (169.254.169.254) — a classic SSRF. This guard is
applied at every connect-out site (registration, probe, runtime).

The check is static (scheme + IP-literal ranges + localhost names). It does
NOT resolve DNS, so it does not stop DNS-rebind (a hostname that resolves to a
public IP at check time and a private one at connect time). By decision, that
defense lives at the infrastructure egress layer (deny RFC1918 / loopback /
link-local egress from the control plane), NOT in this module — see
ADR-0009. This static guard is the defense-in-depth first layer: it blocks the
common cases (literal private IPs, localhost, metadata IP) cheaply.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from typing import Any, Literal
from urllib.parse import urlparse

_LOCALHOST_NAMES = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
)


class RemoteURLError(ValueError):
    """A URL fails remote-endpoint validation (unsupported scheme or SSRF risk)."""


#: Explicit private/internal ranges that are a genuine SSRF / infra threat
#: (RFC1918 + IPv6 unique-local). We do NOT use ``ipaddress.is_private`` here:
#: as of CPython 3.12.4+ it also returns True for RFC2544 benchmarking
#: (198.18.0.0/15), TEST-NET docs ranges, and 240.0.0.0/4 — none of which are an
#: infra threat, and 198.18/15 is exactly what a local fake-ip DNS maps public
#: hosts into, so blanket ``is_private`` over-blocks (audit-eval Phase 2). These
#: harmless reserved ranges are non-routable / synthetic → allowed + audited at
#: egress. See docs/design/sandbox-audit-evaluation.md.
_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),  # IPv6 unique-local
)


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Precise, version-stable predicates for the always-dangerous ranges...
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        # is_link_local includes 169.254.0.0/16 (cloud metadata 169.254.169.254).
        return True
    # ...plus the explicit private/internal networks above.
    return any(ip in net for net in _BLOCKED_NETWORKS if ip.version == net.version)


#: Characters UTS-46 / IDNA fold to an ASCII label separator (``.``) before a
#: resolver — or httpx — connects. ``urlparse`` leaves them intact, so a guard
#: that inspects the raw host sees a "hostname" while the client dials the
#: ASCII-dotted form (e.g. an ideographic-dot spelling of an IPv4 quad becomes
#: ``169.254.169.254`` — cloud metadata). Fold them up front so the IP-literal
#: checks below see what is actually dialed. U+3002, U+FF0E, U+FF61.
_IDNA_LABEL_SEPARATORS: tuple[str, ...] = (chr(0x3002), chr(0xFF0E), chr(0xFF61))


def normalize_host(host: str) -> str:
    """Fold IDNA label-separator variants to ASCII ``.`` and strip a trailing dot.

    Mirrors the host a resolver / httpx actually dials, closing the
    parser-normalization differential a raw-string check would leave open.
    """
    for sep in _IDNA_LABEL_SEPARATORS:
        host = host.replace(sep, ".")
    return host.rstrip(".")


def validate_remote_host(host: str) -> str:
    """Static SSRF check on a **host** (no scheme, no DNS).

    Rejects a localhost name, a non-canonical IP literal, or a private /
    loopback / link-local / metadata / multicast / unspecified IP literal. The
    host is IDNA-normalized first (:func:`normalize_host`) so a Unicode-dot
    spelling of an IP cannot slip past. Returns ``host`` unchanged when it is a
    plausibly-public name; raises :class:`RemoteURLError` otherwise.
    """
    cleaned = normalize_host(host)

    if not cleaned:
        msg = "host is empty"
        raise RemoteURLError(msg)

    if cleaned.lower() in _LOCALHOST_NAMES:
        msg = f"localhost address {cleaned!r} not allowed"
        raise RemoteURLError(msg)

    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError:
        # Non-canonical IP literals (decimal 2130706433, hex 0x7f000001,
        # shortened/octal dotted 127.1 / 0177.0.0.1) parse as private addrs in
        # many HTTP stacks but not in ``ipaddress`` — reject them explicitly.
        if re.fullmatch(r"[0-9.]+", cleaned) or re.fullmatch(r"0[xX][0-9a-fA-F]+", cleaned):
            msg = f"non-canonical IP literal {cleaned!r} not allowed"
            raise RemoteURLError(msg) from None
        return host

    if _ip_is_blocked(ip):
        msg = f"private/loopback/link-local IP {cleaned!r} not allowed"
        raise RemoteURLError(msg)

    return host


def validate_remote_url(
    url: str,
    *,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
) -> str:
    """Validate a tenant-supplied remote URL for safe connect-out.

    Returns ``url`` unchanged when valid. Raises :class:`RemoteURLError` for
    an unsupported scheme, a missing hostname, a localhost name, or a
    private / loopback / link-local / reserved / multicast / unspecified IP
    literal.

    ``allowed_schemes`` defaults to ``("http", "https")``; pass
    ``("https",)`` to forbid plaintext (production).
    """
    parsed = urlparse(url)

    if parsed.scheme not in allowed_schemes:
        msg = f"unsupported URL scheme {parsed.scheme!r}; allowed: {allowed_schemes}"
        raise RemoteURLError(msg)

    hostname = parsed.hostname
    if not hostname:
        msg = f"URL has no hostname: {url!r}"
        raise RemoteURLError(msg)

    validate_remote_host(hostname)
    return url


def resolve_and_pin_host(host: str, port: int = 443) -> str:
    """Resolve ``host`` and return a single safe, **pinned** IP to connect to.

    Unlike :func:`validate_remote_url` (static, no DNS), this is for the egress
    proxy's connect-out: it resolves the name, rejects the connection if *any*
    resolved address is private/loopback/link-local/metadata, and returns the
    first allowed IP. The caller MUST connect to that returned IP (not re-resolve
    the name) — pinning is what closes the DNS-rebind window between check and
    connect (the gap :func:`validate_remote_url` leaves to the infra layer).

    Raises :class:`RemoteURLError` on a localhost name, a non-canonical IP
    literal, a blocked address, or an unresolvable host.
    """
    cleaned = normalize_host(host)
    if cleaned.lower() in _LOCALHOST_NAMES:
        msg = f"localhost address {cleaned!r} not allowed"
        raise RemoteURLError(msg)

    # Reject the non-canonical IP literals ``ipaddress`` would refuse but an
    # HTTP/socket stack may accept as a private addr (decimal/hex/octal forms).
    if (
        re.fullmatch(r"[0-9.]+", cleaned) or re.fullmatch(r"0[xX][0-9a-fA-F]+", cleaned)
    ) and not _is_canonical_ip(cleaned):
        msg = f"non-canonical IP literal {cleaned!r} not allowed"
        raise RemoteURLError(msg)

    try:
        infos = socket.getaddrinfo(cleaned, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        msg = f"could not resolve host {cleaned!r}: {exc}"
        raise RemoteURLError(msg) from exc

    for info in infos:
        addr = info[4][0]
        ip = ipaddress.ip_address(addr)
        if _ip_is_blocked(ip):
            # Any blocked address among the results aborts — refuse to "pick a
            # good one" when a name also resolves to a private target.
            msg = f"host {cleaned!r} resolves to a blocked address {addr!r}"
            raise RemoteURLError(msg)

    if not infos:
        msg = f"host {cleaned!r} did not resolve to any address"
        raise RemoteURLError(msg)
    return str(infos[0][4][0])


def _is_canonical_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# B-308 — "public endpoint only" checks for a platform credential's request
# address. Stricter than the guard above (which MCP / webhook callers keep
# using unchanged): there is no allow-list, internal-looking names are refused
# outright, and every resolved address must be publicly routable.
# ---------------------------------------------------------------------------

PublicEndpointRefusal = Literal["blocked", "unresolvable"]


class PublicEndpointError(RemoteURLError):
    """A host is not a public endpoint. ``reason`` says which way it failed:
    ``blocked`` (an internal name or a non-public address) or ``unresolvable``."""

    def __init__(self, message: str, *, reason: PublicEndpointRefusal) -> None:
        super().__init__(message)
        self.reason: PublicEndpointRefusal = reason


#: Checked in addition to ``is_global``, whose answer differs between Python
#: versions. ``100.64.0.0/10`` holds a cloud vendor's metadata and internal DNS.
_NON_PUBLIC_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fec0::/10"),  # deprecated site-local
)

_NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")
_6TO4_PREFIX = ipaddress.IPv6Network("2002::/16")
#: IPv4-compatible (``::a.b.c.d``, deprecated) and SIIT (``::ffff:0:a.b.c.d``):
#: the IPv4 address is the low 32 bits, as with NAT64.
_LOW32_PREFIXES = (
    _NAT64_PREFIX,
    ipaddress.IPv6Network("::/96"),
    ipaddress.IPv6Network("::ffff:0:0:0/96"),
)

#: Name suffixes that only resolve inside a host, a LAN or a cluster.
_INTERNAL_NAME_SUFFIXES = (".local", ".internal", ".svc", ".localhost")

_IPv4_BITS = 32


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 one carries (IPv4-mapped, NAT64, IPv4-compatible,
    SIIT, 6to4), if any. ``::`` and ``::1`` come out as ``0.0.0.0`` / ``0.0.0.1``
    — not public either way."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if any(ip in prefix for prefix in _LOW32_PREFIXES):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip in _6TO4_PREFIX:
        return ipaddress.IPv4Address((int(ip) >> (128 - 16 - _IPv4_BITS)) & 0xFFFFFFFF)
    return None


def _in_non_public_network(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(ip in net for net in _NON_PUBLIC_NETWORKS if ip.version == net.version)


def _is_global(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_global


def is_public_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether ``ip`` is a publicly routable address.

    An IPv6 address that carries an IPv4 one is judged as that IPv4 address.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            ip = embedded
    if ip.is_multicast or ip.is_unspecified:
        return False
    if _in_non_public_network(ip):
        return False
    return _is_global(ip)


def check_public_endpoint(host: str) -> str:
    """Static check that ``host`` can be a public endpoint (no DNS).

    Refuses a localhost name, a single-label name, an internal-only suffix
    (``.local`` / ``.internal`` / ``.svc`` / ``.cluster.local``), a
    non-canonical IP literal, and a non-public IP literal. Returns the
    canonical host: IDNA label separators folded, lower-cased, punycode,
    without a trailing dot.

    :raises PublicEndpointError: ``reason="blocked"``.
    """
    cleaned = normalize_host(host).lower()
    if not cleaned:
        msg = "host is empty"
        raise PublicEndpointError(msg, reason="blocked")
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError:
        pass
    else:
        if not is_public_address(ip):
            msg = "address is not a public address"
            raise PublicEndpointError(msg, reason="blocked")
        return cleaned
    if re.fullmatch(r"[0-9.]+", cleaned) or re.fullmatch(r"0x[0-9a-f]+", cleaned):
        msg = "non-canonical IP literal not allowed"
        raise PublicEndpointError(msg, reason="blocked")
    try:
        ascii_host = cleaned.encode("idna").decode("ascii")
    except UnicodeError as exc:
        msg = "host name is not valid"
        raise PublicEndpointError(msg, reason="blocked") from exc
    if (
        ascii_host in _LOCALHOST_NAMES
        or "." not in ascii_host
        or ascii_host.endswith(_INTERNAL_NAME_SUFFIXES)
    ):
        msg = "host is an internal name, not a public one"
        raise PublicEndpointError(msg, reason="blocked")
    return ascii_host


async def _getaddrinfo(host: str, port: int) -> list[tuple[Any, ...]]:
    """Resolve on the event loop's resolver (never blocks the loop)."""
    loop = asyncio.get_running_loop()
    return list(await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM))


async def resolve_public_addresses(
    host: str, port: int, *, timeout_s: float | None = None
) -> list[str]:
    """Resolve ``host`` and return **every** address, in resolver order — all
    of them public, or none at all. A lookup that takes longer than
    ``timeout_s`` counts as ``unresolvable``.

    The caller connects to the returned addresses (never to the name again):
    that is what keeps a name from answering "public" to the check and
    "internal" to the connection.

    :raises PublicEndpointError: ``blocked`` when the host fails
        :func:`check_public_endpoint` or any resolved address is not public;
        ``unresolvable`` when the name does not resolve.
    """
    canonical = check_public_endpoint(host)
    try:
        ipaddress.ip_address(canonical)
    except ValueError:
        pass
    else:
        return [canonical]
    try:
        async with asyncio.timeout(timeout_s):
            infos = await _getaddrinfo(canonical, port)
    except (OSError, TimeoutError) as exc:
        msg = "host name could not be resolved"
        raise PublicEndpointError(msg, reason="unresolvable") from exc
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if not is_public_address(ipaddress.ip_address(address.split("%", 1)[0])):
            msg = "host resolves to an address that is not public"
            raise PublicEndpointError(msg, reason="blocked")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        msg = "host name did not resolve to any address"
        raise PublicEndpointError(msg, reason="unresolvable")
    return addresses
