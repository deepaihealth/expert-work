"""Retrieval log of knowledge searches — B-175 P2-PR4.

:class:`LoggedKnowledgeRetriever` is the production :class:`KnowledgeRetriever`:
a search it runs writes one ``knowledge_retrieval_log`` row through its
:class:`RetrievalLogSink` — the bases searched, the query, the parameters, how
the search ended (``error`` when it raised), each result's ids and scores
(never its text), the degradation flags and the latency. Only searches a purge
can find again are logged:

* an agent's ``knowledge_search`` — inside a run, under its run id and
  conversation (:func:`~expert_work.common.context.get_current_run_id` /
  ``get_current_thread_id``, bound by the run worker);
* a console retrieval test — wrapped in :func:`retrieval_log_scope`, as
  ``console_test`` under the employee; the scope's :class:`RetrievalLogReceipt`
  reports the search's own latency and the row id back.

A search with neither (e.g. the skill-evolution replay) writes nothing.

Logging is best-effort and cheap on the agent path: it reuses the bases the
search resolved and the document id on each result (no extra reads); an
agent's search waits for its write at most :data:`AGENT_LOG_TIMEOUT_S` — the
write runs as its own task, never cancelled, and finishes (or fails) in the
background; a failed write never fails the search, and what it logs is the
source and the exception type — never the exception text, which for a driver
error carries the statement's parameters, the query among them (the query must
never reach application logs).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, fields
from typing import Any, Protocol
from uuid import UUID

from expert_work.common.context import get_current_run_id, get_current_thread_id
from expert_work.persistence.knowledge import (
    RETRIEVAL_LOG_ERROR,
    RetrievalLogEntry,
    RetrievalLogSource,
)
from expert_work.protocol import KnowledgeBase, RetrievalMethod
from orchestrator.tools.knowledge import (
    KnowledgeRetriever,
    KnowledgeSearchOutcome,
    RetrievedChunk,
)

logger = logging.getLogger(__name__)

#: How long an agent's search waits for its log write before carrying on; the
#: write itself carries on in the background. A console test waits for its row.
AGENT_LOG_TIMEOUT_S = 1.0

#: Agent-path writes still running: a strong reference, so a pending write is
#: not garbage-collected (the event loop only holds tasks weakly).
_PENDING_WRITES: set[asyncio.Task[None]] = set()


def _write_done(task: asyncio.Task[None]) -> None:
    """Drop the finished write and log how it failed — the type only."""
    _PENDING_WRITES.discard(task)
    if task.cancelled():
        return
    if (exc := task.exception()) is not None:
        logger.warning("knowledge.retrieval_log_failed source=agent error=%s", type(exc).__name__)


async def _write_in_background(sink: RetrievalLogSink, entry: RetrievalLogEntry) -> None:
    """Start an agent's log write and wait for it at most
    :data:`AGENT_LOG_TIMEOUT_S`. The write is never cancelled: cancelling a
    database write waits for its cleanup (the session closes under a shield,
    with no command timeout), which is the hold-up this avoids."""
    task = asyncio.create_task(sink.add(entry))
    _PENDING_WRITES.add(task)
    task.add_done_callback(_write_done)  # before ``wait``: logs before we resume
    await asyncio.wait({task}, timeout=AGENT_LOG_TIMEOUT_S)


#: Outcome fields that are not flags: the results are logged as ids + scores,
#: the status has its own column.
_NOT_FLAGS = frozenset({"results", "status"})


class RetrievalLogSink(Protocol):
    """Where log rows go — ``KnowledgeRetrievalLogStore`` in production."""

    async def add(self, entry: RetrievalLogEntry) -> None:
        """Write one log row."""


@dataclass
class RetrievalLogReceipt:
    """What a scoped search reports back: its latency (measured before
    logging) and the row id once the row is written."""

    latency_ms: int | None = None
    log_id: UUID | None = None


@dataclass(frozen=True)
class RetrievalLogContext:
    """A search someone outside a run asked for: its ``source``, the employee
    behind it (``actor_id``), and flags the caller adds to the row (e.g. the
    console's ``platform_admin`` for a search of another tenant's base)."""

    source: RetrievalLogSource
    actor_id: str | None = None
    receipt: RetrievalLogReceipt = field(default_factory=RetrievalLogReceipt)
    flags: Mapping[str, Any] = field(default_factory=dict)


retrieval_log_context: ContextVar[RetrievalLogContext | None] = ContextVar(
    "knowledge_retrieval_log_context", default=None
)
#: The bases one search resolved, collected for its log row.
_resolved_bases: ContextVar[list[KnowledgeBase] | None] = ContextVar(
    "knowledge_retrieval_log_bases", default=None
)


@contextmanager
def retrieval_log_scope(context: RetrievalLogContext) -> Iterator[RetrievalLogReceipt]:
    """Log the searches run inside the block as ``context`` says; yields its receipt."""
    token = retrieval_log_context.set(context)
    try:
        yield context.receipt
    finally:
        retrieval_log_context.reset(token)


def _uuid_text(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _result(result: RetrievedChunk) -> dict[str, Any]:
    """One result as logged: ids, position and the four scores — no text."""
    return {
        "chunk_id": _uuid_text(result.chunk_id),
        "document_id": _uuid_text(result.document_id),
        "parent_chunk_id": _uuid_text(result.parent_chunk_id),
        "chunk_index": result.chunk_index,
        "parent_index": result.parent_index,
        "recall_source": result.recall_source,
        "vector_score": result.vector_score,
        "keyword_score": result.keyword_score,
        "fused_score": result.fused_score,
        "rerank_score": result.rerank_score,
    }


def _flags(outcome: KnowledgeSearchOutcome) -> dict[str, Any]:
    """Every outcome field but the results and the status — degradation flags,
    candidate counts and the rerank model, including ones added later."""
    return {f.name: getattr(outcome, f.name) for f in fields(outcome) if f.name not in _NOT_FLAGS}


@dataclass(frozen=True)
class _Search:
    """One search, as its log row needs it."""

    tenant_id: UUID
    query: str
    params: Mapping[str, Any]
    context: RetrievalLogContext | None
    run_id: UUID | None
    bases: Sequence[KnowledgeBase]
    started: float


@dataclass(frozen=True)
class LoggedKnowledgeRetriever(KnowledgeRetriever):
    """:class:`KnowledgeRetriever` that logs each search to ``log_sink``
    (nothing is logged when it is ``None``). A plain field, so
    ``dataclasses.replace`` keeps both the subclass and the sink."""

    log_sink: RetrievalLogSink | None = None

    async def _resolve_bases(
        self, tenant_id: UUID, base_names: Sequence[str]
    ) -> list[KnowledgeBase]:
        bases = await super()._resolve_bases(tenant_id, base_names)
        seen = _resolved_bases.get()
        if seen is not None:
            seen.extend(bases)
        return bases

    async def search_detailed(
        self,
        *,
        tenant_id: UUID,
        base_names: Sequence[str],
        query: str,
        limit: int,
        method: RetrievalMethod | None = None,
        score_threshold: float | None = None,
        rerank: bool | None = None,
        rerank_score_threshold: float | None = None,
    ) -> KnowledgeSearchOutcome:
        context, run_id = retrieval_log_context.get(), get_current_run_id()
        overrides: dict[str, Any] = {
            "method": method,
            "score_threshold": score_threshold,
            "rerank": rerank,
            "rerank_score_threshold": rerank_score_threshold,
        }
        if self.log_sink is None or (context is None and run_id is None):
            # Nothing to log to, or nobody a purge could find the row under.
            return await super().search_detailed(
                tenant_id=tenant_id, base_names=base_names, query=query, limit=limit, **overrides
            )
        bases: list[KnowledgeBase] = []
        search = _Search(
            tenant_id=tenant_id,
            query=query,
            params={
                "limit": limit,
                **overrides,
                "method": method.value if method is not None else None,
            },
            context=context,
            run_id=run_id,
            bases=bases,
            started=time.monotonic(),
        )
        token = _resolved_bases.set(bases)
        try:
            outcome = await super().search_detailed(
                tenant_id=tenant_id, base_names=base_names, query=query, limit=limit, **overrides
            )
        except Exception as exc:
            await self._log(search, None, error=type(exc).__name__)
            raise
        finally:
            _resolved_bases.reset(token)
        await self._log(search, outcome)
        return outcome

    async def _log(
        self, search: _Search, outcome: KnowledgeSearchOutcome | None, *, error: str = ""
    ) -> None:
        """Write the search's row — never raising, never logging its text."""
        latency_ms = int((time.monotonic() - search.started) * 1000)
        context = search.context
        if context is not None:
            context.receipt.latency_ms = latency_ms
        source: RetrievalLogSource = context.source if context is not None else "agent"
        try:
            kb_ids = tuple(dict.fromkeys(base.id for base in search.bases))
            if not kb_ids or self.log_sink is None:
                return  # nothing was searched: no base to list the row under
            entry = RetrievalLogEntry(
                tenant_id=search.tenant_id,
                kb_ids=kb_ids,
                source=source,
                query=search.query,
                status=outcome.status if outcome is not None else RETRIEVAL_LOG_ERROR,
                latency_ms=latency_ms,
                params=search.params,
                results=tuple(_result(r) for r in outcome.results) if outcome is not None else (),
                flags={
                    **(_flags(outcome) if outcome is not None else {"error": error}),
                    **(context.flags if context is not None else {}),
                },
                run_id=search.run_id,
                thread_id=get_current_thread_id(),
                actor_id=context.actor_id if context is not None else None,
            )
            if context is None:
                await _write_in_background(self.log_sink, entry)
                return
            await self.log_sink.add(entry)
        except Exception as exc:  # the log never fails a search
            # The type only: a driver error's text echoes the query.
            logger.warning(
                "knowledge.retrieval_log_failed source=%s error=%s", source, type(exc).__name__
            )
            return
        if context is not None:
            context.receipt.log_id = entry.id


__all__ = [
    "AGENT_LOG_TIMEOUT_S",
    "LoggedKnowledgeRetriever",
    "RetrievalLogContext",
    "RetrievalLogReceipt",
    "RetrievalLogSink",
    "retrieval_log_context",
    "retrieval_log_scope",
]
