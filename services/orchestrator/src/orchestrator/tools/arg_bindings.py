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

from expert_work.protocol import ArgBindingSpec, MCPToolSpec


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


def _copy_json(value: Any) -> Any:
    """递归复制 JSON 容器:``dict`` / ``list`` 各造一份新的,别的原样返回。

    **复制到什么深度、为什么够**:一路到底,但只穿 ``dict`` 与 ``list``。JSON 里除这
    两样之外(``str`` / 数字 / ``bool`` / ``None``)全不可变,共享它们改不坏谁;能改坏
    人的只有这两种容器,所以穿完它们就没有残留的共享可变对象了。

    **为什么不是 ``copy.deepcopy``**:它要按 ``__deepcopy__`` / pickle 协议去复制**任意**
    对象,而这里的 schema 是第三方 MCP 服务的 ``inputSchema``、``json.loads`` 出来的纯
    容器,根本没有那种东西;显式只认 dict/list,行为可预期,也不会顺手把谁的对象克隆出
    副作用来。

    **深度不封顶会不会爆栈**:不会。这份结构本来就是 ``json.loads`` 解析出来的,解析时
    已经受同一套递归上限约束 —— 能进得来的深度,再走一遍同样深的递归就还在限内。封一个
    深度上限反而更糟:超过上限的那截又变成共享,就是另一种「拷贝了一半」。
    """
    if isinstance(value, Mapping):
        return {k: _copy_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy_json(v) for v in value]
    return value


def strip_bound_params(input_schema: Mapping[str, Any], bound: Collection[str]) -> dict[str, Any]:
    """返回一份新 schema:删掉被绑定的参数,并从 ``required`` 里移除。

    可变的 **JSON 结构**(``dict`` / ``list``)每条返回路径都逐层复制过,入参与返回值之间
    没有共享的这类容器 —— 同一个 ``inputSchema`` 对象由 MCP 目录持有、被多个 agent 的注册项
    共享,谁改一下我们交回去的东西,原件就跟着变,别人看到的工具也跟着变。
    JSON 之外的可变值(``set``、包在 ``tuple`` 里的 list、对象属性……)**不复制、仍然共享**:
    :func:`_copy_json` 有意只穿 dict/list。schema 是 ``mcp.py`` 从 wire 上解析出来的,产不出
    那些东西;手工构造的入参不在这条保证里。

    只认**顶层**的 ``properties`` / ``required``。``allOf`` / ``anyOf`` / ``$ref`` 拼出来的
    schema 里参数可能压根不在顶层,那种剥不掉 —— 解析 JSON-Schema 组合是另一个特性、有它
    自己的正确性负担,这里不做。退化行为与下面「形状不对」一致:参数仍被模型看见,但值照旧
    由 :func:`apply_arg_bindings` 用平台的覆盖,事故不会复发。
    """
    out: dict[str, Any] = {k: _copy_json(v) for k, v in input_schema.items()}
    sides = _top_level_params(input_schema)
    # 一侧「在,但形状不对」(第三方给的畸形 schema)→ 整份原样退回,**绝不剥一半**。
    # 半剥的产物 —— 参数从 properties 没了却还留在 required —— 会让一部分厂商判定整个
    # 工具非法,该 agent 的 MCP 工具于是整片失踪,比一个参数没剥掉严重得多。降级,不抛:
    # spec §5.4 要的是别阻断 run。而没剥掉还有第二道闸:填值那步照样覆盖模型给的值。
    if sides is None:
        return out
    props, req = sides
    # properties 与 required 两边都算。只看 properties 的话,一个只写在 required 里的
    # 被绑参数会留在 required 而 properties 里没有 —— 同样是上面那个 dangling-required。
    removed = {name for name in bound if name in props or name in req}
    if not removed:
        return out
    # out 里这两个已经是独立副本,直接筛出新的即可。
    if input_schema.get("properties") is not None:
        out["properties"] = {k: v for k, v in out["properties"].items() if k not in removed}
    if input_schema.get("required") is not None:
        out["required"] = [name for name in out["required"] if name not in removed]
    return out


