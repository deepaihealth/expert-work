"""ReAct graph builder — Stream E.6 + E.12.5.

Builds a LangGraph :class:`StateGraph` that implements single-agent
ReAct over :class:`orchestrator.state.AgentState`. The graph has two
nodes wired by a single conditional edge:

::

    START → agent ↔ tools → END
              │
              └─ END (when LLM stops issuing tool_calls or max_steps hit)

The **agent** node delegates the LLM call to an injected
:class:`LLMCaller` (E.11 :class:`LLMRouter` in prod; deterministic fake
in tests) and bumps ``step_count`` by one before returning. Entering with
``step_count >= max_steps`` does NOT fail the run: it degrades gracefully to
one final tool-less "wrap up with what you have" turn (hermes-agent #7915),
preserving work the run already produced. (:class:`MaxStepsExceededError`
remains a defensive safety net consumed by the SSE runner + child-run path.)

The **tools** node walks the most-recent ``AIMessage.tool_calls``,
dispatches each through :class:`ToolRegistry`, and appends one
``ToolMessage`` per call to the messages list. Any uncaught tool
exception (including ``ToolNotFoundError`` for unknown names) is
wrapped into ``ToolMessage(content="[tool error] ...")`` rather than
re-raised, per Mini-ADR E-12 — the LLM sees the error as a tool result
and reasons about retry / different args / final answer.

Stream E.12.5 wires the middleware chain into both nodes. Anchor calls
(only when the corresponding chain is passed; ``None`` → no-op):

- ``before_llm_call`` chain → ``agent_node`` invokes before the LLM
  call. ``ctx.payload`` carries ``messages`` / ``tools`` / ``tenant_id``;
  middlewares (E.3 dynamic_context, E.5 pii_redact) may rewrite the
  messages, and E.13 ``cache_lookup`` may set ``llm_cache_hit`` to a
  cached :class:`AIMessage` — when present, ``agent_node`` skips the
  LLM call entirely.
- ``around_llm_call`` chain → handed to :class:`LLMRouter` which
  invokes the chain **per provider** (Mini-ADR E-13), so each
  fallback attempt gets its own E.4 breaker + E.5 langfuse span.
- ``after_llm_call`` chain → ``agent_node`` invokes after the LLM
  returns (or after a cache hit). ``ctx.payload`` carries ``response``
  (mutable) + ``messages`` (running history) + ``prompt_messages``
  (the exact prompt, for E.13 cache-key derivation) + ``tenant_id`` +
  ``cache_hit`` (bool — E.13 ``cache_store`` skips storing a turn that
  was itself served from cache). Middlewares (E.10.5 loop_detection)
  may rewrite the response or append reminder messages.
- ``before_tool_dispatch`` chain → ``tools_node`` invokes per
  ``tool_call``. ``ctx.payload`` carries ``tool_name`` + ``tool_args``;
  a pre-dispatch middleware may raise to block the dispatch.

Stream RT-1 PR-3 (RT-ADR-4): a terminal turn under a Tier3
``output_schema`` may issue ONE schema-enforced resend when the free
candidate does not validate. That resend gets its own before/after
anchor pass with the :class:`StructuredOutputSpec` instance in
``payload["output_schema"]`` (design § 7.4 — the E.13 schema
fingerprint), so on such a turn each anchor fires once per real LLM
call: primary + resend.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, cast
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from opentelemetry.trace import Status, StatusCode

from expert_work.common.dlp import scan_and_redact
from expert_work.common.observability import (
    ExpertWorkComponent,
    expert_work_counter,
    expert_work_gauge,
    expert_work_histogram,
    expert_work_span,
)
from expert_work.common.output_screen import REFUSAL_TEXT, screen_output
from expert_work.common.spotlight import spotlight_untrusted
from expert_work.common.uplift_metrics import record_memory_inject_mode
from expert_work.protocol import (
    AuditAction,
    AuditEntry,
    AuditResult,
    MemoryItem,
    Plan,
    StructuredOutputSpec,
)
from expert_work.runtime.audit.logger import AuditLogger
from expert_work.runtime.audit.redactor import DEFAULT_PATTERNS, PII_PATTERNS, DefaultSecretRedactor
from expert_work.runtime.cancellation import CancellationToken, RunCancelledError
from expert_work.runtime.middleware import (
    LLMOutputValidationError,
    MiddlewareChain,
    MiddlewareContext,
)
from expert_work.runtime.tokens import CharTokenEstimator, TokenEstimator
from orchestrator.context import (
    CompactionStats,
    ContextCompressor,
    OnCompacted,
    PreCompactionHook,
    ProjectionResult,
    ToolResultPruner,
    WorkingWindow,
    WorkspaceFileWriter,
    WorkspaceProjector,
)
from orchestrator.graph_builder._approval import (
    ApprovalTarget,
    apply_resume_decision,
    build_approval_request,
    find_approval_target,
)
from orchestrator.graph_builder._config import (
    audit_logger_from_config,
    cancellation_token,
    compaction_sink_from_config,
    token_sink_from_config,
)
from orchestrator.graph_builder.memory import MemoryNode, PreCompactionFlush
from orchestrator.graph_builder.planner import PlannerNode, render_plan
from orchestrator.graph_builder.reflect import ReflectNode
from orchestrator.graph_builder.streaming_redact import make_token_sink
from orchestrator.llm import LLMCaller
from orchestrator.llm.structured_output import correction_message, validate_structured_output
from orchestrator.output_judge import ActionJudge, OutputJudge
from orchestrator.state import AgentState
from orchestrator.tools._budget import DELEGATION_GATE_KEY
from orchestrator.tools._guards import (
    GUARD_SINK_KEY,
    TOKEN_BUDGET_KEY,
    TokenBudget,
    build_guard_frame,
    emit_guard_frame,
    usage_total,
)
from orchestrator.tools._worker_events import WORKER_EVENT_SINK_KEY
from orchestrator.tools.error_classifier import (
    ClassifiedToolError,
    classified_invalid_arguments,
    classified_mutation_not_landed,
    classify_tool_error,
    render_recovery_advisory,
)
from orchestrator.tools.find_tools import promotion_events
from orchestrator.tools.mcp import parse_mcp_tool_name
from orchestrator.tools.mutation_classifier import classify as classify_mutation
from orchestrator.tools.overflow import (
    EXEMPT_TOOLS,
    EXTERNALIZE_MIN_CHARS,
    PERSIST_MIN_CHARS,
    TOOL_RESULT_PATH_ARTIFACT_KEY,
    clamp_overflow,
    fallback_truncate,
    make_preview,
    overflow_rel_path,
    render_overflow_footer,
)
from orchestrator.tools.registry import (
    TOOL_ALLOWED_STATE_KEYS,
    Tool,
    ToolContext,
    ToolNotFoundError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from orchestrator.tools.scheduling import MAX_TOOL_WORKERS, plan_stages

logger = logging.getLogger(__name__)

#: Stream HX-12 (Mini-ADR HX-I5) — a promoted tool unused for this many
#: ReAct steps is dropped from the bind when the compressor fires. Constant
#: by design (the HX-6 EWMA discipline: parameterize when hit-rate data
#: demands it); a demoted tool stays re-promotable from the deferred pool.
_PROMOTED_STALE_STEPS = 12

#: Injected as the final turn's instruction when the step budget is spent, so
#: the run wraps up with what it has instead of hard-failing (hermes-agent
#: #7915 — a hard stop discards finished work; a forced summary preserves it).
_MAX_STEPS_WRAPUP_INSTRUCTION = (
    "You have reached this task's step budget and can no longer call any tools. "
    "Using everything you have already gathered, produced, or written so far, "
    "write your best, complete final answer to the user's request now. "
    "Do not ask to continue and do not attempt to call any tools."
)

#: B3 — token 预算耗尽的收尾指令(措辞镜像步数版,原因改为 token)。
_TOKEN_BUDGET_WRAPUP_INSTRUCTION = (
    "You have reached this task's token budget and can no longer call any tools. "  # noqa: S105
    "Using everything you have already gathered, produced, or written so far, "
    "write your best, complete final answer to the user's request now. "
    "Do not ask to continue and do not attempt to call any tools."
)

# Stream L.L6 — counters for the adaptive tool scheduler. ``stages_total``
# counts every stage execution; ``dispatched_total`` counts the underlying
# tool calls. The ratio dispatched / stages gives the average per-stage
# concurrency (1.0 == fully sequential, MAX_TOOL_WORKERS == max parallel).
# Two counters instead of a histogram because validate_metric_name reserves
# histograms for duration-shaped ``_seconds`` metrics.
_tools_stages_total = expert_work_counter(
    "expert_work_tools_stages_total",
    "Tool-call stages executed (Stream L.L6).",
)
_tools_dispatched_total = expert_work_counter(
    "expert_work_tools_dispatched_total",
    (
        "Individual tool calls dispatched within L6 stages — divide by "
        "stages to get average concurrency."
    ),
)

# Stream TE-3 — per-tool observability. ``outcome`` is one of ``ok`` (tool
# returned a non-error result), ``error`` (tool raised / returned an error /
# unknown tool), or ``blocked`` (a pre-dispatch middleware refused the call).
# A separate ``expert_work_tool_error_total`` would be redundant: errors are exactly
# ``expert_work_tool_call_total{outcome="error"}`` + ``{outcome="blocked"}``.
# Cardinality: ``outcome`` is a fixed 3-value set; the ``tool`` label is
# normalised by ``_metric_tool_label`` so externally-defined MCP tool names
# (``mcp:<server>.<tool>`` — a single server can expose dozens) collapse to
# ``mcp:<server>`` and never blow up the series count. tenant / call_id are
# deliberately omitted — those unbounded identifiers live in the TE-2 audit row.
_tool_call_total = expert_work_counter(
    "expert_work_tool_call_total",
    "Tool dispatches by tool name and outcome (ok / error / blocked).",
    ("tool", "outcome"),
)
_tool_latency_seconds = expert_work_histogram(
    "expert_work_tool_latency_seconds",
    "Wall-clock seconds per tool dispatch, labelled by tool name.",
    ("tool",),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)
#: Stream CM-0 — DB→/workspace projections per turn, by outcome
#: (projected = files written / skipped = unchanged / error = best-effort fail).
_cm_projection_total = expert_work_counter(
    "expert_work_cm_projection_total",
    "Workspace state projections at the turn boundary (Stream CM-0).",
    ("outcome",),
)
#: Stream CM-0 (N1) — size (chars) of the most recent plan recitation injected
#: into the prompt tail. Watches for plan-recitation bloat in long runs.
_cm_recitation_chars = expert_work_gauge(
    "expert_work_cm_recitation_chars",
    "Characters of the plan recitation injected into the prompt tail (Stream CM-0 N1).",
)
#: Stream CM-1 — failed tool calls by error class and tool name, fed into
#: the ``<recovery-advisory>`` channel. Raw success/error counts stay on
#: ``expert_work_tool_call_total{outcome}``; this adds the recovery taxonomy.
_cm_tool_error_total = expert_work_counter(
    "expert_work_cm_tool_error_total",
    "Classified tool failures routed into the recovery advisory (Stream CM-1).",
    ("error_class", "tool"),
)
#: Stream CM-1 — size (chars) of the most recent recovery advisory
#: injected into the prompt tail. Watches for advisory bloat.
_cm_recovery_advisory_chars = expert_work_gauge(
    "expert_work_cm_recovery_advisory_chars",
    "Characters of the recovery advisory injected into the prompt tail (Stream CM-1).",
)
#: Stream CM-2 — working-memory sliding window passes by outcome. A high
#: ``trimmed`` rate vs ``noop`` shows how many compressor (LLM) calls the
#: cheap gate spared. ``noop`` covers under-threshold + nothing-to-cut.
_cm_working_window_total = expert_work_counter(
    "expert_work_cm_working_window_trim_total",
    "Working-memory sliding-window passes at the agent_node entry (Stream CM-2).",
    ("outcome",),
)
#: Stream CM-2 — user turns dropped by the most recent window trim (0 when
#: the pass was a no-op). Watches trim depth on long conversations.
_cm_working_window_dropped_turns = expert_work_gauge(
    "expert_work_cm_working_window_dropped_turns",
    "User turns dropped by the most recent working-memory window trim (Stream CM-2).",
)
#: Stream CM-3 — pre-compaction flush passes by outcome. ``flushed`` =
#: memories written from the discarded middle; ``empty`` = nothing
#: extracted (or a swallowed best-effort failure — see memory.flush logs).
_cm_precompaction_flush_total = expert_work_counter(
    "expert_work_cm_precompaction_flush_total",
    "Pre-compaction memory flushes before a compressor pass (Stream CM-3).",
    ("outcome",),
)
#: Stream CM-3 — memories written by the most recent pre-compaction flush.
_cm_precompaction_flush_memories = expert_work_gauge(
    "expert_work_cm_precompaction_flush_memories",
    "Memories written by the most recent pre-compaction flush (Stream CM-3).",
)
#: Stream CM-5 — oversized tool results externalized to the workspace
#: (externalized = full output saved + reference footer appended /
#: degraded = write failed, the truncated content stands alone).
_cm_tool_overflow_total = expert_work_counter(
    "expert_work_cm_tool_overflow_total",
    "Tool-result overflow externalizations (Stream CM-5).",
    ("outcome", "tool"),
)
#: Stream CM-5 — size (chars) of the most recent externalized overflow.
_cm_tool_overflow_chars = expert_work_gauge(
    "expert_work_cm_tool_overflow_chars",
    "Characters of the most recent externalized tool-result overflow (Stream CM-5).",
)
#: Stream CM-9 — limit-hit effort escalations by signal (loop = the
#: loop-detection middleware tripped last turn; budget = step_count
#: crossed 75% of max_steps).
_cm_effort_escalation_total = expert_work_counter(
    "expert_work_cm_effort_escalation_total",
    "Turns served by the escalated higher-effort caller (Stream CM-9).",
    ("signal",),
)
#: Stream RT-2 PR-2 (RT-ADR-10) — recalled-memory items clipped by the
#: injection token budget. ``truncated`` = the boundary item was cut down to
#: the remaining budget (visible marker); ``dropped`` = whole items past the
#: boundary were left out. Incremented per affected item; the default path
#: (top_k=5 ordinary memories, far under budget) never touches it.
_memory_injection_truncated_total = expert_work_counter(
    "expert_work_memory_injection_truncated_total",
    "Recalled memory items clipped by the injection token budget (Stream RT-2).",
    ("outcome",),
)
# Stream PI-2 — model responses blocked by output screening, by the violation
# category that fired (``secret`` / ``exfil_url`` / ``canary``). A non-zero
# rate here is the inline-injection backstop catching a leak the model emitted.
_output_screen_blocked_total = expert_work_counter(
    "expert_work_output_screen_blocked_total",
    "Model responses blocked by PI-2 output screening, by violation category.",
    ("category",),
)
# Stream PI-2b — output-judge rulings by verdict (``aligned`` / ``misaligned``
# / ``leak`` / ``error``). ``misaligned`` + ``leak`` are blocks; ``error`` is a
# judge failure routed through the configured fail-open / fail-closed policy.
_output_judge_total = expert_work_counter(
    "expert_work_output_judge_total",
    "PI-2b output-judge rulings by verdict (aligned/misaligned/leak/error).",
    ("verdict",),
)
# Stream PI-3b — action-judge rulings on proposed tool calls
# (``aligned`` / ``misaligned`` / ``error``). A misaligned call is denied
# (block mode) or routed to the approval gate (approval mode).
_action_screen_total = expert_work_counter(
    "expert_work_action_screen_total",
    "PI-3b action-judge rulings on tool calls (aligned/misaligned/error).",
    ("verdict",),
)
# Stream 7.4 — outbound DLP redactions on terminal responses, by PII category
# (``email`` / ``phone_cn`` / ``id_card_cn`` / ``credit_card``). Conditional
# output: the reply is redacted in place, not blocked.
_output_dlp_redacted_total = expert_work_counter(
    "expert_work_output_dlp_redacted_total",
    "Outbound DLP redactions on terminal responses, by PII category.",
    ("category",),
)
# Stream RT-1 PR-3 (RT-ADR-4) — structured finalization on terminal turns.
# ``outcome``: ``conform`` = the free candidate already validated (zero extra
# calls); ``cache_hit`` = the schema-enforced resend was served from the E.13
# cache; ``resend`` = one extra schema-enforced LLM call was issued.
_llm_structured_finalize_total = expert_work_counter(
    "expert_work_llm_structured_finalize_total",
    "Structured finalization outcomes on terminal agent turns (Stream RT-1 PR-3).",
    ("outcome",),
)
#: B3 — runs (or workers) that hit the per-run token budget and wrapped up.
_token_budget_exhausted_total = expert_work_counter(
    "expert_work_token_budget_exhausted_total",
    "Runs (or workers) that hit the per-run token budget and wrapped up.",
)

#: Truncate raw exception strings before they go to the LLM. Avoids
#: dumping multi-MB tracebacks into messages. Per-tool truncation
#: (E.7/E.8/E.9 + Mini-ADR E-10) still applies to successful results.
_ERROR_SUMMARY_MAX_CHARS = 500


async def _noop(_ctx: MiddlewareContext) -> None:
    """Default terminal for non-around anchors — middlewares run their
    pre-/post-``call_next`` logic, but there's no inner work to wrap."""


