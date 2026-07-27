"""Stream O — Sprint #7 aux model wire-up via :class:`LLMRouterAuxModelAdapter`.

Replaces ``_NullConsolidatorAuxModel`` from Sprint #7 PR B (which
returned hard-coded JSON for every prompt and produced zero
consolidations / purges). The adapter:

1. Resolves the platform / tenant secret_ref through
   :class:`CredentialsResolver` (Stream O Mini-ADR O-3).
2. Builds a single-provider :class:`LLMRouter` via the orchestrator's
   :func:`build_llm_router` factory (reusing E.11 fallback + E.4
   breaker + L.L3 deadline plumbing for free).
3. Wraps the consolidator's plain-text prompt in a single
   :class:`HumanMessage` and unpacks the :class:`AIMessage` response
   text + token usage back into :class:`ConsolidatorLLMReply`.

Per Mini-ADR O-3 there is no fallback to a platform key when the
tenant's mode is ``"tenant"`` and a credential is missing — the
adapter raises :class:`CredentialsResolverError`, which the
consolidator's outer ``try`` translates into a per-cluster skip + an
``CREDENTIALS_RESOLVE_FAILED`` audit row (Mini-ADR O-8).

Per Sprint #7 the consolidator is tolerant of LLM errors (worker
catches and skips to the next cluster / tick), so the adapter does
not retry beyond what :class:`LLMRouter`'s built-in chain already
covers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from langchain_core.messages import HumanMessage

from control_plane.credential_value_cache import CachingSecretStore, CredentialValueCache
from control_plane.memory_consolidator import (
    ConsolidatorAuxModel,
    ConsolidatorLLMReply,
)
from expert_work.common.credentials import (
    CredentialsResolver,
    CredentialsResolverError,
)
from expert_work.common.uplift_metrics import record_credentials_resolve
from expert_work.protocol import ModelSpec, Provider, StructuredOutputSpec

if TYPE_CHECKING:
    from expert_work.runtime.secret_store import SecretStore

logger = logging.getLogger("expert_work.control_plane.credentials_aux_adapter")


class LLMRouterAuxModelAdapter:
    """Stream O — production :class:`ConsolidatorAuxModel` implementation.

    Constructs a :class:`LLMRouter` per call (the consolidator runs
    every 4 h so per-call construction cost is negligible) with the
    correct tenant credential resolved through
    :class:`CredentialsResolver`. The router's own fallback chain is
    not used because the consolidator works on a single default model
    only — multi-provider fallback would muddy the credential mode
    semantics (a tenant-mode tenant should not silently fall back to
    a platform-credentialed provider).

    The adapter is a singleton (one instance per worker) and routes per
    call to the right tenant credentials via the ``tenant_id`` keyword
    argument supplied by the consolidator.
    """

    def __init__(
        self,
        *,
        resolver: CredentialsResolver,
        secret_store: SecretStore,
        default_provider: Provider,
        default_model: str,
        secret_cache: CredentialValueCache | None = None,
    ) -> None:
        self._resolver = resolver
        self._secret_store = secret_store
        self._default_provider = default_provider
        self._default_model = default_model
        # 二期 PR2 T3 — the adapter has no direct ``secret_store.get``; its
        # vault read happens inside ``build_llm_router``. ``None`` (tests /
        # not yet wired) keeps the direct-read behaviour.
        self._secret_cache = secret_cache

    async def __call__(
        self,
        *,
        prompt: str,
        model: str | None,
        tenant_id: UUID,
        output_schema: StructuredOutputSpec | None = None,
    ) -> ConsolidatorLLMReply:
        # Lazy import — ``orchestrator`` depends on control-plane in
        # some test paths via subagent runtime, and importing
        # ``build_llm_router`` at module import would risk an import
        # cycle. Done at call time keeps the dependency graph honest.
        from orchestrator import build_llm_router

        provider = self._default_provider
        model_name = model or self._default_model
        try:
            secret_ref = await self._resolver.resolve_provider(
                tenant_id=tenant_id, provider=provider
            )
            record_credentials_resolve(
                mode="platform",  # mode logged by resolver decision
                role="provider",
                key=provider,
                result="ok",
            )
        except CredentialsResolverError as exc:
            record_credentials_resolve(
                mode=exc.mode,
                role=exc.kind,
                key=exc.key,
                result="missing_cred",
            )
            raise
        spec = ModelSpec(
            provider=provider,
            name=model_name,
            api_key_ref=secret_ref,
            # Consolidator does not declare its own fallback chain; the
            # default model is what the manifest opted into.
            fallback=[],
        )
        # 二期 PR2 T3 — hand the router a tenant-scoped caching store so its
        # internal vault read (agent_factory.py:1956) becomes a cache hit.
        store: SecretStore = self._secret_store
        if self._secret_cache is not None:
            store = CachingSecretStore(
                inner=self._secret_store, cache=self._secret_cache, tenant_id=tenant_id
            )
        router = await build_llm_router(spec, secret_store=store)
        message = HumanMessage(content=prompt)
        # RT-1 — ``output_schema`` threads straight through to the router's
        # validation loop; ``None`` keeps the call wire-identical to the
        # pre-RT-1 behaviour. A validation-exhausted call raises
        # ``LLMOutputValidationError``, which the consolidator maps onto
        # its existing malformed-reply degrade (RT-ADR-3).
        response = await router(messages=[message], tools=[], output_schema=output_schema)
        text = _coerce_text(response.content)
        parsed_raw = response.additional_kwargs.get("parsed")
        usage: dict[str, int] = dict(response.usage_metadata or {})  # type: ignore[arg-type]
        return ConsolidatorLLMReply(
            text=text,
            model=model_name,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            parsed=parsed_raw if isinstance(parsed_raw, dict) else None,
        )


def _coerce_text(content: object) -> str:
    """LangChain AIMessage.content can be ``str`` or
    ``list[dict | str]``. The consolidator only emits plain-text
    prompts so we expect ``str``; degrade gracefully for adapters
    that return list-of-blocks by concatenating the text blocks.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "".join(chunks)
    return str(content)


def make_llm_router_aux_model(
    *,
    resolver: CredentialsResolver,
    secret_store: SecretStore,
    default_provider: Provider,
    default_model: str,
    secret_cache: CredentialValueCache | None = None,
) -> ConsolidatorAuxModel:
    """Factory mirroring :func:`make_null_consolidator_aux_model` so the
    app.py wire-up can swap one for the other with no code change at
    the call site."""
    return LLMRouterAuxModelAdapter(
        resolver=resolver,
        secret_store=secret_store,
        default_provider=default_provider,
        default_model=default_model,
        secret_cache=secret_cache,
    )
