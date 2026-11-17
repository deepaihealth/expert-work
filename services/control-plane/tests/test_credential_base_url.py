"""B-308 §4.5.1 / §4.2 — what a credential's request address may look like,
when a save needs a new key, and whether the address is public."""

from __future__ import annotations

import socket
from typing import Any

import pytest

from control_plane.credential_base_url import (
    BaseUrlRejectedError,
    audit_fields,
    ensure_public_address,
    normalize_base_url,
    requires_new_key,
)
from expert_work.common import url_validation
from orchestrator.llm.endpoints import OFFICIAL_BASE_URLS

_FULLWIDTH_DOT = chr(0xFF0E)


def _rejected(provider: str, raw: str) -> BaseUrlRejectedError:
    with pytest.raises(BaseUrlRejectedError) as excinfo:
        normalize_base_url(provider, raw)
    return excinfo.value


# --- syntax -------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "http://proxy.example.com/v1",
        "ftp://proxy.example.com/v1",
        "proxy.example.com/v1",
        "https://user:pass@proxy.example.com/v1",
        "https://user@proxy.example.com/v1",
        "https://proxy.example.com/v1?a=1",
        "https://proxy.example.com/v1?",
        "https://proxy.example.com/v1#x",
        "https://proxy.example.com/v1#",
        "https:///v1",
        "https://proxy.example.com:99999/v1",
        "https://proxy.example.com:abc/v1",
        "https://proxy.example.com/a b/v1",
        "https://proxy.example.com\\evil/v1",
        "https://proxy.example.com/" + "a" * 2048 + "/v1",
    ],
)
def test_malformed_address_is_invalid(raw: str) -> None:
    error = _rejected("openai", raw)
    assert error.code == "BASE_URL_INVALID"
    assert raw not in error.message  # the path can carry a token: never echoed


@pytest.mark.parametrize(
    "host",
    [
        "proxy\u200b.example.com",  # zero-width space
        "pro\u00adxy.example.com",  # soft hyphen
        "proxy\u2060.example.com",  # word joiner
        "\uff50roxy.example.com",  # full-width letter
        f"169{_FULLWIDTH_DOT}254{_FULLWIDTH_DOT}169{_FULLWIDTH_DOT}254",
        "proxy.example\u3002com",  # ideographic full stop
        "proxy.exam ple.com",
        "proxy.example.com\u202e",  # right-to-left override
    ],
)
def test_host_with_invisible_or_full_width_characters_is_invalid(host: str) -> None:
    """What the admin sees must be what gets dialed: characters that render as
    nothing, or as a look-alike of ASCII, are refused before anything else."""
    assert _rejected("openai", f"https://{host}/v1").code == "BASE_URL_INVALID"


def test_only_https_is_named_as_the_reason_for_http() -> None:
    assert "https" in _rejected("openai", "http://proxy.example.com/v1").message


@pytest.mark.parametrize("provider", ["azure", "self-hosted"])
def test_manifest_addressed_providers_do_not_take_an_address(provider: str) -> None:
    assert _rejected(provider, "https://proxy.example.com/v1").code == "BASE_URL_NOT_SUPPORTED"


# --- version segment -----------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "raw", "normalized"),
    [
        ("openai", "https://proxy.example.com/v1", "https://proxy.example.com/v1"),
        ("openai", "https://proxy.example.com/v1/", "https://proxy.example.com/v1"),
        (
            "openai",
            "  https://proxy.example.com/team-a/v1//  ",
            "https://proxy.example.com/team-a/v1",
        ),
        ("openai", "https://proxy.example.com:8443/v1", "https://proxy.example.com:8443/v1"),
        ("openai", "https://PROXY.Example.com/Team-A/v1", "https://proxy.example.com/Team-A/v1"),
        ("openai", "https://proxy.example.com./v1", "https://proxy.example.com/v1"),
        ("openai", "https://proxy.example.com:443/v1", "https://proxy.example.com/v1"),
        ("openai", "https://bücher.example.com/v1", "https://xn--bcher-kva.example.com/v1"),
        (
            "openai",
            "https://[2606:4700:4700::1111]:8443/v1",
            "https://[2606:4700:4700::1111]:8443/v1",
        ),
        ("anthropic", "https://proxy.example.com/v1", "https://proxy.example.com/v1"),
        ("glm", "https://proxy.example.com/api/paas/v4", "https://proxy.example.com/api/paas/v4"),
        ("glm", "https://proxy.example.com/v4", "https://proxy.example.com/v4"),
        (
            "doubao",
            "https://ark.cn-shanghai.example.com/api/v3",
            "https://ark.cn-shanghai.example.com/api/v3",
        ),
        (
            "qwen",
            "https://proxy.example.com/x/compatible-mode/v1",
            "https://proxy.example.com/x/compatible-mode/v1",
        ),
    ],
)
def test_well_formed_address_is_accepted_and_normalized(
    provider: str, raw: str, normalized: str
) -> None:
    assert normalize_base_url(provider, raw) == normalized