def build_react_graph(
    *,
    llm_caller: LLMCaller,
    tool_registry: ToolRegistry,
    planner_node: PlannerNode | None = None,
    reflect_node: ReflectNode | None = None,
    memory_recall_node: MemoryNode | None = None,
    memory_writeback_node: MemoryNode | None = None,
    # Stream CM-0 PR2b — run-start file→DB ingest of a human-edited PLAN.md.
    workspace_ingest_node: MemoryNode | None = None,
    escalated_llm_caller: LLMCaller | None = None,
    before_llm_chain: MiddlewareChain | None = None,
    after_llm_chain: MiddlewareChain | None = None,
    before_tool_dispatch_chain: MiddlewareChain | None = None,
    context_compressor: ContextCompressor | None = None,
    # Stream CM-2 — working-memory sliding window: cheap LLM-free turn-trim
    # gate run before the compressor at the agent_node entry. ``None`` →
    # no pre-compressor trimming (the default; unchanged from pre-CM-2).
    working_window: WorkingWindow | None = None,
    # Stream CM-12 — mechanical tool-result prune gate: the cheapest, least-lossy
    # gate, run BEFORE the working window at the agent_node entry. Collapses old
    # tool results to 1-line references (lossless for Phase-1-externalized ones).
    # ``None`` → no prune (the default; unchanged from pre-CM-12).
    tool_result_pruner: ToolResultPruner | None = None,
    # Phase 3 — resolved (platform AND agent) master switch for the tool-output
    # budget feature. Threaded into ``_externalize_tool_overflow`` so the
    # generalized (#859) + persist (item 2) branches honour it; the pruner is
    # gated by the factory using the same value. ``True`` (default) keeps the
    # feature on (the env default is resolved upstream in the factory).
    tool_output_budget_enabled: bool = True,
    # Stream CM-3 — pre-compaction flush: when set, agent_node hands the
    # compressor a callback that flushes the about-to-be-discarded middle
    # to long-term memory before each pass summarises it away. ``None`` →
    # no flush (the default; unchanged from pre-CM-3).
    pre_compaction_flush: PreCompactionFlush | None = None,
    # Stream CM-0 — builds a per-turn ``WorkspaceFileWriter`` bound to the
    # run's ToolContext (the real one rides the warm sandbox). ``None`` →
    # no state projection (the default; the unit-test / no-sandbox path).
    workspace_writer_factory: Callable[[ToolContext], WorkspaceFileWriter] | None = None,
    approval_required_tools: frozenset[str] = frozenset(),
    approval_timeout_s: int = 86400,
    # Stream HX-13 — vendor-native tool-disclosure tier from the model
    # catalog (``ModelEntry.tool_disclosure``). ``None`` (default) keeps the
    # HX-12 application tier byte-identical; "native_search" hands the
    # deferred pool to Anthropic's server-side tool search (find_tools
    # excluded); "allowed_tools" freezes the full schema set on the wire
    # and drives the OpenAI allowed subset via promotion.
    tool_disclosure: Literal["native_search", "allowed_tools"] | None = None,
    # Capability Uplift Sprint #8 — Mini-ADR U-8.
    memory_recall_mode: Literal["per_session", "per_turn"] = "per_session",
    # Stream RT-2 PR-2 (RT-ADR-10) — token budget for the injected
    # recalled-memory block plus the guaranteed slice for user-corrected
    # (confidence=1.0) items. Threaded from the manifest's
    # ``memory.long_term.injection_token_budget`` / ``correction_token_budget``;
    # the defaults mirror the protocol defaults. ``token_estimator`` is the
    # shared tiktoken-backed estimator (the same instance the compressor
    # uses); ``None`` falls back to the chars//4 heuristic.
    memory_injection_token_budget: int = 2000,
    memory_correction_token_budget: int = 500,
    token_estimator: TokenEstimator | None = None,
    # Stream PI-1b — when set, untrusted channels (recalled memory, tool
    # results) are spotlighted (datamarked + nonce-fenced) before the model
    # sees them. ``None`` (default) keeps the pre-PI behaviour byte-identical.
    spotlight_nonce: str | None = None,
    # Stream PI-2 — when True, each model response is screened for credential
    # leaks / data-exfil forms and a hit is replaced with a refusal (the
    # inline-injection backstop). ``False`` (default) keeps the path unchanged.
    output_screen: bool = False,
    # Stream PI-2b — the model-backed judge escalation above the rule screen.
    # When set, terminal responses the rules didn't already block are judged for
    # alignment / leakage and a block is replaced with a refusal. ``None``
    # (default) keeps the judge tier inert. ``output_judge_on_error`` picks the
    # fail-open (default) vs fail-closed degradation when the judge call fails.
    output_judge: OutputJudge | None = None,
    output_judge_on_error: Literal["open", "closed"] = "open",
    # Stream PI-3b — when set + ``action_screen`` is on, each proposed tool
    # call is judged for alignment before dispatch; a misaligned turn is denied
    # ("block") or routed to the approval gate ("approval"). ``None`` / "off"
    # keeps the dispatch path unchanged.
    action_judge: ActionJudge | None = None,
    action_screen: Literal["off", "block", "approval"] = "off",
    action_screen_on_error: Literal["open", "closed"] = "open",
    # Stream 7.4 — when True, each terminal response (no tool_calls) is scanned
    # for PII (email / phone / national id / payment card) and matches are
    # redacted in place before the reply leaves. ``False`` (default) keeps the
    # path unchanged. Conditional output: redacts, never blocks.
    output_dlp: bool = False,
    # Stream RT-1 PR-3 (RT-ADR-4) — Tier3 structured final reply. When set,
    # the finalization AIMessage (no tool_calls) must validate against the
    # schema; tool-calling rounds are never constrained. ``None`` (default)
    # keeps every path byte-identical to pre-PR-3 behaviour. See the
    # finalization block inside ``agent_node`` for the enforcement mechanism.
    output_schema: StructuredOutputSpec | None = None,
) -> StateGraph[AgentState, None, AgentState, AgentState]:
    """Assemble the ReAct ``StateGraph`` and return it uncompiled.

    Caller (typically :class:`orchestrator.runner.GraphRunner`)
    compiles it with the shared checkpointer.

    When ``planner_node`` is supplied (Stream J.1 — manifest
    ``workflow.type == "plan_execute"``) the graph is fronted by a
    ``planner`` node: ``START → planner → agent``. The planner writes
    ``AgentState.plan`` and ``agent_node`` renders it into its system
    context every step. ``None`` → plain ``START → agent`` ReAct.

    When ``reflect_node`` is supplied (Stream J.2 — manifest
    ``reflection:`` block) the agent's no-tool-calls exit routes through
    a ``reflect`` node that self-critiques and may loop back to the
    agent instead of ending. ``None`` → the agent ends directly.

    All chain arguments are optional — ``None`` means "no middleware at
    this anchor", and ``agent_node`` / ``tools_node`` short-circuit the
    chain invocation entirely. This preserves the M0 unit-test path
    that doesn't boot a chain.

    The ``around_llm_call`` chain is **not** a parameter here — it
    belongs to :class:`LLMRouter`, which wraps each provider call
    individually (Mini-ADR E-13). Callers configure it on the router
    at construction time.
    """

    # Approval gating is config-driven: only tools the operator lists in the
    # manifest's ``approval_required_tools`` pause for human approval. A tool's
    # ``side_effect="irreversible"`` (resolved via ToolSpec) still drives serial
    # scheduling (L.L6/TE-8) and audit (TE-2), but it no longer force-gates the
    # tool — sandbox isolation + serialisation + audit are the safety floor, and
    # the approval gate is the operator's explicit opt-in. (Earlier TE-4 auto-
    # unioned irreversible tools into the gate; that forced ``bash`` to require
    # approval regardless of config, which the governance UI could not turn off.)
    _gated_tools = approval_required_tools

    async def agent_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        token = cancellation_token(config)
        token.raise_if_cancelled()

        # Stream L.L5 — consume any pending refund the previous tools
        # node wrote (Mini-ADR L-5). Internal-chain tools like
        # ``update_plan`` (K.K8) ask the loop to refund their
        # iterations so housekeeping doesn't burn user-visible budget.
        # Clamp at 0: refund can never produce a negative step count
        # (defensive invariant — a tool can't push the agent into a
        # nonsense negative budget).
        raw_step_count = state.get("step_count", 0)
        refund_pending = state.get("step_count_refund_pending", 0)
        step_count = max(0, raw_step_count - refund_pending)
        max_steps = state.get("max_steps", 0)
        # No-progress stop: the loop-detection middleware arms one escalated
        # turn on the first repeat; if the run keeps tripping it for
        # ``max_no_progress`` (> 0) consecutive turns the agent is stuck, so
        # wrap up early rather than grind to ``max_steps`` making no progress.
        max_no_progress = state.get("max_no_progress", 0)
        no_progress_streak = state.get("no_progress_streak", 0)
        stuck = max_no_progress > 0 and no_progress_streak >= max_no_progress
        # Step budget spent. Rather than raising MaxStepsExceededError and
        # discarding everything the run produced (a finished report, gathered
        # research — the user loses it all), degrade gracefully: do ONE final
        # tool-LESS LLM turn asking the model to wrap up with what it has, then
        # end (hermes-agent #7915). ``budget_exhausted`` forces ``tools=[]`` +
        # a wrap-up instruction just before the call below and strips any
        # tool_calls off the response so the router ends instead of looping.
        # ``stuck`` (no-progress) routes into the same graceful wrap-up.
        # B3 — 全树共享 token 池。对象/sink 缺失(limit=0 或未注入)全为
        # None → 行为与引入前逐字节一致。
        configurable = config.get("configurable") or {}
        token_budget = configurable.get(TOKEN_BUDGET_KEY)
        token_budget = token_budget if isinstance(token_budget, TokenBudget) else None
        guard_sink_raw = configurable.get(GUARD_SINK_KEY)
        guard_sink = guard_sink_raw if callable(guard_sink_raw) else None
        token_tripped = token_budget is not None and token_budget.exhausted
        budget_exhausted = (max_steps > 0 and step_count >= max_steps) or stuck or token_tripped

        # Stream TE-6 — bind active specs plus any deferred tools the run has
        # promoted via ``find_tools`` (carried per-thread on AgentState, so the
        # cached registry stays untouched). ``deferred_specs([])`` is empty when
        # nothing was promoted → identical to the pre-TE-6 ``specs()`` bind.
        # Stream HX-13 — the vendor-native tiers reshape this bind; the
        # ``None`` tier is the HX-12 application tier, byte-identical.
        promoted = state.get("promoted_tools") or []
        if tool_disclosure == "native_search":
            # Anthropic server-side tool search: every still-deferred tool
            # rides along marked ``defer_loading`` (the API retrieves and
            # invokes it; HX-12 call-through promotes on dispatch), and our
            # own ``find_tools`` leaves the bind — one retrieval channel
            # only (Mini-ADR HX-J3).
            promoted_set = set(promoted)
            active = [s for s in tool_registry.specs() if s.name != "find_tools"]
            still_deferred = [
                replace(s, defer_loading=True)
                for s in tool_registry.deferred_specs(tool_registry.deferred_names())
                if s.name not in promoted_set
            ]
            tools = [*active, *tool_registry.deferred_specs(promoted), *still_deferred]
        elif tool_disclosure == "allowed_tools":
            # OpenAI/Azure: the FULL schema set goes on the wire every turn
            # (prompt-cache friendly); still-deferred tools carry the marker
            # so the adapter excludes them from ``tool_choice.allowed_tools``.
            # ``find_tools`` stays — under the allowed constraint it is the
            # only promotion entry point (Mini-ADR HX-J3).
            promoted_set = set(promoted)
            still_deferred = [
                replace(s, defer_loading=True)
                for s in tool_registry.deferred_specs(tool_registry.deferred_names())
                if s.name not in promoted_set
            ]
            tools = [
                *tool_registry.specs(),
                *tool_registry.deferred_specs(promoted),
                *still_deferred,
            ]
        else:
            tools = [*tool_registry.specs(), *tool_registry.deferred_specs(promoted)]
        # Spec 1 D-13 — self-scheduling guardrail: withhold manage_task from the
        # LLM bind on a scheduler-fired run, regardless of which branch above
        # built ``tools``. ManageTaskTool.call also blocks a direct call under
        # this flag (defense-in-depth) — this is the "LLM never sees it" half.
        tools = _filter_scheduling_tools(
            tools,
            trigger_origin=bool((config.get("configurable") or {}).get("trigger_origin", False)),
        )
        messages = list(state["messages"])
        # Stream CM-12 — mechanical tool-result prune: the cheapest, least-lossy
        # gate, run FIRST. When over threshold it collapses OLD tool results
        # (beyond the most-recent N) to 1-line references — lossless for
        # Phase-1-externalized results (full output on disk under .tool_results/),
        # a short stub otherwise — while keeping every turn + the assistant's
        # reasoning intact. Running it before the window means the coarser gates
        # re-estimate against a smaller prompt and fire less often. Prompt-view
        # only — the checkpointed history is never rewritten (CM-C4).
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "context_gates"):
            if tool_result_pruner is not None:
                messages = tool_result_pruner.apply(messages).messages
            # Stream CM-2 — working-memory sliding window: cheap LLM-free first
            # gate. Trims the raw history to first turn + most-recent N turns
            # when over threshold (on HumanMessage boundaries, so tool-call
            # pairs stay intact), BEFORE plan/memory/advisory injection (those
            # are this turn's guidance and must always reach the LLM) and the
            # compressor preflight (the second, LLM-backed gate). Trims only
            # this prompt view — the checkpointed history is never rewritten.
            if working_window is not None:
                trim = working_window.apply(messages)
                messages = trim.messages
                _cm_working_window_total.labels(
                    outcome="trimmed" if trim.dropped_turns else "noop"
                ).inc()
                _cm_working_window_dropped_turns.set(trim.dropped_turns)
        # Stream J.1 — render the plan into the system context so every
        # ReAct step executes against it. No-op for plain ReAct graphs.
        plan = state.get("plan")
        if plan is not None:
            messages = _inject_plan(messages, plan)
        # Stream J.3 — render recalled long-term memories into context.
        # Capability Uplift Sprint #8 (Mini-ADR U-8) — ``memory_recall_mode``
        # decides where the block lands: ``per_session`` (default) anchors
        # it at the prefix slot ``messages[1]`` so the Anthropic adapter
        # can mark it with ``cache_control`` and the prompt cache covers
        # ``[system, task, memories]`` across all turns. ``per_turn`` keeps
        # the J.3 tail-injection behavior as a legacy escape hatch.
        memories = state.get("recalled_memories")
        if memories:
            messages = _inject_memories(
                messages,
                memories,
                mode=memory_recall_mode,
                spotlight_nonce=spotlight_nonce,
                # RT-2 PR-2 (RT-ADR-10) — greedy token budget + correction
                # guarantee; a no-op for the default top_k=5 ordinary recall.
                token_budget=memory_injection_token_budget,
                correction_token_budget=memory_correction_token_budget,
                estimator=token_estimator,
            )
            record_memory_inject_mode(mode=memory_recall_mode)
        # Stream CM-1 (generalising L.L4) — inject a ``<recovery-advisory>``
        # HumanMessage listing every tool call that failed in the previous
        # tools batch, with grounded per-tool recovery guidance. Mini-ADR
        # CM-B4: the advisory is part of the conversation history (persists
        # across turns) and lives in a HumanMessage, NOT the system block,
        # so the L1 prompt-cache prefix invariant stays intact. Append once
        # per failure batch — the channel is reset to ``[]`` in this node's
        # return dict so a follow-on agent step does not double-inject.
        tool_failures = list(state.get("tool_failures", []))
        advisory_message: HumanMessage | None = None
        if tool_failures:
            advisory_message = _build_recovery_advisory(tool_failures)
            messages = [*messages, advisory_message]
            _cm_recovery_advisory_chars.set(len(str(advisory_message.content)))
        # Stream L.L2 — token preflight + summarise-the-middle. When
        # the prompt would exceed the model's configured threshold the
        # compressor swaps the conversation's middle for a
        # ``<context-summary>`` system message, keeping head + tail
        # intact. Mini-ADR L-2 as revised by RT-ADR-6: a transient
        # summariser failure skips compression for this turn (the
        # prompt goes out uncompressed, retried next turn); a
        # ContextOverflowError still surfaces as a run failure — empty
        # middle, max_passes exhausted, or three consecutive failed
        # rounds — so the orchestrator can write a clean RUN_FAILED
        # audit row.
        demoted_tools: list[str] = []
        if context_compressor is not None and context_compressor.should_compress(messages):
            # Stream CM-3 — bind a config-scoped flush so the compressor can
            # hand the middle to long-term memory before discarding it. The
            # callback is best-effort (the flusher swallows its own non-cancel
            # failures); cancellation still propagates out and aborts the run.
            on_pre_compaction: PreCompactionHook | None = None
            if pre_compaction_flush is not None:
                flush_cb = pre_compaction_flush

                async def _on_pre_compaction(middle: Sequence[BaseMessage]) -> None:
                    written = await flush_cb(middle, config, token)
                    _cm_precompaction_flush_total.labels(
                        outcome="flushed" if written else "empty"
                    ).inc()
                    _cm_precompaction_flush_memories.set(written)

                on_pre_compaction = _on_pre_compaction
            # RT-2 PR-4 — surface a COMPACTION event when a summary actually
            # lands. The sink (bridge publish + durable mirror) is injected by
            # ``sse.run_agent`` via config; the compressor stays SSE-agnostic,
            # handing back :class:`CompactionStats` through this best-effort
            # hook. Swallow non-cancel failures here (same contract as the
            # flush hook) so an observability hiccup never fails the run.
            on_compacted: OnCompacted | None = None
            compaction_sink = compaction_sink_from_config(config)
            if compaction_sink is not None:
                sink = compaction_sink

                async def _on_compacted(stats: CompactionStats) -> None:
                    try:
                        await sink(
                            {
                                "passes": stats.passes,
                                "tokens_before": stats.tokens_before,
                                "tokens_after": stats.tokens_after,
                                "summary_chars": stats.summary_chars,
                            }
                        )
                    except Exception:
                        logger.warning("agent.compaction_event_failed", exc_info=True)

                on_compacted = _on_compacted
            # RT-ADR-6 — scope the consecutive-failure streak to this
            # conversation: the compressor instance is cached per
            # (tenant, agent, version) and shared across every thread /
            # user of the agent, so the streak must key on thread_id.
            compress_thread_id = (config.get("configurable") or {}).get("thread_id")
            messages = await context_compressor.compress(
                messages,
                on_pre_compaction=on_pre_compaction,
                on_compacted=on_compacted,
                streak_key=str(compress_thread_id) if compress_thread_id else None,
            )
            # Stream HX-12 (Mini-ADR HX-I5) — promotion demotion rides the
            # same pressure signal: the context is being squeezed, so
            # promoted tools unused for N turns leave the next turn's bind.
            # They stay in the deferred pool — find_tools / a direct
            # call-through re-promotes any of them at any time, so this
            # only slims the bind, never loses a capability. Manifest
            # (core/active) tools are structurally out of scope: demotion
            # touches only the promoted-from-deferred list.
            last_used = state.get("promoted_tool_last_used") or {}
            demoted_tools = [
                name
                for name in promoted
                # No stamp = freshly promoted this very turn; never stale.
                if step_count - last_used.get(name, step_count) > _PROMOTED_STALE_STEPS
            ]
            if demoted_tools:
                promotion_events.labels(event="demote").inc(len(demoted_tools))
                logger.info(
                    "tools.promotion_demoted count=%d step=%d", len(demoted_tools), step_count
                )
        configurable = config.get("configurable") or {}
        tenant_id = _parse_uuid(configurable.get("tenant_id"))
        # Stream Agent-Templates (M1-5a) — the end-user this run is for, threaded
        # to the token-usage middleware for per-user cost attribution.
        user_id = _parse_uuid(configurable.get("user_id"))

        cache_hit_response: AIMessage | None = None
        if before_llm_chain is not None:
            ctx = MiddlewareContext(
                payload={"messages": messages, "tools": tools, "tenant_id": tenant_id}
            )
            await before_llm_chain.invoke(ctx, _noop)
            messages = list(ctx.payload.get("messages", messages))
            tools = list(ctx.payload.get("tools", tools))
            hit = ctx.payload.get("llm_cache_hit")
            if isinstance(hit, AIMessage):
                cache_hit_response = hit

        # Stream CM-9 (Mini-ADR CM-J5) — limit-hit escalation: serve this
        # turn from the higher-effort caller when the loop-detection
        # middleware tripped last turn, or the step budget is nearly
        # spent (one deliberate deep think beats more shallow retries).
        # Request params only — the prompt bytes are unchanged, so the
        # provider prompt cache is unaffected.
        loop_signal = bool(state.get("escalate_next"))
        budget_signal = max_steps > 0 and step_count * 4 >= max_steps * 3
        # Stream CM-11 (Mini-ADR CM-M1) — event-driven escalation, the second
        # of the two dynamic-compute triggers:
        #  * micro: a non-transient tool failure in the previous batch is a
        #    real anomaly to reason through (``transient`` is retryable
        #    jitter, not worth a deep think). The recovery advisory for these
        #    failures lands this same turn, so escalate the turn that reacts.
        #  * macro: the live plan goal changed since the previous turn (a
        #    re-plan, or a human PLAN.md edit ingested via CM-0) — one deep
        #    think to re-calibrate the execution strategy. The initial plan is
        #    NOT a change (the planner already decomposed it deeply), so a
        #    prior goal must exist to diff against.
        error_signal = any(f.error_class != "transient" for f in tool_failures)
        current_goal = plan.goal if plan is not None else None
        prior_goal = state.get("last_plan_goal")
        goal_signal = (
            current_goal is not None and prior_goal is not None and current_goal != prior_goal
        )
        active_caller = llm_caller
        if escalated_llm_caller is not None and (
            loop_signal or budget_signal or error_signal or goal_signal
        ):
            active_caller = escalated_llm_caller
            signal = (
                "loop"
                if loop_signal
                else "budget"
                if budget_signal
                else "error"
                if error_signal
                else "goal"
            )
            _cm_effort_escalation_total.labels(signal=signal).inc()
            logger.info("llm.effort_escalated signal=%s step=%d/%d", signal, step_count, max_steps)

        # Budget-exhausted final turn: no tools (so the model can only answer),
        # append the wrap-up instruction, and bypass the response cache (a
        # cached tool-call hit would re-enter the loop). The escalation above
        # only covers the step-budget case — its ``budget_signal`` is
        # step-count based (fires at >=75% of ``max_steps``), so a
        # step-exhausted wrap-up gets a deliberate think from the
        # higher-effort caller. A token-triggered wrap-up (``token_tripped``)
        # is not what that signal watches, so this final turn falls through
        # to the normal (non-escalated) caller instead.
        if budget_exhausted:
            tools = []
            cache_hit_response = None
            wrapup = (
                _TOKEN_BUDGET_WRAPUP_INSTRUCTION if token_tripped else _MAX_STEPS_WRAPUP_INSTRUCTION
            )
            messages = [*messages, HumanMessage(content=wrapup)]
            # B3 — guard 可见化:每个触发的闸各发一条 tripped 帧(老盲区一起治)。
            if token_tripped and token_budget is not None:
                await emit_guard_frame(
                    guard_sink,
                    build_guard_frame(
                        kind="tripped",
                        guard="token_budget",
                        detail={"spent": token_budget.spent, "limit": token_budget.limit},
                    ),
                )
                _token_budget_exhausted_total.inc()
            if max_steps > 0 and step_count >= max_steps:
                await emit_guard_frame(
                    guard_sink,
                    build_guard_frame(
                        kind="tripped",
                        guard="max_steps",
                        detail={"steps": step_count, "max": max_steps},
                    ),
                )
            if stuck:
                await emit_guard_frame(
                    guard_sink,
                    build_guard_frame(
                        kind="tripped",
                        guard="no_progress",
                        detail={"streak": no_progress_streak, "max": max_no_progress},
                    ),
                )
            logger.warning("agent.budget_graceful_wrapup step=%d max=%d", step_count, max_steps)
        elif token_budget is not None and token_budget.warning:
            # B3 — 80% 预警:首跨发一条 warning 帧(flag 挂共享对象,全树一次);
            # 此后每步 prompt 附预算注(ephemeral,不持久化,不碰 system 前缀)。
            if not token_budget.warned:
                token_budget.warned = True
                await emit_guard_frame(
                    guard_sink,
                    build_guard_frame(
                        kind="warning",
                        guard="token_budget",
                        detail={"spent": token_budget.spent, "limit": token_budget.limit},
                    ),
                )
                logger.info(
                    "agent.token_budget_warning spent=%d limit=%d",
                    token_budget.spent,
                    token_budget.limit,
                )
            messages = [
                *messages,
                HumanMessage(
                    content=(
                        f"[token budget notice] This run has used {token_budget.spent} of its "
                        f"{token_budget.limit} token budget ({token_budget.remaining} remaining). "
                        "Converge quickly: prefer finishing with what you have over further "
                        "tool exploration."
                    )
                ),
            ]

        # ``messages`` is now the exact prompt — the E.13 cache key input.
        if cache_hit_response is not None:
            response: AIMessage = cache_hit_response
        else:
            _token_sink = make_token_sink(
                step=step_count + 1,
                publish=token_sink_from_config(config),
                dlp=output_dlp,
                screen=output_screen,
                judge_enabled=output_judge is not None,
            )
            # Wrap the LLM call so a cancel mid-call interrupts the
            # in-flight await rather than waiting it out (E.15).
            # 10.1 — one ``expert_work.orchestrator.llm_call`` child span per
            # provider call, attached under the session root span.
            with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "llm_call"):
                if _token_sink is not None:
                    # Only pass ``on_delta`` when a sink is actually wired
                    # (子项目 2 streaming path) — omitting the kwarg otherwise
                    # keeps every non-streaming ``LLMCaller`` implementation
                    # (including fixtures predating the ``on_delta`` addition
                    # to the Protocol) byte-identical to before this change.
                    response = await token.run_cancellable(
                        active_caller(messages=messages, tools=tools, on_delta=_token_sink)
                    )
                else:
                    response = await token.run_cancellable(
                        active_caller(messages=messages, tools=tools)
                    )
            if _token_sink is not None:
                await _token_sink.flush()

        # B3 — count the INITIAL response's real spend right where it
        # resolves (cache hit or fresh call), before screen/judge below can
        # replace it with a refusal ``AIMessage`` that carries no
        # ``usage_metadata`` (the billed call still happened — the tokens
        # must not vanish from the budget just because the reply got
        # blocked) and before a structured resend can overwrite ``response``
        # with a second call's reply. A resend's own spend is accounted
        # separately, inside ``_finalize_structured_response``.
        if token_budget is not None:
            token_budget.add(usage_total(getattr(response, "usage_metadata", None)))

        # Budget-exhausted turn must terminate: no tools were bound so the model
        # shouldn't emit tool_calls, but strip any it returns anyway (defends
        # against a provider/stub echoing them) so ``_should_continue`` routes
        # to END rather than back into the (already-spent) loop.
        if budget_exhausted and _extract_tool_calls(response):
            response = response.model_copy(update={"tool_calls": [], "invalid_tool_calls": []})

        # Stream RT-1 PR-3 (RT-ADR-4) — structured finalization. Only a
        # terminal candidate (no tool_calls) is constrained; tool-calling
        # rounds pass through untouched. The mechanism is deliberately
        # uniform across the three RT-ADR-2 provider paths — two-stage
        # deferred enforcement:
        #
        #  * The primary call above NEVER carries the schema. On the
        #    tool_call path a schema forces one schema-tool (superseding
        #    every real tool) and on the prompt path the injected
        #    instruction demands JSON-only output — both break tool use on
        #    intermediate rounds; and the router's RT-ADR-1 loop validates
        #    every structured response, so even the native path (where
        #    ``response_format`` can ride next to tools) would
        #    correction-loop on a legitimate tool_calls turn. The fallback
        #    chain can also mix capabilities (RT-ADR-2 keeps the chosen
        #    path invisible here), so per-path special-casing has no sound
        #    input at this layer.
        #  * A terminal candidate is validated locally first — a conforming
        #    reply costs zero extra calls.
        #  * A non-conforming candidate triggers ONE schema-enforced resend
        #    (candidate + correction appended, ``tools=[]``) where the
        #    provider adapter enforces via its declared path (native
        #    ``response_format`` / forced tool / prompt instruction) and
        #    the router's RT-ADR-1 loop retries residual invalid output.
        #    Still invalid → LLMOutputValidationError fails the run loudly
        #    — silently returning schema-violating output would betray the
        #    Tier3 contract.
        #
        # Cache wiring (design § 7.4 hard requirement): the resend runs its
        # own before-anchor pass with the ``StructuredOutputSpec`` INSTANCE
        # in ``payload["output_schema"]``, and the bottom after-anchor pass
        # carries the same instance — so the E.13 schema fingerprint keys
        # lookup and store identically. The primary call is accounted on
        # its own after-chain pass inside the helper (one pass per real
        # upstream call keeps G.9 token metering exactly-once).
        structured_prompt: list[BaseMessage] | None = None
        structured_cache_hit = False
        primary_loop_detected = False
        if output_schema is not None and not _extract_tool_calls(response):
            finalize = await _finalize_structured_response(
                candidate=response,
                prompt_messages=messages,
                spec=output_schema,
                caller=active_caller,
                token=token,
                before_llm_chain=before_llm_chain,
                after_llm_chain=after_llm_chain,
                tenant_id=tenant_id,
                user_id=user_id,
                primary_cache_hit=cache_hit_response is not None,
                token_budget=token_budget,
            )
            response = finalize.response
            structured_prompt = finalize.structured_prompt
            structured_cache_hit = finalize.structured_cache_hit
            primary_loop_detected = finalize.primary_loop_detected

        # Stream PI-2 — output screening backstop. Catch a credential leak /
        # exfil form the model emitted (e.g. driven by an inline injection
        # spotlighting can't wrap) before it reaches the user or a tool.
        rule_blocked = False
        if output_screen:
            screened, screen_cats = _screen_model_response(response)
            rule_blocked = screened is not response
            response = screened
            if screen_cats:
                await _emit_output_guard_audit(
                    audit_logger_from_config(config),
                    tenant_id,
                    action=AuditAction.OUTPUT_SCREEN_BLOCKED,
                    result=AuditResult.DENIED,
                    categories=screen_cats,
                )
        # Stream PI-2b — model-backed judge escalation. Skip when the rules
        # already blocked (save the call) and run only on a terminal response
        # (no tool_calls) — the judge is a per-response LLM call.
        if output_judge is not None and not rule_blocked and not _extract_tool_calls(response):
            response = await _judge_model_response(
                response,
                messages,
                judge=output_judge,
                on_error=output_judge_on_error,
                token=token,
            )
        # Stream 7.4 — outbound DLP. Redact PII the model emitted in a terminal
        # response before it leaves. Skip when the rules already blocked (the
        # refusal carries no PII) and only on a terminal turn (a tool-call turn's
        # args route through the action screen, not here).
        if output_dlp and not rule_blocked and not _extract_tool_calls(response):
            response, dlp_cats = _dlp_redact_response(response)
            if dlp_cats:
                # RT-1 PR-3 — the redaction rewrote the reply text; a stale
                # ``parsed`` would hand consumers the unredacted values.
                # Re-derive it from the redacted content (drop it when the
                # redacted reply no longer validates — security wins).
                if output_schema is not None and "parsed" in response.additional_kwargs:
                    response = _reconcile_parsed_after_rewrite(response, output_schema)
                await _emit_output_guard_audit(
                    audit_logger_from_config(config),
                    tenant_id,
                    action=AuditAction.OUTPUT_DLP_REDACTED,
                    result=AuditResult.SUCCESS,
                    categories=dlp_cats,
                )

        if after_llm_chain is not None:
            # RT-1 PR-3 — when a structured resend happened, this pass
            # accounts THAT call: its exact prompt view + the spec instance
            # (§ 7.4 store side; the primary call was already accounted
            # inside ``_finalize_structured_response``). Without a resend
            # this is byte-identical to the pre-PR-3 payload.
            prompt_view = structured_prompt if structured_prompt is not None else messages
            after_messages: list[BaseMessage] = [*prompt_view, response]
            after_payload: dict[str, Any] = {
                "messages": after_messages,
                "response": response,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "prompt_messages": prompt_view,
                "cache_hit": (
                    structured_cache_hit
                    if structured_prompt is not None
                    else cache_hit_response is not None
                ),
            }
            if structured_prompt is not None and "parsed" in response.additional_kwargs:
                # Store-side pollution guard (suspenders half; the lookup
                # self-heal in ``_finalize_structured_response`` is the
                # belt): a resend rewritten AFTER finalization — PI-2 /
                # judge refusal, or a DLP redaction that broke a
                # constrained field (``parsed`` dropped) — must NOT land
                # under the structured key, or the next identical turn
                # would hit a non-conforming entry. Without the schema tag
                # the rewritten response stores under the plain key for
                # the resend prompt, which no structured lookup derives.
                after_payload["output_schema"] = output_schema
            ctx = MiddlewareContext(payload=after_payload)
            await after_llm_chain.invoke(ctx, _noop)
            new_messages = _extract_post_llm_messages(ctx, original=after_messages)
            # Stream CM-1 — persist the advisory into history so the
            # next agent step sees it even after this dict's reducer
            # appends. The middleware path's ``new_messages`` is the
            # full post-LLM delta; prepend the advisory in case the
            # middleware filtered the prompt body.
            persisted_messages: list[BaseMessage] = list(new_messages)
            if advisory_message is not None and advisory_message not in persisted_messages:
                persisted_messages = [advisory_message, *persisted_messages]
            looped_this_turn = bool(ctx.payload.get("loop_detected")) or primary_loop_detected
            update_mw: dict[str, Any] = {
                "messages": persisted_messages,
                "step_count": step_count + 1,
                "step_count_refund_pending": 0,
                "tool_failures": [],
                # CM-9 — arm escalation for the next step when the loop
                # middleware tripped on THIS response; otherwise reset
                # the consumed signal. RT-1 PR-3: a trip on the discarded
                # primary candidate (accounted inside the finalize helper)
                # arms it too — the loop signal is about model behaviour,
                # not about which response was ultimately kept.
                "escalate_next": looped_this_turn,
                # No-progress stop — a loop trip this turn bumps the streak;
                # a clean turn resets it (mirrors ``escalate_next``).
                "no_progress_streak": no_progress_streak + 1 if looped_this_turn else 0,
                # CM-11 — rebaseline the goal so a one-off change escalates
                # exactly one turn (next turn diffs against this value).
                "last_plan_goal": current_goal,
            }
            if demoted_tools:
                update_mw["promoted_tools"] = {"remove": demoted_tools}
            return update_mw

        # Stream CM-1 — persist the advisory in conversation history
        # alongside the LLM response so the next agent step sees it.
        emit_messages: list[BaseMessage] = (
            [advisory_message, response] if advisory_message is not None else [response]
        )
        update_plain: dict[str, Any] = {
            "messages": emit_messages,
            "step_count": step_count + 1,
            "step_count_refund_pending": 0,
            "tool_failures": [],
            "escalate_next": False,  # CM-9 — no middleware chain, reset
            "no_progress_streak": 0,  # no middleware → no loop trip → reset
            "last_plan_goal": current_goal,  # CM-11 — rebaseline the goal
        }
        if demoted_tools:
            update_plain["promoted_tools"] = {"remove": demoted_tools}
        return update_plain

    async def tools_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        token = cancellation_token(config)
        token.raise_if_cancelled()

        last = state["messages"][-1]
        tool_calls = _extract_tool_calls(last)
        if not tool_calls:
            return {}

        # Stream J.8 (Mini-ADR J-24) — approval gate. Two re-entrant paths:
        #
        # 1. RESUME — ``approval_resume`` set: a human verdict came back
        #    via ``aupdate_state``. Apply it (approve dispatches, modify
        #    rewrites args, reject synthesises rejection ToolMessages)
        #    and clear the channel. Skips re-detection so the gate does
        #    not re-fire on the same turn.
        # 2. DETECT — no resume in flight: scan for the first gated call
        #    (a tool in ``_gated_tools`` = manifest ``approval_required_tools``
        #    plus TE-4 irreversible tools, or ``ask_for_approval``).
        #    On a hit, write ``pending_approval`` + dispatch nothing —
        #    ``_after_tools`` routes to END (RunStatus.PAUSED). The
        #    end-and-resume model (vs LangGraph ``interrupt()``) keeps
        #    the parallel L.L6 staging below untouched.
        approval_resume = state.get("approval_resume")
        ingest_update: dict[str, Any] = {}
        if approval_resume is not None:
            # Stream CM-8 (Mini-ADR CM-I4) — the resume re-entry skips the
            # entry chain (``aupdate_state(as_node="agent")`` lands the
            # graph straight here), so run the workspace ingest now: a
            # PLAN.md edited during the pause still flows back before the
            # verdict executes. Best-effort + strict-scan semantics live
            # inside the node (CM-0, unchanged).
            if workspace_ingest_node is not None:
                ingest_update = await workspace_ingest_node(state, config)
            resume_outcome = apply_resume_decision(tool_calls, _gated_tools, approval_resume)
            if resume_outcome.binding_drift:
                # RT-6 Tier A (RT-ADR-19) — the checkpointed tool_call drifted
                # from what the human approved (tamper / replay / bug). Record
                # the integrity veto before the terminal reject routes to END.
                configurable = config.get("configurable") or {}
                await _emit_binding_drift_audit(
                    audit_logger_from_config(config),
                    _parse_uuid(configurable.get("tenant_id")),
                    run_id=configurable.get("run_id"),
                    thread_id=configurable.get("thread_id"),
                )
            if resume_outcome.reject_messages:
                rejected: dict[str, Any] = {
                    **ingest_update,
                    "messages": list(resume_outcome.reject_messages),
                    "approval_resume": None,
                }
                if resume_outcome.terminal:
                    rejected["approval_outcome"] = "rejected"
                return rejected
            # approve / modify — fall through to dispatch the (possibly
            # arg-rewritten) calls; clear the resume channel on return.
            tool_calls = resume_outcome.tool_calls
        elif not state.get("pending_approval"):
            # Stream PI-3b — action screening: judge each proposed tool call
            # against the user's request before dispatch. A misaligned turn is
            # denied (block) or routed to the approval gate (approval).
            if action_screen != "off" and action_judge is not None:
                bad_idx = await _first_misaligned_action(
                    tool_calls,
                    state["messages"],
                    judge=action_judge,
                    on_error=action_screen_on_error,
                    token=token,
                )
                if bad_idx is not None:
                    if action_screen == "approval":
                        configurable = config.get("configurable") or {}
                        thread_id = str(configurable.get("run_id") or "run")
                        return {
                            "pending_approval": build_approval_request(
                                ApprovalTarget(
                                    index=bad_idx,
                                    tool_call=tool_calls[bad_idx],
                                    is_agent_initiated=False,
                                ),
                                thread_id=thread_id,
                                timeout_s=approval_timeout_s,
                                # RT-6 Tier A — the action-screen target is chosen
                                # by the judge, not find_approval_target; the resume
                                # re-scan cannot reproduce it, so mint unbound to
                                # avoid verifying the wrong call (RT-ADR-19).
                                bind=False,
                            )
                        }
                    # block — deny the whole turn (one error ToolMessage per
                    # call so no tool_call is left orphaned); the agent re-plans.
                    return {
                        "messages": [
                            ToolMessage(
                                content=(
                                    "[blocked] action screening: a tool call did not match "
                                    "your request and was not run"
                                ),
                                tool_call_id=str(call.get("id") or ""),
                                status="error",
                            )
                            for call in tool_calls
                        ]
                    }
            target = find_approval_target(tool_calls, _gated_tools)
            if target is not None:
                configurable = config.get("configurable") or {}
                thread_id = str(configurable.get("run_id") or "run")
                return {
                    "pending_approval": build_approval_request(
                        target,
                        thread_id=thread_id,
                        timeout_s=approval_timeout_s,
                    )
                }

        ctx_obj = _build_tool_context(config, plan=state.get("plan"))
        # Stream TE-2 — per-tool-call audit sink (may be None on the dev /
        # unit-test path; ``_dispatch_tool`` treats the emit as best-effort).
        audit_logger = audit_logger_from_config(config)
        # Stream CM-5 — one per-turn writer for overflow externalization,
        # from the same factory (and gate) as the CM-0 projection.
        overflow_writer = (
            workspace_writer_factory(ctx_obj) if workspace_writer_factory is not None else None
        )
        # Stream L.L6 — group tool_calls into stages of mutually-non-
        # conflicting calls. Within a stage we ``asyncio.gather`` (capped
        # at MAX_TOOL_WORKERS); stages execute sequentially so any
        # state-mutating call (``update_plan``, ``save_artifact`` on a
        # contested path) still observes the LLM's intended ordering.
        # Stream TE-6 — schedule over ``all_specs()`` so a promoted deferred
        # tool is classified (side_effect / path_args) correctly when called.
        # Equals ``specs()`` when nothing is deferred.
        specs_by_name = {spec.name: spec for spec in tool_registry.all_specs()}
        stages = plan_stages(tool_calls, specs_by_name)
        results: dict[
            int, tuple[ToolMessage, Mapping[str, Any], int, ClassifiedToolError | None]
        ] = {}
        # Stream K.K8 — collect per-tool state writes for promotion to
        # the AgentState update dict. Order follows the LLM's original
        # tool_call sequence: a later call's update wins. L6 preserves
        # that because we apply updates in original-index order after
        # stages complete.
        accumulated_state: dict[str, Any] = {}
        # Stream L.L5 — accumulate iteration refunds across the batch.
        # Refunds are commutative, so stage ordering doesn't affect the
        # total. Seed from any pending refund the previous node left
        # unconsumed (defence-in-depth — agent_node also resets).
        refund_total = state.get("step_count_refund_pending", 0)

        async def _run_call(
            tc: dict[str, Any],
        ) -> tuple[ToolMessage, Mapping[str, Any], int, ClassifiedToolError | None]:
            # Per-call cancel check + ``run_cancellable`` mirror the M0
            # sequential path so cancellation semantics stay identical:
            # a cancel mid-batch interrupts every in-flight tool via
            # the shared token.
            token.raise_if_cancelled()
            return await token.run_cancellable(
                _dispatch_tool(
                    tc,
                    tool_registry,
                    ctx_obj,
                    before_tool_dispatch_chain=before_tool_dispatch_chain,
                    audit_logger=audit_logger,
                    overflow_writer=overflow_writer,
                    spotlight_nonce=spotlight_nonce,
                    budget_enabled=tool_output_budget_enabled,
                )
            )

        semaphore = asyncio.Semaphore(MAX_TOOL_WORKERS)

        async def _bounded(
            tc: dict[str, Any],
        ) -> tuple[ToolMessage, Mapping[str, Any], int, ClassifiedToolError | None]:
            async with semaphore:
                return await _run_call(tc)

        for stage in stages:
            _tools_stages_total.inc()
            _tools_dispatched_total.inc(len(stage))
            # ``return_exceptions=False`` — any exception from a tool
            # already comes back wrapped as a ToolMessage by
            # ``_dispatch_tool``; reaching gather with a raw exception
            # would be ``RunCancelledError`` (cancellation) or a
            # programmer error, both of which should propagate.
            stage_results = await asyncio.gather(
                *(_bounded(tool_calls[call.index]) for call in stage)
            )
            for call, result in zip(stage, stage_results, strict=True):
                results[call.index] = result

        # Re-assemble in original tool_call order. L5 / K8 invariants
        # require a stable iteration order downstream.
        new_messages: list[BaseMessage] = []
        # Stream CM-1 (generalising L.L4) — collect classified tool
        # failures so the next agent step injects the recovery advisory.
        # Two sources, in original tool_call order so the advisory lists
        # failures in the sequence the ToolMessages appear:
        #   1. error path — ``_dispatch_tool`` already classified from the
        #      real exception (4th tuple element).
        #   2. success-but-didn't-land — L-4's mutation classifier on a
        #      non-error ToolMessage, folded into ``mutation_not_landed``.
        tool_failures: list[ClassifiedToolError] = []
        for idx in range(len(tool_calls)):
            tool_message, tool_state, refund_inc, classified = results[idx]
            new_messages.append(tool_message)
            for key, value in tool_state.items():
                if key not in TOOL_ALLOWED_STATE_KEYS:
                    continue
                # Stream TE-6 — list-valued channels (``promoted_tools`` union,
                # ``subagent_invocations`` append) must ACCUMULATE within the
                # batch, not overwrite: when several tools write the same
                # channel in one parallel stage (e.g. two ``find_tools`` or two
                # ``is_parallel_safe`` sub-agents), a plain ``[key] = value``
                # keeps only the last call's list and silently drops the rest
                # — the channel reducer runs at the node boundary and never
                # sees the clobbered intra-batch values. Scalar channels
                # (``plan``) keep last-write-wins.
                if isinstance(value, list):
                    existing = accumulated_state.get(key)
                    accumulated_state[key] = (
                        [*existing, *value] if isinstance(existing, list) else list(value)
                    )
                else:
                    accumulated_state[key] = value
            refund_total += refund_inc
            failure = _classify_tool_failure(tool_calls[idx], tool_message, classified)
            if failure is not None:
                tool_failures.append(failure)
                _cm_tool_error_total.labels(
                    error_class=failure.error_class, tool=failure.tool_name
                ).inc()

        # Stream HX-12 — stamp ``promoted_tool_last_used`` for the demotion
        # gate: every already-promoted tool that dispatched in this batch
        # refreshes its stamp; every name freshly promoted in this batch
        # (find_tools result or a call-through) gets its baseline. Tools
        # without a stamp would otherwise be un-ageable.
        current_step = int(state.get("step_count", 0))
        already_promoted = set(state.get("promoted_tools") or [])
        batch_promoted = accumulated_state.get("promoted_tools")
        freshly_promoted = set(batch_promoted) if isinstance(batch_promoted, list) else set()
        used_stamps: dict[str, int] = dict.fromkeys(
            (
                name
                for name in (str(call.get("name", "")) for call in tool_calls)
                if name in already_promoted
            ),
            current_step,
        )
        for name in freshly_promoted:
            used_stamps.setdefault(name, current_step)

        # CM-8 — the resume-path ingest lands first so a tool's own state
        # write (e.g. ``update_plan`` in the resumed batch) still wins.
        result_dict: dict[str, Any] = {
            **ingest_update,
            "messages": new_messages,
            "step_count_refund_pending": refund_total,
            **accumulated_state,
        }
        if used_stamps:
            result_dict["promoted_tool_last_used"] = used_stamps
        # Only write the channel when there are failures — the absent
        # case keeps the agent_node's ``state.get("tool_failures", [])``
        # default fast-path active.
        if tool_failures:
            result_dict["tool_failures"] = tool_failures
        # Stream J.8 — when this batch ran on an approve / modify resume,
        # clear the transient ``approval_resume`` channel so a follow-on
        # turn does not re-apply the stale verdict.
        if approval_resume is not None:
            result_dict["approval_resume"] = None
        # Stream CM-0 — turn-end DB→/workspace projection (best-effort).
        # Only-if-changed: an unchanged turn skips the sandbox round-trip and
        # leaves ``last_projection_hash`` untouched.
        projection = await _project_workspace_state(
            workspace_writer_factory, state, ctx_obj, audit_logger
        )
        if projection is not None and not projection.skipped:
            result_dict["last_projection_hash"] = projection.digest
        return result_dict

    graph: StateGraph[AgentState, None, AgentState, AgentState] = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)

    # Entry chain(二期 P1.3)—— 两条分支从 START 并发,AND-join 汇到 agent:
    #   分支 1: memory_recall(写 recalled_memories)
    #   分支 2: planner → workspace_ingest(写 plan;分支内保序 —— CM-0:
    #           人改的 PLAN.md 仍覆盖 planner 生成的 plan)
    # 写集不相交;汇合用列表形式 add_edge([a, b], "agent") 建 AND-join
    # 屏障(NamedBarrierValue),agent 等全部父分支完成后执行一次。
    # ``# type: ignore[arg-type]``: the bare Callable node aliases don't
    # match LangGraph's internal ``_NodeWithConfig`` overloads (same gap
    # runs.py documents).
    tails: list[str] = []
    if memory_recall_node is not None:
        graph.add_node("memory_recall", memory_recall_node)  # type: ignore[arg-type]
        graph.add_edge(START, "memory_recall")
        tails.append("memory_recall")
    plan_tail: str | None = None
    if planner_node is not None:
        graph.add_node("planner", planner_node)  # type: ignore[arg-type]
        graph.add_edge(START, "planner")
        plan_tail = "planner"
    if workspace_ingest_node is not None:
        graph.add_node("workspace_ingest", workspace_ingest_node)  # type: ignore[arg-type]
        graph.add_edge(plan_tail if plan_tail is not None else START, "workspace_ingest")
        plan_tail = "workspace_ingest"
    if plan_tail is not None:
        tails.append(plan_tail)
    if not tails:
        tails.append(START)
    if len(tails) > 1:
        # LangGraph 语义:单串 add_edge(a, c) + add_edge(b, c) 是 OR 触发
        # (每个父完成都触发 c 一次);列表形式才建 AND-join 屏障
        # (NamedBarrierValue)。两分支不等长(planner→ingest 是 2 步)时
        # OR 触发会让 agent 双跑 —— 带 tool_calls 时直接 InvalidUpdateError。
        graph.add_edge(tails, "agent")
    else:
        graph.add_edge(tails[0], "agent")

    # Exit: the run's end routes through ``memory_writeback`` when present.
    end_target: str = END
    if memory_writeback_node is not None:
        graph.add_node("memory_writeback", memory_writeback_node)  # type: ignore[arg-type]
        graph.add_edge("memory_writeback", END)
        end_target = "memory_writeback"

    if reflect_node is not None:
        # When the agent stops issuing tool_calls, route to ``reflect``
        # instead of ending — it critiques and may send the agent back.
        graph.add_node("reflect", reflect_node)  # type: ignore[arg-type]
        graph.add_conditional_edges("agent", _should_continue, {"tools": "tools", END: "reflect"})
        graph.add_conditional_edges("reflect", _after_reflect, {"agent": "agent", END: end_target})
    else:
        graph.add_conditional_edges("agent", _should_continue, {"tools": "tools", END: end_target})
    # Stream J.8 — after ``tools``, a run with ``pending_approval`` set
    # routes straight to END (RunStatus.PAUSED): the checkpoint persists
    # and ``memory_writeback`` is deliberately skipped (the run is paused,
    # not finished). Otherwise the normal ReAct loop continues to ``agent``.
    graph.add_conditional_edges("tools", _after_tools, {"agent": "agent", END: END})
    return graph


