"""Save-time connectivity check for a credential's request address — B-308 §4.5.3.

When an admin saves a provider credential with a custom request address, the
platform makes **one real call** with that key to that address before anything
is stored: a very short chat turn on a cheap model, or — for a provider the
platform only uses for embeddings / rerank — one request to that interface.
At most :data:`CHECK_TIMEOUT_S` seconds. Only "did the vendor answer normally"
counts; the answer's content does not, and an answer cut by the output cap is
still an answer.

The call goes through the same address-checking client every later call to that
address will use, so the save-time check and the call-time check are one and
the same. Its tokens are recorded like any other platform call
(``platform_overhead``, agent name :data:`CHECK_AGENT_NAME`).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal
from uuid import UUID

import httpx
from langchain_core.messages import HumanMessage

from control_plane.manifest_model_names import iter_manifest_models
from control_plane.platform_provider_uses import PlatformProviderUse
from control_plane.tenant_scope import bypass_rls_session
from expert_work.common.url_validation import PublicEndpointError
from expert_work.persistence.token_usage_store import (
    PLATFORM_OVERHEAD_USAGE_KIND,
    TokenUsageRecord,
    TokenUsageStore,
)
from expert_work.protocol import (
    AgentSpecRecord,
    ModelRateCardRecord,
    ModelSpec,
    Provider,
    catalog_entry,
)
from expert_work.protocol.model_catalog import ModelEntry, models_for_provider
from expert_work.runtime.middleware import (
    LLMClientError,
    LLMError,
    LLMNetworkError,
    LLMRateLimitError,
    LLMUnauthorizedError,
)
from orchestrator.agent_factory import build_provider
from orchestrator.errors import AgentFactoryError
from orchestrator.llm.embedder import (
    OpenAICompatibleEmbedder,
    embedding_client_for,
    embedding_usage_sink,
)
from orchestrator.llm.rerank import dashscope_rerank_client
from orchestrator.llm.rerank_cohere import RERANK_SHAPE_BY_PROVIDER, cohere_style_client
from orchestrator.llm.rerank_guard import RerankCallError

logger = logging.getLogger("expert_work.control_plane.credential_check")

#: The whole check — connect, send, answer — may take this long.
CHECK_TIMEOUT_S = 15.0
#: ``agent_name`` on the usage row of a check.
CHECK_AGENT_NAME = "credential_check"
#: The one short line sent to a chat model.
CHECK_PROMPT = "Reply with the single word: ok"
#: Output cap for the chat check — the answer is a word.
_CHECK_MAX_TOKENS = 16
_AUTH_STATUSES = frozenset({401, 403})
_RATE_LIMITED = 429
_REQUEST_TIMEOUT = 408
_STATUS_IN_DETAIL = re.compile(r"^\S+ (\d{3}):")
#: The fixed-key embedder ignores the tenant it is called for.
_NO_TENANT = UUID(int=0)


def _log_safe(value: str) -> str:
    """``value`` without line breaks. The provider comes from the request path
    (already matched against the catalog, but CodeQL py/log-injection cannot see
    that); the ``.replace`` chain is the barrier its taint model recognises."""
    return value.replace("\r", "").replace("\n", "")


CheckInterface = Literal["chat", "embeddings", "rerank"]
CheckFailure = Literal["unreachable", "auth_rejected", "timeout", "vendor_error"]
#: Platform features that call a provider's embeddings / rerank interface.
_NON_CHAT_FEATURES: Mapping[str, CheckInterface] = {
    "embedding": "embeddings",
    "rerank": "rerank",
}


@dataclass(frozen=True)
class CheckPlan:
    """What the check will call: which interface, with which model."""

    interface: CheckInterface
    model: str


@dataclass(frozen=True)
class CheckResult:
    """How the check went.

    ``blocked`` — the connection layer refused the address (not public);
    ``invalid`` — the HTTP client could not even form a request to it. Neither
    is a failed check an admin may override; the save is refused outright.
    """

    passed: bool
    reason: CheckFailure | None = None
    http_status: int | None = None
    blocked: bool = False
    invalid: bool = False
    #: Which interface the failing call was for: ``chat`` / ``embedding`` /
    #: ``rerank`` (``None`` when the check passed).
    interface: str | None = None


def _is_chat_model(entry: ModelEntry | None) -> bool:
    """A model a chat check can call: not an embeddings / rerank model and not a
    recognition-only one. A name the catalog does not know (a deployment
    endpoint id, a dedicated deployment's model) is taken as a chat model."""
    if entry is None:
        return True
    return not (entry.embeddings or entry.rerank or entry.ocr_caps is not None)


def _most_used_agent_model(provider: str, specs: Iterable[AgentSpecRecord]) -> str | None:
    counts: Counter[str] = Counter()
    for record in specs:
        for _path, model in iter_manifest_models(record.spec):
            if model.provider == provider and _is_chat_model(catalog_entry(provider, model.name)):
                counts[model.name] += 1
    if not counts:
        return None
    # Most used; ties broken by name so the choice is stable.
    return min(counts, key=lambda name: (-counts[name], name))


def _cheapest_catalog_model(provider: str, prices: Sequence[ModelRateCardRecord]) -> str | None:
    # ``models_for_provider`` already leaves deprecated entries out.
    candidates = [entry for entry in models_for_provider(provider) if _is_chat_model(entry)]
    if not candidates:
        return None
    price = {
        row.model: row.input_per_mtok_micros + row.output_per_mtok_micros
        for row in prices
        if row.provider == provider
    }
    priced = [entry for entry in candidates if entry.name in price]
    if priced:
        return min(priced, key=lambda entry: (price[entry.name], entry.name)).name
    return candidates[0].name


class NoCheckModelError(LookupError):
    """The provider has no model the connectivity check could call."""


def plan_check(
    provider: str,
    *,
    specs: Sequence[AgentSpecRecord],
    used_by_agents: int,
    platform_uses: Sequence[PlatformProviderUse],
    prices: Sequence[ModelRateCardRecord],
) -> list[CheckPlan]:
    """Choose what ``provider``'s check calls: one chat call, or — when no agent
    references the provider and the platform itself only uses it for embeddings
    / rerank — one call per such interface, with the model the platform has
    configured for it (embeddings first). Both are called when both are used:
    a vendor's rerank address is derived from the request address differently
    from its embeddings address, so one passing says nothing about the other.

    The chat model is the first of: the chat model agents that reference this
    provider use most; a chat model a platform feature is configured with; the
    catalog's cheapest enabled chat model by the rate card (input + output
    price), or the first one when none is priced. "Already in use" comes first
    because a deployment endpoint or a dedicated deployment may not serve the
    catalog's cheapest model.
    """
    active = [use for use in platform_uses if use.enabled and use.model]
    non_chat = [use for use in active if use.feature in _NON_CHAT_FEATURES]
    if used_by_agents == 0 and active and len(non_chat) == len(active):
        ordered = sorted(non_chat, key=lambda use: use.feature != "embedding")
        return [
            CheckPlan(interface=_NON_CHAT_FEATURES[use.feature], model=use.model) for use in ordered
        ]
    in_use = _most_used_agent_model(provider, specs)
    if in_use is not None:
        return [CheckPlan(interface="chat", model=in_use)]
    for use in active:
        if use.feature not in _NON_CHAT_FEATURES and _is_chat_model(
            catalog_entry(provider, use.model)
        ):
            return [CheckPlan(interface="chat", model=use.model)]
    cheapest = _cheapest_catalog_model(provider, prices)
    if cheapest is None:
        msg = f"provider {provider!r} has no chat model to check a request address with"
        raise NoCheckModelError(msg)
    return [CheckPlan(interface="chat", model=cheapest)]


def _refused_by_connection_layer(exc: BaseException) -> PublicEndpointError | None:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, PublicEndpointError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def _has_cause(exc: BaseException, kind: type[BaseException]) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, kind):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _transport_failure(exc: BaseException) -> CheckResult:
    refusal = _refused_by_connection_layer(exc)
    if refusal is not None and refusal.reason == "blocked":
        return CheckResult(passed=False, blocked=True)
    if _has_cause(exc, httpx.TimeoutException) or _has_cause(exc, TimeoutError):
        return CheckResult(passed=False, reason="timeout")
    return CheckResult(passed=False, reason="unreachable")