@pytest.mark.parametrize("provider", sorted(OFFICIAL_BASE_URLS))
def test_the_official_root_itself_means_no_request_address(provider: str) -> None:
    """Typing the placeholder back in is "use the official address": stored as
    none, so the key keeps going through the ordinary client."""
    official = OFFICIAL_BASE_URLS[provider]
    assert normalize_base_url(provider, official) is None
    assert normalize_base_url(provider, f"  {official}/ ") is None
    host = official.split("/")[2]
    assert normalize_base_url(provider, official.replace(host, host.upper(), 1)) is None
    assert normalize_base_url(provider, official.replace(host, f"{host}:443", 1)) is None
    assert normalize_base_url(provider, official.replace(host, f"{host}.", 1)) is None


def test_another_path_on_the_official_host_is_still_a_request_address() -> None:
    assert normalize_base_url("openai", "https://api.openai.com/eu/v1") == (
        "https://api.openai.com/eu/v1"
    )


@pytest.mark.parametrize(
    ("provider", "raw", "suffix"),
    [
        ("openai", "https://proxy.example.com", "/v1"),
        ("openai", "https://proxy.example.com/v2", "/v1"),
        ("openai", "https://proxy.example.com/xv1", "/v1"),
        ("kimi", "https://proxy.example.com/", "/v1"),
        ("glm", "https://proxy.example.com/api/paas", "/v4"),
        ("doubao", "https://proxy.example.com/api", "/v3"),
        ("qwen", "https://proxy.example.com/v1", "/compatible-mode/v1"),
        ("qwen", "https://proxy.example.com/compatible-mode", "/compatible-mode/v1"),
    ],
)
def test_missing_version_segment_is_a_format_error_with_the_right_example(
    provider: str, raw: str, suffix: str
) -> None:
    error = _rejected(provider, raw)
    assert error.code == "BASE_URL_FORMAT"
    assert error.extra["expected_suffix"] == suffix
    assert str(error.extra["example"]).endswith(suffix)
    assert suffix in error.message


@pytest.mark.parametrize("provider", ["anthropic", "deepseek"])
def test_vendors_whose_docs_omit_the_version_get_a_dedicated_hint(provider: str) -> None:
    error = _rejected(provider, "https://proxy.example.com")
    assert error.code == "BASE_URL_FORMAT"
    assert error.extra["hint"] == "version_segment_required"
    assert "documentation" in error.message
    # Vendors that document the versioned root do not get that sentence.
    assert "documentation" not in _rejected("openai", "https://proxy.example.com").message


@pytest.mark.parametrize(
    ("provider", "raw", "path"),
    [
        ("openai", "https://proxy.example.com/v1/chat/completions", "/chat/completions"),
        ("openai", "https://proxy.example.com/v1/embeddings", "/embeddings"),
        ("anthropic", "https://proxy.example.com/v1/messages", "/messages"),
        ("glm", "https://proxy.example.com/api/paas/v4/rerank/", "/rerank"),
        ("glm", "https://proxy.example.com/api/paas/v4/layout_parsing", "/layout_parsing"),
    ],
)
def test_address_that_already_includes_the_api_path_says_so(
    provider: str, raw: str, path: str
) -> None:
    error = _rejected(provider, raw)
    assert error.code == "BASE_URL_FORMAT"
    assert error.extra["hint"] == "includes_api_path"
    assert path in error.message


# --- "a new address needs a new key" (§4.2) ----------------------------------------
# Addresses here are already in stored form (``normalize_base_url``), so equal
# addresses are equal strings: case, a trailing dot and ``:443`` are folded
# before the rule ever sees them.

_A = "https://a.example.com/v1"
_A_OTHER_PATH = "https://a.example.com/team-b/v1"
_A_OTHER_PORT = "https://a.example.com:8443/v1"
_B = "https://b.example.com/v1"
_REF = "secret://expert-work/platform/llm/openai"
_OTHER_REF = "secret://someone/elses-key"


def _needs(
    before: tuple[str, str | None] | None, after: tuple[str, str | None], pasted: bool = False
) -> bool:
    return requires_new_key(before=before, after=after, pasted=pasted)


@pytest.mark.parametrize(
    ("before_url", "after_url"),
    [
        (None, _A),  # official -> custom
        (_A, _B),  # another host
        (_A, _A_OTHER_PORT),  # only the port
        (_A, _A_OTHER_PATH),  # only the path (one host, accounts told apart by path)
    ],
)
def test_a_changed_address_with_the_same_key_is_refused(
    before_url: str | None, after_url: str
) -> None:
    assert _needs((_REF, before_url), (_REF, after_url)) is True