def unbindable_params(input_schema: Mapping[str, Any], bound: Collection[str]) -> list[str]:
    """``bound`` 里这份 schema **认不出**的参数名,按输入顺序。

    接线层(``register_mcp_tools``)拿它把「上游改了接口、绑的参数已经没了」的
    绑定项从下发给 ``tools_node`` 的表里摘掉。只记一条日志是不够的:留着的话填值
    那步照样把它注入 args,而 ``additionalProperties: false`` 的服务端会把这次
    **本来能跑的**调用整个硬拒 —— 比 spec §5.4 承诺的「按未命中处理」更糟。

    判据与 :func:`strip_bound_params` 共用 :func:`_top_level_params`,必须逐字同义:
    两边一旦分岔就会出现「剥掉了却还当它不存在」(参数已从模型的 schema 消失、绑定
    却被摘掉 → 谁都填不了这个必填参数)或反过来的裂缝。

    schema 形状不对时返回**空**(一个都不报缺):那时 :func:`strip_bound_params`
    也是整份退回、绑定留着,值仍由 :func:`apply_arg_bindings` 用平台的覆盖。既然
    不可判,就不拿它当「上游删了这个参数」的证据。
    """
    sides = _top_level_params(input_schema)
    if sides is None:
        return []
    props, req = sides
    return [name for name in bound if name not in props and name not in req]


def _top_level_params(
    input_schema: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Sequence[Any]] | None:
    """顶层 ``properties`` / ``required`` 两侧,规整成 ``(props, req)``。

    ``None`` 表示「这份 schema 不可判」(至少一侧在、但形状不对),与「一个参数都
    没有」是两回事 —— 两个调用方对 ``None`` 的处置不同,各自有注释。
    """
    properties = input_schema.get("properties")
    required = input_schema.get("required")
    # 缺键与显式 null 都按「这一侧没有」算:没有就没什么可剥的,不构成剥了一半。
    props_ok = properties is None or isinstance(properties, Mapping)
    # 只认 list(JSON 唯一产得出的形状)与 tuple(行为等价,返回成 list)。别的 Sequence
    # 一律按「形状不对」走整份退回那条路:``str`` 逐字符、``bytes`` / ``bytearray``
    # 逐字节(``required=b"pc"`` 会剥成 ``[112, 99]``)、``range`` 逐整数,拆出来全是垃圾;
    # 更糟的是 ``name in req`` 拿 str 去比 bytes 会抛 ``TypeError`` —— 而调用方那句注释
    # 承诺的是「降级,不抛」。用白名单不用黑名单,免得下一个 Sequence 类型又漏进来。
    req_ok = required is None or isinstance(required, list | tuple)
    if not (props_ok and req_ok):
        return None
    props: Mapping[str, Any] = properties if isinstance(properties, Mapping) else {}
    req: Sequence[Any] = required if required is not None else ()
    return props, req


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

    返回的每一个 call 与入参之间没有共享的可变 **JSON 结构**(``dict`` / ``list``,见
    :func:`_copy_json`),未绑定的那条路也一样 —— 入参里的 ``args`` 是 AIMessage 身上活的
    那个 dict,递回去等于把图状态交给下游随便改(``before_tool_dispatch`` 那一层本来就允许
    改写 ``tool_args``),改到的却是 checkpoint 里的那条消息。从 ``inputs`` 取来的值同理,
    复制一份再填。JSON 之外的可变值(``set`` 等)不复制、仍然共享,同 :func:`_copy_json`。
    """
    filled: list[dict[str, Any]] = []
    names: list[str] = []
    for call in tool_calls:
        bound = bindings.get(str(call.get("name", "")))
        new_call: dict[str, Any] = {k: _copy_json(v) for k, v in call.items()}
        if not bound:
            filled.append(new_call)
            continue
        args = dict(new_call.get("args") or {})
        for param, var_name in bound.items():
            if var_name in inputs:
                args[param] = _copy_json(inputs[var_name])
                names.append(param)
            else:
                args.pop(param, None)
        new_call["args"] = args
        filled.append(new_call)
    return filled, names


def manifest_bindings(tools: Sequence[Any]) -> tuple[ArgBindingSpec, ...]:
    """B-67 §五 —— manifest 里全部 ``arg_bindings`` 原样拼平(按 tools 顺序)。

    给 ``BuiltAgent.arg_bindings`` 用:control-plane 的「本轮输入」段据此报告「这个变量
    已绑定到工具参数」。取 spec 不取 registry —— registry 的键是折叠后的 wire 名,B-65 的
    撞名问题不该传染到 control-plane。
    """
    return tuple(
        binding
        for entry in tools
        if isinstance(entry, MCPToolSpec)
        for binding in entry.arg_bindings
    )
