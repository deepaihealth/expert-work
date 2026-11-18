"""B-317 —— 字面量守卫:不会被展开的 ``$变量名`` / ``$EXPERT_WORK_*`` / 本地路径,拦在派发前。

健管师 154 轮里有一次把字面量 ``$attachment_urls`` 写进了 MCP 表单工具的参数,对方服务收到的
就是这串字。工具参数里的 ``$名字`` 在任何地方都不会被换成值;只有 ``exec_python`` / ``bash`` 的
沙箱里环境变量才有值。

**只查白名单里的三处**(设计 §3.3.1,用户 11-18 定):

1. MCP 工具(``mcp__`` 开头)与 ``http`` 工具:参数里全部字符串叶子(平台绑定填进去的参数除外
   —— 那是租户数据);**本轮有输入时**,整段是本轮输入的本地路径(``inputs/current/…`` 或
   ``inputs/<本轮 run>/…``)也拦(对方读不到沙箱里的文件)。没有输入的智能体不查这一条:别的
   服务完全可能有自己的 ``inputs/…`` 路径(仓库类 MCP 的 ``get_file_contents``),拦了绕不开。
2. 平台工具的工作区路径参数:本模块自己维护的 :data:`PATH_ARGS` 表。**不用**
   ``ToolSpec.path_args``:那是调度器的冲突键,``save_artifact`` 的是产物名 ``name``,真正的
   路径参数是 ``path``。
3. ``ask_image.path``(在同一张表里)。

其余一律不查 —— 委派任务、计划、技能编写、记忆、写文件的内容、搜索查询:平台技能大量教
``$EXPERT_WORK_SKILLS_DIR/...`` 的写法,在子代和之后执行的脚本里都是有效的,拦了反而改坏。

判定是纯函数;接线在 ``builder.tools_node``(与手抄 URL 守卫同一处,审批门之后)。
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from orchestrator.tools.inputs_doc import CURRENT_INPUTS_REL

#: 外部服务:参数原样发出去,对方不展开任何东西、也读不到沙箱里的文件。
_MCP_PREFIX = "mcp__"
_HTTP_TOOL = "http"

#: 平台工具的工作区路径参数。新加接受工作区路径的工具,要在这里登记。
PATH_ARGS: Mapping[str, tuple[str, ...]] = {
    "read_file": ("path",),
    "write_file": ("path",),
    "edit_file": ("path",),
    "list_dir": ("path",),
    "read_document": ("path",),
    "read_page": ("path",),
    "save_artifact": ("path",),
    "ask_image": ("path",),
}

#: ``$名字`` / ``${名字}``:``$`` 后面**完整的**标识符(贪婪取完),再与集合比。
_DOLLAR_RE = re.compile(r"\$(\{)?([A-Za-z_][A-Za-z0-9_]*)(?(1)\})")
_ENV_PREFIX = "EXPERT_WORK_"
_INPUTS_DIR_ENV = "EXPERT_WORK_INPUTS_DIR"
_SKILLS_DIR_ENV = "EXPERT_WORK_SKILLS_DIR"
#: 路径参数开头的这两种写法由 ``resolve_workspace_path`` 当别名认(会话历史里的旧段、租户模板)。
_ALIAS_PREFIXES = (f"${_INPUTS_DIR_ENV}/", f"${{{_INPUTS_DIR_ENV}}}/")


def _local_input_re(inputs_run: str) -> re.Pattern[str]:
    """整段是本轮输入的本地路径(规则 3):``inputs/current/`` 或 ``inputs/<本轮 run>/`` 开头,
    可带 ``/workspace/``,整段不含空白。不认 ``inputs/cache/`` 与别的 run。"""
    return re.compile(rf"(?:/workspace/)?inputs/(?:current|{re.escape(inputs_run)})/\S+")


Kind = Literal["variable", "env", "local_path"]


@dataclass(frozen=True)
class LiteralHit:
    tool: str
    #: 参数名(嵌套时是最外层那个参数)。
    arg: str
    kind: Kind
    #: 变量名或环境变量名;``local_path`` 时为空串。只有名字,没有值。
    name: str
    remote: bool
    #: ``kind == "variable"`` 且这个变量绑定在本工具上:绑定的参数名。
    bound_param: str | None = None


def _strings(value: Any) -> Iterator[str]:
    """字符串叶子,深度优先、按给定顺序;不看字典键。"""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def _dollar_hit(text: str, names: Collection[str], *, path_arg: bool) -> tuple[Kind, str] | None:
    # 解析函数先 ``strip()`` 再认别名(``resolve_workspace_path``),这里同一个口径。
    lead = len(text) - len(text.lstrip())
    aliased = path_arg and text[lead:].startswith(_ALIAS_PREFIXES)
    for match in _DOLLAR_RE.finditer(text):
        name = match.group(2)
        if aliased and match.start() == lead:
            continue
        if name.startswith(_ENV_PREFIX):
            return "env", name
        if name in names:
            return "variable", name
    return None


def find_literal(
    tool: str,
    args: Mapping[str, Any],
    *,
    names: Collection[str],
    skip_args: Collection[str] = (),
    bound_params: Mapping[str, str] | None = None,
    inputs_run: str | None = None,
) -> LiteralHit | None:
    """一次调用里第一处命中(按参数顺序、深度优先);不在受检面上或没有命中是 ``None``。

    ``names`` 是声明过的变量名;``skip_args`` 是这次被平台绑定填过的参数;``bound_params`` 是
    本工具的「变量名 → 绑定参数名」,只用来把提示说具体;``inputs_run`` 是本轮输入目录的 run
    (``None`` = 本轮没有输入,规则 3 不查)。

    规则 2(``$EXPERT_WORK_*``)不看本轮有没有输入:这个前缀归平台所有,外部服务与路径参数里
    出现它只可能是模型照抄了沙箱环境变量,没有合法的字面用途;``$EXPERT_WORK_SKILLS_DIR`` 的
    提示也要对没声明变量的智能体成立。
    """
    local_input = _local_input_re(inputs_run) if inputs_run else None
    remote = tool.startswith(_MCP_PREFIX) or tool == _HTTP_TOOL
    if remote:
        checked = [(arg, value) for arg, value in args.items() if arg not in skip_args]
    elif tool in PATH_ARGS:
        checked = [(arg, args[arg]) for arg in PATH_ARGS[tool] if arg in args]
    else:
        return None
    for arg, value in checked:
        for text in _strings(value):
            dollar = _dollar_hit(text, names, path_arg=not remote)
            if dollar is not None:
                kind, name = dollar
                bound = (bound_params or {}).get(name) if kind == "variable" else None
                return LiteralHit(tool, arg, kind, name, remote, bound)
            if remote and local_input is not None and local_input.fullmatch(text.strip()):
                return LiteralHit(tool, arg, "local_path", "", remote)
    return None


def literal_guard_message(hit: LiteralHit) -> str:
    """回给模型的那句话(设计 §4.5)。只有参数名、变量名、绑定参数名、环境变量名。"""
    arg = hit.arg
    if hit.kind == "local_path":
        return (
            f"[blocked] 参数 {arg} 是工作区里的本地路径，对方服务读不到沙箱里的文件。"  # noqa: RUF001
            "要传文件地址，请用绑定（由平台填）或原始链接。"  # noqa: RUF001
        )
    if hit.kind == "variable":
        var = hit.name
        if hit.bound_param is not None:
            return (
                f"[blocked] 参数 {arg} 里的 ${var} 不会被换成值，收到的就是这串字。"  # noqa: RUF001
                f"{var} 由平台自动填进本工具的 {hit.bound_param} 参数"
                "（它不在你看到的参数表里），这里不用写，去掉后重新调用。"  # noqa: RUF001
            )
        if hit.remote:
            return (
                f"[blocked] 参数 {arg} 里的 ${var} 不会被换成值，对方服务收到的就是这串字。"  # noqa: RUF001
                f"请写出要传的实际内容；如果 {var} 由平台填进绑定它的工具参数，就不用写。"  # noqa: RUF001
            )
        return (
            f"[blocked] 参数 {arg} 里的 ${var} 不会被换成值。"
            f"要用 {var} 的文件，写 {CURRENT_INPUTS_REL}/ 下的路径（见「本轮输入」段）。"  # noqa: RUF001
        )
    name = hit.name
    if hit.remote:
        return (
            f"[blocked] 参数 {arg} 里的 ${name} 只在 exec_python / bash 里有值，"  # noqa: RUF001
            "对方服务收到的就是这串字。"
        )
    head = f"[blocked] 参数 {arg} 里的 ${name} 只在 exec_python / bash 里有值。"
    if name.startswith("EXPERT_WORK_INPUTS"):
        return f"{head}要用本轮输入文件，写 {CURRENT_INPUTS_REL}/ 下的路径。"  # noqa: RUF001
    if name == _SKILLS_DIR_ENV:
        return f"{head}读技能文件用 skill_view。"
    return head
