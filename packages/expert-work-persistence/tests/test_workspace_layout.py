"""Unit tests for workspace layout conventions (reserved-prefix filter)."""

from __future__ import annotations

from expert_work.persistence import (
    RENDERED_FIGURE_DIR,
    RENDERED_FIGURE_PAGE_STEM,
    RENDERED_FIGURE_SHA_HEX_LEN,
    RENDERED_FIGURE_UNIT_PREFIX,
    SANDBOX_AGENTS_ROOT,
    SANDBOX_SKILLS_ROOT,
    WORKSPACE_DELETE_PROTECTED_PREFIXES,
    WORKSPACE_INPUTS_DIR,
    WORKSPACE_OVERFLOW_DIR,
    WORKSPACE_RESERVED_PREFIXES,
    WORKSPACE_SKILLS_DIR,
    WORKSPACE_UPLOADS_DIR,
    is_delete_protected_workspace_path,
    is_rendered_figure_rel,
    is_reserved_workspace_path,
)


def test_reserved_prefixes_cover_skills_uploads_and_overflow() -> None:
    assert WORKSPACE_SKILLS_DIR in WORKSPACE_RESERVED_PREFIXES
    assert WORKSPACE_UPLOADS_DIR in WORKSPACE_RESERVED_PREFIXES
    assert WORKSPACE_OVERFLOW_DIR in WORKSPACE_RESERVED_PREFIXES
    assert WORKSPACE_INPUTS_DIR in WORKSPACE_RESERVED_PREFIXES


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


# ------------------------------------------------------------ B-61 注入变量


def test_injected_variable_paths_are_reserved_but_not_delete_protected() -> None:
    """``inputs/`` 两半都是平台派生的机械产物:浏览面要藏,但平台自己要能回收。

    藏:不藏的话用户的「产物」列表里会混进每一个不透明的
    ``inputs/cache/<32 位 hex><ext>``(内容寻址的预拉缓存,跨轮长期驻留)与每一轮的
    ``inputs/<run_id>/inputs.json`` —— 一个都不是 agent 产出的东西。

    **不**加删除保护:与 ``.tool_results`` 同一个理由 —— 这是平台要能自己收的垃圾
    (control-plane 的 workspace janitor 按 TTL 收它),删除保护挡的是另一个方向的事。
    """
    assert is_reserved_workspace_path("agents/plan-aaaaaaaa/inputs/cache/" + "a" * 32 + ".png")
    assert is_reserved_workspace_path(
        "agents/plan-aaaaaaaa/inputs/9f2a0000-0000-0000-0000-000000000000/inputs.json"
    )
    assert is_reserved_workspace_path("inputs/cache/a.png")  # 未绑 agent 的扁平位置
    assert not is_delete_protected_workspace_path("agents/plan-aaaaaaaa/inputs/cache/a.png")


def test_a_file_named_like_the_inputs_prefix_is_still_agent_output() -> None:
    """收窄不能收过头:叫 ``inputs.md`` 的文件是产物,不是保留目录。"""
    assert not is_reserved_workspace_path("agents/plan-aaaaaaaa/inputs.md")
    assert not is_reserved_workspace_path("agents/plan-aaaaaaaa/客户inputs/x.md")


def test_rendered_figure_shape_accepts_every_pdftoppm_padding_width() -> None:
    """``pdftoppm`` 按**总页数**补零 —— 三种宽度都得认(B-64 末轮 M-b)。

    这条钉的是「不许把页号收紧成固定位宽」。``read_page`` 的测试桩恒写两位
    (``page-03.jpg``),所以那条端到端金丝雀只走过一种宽度:谁把这里收紧成
    ``[0-9]{2}``,金丝雀会**保持绿**,而生产对所有 10 页以下与 99 页以上的文档
    静默停止缓存 —— 正是那条测试声称要防的形状。失败方向只是性能,但「静默」
    这一半正是 B-64 存在的理由。
    """
    base = (
        f"{WORKSPACE_OVERFLOW_DIR}/9f2a0000-0000-0000-0000-000000000000/"
        f"{RENDERED_FIGURE_DIR}/{'a' * RENDERED_FIGURE_SHA_HEX_LEN}/"
        f"{'b' * RENDERED_FIGURE_SHA_HEX_LEN}/{RENDERED_FIGURE_UNIT_PREFIX}3/"
        f"{RENDERED_FIGURE_PAGE_STEM}-"
    )
    for page in ("3", "03", "005"):  # <10 页 / 10-99 页 / >99 页
        assert is_rendered_figure_rel(f"{base}{page}.jpg"), page
