"""Memory nodes — Stream J.3 (long-term memory recall + write-back).

When a manifest enables long-term memory (``memory.long_term``) the
factory adds two graph nodes around the agent loop:

::

    START → [memory_recall] → ... agent ⇄ tools ... → [memory_writeback] → END

- ``memory_recall`` embeds the user's task, fetches the top-k nearest
  past memories, and parks them on ``AgentState.recalled_memories`` —
  ``agent_node`` renders them into its system context every step.
- ``memory_writeback`` makes one LLM call that extracts new durable
  memories from the run's trajectory, embeds them, and persists them.

Both no-op when the run carries no per-user scope (no ``user_id``) —
long-term memory is per-user. Recall / write-back are best-effort: any
failure is logged and swallowed so it never fails the run (cancellation
still propagates).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from expert_work.common.observability import ExpertWorkComponent, expert_work_span
from expert_work.common.search import mmr_select
from expert_work.common.threat_patterns import scan_for_threats
from expert_work.common.uplift_metrics import (
    record_memory_abstain,
    record_memory_drift,
    record_memory_mmr,
    record_memory_reconcile,
    record_memory_redacted,
    record_memory_rerank,
    record_memory_retrieval,
    record_memory_rewrite,
    record_memory_verify,
)
from expert_work.persistence import MemoryStore
from expert_work.persistence.memory import MemoryWritebackDLQ
from expert_work.persistence.memory.base import MemoryInjectionBlockedError
from expert_work.persistence.tenant_config import TenantConfigStore
from expert_work.protocol import MemoryItem, MemoryRecallMode
from expert_work.runtime.cancellation import CancellationToken, RunCancelledError
from orchestrator.graph_builder._config import (
    cancellation_token,
    configurable_uuid,
    current_run_id,
)
from orchestrator.llm import Embedder, LLMCaller
from orchestrator.state import AgentState
from orchestrator.tools.knowledge import Reranker

logger = logging.getLogger(__name__)

#: A memory graph node: takes state + config, returns state updates.
MemoryNode = Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]

#: Stream CM-4/CM-6 — candidate depth recalled before the re-ranking stages
#: (cross-encoder rerank, then MMR diversity selection), wider than the
#: final top-k so both have alternatives to choose from. Mirrors the
#: knowledge retriever's recall limit; always applied since CM-6 (the
#: stores fetch this many fusion candidates anyway — Mini-ADR CM-G5).
_MEMORY_RECALL_WIDE_LIMIT = 20

#: Per-message cap when rendering the trajectory for the extraction prompt.
_TRAJECTORY_CHAR_CAP = 1000

_EXTRACT_SYSTEM = (
    "You are a memory extraction module. From the conversation, extract "
    "durable, reusable memories worth recalling in future sessions — "
    'stable user facts or preferences ("fact"), and concise summaries of '
    'what was done or decided ("episodic"). Extract nothing trivial or '
    "ephemeral. For each memory also rate two scores in [0, 1]: "
    '"importance" (how reusable it is in future sessions — rare stable '
    'user facts high, one-off chatter low) and "confidence" (how sure '
    "you are it is true — explicit statements high, inferred or hedged "
    "low). Also give:\n"
    '- "expected_valid_days": how many days you expect this to stay true '
    "before it is worth re-checking — a rough guide: volatile/ephemeral "
    "~7, seasonal/plans ~90, yearly ~365, stable preferences ~1095, "
    "near-permanent identity facts ~3650. Omit or null if you cannot "
    "estimate.\n"
    '- "is_correction": true ONLY if this memory explicitly corrects or '
    "updates something stated earlier in the conversation (e.g. the user "
    "says they moved, changed jobs, renamed a thing); otherwise false.\n"
    "Respond with ONLY a JSON object, no prose and no code fences:\n"
    '{"memories": [{"kind": "fact" | "episodic", "content": "<one concise '
    'sentence>", "importance": <0-1>, "confidence": <0-1>, '
    '"expected_valid_days": <int or null>, "is_correction": <bool>}]}\n'
    'If there is nothing worth remembering, return {"memories": []}.'
)


def _message_text(message: BaseMessage) -> str:
    content = message.content
    return content if isinstance(content, str) else str(content)


def _last_human_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return _message_text(message)
    return ""


def _render_trajectory(messages: list[BaseMessage]) -> str:
    lines: list[str] = []
    for message in messages:
        # Skip the agent's own system prompt — it is an instruction to the
        # agent, not signal for memory extraction, and it otherwise dominated
        # the extraction input (the confusing ``[system] ...`` line surfaced in
        # the debug console).
        if isinstance(message, SystemMessage):
            continue
        text = _message_text(message).strip()
        if len(text) > _TRAJECTORY_CHAR_CAP:
            text = text[:_TRAJECTORY_CHAR_CAP] + "...[truncated]"
        lines.append(f"[{message.type}] {text}")
    return "\n".join(lines)


def _extract_json_object(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    return text[start : end + 1]


@dataclass(frozen=True)
class ExtractedMemory:
    """One memory parsed from the extraction LLM reply — Stream Memory-Enhance
    (M-2). ``importance`` feeds the write-filter; ``confidence`` is persisted
    for downstream ranking / correction. Both default 0.5 when the model omits
    or malforms them (write-back is best-effort — a missing score never drops
    an otherwise-valid memory)."""

    kind: Literal["fact", "episodic"]
    content: str
    importance: float = 0.5
    confidence: float = 0.5
    # P5b-2b ⑦ — LLM-predicted validity window (days); drives the consolidator
    # due-scan. None = no prediction (never scanned for predictive review).
    expected_valid_days: int | None = None
    # P5b-2b ⑧ — LLM flag: this memory explicitly corrects prior info. Transient
    # (never persisted); threaded into reconcile to bias the UPDATE decision.
    is_correction: bool = False


#: Stream RT-2 PR-2 (review HIGH) — ceiling for extraction-scored confidence.
#: ``confidence == 1.0`` is the M-4 correction API's EXCLUSIVE sentinel
#: (user-asserted truth — the control-plane correction endpoint is its only
#: writer), and the injection budget's correction guarantee keys on it
#: (``builder._render_memory_lines``). The extraction prompt encourages high
#: confidence and ``_clamp_score`` pins >=1 replies to exactly 1.0, so
#: without this cap an ordinary extracted memory could impersonate a
#: correction and eat the guaranteed slice — squeezing a real user
#: correction out under a tight budget. Historical limitation: rows written
#: before this cap may already carry an extracted 1.0 that cannot be told
#: apart from a real M-4 correction (no provenance column); the skew only
#: matters under tight budgets and decays as new writes land, so no
#: migration (the source is indistinguishable after the fact).
_MAX_EXTRACTED_CONFIDENCE = 0.99


def _clamp_score(value: object) -> float:
    """Parse a model-supplied score into [0, 1], falling back to 0.5 (neutral)
    on anything non-numeric or out of range-ish."""
    try:
        score = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5
    if math.isnan(score):
        return 0.5
    return min(1.0, max(0.0, score))


def _parse_valid_days(value: object) -> int | None:
    """P5b-2b ⑦ — parse a model-supplied validity window into a positive int,
    or None on anything non-numeric / non-positive (best-effort — a bad window
    never drops the memory, it just opts the row out of predictive review)."""
    try:
        days = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        # OverflowError — ``int(float('inf'))`` raises this (a sibling of
        # TypeError/ValueError, not a subclass). ``json.loads`` accepts the
        # non-standard ``Infinity`` / ``-Infinity`` tokens by default, so an
        # LLM reply carrying ``"expected_valid_days": Infinity`` must degrade
        # to None here rather than propagate out of ``parse_extracted_memories``
        # and drop the whole extraction batch.
        return None
    return days if days > 0 else None


def parse_extracted_memories(text: str) -> list[ExtractedMemory]:
    """Parse the extraction LLM reply into :class:`ExtractedMemory` items.

    Tolerant — any malformed reply yields ``[]`` (write-back is
    best-effort). Duplicate contents within the batch are dropped. A missing
    or invalid ``importance`` / ``confidence`` defaults to 0.5 (neutral);
    ``confidence`` is additionally capped at ``_MAX_EXTRACTED_CONFIDENCE``
    (0.99) — 1.0 is the M-4 correction API's exclusive sentinel.
    """
    raw = _extract_json_object(text)
    if raw is None:
        return []
    try:
        data = json.loads(raw)
        rows = data["memories"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []
    out: list[ExtractedMemory] = []
    seen: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind", "")).strip().lower()
        content = str(row.get("content", "")).strip()
        if kind in ("fact", "episodic") and content and content not in seen:
            seen.add(content)
            out.append(
                ExtractedMemory(
                    kind=cast(Literal["fact", "episodic"], kind),
                    content=content,
                    importance=_clamp_score(row.get("importance")),
                    # Capped below the M-4 sentinel — see
                    # ``_MAX_EXTRACTED_CONFIDENCE`` above.
                    confidence=min(_MAX_EXTRACTED_CONFIDENCE, _clamp_score(row.get("confidence"))),
                    expected_valid_days=_parse_valid_days(row.get("expected_valid_days")),
                    # Strict bool — only a real JSON `true` counts as a correction
                    # (a truthy string must not silently trip UPDATE recall).
                    is_correction=row.get("is_correction") is True,
                )
            )
    return out


def _redact_memory(item: MemoryItem) -> MemoryItem:
    """Capability Uplift Sprint #2 (Mini-ADR U-3 Layer B) — replace
    poisoned / drifted content with a ``[BLOCKED:<category>]`` placeholder.

    ``drift=True`` wins over pattern matches: drifted content is no
    longer trusted regardless of what it now contains, so the agent
    is told the row was tampered with rather than which pattern fired.
    Pattern matches return the matched category (``injection`` / ``c2``
    / ``exfil`` / etc) so the agent has minimal signal to potentially
    re-ask the user; the ``pattern_id`` itself stays in the audit row
    (oracle defense — see ``docs/runbooks/threat-scanner-tuning.md`` § 4).

    Also blanks ``embedding`` on every returned item (poisoned or not) —
    ``recalled_memories`` only feeds prompt injection / the workspace
    projection, neither of which reads the vector, so shipping it over
    SSE and persisting it to ``run_event`` is pure waste.
    """
    if item.drift:
        record_memory_drift()
        record_memory_redacted()
        return item.model_copy(update={"content": "[BLOCKED:drift_tampered]", "embedding": ()})
    findings = scan_for_threats(item.content, scope="strict")
    if findings:
        record_memory_redacted()
        return item.model_copy(
            update={"content": f"[BLOCKED:{findings[0].category}]", "embedding": ()}
        )
    return item.model_copy(update={"embedding": ()})


async def _resolve_memory_recall_mode(
    *,
    tenant_id: Any,
    tenant_config_store: TenantConfigStore | None,
) -> MemoryRecallMode:
    """Read ``tenant_config.memory_recall_mode``; default ``hybrid``.

    No store wired or no tenant_config row → ``hybrid`` (platform-wide
    default, Mini-ADR U-5). Mirrors the trigger fire-scan-mode resolver
    in ``control_plane.trigger_firing``.
    """
    if tenant_config_store is None:
        return "hybrid"
    record = await tenant_config_store.get(tenant_id=tenant_id)
    if record is None:
        return "hybrid"
    return record.memory_recall_mode


async def _rerank_memories(
    *,
    reranker: Reranker,
    query: str,
    candidates: list[MemoryItem],
    top_k: int,
    tenant_id: UUID,
    token: CancellationToken,
) -> list[MemoryItem]:
    """Stream CM-4 — reorder recall candidates by cross-encoder relevance.

    Best-effort: the reranker's own implementations already degrade to the
    fused order (LLM parse failure / no credential); this wraps the call so
    any unexpected error still degrades to the RRF order (``candidates``
    truncated to ``top_k``) rather than dropping recall entirely. Cancellation
    propagates. Operates on raw content — redaction happens on the final set.
    """
    try:
        order = await token.run_cancellable(
            reranker.rerank(
                query=query,
                documents=[m.content for m in candidates],
                top_k=top_k,
                tenant_id=tenant_id,
            )
        )
    except RunCancelledError:
        raise
    except Exception:
        logger.warning("memory.rerank_failed — using RRF order", exc_info=True)
        record_memory_rerank(outcome="degraded")
        return candidates[:top_k]
    reranked = [candidates[i] for i in order if 0 <= i < len(candidates)]
    if not reranked:
        record_memory_rerank(outcome="degraded")
        return candidates[:top_k]
    record_memory_rerank(outcome="reranked")
    return reranked[:top_k]


def _mmr_memories(
    *,
    query_embedding: Sequence[float],
    candidates: list[MemoryItem],
    top_k: int,
) -> list[MemoryItem]:
    """Stream CM-6 — MMR diversity selection, the recall pipeline's last stage.

    Selects the final ``top_k`` from the (rerank- or RRF-ordered) wide
    candidate set, trading relevance against redundancy (λ=0.7). Best-effort
    mirror of the rerank contract (Mini-ADR CM-G6): any failure — or a
    selection thinned to nothing by dimension mismatches — degrades to the
    input order truncated to ``top_k``, never dropping recall entirely.
    """
    try:
        selected = mmr_select(
            query_embedding=query_embedding,
            candidates=[(m, m.embedding) for m in candidates],
            k=top_k,
        )
    except Exception:
        logger.warning("memory.mmr_failed — using input order", exc_info=True)
        record_memory_mmr(outcome="degraded")
        return candidates[:top_k]
    if not selected:
        record_memory_mmr(outcome="degraded")
        return candidates[:top_k]
    record_memory_mmr(outcome="applied")
    return selected


# Stream Memory-Enhance (M-3) — read-time verification.
_VERIFY_SYSTEM = (
    "You are a memory relevance filter. Given the user's CURRENT request and a "
    "numbered list of candidate memories recalled from past sessions, return "
    "ONLY the indices of memories that are RELEVANT to the request and not "
    "stale or self-contradictory. Drop anything clearly irrelevant, outdated, "
    "or contradictory. A memory the agent could plausibly use should be kept — "
    "when unsure, keep it. Respond with ONLY a JSON object, no prose and no "
    'code fences:\n{"keep": [<indices to keep>]}'
)


def parse_verify_kept(text: str, count: int) -> set[int] | None:
    """Parse the verifier reply into the set of kept indices, or ``None`` when
    the reply is unparseable (the caller then keeps all — fail-open).

    An empty ``{"keep": []}`` is a *valid* "drop everything" verdict and returns
    ``set()`` (distinct from ``None``). Out-of-range indices are ignored.
    """
    raw = _extract_json_object(text)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        rows = data["keep"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    if not isinstance(rows, list):
        return None
    kept: set[int] = set()
    for value in rows:
        try:
            idx = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < count:
            kept.add(idx)
    return kept


async def _verify_memories(
    *,
    llm_caller: LLMCaller,
    query: str,
    candidates: list[MemoryItem],
    token: CancellationToken,
) -> list[MemoryItem]:
    """Stream Memory-Enhance (M-3) — drop recall candidates the verifier judges
    irrelevant / stale for the current ``query``.

    One batched LLM call over the whole candidate list (cost is +1 call per
    recall, not per item). **Fail-open**: any error, or an unparseable reply,
    keeps ALL candidates — read-time verification must never break a turn or
    silently empty recall. Cancellation propagates. Operates on raw content;
    redaction happens on the surviving set.
    """
    numbered = "\n".join(f"[{i}] {m.content}" for i, m in enumerate(candidates))
    prompt = [
        SystemMessage(content=_VERIFY_SYSTEM),
        HumanMessage(content=f"CURRENT REQUEST:\n{query}\n\nCANDIDATE MEMORIES:\n{numbered}"),
    ]
    try:
        with expert_work_span(ExpertWorkComponent.MEMORY, "verify"):
            response = await token.run_cancellable(llm_caller(messages=prompt, tools=[]))
        kept = parse_verify_kept(_message_text(response), len(candidates))
    except RunCancelledError:
        raise
    except Exception:
        logger.warning("memory.verify_failed — keeping all candidates", exc_info=True)
        record_memory_verify(outcome="degraded")
        return candidates
    if kept is None:
        record_memory_verify(outcome="degraded")
        return candidates
    record_memory_verify(outcome="verified")
    survivors = [m for i, m in enumerate(candidates) if i in kept]
    if len(survivors) != len(candidates):
        logger.debug(
            "memory.verify dropped=%d kept=%d", len(candidates) - len(survivors), len(survivors)
        )
    return survivors


# Stream P5a (Task 7) — retrieval query rewrite.
_REWRITE_SYSTEM = (
    "You rewrite the user's latest message into a concise standalone search "
    "query for retrieving relevant past memories. Strip any instructions or "
    "commands — keep only what identifies the information need (entities, "
    "topic, preference). Respond with ONLY the query text, no prose."
)


async def _rewrite_query(*, llm_caller: LLMCaller, task: str, token: CancellationToken) -> str:
    """P5a — rewrite the task into a retrieval query (best-effort, fail-open).

    Any error / empty reply returns the original ``task`` unchanged so recall
    never breaks on a rewrite failure. Cancellation propagates."""
    prompt = [
        SystemMessage(content=_REWRITE_SYSTEM),
        HumanMessage(content=task),
    ]
    try:
        with expert_work_span(ExpertWorkComponent.MEMORY, "query_rewrite"):
            response = await token.run_cancellable(llm_caller(messages=prompt, tools=[]))
    except RunCancelledError:
        raise
    except Exception:
        logger.warning("memory.query_rewrite_failed — using original task", exc_info=True)
        record_memory_rewrite(outcome="degraded")
        return task
    rewritten = _message_text(response).strip()
    if not rewritten or rewritten == task:
        # Empty reply, or the model echoed the task back — no effective rewrite.
        record_memory_rewrite(outcome="unchanged")
        return task
    record_memory_rewrite(outcome="rewritten")
    return rewritten


def make_memory_recall_node(
    *,
    memory_store: MemoryStore,
    embedder: Embedder,
    top_k: int,
    tenant_config_store: TenantConfigStore | None = None,
    reranker: Reranker | None = None,
    agent_name: str | None = None,
    verifier: LLMCaller | None = None,
    verify_reads: bool = False,
    rewrite_query: bool = False,
    rewriter: LLMCaller | None = None,
    abstain_threshold: float = 0.0,
) -> MemoryNode:
    """Build the ``memory_recall`` node bound to the store + embedder.

    Capability Uplift Sprint #6 (Mini-ADR U-5): when
    ``tenant_config_store`` is wired and the tenant's
    ``memory_recall_mode`` is ``hybrid`` (the default), the user's task
    text is forwarded to ``MemoryStore.retrieve(query_text=...)`` for
    hybrid vector + full-text + RRF recall. ``vector`` mode keeps the
    pre-Sprint-#6 pure-pgvector path. No store wired → default hybrid
    (so test fixtures that omit the store still get the improved recall).

    Stream CM-4/CM-6 — the recall pipeline: optional query rewrite (Stream
    P5a, when ``rewrite_query`` + ``rewriter``) → wide recall
    (``max(top_k, _MEMORY_RECALL_WIDE_LIMIT)``, always — Mini-ADR CM-G5)
    → cross-encoder rerank when ``reranker`` is wired (full reorder, no
    cut, so the diversity stage still sees the whole pool) → MMR selects
    the final ``top_k`` (Mini-ADR CM-G1/G4) → read-time verification
    (Stream Memory-Enhance M-3, when ``verify_reads`` + ``verifier``) →
    redaction.

    Stream Memory-Enhance (M-3) — ``verify_reads`` adds one batched LLM call
    (via ``verifier``) that drops candidates judged irrelevant / stale for the
    current request before injection. Fail-open: a verifier error keeps all
    candidates so recall never empties on a transient failure.

    Stream P5a (Task 7) — ``rewrite_query`` adds one LLM call (via
    ``rewriter``) that rewrites the task into a concise standalone search
    query before embedding — guards against long or instruction-laden
    messages polluting the retrieval vector / hybrid full-text query.
    Fail-open: a rewrite error or empty reply keeps the original task, so
    disabled (or unwired) leaves ``search_text is task`` unchanged.

    Stream P5a (Task 8) — ``abstain_threshold`` gates recall on the best
    candidate's cosine similarity right after ``retrieve()``, before rerank /
    MMR / verify: below the threshold, the node bumps ``record_memory_abstain``
    and returns no memories rather than injecting a weak match. Default
    ``0.0`` never abstains (observe-only until a tenant opts in).
    """

    async def memory_recall_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        token = cancellation_token(config)
        token.raise_if_cancelled()

        tenant_id = configurable_uuid(config, "tenant_id")
        user_id = configurable_uuid(config, "user_id")
        if tenant_id is None or user_id is None:
            return {}
        task = _last_human_text(list(state["messages"]))
        if not task:
            return {}
        # 一期 Task 1 —— 整段召回是入口链上最大的一块,父 span 让瀑布图
        # 能先看到总量再下钻。放在两处 no-op 早退之后:没有 tenant/user
        # 或没有 task 的 run 根本没做召回,发一个 0ms 的空 span 只是噪音。
        with expert_work_span(ExpertWorkComponent.MEMORY, "recall"):
            with expert_work_span(ExpertWorkComponent.MEMORY, "resolve_mode"):
                mode = await _resolve_memory_recall_mode(
                    tenant_id=tenant_id, tenant_config_store=tenant_config_store
                )
            recall_limit = max(top_k, _MEMORY_RECALL_WIDE_LIMIT)
            try:
                # Stream P5a (Task 7) — rewrite into a retrieval query before
                # embedding, stripping instructions / trimming long messages so
                # they don't pollute the vector. Best-effort inside
                # ``_rewrite_query`` itself; disabled (or no ``rewriter`` wired)
                # keeps ``search_text is task`` — behaviour unchanged.
                search_text = task
                if rewrite_query and rewriter is not None:
                    search_text = await _rewrite_query(llm_caller=rewriter, task=task, token=token)
                with expert_work_span(ExpertWorkComponent.MEMORY, "embed"):
                    vectors = await token.run_cancellable(
                        embedder.embed([search_text], tenant_id=tenant_id)
                    )
                with expert_work_span(ExpertWorkComponent.MEMORY, "retrieve"):
                    memories = await memory_store.retrieve(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        query_embedding=vectors[0],
                        query_text=search_text if mode == "hybrid" else None,
                        # Stream Agent-Templates (M1-5c) — scope episodic recall to this
                        # agent; shared facts (agent_name NULL) are always included.
                        agent_name=agent_name,
                        limit=recall_limit,
                    )
                # Stream P5a (Task 8) — abstention threshold gate, placed right
                # after retrieve() and before rerank/MMR/verify so an irrelevant
                # recall skips their cost entirely. ``_reconcile_cosine`` already
                # returns a similarity in [-1, 1] (not a distance) — higher is
                # more relevant. Gated on ``abstain_threshold > 0.0`` so the
                # default (0.0) never abstains.
                if memories and abstain_threshold > 0.0:
                    top_sim = max(
                        (_reconcile_cosine(vectors[0], m.embedding) for m in memories),
                        default=0.0,
                    )
                    if top_sim < abstain_threshold:
                        record_memory_abstain()
                        # An abstained recall injects nothing — it is a retrieval
                        # miss for observability. The normal record_memory_retrieval
                        # call below is skipped by this early return, so bump it here
                        # or the abstain branch silently drops off the retrieval
                        # hit/miss dashboards.
                        record_memory_retrieval(mode=mode, result="miss")
                        logger.info(
                            "memory.abstain top_sim=%.3f < %.3f", top_sim, abstain_threshold
                        )
                        return {}
                if reranker is not None and memories:
                    # Full reorder (top_k = pool size) — the MMR stage below
                    # makes the final cut, so diversity can still swap in
                    # candidates the relevance cut would have dropped.
                    with expert_work_span(ExpertWorkComponent.MEMORY, "rerank"):
                        memories = await _rerank_memories(
                            reranker=reranker,
                            query=task,
                            candidates=memories,
                            top_k=len(memories),
                            tenant_id=tenant_id,
                            token=token,
                        )
                if memories:
                    memories = _mmr_memories(
                        query_embedding=vectors[0],
                        candidates=memories,
                        top_k=top_k,
                    )
                # Stream Memory-Enhance (M-3) — read-time verification on the final
                # set (raw content, pre-redaction). Fail-open inside its own helper,
                # so a verifier error keeps all candidates rather than emptying
                # recall. Runs after MMR so it only judges the items that would
                # actually be injected (one batched call over the final top_k).
                if verify_reads and verifier is not None and memories:
                    memories = await _verify_memories(
                        llm_caller=verifier,
                        query=task,
                        candidates=memories,
                        token=token,
                    )
            except RunCancelledError:
                raise
            except Exception:
                logger.warning(
                    "memory.recall_failed — continuing without memories", exc_info=True
                )
                record_memory_retrieval(mode=mode, result="miss")
                return {}
            record_memory_retrieval(mode=mode, result="hit" if memories else "miss")
            if memories:
                with expert_work_span(ExpertWorkComponent.MEMORY, "bump_access"):
                    try:
                        await memory_store.bump_access(
                            tenant_id=tenant_id,
                            user_id=user_id,
                            ids=[m.id for m in memories],
                        )
                    except RunCancelledError:
                        raise
                    except Exception:
                        logger.warning("memory.bump_access_failed", exc_info=True)
            redacted = [_redact_memory(m) for m in memories]
            logger.info("memory.recall count=%d mode=%s", len(redacted), mode)
            return {"recalled_memories": redacted}

    return memory_recall_node


#: Stream CM-7 (Mini-ADR CM-H4) — neighbours below this cosine similarity
#: are not "the same memory" candidates; the new item is ADDed without an
#: LLM decision (the cheap majority path).
_RECONCILE_SIM_THRESHOLD = 0.80
_RECONCILE_NEIGHBOR_LIMIT = 3

_RECONCILE_SYSTEM = (
    "You reconcile newly extracted memories against similar existing "
    "ones. For each candidate decide exactly one operation:\n"
    '- "ADD": genuinely new information\n'
    '- "UPDATE": supersedes or corrects ONE existing memory (set '
    "target_id; the existing memory is rewritten to the candidate)\n"
    '- "DELETE": the candidate retracts ONE existing memory (set '
    "target_id; the existing memory is removed and the candidate "
    "itself is NOT stored)\n"
    '- "NOOP": duplicate of an existing memory; store nothing\n'
    'A candidate with "is_correction": true is the user explicitly '
    "correcting earlier info — strongly prefer UPDATE (or DELETE) against "
    "the matching existing memory over ADD, so the old value is superseded "
    "rather than left to coexist.\n"
    "Respond with ONLY a JSON object, no prose and no code fences:\n"
    '{"ops": [{"index": <candidate index>, "op": "ADD", '
    '"target_id": "<existing id, only for UPDATE/DELETE>"}]}'
)


def _reconcile_cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def parse_reconcile_ops(text: str) -> dict[int, tuple[str, str | None]]:
    """Parse the reconcile LLM reply into ``{index: (op, target_id)}``.

    Tolerant — malformed entries are simply absent, and the caller
    treats an absent decision as a degraded direct ADD (CM-H4: never
    lose a memory over a parse failure).
    """
    raw = _extract_json_object(text)
    if raw is None:
        return {}
    try:
        rows = json.loads(raw)["ops"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}
    out: dict[int, tuple[str, str | None]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        op = str(row.get("op", "")).strip().upper()
        index = row.get("index")
        if op not in ("ADD", "UPDATE", "DELETE", "NOOP") or not isinstance(index, int):
            continue
        target = row.get("target_id")
        out[index] = (op, str(target) if target is not None else None)
    return out


async def _reconcile_and_apply(
    items: list[MemoryItem],
    *,
    corrections: Sequence[bool],
    memory_store: MemoryStore,
    llm_caller: LLMCaller,
    token: CancellationToken,
    log_label: str,
) -> list[MemoryItem]:
    """Stream CM-7 (Mini-ADR CM-H4) — Mem0-style extract→update.

    Returns the items to write directly; UPDATE / DELETE are applied
    in place against the store. Best-effort throughout: every failure
    path degrades to keeping the candidate in the direct-write list
    (never lose a memory), and only cancellation propagates.

    P5b-2b ⑧ — ``corrections[i]`` aligns 1:1 with ``items[i]`` (the LLM's
    ``is_correction`` extraction flag); it rides along into the reconcile
    payload as ``"is_correction"`` per candidate so the ops LLM can bias
    UPDATE/DELETE over ADD on an explicit user correction.
    """
    direct: list[MemoryItem] = []
    pending: list[tuple[MemoryItem, list[MemoryItem], bool]] = []
    for idx, item in enumerate(items):
        is_corr = corrections[idx] if idx < len(corrections) else False
        try:
            neighbors = await token.run_cancellable(
                memory_store.retrieve(
                    tenant_id=item.tenant_id,
                    user_id=item.user_id,
                    query_embedding=item.embedding,
                    limit=_RECONCILE_NEIGHBOR_LIMIT,
                )
            )
        except RunCancelledError:
            raise
        except Exception:
            logger.warning("%s_reconcile_recall_failed — direct add", log_label, exc_info=True)
            record_memory_reconcile(op="degraded")
            direct.append(item)
            continue
        similar = [
            n
            for n in neighbors
            if _reconcile_cosine(item.embedding, n.embedding) >= _RECONCILE_SIM_THRESHOLD
        ]
        if similar:
            pending.append((item, similar, is_corr))
        else:
            record_memory_reconcile(op="add")
            direct.append(item)
    if not pending:
        return direct

    payload = {
        "candidates": [
            {
                "index": idx,
                "kind": item.kind,
                "content": item.content,
                "is_correction": is_corr,
                "existing": [{"id": str(n.id), "content": n.content} for n in similar],
            }
            for idx, (item, similar, is_corr) in enumerate(pending)
        ]
    }
    ops: dict[int, tuple[str, str | None]] = {}
    try:
        with expert_work_span(ExpertWorkComponent.MEMORY, "reconcile"):
            reply = await token.run_cancellable(
                llm_caller(
                    messages=[
                        SystemMessage(content=_RECONCILE_SYSTEM),
                        HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
                    ],
                    tools=[],
                )
            )
        ops = parse_reconcile_ops(_message_text(reply))
    except RunCancelledError:
        raise
    except Exception:
        logger.warning("%s_reconcile_llm_failed — direct add all", log_label, exc_info=True)

    for idx, (item, similar, _is_corr) in enumerate(pending):
        op, target_raw = ops.get(idx, ("", None))
        valid = {str(n.id): n.id for n in similar}
        target = valid.get(target_raw) if target_raw is not None else None
        if op == "NOOP":
            record_memory_reconcile(op="noop")
        elif op == "UPDATE" and target is not None:
            updated = await _apply_update(item, target, memory_store=memory_store, token=token)
            record_memory_reconcile(op="update" if updated else "degraded")
            if not updated:
                direct.append(item)
        elif op == "DELETE" and target is not None:
            deleted = await _apply_delete(item, target, memory_store=memory_store, token=token)
            # The candidate is the retraction event — not stored either way.
            record_memory_reconcile(op="delete" if deleted else "degraded")
        elif op == "ADD":
            record_memory_reconcile(op="add")
            direct.append(item)
        else:
            # Missing / malformed decision — never lose the memory.
            record_memory_reconcile(op="degraded")
            direct.append(item)
    return direct


async def _apply_update(
    item: MemoryItem, target: UUID, *, memory_store: MemoryStore, token: CancellationToken
) -> bool:
    """Stream P5b — append-only UPDATE: supersede ``target`` with ``item``
    (close old row + open new versioned row) instead of overwriting in place."""
    try:
        written = await token.run_cancellable(
            memory_store.supersede(
                tenant_id=item.tenant_id,
                user_id=item.user_id,
                old_id=target,
                new_item=item,
            )
        )
    except RunCancelledError:
        raise
    except Exception:
        logger.warning("memory.reconcile_update_failed id=%s", target, exc_info=True)
        return False
    return written is not None


async def _apply_delete(
    item: MemoryItem, target: UUID, *, memory_store: MemoryStore, token: CancellationToken
) -> bool:
    """Stream P5b — retraction: expire ``target`` (world no longer true, no
    successor) instead of soft-deleting it (which means user-forget)."""
    try:
        return await token.run_cancellable(
            memory_store.expire(tenant_id=item.tenant_id, user_id=item.user_id, memory_id=target)
        )
    except RunCancelledError:
        raise
    except Exception:
        logger.warning("memory.reconcile_delete_failed id=%s", target, exc_info=True)
        return False


async def flush_messages_to_memory(
    messages: Sequence[BaseMessage],
    *,
    memory_store: MemoryStore,
    embedder: Embedder,
    llm_caller: LLMCaller,
    tenant_id: UUID,
    user_id: UUID,
    thread_id: UUID | None,
    run_id: str | None = None,
    token: CancellationToken,
    dlq: MemoryWritebackDLQ | None = None,
    log_label: str = "memory.writeback",
    reconcile: bool = False,
    agent_name: str | None = None,
    write_min_importance: float = 0.0,
) -> int:
    """Extract durable memories from ``messages``, embed, and persist them.

    The shared extraction core behind both the run-end ``memory_writeback``
    node and the Stream CM-3 pre-compaction flush. Makes one LLM extraction
    call, embeds the produced pairs, and writes :class:`MemoryItem`\\s tagged
    with ``source_thread_id``. Returns the number of memories written
    (``0`` on empty extraction / blocked content / any handled failure).

    Best-effort, mirroring the original write-back contract:

    * ``RunCancelledError`` propagates (cancellation is never swallowed).
    * ``MemoryInjectionBlockedError`` (strict scanner) → log + return 0;
      the store has already emitted the block audit.
    * Any other failure after a non-empty extraction → enqueue the pairs
      to ``dlq`` (Stream K.K7) when wired, else log-and-drop; return 0.

    ``log_label`` distinguishes the two call sites in logs
    (``memory.writeback`` vs ``memory.precompaction_flush``) while keeping
    the run-end node's log strings byte-identical.

    ``reconcile`` (Stream CM-7, Mini-ADR CM-H3/H4) — when set, extracted
    items are reconciled against similar existing memories
    (ADD / UPDATE / DELETE / NOOP) before persisting instead of written
    blindly. Only the run-end write-back sets it; the CM-3 pre-compaction
    flush stays a direct write (latency-sensitive, inside a turn).

    ``run_id`` (P5b provenance) is stamped on every written item; the
    DLQ-retry path now carries run_id (P5b-2a) too — the enqueue call
    below passes ``source_run_id=run_id``, so the control-plane worker's
    reconstructed items keep the original run's provenance.
    """
    prompt = [
        SystemMessage(content=_EXTRACT_SYSTEM),
        HumanMessage(content=_render_trajectory(list(messages))),
    ]
    extracted: list[ExtractedMemory] = []
    try:
        with expert_work_span(ExpertWorkComponent.MEMORY, "extract"):
            response = await token.run_cancellable(llm_caller(messages=prompt, tools=[]))
        extracted = parse_extracted_memories(_message_text(response))
        # Stream Memory-Enhance (M-2) — write-filter: drop low-value memories
        # before embedding (saves embed cost on dropped items). Applied here,
        # not in the store, so the threshold is a per-agent manifest knob.
        if write_min_importance > 0.0:
            kept = [m for m in extracted if m.importance >= write_min_importance]
            if len(kept) != len(extracted):
                logger.debug(
                    "%s_write_filter dropped=%d kept=%d min_importance=%.2f",
                    log_label,
                    len(extracted) - len(kept),
                    len(kept),
                    write_min_importance,
                )
            extracted = kept
        if not extracted:
            return 0
        vectors = await token.run_cancellable(
            embedder.embed([m.content for m in extracted], tenant_id=tenant_id)
        )
        items = [
            MemoryItem(
                id=uuid4(),
                tenant_id=tenant_id,
                user_id=user_id,
                kind=mem.kind,
                # Stream Agent-Templates (M1-5c) — tag episodic with the owning
                # agent (per-agent isolation); facts stay shared (agent_name None).
                agent_name=agent_name if mem.kind == "episodic" else None,
                content=mem.content,
                embedding=vector,
                # Stream Memory-Enhance (M-2) — carry the extraction scores.
                importance=mem.importance,
                confidence=mem.confidence,
                source_thread_id=str(thread_id) if thread_id is not None else None,
                source_run_id=run_id,
                # P5b-2b ⑦ — persist the predicted window so the consolidator
                # due-scan can find this fact when it comes due.
                expected_valid_days=mem.expected_valid_days,
            )
            for mem, vector in zip(extracted, vectors, strict=True)
        ]
        if reconcile:
            items = await _reconcile_and_apply(
                items,
                corrections=[m.is_correction for m in extracted],
                memory_store=memory_store,
                llm_caller=llm_caller,
                token=token,
                log_label=log_label,
            )
        if items:
            await memory_store.write(items)
    except RunCancelledError:
        raise
    except MemoryInjectionBlockedError as exc:
        # Capability Uplift Sprint #2 — LLM extracted something the strict
        # scanner caught. The content is deterministic, so retrying will
        # fail identically; drop the batch + log. The store has emitted
        # MEMORY_INJECTION_BLOCKED audit(s) by the time we land here. Run
        # is not affected.
        logger.warning(
            "%s_blocked count=%d — content rejected by strict scanner",
            log_label,
            len(exc.blocked),
        )
        return 0
    except Exception as exc:
        # Stream K.K7 — don't lose the work the LLM already did. If the
        # extraction produced pairs, hand them to the DLQ for a retry
        # sweep; otherwise (parse / cancel-shaped failures below the
        # extraction line) log and drop as before.
        if dlq is not None and extracted:
            try:
                # DLQ enqueue takes ``Sequence[tuple[str, str]]``; widen the
                # Literal kind so mypy accepts the tuple element.
                await dlq.enqueue(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    source_thread_id=str(thread_id) if thread_id is not None else None,
                    source_run_id=run_id,
                    extracted=[(str(m.kind), m.content) for m in extracted],
                    error=f"{type(exc).__name__}: {exc}",
                )
                logger.warning(
                    "%s_failed — enqueued %d pair(s) to DLQ",
                    log_label,
                    len(extracted),
                    exc_info=True,
                )
            except Exception:
                logger.error("%s_dlq_enqueue_failed — pairs lost", log_label, exc_info=True)
        else:
            logger.warning("%s_failed — run unaffected", log_label, exc_info=True)
        return 0
    logger.info("%s count=%d", log_label, len(items))
    return len(items)


#: Stream CM-3 — a config-bound pre-compaction flush callback. Awaited by
#: ``agent_node`` with the middle slice the compressor is about to discard,
#: the run's ``config`` (for tenant/user/thread scope) and its
#: ``CancellationToken``; returns the number of memories written.
PreCompactionFlush = Callable[
    [Sequence[BaseMessage], RunnableConfig, CancellationToken], Awaitable[int]
]


def make_pre_compaction_flush(
    *,
    memory_store: MemoryStore,
    embedder: Embedder,
    llm_caller: LLMCaller,
    dlq: MemoryWritebackDLQ | None = None,
    agent_name: str | None = None,
    write_min_importance: float = 0.0,
) -> PreCompactionFlush:
    """Build the Stream CM-3 pre-compaction flush callback.

    Resolves the run's per-user scope from ``config`` (no-op returning 0
    when absent — long-term memory is per-user) and hands the about-to-be-
    discarded middle slice to :func:`flush_messages_to_memory`, tagged as a
    ``memory.precompaction_flush`` so logs distinguish it from the run-end
    write-back. Shares the same store / embedder / DLQ as the write-back
    node, so flushed memories are indistinguishable downstream.
    """

    async def flush(
        messages: Sequence[BaseMessage],
        config: RunnableConfig,
        token: CancellationToken,
    ) -> int:
        tenant_id = configurable_uuid(config, "tenant_id")
        user_id = configurable_uuid(config, "user_id")
        if tenant_id is None or user_id is None:
            return 0
        thread_id = configurable_uuid(config, "thread_id")
        run_id = current_run_id(config)
        return await flush_messages_to_memory(
            messages,
            memory_store=memory_store,
            embedder=embedder,
            llm_caller=llm_caller,
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=thread_id,
            run_id=run_id,
            token=token,
            dlq=dlq,
            log_label="memory.precompaction_flush",
            agent_name=agent_name,
            write_min_importance=write_min_importance,
        )

    return flush


def make_memory_writeback_node(
    *,
    memory_store: MemoryStore,
    embedder: Embedder,
    llm_caller: LLMCaller,
    dlq: MemoryWritebackDLQ | None = None,
    reconcile: bool = False,
    agent_name: str | None = None,
    write_min_importance: float = 0.0,
) -> MemoryNode:
    """Build the ``memory_writeback`` node bound to the store + embedder.

    Stream K.K7 — when ``dlq`` is supplied, failures after the LLM
    extraction successfully produced memories enqueue the extracted
    pairs so a retry worker can re-do the embed + write later. Without
    a DLQ the previous best-effort log-and-drop behaviour is kept so
    unit tests that don't wire a queue still work.

    Stream CM-3 — the extraction core now lives in the shared
    :func:`flush_messages_to_memory`; this node is the thin run-end
    adapter (resolve config scope → flush the full trajectory).

    Stream CM-7 — ``reconcile`` forwards to the flush so run-end writes
    go through the Mem0-style ADD / UPDATE / DELETE / NOOP decision.
    """

    async def memory_writeback_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        token = cancellation_token(config)
        token.raise_if_cancelled()

        tenant_id = configurable_uuid(config, "tenant_id")
        user_id = configurable_uuid(config, "user_id")
        if tenant_id is None or user_id is None:
            return {}
        thread_id = configurable_uuid(config, "thread_id")
        run_id = current_run_id(config)

        await flush_messages_to_memory(
            list(state["messages"]),
            memory_store=memory_store,
            embedder=embedder,
            llm_caller=llm_caller,
            tenant_id=tenant_id,
            user_id=user_id,
            thread_id=thread_id,
            run_id=run_id,
            token=token,
            dlq=dlq,
            log_label="memory.writeback",
            reconcile=reconcile,
            agent_name=agent_name,
            write_min_importance=write_min_importance,
        )
        return {}

    return memory_writeback_node