def _status_failure(status: int | None) -> CheckResult:
    if status in _AUTH_STATUSES:
        return CheckResult(passed=False, reason="auth_rejected", http_status=status)
    return CheckResult(passed=False, reason="vendor_error", http_status=status)


def _llm_failure(exc: LLMError) -> CheckResult:
    if isinstance(exc, LLMUnauthorizedError):
        return _status_failure(401)
    match = _STATUS_IN_DETAIL.match(str(exc))
    status = int(match.group(1)) if match else None
    if isinstance(exc, LLMNetworkError) and status != _REQUEST_TIMEOUT:
        return _transport_failure(exc)
    if isinstance(exc, LLMClientError) and exc.status is not None:
        status = exc.status
    if isinstance(exc, LLMRateLimitError):
        status = _RATE_LIMITED
    return _status_failure(status)


@dataclass(frozen=True)
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0


#: A 2xx whose body is not what the interface answers with.
_NOT_AN_ANSWER = CheckResult(passed=False, reason="vendor_error")


async def _check_chat(
    provider: str, model: str, *, api_key: str, base_url: str
) -> tuple[CheckResult, _Usage]:
    entry = catalog_entry(provider, model)
    thinking_off = False if entry is not None and entry.thinking is not None else None
    spec = ModelSpec(
        provider=provider,  # type: ignore[arg-type]
        name=model,
        max_tokens=_CHECK_MAX_TOKENS,
        thinking_enabled=thinking_off,
    )
    try:
        client = build_provider(spec, api_key, base_url=base_url, timeout_s=CHECK_TIMEOUT_S)
    except AgentFactoryError:
        # The model cannot take that small a cap (its thinking budget does not
        # fit): leave the cap to the vendor's default.
        client = build_provider(
            spec.model_copy(update={"max_tokens": None}),
            api_key,
            base_url=base_url,
            timeout_s=CHECK_TIMEOUT_S,
        )
    try:
        reply = await client.complete(messages=[HumanMessage(content=CHECK_PROMPT)], tools=[])
    except LLMError as exc:
        return _llm_failure(exc), _Usage()
    metadata = reply.response_metadata
    answered = bool(
        reply.content
        or reply.tool_calls
        or metadata.get("finish_reason")
        or metadata.get("stop_reason")
    )
    if not answered:
        # A 2xx that is not a completion (a login page, some other service's
        # JSON): the address does not serve this API.
        return _NOT_AN_ANSWER, _Usage()
    usage: Mapping[str, Any] = reply.usage_metadata or {}
    return CheckResult(passed=True), _Usage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
    )