def _after_reflect(state: AgentState) -> Literal["agent", "__end__"]:
    """Route out of the ``reflect`` node — ``revise`` loops back to the
    agent, ``accept`` (and budget-exhausted) ends the run."""
    reflections = state.get("reflections", [])
    if reflections and reflections[-1].verdict == "revise":
        return "agent"
    return "__end__"


def _append_tail_human_message(messages: list[BaseMessage], block: str) -> list[BaseMessage]:
    """Stream L.L1 — append per-turn dynamic context as a tail
    ``HumanMessage`` so the leading ``SystemMessage`` stays byte-stable
    across turns (Mini-ADR L-1 — the Anthropic prompt-cache prefix
    invariant).

    The checkpointed ``state['messages']`` is left untouched: the
    injected context rides only in this per-call prompt, the same as
    the pre-L1 ``_merge_into_system`` helper.
    """
    return [*messages, HumanMessage(content=block)]


def _inject_plan(messages: list[BaseMessage], plan: Plan) -> list[BaseMessage]:
    """Render the plan (J.1) into the prompt as a tail HumanMessage.

    Before L1 the plan was concatenated into the leading SystemMessage,
    which would change the cache prefix on every step and disable
    Anthropic prompt caching. L1 moves the per-turn dynamic context
    out of system into a tail HumanMessage so ``system`` stays
    build-once / replay-verbatim.
    """
    rendered = render_plan(plan)
    # Stream CM-0 (N1) — gauge the recitation size to watch for plan bloat.
    _cm_recitation_chars.set(len(rendered))
    return _append_tail_human_message(messages, rendered)


