"""Smoke tests for :class:`AgentState` shape (Stream E.6)."""

from __future__ import annotations

import inspect

from orchestrator import DEFAULT_MAX_STEPS, AgentState
from orchestrator.state import _merge_promoted


def test_required_keys_present() -> None:
    """E.6 ``step_count`` / ``max_steps`` on top of E.1 ``messages``;
    J.1 ``plan``, J.2 ``reflections``, J.3 ``recalled_memories``,
    L.5 ``step_count_refund_pending``, CM-1 ``tool_failures``,
    J.4-补强-2 ``subagent_invocations``, J.8 ``pending_approval`` /
    ``approval_resume`` / ``approval_outcome``, TE-6 ``promoted_tools``,
    HX-12 ``promoted_tool_last_used``, CM-0 ``last_projection_hash``,
    CM-11 ``last_plan_goal``, 委派层 1 ``delegation_nudge_plan_hash``
    (last fourteen ``NotRequired``)."""
    annotations = inspect.get_annotations(AgentState)
    assert set(annotations) == {
        "messages",
        "step_count",
        "max_steps",
        "plan",
        "reflections",
        "recalled_memories",
        "step_count_refund_pending",
        "escalate_next",
        "no_progress_streak",
        "max_no_progress",
        "last_plan_goal",
        "tool_failures",
        "subagent_invocations",
        "pending_approval",
        "approval_resume",
        "approval_outcome",
        "promoted_tools",
        "promoted_tool_last_used",
        "last_projection_hash",
        "delegation_nudge_plan_hash",
    }


def test_default_max_steps_constant_is_documented() -> None:
    """``DEFAULT_MAX_STEPS`` must stay at the design-doc-locked value (20)."""
    assert DEFAULT_MAX_STEPS == 20


# --- Stream TE-6: promoted_tools reducer -----------------------------------


def test_merge_promoted_seeds_from_none() -> None:
    assert _merge_promoted(None, ["a", "b"]) == ["a", "b"]


def test_merge_promoted_unions_and_dedupes_preserving_order() -> None:
    # existing first, then only the genuinely new names from ``new``.
    assert _merge_promoted(["a", "b"], ["b", "c"]) == ["a", "b", "c"]


def test_merge_promoted_dedupes_within_new() -> None:
    assert _merge_promoted([], ["x", "x", "y"]) == ["x", "y"]


def test_merge_promoted_empty_new_keeps_existing() -> None:
    assert _merge_promoted(["a"], []) == ["a"]
