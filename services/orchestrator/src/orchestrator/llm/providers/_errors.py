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
- **other 4xx** → :class:`LLMClientError`. Malformed request — fail-fast, the
  router does not fall back (a bad request fails identically everywhere).
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

from expert_work.runtime.middleware import (
    LLMClientError,
    LLMError,
    LLMKeyUnavailableError,
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


def classify_http_error(provider: str, status: int, body: str) -> LLMError:
    """Map an upstream HTTP status + response body to an :class:`LLMError`.

    ``body`` is the truncated response text; pass an empty string when no body
    is available. The returned exception is ready to ``raise``.
    """
    detail = f"{provider} {status}: {body}"
    if status == 401:
        return LLMUnauthorizedError(detail)
    if status == 402:
        return LLMKeyUnavailableError(detail)
    if status == 429:
        if _looks_account_dead(body):
            return LLMKeyUnavailableError(detail)
        return LLMRateLimitError(detail)
    if status == 403 and _looks_account_dead(body):
        return LLMKeyUnavailableError(detail)
    if 400 <= status < 500:
        client_error = LLMClientError(detail)
        client_error.status = status
        return client_error
    return LLMServerError(detail)
