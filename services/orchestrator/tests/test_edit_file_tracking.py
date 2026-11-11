"""B-302 PR 3 —— 每个文件的连续失败账与本轮改动账的规则(spec §4.3.3 末段、§4.3.4)。

整条链路(tools 节点 → 检查点 → 下一批的 ``edit_file``)在
``test_edit_file_diagnostics.py`` 的 T3-9 里走真图;这里逐条钉规则。
"""

from __future__ import annotations

from typing import Any

from orchestrator.tools.edit_file_tracking import (
    HISTORY_CHARS,
    HISTORY_PER_FILE,
    EditStreak,
    FileToolEvent,
    history_for,
    next_history,
    next_streaks,
    streak_for,
)


def _edit(path: str, *, ok: bool, kind: str | None = None, **args: Any) -> FileToolEvent:
    return FileToolEvent(tool="edit_file", args={"path": path, **args}, ok=ok, failure_kind=kind)


def _tool(tool: str, path: str, *, ok: bool = True) -> FileToolEvent:
    return FileToolEvent(tool=tool, args={"path": path}, ok=ok)


def test_counted_failures_add_up_per_file() -> None:
    events = [
        _edit("a.py", ok=False, kind="no_match"),
        _edit("a.py", ok=False, kind="ambiguous"),
        _edit("a.py", ok=False, kind="stale"),
        _edit("b.py", ok=False, kind="no_match"),
    ]
    out = next_streaks({}, events)
    assert streak_for(out, "a.py") == EditStreak(failures=3, read=False)
    assert streak_for(out, "b.py") == EditStreak(failures=1, read=False)


def test_other_edit_file_failures_neither_count_nor_reset() -> None:
    previous = {"a.py": {"failures": 2, "read": False}}
    out = next_streaks(previous, [_edit("a.py", ok=False, kind="not_found")])
    assert streak_for(out, "a.py").failures == 2
    out = next_streaks(previous, [_edit("a.py", ok=False, kind=None)])
    assert streak_for(out, "a.py").failures == 2


def test_a_successful_edit_or_write_resets_and_a_read_only_marks() -> None:
    previous = {"a.py": {"failures": 2, "read": False}}
    assert streak_for(next_streaks(previous, [_edit("a.py", ok=True)]), "a.py").failures == 0
    assert streak_for(next_streaks(previous, [_tool("write_file", "a.py")]), "a.py").failures == 0
    after_read = next_streaks(previous, [_tool("read_file", "a.py")])
    assert streak_for(after_read, "a.py") == EditStreak(failures=2, read=True)
    # a failed read marks nothing; a failed write resets nothing.
    assert (
        streak_for(next_streaks(previous, [_tool("read_file", "a.py", ok=False)]), "a.py").read
        is False
    )
    assert (
        streak_for(next_streaks(previous, [_tool("write_file", "a.py", ok=False)]), "a.py").failures
        == 2
    )


def test_a_read_before_any_failure_is_not_a_read_after_failing() -> None:
    out = next_streaks({}, [_tool("read_file", "a.py"), _edit("a.py", ok=False, kind="no_match")])
    assert streak_for(out, "a.py") == EditStreak(failures=1, read=False)


def test_paths_are_keyed_like_the_ledger() -> None:
    out = next_streaks({}, [_edit("  a.py ", ok=False, kind="no_match")])
    assert streak_for(out, "a.py").failures == 1
    assert streak_for(out, "./a.py").failures == 0  # same limitation as the run ledger


def test_junk_in_the_checkpoint_reads_as_nothing() -> None:
    junk = {"a.py": {"failures": "3"}, "b.py": [1], "c.py": {"failures": True}}
    for path in ("a.py", "b.py", "c.py"):
        assert streak_for(junk, path) == EditStreak()
    assert history_for({"a.py": [{"old": 1, "new": "x"}, "y"]}, "a.py") == []


def test_history_keeps_this_runs_successful_edits_only() -> None:
    events = [
        _edit("a.py", ok=True, old_string="one", new_string="uno"),
        _edit("a.py", ok=False, kind="no_match", old_string="two", new_string="dos"),
        FileToolEvent(
            tool="edit_file",
            args={
                "path": "a.py",
                "edits": [
                    {"old_string": "three", "new_string": "tres"},
                    {"old_string": "four", "new_string": "cuatro"},
                    {"old_string": "same", "new_string": "same"},
                ],
            },
            ok=True,
            already_applied=(2,),
        ),
    ]
    out = next_history({}, events)
    assert history_for(out, "a.py") == [
        {"old": "one", "new": "uno"},
        {"old": "three", "new": "tres"},
    ]


def test_history_is_bounded() -> None:
    long = "字" * (HISTORY_CHARS + 50)
    events = [
        _edit("a.py", ok=True, old_string=f"{k}{long}", new_string=f"n{k}")
        for k in range(HISTORY_PER_FILE + 5)
    ]
    kept = history_for(next_history({}, events), "a.py")
    assert len(kept) == HISTORY_PER_FILE
    assert kept[0]["old"].startswith("5")
    assert all(len(item["old"]) == HISTORY_CHARS for item in kept)


def test_inputs_are_not_mutated() -> None:
    previous = {"a.py": {"failures": 1, "read": False}}
    history = {"a.py": [{"old": "x", "new": "y"}]}
    next_streaks(previous, [_edit("a.py", ok=False, kind="no_match")])
    next_history(history, [_edit("a.py", ok=True, old_string="p", new_string="q")])
    assert previous == {"a.py": {"failures": 1, "read": False}}
    assert history == {"a.py": [{"old": "x", "new": "y"}]}
