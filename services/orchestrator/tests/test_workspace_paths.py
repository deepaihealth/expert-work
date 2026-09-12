"""B-50 Task 6 —— agent 根 / ``shared:`` 前缀的单源解析。

这个模块是 PR3 的地基:四个文件工具 + ``read_document`` 都从它取 ``ws``,
所以它错一处就是全部工具一起错。``agent_key`` 来自 ``config["configurable"]``
(不可信输入),会被拼进 ``ws`` —— 带 ``/`` 或 ``..`` 的值能把作用域撬出
``/workspace``,因此它自己必须校验,不能指望调用方。
"""

from __future__ import annotations

import pytest

from orchestrator.tools.workspace_paths import (
    SHARED_PREFIX,
    USER_ROOT,
    WriteToSharedError,
    agent_workspace_root,
    resolve_scope,
)


def test_bare_path_resolves_under_agent_root() -> None:
    ws, rel = resolve_scope("MEMORY.md", agent_key="plan-aaaaaaaa", tool="read_file")
    assert ws == "/workspace/agents/plan-aaaaaaaa"
    assert rel == "MEMORY.md"


def test_shared_prefix_resolves_to_shared_root() -> None:
    ws, rel = resolve_scope(
        "shared:style/PLAN_STYLE.md", agent_key="plan-aaaaaaaa", tool="read_file"
    )
    assert ws == "/workspace/shared"
    assert rel == "style/PLAN_STYLE.md"


def test_empty_agent_key_falls_back_to_user_root() -> None:
    """迁移期回落:没绑 agent 时读写用户根,与今天行为一致。"""
    ws, rel = resolve_scope("MEMORY.md", agent_key="", tool="read_file")
    assert ws == USER_ROOT == "/workspace"
    assert rel == "MEMORY.md"


def test_shared_prefix_is_rejected_for_writes() -> None:
    """写 ``shared:`` 必须显式报错,不能静默改写到 agent 目录。"""
    with pytest.raises(WriteToSharedError):
        resolve_scope("shared:x.md", agent_key="plan-aaaaaaaa", tool="write_file")


def test_shared_prefix_is_rejected_for_edits() -> None:
    """``edit_file`` 与 ``write_file`` 同档 —— 它也会改字节。"""
    with pytest.raises(WriteToSharedError):
        resolve_scope("shared:x.md", agent_key="plan-aaaaaaaa", tool="edit_file")


def test_shared_prefix_still_rejects_traversal() -> None:
    """``shared:`` 不是绕过 ``..`` 校验的后门。"""
    with pytest.raises(ValueError, match=r"\.\."):
        resolve_scope("shared:../../etc/passwd", agent_key="plan-aaaaaaaa", tool="read_file")


def test_shared_prefix_rejects_absolute_remainder() -> None:
    with pytest.raises(ValueError):
        resolve_scope("shared:/etc/passwd", agent_key="plan-aaaaaaaa", tool="read_file")


def test_shared_prefix_rejects_empty_remainder() -> None:
    with pytest.raises(ValueError):
        resolve_scope("shared:", agent_key="plan-aaaaaaaa", tool="read_file")


@pytest.mark.parametrize("bad", ["../../etc", "a/b", "plan/", "", " "])
def test_agent_key_that_is_not_one_path_segment_is_refused(bad: str) -> None:
    """``agent_key`` 会被拼进 ``ws``;不是单个安全路径段就必须拒。

    空串是唯一的例外(=未绑定,走用户根),由
    :func:`test_empty_agent_key_falls_back_to_user_root` 覆盖。
    """
    if bad == "":
        assert agent_workspace_root(bad) == USER_ROOT
        return
    with pytest.raises(ValueError):
        agent_workspace_root(bad)


def test_agent_workspace_root_accepts_sanitize_agent_key_output() -> None:
    """真实取值必须过 —— 形状由 ``sanitize_agent_key`` 决定。"""
    from expert_work.protocol.agent_key import sanitize_agent_key

    for name in ("ai-health-plan", "sop2-designer", "方案 设计师", "a" * 200):
        key = sanitize_agent_key(name)
        assert agent_workspace_root(key) == f"{USER_ROOT}/agents/{key}"


def test_shared_prefix_constant_is_the_documented_one() -> None:
    assert SHARED_PREFIX == "shared:"
