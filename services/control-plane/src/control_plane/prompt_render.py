"""Run-time Jinja rendering of an agent's ``system_prompt`` (Dynamic-Prompt).

Renders ONLY the human-authored ``base`` template (the ``system_prompt``
field) with the run's ``inputs``; the orchestrator-computed ``suffix``
(spotlight clause, skill bodies, memory blocks) is appended verbatim and
never itself Jinja-rendered — so a skill body containing literal ``{{ }}``
can't break the run, and untrusted memory/skill content can't become an
SSTI primitive. See ``docs/design/jinja-dynamic-prompt.md`` §3.

``trusted=False`` variables are spotlight-fenced as DATA before
substitution (shared nonce with the model-side tool/RAG channels);
``trusted=True`` (the owner-set default, §4) renders verbatim.

B-67 §五 —— 本轮传了的变量,值先过 :func:`render_value`,**按值的形态**决定放什么进
模板:``render: raw`` → 原值;URL → 本地链接路径;列表 / 对象 / 可解析的 JSON 字符串里有
URL → 逐项「说明 → 路径」;其它 → 原值(今天行为)。被绑定的变量同样按形态渲染 —— 绑定
是逐工具的,漏绑的工具还要从提示词里拿值;绑定状态由「本轮输入」段报告。没传的变量取
今天的值,不走形态渲染。渲染早于预拉,所以这里只做字符串替换 —— 不发网、不读盘、不等
下载结果;名字由 ``inputs_doc.link_names`` 决定,与预拉建出来的链接逐字相同。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from jinja2 import TemplateError

from control_plane.manifest.loader import build_sandboxed_environment
from expert_work.common.spotlight import spotlight_untrusted
from orchestrator.tools.inputs_doc import LinkedSite, linked_sites, parse_json_value

logger = logging.getLogger(__name__)

#: 沙箱里两个环境变量的名字(``sandbox.agent_key_envs`` 注入);提示词里写成 ``$NAME``,
#: 模型在 exec_python / bash 里直接用,不用手抄任何路径。
INPUTS_ENV = "EXPERT_WORK_INPUTS"
INPUTS_DIR_ENV = "EXPERT_WORK_INPUTS_DIR"
#: 被绑定变量的状态文本。PR3 本轮输入段用(模板里的值不再替换成它)。
BOUND_TEXT = "（已绑定到工具参数，调用时平台自动填）"  # noqa: RUF001 — 全角标点,面向模型的中文
#: URL / 逐项渲染的尾注:不断言「已下载」(渲染早于预拉),只说名字与回落办法。
URL_NOTE = "（已就位；不在则按输入清单里的原地址下载）"  # noqa: RUF001
#: 带路径的行以它收尾:untrusted 的 datamarking 把空白换成 ``▁``,不能直接贴在路径上。
_PATH_END = "；"  # noqa: RUF001

# ``built`` is the orchestrator ``BuiltAgent`` (typed ``Any`` here, matching
# ``build_run_graph_input``); the renderer reads ``system_prompt``,
# ``prompt_jinja``, ``prompt_variables`` (each with ``.name``/``.trusted``/
# ``.render``), ``prompt_base``, ``prompt_suffix``, ``spotlight_nonce``;
# :func:`bound_variable_names` reads ``arg_bindings`` (each with ``.args``).


class PromptRenderError(ValueError):
    """Template render failed (bad syntax / undefined). Maps to 422."""


def _fence_value(value: str, *, nonce: str | None) -> str:
    """Wrap an untrusted value as DATA. With spotlighting off (no nonce)
    degrade to a plain marker — same backstop as ``untrusted_content``."""
    if nonce:
        return spotlight_untrusted(value, nonce=nonce)
    return f"[untrusted content]\n{value}"


def bound_variable_names(built: Any) -> frozenset[str]:
    """被某条 ``arg_bindings`` 引用的变量名(``BuiltAgent.arg_bindings``,manifest 原件)。"""
    return frozenset(
        var_name
        for binding in getattr(built, "arg_bindings", ())
        for var_name in binding.args.values()
    )


def _raw_or_fenced(var: Any, raw: Any, *, nonce: str | None) -> Any:
    """今天的行为:trusted 原值,untrusted 围栏。"""
    return raw if var.trusted else _fence_value(str(raw), nonce=nonce)


def _fence_if_untrusted(var: Any, text: str, *, nonce: str | None) -> str:
    return text if var.trusted else _fence_value(text, nonce=nonce)


def _plain(item: Any) -> str:
    return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)


def _label(item: Any, fallback: str) -> str:
    desc = item.get("description") if isinstance(item, Mapping) else None
    return desc if isinstance(desc, str) and desc else fallback


def _render_items(var: Any, root: Any, sites: list[LinkedSite], *, nonce: str | None) -> str:
    """列表 / 对象逐项:有 URL 的项 → 「说明 → 路径」,没有的项照原样(裁定 10:有 URL 的
    项只给说明与路径,其它字段不进提示词,清单里都有)。

    ``trusted: false`` 时,从值推出来的**全部**行(说明、dict 键、路径 —— 路径里带着
    description 的 slug)合成一段、围栏一次;只有尾注是纯平台文本,留在围栏外(裁定 P8)。

    整块另起一行(``素材:{{ materials }}`` 不能把第 0 项粘在标签上);带路径的行以
    :data:`_PATH_END` 收尾;不是对象的列表项(列表套列表等)只给序号与路径。
    """
    lines: list[str] = []
    is_list = isinstance(root, list)
    members: list[tuple[str | int, Any]] = (
        list(enumerate(root)) if is_list else [(str(k), v) for k, v in root.items()]
    )
    # 按项分组一次(保持 site 顺序);逐项去扫全部 site 是平方级,几千项的列表就慢到秒级。
    by_head: dict[str | int, list[str]] = {}
    for s in sites:
        by_head.setdefault(s.site.path[0], []).append(s.link)
    for head, item in members:
        links = by_head.get(head)
        if links:
            paths = "、".join(f"${INPUTS_DIR_ENV}/{link}" for link in links)
            if not is_list:
                lines.append(f"- {head} → {paths}{_PATH_END}")
            elif isinstance(item, Mapping):
                lines.append(f"{head}. {_label(item, str(head))} → {paths}{_PATH_END}")
            else:
                lines.append(f"{head}. {paths}{_PATH_END}")
        else:
            plain = _plain(item)
            lines.append(f"{head}. {plain}" if is_list else f"- {head}: {plain}")
    body = _fence_if_untrusted(var, "\n".join(lines), nonce=nonce)
    return f"\n{body}\n{URL_NOTE}"


def render_value(var: Any, raw: Any, *, nonce: str | None) -> tuple[Any, bool]:
    """一个本轮传了值的声明变量在模板上下文里的值,按形态定(spec §五的表,判定顺序即
    代码顺序)。被绑定与否不影响这里:绑定是逐工具的,漏绑的工具仍要从提示词里拿值。

    返回 ``(值, 是否走了引用渲染)``,第二项只给日志用。

    只做字符串替换:不发网、不读盘、不等预拉 —— 路径由名字决定,不由下载决定。
    ``trusted: false`` 时路径也在围栏里(它由租户数据推出),尾注是平台文本,不围栏(裁定 P8)。
    已知代价:模板对被改写的值做**内容比较**(``{{ 'x' if org_logo == '…' }}``)会失真;
    ``| default('')`` 这类**存在性**判断照旧成立(改写后的字符串非空)。
    """
    if getattr(var, "render", "auto") == "raw":
        return _raw_or_fenced(var, raw, nonce=nonce), False
    sites = linked_sites(var.name, raw)
    if not sites:
        return _raw_or_fenced(var, raw, nonce=nonce), False
    if isinstance(raw, str) and sites[0].site.path == ():
        # 整个值就是一个 URL:名字确定,不等预拉。
        path = _fence_if_untrusted(var, f"${INPUTS_DIR_ENV}/{sites[0].link}", nonce=nonce)
        return f"{path}{URL_NOTE}", True
    parsed = parse_json_value(raw)
    return _render_items(var, parsed if parsed is not None else raw, sites, nonce=nonce), True


def render_system_prompt(built: Any, inputs: dict[str, Any]) -> str:
    """Return the system prompt for one run.

    Non-Jinja agents (``prompt_jinja`` False — every existing agent) return
    the stored prompt unchanged: byte-identical, zero overhead, prompt cache
    intact. Jinja agents render ``prompt_base`` with the declared variables
    and append ``prompt_suffix`` verbatim.

    ``inputs`` is assumed already validated by :func:`validate_prompt_inputs`
    (undeclared / missing-required rejected at request time); this stays
    defensive — a missing value renders as the empty string.
    """
    # ``getattr`` default keeps older ``Any``-typed build doubles (and any
    # caller predating these fields) on the non-jinja path — byte-identical.
    if not getattr(built, "prompt_jinja", False):
        verbatim: str = built.system_prompt
        return verbatim

    context: dict[str, Any] = {}
    by_reference: list[str] = []
    for var in built.prompt_variables:
        if var.name not in inputs:
            # 本轮没传:今天的值(裁定 9),不走形态渲染 —— 平台此时什么都不填
            # (``apply_arg_bindings`` 会丢掉该参数),模板里的 ``if`` / ``default`` 不能被翻过来。
            context[var.name] = _raw_or_fenced(var, "", nonce=built.spotlight_nonce)
            continue
        value, referenced = render_value(var, inputs[var.name], nonce=built.spotlight_nonce)
        context[var.name] = value
        if referenced:
            by_reference.append(var.name)
    if by_reference:
        # spec §十 —— 只记名字,不记值。
        logger.info("prompt.rendered_by_reference", extra={"variable_names": by_reference})

    env = build_sandboxed_environment()
    try:
        rendered_base: str = env.from_string(built.prompt_base).render(**context)
    except TemplateError as exc:
        # No ``from exc``: the API layer surfaces a clean message and CodeQL's
        # py/stack-trace-exposure flags the chained cause if it reaches a body.
        raise PromptRenderError(f"system_prompt render failed: {exc}") from None
    suffix: str = built.prompt_suffix
    return rendered_base + suffix


def validate_prompt_inputs(built: Any, inputs: dict[str, Any]) -> None:
    """Validate a run's ``inputs`` against the agent's declared variables.

    Raises :class:`PromptRenderError` (caller maps to HTTP 422) so a bad
    request fails synchronously — including queue-mode runs, which validate
    before enqueue rather than blowing up later in the worker.
    """
    if not getattr(built, "prompt_jinja", False):
        if inputs:
            raise PromptRenderError("agent declares no prompt variables; 'inputs' not accepted")
        return
    declared = {v.name: v for v in built.prompt_variables}
    for key in inputs:
        if key not in declared:
            raise PromptRenderError(f"unknown input variable: {key}")
    for name, var in declared.items():
        if var.required and name not in inputs:
            raise PromptRenderError(f"missing required input: {name}")
