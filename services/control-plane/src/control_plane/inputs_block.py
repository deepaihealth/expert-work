"""B-67 §六 —— 平台生成的「本轮输入」段:只有名字、说明、状态、路径,**零值**。

B-317 —— 段里**只陈述事实,不下命令**。「代码里从清单读、别手抄」只写在 ``exec_python`` /
``bash`` 的描述里(``inputs_doc.INPUTS_CODE_NOTE``);这里每轮都出现,写了命令,纯对话的
智能体也会被引去跑代码读值(健管师 154 轮 19 次)。文件一律写短路径 ``inputs/current/…``,
所有工具都认(``workspace_paths.resolve_workspace_path`` 与 ``exec_view``)。

作为隐藏 ``HumanMessage`` 由 ``api/runs.build_run_graph_input`` 贴在用户消息**之后**:
首轮时它是上下文最后一条(注意力最好的位置);不拼进用户自己的消息(那条经 ``/messages``
原样回给对接方)。进模型、进 durable 记录与镜像;不进对外会话消息、控制台气泡、对话条目
(``expert_work_hide_from_ui`` 现成惯用法)。

「已下载」不能断言(渲染早于预拉):有文件时只说一次「预先下载失败的不在」,补救办法在代码
工具描述里。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any

from langchain_core.messages import HumanMessage

from expert_work.common.conversation_channel import HIDE_FROM_UI, INPUTS_BLOCK_MARK
from orchestrator.tools.inputs_doc import (
    CURRENT_INPUTS_REL,
    current_input_path,
    linked_sites,
    parse_json_value,
)

#: 标在隐藏消息 ``additional_kwargs`` 上:``:regenerate`` 重放原件时据此认出它并一起带走
#: (其它隐藏 HumanMessage —— 委派提醒、恢复建议 —— 不带);``graph_builder`` 压缩之后
#: 也靠它把最新一段放回去。常量住在 common(orchestrator 不能 import control-plane),
#: 这里再导出一次,原有调用点不动。
__all__ = [
    "FILES_FACT",
    "FILE_READING_TOOLS",
    "HEADER",
    "INPUTS_BLOCK_MARK",
    "block_stats",
    "build_inputs_block",
    "inputs_block_message",
    "is_inputs_block",
    "reads_files_from_catalog",
]
HEADER = "[本轮输入]（平台自动生成）"  # noqa: RUF001 — 面向模型的中文全角标点
#: 裁定 P10 —— 被 ``arg_bindings`` 引用的变量在模板里照形态渲染,绑定只在本段说一次,
#: 接在形态状态之后。B-317:只说事实,不列工具名(参数已从 schema 删掉,列了也没用,还每轮
#: 多一二百 token);写错时守卫的提示会点名是本工具的哪个参数。
_BOUND = "由平台填进绑定它的工具参数"
_UNSET = "本轮未提供"
_TEXT = "已提供"
_UNTRUSTED_TEXT = "已提供（外部数据）"  # noqa: RUF001
#: B-317 —— 有文件且这个智能体有读文件的工具时,段里说一次的那句事实。
FILES_FACT = f"本轮输入文件在 {CURRENT_INPUTS_REL}/ 下（预先下载失败的不在）。"  # noqa: RUF001
#: 说明是 manifest 里的自由文本:压成一行、最多这么多字,超出加「…」。
_DESC_MAX_CHARS = 40


def _status(
    var: Any, inputs: Mapping[str, Any], bindings: Collection[str], *, reads_files: bool
) -> str:
    """一个变量的状态行。**不含任何租户数据**(裁定 P8):只有变量名、计数、平台文本,
    以及 trusted 变量顶层 URL 的链接名(= 变量名 + ``[A-Za-z0-9]{1,5}`` 扩展名)。untrusted
    的顶层 URL(裁定 P16)只说「以变量名开头的文件」;列表项说明、dict 键这类从值推出来的
    名字只用占位符 ``<下标-说明>`` / ``<字段名>`` 指代。

    先判「本轮未提供」(裁定 P12):可选变量被绑定但本轮没传,只说没提供 —— 那条绑定
    本轮也填不出值。传了的变量先给形态状态,被绑定的再接 :data:`_BOUND`(裁定 P10)。
    """
    if var.name not in inputs:
        return _UNSET
    shape = _shape_status(var, inputs[var.name], reads_files=reads_files)
    return f"{shape}；{_BOUND}" if var.name in bindings else shape  # noqa: RUF001


def _shape_status(var: Any, raw: Any, *, reads_files: bool) -> str:
    """传了值的变量按值的形态说:非文件 / 文件 / N 项 / N 个文件。

    ``reads_files=False``(这个智能体没有任何能读文件的工具)时路径对模型没用,文件变量也只说
    「已提供」(平台提示词跟着能力走)。
    """
    sites = linked_sites(var.name, raw) if reads_files else []
    if not sites:
        return _TEXT if var.trusted else _UNTRUSTED_TEXT
    if isinstance(raw, str) and sites[0].site.path == ():
        # 裁定 P16 —— 真名里的扩展名取自租户 URL;untrusted 时不写真名。只说前缀:无扩展名、
        # 撞名加 ``-2`` 时「<变量名>.<扩展名>」这种占位都会说错。
        # B-73 ④ —— 前缀本身有歧义:声明了 ``org`` 与 ``org_logo`` 两个变量时,
        # 「以 org 开头的文件」把两个都框进去了。精确名平台知道(``sites[0].link``)
        # 但 untrusted 不能写进提示词,所以指向清单 —— 预拉写回的 ``local_path``
        # 就是那个精确的相对路径,而清单是沙箱读的文件、不是提示词。
        if var.trusted:
            return f"文件 {current_input_path(sites[0].link)}"
        return (
            f"文件在 {CURRENT_INPUTS_REL}/ 下，名字以 {var.name} 开头"  # noqa: RUF001
            f"（确切文件名见清单里 {var.name} 的 local_path）"  # noqa: RUF001
        )
    parsed = parse_json_value(raw)
    root = parsed if parsed is not None else raw
    if isinstance(root, list):
        return (
            f"{len(root)} 项，其中 {len(sites)} 个文件在 {CURRENT_INPUTS_REL}/{var.name}/ 下"  # noqa: RUF001
            "（按 <下标-说明> 命名）"  # noqa: RUF001
        )
    return (
        f"{len(sites)} 个文件在 {CURRENT_INPUTS_REL}/ 下"
        f"（按 {var.name}.<字段名> 命名）"  # noqa: RUF001
    )


def _one_line(desc: str) -> str:
    """manifest 说明压成一行(连续空白含换行 → 一个空格),超过 :data:`_DESC_MAX_CHARS`
    截断加「…」。"""
    text = " ".join(desc.split())
    return text if len(text) <= _DESC_MAX_CHARS else f"{text[:_DESC_MAX_CHARS]}…"


def build_inputs_block(
    variables: Sequence[Any],
    inputs: Mapping[str, Any],
    *,
    bindings: Collection[str],
    reads_files: bool = True,
) -> str | None:
    """从声明 + 本轮实际传值 + 绑定表生成;没有声明变量返回 ``None``。

    说明文字来自 ``PromptVariableSpec.description``(租户管理员写的 manifest,与系统提示词
    同一信任级),压成一行并限长;没写(或只有空白)就只有名字。模板里已经引用的变量也列
    (去重不做):C 的职责是兜底与位置,B 的职责是原地渲染,两者口径一致,重复一行不造成
    歧义。

    ``reads_files`` —— 这个智能体有没有能读文件的工具(调用方按工具表算,见
    :func:`reads_files_from_catalog`)。没有时不写文件句,文件变量只说「已提供」。
    """
    if not variables:
        return None
    lines = [HEADER]
    if reads_files and _has_files(variables, inputs):
        lines.append(FILES_FACT)
    for var in variables:
        desc = _one_line(getattr(var, "description", None) or "")
        label = f"{var.name}（{desc}）" if desc else var.name  # noqa: RUF001
        status = _status(var, inputs, bindings, reads_files=reads_files)
        lines.append(f"- {label}：{status}")  # noqa: RUF001
    return "\n".join(lines)


def _has_files(variables: Sequence[Any], inputs: Mapping[str, Any]) -> bool:
    """本轮传了值的声明变量里,有没有一个被认出文件(与预拉同一个判定)。"""
    return any(v.name in inputs and linked_sites(v.name, inputs[v.name]) for v in variables)


#: 能读文件的工具 —— 智能体的工具表里有其中任何一个,「本轮输入」段才写文件路径。
FILE_READING_TOOLS = frozenset({"exec_python", "bash", "read_document", "read_page", "ask_image"})


def reads_files_from_catalog(tool_names: Collection[str]) -> bool:
    """工具名里有没有能读文件的工具(:data:`FILE_READING_TOOLS`)。"""
    return not FILE_READING_TOOLS.isdisjoint(tool_names)


def block_stats(
    variables: Sequence[Any], inputs: Mapping[str, Any], *, bindings: Collection[str]
) -> dict[str, int]:
    """``inputs.block_injected`` 日志的三个计数(spec §十),不含任何值。"""
    return {
        "variable_count": len(variables),
        "bound_count": sum(1 for v in variables if v.name in bindings),
        "url_count": sum(
            len(linked_sites(v.name, inputs[v.name])) for v in variables if v.name in inputs
        ),
    }


def inputs_block_message(text: str) -> HumanMessage:
    """隐藏 + 打标的 HumanMessage;调用方按需再盖 run 戳。"""
    return HumanMessage(
        content=text, additional_kwargs={HIDE_FROM_UI: True, INPUTS_BLOCK_MARK: True}
    )


def is_inputs_block(msg: Any) -> bool:
    """这条消息是不是 :func:`inputs_block_message` 生成的「本轮输入」段。"""
    kwargs = getattr(msg, "additional_kwargs", None) or {}
    return isinstance(msg, HumanMessage) and bool(kwargs.get(INPUTS_BLOCK_MARK))
