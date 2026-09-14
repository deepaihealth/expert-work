"""B-50 Task 6 —— agent 根 / ``shared:`` 前缀的单源解析。

这个模块是 PR3 的地基:四个文件工具 + ``read_document`` 都从它取 ``ws``,
所以它错一处就是全部工具一起错。``agent_key`` 来自 ``config["configurable"]``
(不可信输入),会被拼进 ``ws`` —— 带 ``/`` 或 ``..`` 的值能把作用域撬出
``/workspace``,因此它自己必须校验,不能指望调用方。
"""

from __future__ import annotations

import pytest

from expert_work.persistence import WORKSPACE_AGENTS_DIR, WORKSPACE_SHARED_DIR
from orchestrator.tools import workspace_paths
from orchestrator.tools.sandbox_image_contract import EXEC_VIEW, NAS_MOUNT
from orchestrator.tools.workspace_paths import (
    SHARED_PREFIX,
    USER_ROOT,
    WriteToSharedError,
    agent_nas_root,
    agent_view_alias,
    agent_workspace_root,
    resolve_scope,
)


def test_bare_path_resolves_to_the_exec_view() -> None:
    """B-60 —— 文件工具的片段与用户代码在同一个命名空间里,看同一个视图:
    绑了 agent 时 /workspace **就是** agent 目录,ws 不再拼 agents/<key>。"""
    ws, rel = resolve_scope("MEMORY.md", agent_key="plan-aaaaaaaa", tool="read_file")
    assert (ws, rel) == (EXEC_VIEW, "MEMORY.md")


def test_shared_prefix_resolves_to_shared_root() -> None:
    ws, rel = resolve_scope(
        "shared:style/PLAN_STYLE.md", agent_key="plan-aaaaaaaa", tool="read_file"
    )
    assert ws == "/workspace/shared"
    assert rel == "style/PLAN_STYLE.md"


def test_empty_agent_key_falls_back_to_user_root() -> None:
    """迁移期回落:没绑 agent 时读写用户根,与今天行为一致。"""
    ws, rel = resolve_scope("MEMORY.md", agent_key="", tool="read_file")
    assert ws == EXEC_VIEW
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


# ``..`` / ``.`` **单独**列出来不是凑数:原来这张表里每一项都带分隔符或是
# 空白,于是「一段裸的 ``..``」从来没被试过 —— 而它恰恰能过那条正则
# (两个点都在字符集里),``{root}/agents/..`` 就是 ``{root}``,agent 作用域
# 塌回用户根。穷举形状时漏掉的往往是最短的那个。
@pytest.mark.parametrize("bad", ["../../etc", "a/b", "plan/", "", " ", "..", "."])
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


def test_layout_dirs_are_the_persistence_constants_not_a_second_copy() -> None:
    """布局目录名只许有一份定义 —— 这里是别名,真源在 persistence 的 layout.py。

    搬迁脚本、留存 job、控制台浏览面都够不着 orchestrator,它们从
    ``expert_work.persistence`` 拿这两个名字。如果本模块自己写一份字面量,
    有人把真源改成别的、沙箱仍去老名字下找,**没有任何功能测试会红** ——
    文件明明搬过去了,agent 就是看不见。

    **用 ``==`` 而不是 ``is``。** PR2 给 ``sanitize_agent_key`` 钉的是
    ``reexported is sanitize_agent_key``,那对函数成立;字符串不成立 ——
    CPython 会 intern ``"agents"`` 这类标识符形状的短字面量,所以把右边换成
    一份同值的第二字面量,``is`` 照样为真(已实测)。``is`` 在这里是摆设,
    真正要咬住的是「两个名字对不上」,那正是 ``==`` 管的事。
    """
    assert workspace_paths.AGENTS_DIR == WORKSPACE_AGENTS_DIR
    assert workspace_paths._SHARED_DIR == WORKSPACE_SHARED_DIR


def test_shared_prefix_always_names_the_shared_dir() -> None:
    """``shared:`` 前缀与它指向的目录同名 —— 前缀是目录名拼出来的,不是第二份字面量。

    两者分叉的形态很隐蔽:``read_file("shared:x")`` 会被解析到一个不存在的
    目录,报的是「文件不存在」,看不出是前缀错了。
    """
    assert workspace_paths.SHARED_PREFIX == f"{WORKSPACE_SHARED_DIR}:"
    assert workspace_paths.resolve_scope(
        f"{WORKSPACE_SHARED_DIR}:MEMORY.md", agent_key="plan-aaaaaaaa", tool="read_file"
    ) == (f"{EXEC_VIEW}/{WORKSPACE_SHARED_DIR}", "MEMORY.md")


def test_agent_nas_root_points_under_the_nas_mount() -> None:
    """B-60 —— 后端拼 exec 用的 bind 源:NAS 挂载点下的真实目录。"""
    assert agent_nas_root("plan-aaaaaaaa") == "/mnt/workspace/agents/plan-aaaaaaaa"


def test_agent_view_alias_is_the_legacy_absolute_spelling() -> None:
    """只用于折叠模型照旧写的 ``/workspace/agents/<key>/x``;视图里没有这个目录。"""
    assert agent_view_alias("plan-aaaaaaaa") == "/workspace/agents/plan-aaaaaaaa"


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a b", "../x", "a\x00b"])
def test_nas_root_and_alias_refuse_unsafe_or_empty_keys(bad: str) -> None:
    """空 key 也拒:未绑 agent 没有「自己的目录」,调用方自己分支,别让它拿到用户根。"""
    with pytest.raises(ValueError):
        agent_nas_root(bad)
    with pytest.raises(ValueError):
        agent_view_alias(bad)


def test_view_and_mount_are_distinct_roots() -> None:
    """视图根与挂载点互不包含 —— tmpfs 盖挂载点时不能把视图也盖掉。"""
    assert (NAS_MOUNT, EXEC_VIEW) == ("/mnt/workspace", "/workspace")
    assert not NAS_MOUNT.startswith(EXEC_VIEW + "/")
    assert not EXEC_VIEW.startswith(NAS_MOUNT + "/")
