"""谁可以对产物查询传 ``agent_key=None``(不按 agent 过滤)—— 登记表盯着。

``agent_key`` 在 ``ArtifactStore`` 上是**必传、无默认值**的参数,传 ``None``
表示不按 agent 过滤。这个逃生口是有理由存在的(控制台的跨 agent 全量视图、
留存 job 的运维视角、对外端点在 PR4 收口之前的迁移期),但它**必须是可数的**:
多出一个没登记的 ``None``,就是一条 agent A 能看见 agent B 产物的路径,
而且不会报错、不会有测试变红 —— 正是 B-50 要根治的形态本身。

与 ``test_agent_key_plumbing.py`` 同一套做法:AST 穷举 + 登记表,新增调用点
漏登记会红。

**两张表,因为「不过滤」有两种写法:**

* 字面量 ``agent_key=None`` —— 无条件不过滤;
* ``agent_key=<某个调用>`` —— 该调用的返回值可能是 ``None``(例如
  ``_session_agent_key(meta)``:会话没有 ``agent_name`` 时回落不过滤)。
  静态看不出它会不会返回 ``None``,所以同样要登记 + 写理由。

只认字面量会漏掉第二种,而第二种恰恰更容易在后续改动里变成一个静默的
「看全部」。
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: 只扫真正会读写用户产物的源码根。
_ROOTS = {
    "control-plane": _REPO_ROOT / "services" / "control-plane" / "src" / "control_plane",
    "orchestrator": _REPO_ROOT / "services" / "orchestrator" / "src" / "orchestrator",
    "retention-cleanup-job": (
        _REPO_ROOT / "services" / "retention-cleanup-job" / "src" / "retention_cleanup_job"
    ),
}

#: 带 ``agent_key`` 参数的 ``ArtifactStore`` 方法。
_SCOPED_METHODS = ("save_version", "list_for_user", "get_latest_version", "soft_delete")

#: 允许传 ``agent_key=None``(不过滤)的调用点 → 理由。
#: 键是 ``"<包>/<相对路径>::<函数>"``,与 ``_SCOPED_METHODS`` 的每次 ``None`` 调用
#: 一一对应(同一函数里多次调用只登记一次)。
_ALLOWED_UNFILTERED: dict[str, str] = {
    # 控制台用户档案页 / 用户详情:**跨 agent 的全量视图**,运维要看见这个用户
    # 名下所有东西。它拿到的行带 ``agent_key``,单条操作走 ``*_by_id``。
    "control-plane/api/artifacts.py::list_artifacts": "控制台跨 agent 全量列表",
    "control-plane/api/artifacts.py::download_artifact": "回取父行判 MIME,身份已由 id 定死",
    "control-plane/api/workspace.py::get_workspace": "控制台工作区面板跨 agent 全量列表",
    # 对外端点 —— PR4 已收口。``download_artifact`` / ``delete_artifact`` 从本表
    # **删除**:它们现在无条件按 URL 里那个 ``agent_code`` 算出的 key 过滤,
    # 没有不过滤的分支。
    #
    # ``list_artifacts`` 留着,但理由换了:不再是「迁移期还没收口」,而是
    # ``?scope=user`` 那条并集分支 —— 对接方一个 app 编排两个 agent、服务同一批
    # 终端用户,他们点名要一次拿到某员工的全部产物。那条分支查全量之后**按反查
    # 表逐条贴 ``agent_code`` 并丢掉贴不上的**(agent 已删 → 第三方没有可用的
    # code 去下载它),所以「不过滤」在这里是入口宽、出口仍然逐条有归属。
    # 默认的 ``scope=agent`` 分支传的是算出来的 key,不走这条。
    "control-plane/api/external_artifacts.py::list_artifacts": (
        "?scope=user 并集入口,出口逐条贴 agent_code 并丢弃贴不上的"
    ),
}


#: 允许传「算出来的 ``agent_key``」(可能是 ``None``)的调用点 → 理由。
_ALLOWED_COMPUTED: dict[str, str] = {
    # 会话面三处都用 ``_session_agent_key(meta)``:会话属于某一个 agent;
    # ``agent_name`` 为空的老会话 / 机器线程没有可用归属,回落不过滤。
    "control-plane/api/sessions.py::get_session_workspace": "按会话自己的 agent 收口",
    "control-plane/api/sessions.py::download_session_artifact": "按会话自己的 agent 收口",
    "control-plane/api/sessions.py::delete_session_artifact": "按会话自己的 agent 收口",
}


def _site(pkg: str, path: Path, func: str) -> str:
    return f"{pkg}/{path.relative_to(_ROOTS[pkg]).as_posix()}::{func}"


def _enclosing_function(tree: ast.Module, node: ast.AST) -> str:
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    line = getattr(node, "lineno", 0)
    for candidate in ast.walk(tree):
        if not isinstance(candidate, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        end = candidate.end_lineno or candidate.lineno
        if candidate.lineno <= line <= end and (best is None or candidate.lineno > best.lineno):
            best = candidate
    return best.name if best is not None else "<module>"


def _passes_literal_none(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg != "agent_key":
            continue
        return isinstance(kw.value, ast.Constant) and kw.value.value is None
    return False


def _discover_unfiltered_sites() -> set[str]:
    found: set[str] = set()
    for pkg, root in _ROOTS.items():
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _SCOPED_METHODS
                    and _passes_literal_none(node)
                ):
                    found.add(_site(pkg, path, _enclosing_function(tree, node)))
    return found


def _discover_computed_sites() -> set[str]:
    """``agent_key=<Call>`` —— 值是算出来的,静态看不出会不会是 ``None``。"""
    found: set[str] = set()
    for pkg, root in _ROOTS.items():
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _SCOPED_METHODS
                ):
                    continue
                for kw in node.keywords:
                    if kw.arg == "agent_key" and isinstance(kw.value, ast.Call):
                        found.add(_site(pkg, path, _enclosing_function(tree, node)))
    return found


def test_unfiltered_artifact_reads_are_all_registered() -> None:
    """``agent_key=None`` 的调用点集合必须与登记表一致。

    多出一个 = 一条 agent A 看得见 agent B 产物的路径,静默存在。
    少一个 = 某处收口了但表没更新(PR4 之后对外那四条就该从表里删掉)。
    """
    discovered = _discover_unfiltered_sites()
    registered = set(_ALLOWED_UNFILTERED)

    assert discovered == registered, (
        f"不按 agent 过滤的产物查询变了。"
        f"新出现未登记={sorted(discovered - registered)} "
        f"登记了但已消失={sorted(registered - discovered)}"
    )


def test_conditionally_unfiltered_sites_are_all_registered() -> None:
    """``agent_key=<调用>`` 的调用点也要登记 —— 那个调用可能返回 ``None``。

    静态分析看不出返回值会不会是 ``None``,所以这里不判断、只要求「登记 +
    写清理由」。漏登记 = 一条可能不过滤的路径没人看着。
    """
    discovered = _discover_computed_sites()
    registered = set(_ALLOWED_COMPUTED)

    assert discovered == registered, (
        f"用「算出来的 agent_key」查产物的调用点变了。"
        f"新出现未登记={sorted(discovered - registered)} "
        f"登记了但已消失={sorted(registered - discovered)}"
    )


def test_every_registered_exception_carries_a_reason() -> None:
    """两张登记表里都不许有空理由 —— 逃生口必须写清为什么。"""
    blank = sorted(
        site
        for table in (_ALLOWED_UNFILTERED, _ALLOWED_COMPUTED)
        for site, why in table.items()
        if not why.strip()
    )
    assert not blank, f"这些例外没写理由: {blank}"