#: RT-2 PR-2 (RT-ADR-10) — visible marker appended when a memory item is cut
#: at the injection budget boundary, so the model (and a transcript reader)
#: can see the item is incomplete rather than mistaking the cut for the end.
_MEMORY_TRUNCATION_MARKER = " [... truncated: memory injection token budget]"


def _truncate_to_tokens(text: str, max_tokens: int, count: Callable[[str], int]) -> str:
    """RT-2 PR-2 — cut ``text`` down to roughly ``max_tokens`` per ``count``.

    Proportional shrink with a bounded refinement loop: the estimator is
    a heuristic (tiktoken or chars//4), so exact-token precision is not
    the contract — the budget is approximate by design, and the marker's
    own few tokens ride on top.
    """
    keep = len(text)
    for _ in range(6):
        tokens = count(text[:keep])
        if tokens <= max_tokens:
            break
        # Proportional shrink; the min() guarantees progress even when
        # the estimate barely moves between iterations.
        keep = min(keep - 1, keep * max_tokens // max(tokens, 1))
        if keep <= 0:
            keep = 0
            break
    return text[:keep] + _MEMORY_TRUNCATION_MARKER


def _render_memory_lines(
    memories: Sequence[MemoryItem],
    *,
    token_budget: int,
    correction_token_budget: int,
    count: Callable[[str], int],
) -> list[str]:
    """RT-2 PR-2 (RT-ADR-10) — pick the memory item lines that fit the
    injection token budget.

    Items are considered in their existing order (already rerank/MMR
    ranked upstream) and rendered in that same order — the budget
    changes SELECTION, never ordering, so the default path (top_k=5
    ordinary memories, far under budget) renders byte-identical to the
    pre-budget logic.

    Two greedy passes (deer-flow guaranteed_categories shape):

    1. correction pass — user-corrected items (``confidence >= 1.0``:
       the M-4 correction API's EXCLUSIVE sentinel — the extraction
       write path caps LLM-scored confidence at 0.99
       (``memory._MAX_EXTRACTED_CONFIDENCE``) and ``MemoryItem.kind``
       has no correction value) get first claim on up to
       ``correction_token_budget`` tokens, so ordinary memories can
       never squeeze a user's explicit correction out of the block.
       Precise shape of the guarantee: within the reserve, corrections
       are packed greedily in rank order; one that overflows the
       REMAINING reserve is skipped — later, smaller corrections still
       get their guarantee — and falls through to the general pass,
       where with room left it can still land whole (the reserve is a
       floor, not a cap);
    2. general pass — every still-unselected item fills the rest of
       ``token_budget``: an item that fits is taken whole, the first
       item that does not fit is truncated to the remaining budget
       (visible marker) and the pass stops — later items are dropped.

    Truncated / dropped items are counted on
    ``expert_work_memory_injection_truncated_total{outcome}``.
    """
    lines = [f"- ({item.kind}) {item.content}" for item in memories]
    costs = [count(line) for line in lines]
    selected: dict[int, str] = {}

    def _greedy(indices: Sequence[int], budget: int, *, truncate_boundary: bool) -> int:
        spent = 0
        for idx in indices:
            room = budget - spent
            if room <= 0:
                break
            if costs[idx] <= room:
                selected[idx] = lines[idx]
                spent += costs[idx]
            elif truncate_boundary:
                selected[idx] = _truncate_to_tokens(lines[idx], room, count)
                _memory_injection_truncated_total.labels(outcome="truncated").inc()
                spent = budget
                break
            # else: guarantee pass — the item overflows the remaining
            # reserve; skip it (it falls through to the general pass) and
            # keep going, so later, smaller corrections still get their
            # guarantee (review MEDIUM-1).
        return spent

    corrections = [idx for idx, item in enumerate(memories) if item.confidence >= 1.0]
    spent = _greedy(
        corrections, min(correction_token_budget, token_budget), truncate_boundary=False
    )
    rest = [idx for idx in range(len(memories)) if idx not in selected]
    _greedy(rest, token_budget - spent, truncate_boundary=True)

    dropped = len(memories) - len(selected)
    if dropped:
        _memory_injection_truncated_total.labels(outcome="dropped").inc(dropped)
    return [selected[idx] for idx in sorted(selected)]


def _inject_memories(
    messages: list[BaseMessage],
    memories: list[MemoryItem],
    *,
    mode: Literal["per_session", "per_turn"] = "per_session",
    spotlight_nonce: str | None = None,
    # RT-2 PR-2 (RT-ADR-10) — defaults mirror ``LongTermMemorySpec``
    # (``injection_token_budget`` / ``correction_token_budget``); the factory
    # always passes the manifest-resolved values through the graph builder.
    token_budget: int = 2000,
    correction_token_budget: int = 500,
    estimator: TokenEstimator | None = None,
) -> list[BaseMessage]:
    """Render recalled long-term memories (J.3) into the prompt.

    ``mode='per_turn'`` (legacy J.3): append a HumanMessage at the tail
    every turn — same L1 rationale as :func:`_inject_plan`. The memory
    block's position shifts every turn as AI/Tool messages accumulate,
    so the Anthropic prompt cache cannot include it.

    ``mode='per_session'`` (Sprint #8 default, Mini-ADR U-8): insert
    the memory block once at messages position 1 (right after the
    user's task) with ``additional_kwargs["expert_work_cache_anchor"] = True``
    so the Anthropic adapter (Mini-ADR U-7) marks it with
    ``cache_control: ephemeral``. The prefix
    ``[system_payload, task, memories]`` is then cached across every
    turn of the session — long sessions stop paying full price for the
    memory block on every step.

    RT-2 PR-2 (RT-ADR-10): the block is bounded by ``token_budget`` —
    the recall path caps item COUNT only (``retrieve_top_k``), so a
    single oversized memory could otherwise blow up the block. See
    :func:`_render_memory_lines` for the selection rules (greedy in
    rank order, boundary truncation, correction guarantee). The budget
    is measured on the RAW item lines, before spotlighting — the PI-1b
    datamarking below inflates the wire size by a bounded constant
    factor, which the approximate budget deliberately ignores.
    """
    # Stream PI-1b — recalled memory is untrusted (an injection can be written
    # into a memory in an earlier session and recalled here). Spotlight the
    # item block (the expert-work-owned header stays trusted) so the model treats it
    # as data, not instructions.
    items = "\n".join(
        _render_memory_lines(
            memories,
            token_budget=token_budget,
            correction_token_budget=correction_token_budget,
            count=(estimator or CharTokenEstimator()).count,
        )
    )
    if spotlight_nonce:
        items = spotlight_untrusted(items, nonce=spotlight_nonce)
    body = "## Relevant memories from past sessions\n" + items

    if mode == "per_turn":
        return _append_tail_human_message(messages, body)

    # per_session: stable prefix slot + cache anchor metadata. The
    # block lands at position 1 so it sits right after the user task
    # (messages[0] is typically the SystemMessage placeholder for
    # in-graph state, but the provider builds ``system`` separately
    # from its first SystemMessage entry, so ``messages[1]`` is the
    # first non-system slot for downstream content).
    block = HumanMessage(
        content=body,
        additional_kwargs={"expert_work_cache_anchor": True},
    )
    if not messages:
        return [block]
    return [messages[0], block, *messages[1:]]


def _classify_tool_failure(
    tool_call: dict[str, Any],
    tool_message: ToolMessage,
    classified: ClassifiedToolError | None,
) -> ClassifiedToolError | None:
    """Resolve a single tool call's failure into a classification, if any.

    L-4's mutation classifier wins first (CM-B2): for a known mutation
    tool it carries the more actionable "the write did NOT land — don't
    assume the path has content" guidance + the path, whether the tool
    raised (error-path ``ToolMessage(status="error")``) or returned a
    success-looking message that didn't actually land. Any other failure
    falls back to the error-path ``classified`` from the catch site.
    Returns ``None`` for a genuine success (no mutation gap, no error).
    """
    outcome = classify_mutation(
        str(tool_call.get("name", "")),
        tool_call.get("args") or {},
        tool_message,
    )
    if outcome is not None and not outcome.landed:
        return classified_mutation_not_landed(
            tool_name=outcome.tool_name,
            summary=outcome.error or "mutation did not land",
            path=outcome.path,
        )
    return classified


def _build_recovery_advisory(failures: list[ClassifiedToolError]) -> HumanMessage:
    """Stream CM-1 (generalising L.L4) — render a ``<recovery-advisory>``
    HumanMessage from the classified tool failures of the previous tools
    batch (Mini-ADR CM-B2/CM-B4).

    Generalises L-4's ``<mutation-advisory>`` to every tool failure: each
    line carries the error class + summary + grounded recovery guidance,
    so the model neither claims success on failed calls nor retries them
    blindly. Lives as a HumanMessage (not SystemMessage) so the L1
    prompt-cache prefix invariant — ``system`` is build-once /
    replay-verbatim — stays intact.

    RT-2 PR-4 (RT-ADR-9) — this advisory is orchestrator-authored guidance
    that IS persisted into ``state["messages"]`` (so the next agent step
    sees it), unlike the plan / per_session-memory injections which ride
    the prompt view only. Being a ``type=human`` message in the checkpoint,
    it would otherwise surface as a spurious USER bubble in the conversation
    detail (the known CM-1 leak). ``expert_work_hide_from_ui`` marks it so the UI
    bubble view (``control_plane.transcript.read_turns`` with
    ``include_hidden=False``) filters it, while the durable record + the
    search/audit mirror stay faithful (RT-ADR-9, Option A) and it still
    reaches the model.
    """
    return HumanMessage(
        content=render_recovery_advisory(failures),
        additional_kwargs={"expert_work_hide_from_ui": True},
    )


def _should_continue(state: AgentState) -> Literal["tools", "__end__"]:
    last = state["messages"][-1]
    if _extract_tool_calls(last):
        return "tools"
    return "__end__"


def _after_tools(state: AgentState) -> Literal["agent", "__end__"]:
    """Route out of ``tools`` — Stream J.8.

    Ends the run (→ END) in two cases:

    * ``pending_approval`` set — the run paused at an approval gate
      (RunStatus.PAUSED); the checkpoint is what the resume endpoint
      re-invokes from.
    * ``approval_outcome == "rejected"`` — a declarative-gate reject
      vetoed the run; it terminates rather than looping back.

    A normal tools batch (and an agent-initiated ask_for_approval
    reject) loops back to ``agent``.
    """
    if state.get("pending_approval") or state.get("approval_outcome") == "rejected":
        return "__end__"
    return "agent"


def _extract_tool_calls(message: BaseMessage) -> list[dict[str, Any]]:
    """Return ``AIMessage.tool_calls`` if present, else empty list.

    LangChain represents tool_calls as a list of ``{name, args, id}``
    dicts; non-AI messages never carry them.
    """
    if not isinstance(message, AIMessage):
        return []
    raw = getattr(message, "tool_calls", None)
    if not raw:
        return []
    return cast(list[dict[str, Any]], raw)


@dataclass(frozen=True)
class _StructuredFinalize:
    """Result of the RT-ADR-4 finalization step (see ``agent_node``).

    ``structured_prompt`` is ``None`` when the free candidate already
    conformed (no resend happened); otherwise it is the exact prompt the
    schema-enforced resend was issued with — the bottom after-chain pass
    must account THAT call (prompt view + spec instance, § 7.4 store side).
    """

    response: AIMessage
    structured_prompt: list[BaseMessage] | None = None
    structured_cache_hit: bool = False
    primary_loop_detected: bool = False


def _with_parsed(message: AIMessage, parsed: dict[str, Any]) -> AIMessage:
    """Attach the validated dict without mutating the provider's message."""
    if message.additional_kwargs.get("parsed") == parsed:
        return message
    return message.model_copy(
        update={"additional_kwargs": {**message.additional_kwargs, "parsed": parsed}}
    )


def _reconcile_parsed_after_rewrite(response: AIMessage, spec: StructuredOutputSpec) -> AIMessage:
    """RT-1 PR-3 — re-derive ``parsed`` after a content rewrite (7.4 DLP).

    The redaction rewrote the reply text; a stale ``parsed`` would hand
    consumers the unredacted values. Re-validate the redacted content:
    still conforming → attach the redacted dict; no longer conforming (a
    redaction landed inside a pattern/enum-constrained field) → drop
    ``parsed`` entirely — the redacted text is authoritative and security
    wins over the schema contract on this turn.
    """
    parsed, _ = validate_structured_output(response, spec)
    kwargs = {k: v for k, v in response.additional_kwargs.items() if k != "parsed"}
    if parsed is not None:
        kwargs["parsed"] = parsed
    return response.model_copy(update={"additional_kwargs": kwargs})


async def _finalize_structured_response(
    *,
    candidate: AIMessage,
    prompt_messages: list[BaseMessage],
    spec: StructuredOutputSpec,
    caller: LLMCaller,
    token: CancellationToken,
    before_llm_chain: MiddlewareChain | None,
    after_llm_chain: MiddlewareChain | None,
    tenant_id: UUID | None,
    user_id: UUID | None,
    primary_cache_hit: bool,
    token_budget: TokenBudget | None = None,
) -> _StructuredFinalize:
    """RT-ADR-4 two-stage finalization (mechanism: ``agent_node`` comment).

    Stage 1 — validate the free candidate locally; conforming → attach
    ``parsed``, zero extra calls. Stage 2 — account the primary call on
    its own after-chain pass, then issue ONE schema-enforced resend
    (candidate + correction appended, ``tools=[]``) through its own
    before-anchor pass carrying the spec INSTANCE in
    ``payload["output_schema"]`` (§ 7.4 lookup side — an E.13 hit skips
    the LLM call). The resend's response is re-validated here so a cached
    entry is held to the same contract as a fresh router-validated one;
    a still-invalid response raises :class:`LLMOutputValidationError`.

    B3 — ``token_budget`` (when wired) only needs to hear about the STAGE 2
    call here: the primary candidate was already counted by the caller
    (``agent_node``) right after it resolved, before this function ever
    runs. Stage 1's zero-extra-call return therefore adds nothing more; a
    real stage 2 call (cache hit or fresh resend) is real spend and gets
    counted once, right after it resolves below.
    """
    parsed, error_summary = validate_structured_output(candidate, spec)
    if parsed is not None:
        _llm_structured_finalize_total.labels(outcome="conform").inc()
        return _StructuredFinalize(response=_with_parsed(candidate, parsed))

    # The bottom after-chain pass will carry the resend, so account the
    # primary call now — one after_llm_call pass per real upstream call
    # keeps G.9 token metering exactly-once, and stores the candidate
    # under the unstructured key its lookup used.
    primary_loop_detected = False
    if after_llm_chain is not None:
        primary_ctx = MiddlewareContext(
            payload={
                "messages": [*prompt_messages, candidate],
                "response": candidate,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "prompt_messages": prompt_messages,
                "cache_hit": primary_cache_hit,
            }
        )
        await after_llm_chain.invoke(primary_ctx, _noop)
        primary_loop_detected = bool(primary_ctx.payload.get("loop_detected"))

    assert error_summary is not None  # noqa: S101 - parsed is None ⇒ summary set
    finalize_messages: list[BaseMessage] = [
        *prompt_messages,
        candidate,
        HumanMessage(content=correction_message(error_summary, spec)),
    ]
    cache_hit: AIMessage | None = None
    if before_llm_chain is not None:
        before_ctx = MiddlewareContext(
            payload={
                "messages": finalize_messages,
                "tools": [],
                "tenant_id": tenant_id,
                # § 7.4 hard requirement — the spec INSTANCE, so the E.13
                # lookup keys with the schema fingerprint.
                "output_schema": spec,
            }
        )
        await before_llm_chain.invoke(before_ctx, _noop)
        finalize_messages = list(before_ctx.payload.get("messages", finalize_messages))
        hit = before_ctx.payload.get("llm_cache_hit")
        if isinstance(hit, AIMessage):
            # Lookup-side self-heal (belt half of the poisoning defence;
            # the store guard in ``agent_node`` is the suspenders): a
            # non-conforming entry under the structured key — however it
            # got there — must degrade to a real resend, never surface as
            # a hard LLMOutputValidationError on a turn that would
            # otherwise succeed. Only a VALIDATED hit takes the cache
            # branch; ignored hits fall through to the resend below.
            hit_parsed, _hit_error = validate_structured_output(hit, spec)
            if hit_parsed is not None:
                cache_hit = hit
            else:
                logger.warning(
                    "structured_finalize.poisoned_cache_hit_ignored schema=%s", spec.name
                )

    if cache_hit is not None:
        # Metric fidelity: ``cache_hit`` counts only a hit that VALIDATED;
        # the poisoned-hit fallback above lands in ``resend`` below.
        _llm_structured_finalize_total.labels(outcome="cache_hit").inc()
        response = cache_hit
    else:
        _llm_structured_finalize_total.labels(outcome="resend").inc()
        # Same span + cancellation discipline as the primary call (E.15 /
        # 10.1): a cancel mid-resend interrupts the in-flight await.
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "llm_call"):
            response = await token.run_cancellable(
                caller(messages=finalize_messages, tools=[], output_schema=spec)
            )

    # B3 — stage 2 spend (finding 2 fix): a resend is a second real billed
    # call the pre-hoist budget accounting never saw (it only ran once, on
    # the primary candidate, before this function was invoked). A cache hit
    # is counted too, matching the primary-call cache-hit convention in
    # ``agent_node`` (whatever ``usage_metadata`` the resolved response
    # carries gets counted, cached or not).
    if token_budget is not None:
        token_budget.add(usage_total(getattr(response, "usage_metadata", None)))

    parsed, error_summary = validate_structured_output(response, spec)
    if parsed is None:
        raise LLMOutputValidationError(
            f"structured finalization response failed validation "
            f"(schema={spec.name!r}): {error_summary}"
        )
    return _StructuredFinalize(
        response=_with_parsed(response, parsed),
        structured_prompt=finalize_messages,
        structured_cache_hit=cache_hit is not None,
        primary_loop_detected=primary_loop_detected,
    )


