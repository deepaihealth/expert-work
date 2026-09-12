"""Unit tests for workspace layout conventions (reserved-prefix filter)."""

from __future__ import annotations

from expert_work.persistence import (
    SANDBOX_AGENTS_ROOT,
    SANDBOX_SKILLS_ROOT,
    WORKSPACE_DELETE_PROTECTED_PREFIXES,
    WORKSPACE_OVERFLOW_DIR,
    WORKSPACE_RESERVED_PREFIXES,
    WORKSPACE_SKILLS_DIR,
    WORKSPACE_UPLOADS_DIR,
    is_delete_protected_workspace_path,
    is_reserved_workspace_path,
)


def test_reserved_prefixes_cover_skills_uploads_and_overflow() -> None:
    assert WORKSPACE_SKILLS_DIR in WORKSPACE_RESERVED_PREFIXES
    assert WORKSPACE_UPLOADS_DIR in WORKSPACE_RESERVED_PREFIXES
    assert WORKSPACE_OVERFLOW_DIR in WORKSPACE_RESERVED_PREFIXES


def test_seeded_skill_and_upload_paths_are_reserved() -> None:
    assert is_reserved_workspace_path("skills/pptx/SKILL.md")
    assert is_reserved_workspace_path("uploads/ticket.pdf")


def test_agent_output_paths_are_not_reserved() -> None:
    # Bare top-level files and the agent's own output dirs stay visible.
    assert not is_reserved_workspace_path("report.pdf")
    assert not is_reserved_workspace_path("out/notes.txt")
    # A file literally named like a prefix (not a dir) is output, not reserved.
    assert not is_reserved_workspace_path("skills.md")


def test_sandbox_local_roots_are_absolute_and_distinct() -> None:
    # sandbox migration wave 2 (spec § 四 / 决策 10) — both roots live on
    # sandbox-local disk, outside the (NAS-backed) workspace tree entirely,
    # so they must never collide with WORKSPACE_* or each other.
    assert SANDBOX_SKILLS_ROOT == "/opt/skills"
    assert SANDBOX_AGENTS_ROOT == "/opt/agents"
    assert SANDBOX_SKILLS_ROOT != SANDBOX_AGENTS_ROOT


# ------------------------------------------------------------ B-50 容器层


def test_uploads_under_agent_dir_is_still_reserved() -> None:
    """搬迁后 uploads 在 ``agents/<key>/uploads/`` —— 仍须被浏览面隐藏。

    不修的话上传文件会突然出现在「产物」视图里。这是搬迁引入的**静默**
    行为变更:没有报错,列表只是多出一批从来不是 agent 产出的文件。
    """
    assert is_reserved_workspace_path("agents/plan-aaaaaaaa/uploads/a.docx")
    assert is_reserved_workspace_path("agents/plan-aaaaaaaa/skills/pptx/SKILL.md")


def test_legacy_toplevel_and_shared_uploads_are_still_reserved() -> None:
    """回落期的老路径与 ``shared/`` 下的同样要认。"""
    assert is_reserved_workspace_path("uploads/a.docx")
    assert is_reserved_workspace_path("shared/uploads/a.docx")


def test_agent_working_files_are_not_reserved() -> None:
    """收窄不能收过头:agent 自己的工作文件照常可见。"""
    assert not is_reserved_workspace_path("agents/plan-aaaaaaaa/MEMORY.md")
    assert not is_reserved_workspace_path("agents/plan-aaaaaaaa/客户案例/秀域/x.md")
    assert not is_reserved_workspace_path("shared/MEMORY.md")


def test_a_segment_named_uploads_deeper_in_is_not_reserved() -> None:
    """**只看剥完容器后的第一段**,不是「任何一段叫 uploads 就算」。

    后者会把 agent 自己建的 ``客户案例/uploads/`` 一起吃掉 —— 那是它的产出,
    不是平台的输入区。这条是「收窄收过头」的判据,少了它,把规则写成
    「任何一段命中即算」不会有任何测试红。
    """
    assert not is_reserved_workspace_path("agents/plan-aaaaaaaa/客户案例/uploads/合同.pdf")
    assert not is_reserved_workspace_path("out/uploads/x.txt")


def test_tool_results_is_hidden_everywhere() -> None:
    """``.tool_results`` 是纯缓存,任何位置都不该出现在浏览面。"""
    assert is_reserved_workspace_path(".tool_results/abc/x.txt")
    assert is_reserved_workspace_path("agents/plan-aaaaaaaa/.tool_results/abc/x.txt")


def test_bare_container_dirs_are_not_reserved() -> None:
    """容器段本身不是保留段 —— 它只说「谁的」,不说「什么类型」。

    把 ``agents`` / ``shared`` 当保留段会把**整棵树**从浏览面隐藏掉。
    """
    assert not is_reserved_workspace_path("agents")
    assert not is_reserved_workspace_path("shared")
    assert not is_reserved_workspace_path("agents/plan-aaaaaaaa")


def test_overflow_is_hidden_but_still_deletable() -> None:
    """「浏览面隐藏」与「禁止删除」是两件事,``.tool_results`` 是把它们分开的那个成员。

    合成一个集合曾经是对的 —— 里头只有 ``skills``(播种的机器文件)和
    ``uploads``(用户输入),两者都得挺过客户端的任何删除请求。``.tool_results``
    打破了这个巧合:它对浏览的人同样没意义,但它是**平台自己要回收的垃圾** ——
    会话 purge 钩子正是通过 ``delete_tree`` 去 rm -rf ``.tool_results/<run_id>/``。

    两者合一的话那次调用会撞上「path is reserved and cannot be deleted」,
    而 purge 是 best-effort 的:异常被吞进日志,清理**静默失效**,要等一天
    宽限期后孤儿扫描才兜得住。所以这条必须钉住。
    """
    assert is_reserved_workspace_path("agents/plan-aaaaaaaa/.tool_results/r/x.txt")
    assert not is_delete_protected_workspace_path("agents/plan-aaaaaaaa/.tool_results/r/x.txt")

    # 另外两个成员两边都在 —— 收窄不能把它们一起放开。
    for p in ("agents/plan-aaaaaaaa/uploads/a.docx", "skills/pptx/SKILL.md"):
        assert is_reserved_workspace_path(p)
        assert is_delete_protected_workspace_path(p)


def test_delete_protection_is_a_strict_subset_of_hiding() -> None:
    """可删保护必须真包含于隐藏集 —— 一个「能删但看不见」之外的组合都不该存在。

    反过来的组合(不隐藏却禁止删)意味着浏览面列出了一个用户删不掉的文件,
    没有任何界面能解释那是为什么。
    """
    assert WORKSPACE_DELETE_PROTECTED_PREFIXES < WORKSPACE_RESERVED_PREFIXES
