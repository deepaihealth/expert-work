"""What a knowledge search and any reranker agree on.

The reranker protocols and the ranking a scoring reranker returns (moved here
from :mod:`orchestrator.tools.knowledge`, which re-exports them), plus B-206
K-7's additions: how one search's rerank ended (:data:`RerankOutcome`), the
errors that say how a failed rerank is reported (:class:`RerankError`, and
:class:`RerankUnavailableError` when a reranker gives up), and the time one
search's rerank stage may take (:func:`rerank_budget`) — shared by every rerank
request of that search, so the dual view does not double it.

Nothing here imports :mod:`orchestrator.llm`: the vendor-side guard
(:mod:`orchestrator.llm.rerank_guard`) imports this module, not the reverse.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

#: How a search's rerank ended (B-206 K-7):
#: ``ok`` the model's order was used; ``partial`` (dual view) one view's order
#: was used because the other view failed or answered without scores;
#: ``rate_limited`` the model's rate limit kept it from answering in time
#: (retries on HTTP 429 ran out, or the request waited for a pacing token past
#: the budget); ``model_error`` any other failure of the configured model (a
#: refusal, a 5xx / timeout that retries did not clear, an empty or malformed
#: answer, too little budget left to send a request); ``model_unavailable`` a
#: model is configured but its credential could not be resolved;
#: ``model_unsupported`` (B-206 K-7c) a model is configured whose rerank API the
#: platform has no client for; ``no_model`` rerank was asked for but no rerank
#: model is configured. In every case but ``ok`` and ``partial`` the search used
#: the fused order.
RerankOutcome = Literal[
    "ok",
    "partial",
    "rate_limited",
    "model_error",
    "model_unavailable",
    "model_unsupported",
    "no_model",
]

#: The outcomes where the configured model was called and failed — what
#: ``rerank_degraded`` reports. ``model_unavailable`` / ``model_unsupported`` /
#: ``no_model`` never called it.
RERANK_FAILED: frozenset[RerankOutcome] = frozenset({"rate_limited", "model_error"})

#: The longest one search's rerank stage may take: every request of both views,
#: their queueing for the model's rate and concurrency, and their retries. Past
#: it the search falls back to the fused order. 2026-10-22 measurement
#: (``qwen3-vl-rerank``, 600-character passages): a 100-passage request answers
#: in 0.5-0.7 s and the 20-passage second view in about 0.4 s, so a healthy
#: search spends about 1 s here; one request's full retry schedule (0.5 + 1 + 2 s
#: of backoff, see :mod:`orchestrator.llm.rerank_guard`) plus its four tries is
#: about 6-7 s. 10 s lets one view run its whole schedule and the other still
#: answer, and caps the stage at about ten times its healthy time — well under
#: the 30 s per-request HTTP timeout it used to be able to wait several times.
RERANK_BUDGET_S = 10.0


@runtime_checkable
class Reranker(Protocol):
    """Reorders retrieval candidates by judged relevance to a query.

    Stream O (Mini-ADR O-9) — ``tenant_id`` lets a credential-resolving
    reranker pick the per-tenant API key at call time. Implementations
    without per-tenant keys accept and ignore it. A resolving reranker
    that finds no credential degrades to the fused order (rerank is
    optional), so it is *not* gated at mode-switch time."""

    async def rerank(
        self, *, query: str, documents: Sequence[str], top_k: int, tenant_id: UUID
    ) -> list[int]:
        """Return the indices of the ``top_k`` most relevant ``documents``,
        most relevant first. Indices are positions in ``documents``."""


class RerankRanking(list[tuple[int, float | None]]):
    """A scoring reranker's ranking that also says how many of the documents it
    was handed it never judged (B-206 K-14): the trailing ones its model's
    per-request cap, or a vendor size refusal, left out. A plain list from a
    reranker means it judged them all.

    ``outcome`` (B-206 K-7) is how the ranking came about when it is not the
    model's own: a reranker that had no model or credential to call, or got an
    answer it could not read, returns the input order with that outcome instead
    of raising (long-term memory recall shares the reranker and must not log a
    failure for a deployment without rerank).

    ``logits`` (B-206 K-7c) — a model whose catalog ``score_scale`` is ``logit``
    answers raw logits; every score in the ranking is then their
    :func:`sigmoid`, in (0, 1), and ``logits`` keeps the raw logit behind each
    (index → logit) so rankings can be merged or combined in logit space, where
    a sigmoid's saturation near 1 does not hide differences. A score threshold
    applies to the exposed (sigmoid) score. ``None`` for every other scale.

    ``model`` (B-206 K-7b) is the model whose scores these are, as
    :func:`~expert_work.protocol.rerank_model_key` names it — a base's threshold
    set under another model is left out; ``None`` when the reranker does not say."""

    def __init__(
        self,
        ranked: Iterable[tuple[int, float | None]] = (),
        *,
        dropped: int = 0,
        outcome: RerankOutcome = "ok",
        logits: Mapping[int, float] | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(ranked)
        self.dropped = dropped
        self.outcome: RerankOutcome = outcome
        self.logits = logits
        self.model = model


def ranking_dropped(ranked: Sequence[tuple[int, float | None]]) -> int:
    """How many documents ``ranked`` left unjudged (0 for a plain list)."""
    return ranked.dropped if isinstance(ranked, RerankRanking) else 0


def ranking_outcome(ranked: Sequence[tuple[int, float | None]]) -> RerankOutcome:
    """How ``ranked`` came about (``ok`` for a plain list)."""
    return ranked.outcome if isinstance(ranked, RerankRanking) else "ok"


def ranking_logits(ranked: Sequence[tuple[int, float | None]]) -> Mapping[int, float] | None:
    """The raw logits behind ``ranked``'s scores (see :class:`RerankRanking`)."""
    return ranked.logits if isinstance(ranked, RerankRanking) else None


