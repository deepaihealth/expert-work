"""Unit tests for Stream Y-MK HTTP-status → LLMError classification.

The shared classifier distinguishes *account/key-level* failures (balance /
quota / revoked key) — which the router recovers from by trying a sibling key —
from plain rate-limits, malformed requests, and server faults.
"""

from __future__ import annotations

import pytest

from expert_work.runtime.middleware import (
    LLMClientError,
    LLMKeyUnavailableError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
    LLMUnauthorizedError,
)
from orchestrator.llm.providers._errors import classify_http_error


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        # MK-9 — account/key dead → LLMKeyUnavailableError (try sibling key)
        (402, "Insufficient Balance", LLMKeyUnavailableError),  # deepseek 欠费
        (429, '{"error":{"code":"insufficient_quota"}}', LLMKeyUnavailableError),  # openai 欠费
        (429, "You exceeded your current quota, please check billing", LLMKeyUnavailableError),
        (403, "Your account has been deactivated due to billing", LLMKeyUnavailableError),
        (403, "billing_hard_limit_reached", LLMKeyUnavailableError),
        # MK-10 — plain transient / request / server → unchanged classes
        (429, '{"error":{"type":"rate_limit_exceeded"}}', LLMRateLimitError),  # 纯限流
        (429, "Rate limit reached for requests", LLMRateLimitError),
        (400, "invalid 'tools[0].function' schema", LLMClientError),  # malformed
        (403, "model not allowed for this org", LLMClientError),  # non-billing 403
        (404, "model not found", LLMClientError),
        (401, "invalid api key", LLMUnauthorizedError),  # revoked key (router → key-level)
        (500, "internal server error", LLMServerError),
        (503, "overloaded", LLMServerError),
    ],
)
def test_classify_http_error(status: int, body: str, expected: type[Exception]) -> None:
    err = classify_http_error("openai", status, body)
    assert isinstance(err, expected)
    # The originating provider + status are preserved in the message for logs.
    assert "openai" in str(err)


def test_classify_is_case_insensitive_on_quota_markers() -> None:
    assert isinstance(
        classify_http_error("deepseek", 402, "INSUFFICIENT BALANCE"),
        LLMKeyUnavailableError,
    )


# ---------------------------------------------------------------------------
# B-297 — 408 / 409 retryable (official SDKs retry both); Retry-After carried
# ---------------------------------------------------------------------------


def test_408_is_retryable_network_class() -> None:
    err = classify_http_error("openai", 408, "request timeout")
    assert isinstance(err, LLMNetworkError)


def test_409_is_retryable_server_class() -> None:
    err = classify_http_error("openai", 409, "conflict")
    assert isinstance(err, LLMServerError)


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_other_4xx_unchanged_not_retryable(status: int) -> None:
    err = classify_http_error("openai", status, "nope")
    assert isinstance(err, LLMClientError)
    assert err.status == status


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after": "7"}, 7.0),
        ({"Retry-After": "2.5"}, 2.5),
        ({"retry-after-ms": "1500"}, 1.5),
        ({"retry-after-ms": "1500", "retry-after": "9"}, 1.5),
        ({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}, None),
        ({"retry-after": "-3"}, None),
        ({"retry-after": "nan"}, None),
        ({}, None),
    ],
)
def test_retry_after_carried_on_rate_limit(headers: dict[str, str], expected: float | None) -> None:
    err = classify_http_error("openai", 429, "slow down", headers=headers)
    assert isinstance(err, LLMRateLimitError)
    assert err.retry_after_s == expected


def test_retry_after_carried_on_server_error() -> None:
    err = classify_http_error("anthropic", 503, "overloaded", headers={"retry-after": "12"})
    assert isinstance(err, LLMServerError)
    assert err.retry_after_s == 12.0


def test_retry_after_works_with_httpx_headers() -> None:
    import httpx

    headers = httpx.Headers({"Retry-After": "4"})
    assert classify_http_error("openai", 429, "x", headers=headers).retry_after_s == 4.0


def test_retry_after_defaults_none_without_headers() -> None:
    assert classify_http_error("openai", 503, "x").retry_after_s is None
