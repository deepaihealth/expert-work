"""B-302 PR 1 —— 每个起跑入口都写时限与本段令牌;每轮清零记账(spec §4.1.2、§4.1.6)。

T1-5:用户 run 的 5 个入口(对话、队列、触发器、孤儿恢复、审批 / 澄清续跑)都调同一个
``run_segment_config``,传的是这次构建实际生效的时限。审批续跑以前一个都没有 —— 续跑那段
完全不限时。入口清单借 ``test_agent_key_plumbing`` 的穷举(新加入口会先在那边红)。

T1-8:新一轮与重新生成的图输入把三个记账键一起清掉(只清已用、不清令牌的话,下一轮第一步
会把上一轮的令牌当成同一段),「已提醒」也清零。

T1-9 第一项:平台默认 5400 秒。
"""

from __future__ import annotations

import ast
from uuid import uuid4

from langchain_core.messages import HumanMessage, SystemMessage

from control_plane.api.runs import build_run_graph_input, replay_graph_input
from control_plane.settings import Settings
from orchestrator import BuiltAgent
from orchestrator.agent_factory import _effective_run_deadline_s

from .test_agent_key_plumbing import _LAYOUT_BUILDERS, _function_node

#: 用户 run 的五个入口(构造 configurable 的函数)。
_ENTRY_BUILDERS = {
    "control-plane/api/runs.py::_start_run",
    "control-plane/api/runs.py::resolve_approval_decision",
    "control-plane/run_queue_worker.py::_execute",
    "control-plane/trigger_firing.py::fire_trigger",
    "control-plane/orphan_sweep.py::_respawn",
}


def _segment_calls(node: ast.AST) -> list[ast.Call]:
    return [
        sub
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Name)
        and sub.func.id == "run_segment_config"
    ]


def _passes_built_deadline(call: ast.Call) -> bool:
    args = [*call.args, *(kw.value for kw in call.keywords)]
    return any(
        isinstance(arg, ast.Attribute)
        and arg.attr == "run_deadline_s"
        and isinstance(arg.value, ast.Name)
        and arg.value.id == "built"
        for arg in args
    )


def _writes_deadline_at(node: ast.AST) -> bool:
    return any(
        isinstance(sub, ast.Subscript)
        and isinstance(sub.slice, ast.Constant)
        and sub.slice.value == "deadline_at"
        for sub in ast.walk(node)
    )


def test_t1_5_entry_builders_are_every_user_run_launch() -> None:
    """五个入口就是 B-236 穷举出的全部用户 run 构造点(委派子代另走 ``_child_config``)。

    守卫(旧代码也绿):咬住「新加了一个用户 run 入口、登记进了 B-236 的穷举,却没进这里」——
    那个入口的时限检查会被下面两条静默跳过。
    """
    user_runs = {site for site in _LAYOUT_BUILDERS if site.startswith("control-plane/")}
    assert user_runs == _ENTRY_BUILDERS


def test_t1_5_every_entry_writes_the_segment_config() -> None:
    missing = sorted(
        site
        for site in _ENTRY_BUILDERS
        if not any(_passes_built_deadline(c) for c in _segment_calls(_function_node(site)))
    )
    assert not missing, f"这些入口没写时限与本段令牌:{missing}"


def test_t1_5_no_entry_still_writes_a_process_local_deadline() -> None:
    """旧做法:各入口自己算 ``time.monotonic() + 秒数`` 写进 ``deadline_at`` —— 换实例失效、
    孤儿恢复给满额。现在由 tools 节点按检查点里的已用秒数现算。"""
    stale = sorted(site for site in _ENTRY_BUILDERS if _writes_deadline_at(_function_node(site)))
    assert not stale, f"这些入口还在写 deadline_at:{stale}"


def test_t1_8_every_turn_input_resets_the_run_clock_together() -> None:
    built = BuiltAgent(graph=None, system_prompt="sys", max_steps=3)  # type: ignore[arg-type]
    fresh = build_run_graph_input(
        built, input_text="hi", image_refs=[], untrusted_content=None, run_id=uuid4()
    )
    replay = replay_graph_input(
        built, [SystemMessage(content="sys"), HumanMessage(content="hi")], run_id=uuid4()
    )
    for graph_input in (fresh, replay):
        assert graph_input["run_active_s"] == 0.0
        assert graph_input["run_clock_mark"] is None
        assert graph_input["run_clock_segment"] is None
        assert graph_input["deadline_warned"] is False


def test_t1_9_platform_default_is_90_minutes() -> None:
    default = Settings.model_fields["default_run_deadline_s"].default
    assert default == 5400
    assert _effective_run_deadline_s(0, default) == 5400
    assert _effective_run_deadline_s(86_400, default) == 86_400
    assert _effective_run_deadline_s(0, 0) == 0
