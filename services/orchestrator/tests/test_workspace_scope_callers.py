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
        "build_edit_wrapper",
        "build_edits_wrapper",
        "build_read_document_wrapper",
    }
)

#: ``(模块, 所在函数)`` → ``(是否 agent 作用域, 理由)``。
#: ``理由`` 只在「有意留在用户根」时读,但两种都必须写 —— 逼着下一个人说清楚。
#:
#: B-84 —— ``ReadFileTool.call`` / ``ListDirTool.call`` 从这张表里**消失**了,不是
#: 漏登记:它们不再构造沙箱片段,读走 control-plane 自己挂着的 NAS
#: (``orchestrator.tools.workspace_scope``)。那条路径的作用域由
#: ``test_workspace_scoped_reads.py`` 钉(三个实现一套用例 + 五类逃逸),不在这张
#: 表的管辖范围内 —— 这张表管的是「谁把 ``ws`` 编进了沙箱片段」。
_CALL_SITES: dict[tuple[str, str], tuple[bool, str]] = {
    ("file_ops.py", "WriteFileTool.call"): (True, "写:agent 根,永不回落"),
    ("file_ops.py", "EditFileTool.call"): (True, "写:agent 根,永不回落"),
    ("read_document.py", "ReadDocumentTool.call"): (True, "读:agent 根,不回落(PR6 摘掉)"),
    # B-236 —— 平台自己写 / 读的三处一律显式按 ``/agent``(agent 根)寻址:对话视图里
    # ``/workspace`` 是 ``threads/<conv>/work``,按它写投影会落进对话目录里一个嵌套的
    # ``threads/<conv>/PLAN.md``;``/agent`` 在两种视图里都是 agent 根。
    ("file_ops.py", "SandboxWorkspaceWriter.write"): (True, "状态投影:/agent,两种视图同一处"),
    ("file_ops.py", "SandboxWorkspaceReader.read"): (True, "状态回读:与投影同进同退"),
    ("file_ops.py", "SandboxWorkspaceWriter.read"): (True, "B-188 旧投影回读:与投影同进同退"),
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
