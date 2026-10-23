"""B-206 K-7b — a knowledge base's rerank-score threshold is bound to the
platform rerank model it was set under (``knowledge_base.rerank_threshold_model``,
migration 0171).

Rerank scores are not comparable across models, so a threshold set under one
model is left out of searches another model scores (the retriever decides that
per search, :mod:`orchestrator.tools.knowledge_threshold`). Here: which model a
threshold written through the API is bound to, and what the settings page is
told about it.
"""

from __future__ import annotations

from typing import Any

from control_plane.rerank_vendors import callable_rerank_api
from expert_work.protocol import KnowledgeBase, rerank_model_key, rerank_threshold_paused


async def current_rerank_model(config_service: Any | None) -> str | None:
    """The live platform rerank model as :func:`~expert_work.protocol.rerank_model_key`
    names it, decided as the search routes it
    (:func:`~control_plane.rerank_vendors.callable_rerank_api`): a model served by
    a vendor rerank endpoint, whose rankings carry this name. ``None`` when none
    is configured, or it ranks by a chat model (no scores, so no threshold applies
    under it to pause or to be set under), or the platform cannot call it."""
    if config_service is None:
        return None
    pair: tuple[str, str] | None = await config_service.effective_rerank_config()
    if pair is None:
        return None
    vendor = callable_rerank_api(*pair) in ("dashscope", "cohere_style")
    return rerank_model_key(*pair) if vendor else None


def model_for_threshold(threshold: float | None, current: str | None) -> str | None:
    """The model a threshold is bound to when it is written, or its stored value
    confirmed: the live one; none when there is no threshold. Writing one — the
    same value included — is calibrating it: the settings page sends it only when
    the field was edited, so saving other settings does not rebind it; confirming
    sends no value, so it cannot put back one another admin has changed since."""
    return current if threshold is not None else None


def threshold_binding(base: KnowledgeBase, current: str | None) -> dict[str, Any]:
    """What the settings page shows next to the threshold: the model it was set
    under, the live one, and whether it is left out of searches for that."""
    return {
        "rerank_threshold_model": base.rerank_threshold_model,
        "rerank_threshold_paused": rerank_threshold_paused(base, current),
        "current_rerank_model": current,
    }
