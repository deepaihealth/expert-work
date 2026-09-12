"""B-50 —— 谁在往沙箱工作区里读写,必须逐个登记。

写这条测试的直接原因:PR3 的实施计划登记了**四个**文件工具调用点,实测是
**七个** —— 漏了状态投影的写 / 读两处和 ``read_document``。同一个 program 里
「入口只写一处就漏两处」已经犯过两次(PR1 的九个 configurable 构造点也是),
所以这次把清单变成可执行的:新增一个没登记的调用点,这里直接红。

登记表回答两个问题:
1. 这个调用点在不在清单里(漏登记 = 红);
2. 它是 **agent 作用域**(必须显式传 ``ws=``),还是**有意留在用户根**
   (必须写明理由 —— 空理由 = 红)。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_TOOLS_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "tools"

#: 会把 ``ws`` 编进沙箱片段的构造函数 —— 工作区的真正入口。
_BUILDERS = frozenset(
    {
        "build_read_wrapper",
        "build_write_wrapper",
        "build_list_wrapper",
        "build_edit_wrapper",
        "build_read_document_wrapper",
    }
)

#: ``(模块, 所在函数)`` → ``(是否 agent 作用域, 理由)``。
#: ``理由`` 只在「有意留在用户根」时读,但两种都必须写 —— 逼着下一个人说清楚。
_CALL_SITES: dict[tuple[str, str], tuple[bool, str]] = {
    ("file_ops.py", "ReadFileTool.call"): (True, "读:agent 根 + 迁移期回落"),
    ("file_ops.py", "WriteFileTool.call"): (True, "写:agent 根,永不回落"),
    ("file_ops.py", "ListDirTool.call"): (True, "读:agent 根 + 迁移期回落"),
    ("file_ops.py", "EditFileTool.call"): (True, "写:agent 根,永不回落"),
    ("read_document.py", "ReadDocumentTool.call"): (True, "读:agent 根 + 迁移期回落"),
    ("file_ops.py", "SandboxWorkspaceWriter.write"): (
        False,
        "状态投影(threads/<tid>/PLAN.md)—— **有意暂缓到 PR5**。"
        "control_plane/api/sessions.py 的 B-27 留存链按用户根下的 threads/<thread_id>/ "
        "删目录;写入先搬走、删除侧要到 PR5(Task 12)才跟上,中间每个被清理的会话都会 "
        "留下永远删不掉的投影文件。两边必须同一个 PR 改。",
    ),
    ("file_ops.py", "SandboxWorkspaceReader.read"): (
        False,
        "状态回读 —— 与 SandboxWorkspaceWriter 成对,必须同进同退:"
        "一边搬一边不搬 = 写进 agent 根、从用户根读,回读恒空。",
    ),
}


def _qualname(stack: list[ast.AST]) -> str:
    parts = [
        node.name
        for node in stack
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    return ".".join(parts)


def _discover() -> dict[tuple[str, str], bool]:
    """全仓扫出 ``(模块, 所在函数) → 是否显式传了 ws=``。"""
    found: dict[tuple[str, str], bool] = {}
    for path in sorted(_TOOLS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stack: list[ast.AST] = []

        def visit(node: ast.AST, stack: list[ast.AST] = stack, path: pathlib.Path = path) -> None:
            pushed = isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            if pushed:
                stack.append(node)
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else None
                # 只算**调用**,不算 def —— def 的默认值是 workspace_paths 的事。
                if name in _BUILDERS:
                    has_ws = any(kw.arg == "ws" for kw in node.keywords)
                    key = (path.name, _qualname(stack))
                    # 同一个函数里多次调用(如回落的两跳)一律要求都带 ws。
                    found[key] = found.get(key, True) and has_ws
            for child in ast.iter_child_nodes(node):
                visit(child)
            if pushed:
                stack.pop()

        visit(tree)
    return found


def test_every_workspace_call_site_is_registered() -> None:
    """清单穷举 —— 新增一个没登记的调用点必须红。"""
    discovered = set(_discover())
    registered = set(_CALL_SITES)
    assert discovered == registered, (
        f"没登记的调用点: {sorted(discovered - registered)};"
        f" 登记了但已消失: {sorted(registered - discovered)}"
    )


@pytest.mark.parametrize(("site", "expected"), sorted(_CALL_SITES.items()))
def test_call_site_scope_matches_its_registration(
    site: tuple[str, str], expected: tuple[bool, str]
) -> None:
    """agent 作用域的必须显式传 ``ws=``;留在用户根的必须**不传**(吃缺省)。

    缺省值就是用户根,所以「传了 ws 但登记成用户根」是自相矛盾的登记,
    和反过来一样必须红。
    """
    agent_scoped, reason = expected
    assert reason.strip(), f"{site} 登记了却没写理由"
    assert _discover()[site] is agent_scoped


def test_user_root_exceptions_carry_a_real_reason() -> None:
    """留在用户根的每一条都要说清**为什么**,而不是「暂时没改」。

    这几条是 PR5 的待办清单:搬迁脚本落地时逐条翻过来。
    """
    exceptions = {k: v[1] for k, v in _CALL_SITES.items() if not v[0]}
    assert exceptions, "一条例外都没有的话这条测试该删,而不是空过"
    for site, reason in exceptions.items():
        assert len(reason) > 40, f"{site} 的理由太短,说不清就是没想清"
