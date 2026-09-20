"""B-85 ③ —— ``compute_completed``: run 做没做成。

**判据纪律**(spec `2026-09-20-run-completion-signal-design.md` §4):只用客观事实,
不推断模型意图。参考 hermes-agent 的 ``turn_finalizer`` 与 openclaw 的 ``stopReason``:
两家都不猜「模型放弃了」,只记「从哪个出口出去的」。
"""

from __future__ import annotations

from typing import Any

from expert_work.runtime.runs.schemas import compute_completed


class _Failure:
    """占位:这个判据只看**有没有**,不看内容。"""

    def __init__(self, error_class: str = "tool_error") -> None:
        self.error_class = error_class


def _failures(n: int) -> list[Any]:
    return [_Failure() for _ in range(n)]


def test_completed_is_false_when_the_last_tool_batch_still_had_failures() -> None:
    """B-85 那次的逐字重放。

    从 ``text_response`` 出口正常结束、``status`` 仍是 ``success``,但最后一批
    工具调用里有未解决的非 transient 失败 → ``completed=False``。

    注意这里判的是「**结束得紧挨着一批失败**」这个事实,不是「模型放弃了」这个
    意图 —— 后者猜不准(模型合理地换个方法也长这样),前者客观可判。
    """
    assert compute_completed(exit_reason="text_response", last_batch_failures=_failures(1)) is False


def test_completed_is_true_on_a_clean_text_response_exit() -> None:
    assert compute_completed(exit_reason="text_response", last_batch_failures=[]) is True


def test_every_non_text_response_exit_is_not_completed() -> None:
    """平台**主动**中止的,按定义就没跑完 —— 撞预算 / 挂审批 / 被否决都算。"""
    for reason in (
        "max_steps",
        "no_progress",
        "token_budget",
        "approval_pending",
        "approval_rejected",
    ):
        assert compute_completed(exit_reason=reason, last_batch_failures=[]) is False, reason


def test_an_unknown_exit_reason_is_not_completed() -> None:
    """取值是封闭集合,但这个函数对没见过的值要**保守**:

    判 ``True`` 等于替一个我们不认识的出口作保,而本条要修的正是「平台替一个
    它不了解的终局打包票」。
    """
    assert compute_completed(exit_reason="something_new", last_batch_failures=[]) is False
