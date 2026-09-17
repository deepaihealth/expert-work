"""B-61 Task 6 —— 绑定的纯函数。"""

from __future__ import annotations

import dataclasses
from typing import Any

from expert_work.protocol import ArgBindingSpec, BuiltinToolSpec, MCPToolSpec
from orchestrator.tools.arg_bindings import (
    apply_arg_bindings,
    bindings_by_tool,
    manifest_bindings,
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


def _nested_schema() -> dict[str, Any]:
    """带一层嵌套的 schema,每次现造一份 —— 纯度用例要改返回值,共享常量会被改花。"""
    return {
        "type": "object",
        "properties": {
            "project_code": {"type": "string"},
            "keyword": {"type": "string", "enum": ["a", "b"]},
        },
        "required": ["project_code", "keyword"],
    }


def test_stripping_an_absent_param_is_a_no_op() -> None:
    assert strip_bound_params(SCHEMA, {"nope"}) == SCHEMA
    # 上面那行单独在这儿证不了「不碰入参」:`==` 分不开「拷贝」和「就是入参那个对象」。
    # 什么都不用剥的这条路最容易漏 —— 直接把入参递回去,`==` 照样绿。
    schema = _nested_schema()
    out = strip_bound_params(schema, {"nope"})
    assert out is not schema
    assert out["properties"] is not schema["properties"]
    assert out["required"] is not schema["required"]
    assert out["properties"]["keyword"] is not schema["properties"]["keyword"]
    assert out["properties"]["keyword"]["enum"] is not schema["properties"]["keyword"]["enum"]
    out["properties"]["keyword"]["enum"].append("z")
    out["properties"]["injected"] = {"type": "string"}
    out["required"].append("injected")
    assert schema == _nested_schema(), "改返回值不能改到入参"


def test_the_stripped_schema_shares_no_mutable_object_with_its_input() -> None:
    """剥过的那条路同样要独立 —— 留下来的参数的子 schema 最容易被共享着递出去。"""
    schema = _nested_schema()
    out = strip_bound_params(schema, {"project_code"})
    assert out["properties"]["keyword"] is not schema["properties"]["keyword"]
    assert out["properties"]["keyword"]["enum"] is not schema["properties"]["keyword"]["enum"]
    out["properties"]["keyword"]["enum"].append("z")
    out["required"].append("injected")
    assert schema == _nested_schema(), "改返回值不能改到入参"


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
    calls: list[dict[str, Any]] = [
        {"name": "exec_python", "args": {"code": "print(1)", "env": {"K": "v"}}, "id": "c1"}
    ]
    filled, names = apply_arg_bindings(calls, bindings={}, inputs={"pc": "x"})
    assert filled == calls
    assert names == []
    # 「原样透传」不等于「把原对象递回去」:入参里的 args 是 AIMessage 身上活的那个
    # dict,递回去等于把图状态交给下游随便改。`==` 分不开这两件事,身份断言才分得开。
    assert filled[0] is not calls[0]
    assert filled[0]["args"] is not calls[0]["args"]
    assert filled[0]["args"]["env"] is not calls[0]["args"]["env"]
    filled[0]["args"]["env"]["K"] = "改了"
    filled[0]["args"]["injected"] = 1
    assert calls[0]["args"] == {"code": "print(1)", "env": {"K": "v"}}


def test_the_filled_call_shares_no_mutable_object_with_call_or_inputs() -> None:
    """绑定过的那条路同样要独立,而且从 ``inputs`` 取来的值也得复制一份再填。"""
    calls: list[dict[str, Any]] = [
        {"name": "mcp__deepcare__t1", "args": {"filters": {"age": [1, 2]}}, "id": "c1"}
    ]
    inputs: dict[str, Any] = {"pc": {"code": "PRJ001", "tags": ["a"]}}
    filled, _ = apply_arg_bindings(
        calls, bindings={"mcp__deepcare__t1": {"project_code": "pc"}}, inputs=inputs
    )
    assert filled[0]["args"]["filters"] is not calls[0]["args"]["filters"]
    assert filled[0]["args"]["project_code"] is not inputs["pc"]
    filled[0]["args"]["filters"]["age"].append(3)
    filled[0]["args"]["project_code"]["tags"].append("b")
    assert calls[0]["args"] == {"filters": {"age": [1, 2]}}
    assert inputs == {"pc": {"code": "PRJ001", "tags": ["a"]}}


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


def test_a_malformed_required_makes_the_whole_strip_a_no_op() -> None:
    """``required`` 在、但不是列表形状 → 一个参数都不剥,而不是只剥 properties 那半边。

    半剥的产物(properties 里没了、required 里还在)正是让一部分厂商拒掉**整个工具**的
    那个形态,该 agent 的 MCP 工具会整片失踪。没剥掉只是模型多看见一个参数,值仍由
    ``apply_arg_bindings`` 用平台的覆盖 —— 两害相权,退回原样。降级,不抛。

    非 list/tuple 的形状一个都不许放行:``str`` 逐字符、``bytes`` / ``bytearray`` 逐字节
    (``b"pc"`` 会剥成 ``[112, 99]``)、``range`` 逐整数,而 bytes 那条还会在 ``name in req``
    处直接抛 ``TypeError`` —— 「降级,不抛」这句承诺要经得起它们。
    """
    bad_shapes: list[Any] = [
        "project_code",
        b"project_code",
        bytearray(b"project_code"),
        range(3),
        {"project_code"},
    ]
    for bad in bad_shapes:
        schema = {"properties": {"project_code": {"type": "string"}}, "required": bad}
        out = strip_bound_params(schema, {"project_code"})
        assert out == schema, f"{type(bad).__name__} 应该整份原样退回"
        assert out is not schema


def test_a_malformed_properties_makes_the_whole_strip_a_no_op() -> None:
    """``properties`` 那一侧的镜像情形,同样不许剥出半成品。"""
    schema = {"properties": ["project_code"], "required": ["project_code"]}
    out = strip_bound_params(schema, {"project_code"})
    assert out == schema
    assert out["required"] == ["project_code"]


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


def test_manifest_bindings_flatten_every_mcp_entry_verbatim() -> None:
    """B-67 §五 —— 渲染层从 ``BuiltAgent.arg_bindings`` 判断哪些变量已绑定;取 manifest
    原件(server / tool 原名、变量名原样),不取 registry 折叠后的 wire 名。"""
    tools = [
        BuiltinToolSpec(name="exec_python"),
        MCPToolSpec(
            servers=["deep-care"],
            arg_bindings=[
                ArgBindingSpec(server="deep-care", tool="t1", args={"pc": "project_code"})
            ],
        ),
        MCPToolSpec(
            servers=["other"],
            arg_bindings=[ArgBindingSpec(server="other", tool="t2", args={"cc": "customer_code"})],
        ),
    ]
    out = manifest_bindings(tools)
    assert [(b.server, b.tool, b.args) for b in out] == [
        ("deep-care", "t1", {"pc": "project_code"}),
        ("other", "t2", {"cc": "customer_code"}),
    ]
    assert manifest_bindings([BuiltinToolSpec(name="bash")]) == ()


def test_built_agent_defaults_to_no_bindings() -> None:
    """存量构造点一处都不传 ``arg_bindings`` —— 字段必须有默认值,且默认就是空。"""
    from orchestrator.built_agent import BuiltAgent

    (field,) = [f for f in dataclasses.fields(BuiltAgent) if f.name == "arg_bindings"]
    factory = field.default_factory
    default = field.default if factory is dataclasses.MISSING else factory()
    assert default == ()
