"""B-67 §六 —— 平台生成的「本轮输入」段:只有名字、说明、状态、路径,**零值**。

作为隐藏 ``HumanMessage`` 由 ``api/runs.build_run_graph_input`` 贴在用户消息**之后**:
首轮时它是上下文最后一条(注意力最好的位置);不拼进用户自己的消息(那条经 ``/messages``
原样回给对接方)。进模型、进 durable 记录与镜像;不进对外会话消息、控制台气泡、对话条目
(``expert_work_hide_from_ui`` 现成惯用法)。

「已下载」不能断言(渲染早于预拉),统一写「文件 …;不在则按清单原地址下载」。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any

from langchain_core.messages import HumanMessage

from control_plane.prompt_render import INPUTS_DIR_ENV, INPUTS_ENV
from expert_work.common.conversation_channel import HIDE_FROM_UI
from orchestrator.tools.inputs_doc import linked_sites, parse_json_value

#: 标在隐藏消息 ``additional_kwargs`` 上:``:regenerate`` 重放原件时据此认出它并一起带走
#: (其它隐藏 HumanMessage —— 委派提醒、恢复建议 —— 不带)。
INPUTS_BLOCK_MARK = "expert_work_inputs_block"
HEADER = "[本轮输入]（平台自动生成）"  # noqa: RUF001 — 面向模型的中文全角标点
#: 裁定 P10 —— 被 ``arg_bindings`` 引用的变量在模板里照形态渲染,绑定只在本段说一次,
#: 接在形态状态之后。
_BOUND = "绑定了它的工具由平台自动填，不用手抄"  # noqa: RUF001
_UNSET = "本轮未提供"
_TEXT = "已提供，短文本"  # noqa: RUF001
_UNTRUSTED_TEXT = "已提供，外部数据，需逐字使用时从清单读"  # noqa: RUF001


def _status(var: Any, inputs: Mapping[str, Any], bindings: Collection[str]) -> str:
    """一个变量的状态行。**不含任何租户数据**(裁定 P8):只有变量名、计数、平台文本,
    以及顶层 URL 的链接名(= 变量名 + ``[A-Za-z0-9]{1,5}`` 扩展名)。列表项说明、dict 键
    这类从值推出来的名字只用占位符 ``<下标-说明>`` / ``<字段名>`` 指代。

    先判「本轮未提供」(裁定 P12):可选变量被绑定但本轮没传,只说没提供 —— 那条绑定
    本轮也填不出值。传了的变量先给形态状态,被绑定的再接 :data:`_BOUND`(裁定 P10)。
    """
    if var.name not in inputs:
        return _UNSET
    shape = _shape_status(var, inputs[var.name])
    return f"{shape}；{_BOUND}" if var.name in bindings else shape  # noqa: RUF001


def _shape_status(var: Any, raw: Any) -> str:
    """传了值的变量按值的形态说:短文本 / 文件 / N 项 / N 个文件。"""
    sites = linked_sites(var.name, raw)
    if not sites:
        return _TEXT if var.trusted else _UNTRUSTED_TEXT
    if isinstance(raw, str) and sites[0].site.path == ():
        return f"文件 ${INPUTS_DIR_ENV}/{sites[0].link}；不在则按清单里的原地址下载"  # noqa: RUF001
    parsed = parse_json_value(raw)
    root = parsed if parsed is not None else raw
    if isinstance(root, list):
        return (
            f"{len(root)} 项，每项的文件在 ${INPUTS_DIR_ENV}/{var.name}/<下标-说明>；"  # noqa: RUF001
            "不在则按清单里该项的原地址下载"
        )
    return (
        f"{len(sites)} 个文件在 ${INPUTS_DIR_ENV}/{var.name}.<字段名>；"  # noqa: RUF001
        "不在则按清单里该字段的原地址下载"
    )


def build_inputs_block(
    variables: Sequence[Any], inputs: Mapping[str, Any], *, bindings: Collection[str]
) -> str | None:
    """从声明 + 本轮实际传值 + 绑定表生成;没有声明变量返回 ``None``。

    说明文字来自 ``PromptVariableSpec.description``(租户管理员写的 manifest,与系统提示词
    同一信任级),没写就只有名字。模板里已经引用的变量也列(去重不做):C 的职责是兜底与
    位置,B 的职责是原地渲染,两者口径一致,重复一行不造成歧义。
    """
    if not variables:
        return None
    lines = [
        HEADER,
        f"输入文件目录 ${INPUTS_DIR_ENV}，清单 ${INPUTS_ENV}（exec_python / bash 里直接用）。",  # noqa: RUF001
        "要用到下面任何值时用代码从目录或清单读；不要从上文手抄，长串抄错一位就是 404。",  # noqa: RUF001
    ]
    for var in variables:
        desc = getattr(var, "description", None)
        label = f"{var.name}（{desc}）" if desc else var.name  # noqa: RUF001
        lines.append(f"- {label}：{_status(var, inputs, bindings)}")  # noqa: RUF001
    return "\n".join(lines)


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
