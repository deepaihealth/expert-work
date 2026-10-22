"""The chat-model reranker — for vendors without a rerank endpoint.

Moved here from :mod:`orchestrator.tools.knowledge` (which re-exports it) when
B-206 K-7a gave it :meth:`LLMReranker.ranked`: a ranking that says when the
model's reply could not be used, instead of passing the input order off as
the model's.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage

from expert_work.common.observability import ExpertWorkComponent, expert_work_span
from orchestrator.tools.knowledge_rerank import RerankRanking

if TYPE_CHECKING:
    # Imported under TYPE_CHECKING only — a runtime import of
    # ``orchestrator.llm`` here would cycle (llm → tools.registry → tools).
    from orchestrator.llm import LLMCaller

logger = logging.getLogger(__name__)

#: Each candidate is truncated to this many characters in the rerank
#: prompt — enough to judge relevance without an enormous prompt.
_RERANK_DOC_CHARS = 600


@dataclass(frozen=True)
class LLMReranker:
    """LLM-backed :class:`~orchestrator.tools.knowledge_rerank.Reranker` —
    reuses an :class:`LLMCaller`.

    A cheap, dependency-free reranker: the LLM is asked to rank the
    candidates and reply with their numbers. A response it cannot parse
    falls back to the input order, so a flaky rerank never breaks search.
    """

    llm_caller: LLMCaller

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        ranked = await self.ranked(
            query=query, documents=documents, top_k=top_k, tenant_id=tenant_id
        )
        return [index for index, _score in ranked]

    async def ranked(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> RerankRanking:
        """:meth:`rerank` as an unscored ranking; when the model failed or its
        reply could not be parsed, the input order marked ``model_error``
        (B-206 K-7a)."""
        del tenant_id  # fixed-key reranker — credential baked into llm_caller
        if not documents:
            return RerankRanking()
        listing = "\n\n".join(
            f"[{i + 1}] {doc[:_RERANK_DOC_CHARS]}" for i, doc in enumerate(documents)
        )
        prompt = [
            SystemMessage(
                content=(
                    "You rank documents by relevance to a query. Reply with "
                    "ONLY a JSON array of the document numbers, most relevant "
                    "first, e.g. [3, 1, 5]. Include only genuinely relevant "
                    "documents."
                )
            ),
            HumanMessage(content=f"Query: {query}\n\nDocuments:\n{listing}"),
        ]
        try:
            with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "rerank"):
                response = await self.llm_caller(messages=prompt, tools=[])
            order = _parse_rerank_order(_message_text(response), len(documents))
        except Exception:
            logger.warning("knowledge.rerank_failed — using fused order", exc_info=True)
            order = []
        if not order:
            unranked = ((index, None) for index in range(min(len(documents), top_k)))
            return RerankRanking(unranked, outcome="model_error")
        return RerankRanking((index, None) for index in order[:top_k])


def _parse_rerank_order(text: str, count: int) -> list[int]:
    """Parse an LLM rerank reply into 0-based, in-range, de-duplicated
    indices. An unparseable reply yields ``[]`` (caller falls back)."""
    numbers: list[int] = []
    match = re.search(r"\[[^\]]*\]", text)
    if match is not None:
        try:
            parsed = json.loads(match.group())
            numbers = [int(n) for n in parsed if isinstance(n, int | float)]
        except (ValueError, TypeError):
            numbers = []
    if not numbers:
        numbers = [int(n) for n in re.findall(r"\d+", text)]
    seen: set[int] = set()
    order: list[int] = []
    for one_based in numbers:
        index = one_based - 1
        if 0 <= index < count and index not in seen:
            seen.add(index)
            order.append(index)
    return order


def _message_text(message: object) -> str:
    content = getattr(message, "content", "")
    return content if isinstance(content, str) else str(content)
