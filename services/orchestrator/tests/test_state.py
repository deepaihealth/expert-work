"""Smoke tests for :class:`AgentState` shape (Stream E.6)."""

from __future__ import annotations

import inspect

from orchestrator import DEFAULT_MAX_STEPS, AgentState
from orchestrator.state import _merge_promoted, _merge_viewed_figures


def test_required_keys_present() -> None:
    """E.6 ``step_count`` / ``max_steps`` on top of E.1 ``messages``;
    J.1 ``plan``, J.2 ``reflections``, J.3 ``recalled_memories``,
    L.5 ``step_count_refund_pending``, CM-1 ``tool_failures``,
    J.4-补强-2 ``subagent_invocations``, J.8 ``pending_approval`` /
    ``approval_resume`` / ``approval_outcome``, TE-6 ``promoted_tools``,
    HX-12 ``promoted_tool_last_used``, CM-0 ``last_projection_hash``,
    CM-11 ``last_plan_goal``, 委派层 1 ``delegation_nudge_plan_hash``,
    B-35 ``plan_first_dispatch_plan_hash`` / ``plan_first_dispatch_active``
    / ``plan_first_dispatch_retries``,本轮附件 ``turn_documents`` /
    ``turn_image_refs``,B-85 ③ ``unresolved_failures`` / ``exit_reason``,
    B-64 ``viewed_figures``
    (last twenty-two ``NotRequired``)。

    ``turn_*`` 放在 state 而不是 config,是为了让检查点在
    ``graph_input=None`` 的续跑(审批 / orphan 复活)里替我们保住它们。"""
    annotations = inspect.get_annotations(AgentState)
    assert set(annotations) == {
        "messages",
        "step_count",
        "max_steps",
        "turn_documents",
        "turn_image_refs",
        "plan",
        "reflections",
        "recalled_memories",
        "step_count_refund_pending",
        "escalate_next",
        "no_progress_streak",
        "max_no_progress",
        "last_plan_goal",
        "tool_failures",
        # B-85 ③ / B-84 第 3 条 —— 见 state.py 上的注释:``tool_failures`` 按轮
        # 重置、终局拿不到,这一条按 ``(tool_name, path)`` 跨批记账,留到 END。
        "unresolved_failures",
        "exit_reason",
        "subagent_invocations",
        "pending_approval",
        "approval_resume",
        "approval_outcome",
        "promoted_tools",
        "promoted_tool_last_used",
        "last_projection_hash",
        "delegation_nudge_plan_hash",
        "plan_first_dispatch_plan_hash",
        "plan_first_dispatch_active",
        "plan_first_dispatch_retries",
        "viewed_figures",
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


# --- B-64: viewed_figures reducer -------------------------------------------


def test_viewed_figures_merges_across_turns_in_order() -> None:
    merged = _merge_viewed_figures(["a", "b"], ["c"])
    assert merged == ["a", "b", "c"]


def test_viewed_figures_dedupes_without_reordering() -> None:
    """重看同一页不该把它挪到队尾 —— 滑窗按「首次看到」排。"""
    assert _merge_viewed_figures(["a", "b"], ["a", "c"]) == ["a", "b", "c"]


def test_tools_may_write_viewed_figures() -> None:
    from orchestrator.tools.registry import TOOL_ALLOWED_STATE_KEYS

    assert "viewed_figures" in TOOL_ALLOWED_STATE_KEYS