def _screen_model_response(response: AIMessage) -> tuple[AIMessage, tuple[str, ...]]:
    """Stream PI-2 — screen a model response; refuse a flagged one.

    Returns ``(response, ())`` when clean, else ``(refusal, categories)`` where
    the refusal is a fresh :class:`AIMessage` carrying **no tool_calls** (a
    blocked response must terminate the turn rather than proceed to a
    possibly-injected tool call) and ``categories`` are the fired categories for
    the audit row (audit-eval Phase 4). The matched value is never logged.
    """
    verdict = screen_output(str(response.content))
    if not verdict.blocked:
        return response, ()
    for category in verdict.categories:
        _output_screen_blocked_total.labels(category=category).inc()
    logger.warning("output_screen.blocked categories=%s", ",".join(verdict.categories))
    return AIMessage(content=REFUSAL_TEXT), tuple(verdict.categories)


def _dlp_redact_response(response: AIMessage) -> tuple[AIMessage, tuple[str, ...]]:
    """Stream 7.4 — redact PII in a terminal response (conditional output).

    Returns ``(response, ())`` when no PII matched, else ``(copy, categories)``
    with the matched spans replaced by ``[redacted]`` and the fired categories
    for the audit row (audit-eval Phase 4). Only string content is scanned
    (multimodal content blocks pass through, M2/M3 scope). The matched value is
    never logged — only the category that fired.
    """
    content = response.content
    if not isinstance(content, str):
        return response, ()
    result = scan_and_redact(content)
    if not result.changed:
        return response, ()
    for category in result.categories:
        _output_dlp_redacted_total.labels(category=category).inc()
    logger.info("output_dlp.redacted categories=%s", ",".join(result.categories))
    return response.model_copy(update={"content": result.redacted}), tuple(result.categories)


