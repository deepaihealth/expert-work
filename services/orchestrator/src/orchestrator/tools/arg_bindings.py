"""B-61 §五 —— 声明变量绑定到 MCP 工具参数,全是纯函数。

两件事分在两层(spec §5.2 / §5.3):

* **剥 schema** 在建工具目录时做 —— 绑定属于 spec 而非 run,跟 BuiltAgent 缓存天然一致。
* **填值** 在 ``tools_node`` 最前面做 —— 审批门与 action screening 都在 dispatch 之前
  读 args,填在最前面,人审批时看到的是真值。

平台的值**永远覆盖**模型给的同名参数:schema 里已经没有那个字段了,模型还塞进来,
只可能是幻觉或注入。

名字在这一层一律**原样**传递,不折叠、不校验形状。协议层存的是原始 ``server`` /
``tool``,而 registry 的键是 ``mcp_tool_name()`` 折过的 wire 名(非 ``[a-zA-Z0-9_-]``
折 ``_``、截断 64),两者不是一一对应;这个换算归接线那一层,本模块不引入运行期命名
规则。所以 :func:`bindings_by_tool` 吐的是**裸**工具名,而 :func:`apply_arg_bindings`
收的是 tool_call 里实际出现的那个名 —— 对齐由调用方负责。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any

from expert_work.protocol import ArgBindingSpec


def bindings_by_tool(
    entries: Sequence[ArgBindingSpec], *, server: str
) -> dict[str, dict[str, str]]:
    """该服务器下的 ``{裸工具名: {参数名: 变量名}}``。

    可以跨 mcp 条目拼:``(server, tool)`` 在整份 manifest 里唯一是协议层校验保证的
    (``AgentSpecBody._check_arg_bindings``),这里不会有后来者静默覆盖前者。

    ``dict(e.args)`` 是**拷贝**不是引用:spec 对象活在 BuiltAgent 缓存里,下游改一下
    返回值就会改到缓存里的 spec,下一个 run 拿到的是被污染的绑定。
    """
    return {e.tool: dict(e.args) for e in entries if e.server == server}


def strip_bound_params(input_schema: Mapping[str, Any], bound: Collection[str]) -> dict[str, Any]:
    """返回一份新 schema:删掉被绑定的参数,并从 ``required`` 里移除。

    输入原样不动 —— 同一个 schema 对象来自 MCP 目录、多个 agent 共享,就地改会串味。
    """
    properties = input_schema.get("properties")
    props: Mapping[str, Any] = properties if isinstance(properties, Mapping) else {}
    required = input_schema.get("required")
    # str 自己也是 Sequence,当成列表迭代会逐字符拆成垃圾,必须单独排除。
    req: Sequence[Any] | None = (
        required if isinstance(required, Sequence) and not isinstance(required, str) else None
    )
    # properties 与 required 两边都算。只看 properties 的话,一个只写在 required 里的
    # 被绑参数会留在 required 而 properties 里没有 —— 部分厂商据此判定整个工具非法,
    # 该 agent 的 MCP 工具会整片失踪,比漏剥一个参数严重得多。
    removed = {name for name in bound if name in props or (req is not None and name in req)}
    if not removed:
        return dict(input_schema)
    out = dict(input_schema)
    if isinstance(properties, Mapping):
        out["properties"] = {k: v for k, v in props.items() if k not in removed}
    if req is not None:
        out["required"] = [name for name in req if name not in removed]
    return out


def apply_arg_bindings(
    tool_calls: Sequence[Mapping[str, Any]],
    *,
    bindings: Mapping[str, Mapping[str, str]],
    inputs: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """把绑定的参数填进每个 tool_call,返回(新 calls, 实际填入的参数名)。

    变量本轮没传(``required: false`` 的可选变量)→ 该参数**不填,并且把模型塞进来的
    同名值删掉**。被绑的参数只有平台一个来源;平台没值时放行模型自己编的串,等于给
    「模型手抄长串」留了条后门,而那正是本特性要消灭的故障。参数缺席之后 MCP 服务端
    会按自己的 required 报错给模型,模型看得懂、能改道 —— 不静默。

    第二项只有**参数名**,绝不含值:它是给审计看的,而这些值就是客户的真实资料
    (项目号、姓名)。
    """
    filled: list[dict[str, Any]] = []
    names: list[str] = []
    for call in tool_calls:
        bound = bindings.get(str(call.get("name", "")))
        if not bound:
            filled.append(dict(call))
            continue
        args = dict(call.get("args") or {})
        for param, var_name in bound.items():
            if var_name in inputs:
                args[param] = inputs[var_name]
                names.append(param)
            else:
                args.pop(param, None)
        filled.append({**call, "args": args})
    return filled, names
