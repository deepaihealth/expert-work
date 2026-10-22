"""The reranker over a Cohere-style client — B-206 K-7c.

What the model catalog says about the model
(:class:`~expert_work.protocol.model_catalog.RerankCaps`) shapes its requests:

* **lengths** — a model that counts characters (``length_unit="char"``) is
  measured exactly: the query is cut to ``query_max_tokens``, each document to
  ``max_doc_tokens`` and to what ``max_total_tokens`` leaves after the query, and
  requests are packed so the query (counted once — Zhipu, measured 2026-10-22)
  plus their documents stay within ``max_total_tokens``. A model counting its
  own tokens gets the text as handed (callers cap each document, see
  :data:`~orchestrator.tools.knowledge.RERANK_DOC_MAX_CHARS`); when its vendor
  refuses a request for size (``too_large``) that request is sent once more
  with what surely fits — the query and each document cut to their limits in
  UTF-8 bytes (a token is never shorter than a byte), then the leading
  documents within ``max_total_tokens`` (:func:`~orchestrator.llm.rerank_limits.fit_total`).
* **pool size** — a pool that takes more than one request (over
  ``max_documents``, or over ``max_total_tokens`` for a character model) is
  split, the requests sent together (how many run at once is the guard's cap,
  :class:`~orchestrator.llm.rerank_guard.RerankGuards`) and merged by score —
  only when scores from different requests mean the same thing
  (``normalized``, ``sigmoid``, ``logit``). A ``relative`` score, or an
  undeclared scale, orders one request's documents only: the first request's
  documents are sent and the rest reported as dropped, as
  :class:`~orchestrator.llm.rerank.DashScopeReranker` does. A split pool's query
  is cut to its limit (in UTF-8 bytes for a token model) before any request, so
  a size refusal's resend of one slice keeps the query every other slice was
  judged against. The first request to fail cancels the others and fails the
  rerank (the search falls back to the fused order): half a pool's ranking is
  not a ranking of the pool.
* **scores** — ``score_scale="logit"`` scores are exposed as their sigmoid, in
  (0, 1) like the ``normalized`` / ``sigmoid`` ones, with the raw logits kept on
  the ranking (:class:`~orchestrator.tools.knowledge_rerank.RerankRanking`'s
  ``logits``); a split pool merges on the raw score, which a sigmoid saturated
  at 1.0 would not order. Every other score is kept as the vendor gave it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from expert_work.protocol.model_catalog import RerankCaps
from orchestrator.llm.rerank_guard import RerankCallError, RerankClient
from orchestrator.llm.rerank_limits import cut, fit_total, relevance
from orchestrator.tools.knowledge_rerank import RerankError, RerankRanking, sigmoid

logger = logging.getLogger(__name__)

#: Scales whose scores compare across requests (see the module docstring).
_COMPARABLE_SCALES = frozenset({"normalized", "sigmoid", "logit"})

#: One request's documents and its ``(index, raw score)`` rows — ``None`` when
#: the answer had no result list.
_Answer = tuple[list[str], list[tuple[int, float | None]] | None]


@dataclass(frozen=True)
class CohereStyleReranker:
    """:class:`orchestrator.tools.knowledge.Reranker` (and ``ScoredReranker``)
    over a Cohere-style client (:mod:`orchestrator.llm.rerank_cohere`, behind the
    guard). ``caps`` is the model's catalog entry — ``None`` for an off-catalog
    model, which gets every document in one request and its scores as given.

    Returns a :class:`~orchestrator.tools.knowledge.RerankRanking`: best first,
    at most ``top_k``, saying how many documents went unjudged; an answer it
    cannot read degrades to the input order without scores (``model_error``)."""

    client: RerankClient
    model: str
    caps: RerankCaps | None = None

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        ranked = await self.rerank_scored(
            query=query, documents=documents, top_k=top_k, tenant_id=tenant_id
        )
        return [index for index, _score in ranked]

    async def rerank_scored(
        self,
        *,
        query: str,
        documents: Sequence[str],
        top_k: int,
        tenant_id: UUID,
        instruction: str | None = None,
    ) -> list[tuple[int, float | None]]:
        del tenant_id  # credential is baked into ``client``
        if not documents:
            return []
        caps = self.caps
        if caps is None or not caps.supports_instruction:
            instruction = None
        query, items = _within_lengths(query, documents, instruction, caps)
        slices = _slices(query, items, instruction, caps)
        if len(slices) > 1 and caps is not None:
            query = cut(query, caps.query_max_tokens, by_char=caps.length_unit == "char")
        extra: dict[str, Any] = {} if instruction is None else {"instruction": instruction}
        answers = await self._all(query, items, slices, top_k, extra)
        dropped = len(documents) - sum(len(sent) for sent, _rows in answers)
        if any(rows is None for _sent, rows in answers):
            unscored = ((index, None) for index in range(min(len(documents), top_k)))
            return RerankRanking(unscored, dropped=dropped, outcome="model_error")
        ranked = [
            (start + index, raw)
            for (start, _stop), (_sent, rows) in zip(slices, answers, strict=True)
            for index, raw in rows or ()
        ]
        if len(slices) > 1:
            ranked.sort(key=_best_first)
        ranked = ranked[:top_k]
        if caps is None or caps.score_scale != "logit":
            return RerankRanking(ranked, dropped=dropped)
        return RerankRanking(
            ((index, None if raw is None else sigmoid(raw)) for index, raw in ranked),
            dropped=dropped,
            logits={index: raw for index, raw in ranked if raw is not None},
        )

    async def _all(
        self,
        query: str,
        items: list[str],
        slices: list[tuple[int, int]],
        top_k: int,
        extra: Mapping[str, Any],
    ) -> list[_Answer]:
        """Every slice's request, sent together; the first to fail cancels the
        rest and its error is raised (see :func:`_most_informative`)."""
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(self._request(query, items[start:stop], top_k, extra))
                    for start, stop in slices
                ]
        except ExceptionGroup as failed:
            # Siblings are cancelled at the first failure, so the group rarely
            # holds more than one; the chosen error is raised as itself.
            raise _most_informative(failed) from None
        return [task.result() for task in tasks]

    async def _request(
        self, query: str, documents: list[str], top_k: int, extra: Mapping[str, Any]
    ) -> _Answer:
        """One request; on a size refusal, once more with what surely fits."""
        try:
            body = await self.client.rerank(
                model=self.model,
                query=query,
                documents=documents,
                top_n=min(top_k, len(documents)),
                **extra,
            )
        except RerankCallError as exc:
            if exc.failure != "too_large":
                raise
            fitted_query, fitted = _surely_fits(
                query, documents, extra.get("instruction"), self.caps
            )
            if not fitted or (fitted_query, fitted) == (query, documents):
                raise
            logger.warning(
                "rerank refused %d documents for size; retrying with the %d that surely fit "
                "model=%s",
                len(documents),
                len(fitted),
                self.model,
            )
            query, documents = fitted_query, fitted
            body = await self.client.rerank(
                model=self.model,
                query=query,
                documents=documents,
                top_n=min(top_k, len(documents)),
                **extra,
            )
        rows = body.get("results")
        if not isinstance(rows, list):
            return documents, None
        return documents, [
            (row["index"], relevance(row.get("relevance_score")))
            for row in rows
            if isinstance(row, Mapping)
            and isinstance(row.get("index"), int)
            and not isinstance(row.get("index"), bool)
            and 0 <= row["index"] < len(documents)
        ]


def _most_informative(failed: ExceptionGroup[Exception]) -> Exception:
    """The error a failed split rerank raises: the first that says how the
    search should report it (a :class:`RerankError` — ``rate_limited`` or
    ``model_error``), else the first."""
    errors = list(failed.exceptions)
    for error in errors:
        if isinstance(error, RerankError):
            return error
    return errors[0]


def _within_lengths(
    query: str, documents: Sequence[str], instruction: str | None, caps: RerankCaps | None
) -> tuple[str, list[str]]:
    """``query`` and ``documents`` as first sent: for a character model cut to
    its query and document limits, and each document to what the request total
    leaves after the query and instruction; as handed otherwise."""
    if caps is None or caps.length_unit != "char":
        return query, list(documents)
    query = cut(query, caps.query_max_tokens, by_char=True)
    room = _room(query, instruction, caps)
    limits = [limit for limit in (caps.max_doc_tokens, room) if limit is not None]
    longest = min(limits) if limits else None
    return query, [cut(document, longest, by_char=True) for document in documents]


def _room(query: str, instruction: str | None, caps: RerankCaps) -> int | None:
    """The characters a character model's request total leaves for documents
    once the query and instruction are counted (once); ``None`` when no total
    is declared, or nothing is left (the vendor is left to refuse)."""
    if caps.length_unit != "char" or caps.max_total_tokens is None:
        return None
    room = caps.max_total_tokens - len(query) - len(instruction or "")
    return room if room > 0 else None


def _slices(
    query: str, items: Sequence[str], instruction: str | None, caps: RerankCaps | None
) -> list[tuple[int, int]]:
    """``(start, stop)`` of each request's share of the pool (see the module
    docstring): at most ``max_documents`` each and, for a character model,
    within the request total; only the first when scores do not compare."""
    limit = caps.max_documents if caps is not None else None
    room = _room(query, instruction, caps) if caps is not None else None
    slices: list[tuple[int, int]] = []
    start = used = 0
    for index, item in enumerate(items):
        full = limit is not None and index - start >= limit
        if room is not None and index > start and used + len(item) > room:
            full = True
        if full:
            slices.append((start, index))
            start, used = index, 0
        used += len(item)
    slices.append((start, len(items)))
    if len(slices) > 1 and (caps is None or caps.score_scale not in _COMPARABLE_SCALES):
        return slices[:1]
    return slices


def _surely_fits(
    query: str, documents: list[str], instruction: str | None, caps: RerankCaps | None
) -> tuple[str, list[str]]:
    """What surely fits the model's limits: the query and each document cut to
    them (UTF-8 bytes for a token model), then the leading documents within the
    request total."""
    if caps is None:
        return query, documents
    by_char = caps.length_unit == "char"
    query = cut(query, caps.query_max_tokens, by_char=by_char)
    trimmed = [cut(document, caps.max_doc_tokens, by_char=by_char) for document in documents]
    return query, fit_total(query, trimmed, instruction, caps)


def _best_first(row: tuple[int, float | None]) -> tuple[bool, float]:
    """Sort key: highest raw score first, unscored rows last (stable otherwise)."""
    _index, raw = row
    return (raw is None, -(raw or 0.0))
