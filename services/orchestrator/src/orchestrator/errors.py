"""Orchestrator-level error types."""

from __future__ import annotations


class OrchestratorError(Exception):
    """Base class for orchestrator-raised errors."""


class AgentFactoryError(OrchestratorError):
    """Raised when an ``AgentSpec`` cannot be assembled into a runnable
    agent — a missing ``api_key_ref``, an unsupported provider, or a
    ``tools:`` entry whose backing dependency is not configured.

    ``build_tool_registry`` raises this too, so a control-plane caller
    that catches :class:`AgentFactoryError` handles tool-assembly
    failures the same way (HTTP 422 — the manifest is un-buildable).
    """


class PlatformNotConfiguredError(AgentFactoryError):
    """The manifest is fine; this *deployment* is missing something it needs.

    Split out from plain :class:`AgentFactoryError` because the two have
    different owners and therefore different handling:

    - plain ``AgentFactoryError`` = the manifest is wrong (a cycle, an
      unresolvable skill, a model that has no thinking toggle). The person
      editing it can fix it, so a save-time gate should refuse the write.
    - ``PlatformNotConfiguredError`` = the platform has no credential for the
      referenced provider, or no embedder for the declared long-term memory.
      The manifest author usually cannot change either. Refusing the save
      would lock them out of authoring until an operator acts — and would make
      a fresh deployment unable to register any agent before credentials land.

    Callers that only care "can this run?" can keep catching
    :class:`AgentFactoryError` and get both, unchanged.
    """


class MaxStepsExceededError(OrchestratorError):
    """Raised when the ReAct loop hits ``max_steps`` and the LLM still
    wants to call tools.

    Per [STREAM-E-DESIGN § 1.1 E.6](../../../../docs/streams/STREAM-E-DESIGN.md),
    the loop has a hard runaway guard — once ``state["step_count"]``
    reaches ``state["max_steps"]``, the agent node raises this instead
    of dispatching another LLM call. Callers should write a
    ``RUN_FAILED`` audit row and surface ``"reached max_steps"`` to
    the user.
    """

    def __init__(self, step_count: int, max_steps: int) -> None:
        super().__init__(f"ReAct loop exceeded max_steps={max_steps} (step_count={step_count})")
        self.step_count = step_count
        self.max_steps = max_steps


class SkillNotFoundError(AgentFactoryError):
    """Manifest references a skill name that the tenant has no skill row for.

    Stream J.7a — caller (control-plane) maps to HTTP 422 (manifest is
    un-buildable until the skill exists).
    """


class SkillVersionNotFoundError(AgentFactoryError):
    """Manifest pins ``name@N`` but no ``skill_version`` row has that version.

    Stream J.7a — caller maps to HTTP 422. Distinct from
    :class:`SkillNotFoundError` so the operator can tell "skill exists
    but pin is bad" from "skill never existed".
    """


class SkillNotActiveError(AgentFactoryError):
    """Bare-name reference to a skill that is not in ``ACTIVE`` status.

    Stream J.7a — caller maps to HTTP 422. Pinned ``name@N`` references
    bypass this check (they explicitly opt into draft / archived).
    """


class SkillConflictError(AgentFactoryError):
    """Two skills in a manifest declare the same tool name.

    Stream J.7a — Mini-ADR J-23 § 15.6 (c) 红线 build 期校验:tool 重叠
    必须 reject build,避免 agent 在运行期拿到非预期 tool (safety > 灵活).
    """


class SkillModelMismatchError(AgentFactoryError):
    """Skill declares ``required_models`` and the agent's primary model is not in it.

    Stream J.7a — Mini-ADR J-23 § 15.6 build 期 5 项校验之一。
    """