@pytest.mark.parametrize(
    ("before_url", "after_url"),
    [(None, _A), (_A, _B), (_A, _A_OTHER_PATH)],
)
def test_a_changed_address_with_a_pasted_key_or_another_ref_is_fine(
    before_url: str | None, after_url: str
) -> None:
    assert _needs((_REF, before_url), (_REF, after_url), pasted=True) is False
    assert _needs((_REF, before_url), (_OTHER_REF, after_url)) is False


@pytest.mark.parametrize("same_key", [f"{_REF}/", _REF.replace("secret://", "kms://")])
def test_another_spelling_of_the_same_ref_is_not_a_new_key(same_key: str) -> None:
    """``secret://x/`` and ``kms://x`` read the same slot as ``secret://x``."""
    assert _needs((_REF, _A), (same_key, _B)) is True


def test_an_unchanged_address_needs_nothing() -> None:
    assert _needs((_REF, _A), (_REF, _A)) is False
    assert _needs((_REF, None), (_REF, None)) is False


def test_clearing_back_to_official_needs_nothing() -> None:
    assert _needs((_REF, _A), (_REF, None)) is False


def test_a_new_row_brings_its_own_key() -> None:
    """A new row is always written with a key (pasted or by ref): that is the
    key it is being set up with, so nothing was "forgotten"."""
    assert _needs(None, (_REF, _A)) is False
    assert _needs(None, (_OTHER_REF, None)) is False


# --- public address ----------------------------------------------------------------


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
@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.41.73",
        "192.168.0.1",
        "169.254.169.254",
        "100.100.100.200",
        "198.18.0.1",
        "224.0.0.1",
        "[::1]",
        "[fd00::1]",
        "[fe80::1]",
        "[::ffff:10.0.0.1]",
        "[64:ff9b::a00:1]",
        "[2002:a00:1::]",
        "localhost",
        "api",
        "x.svc",
        "x.svc.cluster.local",
        "x.internal",
        f"169{_FULLWIDTH_DOT}254{_FULLWIDTH_DOT}169{_FULLWIDTH_DOT}254",
    ],
)
async def test_internal_host_is_blocked_without_a_lookup(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_dns(monkeypatch, {})  # any lookup would raise KeyError
    with pytest.raises(BaseUrlRejectedError) as excinfo:
        await ensure_public_address(f"https://{host}/v1")
    assert excinfo.value.code == "BASE_URL_BLOCKED_HOST"


@pytest.mark.asyncio
async def test_name_with_one_internal_answer_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns(monkeypatch, {"mixed.example.com": ["93.184.216.34", "10.0.0.5"]})
    with pytest.raises(BaseUrlRejectedError) as excinfo:
        await ensure_public_address("https://mixed.example.com/secret-path/v1")
    assert excinfo.value.code == "BASE_URL_BLOCKED_HOST"
    assert "secret-path" not in excinfo.value.message
    assert "10.0.0.5" not in excinfo.value.message


@pytest.mark.asyncio
async def test_name_that_does_not_resolve_is_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_dns(monkeypatch, {"gone.example.com": OSError("no such host")})
    with pytest.raises(BaseUrlRejectedError) as excinfo:
        await ensure_public_address("https://gone.example.com/v1")
    assert excinfo.value.code == "BASE_URL_UNRESOLVABLE"


@pytest.mark.asyncio
async def test_public_name_passes_and_is_resolved_on_its_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_dns(monkeypatch, {"proxy.example.com": ["93.184.216.34"]})
    await ensure_public_address("https://proxy.example.com:8443/v1")


# --- audit -------------------------------------------------------------------------


def test_audit_fields_carry_host_and_a_path_fingerprint_never_the_address() -> None:
    fields = audit_fields("https://proxy.example.com:8443/tok-123/v1")
    assert fields["base_url_host"] == "proxy.example.com:8443"
    fingerprint = fields["base_url_path_fp"]
    assert isinstance(fingerprint, str)
    assert len(fingerprint) == 12
    assert "tok-123" not in str(fields)
    # The fingerprint moves with the path and only with the path.
    assert audit_fields("https://proxy.example.com:8443/tok-456/v1")["base_url_path_fp"] != (
        fingerprint
    )
    assert audit_fields("https://other.example.com/tok-123/v1")["base_url_path_fp"] == fingerprint


def test_audit_fields_for_the_official_address() -> None:
    assert audit_fields(None) == {"base_url_host": None, "base_url_path_fp": None}