async def _check_embeddings(
    provider: str, model: str, *, api_key: str, base_url: str
) -> tuple[CheckResult, _Usage]:
    reported: list[Mapping[str, Any]] = []
    embedder = OpenAICompatibleEmbedder(
        client=embedding_client_for(provider, api_key=api_key, base_url=base_url), model=model
    )
    try:
        # The vendor's usage arrives through the sink every embeddings call reports to.
        with embedding_usage_sink(lambda _model, usage: reported.append(usage)):
            vectors = await embedder.embed(["ok"], tenant_id=_NO_TENANT)
    except httpx.HTTPStatusError as exc:
        return _status_failure(exc.response.status_code), _Usage()
    except httpx.HTTPError as exc:
        return _transport_failure(exc), _Usage()
    if len(vectors) != 1 or not vectors[0]:
        return _NOT_AN_ANSWER, _Usage()
    tokens = sum(
        int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0) for usage in reported
    )
    return CheckResult(passed=True), _Usage(input_tokens=tokens)


async def _check_rerank(
    provider: str, model: str, *, api_key: str, base_url: str
) -> tuple[CheckResult, _Usage]:
    if provider in RERANK_SHAPE_BY_PROVIDER:
        client: Any = cohere_style_client(provider, api_key=api_key, base_url=base_url)
    else:
        client = dashscope_rerank_client(api_key=api_key, base_url=base_url)
    try:
        body = await client.rerank(model=model, query="ok", documents=["ok", "no"], top_n=2)
    except RerankCallError as exc:
        if exc.status is not None:
            return _status_failure(exc.status), _Usage()
        return _transport_failure(exc), _Usage()
    except httpx.HTTPError as exc:
        return _transport_failure(exc), _Usage()
    # Either vendor shape: ``output.results`` (native) or ``results`` (Cohere-style).
    output = body.get("output")
    rows = output.get("results") if isinstance(output, Mapping) else body.get("results")
    if not isinstance(rows, list) or not rows:
        return _NOT_AN_ANSWER, _Usage()
    usage = body.get("usage")
    tokens = int(usage.get("total_tokens") or 0) if isinstance(usage, Mapping) else 0
    return CheckResult(passed=True), _Usage(input_tokens=tokens)


