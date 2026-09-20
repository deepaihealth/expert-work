"""Stream L.L4 — file-mutation outcome classifier.

Hermes guards against the "agent hallucinates a write that didn't land"
class of failure by aggregating per-tool mutation outcomes across a
turn and injecting an advisory footer back to the model
(``conversation_loop.py:3916-3939`` +
``tool_result_classification.py:9-26``). The agent cannot then claim
"I wrote a/b/c" when ``b`` actually failed — the next prompt carries
the explicit list of unsuccessful mutations.

The mutation surface is ``save_artifact`` (Stream J.9's artifact write
path) plus ``write_file`` / ``edit_file`` (the workspace writers). The
classifier is deliberately tool-specific: writing a classifier stub for
tools that don't exist yet would violate the "don't write speculative
code" rule. Future mutation tools (J.8 HITL diffs) extend
:data:`_MUTATION_TOOLS` with their own entries when they ship.

**B-84 第 3 条 —— 为什么 ``write_file`` / ``edit_file`` 必须在这里。**
它们原来不在,于是它们的失败取不到路径。而 run 级欠账
(``AgentState["unresolved_failures"]``)是按「哪份东西没写成」记的:
``edit_file`` 改 ``a.py`` 失败、模型退回整份 ``write_file`` 写 ``a.py``
成功 —— 这是 60 天数据里最常见的一格(``edit_file`` 失败率约 16%,
``no_match`` / ``stale` 为主),取不到路径就抵消不掉,一个真做成了的 run
会被判成没做成。**缺口在取不到 path 这一层,不在记账键的形状。**

Mini-ADR L-4 anchors:

* **Tool-specific by name, not by capability flag** — keeps the
  detection logic close to the tool's failure signal (a ``status``
  attribute or a specific meta key) instead of trying to standardise
  every tool.
* **Conservative on unknowns** — :func:`classify` returns ``None``
  for any tool name it doesn't recognise; the runtime simply omits
  it from the advisory. False negatives (no advisory when one
  should fire) are preferable to false positives (spurious advisories
  that train the model to ignore them).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import ToolMessage


@dataclass(frozen=True)
class MutationOutcome:
    """One file-mutation tool call's outcome.

    ``tool_name`` and ``path`` identify the mutation in the advisory
    footer. ``landed=False`` means the file change is NOT in effect —
    the model must not assume the path has the content it requested.
    ``error`` carries the failure summary when available.
    """

    tool_name: str
    path: str
    landed: bool
    error: str | None = None


#: 已知写类工具 -> (路径参数名, 资源空间)。
#:
#: **资源空间**回答的是「这两次调用写的是不是同一份东西」,是 B-84 第 3 条的
#: 记账单位。不能只用路径:``save_artifact`` 的 ``name`` 是产物名字空间,
#: ``write_file`` / ``edit_file`` 的 ``path`` 是工作区相对路径,两个空间会撞名
#: (都可以叫 ``a.py``),不分开就是一次假抵消 —— 存一份同名产物把一次失败的
#: 文件写「还」掉了。
#:
#: ``write_file`` 与 ``edit_file`` 共用 ``file`` 空间是**刻意**的:欠账的单位是
#: 那个文件,不是哪个工具写的它。``edit_file`` 失败后模型退回整份 ``write_file``
#: 重写成功,债就该还清。
_MUTATION_TOOLS: dict[str, tuple[str, str]] = {
    "save_artifact": ("name", "artifact"),
    "write_file": ("path", "file"),
    "edit_file": ("path", "file"),
}


def resource_space(tool_name: str) -> str | None:
    """``tool_name`` 写的是哪个资源空间;不是写类工具就是 ``None``。

    给 B-84 第 3 条的欠账记账键用。它只吃工具名,因为记账键要能从一条
    光杆 :class:`~orchestrator.tools.error_classifier.ClassifiedToolError`
    (跨轮从检查点取回来的那些)重算出来,那上面只有 ``tool_name`` 与 ``path``。
    """
    entry = _MUTATION_TOOLS.get(tool_name)
    return entry[1] if entry is not None else None


def classify(
    tool_name: str,
    args: Mapping[str, Any],
    tool_message: ToolMessage,
) -> MutationOutcome | None:
    """Return a :class:`MutationOutcome` iff ``tool_name`` is a known
    file-mutation tool, else ``None``.

    Tracked tools live in :data:`_MUTATION_TOOLS`; new mutation tools
    register there when they ship, and the set stays tight on purpose so
    spurious advisories never train the model to ignore them.
    """
    entry = _MUTATION_TOOLS.get(tool_name)
    if entry is None:
        return None
    path_arg, _space = entry
    return _classify_mutation(tool_name, path_arg, args, tool_message)


def _classify_mutation(
    tool_name: str,
    path_arg: str,
    args: Mapping[str, Any],
    tool_message: ToolMessage,
) -> MutationOutcome:
    """A mutation lands iff the dispatch wrapper produced a non-error
    :class:`ToolMessage`. Errors are translated by :func:`_invoke_tool`
    into ``ToolMessage(status="error")`` already, so we don't need to
    re-parse the body — the status flag is the canonical "did this work"
    signal, and it is the same signal for all three tools (``write_file``
    / ``edit_file`` both go through ``_raise_for_error``).

    读的是**原始入参**而不是成功结果的 ``meta["path"]``:失败那条抛了异常,
    压根没有 meta,两侧必须用同一个来源才对得上键。
    """
    raw = args.get(path_arg)
    path = str(raw).strip() if isinstance(raw, str) else "<unknown>"
    landed = _tool_message_succeeded(tool_message)
    error = None if landed else _tool_message_error_summary(tool_message)
    return MutationOutcome(
        tool_name=tool_name,
        path=path or "<unknown>",
        landed=landed,
        error=error,
    )


def _tool_message_succeeded(message: ToolMessage) -> bool:
    """A ``ToolMessage`` with explicit ``status="error"`` did not land.

    LangChain's ``ToolMessage.status`` defaults to ``"success"`` so
    only an explicit failure flag here means the tool surfaced an
    error to the model. The error-path branches in
    :func:`~orchestrator.graph_builder.builder._dispatch_tool` and
    :func:`~orchestrator.graph_builder.builder._invoke_tool` both set
    ``status="error"`` explicitly.
    """
    return getattr(message, "status", "success") != "error"


def _tool_message_error_summary(message: ToolMessage) -> str:
    """Extract a short error summary for the advisory footer."""
    content = message.content
    if isinstance(content, str):
        return content.strip() or "no error message"
    return repr(content)
