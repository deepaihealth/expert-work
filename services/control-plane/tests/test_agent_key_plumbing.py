"""每一条「起 agent 图」的路径都必须把 ``agent_key`` 放进 ``configurable``。

``agent_key`` 是工作区分层的唯一归属依据(``ToolContext.agent_key`` →
文件工具的根目录)。漏掉一条路径不会报错:那条路径下的工具**静默**回落到
用户根,于是同一个用户的两个 agent 又开始互相读写对方的文件 —— 正是本
program 要根治的形态。

这条不变式的形态是「有没有漏掉某一处」,所以用 AST 穷举而不是逐条跑真调用:
七个构造点各自要一整套 store 桩(队列 worker / SSE 建 run / 审批续跑 / 触发器 /
孤儿复活 / 技能演化 replay / 委派子代),跑真调用的成本远大于收益,而且逐个
点名的测试恰恰逮不住「新加了第八条路径」。

仓库里有过先例:run 计费 trace 绑定那次「执行入口三个,规矩只写一处就漏两个」
(#1373 + #1382)。本 program 的实施计划一开始登记了**四个**入口,实测是**九个**
调用点 / **七个**构造点,并且四个里有三个的函数名是错的。所以这里登记的是
「调用点 → 构造点」的完整映射,由 :func:`test_graph_launch_inventory_is_complete`
盯住,新增路径漏登记会红。
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: 包名 → 源码根。键就是下方站点字符串的第一段。
_PKG_ROOTS = {
    "control-plane": _REPO_ROOT / "services" / "control-plane" / "src" / "control_plane",
    "orchestrator": _REPO_ROOT / "services" / "orchestrator" / "src" / "orchestrator",
}

#: 每个「起 agent 图」的调用点 → ``(构造它 configurable 的位置, 是否必须现算 agent_key)``。
#:
#: ``None`` = 这条路径不落用户工作区,不需要 ``agent_key``(理由写在各行)。
#: 第二项 ``False`` = 该处透传上游已经算好的值,不该再算一遍
#: (Global Constraint:``sanitize_agent_key`` 只许有一种算法)。
_LAUNCH_SITES: dict[str, tuple[str, bool] | None] = {
    # —— 用户的五条真 run 入口,各自就地建 configurable ——
    "control-plane/api/runs.py::spawn_run": (
        "control-plane/api/runs.py::spawn_run",
        True,
    ),
    "control-plane/api/runs.py::resolve_approval_decision": (
        "control-plane/api/runs.py::resolve_approval_decision",
        True,
    ),
    "control-plane/run_queue_worker.py::_execute": (
        "control-plane/run_queue_worker.py::_execute",
        True,
    ),
    "control-plane/trigger_firing.py::fire_trigger": (
        "control-plane/trigger_firing.py::fire_trigger",
        True,
    ),
    "control-plane/orphan_sweep.py::_respawn": (
        "control-plane/orphan_sweep.py::_respawn",
        True,
    ),
    # —— 技能演化 replay:config 由工厂产,不在调用点就地建 ——
    # 它带着**真实的** tenant_id / user_id 跑完整 agent 图(含文件工具),
    # 所以它写的东西落在真实用户的工作区里。漏了这一处 = replay 看不见被
    # replay 的那个 agent 自己的文件,held-out 判定失真。
    "orchestrator/evolution/graph_runner.py::run": (
        "control-plane/skill_evolution_wiring.py::_make_replay_config_factory",
        True,
    ),
    # —— 委派子代:透传父的 agent_key ——
    # 子代(worker / 静态子 Agent)干的是**父 agent 的活**,产物必须落在父的
    # 子树里,否则父读不到自己 worker 刚写的文件。与技能种子路径同样的取值
    # 口径(用父 key,不是 worker 自己的 key)。
    "orchestrator/tools/_child_run.py::run_child_to_result": (
        "orchestrator/tools/_child_run.py::_child_config",
        False,
    ),
    # —— 以下两处不需要 ——
    # 对抗评测建的是内联 spec,连 tenant_id / user_id 都不给:没有用户工作区可言。
    "control-plane/eval_engine_live.py::_run": None,
    # merge 点不是构造点:它 ``**`` 展开调用方传进来的 configurable,
    # agent_key 原样穿过。``RunRecord`` 里也没有 agent 身份,拿不到也不该在这里塞。
    "orchestrator/sse.py::run_agent": None,
}


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _site(pkg: str, path: Path, func: str) -> str:
    return f"{pkg}/{path.relative_to(_PKG_ROOTS[pkg]).as_posix()}::{func}"


def _launch_kind(call: ast.Call) -> str | None:
    """这个调用是不是「起一张 agent 图」?"""
    func = call.func
    if isinstance(func, ast.Name) and func.id == "run_agent":
        return "run_agent"
    if isinstance(func, ast.Attribute) and func.attr in ("ainvoke", "astream", "invoke", "stream"):
        value = func.value
        # ``graph.astream(...)`` —— 图是本函数的参数
        if isinstance(value, ast.Name) and value.id == "graph":
            return f"graph.{func.attr}"
        # ``built.graph.ainvoke(...)`` / ``child.graph.astream(...)``
        if isinstance(value, ast.Attribute) and value.attr == "graph":
            return f"*.graph.{func.attr}"
    return None


def _enclosing_function(tree: ast.Module, node: ast.AST) -> str:
    """最内层包住 ``node`` 的函数名;不在任何函数里则 ``"<module>"``。"""
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    line = getattr(node, "lineno", 0)
    for candidate in ast.walk(tree):
        if not isinstance(candidate, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        end = candidate.end_lineno or candidate.lineno
        if candidate.lineno <= line <= end and (best is None or candidate.lineno > best.lineno):
            best = candidate
    return best.name if best is not None else "<module>"


def _discover_launch_sites() -> set[str]:
    found: set[str] = set()
    for pkg, root in _PKG_ROOTS.items():
        for path in sorted(root.rglob("*.py")):
            tree = _parse(path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _launch_kind(node) is not None:
                    found.add(_site(pkg, path, _enclosing_function(tree, node)))
    return found


def _function_node(site: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    pkg, rest = site.split("/", 1)
    rel, func = rest.split("::", 1)
    tree = _parse(_PKG_ROOTS[pkg] / rel)
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == func
    ]
    assert len(matches) == 1, f"{site}: 期望正好一个同名函数,实际 {len(matches)} 个"
    return matches[0]


def _sets_agent_key(node: ast.AST) -> bool:
    """函数体里有没有往某个 mapping 写 ``agent_key``(下标赋值或字典字面量)。"""
    for sub in ast.walk(node):
        # configurable["agent_key"] = ...
        if (
            isinstance(sub, ast.Subscript)
            and isinstance(sub.slice, ast.Constant)
            and sub.slice.value == "agent_key"
        ):
            return True
        # {"agent_key": ...}
        if isinstance(sub, ast.Dict) and any(
            isinstance(key, ast.Constant) and key.value == "agent_key" for key in sub.keys
        ):
            return True
    return False


def _calls(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == name
        for sub in ast.walk(node)
    )


def test_graph_launch_inventory_is_complete() -> None:
    """全仓「起 agent 图」的调用点集合,必须与 ``_LAUNCH_SITES`` 登记的一致。

    漏登记一条新路径 = 那条路径的 ``agent_key`` 永远为空 = 该路径下所有文件
    工具静默回落用户根。这条测试就是防这个 —— 新增路径时把它加进
    ``_LAUNCH_SITES`` 并想清楚它该不该带 ``agent_key``。
    """
    discovered = _discover_launch_sites()
    registered = set(_LAUNCH_SITES)

    assert discovered == registered, (
        f"起 agent 图的调用点集合变了。"
        f"新出现未登记={sorted(discovered - registered)} "
        f"登记了但已消失={sorted(registered - discovered)}"
    )


def test_every_user_run_config_sets_agent_key() -> None:
    """每个「要 agent_key」的构造点都真的写了 ``agent_key``。"""
    missing = sorted(
        {
            builder
            for entry in _LAUNCH_SITES.values()
            if entry is not None
            for builder, _ in [entry]
            if not _sets_agent_key(_function_node(builder))
        }
    )

    assert not missing, f"这些构造点没往 configurable 塞 agent_key: {missing}"


def test_agent_key_is_always_the_one_canonical_algorithm() -> None:
    """现算 ``agent_key`` 的构造点必须调 ``sanitize_agent_key``,透传的必须不调。

    Global Constraint:``agent_key`` 只许有一种算法(与 ``/opt/skills/<agent_key>``
    同源)。就地写 ``meta.agent_name`` 这类未净化的值会让工作区路径与沙箱里的
    技能路径对不上,而且 agent 名没有字符集约束(见 manifest program 的
    log-injection 结论)——未净化的值直接进路径是真汇点。
    """
    wrong: list[str] = []
    for entry in _LAUNCH_SITES.values():
        if entry is None:
            continue
        builder, must_compute = entry
        node = _function_node(builder)
        if _calls(node, "sanitize_agent_key") is not must_compute:
            wrong.append(
                f"{builder}(期望{'现算' if must_compute else '透传'},"
                f"实际{'调了' if not must_compute else '没调'} sanitize_agent_key)"
            )

    assert not wrong, "; ".join(wrong)