def sigmoid(logit: float) -> float:
    """The logistic function, without overflow for a very negative ``logit``."""
    if logit >= 0:
        return 1.0 / (1.0 + math.exp(-logit))
    tail = math.exp(logit)
    return tail / (1.0 + tail)


def weighted_views(
    top: Sequence[int],
    titled: Sequence[tuple[int, float | None]],
    body: Sequence[tuple[int, float | None]],
    weight: float,
    *,
    dropped: int,
) -> RerankRanking:
    """``top`` (the titled view's indices; ``body`` rows are positions in
    ``top``) ranked by ``weight`` x the titled score + the rest x the body score.

    Scores from a logit-scale model (both views carry ``logits``) are combined
    as logits and exposed as the sigmoid of the combination: averaging
    sigmoids lets one saturated view outweigh real evidence in the other (a
    titled logit of 16 with a body logit of 0 would lose to 4 and 4). Callers
    pass views whose every row is scored."""
    titled_logits, body_logits = ranking_logits(titled), ranking_logits(body)
    if titled_logits is not None and body_logits is not None:
        first = {i: titled_logits[i] for i in top}
        second = {top[j]: logit for j, logit in body_logits.items() if j < len(top)}
    else:
        first = {i: score for i, score in titled if score is not None}
        second = {top[j]: score for j, score in body if score is not None}
    combined = {i: weight * first[i] + (1 - weight) * second[i] for i in top}
    order = sorted(top, key=lambda i: (-combined[i], i))
    if titled_logits is None or body_logits is None:
        return RerankRanking(
            ((i, combined[i]) for i in order), dropped=dropped, model=ranking_model(titled)
        )
    return RerankRanking(
        ((i, sigmoid(combined[i])) for i in order),
        dropped=dropped,
        logits=combined,
        model=ranking_model(titled),
    )


def ranking_model(ranked: Sequence[tuple[int, float | None]]) -> str | None:
    """The model that scored ``ranked`` (``None`` for a plain list)."""
    return ranked.model if isinstance(ranked, RerankRanking) else None


@runtime_checkable
class ScoredReranker(Protocol):
    """A :class:`Reranker` that can also say how relevant each document is.

    ``instruction`` (B-206 K-14) is a task statement for a model that takes one;
    a reranker whose model takes none ignores it. Callers pass it only when they
    have one, so a scoring reranker written before it existed keeps working. A
    reranker that judges only some of the documents returns a
    :class:`RerankRanking` saying how many it left out."""

    async def rerank_scored(
        self,
        *,
        query: str,
        documents: Sequence[str],
        top_k: int,
        tenant_id: UUID,
        instruction: str | None = None,
    ) -> list[tuple[int, float | None]]:
        """Like :meth:`Reranker.rerank`, each index paired with its relevance
        score: ``None`` throughout when the backend ranks without scoring (a
        chat-model reranker) or rerank was skipped."""


class RerankError(Exception):
    """A failed rerank that says how the search should report it (B-206 K-7a):
    ``outcome`` is ``rate_limited`` or ``model_error``. Vendor clients raise a
    subclass (:class:`~orchestrator.llm.rerank_guard.RerankCallError`) whether
    or not a guard sits in front of them; any other exception is a
    ``model_error``."""

    def __init__(self, message: str, *, outcome: RerankOutcome = "model_error") -> None:
        super().__init__(message)
        self.outcome: RerankOutcome = outcome


class RerankUnavailableError(RerankError):
    """A rerank request that gave up (B-206 K-7): retries ran out, or the
    search's :func:`rerank_budget` did."""


@dataclass
class RerankBudget:
    """One search's rerank allowance: the ``deadline`` (on the clock that made
    it, :func:`time.monotonic` in production) and how many requests the search
    has sent so far — counted by whoever sends them, for the degraded-search log."""

    deadline: float
    attempts: int = 0


_BUDGET: ContextVar[RerankBudget | None] = ContextVar("rerank_budget", default=None)


@contextmanager
def rerank_budget(
    seconds: float = RERANK_BUDGET_S, *, clock: Callable[[], float] = time.monotonic
) -> Iterator[RerankBudget]:
    """Give every rerank request made inside the block one shared
    :class:`RerankBudget` of ``seconds``."""
    budget = RerankBudget(deadline=clock() + seconds)
    token = _BUDGET.set(budget)
    try:
        yield budget
    finally:
        _BUDGET.reset(token)


def current_rerank_budget() -> RerankBudget | None:
    """The budget of the search this request belongs to; ``None`` outside one
    (long-term memory recall, the eval) — the request then gets its own."""
    return _BUDGET.get()
