"""The request address on a platform credential: validation and comparison — B-308.

A provider credential (a platform key or a per-tenant override) may carry a
**request address**: the API root, up to and including the version segment
(``https://proxy.example.com/v1``); the platform appends the provider's own
path per kind of call. This module holds what the write endpoints need:

* :func:`normalize_base_url` — syntax and per-provider format (§4.5.1 steps 1-3);
* :func:`ensure_public_address` — the host must be a public one, by name and by
  every address it resolves to (steps 4-5; the same check the connection layer
  repeats on every new connection);
* :func:`requires_new_key` — "a new address needs a new key" (§4.2);
* :func:`audit_fields` — what an audit row may say about an address.

Messages never echo the address: its path may carry a token.
"""

from __future__ import annotations

import hashlib
import ipaddress
import unicodedata
from urllib.parse import urlsplit

from expert_work.common.url_validation import (
    PublicEndpointError,
    normalize_host,
    resolve_public_addresses,
)
from expert_work.runtime.secret_store import InvalidSecretRefError, parse_secret_ref
from orchestrator.llm.endpoints import (
    API_PATHS,
    DOCS_OMIT_VERSION_SEGMENT,
    OFFICIAL_BASE_URLS,
    VERSION_SUFFIXES,
    endpoint_host,
)

_MAX_LENGTH = 2048
_DEFAULT_PORT = 443
#: A save waits this long for the address's name to resolve.
_RESOLVE_TIMEOUT_S = 5.0
_FINGERPRINT_CHARS = 12