#: What a failed check calls the interface it was on (API detail and audit).
_INTERFACE_NAMES: Mapping[CheckInterface, str] = {
    "chat": "chat",
    "embeddings": "embedding",
    "rerank": "rerank",
}

_CHECKS = {"chat": _check_chat, "embeddings": _check_embeddings, "rerank": _check_rerank}


async def run_check(
    plans: Sequence[CheckPlan],
    *,
    provider: Provider,
    base_url: str,
    api_key: str,
    usage_store: TokenUsageStore | None,
    usage_tenant_id: UUID,
) -> CheckResult:
    """Make the real call(s) and say how it went: every plan must pass, the
    first failure is the result, and all of them share the one
    :data:`CHECK_TIMEOUT_S` budget. Never raises for a vendor or network
    failure — those are the result — nor for anything unexpected the client
    does with an odd answer (that is a ``vendor_error``, not a server error).
    Each passed call's tokens are recorded under ``usage_tenant_id``."""
    running: str | None = None
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            for plan in plans:
                running = _INTERFACE_NAMES[plan.interface]
                result, usage = await _run_one(plan, provider, api_key=api_key, base_url=base_url)
                if not result.passed:
                    return replace(result, interface=running)
                await _record_usage(
                    usage_store,
                    tenant_id=usage_tenant_id,
                    provider=provider,
                    model=plan.model,
                    usage=usage,
                )
    except TimeoutError:
        return CheckResult(passed=False, reason="timeout", interface=running)
    return CheckResult(passed=True)


async def _run_one(
    plan: CheckPlan, provider: str, *, api_key: str, base_url: str
) -> tuple[CheckResult, _Usage]:
    try:
        return await _CHECKS[plan.interface](
            provider, plan.model, api_key=api_key, base_url=base_url
        )
    except httpx.InvalidURL:
        return CheckResult(passed=False, invalid=True), _Usage()
    except TimeoutError:
        raise  # the shared budget ran out: ``run_check`` reports it
    except Exception as exc:
        # A redirect, an HTML page, JSON of another shape: whatever the client
        # tripped over, the address did not answer as this API does.
        logger.warning(
            "credential_check.unexpected_answer provider=%s interface=%s error=%s",
            _log_safe(provider),
            plan.interface,
            type(exc).__name__,
        )
        return _NOT_AN_ANSWER, _Usage()


async def _record_usage(
    store: TokenUsageStore | None, *, tenant_id: UUID, provider: str, model: str, usage: _Usage
) -> None:
    if store is None or (usage.input_tokens <= 0 and usage.output_tokens <= 0):
        return
    record = TokenUsageRecord(
        tenant_id=tenant_id,
        agent_name=CHECK_AGENT_NAME,
        agent_version="",
        model=model,
        provider=provider,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        usage_kind=PLATFORM_OVERHEAD_USAGE_KIND,
    )
    try:
        async with bypass_rls_session():
            await store.insert(record)
    except Exception:
        # The vendor billed it either way; the save itself must not fail on this.
        logger.warning(
            "credential_check.usage_write_failed provider=%s model=%s input_tokens=%d "
            "output_tokens=%d",
            _log_safe(provider),
            model,
            usage.input_tokens,
            usage.output_tokens,
            exc_info=True,
        )