def _latest_human_text(messages: Sequence[BaseMessage]) -> str:
    """The most recent user-message text — the judge's alignment baseline."""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return str(msg.content)
    return ""


async def _judge_model_response(
    response: AIMessage,
    messages: Sequence[BaseMessage],
    *,
    judge: OutputJudge,
    on_error: Literal["open", "closed"],
    token: CancellationToken,
) -> AIMessage:
    """Stream PI-2b — judge a terminal response for alignment / leakage.

    Returns ``response`` when the judge clears it, else a refusal. A judge
    failure (timeout / outage) routes through ``on_error``: ``"open"`` lets the
    response through (best-effort backstop), ``"closed"`` blocks. The reason is
    logged at category level only — never the response text or any secret.
    """
    try:
        verdict = await token.run_cancellable(
            judge.judge(
                user_request=_latest_human_text(messages),
                response=str(response.content),
                context_hint=None,
            )
        )
    except Exception:
        _output_judge_total.labels(verdict="error").inc()
        if on_error == "closed":
            logger.warning("output_judge.failed policy=fail-closed -> blocking")
            return AIMessage(content=REFUSAL_TEXT)
        logger.warning("output_judge.failed policy=fail-open -> allowing")
        return response
    if verdict.blocked:
        label = "leak" if verdict.leak_suspected else "misaligned"
        _output_judge_total.labels(verdict=label).inc()
        logger.warning("output_judge.blocked verdict=%s reason=%s", label, verdict.reason)
        return AIMessage(content=REFUSAL_TEXT)
    _output_judge_total.labels(verdict="aligned").inc()
    return response


async def _first_misaligned_action(
    tool_calls: list[dict[str, Any]],
    messages: Sequence[BaseMessage],
    *,
    judge: ActionJudge,
    on_error: Literal["open", "closed"],
    token: CancellationToken,
) -> int | None:
    """Stream PI-3b — judge every proposed tool call; return the index of the
    first misaligned one (or ``None`` when all align).

    Records a per-call ``aligned`` / ``misaligned`` / ``error`` metric. A judge
    failure routes through ``on_error``: ``"open"`` treats the call as aligned
    (best-effort backstop), ``"closed"`` treats it as misaligned. Never logs
    the args.
    """
    user_request = _latest_human_text(messages)

    async def _judge(index: int, call: dict[str, Any]) -> tuple[int, bool]:
        name = str(call.get("name", ""))
        args = call.get("args") or {}
        try:
            verdict = await token.run_cancellable(
                judge.judge_action(user_request=user_request, tool_name=name, tool_args=args)
            )
        except Exception:
            _action_screen_total.labels(verdict="error").inc()
            return index, on_error == "closed"
        bad = verdict.blocked
        _action_screen_total.labels(verdict="misaligned" if bad else "aligned").inc()
        return index, bad

    # Judge every proposed call concurrently (bounded, mirroring tools_node's
    # dispatch below) rather than serially: the loop never early-exits, so this
    # is the same set of judge round trips at a fraction of the wall-clock. The
    # lowest bad index == the old first-in-order ``first_bad``.
    semaphore = asyncio.Semaphore(MAX_TOOL_WORKERS)

    async def _bounded(index: int, call: dict[str, Any]) -> tuple[int, bool]:
        async with semaphore:
            return await _judge(index, call)

    verdicts = await asyncio.gather(*(_bounded(i, c) for i, c in enumerate(tool_calls)))
    bad_indices = [index for index, bad in verdicts if bad]
    return min(bad_indices) if bad_indices else None


def _extract_post_llm_messages(
    ctx: MiddlewareContext,
    *,
    original: list[BaseMessage],
) -> list[BaseMessage]:
    """Decode what ``after_llm_call`` middlewares left in ``ctx``.

    Convention:
    - ``ctx.payload["messages"]`` is the updated message list; we
      return the suffix beyond the original prefix so LangGraph's
      ``add_messages`` reducer appends exactly the new tail.
    - If the chain returned a strictly-shorter list (e.g., E.10.5
      loop_detection rewrites the trailing AIMessage and appends a
      reminder), we return that list as-is — same-id messages cause
      ``add_messages`` to replace the prior copy rather than duplicate.
    """
    updated = ctx.payload.get("messages")
    if not isinstance(updated, list):
        response = ctx.payload.get("response")
        return [response] if isinstance(response, AIMessage) else []

    original_len = len(original) - 1  # exclude the freshly-appended response
    if len(updated) >= original_len:
        prefix_unchanged = updated[:original_len] == original[:original_len]
        if prefix_unchanged:
            return list(updated[original_len:])
    return list(updated)


# 10.1 follow-up — the trace detail panel can't show tool call args/result
# because the tool_call span never carries them. ``_record_tool_io`` fixes
# that: masked (the OTLP export path does NOT apply Langfuse's PII mask —
# spike-confirmed, so we mask manually with the same pattern union the
# Langfuse SDK mask uses) + capped (avoid huge Langfuse payloads) input/output
# on the two Langfuse-recognised OTel attribute keys.
_TOOL_IO_CAP = 8192
_LANGFUSE_OBS_INPUT_KEY = "langfuse.observation.input"
_LANGFUSE_OBS_OUTPUT_KEY = "langfuse.observation.output"
_tool_io_redactor = DefaultSecretRedactor(patterns={**DEFAULT_PATTERNS, **PII_PATTERNS})


def _record_tool_io(span: Any, args: Mapping[str, Any], result: Any) -> None:
    """Best-effort: give the tool_call span masked+capped input/output.

    The OTLP export path does not run Langfuse's PII mask (spike-confirmed
    with a planted secret), so this redacts manually before setting the
    attributes. This is a side-channel: any failure only drops observability
    data and must never block tool execution.
    """
    try:
        masked_in = _tool_io_redactor.redact_tree(dict(args))
        masked_out = _tool_io_redactor.redact_tree(str(result))
        in_text = json.dumps(masked_in, ensure_ascii=False)[:_TOOL_IO_CAP]
        out_text = str(masked_out)[:_TOOL_IO_CAP]
        span.set_attribute(_LANGFUSE_OBS_INPUT_KEY, in_text)
        span.set_attribute(_LANGFUSE_OBS_OUTPUT_KEY, out_text)
    except Exception:  # instrumentation side-channel, never blocks a run
        logger.warning("tool_span_io.record_failed", exc_info=True)


