"""The runnable build artefact — a neutral leaf module (B-26 follow-up).

:class:`BuiltAgent` used to live in ``agent_factory``, but the delegation
tool modules (``tools/subagent`` / ``tools/spawn_worker`` / ``tools/_child_run``
/ ``tools/assembly``) need the name for their builder protocols' return types
while ``agent_factory`` imports those same modules to assemble the registry —
a cyclic import (CodeQL py/unsafe-cyclic-import; the query does not model
``TYPE_CHECKING`` guards, so guarded imports count as cycle edges too). This
module imports nothing from ``agent_factory`` or ``orchestrator.tools`` at
runtime, so both sides can depend on it; ``agent_factory`` keeps re-exporting
the name, existing importers are untouched.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langgraph.graph.state import CompiledStateGraph

from expert_work.common.skill_run_usage import BoundDistilledSkill
from expert_work.protocol import PromptVariableSpec

if TYPE_CHECKING:
    # Type-only so this module stays a leaf (no runtime edge into
    # orchestrator.tools). The dataclass field annotation stays a string
    # (``from __future__ import annotations``) and is never resolved.
    from orchestrator.tools.registry import ToolCatalogEntry


@dataclass(frozen=True)
class BuiltAgent:
    """The runnable artefacts the worker / control-plane needs.

    ``graph`` is invoked via ``astream``; ``system_prompt`` and
    ``max_steps`` seed the initial ``AgentState`` (the factory builds
    the graph, the caller builds each run's input).
    """

    graph: CompiledStateGraph[Any, Any, Any, Any]
    system_prompt: str
    max_steps: int
    #: Whether the main model accepts image content blocks (J.6 Path A).
    #: The control-plane run assembler uses this to decide whether to
    #: emit a multimodal ``HumanMessage`` or a plain-text one.
    supports_vision: bool = False
    #: Mini-ADR J-40 (J.4-补强-2) — wall-clock cap on the whole run
    #: including sub-agent recursion, in seconds. ``0`` disables the
    #: deadline. ``sse.run_agent`` reads this to compute
    #: ``deadline_at = time.monotonic() + run_deadline_s`` once per run.
    run_deadline_s: int = 0
    #: No-progress stop — consecutive loop-detection trips after which the
    #: ReAct loop force-wraps up early (0 = off). Seeds ``max_no_progress``
    #: in the initial ``AgentState``; mirrors ``max_steps``.
    max_no_progress: int = 0
    #: Stream SE (SE-7d-3b-ii) — distilled skill versions bound into this agent
    #: at build time. The run carries these to its finalization hook so the
    #: rollback monitor can attribute each run's outcome to the versions it used.
    #: Only distilled (auto-promotable) versions — human skills never roll back.
    bound_distilled_skills: tuple[BoundDistilledSkill, ...] = ()
    #: Stream HX-3 (Mini-ADR HX-C2) — capability resolver for the run-retry
    #: replay-safety guard: whether re-dispatching the named tool is safe
    #: (CM-B5 rule: ``read_only`` or ``idempotent``). Closes over this
    #: build's tool registry; unknown names resolve unsafe (fail-closed).
    tool_replay_safe: Callable[[str], bool] | None = None
    #: Stream PI-1c — the per-build spotlight nonce (same value the graph
    #: uses to fence tool/RAG/memory). ``None`` when spotlighting is off.
    #: The control-plane run assembler reuses it to fence structured
    #: ``untrusted_content`` seed input with the matching marker, so inline
    #: data shares one provenance fence with the model-side channels.
    spotlight_nonce: str | None = None
    #: Stream Dynamic-Prompt — opt-in run-time Jinja rendering of the system
    #: prompt. ``prompt_jinja`` off (default) → the control-plane uses
    #: ``system_prompt`` verbatim (byte-identical, cache intact). On → it
    #: renders ``prompt_base`` (the human-authored template) with the run's
    #: ``inputs`` against ``prompt_variables`` and appends ``prompt_suffix``
    #: (the platform-computed spotlight/skill/memory blocks) unrendered.
    prompt_jinja: bool = False
    prompt_variables: tuple[PromptVariableSpec, ...] = ()
    prompt_base: str = ""
    prompt_suffix: str = ""
    #: Stream L.L7 — per-agent trajectory-recording opt-out
    #: (``policies.trajectory_recording``). Callers gate
    #: ``sse.run_agent(trajectory_enabled=...)`` on this; ``False`` means
    #: the run is never serialised to ObjectStore even when the deployment
    #: has a recorder configured.
    trajectory_recording: bool = True
    #: B3 — per-run token breaker limit (``policies.token_budget``). Callers
    #: pass it to ``sse.run_agent(token_budget=...)``; 0 disables (no budget
    #: object is created, zero behaviour change).
    token_budget: int = 0
    #: PR-A.3 — the build's full tool registry projection
    #: (``ToolRegistry.catalog()``) for the console's Schema tab. Read-only
    #: metadata; nothing on the run path consumes it.
    tool_catalog: tuple[ToolCatalogEntry, ...] = ()
    #: 弹性 worker 预算(2026-08-28)— the manifest's per-agent spawn-budget
    #: *requests* (``dynamic_workers.max_concurrent`` / ``max_per_run``),
    #: projected here so the run entry points can hand them to
    #: ``AgentRuntime.new_worker_spawn_budget``, which clamps each to the
    #: platform hard cap. ``None`` = manifest didn't ask → platform default.
    worker_max_concurrent: int | None = None
    worker_max_per_run: int | None = None
