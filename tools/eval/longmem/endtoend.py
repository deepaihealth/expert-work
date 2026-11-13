"""P1 end-to-end QA tier — Stream CM-N5 (Mini-ADR CM-K1/K2/K6).

The cross-vendor-comparable number: benchmark sessions are **ingested
through the production write path** — ``flush_messages_to_memory``
(LLM extraction, optionally CM-7 reconcile) — then each question runs
retrieve → MMR → an LLM reading step, and the official judge protocol
grades the hypothesis (``judge`` module).

Shape notes (stated on reports; they are part of the number's
fingerprint):

- **Single memory user per corpus.** Mem0's LoCoMo protocol builds one
  store per speaker and searches both; both stores are fed the same
  transcript, so with extraction-based ingestion the contents converge
  and the split only doubles LLM cost. expert_work's product shape is one
  per-user agent memory — one store is the faithful mapping.
- **Ingestion timestamps**: ``flush_messages_to_memory`` stamps items
  "now", so CM-6 decay is neutral inside this tier (the whole corpus is
  ingested in one sitting — decay attribution lives in the P0 tier).
  Session dates are injected as a transcript header instead, so the
  extractor can fold them into memory content for temporal questions
  (the Mem0 metadata-timestamp analogue).
- Corpus groups are ingested once and shared across their questions
  (every LoCoMo QA of one conversation), exactly like the P0 tier.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage

from expert_work.common.search.mmr import mmr_select
from expert_work.persistence import InMemoryMemoryStore
from expert_work.runtime.cancellation import CancellationToken
from longmem.adapter import MemoryDoc, RetrievalInstance
from longmem.judge import (
    TextJudge,
    locomo_judge_prompt,
    longmemeval_judge_prompt,
    parse_locomo_verdict,
    parse_longmemeval_verdict,
)
from longmem.retrieval import EVAL_TENANT_ID, EVAL_USER_ID, Embedder

logger = logging.getLogger(__name__)

Benchmark = Literal["longmemeval", "locomo"]

_ANSWER_SYSTEM = (
    "You are a helpful assistant answering a question about a user from "
    "their long-term memory. Use ONLY the memories provided. Answer the "
    "question concisely. If the memories do not contain the information "
    "needed, say that the information is not available."
)


@dataclass(frozen=True)
class EndToEndConfig:
    """Production-default recall shape + ingestion switches."""

    top_k: int = 10
    wide_limit: int = 20
    hybrid: bool = True
    mmr: bool = True
    reconcile: bool = True


@dataclass(frozen=True)
class QAResult:
    question_id: str
    question_type: str
    hypothesis: str
    correct: bool
    n_memories: int


@dataclass(frozen=True)
class EndToEndReport:
    config: EndToEndConfig
    benchmark: Benchmark
    n_instances: int
    accuracy: float
    memories_written: int
    by_question_type: dict[str, dict[str, float]] = field(default_factory=dict)
    results: tuple[QAResult, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("results")
        return data


#: B-300 PR 1(设计 §4.1):第一次提炼只看白名单里「用户在对话里输入的文字」,只有生产的
#: ``_build_human_message`` 写那个标记。这里的语料是整段会话塞进一条用户消息、不经那个构造点,
#: 所以必须经提炼入口的**显式参数**声明「这几条是用户文字」,而不是去仿造标记。参数名以 PR 1 为准;
#: 入口还没有这个参数(PR 1 之前的提交)时什么都不传,行为与改前一致。
USER_TEXT_PARAM = "user_text_messages"


def user_text_kwargs(entry: Callable[..., Any], messages: Sequence[Any]) -> dict[str, Any]:
    """``entry`` 认 :data:`USER_TEXT_PARAM` 就把 ``messages`` 声明为用户文字;不认就返回空。"""
    if USER_TEXT_PARAM in inspect.signature(entry).parameters:
        return {USER_TEXT_PARAM: list(messages)}
    logger.debug("longmem.ingest: %s has no %s parameter (pre-B-300 code)", entry, USER_TEXT_PARAM)
    return {}


def _session_transcripts(docs: Sequence[MemoryDoc]) -> list[tuple[datetime | None, str]]:
    """Group docs back into per-session transcripts, oldest first.

    One human message per session keeps the extraction call count at
    one per session and lets the date header sit next to its turns.
    """
    by_session: dict[str, list[MemoryDoc]] = defaultdict(list)
    for doc in docs:
        by_session[doc.session_id].append(doc)
    sessions = [
        (rows[0].timestamp, "\n".join(d.content for d in rows)) for rows in by_session.values()
    ]
    sessions.sort(key=lambda pair: (pair[0] is not None, pair[0] or datetime.min))
    return sessions


async def _ingest_corpus(
    docs: Sequence[MemoryDoc],
    *,
    store: InMemoryMemoryStore,
    embedder: Embedder,
    llm_caller: Any,
    reconcile: bool,
    token: CancellationToken,
    sem: asyncio.Semaphore,
) -> int:
    """Feed every session through the production extraction path.

    Sessions are independent extraction calls, so they run concurrently
    under the shared semaphore. Note: with ``reconcile=True`` concurrent
    sessions reconcile against a moving store — same best-effort
    semantics as production runs landing memories while others write.
    """
    from orchestrator.graph_builder.memory import flush_messages_to_memory

    async def _one(timestamp: datetime | None, transcript: str) -> int:
        header = (
            f"[Conversation session dated {timestamp:%Y-%m-%d %H:%M}]"
            if timestamp is not None
            else "[Conversation session]"
        )
        session = HumanMessage(content=f"{header}\n{transcript}")
        async with sem:
            return await flush_messages_to_memory(
                [session],
                memory_store=store,
                embedder=embedder,  # type: ignore[arg-type]
                llm_caller=llm_caller,
                tenant_id=EVAL_TENANT_ID,
                user_id=EVAL_USER_ID,
                thread_id=None,
                token=token,
                log_label="longmem.ingest",
                reconcile=reconcile,
                **user_text_kwargs(flush_messages_to_memory, [session]),
            )

    counts = await asyncio.gather(
        *[_one(ts, transcript) for ts, transcript in _session_transcripts(docs)]
    )
    return sum(counts)


async def _answer(
    instance: RetrievalInstance,
    *,
    store: InMemoryMemoryStore,
    embedder: Embedder,
    llm_caller: Any,
    config: EndToEndConfig,
) -> tuple[str, int]:
    """retrieve → MMR → reading call; returns (hypothesis, n_memories)."""
    (query_embedding,) = await embedder.embed([instance.question], tenant_id=EVAL_TENANT_ID)
    candidates = await store.retrieve(
        tenant_id=EVAL_TENANT_ID,
        user_id=EVAL_USER_ID,
        query_embedding=query_embedding,
        query_text=instance.question if config.hybrid else None,
        limit=max(config.top_k, config.wide_limit),
    )
    if config.mmr and candidates:
        selected = mmr_select(
            query_embedding=query_embedding,
            candidates=[(m, m.embedding) for m in candidates],
            k=config.top_k,
        )
        memories = selected if selected else candidates[: config.top_k]
    else:
        memories = candidates[: config.top_k]
    memory_block = "\n".join(f"- {m.content}" for m in memories) or "- (no memories recalled)"
    prompt = (
        f"Today's date: {instance.question_date:%Y-%m-%d}.\n\n"
        f"Memories about the user:\n{memory_block}\n\n"
        f"Question: {instance.question}"
    )
    response = await llm_caller(
        messages=[SystemMessage(content=_ANSWER_SYSTEM), HumanMessage(content=prompt)],
        tools=[],
    )
    content = response.content
    return (content if isinstance(content, str) else str(content)), len(memories)


async def _judge_one(
    instance: RetrievalInstance,
    hypothesis: str,
    *,
    judge: TextJudge,
    benchmark: Benchmark,
) -> bool:
    answer = instance.answer or ""
    if benchmark == "locomo":
        reply = await judge.complete(
            prompt=locomo_judge_prompt(
                question=instance.question, gold_answer=answer, generated_answer=hypothesis
            )
        )
        return parse_locomo_verdict(reply)
    reply = await judge.complete(
        prompt=longmemeval_judge_prompt(
            question_type=instance.question_type,
            question=instance.question,
            answer=answer,
            hypothesis=hypothesis,
            abstention=instance.question_id.endswith("_abs"),
        )
    )
    return parse_longmemeval_verdict(reply)


async def run_end_to_end(
    instances: Sequence[RetrievalInstance],
    *,
    benchmark: Benchmark,
    embedder: Embedder,
    llm_caller: Any,
    judge: TextJudge,
    config: EndToEndConfig | None = None,
    done_ids: frozenset[str] = frozenset(),
    on_result: Callable[[QAResult], Awaitable[None]] | None = None,
    concurrency: int = 1,
) -> EndToEndReport:
    """Run the full QA tier; supports checkpoint-resume.

    ``done_ids`` skips already-graded questions (their rows live in the
    runner's jsonl); ``on_result`` fires after each verdict so the
    runner can append incrementally — a multi-hour, $100+ full run must
    survive interruption without re-paying for finished questions. A
    corpus group whose every question is done is never ingested again.

    ``concurrency`` bounds in-flight LLM work (session extractions and
    per-question answer+judge) under one shared semaphore; ``gather``
    keeps results input-ordered and a write lock serialises
    ``on_result`` so concurrent verdicts never interleave a jsonl line.
    """
    cfg = config or EndToEndConfig()
    token = CancellationToken()
    sem = asyncio.Semaphore(max(1, concurrency))
    write_lock = asyncio.Lock()

    groups: dict[tuple[int, datetime], list[RetrievalInstance]] = defaultdict(list)
    for instance in instances:
        groups[(id(instance.docs), instance.question_date)].append(instance)

    async def _process_question(
        instance: RetrievalInstance, store: InMemoryMemoryStore
    ) -> QAResult:
        async with sem:
            hypothesis, n_memories = await _answer(
                instance, store=store, embedder=embedder, llm_caller=llm_caller, config=cfg
            )
            correct = await _judge_one(instance, hypothesis, judge=judge, benchmark=benchmark)
        result = QAResult(
            question_id=instance.question_id,
            question_type=instance.question_type,
            hypothesis=hypothesis,
            correct=correct,
            n_memories=n_memories,
        )
        if on_result is not None:
            async with write_lock:
                await on_result(result)
        return result

    async def _process_group(grouped: list[RetrievalInstance]) -> tuple[list[QAResult], int]:
        pending = [i for i in grouped if i.question_id not in done_ids]
        if not pending:
            return [], 0
        store = InMemoryMemoryStore()
        written = await _ingest_corpus(
            grouped[0].docs,
            store=store,
            embedder=embedder,
            llm_caller=llm_caller,
            reconcile=cfg.reconcile,
            token=token,
            sem=sem,
        )
        rows = await asyncio.gather(*[_process_question(i, store) for i in pending])
        return list(rows), written

    group_outcomes = await asyncio.gather(*[_process_group(g) for g in groups.values()])
    results: list[QAResult] = []
    written_total = 0
    for rows, written in group_outcomes:
        results.extend(rows)
        written_total += written

    by_type: dict[str, list[QAResult]] = defaultdict(list)
    for result in results:
        by_type[result.question_type].append(result)
    breakdown = {
        qtype: {
            "n": float(len(rows)),
            "accuracy": sum(1 for r in rows if r.correct) / len(rows),
        }
        for qtype, rows in sorted(by_type.items())
    }
    return EndToEndReport(
        config=cfg,
        benchmark=benchmark,
        n_instances=len(results),
        accuracy=(sum(1 for r in results if r.correct) / len(results)) if results else 0.0,
        memories_written=written_total,
        by_question_type=breakdown,
        results=tuple(results),
    )