# R1 fix — ``_invoke_tool`` catches tool exceptions and returns a
# ``ToolMessage(status="error")`` instead of re-raising, so the span body
# never raises and ``expert_work_span``'s own exception path (which would
# otherwise set ``StatusCode.ERROR``) never fires. The tool_call span stays
# ``UNSET`` and Langfuse shows the failed call at ``level=DEFAULT``.
# ``_record_tool_error`` detects the error outcome inside the span block and
# sets the status explicitly, so Langfuse gets ``level=ERROR`` +
# ``status_message`` for the trace waterfall's error red-marking.
def _record_tool_error(span: Any, outcome: tuple[Any, ...]) -> None:
    """Best-effort: mark the tool_call span ERROR when the outcome failed.

    Side-channel like ``_record_tool_io`` — any failure only drops
    observability data and must never affect the tool's return path.
    """
    try:
        classified = outcome[3] if len(outcome) > 3 else None
        summary = getattr(classified, "summary", None) or str(outcome[0].content)[:200]
        span.set_status(Status(StatusCode.ERROR, summary))
    except Exception:  # instrumentation side-channel, never blocks a run
        logger.warning("tool_span_error.record_failed", exc_info=True)


async def _dispatch_tool(
    tool_call: dict[str, Any],
    registry: ToolRegistry,
    ctx: ToolContext,
    *,
    before_tool_dispatch_chain: MiddlewareChain | None,
    audit_logger: AuditLogger | None = None,
    overflow_writer: WorkspaceFileWriter | None = None,
    spotlight_nonce: str | None = None,
    budget_enabled: bool = True,
) -> tuple[ToolMessage, Mapping[str, Any], int, ClassifiedToolError | None]:
    """Dispatch one tool call.

    Returns ``(tool_message, state_updates, refund_iterations,
    classified_error)`` so the surrounding tools node can promote
    allowlisted ``state_updates`` keys (Stream K.K8) into the
    ``AgentState`` update dict, accumulate ``refund_iterations`` (Stream
    L.L5), and route ``classified_error`` into the CM-1
    ``<recovery-advisory>`` channel. ``state_updates`` is empty and
    refund is ``0`` for every code path that does not produce a
    successful :class:`~orchestrator.tools.registry.ToolResult` (errors,
    blocks, unknown tools); ``classified_error`` is ``None`` on the
    success path and set on every failure path.

    Stream TE-2 — each dispatch emits one ``TOOL_CALL`` audit row
    (``result=ERROR`` when the tool returns an error) or, when a
    pre-dispatch middleware blocks the call, one ``TOOL_BLOCKED`` row.
    The emit is best-effort: a missing ``audit_logger`` / ``tenant_id``
    is skipped and an audit-write failure is swallowed, so auditing never
    changes the dispatch result (mirrors ``sse._emit_run_end_audit``).
    """
    name = str(tool_call.get("name", ""))
    call_id = str(tool_call.get("id", ""))
    args = tool_call.get("args") or {}
    started = time.monotonic()

    try:
        if before_tool_dispatch_chain is not None:
            mw_ctx = MiddlewareContext(payload={"tool_name": name, "tool_args": dict(args)})
            await before_tool_dispatch_chain.invoke(mw_ctx, _noop)
            # Middlewares may rewrite tool_args (e.g., redact PII before
            # dispatch); tool_name is treated as immutable.
            args = mw_ctx.payload.get("tool_args", args) or {}

        tool = registry.get_required(name)
        # 10.1 — one ``expert_work.orchestrator.tool_call`` child span per tool
        # dispatch, attached under the session root span.
        with expert_work_span(
            ExpertWorkComponent.ORCHESTRATOR, "tool_call", attributes={"tool": name}
        ) as span:
            outcome = await _invoke_tool(
                tool,
                args,
                call_id,
                ctx,
                overflow_writer=overflow_writer,
                spotlight_nonce=spotlight_nonce,
                budget_enabled=budget_enabled,
            )
            _record_tool_io(span, args, outcome[0].content)
            if outcome[0].status == "error":
                _record_tool_error(span, outcome)
        ok = outcome[0].status != "error"
        # Stream HX-12 (Mini-ADR HX-I4) — call-through: the model called a
        # deferred name directly (it remembered the tool without a
        # find_tools round-trip). Dispatch already routes (TE-6 keeps
        # deferred tools in the lookup table); what was missing is the
        # promotion — without it the schema never enters the next turn's
        # bind and the model keeps calling blind. Piggyback the promote
        # on the tool's own state updates.
        if name in registry.deferred_names():
            message, state_updates, refund, classified = outcome
            merged_updates = dict(state_updates)
            promoted = merged_updates.get("promoted_tools")
            merged_updates["promoted_tools"] = [
                *(promoted if isinstance(promoted, list) else []),
                name,
            ]
            outcome = (message, merged_updates, refund, classified)
            promotion_events.labels(event="call_through").inc()
        duration_ms = _elapsed_ms(started)
        outcome[0].additional_kwargs["duration_ms"] = duration_ms
        _record_tool_metrics(name, started, "ok" if ok else "error")
        await _emit_tool_audit(
            audit_logger,
            ctx,
            name=name,
            call_id=call_id,
            args=args,
            path_args=tool.spec.path_args,
            from_skill=tool.spec.from_skill,
            action=AuditAction.TOOL_CALL,
            result=AuditResult.SUCCESS if ok else AuditResult.ERROR,
            reason=None if ok else "tool_error",
            duration_ms=duration_ms,
            # Stream 14.4 — MCP traffic audit: server + response volume.
            extra_details=_mcp_audit_details(
                name, content=str(outcome[0].content), is_error=not ok
            ),
        )
        return outcome
    except ToolNotFoundError as exc:
        logger.warning("tools.unknown_tool name=%s call_id=%s", name, call_id)
        duration_ms = _elapsed_ms(started)
        _record_tool_metrics(name, started, "error")
        await _emit_tool_audit(
            audit_logger,
            ctx,
            name=name,
            call_id=call_id,
            args=args,
            path_args=(),
            from_skill=None,
            action=AuditAction.TOOL_CALL,
            result=AuditResult.ERROR,
            reason="unknown_tool",
            duration_ms=duration_ms,
            extra_details=_mcp_audit_details(name, is_error=True),
        )
        # Stream HX-12 — a truly unknown name gets ranked suggestions from
        # the deferred pool instead of a dead-end error (fail-open: worst
        # case is the unchanged bare error).
        content = _format_error(exc)
        try:
            suggestions = [spec.name for spec in registry.search(name)[:3]]
        except Exception:
            suggestions = []
        if suggestions:
            content += (
                f" Did you mean: {', '.join(suggestions)}? "
                "Use find_tools to search for and load tools."
            )
        return (
            ToolMessage(
                content=content,
                tool_call_id=call_id,
                status="error",
                name=name,
                additional_kwargs={"duration_ms": duration_ms},
            ),
            {},
            0,
            classify_tool_error(tool_name=name, error=exc, spec=None),
        )
    except Exception as exc:
        # A pre-dispatch middleware (or the tool itself) may raise to block —
        # wrap so the LLM sees a normal error result rather than the run
        # crashing (Mini-ADR E-12).
        logger.warning(
            "tools.before_dispatch_blocked name=%s call_id=%s err=%s",
            name,
            call_id,
            type(exc).__name__,
        )
        duration_ms = _elapsed_ms(started)
        _record_tool_metrics(name, started, "blocked")
        await _emit_tool_audit(
            audit_logger,
            ctx,
            name=name,
            call_id=call_id,
            args=args,
            path_args=(),
            from_skill=None,
            action=AuditAction.TOOL_BLOCKED,
            result=AuditResult.DENIED,
            reason=type(exc).__name__,
            duration_ms=duration_ms,
            extra_details=_mcp_audit_details(name, is_error=True),
        )
        return (
            ToolMessage(
                content=_format_error(exc),
                tool_call_id=call_id,
                status="error",
                name=name,
                additional_kwargs={"duration_ms": duration_ms},
            ),
            {},
            0,
            classify_tool_error(tool_name=name, error=exc, blocked=True),
        )


def _elapsed_ms(started: float) -> int:
    """Whole milliseconds elapsed since a ``time.monotonic`` timestamp."""
    return int((time.monotonic() - started) * 1000)


def _metric_tool_label(name: str) -> str:
    """Bound the ``tool`` metric label (Stream TE-3).

    MCP tool names are server-defined (``mcp__<server>__<tool>``) and thus
    not bounded by anything we author — one server can expose dozens of
    tools. Collapse them to ``mcp:<server>`` so the label stays bounded by
    the (catalog-curated, pool-capped) server set (the ``mcp:`` label form is
    kept for dashboard stability — it is a Prometheus label, never a wire
    function name). The exact tool name is still recorded in the TE-2 audit
    row, the right home for unbounded identifiers. Builtin / manifest-authored
    HTTP / skill tool names are human-authored and finite-per-config, so they
    pass through unchanged.
    """
    parsed = parse_mcp_tool_name(name)
    if parsed is not None:
        return f"mcp:{parsed[0]}"
    return name


def _record_tool_metrics(name: str, started: float, outcome: str) -> None:
    """Emit per-tool Prometheus metrics for one dispatch (Stream TE-3).

    Unconditional (unlike the audit emit, which needs a tenant): every
    dispatch increments ``expert_work_tool_call_total{tool,outcome}`` and
    observes ``expert_work_tool_latency_seconds{tool}``. ``outcome`` is one of
    ``ok`` / ``error`` / ``blocked``; ``tool`` is normalised for cardinality
    via :func:`_metric_tool_label`.
    """
    label = _metric_tool_label(name)
    _tool_call_total.labels(tool=label, outcome=outcome).inc()
    _tool_latency_seconds.labels(tool=label).observe(time.monotonic() - started)


def _mcp_audit_details(
    name: str, *, content: str | None = None, is_error: bool = False
) -> dict[str, Any] | None:
    """Stream 14.4 — structured MCP traffic dimensions for the audit row.

    Returns ``None`` for non-MCP tools (the generic ``tool:call`` audit is
    unchanged). For an ``mcp__<server>__<tool>`` name it returns the server +
    bare tool as structured fields (so operators filter MCP traffic without
    parsing the name) plus, on the success path, ``response_chars`` — the size
    of the textified MCP response, a data-volume / exfil signal. The response
    CONTENT is never recorded (privacy / clear-text-logging), only its length.
    """
    parsed = parse_mcp_tool_name(name)
    if parsed is None:
        return None
    server, tool = parsed
    details: dict[str, Any] = {"mcp_server": server, "mcp_tool": tool, "mcp_is_error": is_error}
    if content is not None:
        details["response_chars"] = len(content)
    return details


#: Sandbox executors whose submitted code/command IS recorded into the audit
#: trail (a capped preview + a full-content sha256). This is the deliberate
#: "audit over blocking" trade for sandbox execution: the gVisor sandbox (read-
#: only rootfs, cap-drop, no-new-privileges, pids/mem/cpu caps, proxy-only egress)
#: is the real boundary, so we no longer denylist calls like ``subprocess.run``
#: (which a soffice/poppler skill legitimately needs) — instead every run is
#: traceable. See docs/design/sandbox-audit-evaluation.md.
_SANDBOX_CODE_ARGS: dict[str, tuple[str, ...]] = {
    "exec_python": ("code", "script"),
    "bash": ("command", "cmd"),
}
#: Cap the stored preview so an audit row stays bounded; the sha256 covers the
#: full content for forensic matching.
_CODE_PREVIEW_MAX = 4000


async def _emit_tool_audit(
    audit_logger: AuditLogger | None,
    ctx: ToolContext,
    *,
    name: str,
    call_id: str,
    args: Mapping[str, Any],
    path_args: tuple[str, ...],
    from_skill: str | None,
    action: AuditAction,
    result: AuditResult,
    reason: str | None,
    duration_ms: int,
    extra_details: Mapping[str, Any] | None = None,
) -> None:
    """Write one per-tool-call audit row (Stream TE-2).

    Best-effort and non-fatal: skipped when no ``audit_logger`` or no
    ``tenant_id`` (dev / unit-test path), and any write failure is logged
    and swallowed so auditing never breaks a tool dispatch.

    Privacy: ``details`` records the **argument names** and the declared
    path-arg **values** (filesystem paths) — never other raw argument values,
    which may carry PII / credentials (CodeQL clear-text-logging;
    [memory:feedback_codeql_clear_text_logging_secret_name]). The one
    exception is **sandbox executor code** (``exec_python`` / ``bash``): a
    capped preview + full-content sha256 are recorded as the traceability
    substitute for the removed call denylist (audit over blocking).
    """
    if audit_logger is None or ctx.tenant_id is None:
        return
    # The ENTIRE body — including the ``details`` build (``str(...)`` on
    # arbitrary arg keys / declared path values can in principle raise) —
    # is wrapped so this helper is genuinely total. On the success path the
    # call site sits inside ``_dispatch_tool``'s try whose ``except`` is the
    # middleware-block handler; an exception escaping here would otherwise
    # misclassify a successful dispatch as TOOL_BLOCKED (review HIGH).
    try:
        details: dict[str, Any] = {
            "tool": name,
            "call_id": call_id,
            "arg_keys": sorted(str(k) for k in args),
            "duration_ms": duration_ms,
        }
        if path_args:
            details["paths"] = [str(args[a]) for a in path_args if a in args]
        code_keys = _SANDBOX_CODE_ARGS.get(name)
        if code_keys is not None:
            for key in code_keys:
                value = args.get(key)
                if isinstance(value, str):
                    raw = value.encode("utf-8", "replace")
                    details["code_sha256"] = hashlib.sha256(raw).hexdigest()
                    details["code_bytes"] = len(raw)
                    details["code"] = (
                        value
                        if len(value) <= _CODE_PREVIEW_MAX
                        else value[:_CODE_PREVIEW_MAX] + "…(truncated)"
                    )
                    break
        if from_skill is not None:
            details["from_skill"] = from_skill
        if ctx.run_id is not None:
            details["run_id"] = str(ctx.run_id)
        # Stream 14.4 — MCP traffic dimensions (server / response volume), merged
        # last so the structured fields sit alongside the generic tool details.
        if extra_details:
            details.update(extra_details)
        await audit_logger.write(
            AuditEntry(
                tenant_id=ctx.tenant_id,
                actor_type="agent",
                actor_id=str(ctx.run_id) if ctx.run_id is not None else "agent",
                action=action,
                resource_type="tool",
                resource_id=name,
                result=result,
                reason=reason,
                details=details,
            )
        )
    except Exception:
        logger.exception("tools.audit_failed name=%s call_id=%s", name, call_id)


async def _emit_output_guard_audit(
    audit_logger: AuditLogger | None,
    tenant_id: object,
    *,
    action: AuditAction,
    result: AuditResult,
    categories: tuple[str, ...],
) -> None:
    """Durable audit row for an output-guard event (audit-eval Phase 4).

    PI-2 output screen blocks + 7.4 DLP redactions were previously metric-only;
    this records a per-event row (only the fired *categories*, never the matched
    value). Best-effort: no logger / no tenant / write failure never breaks the
    run. ``tenant_id`` is accepted loosely (str | UUID) and coerced.
    """
    if audit_logger is None or tenant_id is None:
        return
    try:
        tid = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
    except (TypeError, ValueError):
        return
    try:
        await audit_logger.write(
            AuditEntry(
                tenant_id=tid,
                actor_type="agent",
                actor_id="agent",
                action=action,
                resource_type="run",
                resource_id="agent",
                result=result,
                details={"categories": list(categories)},
            )
        )
    except Exception:
        logger.exception("output_guard.audit_failed action=%s", action)


