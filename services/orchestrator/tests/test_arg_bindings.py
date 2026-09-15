"""B-61 Task 6 —— 绑定的纯函数。"""

from __future__ import annotations

from expert_work.protocol import ArgBindingSpec
from orchestrator.tools.arg_bindings import (
    apply_arg_bindings,
    bindings_by_tool,
    strip_bound_params,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "project_code": {"type": "string"},
        "keyword": {"type": "string"},
    },
    "required": ["project_code", "keyword"],
}


def test_bindings_are_selected_by_server() -> None:
    entries = [
        ArgBindingSpec(server="deepcare", tool="t1", args={"project_code": "pc"}),
        ArgBindingSpec(server="other", tool="t1", args={"project_code": "pc"}),
    ]
    assert bindings_by_tool(entries, server="deepcare") == {"t1": {"project_code": "pc"}}


def test_stripping_removes_the_property_and_the_required_entry() -> None:
    stripped = strip_bound_params(SCHEMA, {"project_code"})
    assert set(stripped["properties"]) == {"keyword"}
    assert stripped["required"] == ["keyword"]
    # 原 schema 不变
    assert set(SCHEMA["properties"]) == {"project_code", "keyword"}


def test_stripping_an_absent_param_is_a_no_op() -> None:
    assert strip_bound_params(SCHEMA, {"nope"}) == SCHEMA


def test_apply_fills_the_bound_arg_from_this_runs_inputs() -> None:
    calls = [{"name": "mcp__deepcare__t1", "args": {"keyword": "王"}, "id": "c1"}]
    filled, names = apply_arg_bindings(
        calls,
        bindings={"mcp__deepcare__t1": {"project_code": "pc"}},
        inputs={"pc": "PRJ001"},
    )
    assert filled[0]["args"] == {"keyword": "王", "project_code": "PRJ001"}
    assert names == ["project_code"]
    assert calls[0]["args"] == {"keyword": "王"}, "原 tool_calls 不可变"


def test_a_missing_optional_variable_leaves_the_arg_unfilled() -> None:
    calls = [{"name": "mcp__deepcare__t1", "args": {}, "id": "c1"}]
    filled, names = apply_arg_bindings(
        calls, bindings={"mcp__deepcare__t1": {"project_code": "pc"}}, inputs={}
    )
    assert filled[0]["args"] == {}
    assert names == []


def test_a_model_supplied_value_never_wins_over_the_binding() -> None:
    """schema 里已经没这个参数了,模型还是塞了一个 —— 平台的值覆盖它。"""
    calls = [{"name": "mcp__deepcare__t1", "args": {"project_code": "伪造"}, "id": "c1"}]
    filled, _ = apply_arg_bindings(
        calls, bindings={"mcp__deepcare__t1": {"project_code": "pc"}}, inputs={"pc": "PRJ001"}
    )
    assert filled[0]["args"]["project_code"] == "PRJ001"


def test_unbound_tools_pass_through_untouched() -> None:
    calls = [{"name": "exec_python", "args": {"code": "print(1)"}, "id": "c1"}]
    filled, names = apply_arg_bindings(calls, bindings={}, inputs={"pc": "x"})
    assert filled == calls
    assert names == []


# --------------------------------------------------------------------------
# 以下是 brief 之外补的:四条判据 brief 的用例都分不开真假实现。
# --------------------------------------------------------------------------


def test_another_servers_binding_never_leaks_into_this_servers_map() -> None:
    """上面那条其实证不了按 server 过滤。

    它两个条目的 ``tool`` 与 ``args`` 一模一样,把 ``if e.server == server`` 整个删掉,
    后者覆盖前者,结果一字不差还是绿的。换成 tool 和 args 都不同,过滤没了就会漏进来。
    """
    entries = [
        ArgBindingSpec(server="deepcare", tool="t1", args={"project_code": "pc"}),
        ArgBindingSpec(server="other", tool="t2", args={"patient_id": "pid"}),
    ]
    assert bindings_by_tool(entries, server="deepcare") == {"t1": {"project_code": "pc"}}


def test_bindings_by_tool_hands_back_a_copy_not_the_spec_s_own_dict() -> None:
    """返回值被下游改了不能回头改到 spec —— spec 对象在 BuiltAgent 缓存里活很久。"""
    entries = [ArgBindingSpec(server="deepcare", tool="t1", args={"project_code": "pc"})]
    out = bindings_by_tool(entries, server="deepcare")
    out["t1"]["project_code"] = "被改了"
    assert entries[0].args == {"project_code": "pc"}


def test_a_bound_param_listed_only_in_required_is_still_dropped_from_required() -> None:
    """MCP 服务端给的 schema 可能把参数写进 required 却漏在 properties 外。

    留在 required 里而 properties 里没有,一部分厂商会直接判整个工具非法,
    于是这个 agent 的**所有** MCP 工具一起失踪 —— 剥离必须把 required 和 properties
    两边都算上,不能只看 properties。
    """
    schema = {"type": "object", "properties": {}, "required": ["project_code"]}
    assert strip_bound_params(schema, {"project_code"})["required"] == []


def test_a_forged_value_is_dropped_when_the_variable_is_absent() -> None:
    """变量本轮没传,模型又塞了个值:平台的值没有,模型的值也不算数。

    被绑的参数只有一个合法来源。变量缺席时放行模型自己编的串,正好就是本特性
    要消灭的那个故障(模型手抄长串抄错),只是换了条路进来。
    """
    calls = [{"name": "mcp__deepcare__t1", "args": {"project_code": "伪造", "k": "1"}}]
    filled, names = apply_arg_bindings(
        calls, bindings={"mcp__deepcare__t1": {"project_code": "pc"}}, inputs={}
    )
    assert filled[0]["args"] == {"k": "1"}
    assert names == []
    assert calls[0]["args"] == {"project_code": "伪造", "k": "1"}, "原 tool_calls 不可变"
