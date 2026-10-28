"""B-85 ③ —— ``compute_completed``: run 做没做成。

**判据纪律**(spec `2026-09-20-run-completion-signal-design.md` §4):只用客观事实,
不推断模型意图。参考 hermes-agent 的 ``turn_finalizer`` 与 openclaw 的 ``stopReason``:
两家都不猜「模型放弃了」,只记「从哪个出口出去的」。
"""

from __future__ import annotations

from typing import Any

import pytest

from expert_work.runtime.runs.schemas import (
    INCOMPLETE_EXIT_REASONS,
    RUN_EXIT_REASONS,
    UNRESOLVED_FAILURES_CAP,
    ExternalRunStatus,
    RunStatus,
    compute_completed,
    external_run_status,
    run_status_filter,
    unresolved_failures_view,
)


class _Failure:
    """占位:这个判据只看**有没有**,不看内容。"""

    def __init__(self, error_class: str = "tool_error") -> None:
        self.error_class = error_class


def _failures(n: int) -> list[Any]:
    return [_Failure() for _ in range(n)]


def test_completed_is_false_while_a_tool_failure_is_still_unresolved() -> None:
    """B-85 那次的逐字重放。

    从 ``text_response`` 出口正常结束、``status`` 仍是 ``success``,但整个 run 里
    还有没被抵消掉的非 transient 工具失败 → ``completed=False``。

    注意这里判的是「**有一次工具失败自始至终没成功过**」这个事实,不是「模型放弃了」
    这个意图 —— 后者猜不准(模型合理地换个方法也长这样),前者客观可判。
    """
    assert compute_completed(exit_reason="text_response", unresolved_failures=_failures(1)) is False


def test_completed_is_true_on_a_clean_text_response_exit() -> None:
    assert compute_completed(exit_reason="text_response", unresolved_failures=[]) is True


def test_every_non_text_response_exit_is_not_completed() -> None:
    """平台**主动**中止的,按定义就没跑完 —— 撞预算 / 挂审批 / 被否决都算。"""
    for reason in (
        "max_steps",
        "no_progress",
        "token_budget",
        "approval_pending",
        "approval_rejected",
    ):
        assert compute_completed(exit_reason=reason, unresolved_failures=[]) is False, reason


def test_an_unknown_exit_reason_is_not_completed() -> None:
    """取值是封闭集合,但这个函数对没见过的值要**保守**:

    判 ``True`` 等于替一个我们不认识的出口作保,而本条要修的正是「平台替一个
    它不了解的终局打包票」。
    """
    assert compute_completed(exit_reason="something_new", unresolved_failures=[]) is False


# ---------------------------------------------------------------------------
# B-226 ⑥ — external status ``incomplete`` + the unresolved-failures view
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "exit_reason", "expected"),
    [
        # A platform limit stopped the run before its natural end.
        ("success", "max_steps", "incomplete"),
        ("success", "no_progress", "incomplete"),
        ("success", "token_budget", "incomplete"),
        # A natural end stays success, even with unresolved failures
        # (that is completed=false, not incomplete).
        ("success", "text_response", "success"),
        ("success", "approval_rejected", "success"),
        ("success", None, "success"),
        # Only success is ever rewritten.
        ("paused", "approval_pending", "paused"),
        ("interrupted", "approval_pending", "interrupted"),
        ("error", "max_steps", "error"),
        ("timeout", None, "timeout"),
        ("running", None, "running"),
        # An enum value behaves like its string.
        (RunStatus.SUCCESS, "max_steps", "incomplete"),
        (RunStatus.SUCCESS, "text_response", "success"),
    ],
)
def test_external_run_status_truth_table(
    status: str, exit_reason: str | None, expected: str
) -> None:
    result = external_run_status(status, exit_reason)
    assert result == expected
    assert type(result) is str


def test_incomplete_exit_reasons_are_exactly_the_budget_exits() -> None:
    """The three exits a platform limit stamps (``budget_exit_reason``) and
    nothing else: approval exits and the natural end are not limits."""
    assert INCOMPLETE_EXIT_REASONS == {"max_steps", "no_progress", "token_budget"}
    assert INCOMPLETE_EXIT_REASONS <= RUN_EXIT_REASONS


def test_external_run_status_is_run_status_plus_incomplete() -> None:
    """The partner filter vocabulary is the words the rows can say: every
    internal status (a new ``RunStatus`` member must be added here too) plus
    ``incomplete``."""
    assert {s.value for s in ExternalRunStatus} == {s.value for s in RunStatus} | {"incomplete"}


@pytest.mark.parametrize(
    ("external", "expected"),
    [
        # Only limit-stopped successes.
        ("incomplete", (RunStatus.SUCCESS, True)),
        # Successes that a partner reads as ``success`` (NULL exit included).
        ("success", (RunStatus.SUCCESS, False)),
        # Every other status: no exit_reason predicate.
        ("error", (RunStatus.ERROR, None)),
        ("paused", (RunStatus.PAUSED, None)),
        ("interrupted", (RunStatus.INTERRUPTED, None)),
        ("running", (RunStatus.RUNNING, None)),
    ],
)
def test_run_status_filter_truth_table(
    external: str, expected: tuple[RunStatus, bool | None]
) -> None:
    assert run_status_filter(external) == expected


def test_run_status_filter_rejects_unknown_values() -> None:
    with pytest.raises(ValueError):
        run_status_filter("done")


class _Ledger:
    """A ledger entry shaped like ``ClassifiedToolError``, tenant content included."""

    def __init__(self, tool_name: str, error_class: str) -> None:
        self.tool_name = tool_name
        self.error_class = error_class
        self.summary = "stderr: secret customer row"
        self.path = "/workspace/客户资料.xlsx"
        self.advice = "retry"
        self.retryable = False


def test_unresolved_failures_view_keeps_only_tool_and_kind() -> None:
    view = unresolved_failures_view(
        [
            _Ledger("bash", "unknown"),
            {"tool_name": "mcp__crm__update", "error_class": "blocked_by_policy", "summary": "x"},
        ]
    )
    assert view == [
        {"tool": "bash", "kind": "unknown"},
        {"tool": "mcp__crm__update", "kind": "blocked_by_policy"},
    ]
    blob = repr(view)
    assert "secret" not in blob
    assert "客户资料" not in blob
    assert "retry" not in blob


def test_unresolved_failures_view_caps_at_twenty() -> None:
    entries = [_Ledger(f"tool_{i}", "unknown") for i in range(25)]
    view = unresolved_failures_view(entries)
    assert UNRESOLVED_FAILURES_CAP == 20
    assert len(view) == 20
    assert view[0]["tool"] == "tool_0"
    assert view[-1]["tool"] == "tool_19"


def test_unresolved_failures_view_keeps_an_entry_it_cannot_read() -> None:
    """A malformed entry still counts (``completed`` is already false because
    of it): it shows with an empty tool name and kind ``unknown``."""
    assert unresolved_failures_view([object()]) == [{"tool": "", "kind": "unknown"}]
    assert unresolved_failures_view([]) == []
