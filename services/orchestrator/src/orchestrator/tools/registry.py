"""Tool Protocol + ``ToolRegistry`` — Stream E.6.

Concrete tool adapters (``web_search`` E.7, ``http`` E.8, ``mcp:*`` E.9,
``exec_python`` F.4) all implement :class:`Tool` and register here. The
ReAct graph (``orchestrator.graph_builder``) reads
:meth:`ToolRegistry.specs` to hand the LLM the list of callable tools,
and dispatches by name via :meth:`ToolRegistry.get`.

Tool ``call`` exceptions are wrapped into ``ToolMessage(error=...)`` by
the graph's ``tools`` node (per Mini-ADR E-12 in
[STREAM-E-DESIGN](../../../../../docs/streams/STREAM-E-DESIGN.md)) —
adapters can raise freely; the LLM sees the error as a tool result and
reasons about retry / different args / final answer.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from expert_work.protocol import Plan
from expert_work.runtime.cancellation import CancellationToken
from orchestrator.tools._budget import DelegationGate, WorkerSpawnBudget
from orchestrator.tools._guards import TokenBudget
from orchestrator.tools.ranking import build_document, rank_tools

#: Stream TE-1 — a tool's effect on the world. Descriptive metadata only in
#: TE-1; intended to drive the side-effect-aware scheduler / approval gate
#: (TE-4) and per-tool audit (TE-2). Three levels:
#: - ``read_only``: observes state only (file read, search) — intended to be
#:   safe to parallelise.
#: - ``reversible``: mutates recoverable state (write/overwrite an artifact or
#:   workspace file that can be re-written) — intended to serialise on path
#:   conflict.
#: - ``irreversible``: effects that cannot be cleanly undone (shell command,
#:   sending an email, destructive ops) — intended to be forced serial +
#:   approval-gated.
#: ``None`` on a :class:`ToolSpec` means "derive from ``is_read_only``" (see
#: :attr:`ToolSpec.resolved_side_effect`) so existing tools keep their behaviour.
SideEffectLevel = Literal["read_only", "reversible", "irreversible"]


#: Stream TE-6 — cap on a ``find_tools`` query length before it is compiled as
#: a regex. A model-derived pattern past this falls back to substring matching,
#: bounding the catastrophic-backtracking (ReDoS) surface.
_MAX_SEARCH_QUERY_LEN = 200


def _haystack(spec: ToolSpec) -> str:
    """Lower-cased ``name + description`` for Stream TE-6 substring matching."""
    return f"{spec.name}\n{spec.description}".lower()


@dataclass(frozen=True)
class ToolSpec:
    """Static descriptor of a tool — handed to the LLM for tool selection.

    ``is_read_only`` and ``path_args`` (Stream L.L6) feed the ReAct
    ``tools`` node's adaptive parallel scheduler. Read-only tools
    without overlapping paths run concurrently; conflicting calls
    serialise. Defaults (``False`` / ``()``) are deliberately
    conservative — a third-party tool that doesn't opt in stays on the
    sequential path it had before L6. See [STREAM-L-DESIGN § 3.L6](
    ../../../../../docs/streams/STREAM-L-DESIGN.md) + Mini-ADR L-6.

    ``side_effect`` and ``idempotent`` (Stream TE-1) add a richer
    side-effect classification consumed later by the side-effect-driven
    scheduler / approval gate (TE-4) and per-tool audit (TE-2). Both
    default to a value that preserves current behaviour: ``side_effect``
    derives from ``is_read_only`` via :attr:`resolved_side_effect` and
    ``idempotent`` defaults ``False``. See [STREAM-TE-DESIGN § TE-ADR-1](
    ../../../../../docs/streams/STREAM-TE-DESIGN.md).
    """

    name: str
    description: str
    #: JSON Schema for the tool's ``args`` parameter.
    parameters: Mapping[str, Any] = field(default_factory=dict)
    #: Stream L.L6 — when ``True``, multiple invocations of this tool
    #: (and concurrent invocations of other read-only tools) may run in
    #: parallel without conflict. Tools that mutate filesystem,
    #: ``AgentState``, sandbox, or any third-party state MUST keep the
    #: default ``False`` — they serialise against any tool that touches
    #: the same path (or against every other call, when no path is
    #: declared).
    is_read_only: bool = False
    #: Stream L.L6 — argument names whose values are filesystem-like
    #: paths the tool reads or writes. The scheduler detects conflicts
    #: between two tool calls by comparing the resolved values of
    #: these args. Empty tuple means "this tool has no per-call path";
    #: combined with ``is_read_only=False`` that yields the worst-case
    #: "conflicts with every other tool" stance (e.g., ``update_plan``
    #: writes ``AgentState.plan``, a global channel).
    path_args: tuple[str, ...] = ()
    #: Mini-ADR J-40 (J.4-补强-2) — a tool that **mutates** but whose
    #: invocations are nevertheless independent of one another. Multiple
    #: calls to such a tool (including against the same name) may share
    #: a stage and run via ``asyncio.gather``. ``SubAgentTool`` is the
    #: canonical example: each delegation spins up a fresh child
    #: ``thread_id`` / sandbox session, so two sub-agent calls don't
    #: collide. Defaults to ``False`` — third-party tools stay on the
    #: ``is_read_only`` path.
    is_parallel_safe: bool = False
    #: Stream J.7a (Mini-ADR J-23) — name of the skill that contributed
    #: this tool to the agent's registry, or ``None`` when the tool is
    #: declared directly in the manifest's ``tools:`` block. The dispatch
    #: path uses this to label the ``expert_work_skill_call_total`` /
    #: ``expert_work_skill_call_errors_total`` metrics so per-skill usage can
    #: be observed (Mini-ADR J-23 § 15.4 telemetry 双 counter).
    from_skill: str | None = None
    #: Stream TE-1 — explicit side-effect classification. ``None`` means
    #: "derive from ``is_read_only``" (see :attr:`resolved_side_effect`),
    #: which keeps every existing tool's behaviour unchanged. A tool whose
    #: effects cannot be cleanly undone (e.g. ``bash``, ``send_email``,
    #: destructive MCP ops) declares ``"irreversible"`` so the TE-4
    #: scheduler forces it serial and the approval gate triggers on it.
    #: This is purely descriptive metadata until TE-4 wires it into
    #: scheduling/gating — TE-1 adds no behavioural change.
    side_effect: SideEffectLevel | None = None
    #: Stream TE-1 — whether repeating this call with the same args is safe
    #: (no additional effect). Read-only tools are inherently idempotent;
    #: a write that overwrites to a fixed content is too, but e.g. an
    #: "append" or "send" is not. Conservative default ``False``. Reserved
    #: for retry/self-correction logic; not yet consumed in TE-1.
    idempotent: bool = False
    #: Stream HX-13 (Mini-ADR HX-J2) — vendor-native disclosure marker, set
    #: per-bind by ``agent_node`` (via ``dataclasses.replace`` copies, never
    #: on the registry's own specs). One flag serves both vendor tiers:
    #: Anthropic sends marked tools with ``defer_loading: true`` (server-side
    #: tool search); OpenAI/Azure excludes marked tools from the
    #: ``tool_choice.allowed_tools`` subset (full schema stays on the wire).
    #: Default ``False`` — the application tier (HX-12) never sets it.
    defer_loading: bool = False

    @property
    def resolved_side_effect(self) -> SideEffectLevel:
        """Effective side-effect level: explicit value, else derived.

        When :attr:`side_effect` is unset, derive a conservative level
        from :attr:`is_read_only` so legacy tools that never declared a
        level still classify correctly: read-only tools are ``read_only``,
        everything else is ``reversible`` (not ``irreversible`` — a tool
        must opt in to the gated tier explicitly, preserving today's
        behaviour where no tool is auto-gated).
        """
        if self.side_effect is not None:
            return self.side_effect
        return "read_only" if self.is_read_only else "reversible"


@dataclass(frozen=True)
class ToolContext:
    """Per-invocation context threaded from the ReAct ``tools`` node.

    Most fields are optional because E.6 / E.7 tools didn't need any
    of them; E.8 HTTPTool is the first to require ``tenant_id`` (for
    the per-tenant allowlist lookup). Future tools read ``run_id`` for
    audit attribution. ``user_id`` (Stream J.15) scopes ``exec_python``'s
    persistent workspace volume — ``None`` when the run has no user
    binding. ``cancellation_token`` (Stream J.4) lets a tool propagate
    the run's cancellation into work it spawns — notably ``SubAgentTool``
    threading it into a child agent run.
    """

    tenant_id: UUID | None = None
    run_id: UUID | None = None
    user_id: UUID | None = None
    #: originating conversation thread — set on the normal + trigger run paths;
    #: manage_task stamps it as a task's delivery target (Spec 1).
    thread_id: UUID | None = None
    #: True when this run was started by the scheduler (fire_trigger). The
    #: manage_task tool is filtered from the LLM bind list and refuses to run
    #: under it (self-scheduling guardrail, Spec 1 D-13).
    trigger_origin: bool = False
    #: Stream MCP-OAUTH (OA-3b-后续) — the caller's OAuth subject id (the JWT
    #: ``sub`` / ``mcp_oauth_connection.user_id``), distinct from ``user_id``
    #: (the ``tenant_user.id`` UUID). Carried so a ``SubAgentTool`` /
    #: ``spawn_worker`` child can resolve the SAME per-user OAuth pool as the
    #: parent — the child build keys its OAuth pool on this, not ``user_id``.
    #: ``None`` when the caller has no OAuth identity (service principals).
    oauth_user_id: str | None = None
    cancellation_token: CancellationToken | None = None
    #: Stream K.K8 / P3 — current plan. Set by the planner node
    #: (``plan_execute``) or by the ``update_plan`` tool, which any agent can
    #: call to create or revise a plan. ``update_plan`` keeps ``plan.goal`` on
    #: a revise unless a new goal is supplied. ``None`` only before the first
    #: plan is established.
    plan: Plan | None = None
    #: Mini-ADR J-40 (J.4-补强-2) — wall-clock deadline (``time.monotonic``
    #: timestamp) for the *current run including any sub-agent recursion*.
    #: Established once in ``sse.run_agent`` from the manifest's
    #: ``policies.run_deadline_s``; ``SubAgentTool`` propagates the value
    #: to child config unchanged (child does not reset). A tool that
    #: opts in checks ``deadline_at - time.monotonic() <= 0`` before
    #: doing expensive work and short-circuits with a cancel. ``None``
    #: when no deadline is configured.
    deadline_at: float | None = None
    #: 1.3 Orchestrator-Worker — the per-run dynamic-worker spawn budget
    #: (cumulative count cap + concurrency semaphore), created once per run
    #: and shared across every ``spawn_worker`` call. ``None`` when the
    #: feature is unwired (tests / eval) — workers still run, bounded by
    #: depth + iteration cap + deadline + the per-tenant quota engine.
    worker_spawn_budget: WorkerSpawnBudget | None = None
    #: B2 worker 可观测性 — async sink publishing ``worker`` SSE frames into
    #: the parent run's bridge + event store. Injected per-run by
    #: ``sse.run_agent`` via ``WORKER_EVENT_SINK_KEY``; ``None`` when unwired
    #: (tests / eval) — child runs then emit no frames.
    worker_event_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    #: B2 — id of the tool_call this invocation serves (AIMessage
    #: ``tool_calls[].id``), set per-dispatch in ``_invoke_tool`` via
    #: ``dataclasses.replace``. Worker frames carry it so the frontend can
    #: attach the worker sub-timeline to the pending tool card.
    tool_call_id: str | None = None
    #: B3 — 全树共享 token 池(run_agent 注入,None=未启用)。
    token_budget: TokenBudget | None = None
    #: B3 — guard marker 帧 sink(下传给子树,None=未接线)。
    guard_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None
    #: 二期 PR3(spec P4)— process-wide delegation concurrency gate (subagent
    #: + spawn_worker), a single process-level singleton shared across every
    #: run and every delegation depth (下传给子树,见 ``_child_run._child_config``).
    #: Injected per-run by ``sse.run_agent`` via ``DELEGATION_GATE_KEY``;
    #: ``None`` when unwired (tests / eval / no config service) — delegations
    #: run ungated, same as before this gate existed.
    delegation_gate: DelegationGate | None = None


#: Stream K.K8 — keys a tool is allowed to write back to ``AgentState``
#: via :attr:`ToolResult.state_updates`. Limiting the set prevents a tool
#: from inadvertently rewriting unrelated channels (``messages``,
#: ``step_count`` …); add a key here when a new tool needs to mutate a
#: specific channel.
#:
#: Channels:
#: - ``plan`` — Stream J.1 / K.K8 ``update_plan``
#: - ``subagent_invocations`` — Stream J.4-补强-2 / Mini-ADR J-40
#:   ``SubAgentTool`` appends one :class:`SubAgentInvocation` per
#:   delegation outcome (the state.py channel uses ``operator.add``).
#: - ``promoted_tools`` — Stream TE-6 ``find_tools`` writes the names of
#:   deferred tools it just retrieved so the next ``agent_node`` adds them
#:   to the LLM bind (the state.py channel uses ``_merge_promoted`` to
#:   union-dedupe across turns).
TOOL_ALLOWED_STATE_KEYS: frozenset[str] = frozenset(
    {"plan", "subagent_invocations", "promoted_tools"}
)


@dataclass(frozen=True)
class ToolResult:
    """Result of a successful tool dispatch.

    ``content`` is fed back to the LLM as a ``ToolMessage`` body.
    ``meta`` carries truncation flags and any per-tool metadata (per
    Mini-ADR E-10 — caller knows e.g. ``meta.truncated=True`` ↔ output
    was cut).

    ``state_updates`` (Stream K.K8) is the narrow channel through which
    a tool may write back to :class:`AgentState`. The tools node
    promotes only keys in :data:`TOOL_ALLOWED_STATE_KEYS`; other keys
    are silently dropped (so a malformed or compromised tool can't
    rewrite ``messages`` or ``step_count``).

    ``refund_iterations`` (Stream L.L5 / Mini-ADR L-5) lets a tool ask
    the ReAct loop to refund iterations from the agent's ``step_count``
    budget. Internal-chain tools like ``update_plan`` (K.K8) shouldn't
    burn user-visible budget for housekeeping calls. The tools node
    accumulates this across the batch into
    ``step_count_refund_pending``; the next agent node subtracts it
    before computing the new ``step_count`` (clamped at 0 — refund
    never produces negative). Must be ``>= 0`` — a tool can't reverse
    the polarity and *consume* budget through this channel.

    ``full_content`` (Stream CM-5, Mini-ADR CM-F1/F3) carries the
    complete un-truncated rendering when ``content`` was cut, so the
    tools node can externalize it to the user's workspace and leave a
    recoverable reference instead of losing the overflow forever. Only
    tools whose output is otherwise unrecoverable may set it (bash /
    exec_python / http / mcp); read-only tools must leave it ``None`` —
    their sources are re-readable, and the exemption is the
    persist→read→persist loop guard.
    """

    content: str
    meta: Mapping[str, Any] = field(default_factory=dict)
    state_updates: Mapping[str, Any] = field(default_factory=dict)
    refund_iterations: int = 0
    full_content: str | None = None

    def __post_init__(self) -> None:
        # Frozen dataclass — direct setattr is disabled. The check runs
        # at construction time so a misbehaving tool fails loudly rather
        # than silently corrupting the agent's iteration budget.
        if self.refund_iterations < 0:
            msg = (
                f"ToolResult.refund_iterations must be >= 0 (got "
                f"{self.refund_iterations}); a tool cannot consume the "
                f"agent's iteration budget through this channel."
            )
            raise ValueError(msg)


@runtime_checkable
class Tool(Protocol):
    """Async callable wrapped with its static spec.

    ``spec`` is declared read-only so both a plain attribute (MCPTool's
    ``field(init=False)``) and a ``@property`` (WebSearchTool / HTTPTool)
    satisfy the Protocol.
    """

    @property
    def spec(self) -> ToolSpec:
        """The tool's static descriptor — handed to the LLM for selection."""

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        """Dispatch the tool with the given args and return a
        :class:`ToolResult`. ``ctx`` carries tenant binding etc. so
        per-tenant policies (E.8 allowlist, F.6 secret resolution) can
        run inside the tool. Implementations may raise; the ReAct graph's
        tools node wraps any exception into a ``ToolMessage(status='error')``
        (Mini-ADR E-12) — never let it propagate to the runner."""


