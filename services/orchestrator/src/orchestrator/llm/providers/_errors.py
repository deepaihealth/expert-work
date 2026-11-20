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
import re
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


# B-328 PR R —— 「输入太长」按**短语**认,不按两个散落的词(审查 R1:``budget_tokens: Input
# should be …``、严格 schema 的 ``In context=(…), 'maximum' is not permitted``、``input image
# exceeds the maximum size`` 用「输入词 + 长度词」都会误中)。不收 ``messages``:OpenAI / Anthropic
# 的 400 正文常以 ``messages.0.content.1.image…`` 这种路径开头。核过出处的厂商原话(正反例)见
# ``test_b328_vendor_too_long``。
_TOO_LONG_RE = re.compile(
    # ``context (length|window)`` 故意宽:几家都这么说(OpenAI「maximum context length」、代理转出的
    # 「exceeds the context window」),误中的形状靠下面的就近排除词挡。
    r"context (?:length|window)"
    r"|maximum context"
    r"|prompt (?:is )?too long"
    r"|input.{0,30}(?:too long|length)"
    r"|input token count.{0,40}exceeds"
    r"|exceeded model token limit"
    r"|上下文.{0,10}(?:长度|超过|超出|超长)"
    r"|输入.{0,10}(?:长度|超过|超出|超长)"
)
#: 短语**附近**(前后 60 字内)说的是限流、额度、输出上限、思考预算或图片 —— 缩输入救不了,
#: 不认。只看附近:正文别处(请求 id、括号里的补充说明)出现这些词不算。``max_tokens`` 不在
#: 这里:Anthropic「input length and ``max_tokens`` exceed context limit: N + M > W」是真的
#: 超窗口,按输入 / 输出谁占大头处理(``graph_builder.vendor_too_long``)。
_NOT_TOO_LONG_RE = re.compile(
    r"rate limit|rate_limit|per minute|quota|budget_tokens|completion tokens|image"
)
_EXCLUSION_REACH = 60


def looks_context_too_long(body: str) -> bool:
    """B-328 PR R —— 厂商这句话是不是在说「输入太长 / 超过上下文」。

    只在 4xx 的通用分支(401 / 402 / 408 / 409 / 429 / 403 都已先分走)和两个流式适配器的
    错误事件默认分支之前用(流式那里没有状态码,网关的限流事件也会提到 input tokens)。
    有一处短语附近没有排除词就算。"""
    low = body.lower()
    for match in _TOO_LONG_RE.finditer(low):
        around = low[max(0, match.start() - _EXCLUSION_REACH) : match.end() + _EXCLUSION_REACH]
        if _NOT_TOO_LONG_RE.search(around) is None:
            return True
    return False


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