class BaseUrlRejectedError(ValueError):
    """A request address was refused. ``code`` is the API error code; ``extra``
    carries the structured fields a client needs to explain it."""

    def __init__(self, code: str, message: str, **extra: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra: dict[str, object] = dict(extra)

    def detail(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, **self.extra}


def official_base_url(provider: str) -> str | None:
    """The provider's official root address (the form a request address takes);
    ``None`` for a provider that takes its address from the agent manifest."""
    return OFFICIAL_BASE_URLS.get(provider)


def _invalid(reason: str) -> BaseUrlRejectedError:
    return BaseUrlRejectedError("BASE_URL_INVALID", f"the request address is not valid: {reason}")


#: Variants of ``.`` that name resolution folds to an ASCII dot.
_DOT_LOOKALIKES = frozenset({chr(0x3002), chr(0xFF0E), chr(0xFF61)})


def _deceptive_host_character(host: str) -> bool:
    """Whether ``host`` holds a character that shows as nothing or as a
    look-alike of ASCII: invisible / formatting / control characters and
    spaces, full-width forms, and the dot variants. (Ordinary non-ASCII letters
    are fine — an internationalised name is stored as punycode.)"""
    for ch in host:
        if ch.isascii():
            continue
        if ch in _DOT_LOOKALIKES or unicodedata.east_asian_width(ch) == "F":
            return True
        if unicodedata.category(ch)[0] in ("C", "Z"):
            return True
    return False


def _stored_form(hostname: str, port: int | None, path: str) -> str:
    """The address as stored: canonical ASCII host (lower case, punycode, no
    trailing dot), the port unless it is the default, then the path."""
    host = _canonical_host(hostname)
    try:
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        pass  # not an IP literal: a host name needs no brackets
    netloc = host if port in (None, _DEFAULT_PORT) else f"{host}:{port}"
    return f"https://{netloc}{path}"


def normalize_base_url(provider: str, raw: str) -> str | None:
    """Validate ``raw`` as ``provider``'s request address and return its stored
    form: rebuilt from the canonical ASCII host, no trailing ``/``. ``None``
    when that is exactly the provider's official root — "use the official
    address", which is what no request address means.

    :raises BaseUrlRejectedError: ``BASE_URL_NOT_SUPPORTED`` (the provider takes
        no request address), ``BASE_URL_INVALID`` (not a plain https URL) or
        ``BASE_URL_FORMAT`` (the path does not end with the provider's version
        segment, or already includes an API path).
    """
    suffix = VERSION_SUFFIXES.get(provider)
    if suffix is None:
        raise BaseUrlRejectedError(
            "BASE_URL_NOT_SUPPORTED",
            f"a request address cannot be set for provider {provider!r}",
        )
    text = raw.strip()
    if len(text) > _MAX_LENGTH:
        raise _invalid(f"it is longer than {_MAX_LENGTH} characters")
    if any(ch.isspace() or ord(ch) < 0x20 or ch == "\\" for ch in text):
        raise _invalid("it contains whitespace or a backslash")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise _invalid("it cannot be parsed as a URL") from None
    if parts.scheme != "https":
        raise _invalid("only https addresses are supported")
    if "@" in parts.netloc:
        raise _invalid("it must not contain a user name or password")
    if "?" in text or "#" in text:
        raise _invalid("it must not contain a query string or a fragment")
    if not parts.hostname:
        raise _invalid("it has no host name")
    if _deceptive_host_character(parts.hostname):
        raise _invalid("its host name contains invisible or full-width characters")
    try:
        parts.hostname.encode("idna")
    except UnicodeError:
        raise _invalid("its host name is not a valid domain name") from None
    path = parts.path.rstrip("/")
    for api_path in API_PATHS:
        if path.endswith(api_path):
            raise BaseUrlRejectedError(
                "BASE_URL_FORMAT",
                "enter the address up to the version segment only; "
                f"do not include {api_path} — the platform appends it",
                hint="includes_api_path",
                expected_suffix=suffix,
                example=_example(suffix),
            )
    if not path.endswith(suffix):
        message = (
            f"the request address for {provider} must end with {suffix}, "
            f"for example {_example(suffix)}"
        )
        hint = "version_segment"
        if provider in DOCS_OMIT_VERSION_SEGMENT:
            hint = "version_segment_required"
            message += (
                f" — include {suffix} here even if the vendor's documentation "
                "shows the address without it"
            )
        raise BaseUrlRejectedError(
            "BASE_URL_FORMAT",
            message,
            hint=hint,
            expected_suffix=suffix,
            example=_example(suffix),
        )
    stored = _stored_form(parts.hostname, port, path)
    return None if stored == OFFICIAL_BASE_URLS[provider] else stored


def _example(suffix: str) -> str:
    return f"https://your-host{suffix}"


def _canonical_host(hostname: str) -> str:
    host = normalize_host(hostname).lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def requires_new_key(
    *,
    before: tuple[str, str | None] | None,
    after: tuple[str, str | None],
    pasted: bool,
) -> bool:
    """Whether a save must be refused because the address changed but the key
    did not (§4.2 — "a new address needs a new key").

    ``before`` / ``after`` are the row's ``(secret_ref, base_url)`` before and
    after the save, addresses in stored form (``None`` = official; ``before`` is
    ``None`` for a new row). Guards against a hand error: pointing a key at a
    relay and forgetting to replace the key, so the vendor-issued key goes to a
    third party. A changed, non-official address is fine only with a pasted
    key or a ref other than the row's current one. A new row is written with
    its own key, and clearing back to the official address needs nothing.
    """
    after_ref, after_url = after
    if pasted or after_url is None or before is None:
        return False
    before_ref, before_url = before
    return after_url != before_url and _same_secret(after_ref, before_ref)


def _same_secret(one: str, other: str) -> bool:
    """Whether two refs name the same stored secret: ``secret://x``,
    ``secret://x/`` and ``kms://x`` all read the slot ``x``."""
    try:
        return parse_secret_ref(one) == parse_secret_ref(other)
    except InvalidSecretRefError:
        return one == other


async def ensure_public_address(base_url: str) -> None:
    """The address's host must be public: not an internal name, and every
    address it resolves to publicly routable (resolved on the event loop).

    :raises BaseUrlRejectedError: ``BASE_URL_BLOCKED_HOST`` or
        ``BASE_URL_UNRESOLVABLE``. Neither can be overridden by "save anyway".
    """
    parts = urlsplit(base_url)
    try:
        await resolve_public_addresses(
            parts.hostname or "", parts.port or _DEFAULT_PORT, timeout_s=_RESOLVE_TIMEOUT_S
        )
    except PublicEndpointError as exc:
        if exc.reason == "unresolvable":
            raise BaseUrlRejectedError(
                "BASE_URL_UNRESOLVABLE", "the request address's host name could not be resolved"
            ) from exc
        raise BaseUrlRejectedError(
            "BASE_URL_BLOCKED_HOST",
            "the request address is not allowed: only public https addresses are supported",
        ) from exc


def audit_fields(base_url: str | None) -> dict[str, object]:
    """What an audit row records about a request address: the host and a
    fingerprint of the path (so a path change is visible) — never the address
    itself, whose path may carry a token."""
    if base_url is None:
        return {"base_url_host": None, "base_url_path_fp": None}
    path = urlsplit(base_url).path
    return {
        "base_url_host": endpoint_host(base_url),
        "base_url_path_fp": hashlib.sha256(path.encode()).hexdigest()[:_FINGERPRINT_CHARS],
    }
