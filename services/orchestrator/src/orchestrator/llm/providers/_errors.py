"""Shared HTTP-status → :class:`LLMError` classification — Stream Y-MK.

OpenAI- and Anthropic-family adapters share one classifier so the
*account/key-level* failure axis is consistent across vendors. The router
(E.11 + Y-MK) recovers from key-level failures by trying a sibling key of the
same provider before falling through to the next provider:

- **402 / quota / billing** → :class:`LLMKeyUnavailableError`. The balance or
  quota for *this* key/account is exhausted; a sibling key on a different
  billing account can serve. Not retried (backoff can't un-exhaust an account).
- **plain 429** → :class:`LLMRateLimitError`. Transient rate-limit, retryable.
- **401** → :class:`LLMUnauthorizedError`. OAuth providers refresh + retry; a
  non-OAuth revoked key is treated as key-level by the router.
- **408 / 409** → retryable :class:`LLMNetworkError` / :class:`LLMServerError`
  (B-297, matching the official SDKs).
- **other 4xx** → :class:`LLMClientError`. Malformed request — fail-fast, the
  router does not fall back (a bad request fails identically everywhere).
  B-328: a body saying the input is too long → :class:`LLMContextTooLongError`
  (same no-fallback semantics; ``agent_node`` shrinks once and retries).
- **5xx** → :class:`LLMServerError`. Vendor-side, retryable / next provider.

Vendors overload HTTP 429 for *both* rate-limits and quota-exhaustion, and 403
for both permission denials and billing lockouts, so the body is inspected for
billing/quota markers to split them. Markers are matched case-insensitively as
substrings — conservative but covering the common vendor phrasings (OpenAI
``insufficient_quota`` / ``exceeded your current quota`` /
``billing_hard_limit_reached``, DeepSeek ``Insufficient Balance``, Anthropic
``credit balance is too low``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from expert_work.runtime.middleware import (
    LLMClientError,
    LLMContextTooLongError,
    LLMError,
    LLMKeyUnavailableError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMServerError,
    LLMUnauthorizedError,
)

# Substrings (lowercased) that mark a billing/quota/balance exhaustion rather
# than a transient rate-limit or a permission denial.
_KEY_DEAD_MARKERS: tuple[str, ...] = (
    "insufficient_quota",
    "insufficient balance",
    "insufficient_balance",
    "exceeded your current quota",
    "billing_hard_limit_reached",
    "billing hard limit",
    "credit balance is too low",
    "quota",
    "billing",
    "payment required",
    "deactivated",
    "account is not active",
)


def _looks_account_dead(body: str) -> bool:
    low = body.lower()
    return any(marker in low for marker in _KEY_DEAD_MARKERS)


# B-328 PR R —— 「输入太长」要**同时**有一个说输入的词和一个说长度的词(不认厂商名)。不收
# ``messages``:OpenAI / Anthropic 的 400 正文常以 ``messages.0.content.1.image…`` 这种 JSON
# 路径开头,收了它「图片超过 5 MB」也会命中。核过的厂商原话见 ``test_b328_vendor_too_long``。
_INPUT_WORDS: tuple[str, ...] = ("context", "prompt", "input", "上下文", "输入", "提示")
_LENGTH_WORDS: tuple[str, ...] = (
    "length",
    "too long",
    "tokens",
    "maximum",
    "exceed",
    "长度",
    "超过",
    "超出",
)


def looks_context_too_long(body: str) -> bool:
    """B-328 PR R —— 厂商这句话是不是在说「输入太长 / 超过上下文」。

    只在 4xx 的通用分支(401 / 402 / 408 / 409 / 429 / 403 都已先分走,限流到不了这里)和
    两个流式适配器的错误事件默认分支之前用。"""
    low = body.lower()
    return any(word in low for word in _INPUT_WORDS) and any(word in low for word in _LENGTH_WORDS)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header read for ``httpx.Headers`` and plain dicts alike."""
    value = headers.get(name)
    if value is not None:
        return value
    for key, raw in headers.items():
        if key.lower() == name:
            return raw
    return None


def _seconds(raw: str | None, scale: float) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw) * scale
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def retry_after_from_headers(headers: Mapping[str, str] | None) -> float | None:
    """B-297 — the vendor's requested wait in seconds, or ``None``.

    ``retry-after-ms`` (OpenAI-family) wins over ``retry-after``; only the
    delta-seconds form of ``retry-after`` is read — an HTTP-date is rare from
    LLM vendors and the backoff applies instead (same stance as the rerank
    guard's parser).
    """
    if not headers:
        return None
    ms = _seconds(_header(headers, "retry-after-ms"), 0.001)
    if ms is not None:
        return ms
    return _seconds(_header(headers, "retry-after"), 1.0)


def classify_http_error(
    provider: str,
    status: int,
    body: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> LLMError:
    """Map an upstream HTTP status + response body to an :class:`LLMError`.

    ``body`` is the truncated response text; pass an empty string when no body
    is available. ``headers`` (B-297, optional) are the response headers; a
    ``Retry-After`` there lands on the error's ``retry_after_s``. The returned
    exception is ready to ``raise``.

    B-297 — 408 (request timeout) is a retryable :class:`LLMNetworkError` and
    409 (conflict / lock contention) a retryable :class:`LLMServerError`, as
    the official OpenAI and Anthropic SDKs retry both.
    """
    detail = f"{provider} {status}: {body}"
    error = _classify(status, body, detail)
    error.retry_after_s = retry_after_from_headers(headers)
    return error


def _classify(status: int, body: str, detail: str) -> LLMError:
    if status == 401:
        return LLMUnauthorizedError(detail)
    if status == 402:
        return LLMKeyUnavailableError(detail)
    if status == 408:
        return LLMNetworkError(detail)
    if status == 409:
        return LLMServerError(detail)
    if status == 429:
        if _looks_account_dead(body):
            return LLMKeyUnavailableError(detail)
        return LLMRateLimitError(detail)
    if status == 403 and _looks_account_dead(body):
        return LLMKeyUnavailableError(detail)
    if 400 <= status < 500:
        client_error = (
            LLMContextTooLongError(detail)
            if looks_context_too_long(body)
            else LLMClientError(detail)
        )
        client_error.status = status
        return client_error
    return LLMServerError(detail)