async def _emit_binding_drift_audit(
    audit_logger: AuditLogger | None,
    tenant_id: UUID | None,
    *,
    run_id: object,
    thread_id: object = None,
) -> None:
    """Durable audit row for an RT-6 Tier A binding-drift veto (RT-ADR-19).

    The approved args digest failed re-verification before dispatch — the
    checkpointed tool_call no longer matches what the human approved. Never
    records the args themselves (only that drift occurred). ``thread_id`` is
    carried in details because ``run_id`` here is the *continuation* run, while
    the approval row + APPROVAL_DECIDED audit key on the original paused run —
    the thread id is the stable join back to the approval. Best-effort: no
    logger / no tenant / write failure never breaks the run.
    """
    if audit_logger is None or tenant_id is None:
        return
    try:
        details = {"thread_id": str(thread_id)} if thread_id is not None else {}
        await audit_logger.write(
            AuditEntry(
                tenant_id=tenant_id,
                actor_type="agent",
                actor_id="agent",
                action=AuditAction.APPROVAL_BINDING_DRIFT,
                resource_type="approval",
                resource_id=str(run_id) if run_id is not None else None,
                result=AuditResult.DENIED,
                reason="approved tool arguments changed before execution",
                details=details,
            )
        )
    except Exception:
        logger.exception("approval_binding_drift.audit_failed run_id=%s", run_id)


async def _emit_state_projected_audit(
    audit_logger: AuditLogger | None, ctx: ToolContext, *, written: tuple[str, ...]
) -> None:
    """Audit one ``DB→/workspace`` projection (Stream CM-0). Best-effort —
    a failed audit must not break the run. ``resource_type`` reuses the
    existing ``user_workspace`` (Mini-ADR CM-A6)."""
    if audit_logger is None or ctx.tenant_id is None:
        return
    try:
        details: dict[str, Any] = {"written": list(written)}
        if ctx.run_id is not None:
            details["run_id"] = str(ctx.run_id)
        await audit_logger.write(
            AuditEntry(
                tenant_id=ctx.tenant_id,
                actor_type="agent",
                actor_id=str(ctx.run_id) if ctx.run_id is not None else "agent",
                action=AuditAction.STATE_PROJECTED,
                resource_type="user_workspace",
                result=AuditResult.SUCCESS,
                details=details,
            )
        )
    except Exception:
        logger.exception("workspace_projection.audit_failed")


async def _project_workspace_state(
    factory: Callable[[ToolContext], WorkspaceFileWriter] | None,
    state: AgentState,
    ctx: ToolContext,
    audit_logger: AuditLogger | None,
) -> ProjectionResult | None:
    """Best-effort turn-end ``DB→/workspace`` projection (Stream CM-0).

    Renders ``AgentState.plan`` + recalled memories into PLAN.md / TODO.md /
    MEMORY.md and writes them through a per-turn :class:`WorkspaceFileWriter`
    (built from ``factory``), skipping when content is unchanged since
    ``last_projection_hash``. Never raises — projection must not break a run
    (Mini-ADR CM-A8) — returning ``None`` when disabled or on error."""
    if factory is None:
        return None
    try:
        result = await WorkspaceProjector(writer=factory(ctx)).project(
            plan=state.get("plan"),
            memories=state.get("recalled_memories") or [],
            last_digest=state.get("last_projection_hash"),
        )
    except Exception:
        logger.exception("workspace_projection.turn_failed")
        _cm_projection_total.labels(outcome="error").inc()
        return None
    if result.skipped:
        _cm_projection_total.labels(outcome="skipped").inc()
    elif result.written:
        _cm_projection_total.labels(outcome="projected").inc()
        await _emit_state_projected_audit(audit_logger, ctx, written=result.written)
    return result


def _filter_scheduling_tools(tools: list[ToolSpec], *, trigger_origin: bool) -> list[ToolSpec]:
    """Withhold manage_task from the LLM under a scheduler-triggered run so the
    model can't self-schedule (Spec 1 D-13). Applied to the final bind list,
    independent of which branch built it."""
    if not trigger_origin:
        return tools
    return [s for s in tools if s.name != "manage_task"]


def _build_tool_context(config: RunnableConfig, *, plan: Plan | None = None) -> ToolContext:
    """Lift tenant / user binding out of ``config["configurable"]`` into
    a :class:`ToolContext`. Missing values fall through as ``None`` —
    M0 dev / unit tests rarely supply tenant_id, and per-tenant tools
    (E.8 HTTP, E.9 MCP) handle the ``None`` case explicitly (deny-all).

    The run's :class:`CancellationToken` is threaded through too (Stream
    J.4) — ``cancellation_token`` returns a fresh, never-cancelled token
    when the config carries none, so the field is always populated.

    ``plan`` (Stream K.K8) carries the current ``AgentState.plan`` so the
    ``update_plan`` builtin can keep the original goal when revising
    steps. ``None`` for react-mode runs.
    """
    configurable = config.get("configurable") or {}
    tenant_id = _parse_uuid(configurable.get("tenant_id"))
    run_id = _parse_uuid(configurable.get("run_id"))
    user_id = _parse_uuid(configurable.get("user_id"))
    thread_id = _parse_uuid(configurable.get("thread_id"))
    trigger_origin = bool(configurable.get("trigger_origin", False))
    # Mini-ADR J-40 — global deadline lands in config["configurable"]
    # ["deadline_at"] (a ``time.monotonic`` timestamp). ``None`` when the
    # manifest carries no ``policies.run_deadline_s``.
    deadline_raw = configurable.get("deadline_at")
    deadline_at = float(deadline_raw) if isinstance(deadline_raw, int | float) else None
    # 1.3 Orchestrator-Worker — the per-run spawn budget is created once in
    # ``sse.run_agent`` and lives in config["configurable"]; ``None`` when the
    # feature is unwired. Read verbatim (mirrors cancellation_token).
    worker_spawn_budget = configurable.get("worker_spawn_budget")
    # 二期 PR3(spec P4)— the process-wide delegation concurrency gate is
    # injected once per run (or forwarded verbatim into a child's config by
    # ``_child_run._child_config``); read verbatim (mirrors worker_spawn_budget).
    delegation_gate = configurable.get(DELEGATION_GATE_KEY)
    # MCP-OAUTH (OA-3b-后续) — the caller's OAuth subject id (a string), kept
    # distinct from user_id so a child run can resolve the same per-user OAuth
    # pool. ``None`` when absent (no OAuth identity).
    oauth_raw = configurable.get("oauth_user_id")
    oauth_user_id = oauth_raw if isinstance(oauth_raw, str) and oauth_raw else None
    # B2 — worker 事件 sink(镜像 worker_spawn_budget 的 config 读取)。
    sink_raw = configurable.get(WORKER_EVENT_SINK_KEY)
    worker_event_sink = sink_raw if callable(sink_raw) else None
    # B3 — token 池 + guard sink(镜像同一读取惯例)。
    tb_raw = configurable.get(TOKEN_BUDGET_KEY)
    guard_raw = configurable.get(GUARD_SINK_KEY)
    return ToolContext(
        tenant_id=tenant_id,
        run_id=run_id,
        user_id=user_id,
        thread_id=thread_id,
        trigger_origin=trigger_origin,
        oauth_user_id=oauth_user_id,
        cancellation_token=cancellation_token(config),
        plan=plan,
        deadline_at=deadline_at,
        worker_spawn_budget=worker_spawn_budget,
        delegation_gate=delegation_gate,
        worker_event_sink=worker_event_sink,
        token_budget=tb_raw if isinstance(tb_raw, TokenBudget) else None,
        guard_sink=guard_raw if callable(guard_raw) else None,
    )


def _parse_uuid(raw: object) -> UUID | None:
    if isinstance(raw, UUID):
        return raw
    if isinstance(raw, str):
        try:
            return UUID(raw)
        except ValueError:
            return None
    return None


def _validate_tool_args(tool: Tool, args: Mapping[str, Any]) -> str | None:
    """2.2 — validate ``args`` against the tool's JSON Schema before dispatch.

    Returns ``None`` when valid (or when the tool declares no schema), else a
    concise, value-free message naming the offending paths + failed keyword.
    The args are LLM-generated and may be malformed (model slip or injection);
    catching them here gives the model a grounded fix-it signal instead of an
    opaque downstream crash. A malformed *schema* (the tool's own bug) is not
    allowed to block dispatch — we skip validation in that case.
    """
    schema = tool.spec.parameters
    if not schema:
        return None
    validator_cls = Draft202012Validator
    try:
        validator_cls.check_schema(schema)
    except SchemaError:
        return None
    errors = sorted(validator_cls(schema).iter_errors(args), key=lambda e: list(e.absolute_path))
    if not errors:
        return None
    # Name path + failed keyword only — never echo the offending value.
    parts = [f"{e.json_path} ({e.validator})" for e in errors[:5]]
    return "arguments failed schema validation: " + "; ".join(parts)


async def _invoke_tool(
    tool: Tool,
    args: dict[str, Any],
    call_id: str,
    ctx: ToolContext,
    *,
    overflow_writer: WorkspaceFileWriter | None = None,
    spotlight_nonce: str | None = None,
    budget_enabled: bool = True,
) -> tuple[ToolMessage, Mapping[str, Any], int, ClassifiedToolError | None]:
    # B2 — 让工具知道自己服务的 tool_call id(worker 帧挂前端工具卡用)。
    ctx = replace(ctx, tool_call_id=call_id)
    schema_error = _validate_tool_args(tool, args)
    if schema_error is not None:
        return (
            ToolMessage(
                content=f"[invalid args] {schema_error}",
                tool_call_id=call_id,
                status="error",
                name=tool.spec.name,
            ),
            {},
            0,
            classified_invalid_arguments(tool_name=tool.spec.name, summary=schema_error),
        )
    try:
        result = await tool.call(args, ctx=ctx)
    except Exception as exc:
        logger.warning(
            "tools.dispatch_failed name=%s call_id=%s err=%s",
            tool.spec.name,
            call_id,
            type(exc).__name__,
        )
        # CM-1 / CM-B3 — classify here, where the real exception (and the
        # tool's capability spec) are in hand, rather than re-parsing the
        # formatted ToolMessage downstream.
        classified = classify_tool_error(tool_name=tool.spec.name, error=exc, spec=tool.spec)
        return (
            ToolMessage(
                content=_format_error(exc),
                tool_call_id=call_id,
                status="error",
                name=tool.spec.name,
            ),
            {},
            0,
            classified,
        )
    # Stream CM-5 — recoverable compression: save an oversized result to the
    # workspace and let the LLM see a recoverable reference (the tool's own
    # truncated body, or a head+tail preview for tools that didn't pre-truncate)
    # instead of a dead end or a context blowup.
    replacement_body, footer, persist_path = await _externalize_tool_overflow(
        result, tool, call_id, ctx, overflow_writer, budget_enabled=budget_enabled
    )
    body = replacement_body if replacement_body is not None else result.content
    # Stream PI-1b — a tool's output is untrusted (web pages, MCP servers, files
    # an attacker can control = the classic indirect-injection vector). Spotlight
    # it so embedded instructions read as data. The expert-work-owned overflow footer
    # stays trusted (outside the fence).
    tool_content = spotlight_untrusted(body, nonce=spotlight_nonce) if spotlight_nonce else body
    content = tool_content + footer if footer is not None else tool_content
    # ``artifact`` surfaces the tool's structured metadata (``ToolResult.meta``
    # — e.g. ask_image's ``image_ref`` / VL usage, truncation flags) in the raw
    # event stream / audit / trace. It rides alongside ``content`` but is NOT
    # sent back to the LLM, so it never affects the model's input. Item 2 — when a
    # full copy was persisted, stash its path here so the CM-12 prune gate can
    # later collapse the result to a lossless reference.
    meta_artifact: dict[str, Any] = dict(result.meta) if result.meta else {}
    if persist_path is not None:
        meta_artifact[TOOL_RESULT_PATH_ARTIFACT_KEY] = persist_path
    artifact: dict[str, Any] | None = meta_artifact or None
    return (
        # ``name`` records which tool produced this result (for MCP tools,
        # ``mcp:server.tool``). LangChain leaves it null unless set, so the raw
        # ToolMessage, audit, and trace all lose the attribution otherwise.
        ToolMessage(content=content, tool_call_id=call_id, name=tool.spec.name, artifact=artifact),
        result.state_updates,
        result.refund_iterations,
        None,
    )


async def _externalize_tool_overflow(
    result: ToolResult,
    tool: Tool,
    call_id: str,
    ctx: ToolContext,
    writer: WorkspaceFileWriter | None,
    *,
    budget_enabled: bool = True,
) -> tuple[str | None, str | None, str | None]:
    """Externalize an oversized tool result to the workspace (Stream CM-5).

    Returns ``(replacement_body, footer, persist_path)``:

    * ``replacement_body`` — the in-context content to use INSTEAD of
      ``result.content`` (a head+tail preview), or ``None`` to keep
      ``result.content`` unchanged.
    * ``footer`` — the reference footer to append, or ``None``.
    * ``persist_path`` — the workspace path the full copy was written to (for the
      caller to stash in ``ToolMessage.artifact`` so the CM-12 prune gate can
      recover it), or ``None`` when nothing was written.

    Two trigger paths:

    1. **``full_content`` set** (bash / exec_python / http / mcp) — the tool
       already truncated ``content``; save the full rendering, keep the
       tool's truncated body, append the reference.
    2. **Generalized size budget** (tool-result-context-budget) — the tool did
       NOT set ``full_content`` but its ``content`` exceeds
       ``EXTERNALIZE_MIN_CHARS`` (e.g. ``web_search``). Save the content,
       replace the body with a head+tail preview, append the reference. This is
       what keeps many medium results (8x web_search) from accumulating into a
       context blowup.

    Best-effort (Mini-ADR CM-F5): a write failure never affects the run — the
    ``full_content`` path keeps the already-truncated body; the generalized path
    degrades to in-place head+tail truncation so context is still bounded.
    The reference footer is returned only after the write lands (it must never
    point at a file that does not exist). The fetch-back readers
    (:data:`EXEMPT_TOOLS`) are skipped — their source is cheaply re-readable, so
    externalizing them would just create a persist→read→persist loop (CM-F3).
    """
    if writer is None or tool.spec.name in EXEMPT_TOOLS:
        return None, None, None

    budget_on = budget_enabled
    if result.full_content is not None:
        # CM-5 (always on — the kill switch does not gate this older path).
        source = result.full_content
        replacement: str | None = None  # keep the tool's already-truncated body
        footer_mode = True
    elif budget_on and len(result.content) > EXTERNALIZE_MIN_CHARS:
        # #859 — generalized externalization: replace bulk with a preview.
        source = result.content
        replacement = make_preview(result.content)
        footer_mode = True
    elif budget_on and len(result.content) > PERSIST_MIN_CHARS:
        # Item 2 — persist floor: keep the full result in context (no preview, no
        # footer) but write a full copy so a later CM-12 prune can collapse it to
        # a lossless reference (path recorded in the ToolMessage artifact).
        source = result.content
        replacement = None
        footer_mode = False
    else:
        return None, None, None

    rel = overflow_rel_path(run_id=ctx.run_id, call_id=call_id, tool_name=tool.spec.name)
    try:
        await writer.write(rel=rel, content=clamp_overflow(source))
    except (asyncio.CancelledError, RunCancelledError):
        raise
    except Exception as exc:
        logger.warning(
            "tool.overflow_failed tool=%s rel=%s err=%s",
            tool.spec.name,
            rel,
            type(exc).__name__,
        )
        _cm_tool_overflow_total.labels(outcome="degraded", tool=tool.spec.name).inc()
        # Generalized path: bound context in-place (no file to reference).
        # full_content path: keep the tool's own truncated body. Persist path:
        # keep the full content (no path → prune falls back to a stub later).
        if replacement is not None:
            return fallback_truncate(result.content), None, None
        return None, None, None

    total_chars = len(source)
    _cm_tool_overflow_total.labels(outcome="externalized", tool=tool.spec.name).inc()
    _cm_tool_overflow_chars.set(total_chars)
    logger.info("tool.overflow tool=%s rel=%s chars=%d", tool.spec.name, rel, total_chars)
    footer = render_overflow_footer(rel=rel, total_chars=total_chars) if footer_mode else None
    return replacement, footer, rel


def _format_error(exc: BaseException) -> str:
    summary = str(exc)
    if len(summary) > _ERROR_SUMMARY_MAX_CHARS:
        summary = summary[:_ERROR_SUMMARY_MAX_CHARS] + "...[truncated]"
    return f"[tool error] {type(exc).__name__}: {summary}"