class ToolNotFoundError(KeyError):
    """Raised by :meth:`ToolRegistry.get_required` when ``name`` isn't
    registered. The graph's ``tools`` node turns this into a
    ``ToolMessage(error=...)`` rather than propagating."""


class ToolBlockedError(RuntimeError):
    """Raised when a tool's policy denies the call (e.g. URL not in
    the per-tenant HTTP allowlist; tenant_id missing for a
    tenant-scoped tool). The graph's ``tools`` node wraps it into a
    ``ToolMessage(status='error')`` per Mini-ADR E-12 and the
    surrounding orchestrator writes a ``tool:blocked`` audit row."""


class ToolRegistry:
    """In-memory tool catalogue.

    M0 instantiates one per ``orchestrator`` process at startup;
    register all tools available to any agent. Per-agent / per-tenant
    filtering (``http_tool_allowlist`` / ``mcp_servers``) happens at
    dispatch / spec-resolution time — the registry itself is just a
    lookup table.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        #: Stream TE-6 — names of tools registered as *deferred* (the tool
        #: RAG mechanism). Deferred tools are excluded from :meth:`specs`
        #: (so they don't bloat every turn's LLM ``tools`` list) and are
        #: surfaced only via :meth:`search` / ``find_tools``. They stay
        #: dispatchable through :meth:`get` once promoted.
        self._deferred: set[str] = set()
        #: Stream HX-12 — tool provenance ("builtin" / "skill" /
        #: "mcp:<server>"), shown in find_tools results so the model can
        #: tell where a capability comes from.
        self._sources: dict[str, str] = {}
        #: Stream HX-12 — lazily-built BM25 corpus over the deferred pool;
        #: invalidated on every register (cheap: rebuilt on next search).
        self._ranking_corpus: list[tuple[str, list[str]]] | None = None

    def register(self, tool: Tool, *, deferred: bool = False, source: str | None = None) -> None:
        """Register a tool by its spec ``name``. Re-registering replaces.

        Stream TE-6 — ``deferred=True`` marks the tool as *latent*: it is
        omitted from :meth:`specs` (the per-turn LLM bind) and exposed only
        through :meth:`search` until ``find_tools`` promotes it. The tool
        remains fully dispatchable via :meth:`get` / :meth:`get_required`
        so a promoted call still routes. Default ``False`` keeps every
        existing tool active — zero behaviour change.

        Stream HX-12 — ``source`` records provenance for find_tools result
        labelling (``"mcp:<server>"`` / ``"skill"``; default ``"builtin"``).
        """
        name = tool.spec.name
        self._tools[name] = tool
        if deferred:
            self._deferred.add(name)
        else:
            # Re-registering a previously-deferred name as active un-defers it.
            self._deferred.discard(name)
        if source is not None:
            self._sources[name] = source
        self._ranking_corpus = None

    def source_of(self, name: str) -> str:
        """Provenance label for find_tools listings (Stream HX-12)."""
        return self._sources.get(name, "builtin")

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def get_required(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            msg = f"unknown tool: {name!r}"
            raise ToolNotFoundError(msg)
        return tool

    def specs(self) -> list[ToolSpec]:
        """Active (non-deferred) specs in registration order — handed to the LLM.

        Stream TE-6 — deferred tools are excluded so they don't inflate the
        per-turn ``tools`` list (Context Bloat). With no deferred tools this
        returns every registered spec, identical to pre-TE-6 behaviour.
        """
        return [tool.spec for name, tool in self._tools.items() if name not in self._deferred]

    def all_specs(self) -> list[ToolSpec]:
        """Every registered spec — active *and* deferred — in registration order.

        Stream TE-6 — scheduling / approval-gating classify by spec, so they
        must see deferred tools too (a deferred irreversible tool stays gated
        once promoted, and a promoted tool still schedules correctly). With no
        deferred tools this equals :meth:`specs`.
        """
        return [tool.spec for tool in self._tools.values()]

    def deferred_specs(self, names: Iterable[str]) -> list[ToolSpec]:
        """Specs for the given ``names`` that are actually deferred.

        Stream TE-6 — ``agent_node`` calls this with the run's promoted-tool
        names to add just-retrieved deferred tools to the LLM bind. Names that
        aren't registered or aren't deferred (e.g. an already-active tool) are
        dropped. Order follows ``names``.
        """
        out: list[ToolSpec] = []
        for name in names:
            if name in self._deferred:
                tool = self._tools.get(name)
                if tool is not None:
                    out.append(tool.spec)
        return out

    def search(self, query: str) -> list[ToolSpec]:
        """Retrieve matching *deferred* tool specs for ``find_tools`` (Stream TE-6).

        Active tools are never returned — they're already in the bind, so
        there's nothing to retrieve. The query syntax mirrors deer-flow's
        ``tool_search``, with HX-12 adding a ranked natural-language mode:

        - ``select:a,b,c`` — exact name match (comma-separated).
        - ``+keyword rest...`` — require ``keyword`` (case-insensitive) in the
          name or description, then further filter by every remaining word.
        - otherwise — BM25-ranked retrieval over names / descriptions /
          parameter names (CJK-aware, best match first; Stream HX-12). A
          zero-overlap query falls back to the pre-HX-12 regex / substring
          path, so retrieval is never worse than before.
        """
        # Iterate ``_tools`` (registration-ordered) so results are
        # deterministic; ``_deferred`` is an unordered set.
        candidates = [tool.spec for name, tool in self._tools.items() if name in self._deferred]
        stripped = query.strip()
        if not stripped:
            return []

        if stripped.startswith("select:"):
            wanted = {n.strip() for n in stripped[len("select:") :].split(",") if n.strip()}
            return [spec for spec in candidates if spec.name in wanted]

        if stripped.startswith("+"):
            terms = stripped[1:].split()
            if not terms:
                return []
            lowered_terms = [t.lower() for t in terms]
            return [
                spec
                for spec in candidates
                if all(term in _haystack(spec) for term in lowered_terms)
            ]

        # Stream HX-12 — ranked natural-language retrieval first; an empty
        # result (zero lexical overlap) drops to the legacy path below.
        ranked_names = rank_tools(stripped, self._ranking_documents())
        if ranked_names:
            by_name = {spec.name: spec for spec in candidates}
            return [by_name[name] for name in ranked_names if name in by_name]

        # Stream TE-6 — the query is model-derived; an over-long pattern is the
        # ReDoS surface (catastrophic backtracking). Cap it: anything past the
        # limit degrades to a plain substring match (never compiled as a regex).
        if len(stripped) > _MAX_SEARCH_QUERY_LEN:
            needle = stripped[:_MAX_SEARCH_QUERY_LEN].lower()
            return [spec for spec in candidates if needle in _haystack(spec)]
        try:
            pattern = re.compile(stripped, re.IGNORECASE)
        except re.error:
            needle = stripped.lower()
            return [spec for spec in candidates if needle in _haystack(spec)]
        return [
            spec
            for spec in candidates
            if pattern.search(spec.name) or pattern.search(spec.description)
        ]

    def _ranking_documents(self) -> list[tuple[str, list[str]]]:
        """Lazily (re)build the BM25 corpus over the deferred pool (HX-12)."""
        if self._ranking_corpus is None:
            self._ranking_corpus = [
                (
                    name,
                    build_document(
                        name,
                        tool.spec.description,
                        list((tool.spec.parameters or {}).get("properties", {}) or {}),
                    ),
                )
                for name, tool in self._tools.items()
                if name in self._deferred
            ]
        return self._ranking_corpus

    def has_deferred(self) -> bool:
        """Whether any tool is registered deferred (Stream TE-6).

        The assembler uses this to auto-register ``find_tools`` only when
        there is something to discover (TE-6b)."""
        return bool(self._deferred)

    def deferred_names(self) -> list[str]:
        """Names of every deferred tool, in registration order (Stream HX-12).

        The assembler's small-pool escape hatch iterates these to estimate
        the pool's schema size and, under the threshold, re-register each
        tool active."""
        return [name for name in self._tools if name in self._deferred]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __len__(self) -> int:
        return len(self._tools)
