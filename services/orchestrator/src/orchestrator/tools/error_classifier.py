"""Stream CM-1 — runtime tool-error classifier (error-as-guidance).

When a tool call fails inside the ReAct loop, the runtime currently
turns any exception into a flat ``ToolMessage(status="error",
content="[tool error] …")`` with no structure (see
:func:`~orchestrator.graph_builder.builder._invoke_tool` /
``_dispatch_tool``). The model is then left to guess whether to retry
verbatim, change tack, or surface the failure — and it frequently
guesses wrong (the framework report's A1 evidence: structured error
recovery lands >85% of the time vs ~17% for fuzzy signals).

This module maps a failed tool call to a :class:`ClassifiedToolError`
carrying an error class, a ``retryable`` hint and a templated, grounded
recovery ``advice`` string, and renders each failure into a
``<recovery-advisory>`` block that the prompt view appends to the failing
tool result (B-163). It lives in the
``tools`` layer beside L-4's :mod:`~orchestrator.tools.mutation_classifier`
(a lower layer than ``graph_builder``) so the ``AgentState`` channel can
import :class:`ClassifiedToolError` at runtime without an import cycle.
``builder.py`` wires it at the tool catch sites + the tools-node marking
(``graph_builder.platform_context.with_recovery_advisories`` attaches it).

Design anchors (STREAM-CM-DESIGN §3):

* **CM-B1 — deterministic, no LLM.** Classification is derived purely
  from the exception type, the error text and the tool's
  :class:`~orchestrator.tools.registry.ToolSpec` capability metadata.
  No model introspection: the advice is grounded in real execution
  signals, which is exactly what makes it reliable.
* **CM-B3 — classify at the catch site.** The classifier takes the real
  exception object (richest signal) rather than re-parsing a formatted
  string after the fact.
* **CM-B5 — retry hints are capability-bounded.** Even a transient
  failure is only flagged ``retryable`` when the tool is read-only or
  idempotent; an irreversible non-idempotent tool is told to verify
  state first, never to blindly replay.

``mutation_not_landed`` is the taxonomy's L-4 case ("the write said OK
but didn't land") — a success-path signal contributed by
:mod:`orchestrator.tools.mutation_classifier` and built via
:func:`classified_mutation_not_landed` rather than from an exception, so
every error class speaks through this one module's vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from orchestrator.tools.registry import ToolNotFoundError, ToolSpec

#: All tool-error classes the runtime distinguishes. Extend the taxonomy
#: only here so the classifier, renderer and metrics stay in lockstep.
ToolErrorClass = Literal[
    "unknown_tool",
    "invalid_arguments",
    "blocked_by_policy",
    "resource_not_found",
    "permission_denied",
    "transient",
    "mutation_not_landed",
    "unknown",
]

#: Per-failure summary cap. Mirrors the 500-char cap ``builder._format_error``
#: already applies on the ToolMessage path. B-163: the advisory no longer
#: quotes the summary (the error text sits right above it in the same tool
#: result); the summary stays on the run-level failure ledger.
_SUMMARY_MAX_CHARS = 300


@dataclass(frozen=True)
class ClassifiedToolError:
    """A single failed tool call, classified for recovery guidance.

    ``error_class`` drives the templated ``advice``; ``retryable`` is the
    grounded "is a blind retry safe?" hint (CM-B5). ``path`` is carried
    for the ``mutation_not_landed`` class (PR2), unused on the error
    path.
    """

    tool_name: str
    error_class: ToolErrorClass
    summary: str
    retryable: bool
    advice: str
    path: str | None = None


class GuidedTimeoutError(TimeoutError):
    """A timeout whose recovery the raising tool knows better than the generic advice.

    B-64 Task 10 — ``ask_image`` times out after its per-call cap. It stays
    ``transient`` (run-level ledger and escalation treat it as jitter, same as
    any timeout), but the generic "safe to retry once" advice for a read-only
    tool is wrong here: an identical retry just burns another full window. The
    tool supplies the advice instead, and the failure is marked not retryable.
    """

    def __init__(self, message: str, *, advice: str) -> None:
        super().__init__(message)
        self.advice = advice


class GuidedToolError(RuntimeError):
    """A non-timeout tool failure whose recovery the raising tool knows better.

    B-105 — ``ask_image`` whose vision model was cut off by its output cap
    with nothing usable. A real failure (``unknown`` class, it enters the
    run's failure ledger), but an identical retry hits the same cap: the tool
    supplies the advice and the failure is marked not retryable.
    """

    def __init__(self, message: str, *, advice: str) -> None:
        super().__init__(message)
        self.advice = advice


# ---------------------------------------------------------------------------
# Classification (error path)
# ---------------------------------------------------------------------------


def classify_tool_error(
    *,
    tool_name: str,
    error: BaseException,
    spec: ToolSpec | None = None,
    blocked: bool = False,
) -> ClassifiedToolError:
    """Classify a failed tool dispatch into a :class:`ClassifiedToolError`.

    ``blocked=True`` marks a middleware/approval/guardrail rejection
    (the ``_dispatch_tool`` "blocked" outcome) — a control-flow signal
    not derivable from the exception alone. ``spec`` is ``None`` for an
    ``unknown_tool`` (the tool was never registered).
    """
    summary = _summarise(error)
    if blocked:
        return _make("blocked_by_policy", tool_name, summary, spec)
    if isinstance(error, ToolNotFoundError):
        return _make("unknown_tool", tool_name, summary, spec)
    if isinstance(error, GuidedTimeoutError):
        return ClassifiedToolError(
            tool_name=tool_name,
            error_class="transient",
            summary=summary,
            retryable=False,
            advice=error.advice,
        )
    if isinstance(error, GuidedToolError):
        return ClassifiedToolError(
            tool_name=tool_name,
            error_class="unknown",
            summary=summary,
            retryable=False,
            advice=error.advice,
        )
    return _make(_classify_by_signal(error), tool_name, summary, spec)


def classified_invalid_arguments(*, tool_name: str, summary: str) -> ClassifiedToolError:
    """Build the ``invalid_arguments`` classification for a 2.2 pre-dispatch
    schema-validation reject.

    Like :func:`classified_mutation_not_landed`, there is no exception to
    classify — the orchestrator rejected the ``tool_call`` args against the
    tool's JSON Schema before ever calling it. ``retryable=False``: a blind
    replay of the same bad args is pointless; the model must fix them.
    """
    return ClassifiedToolError(
        tool_name=tool_name,
        error_class="invalid_arguments",
        summary=summary,
        retryable=False,
        advice=_ADVICE["invalid_arguments"],
    )


def classified_mutation_not_landed(
    *, tool_name: str, summary: str, path: str | None
) -> ClassifiedToolError:
    """Build the ``mutation_not_landed`` classification (L-4 convergence).

    This is the *success-path* failure — the tool returned a non-error
    ``ToolMessage`` but the write did not take effect (detected by
    :func:`orchestrator.tools.mutation_classifier.classify`). It can't go
    through :func:`classify_tool_error` because there is no exception;
    this factory keeps the ``mutation_not_landed`` advice owned here so
    every error class speaks through one module. Retryable (retry or
    surface), mirroring the original L-4 advisory's intent.
    """
    return ClassifiedToolError(
        tool_name=tool_name,
        error_class="mutation_not_landed",
        summary=summary,
        retryable=True,
        advice=_ADVICE["mutation_not_landed"],
        path=path,
    )


def _classify_by_signal(error: BaseException) -> ToolErrorClass:
    """Map an exception to an error class by type then text (CM-B1).

    Specific ``OSError`` subclasses (``TimeoutError``/``PermissionError``/
    ``FileNotFoundError``) are checked before generic text matching, then
    the lowercased message is scanned for well-known phrases. Order is
    deliberate: type is a stronger signal than free text.
    """
    if isinstance(error, TimeoutError):
        return "transient"
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, FileNotFoundError):
        return "resource_not_found"

    text = str(error).lower()
    if _matches(text, _TRANSIENT_NEEDLES):
        return "transient"
    if _matches(text, _PERMISSION_NEEDLES):
        return "permission_denied"
    if _matches(text, _NOT_FOUND_NEEDLES):
        return "resource_not_found"
    if _matches(text, _INVALID_NEEDLES):
        return "invalid_arguments"
    return "unknown"


#: Lowercased text signals per class, scanned only after exception-type
#: checks. Tuples not sets so matching order is stable and obvious.
_TRANSIENT_NEEDLES = (
    "timed out",
    "timeout",
    # B-84 — "504 Gateway Time-out" is what the sandbox supervisor returns
    # when creation exceeds the ALB deadline, and it used to match nothing
    # here: 504 was absent from the status list, and nginx spells it
    # "Time-out" with a hyphen so "timeout" misses too. It classified as
    # ``unknown``, whose advice invites the model to "consider an
    # alternative approach" — in run 7fad305b the model took that to mean
    # the directory was gone and re-typed a 21,173-character script.
    "time-out",
    "connection",
    "unavailable",
    "overloaded",
    " 503",
    " 502",
    " 504",
    " 500",
    " 529",
)
_PERMISSION_NEEDLES = ("permission denied", "forbidden", "unauthorized", " 401", " 403")
#: ``not_found`` —— 工作区文件工具的失败文本是 ``<tool> failed: not_found``(``file_ops``
#: 的错误码, 下划线)。B-159 的豁免靠这一类;漏了它, 读一个还没建的文件就被记成欠账
#: (10-08 B-140 c02)。
_NOT_FOUND_NEEDLES = ("not found", "not_found", "no such file", "does not exist", " 404")
_INVALID_NEEDLES = ("invalid", "validation", "is required", "must be", "bad request", " 400")


def _matches(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _make(
    error_class: ToolErrorClass,
    tool_name: str,
    summary: str,
    spec: ToolSpec | None,
) -> ClassifiedToolError:
    retryable = _is_retryable(error_class, spec)
    return ClassifiedToolError(
        tool_name=tool_name,
        error_class=error_class,
        summary=summary,
        retryable=retryable,
        advice=_advice(error_class, retryable, spec),
    )


def _is_retryable(error_class: ToolErrorClass, spec: ToolSpec | None) -> bool:
    """Only a transient failure on a safe-to-replay tool is retryable.

    Safe-to-replay = read-only or explicitly idempotent (CM-B5). Without
    a spec we cannot prove safety, so default to not-retryable. Every
    other class is a "fix something first" failure, never a blind retry.
    """
    if error_class != "transient":
        return False
    if spec is None:
        return False
    return spec.resolved_side_effect == "read_only" or spec.idempotent


# ---------------------------------------------------------------------------
# Recovery advice (templated, grounded)
# ---------------------------------------------------------------------------

#: B-84 — the sentence every "the call never got to look" class must carry.
#: ``resource_not_found`` is the ONLY class that licenses "the target is not
#: there"; a transient or unclassified failure says nothing either way. The
#: distinction was invisible in the advisory text, and a model that cannot
#: tell "absent" from "unreachable" does the expensive safe thing: it rebuilds
#: from scratch. Measured cost of exactly that inference on the test
#: environment: 14 re-typings of a file the same user already had, 293,012
#: characters, about 6,002 seconds of wall clock over 60 days.
#:
#: B-84 PR-2 —— 改成公开名。系统提示词里的工作区快照块
#: (:mod:`orchestrator.tools.workspace_tree`)是这句话的**第二个落点**:模型在提示词
#: 里和在错误消息里都该读到同一条规矩。两处引用同一个常量, 就结构上不可能各说各的 ——
#: 抄一份措辞过去的话, 改其中一处时另一处不会跟着动。
#:
#: B-163 —— 改成陈述事实, 不下命令(与下面各类的模板同一口径, 理由见 ``_ADVICE``)。
EXISTENCE_UNKNOWN = (
    "This says NOTHING about whether the target exists — the call never got "
    "far enough to look, so the target may well still be there. This error is "
    "no reason to treat it as absent or to recreate it from scratch; a separate "
    "read once the tool works again shows its actual state."
)


#: B-163 —— 每一类都写成**事实**:发生了什么、由此知道什么、同样的调用会怎样。不写
#: 「Do not … / Verify … / Retry …」这类命令。建议现在贴在工具结果末尾, Claude Code
#: hooks 文档明说带外文字写成命令口吻会触发模型的注入防御(anthropics/claude-code
#: #52018 是实例);事实陈述同样能让模型自己得出下一步。
_ADVICE: dict[ToolErrorClass, str] = {
    "unknown_tool": (
        "No tool with this name is available, so the same name fails again; the "
        "callable tools are the ones offered in this conversation."
    ),
    "invalid_arguments": (
        "The tool rejected the arguments before running (the reason is in the "
        "result above), so the identical call is rejected again."
    ),
    "blocked_by_policy": (
        "A policy or approval gate stopped this call, so it did not run. The gate "
        "is enforced by the platform, not by the tool: the same call is stopped "
        "the same way until the user or an administrator changes it."
    ),
    "resource_not_found": ("The tool reported that the target (path or id) does not exist."),
    "permission_denied": (
        "Permission was denied, so the identical call is denied again; only the "
        "user or an administrator can change the permission. " + EXISTENCE_UNKNOWN
    ),
    "mutation_not_landed": (
        "This write did NOT land: the target does not have the requested content."
    ),
    "unknown": (
        "The call failed for a reason the platform could not classify; the error "
        "above is all that is known, and the identical call is likely to fail the "
        "same way. " + EXISTENCE_UNKNOWN
    ),
}

_TRANSIENT_RETRYABLE = (
    "A transient failure (timeout or temporary unavailability). This tool is "
    "read-only or idempotent, so it is safe to retry once; repeated failures "
    "point to an outage the user would need to know about. " + EXISTENCE_UNKNOWN
)
_TRANSIENT_UNSAFE = (
    "A transient failure (timeout or temporary unavailability). This tool "
    "changes state and is not idempotent: the change may or may not have taken "
    "effect, so a blind replay could apply it twice; the current state shows "
    "which. " + EXISTENCE_UNKNOWN
)


def _advice(error_class: ToolErrorClass, retryable: bool, spec: ToolSpec | None) -> str:
    del spec  # capability already folded into ``retryable``
    if error_class == "transient":
        return _TRANSIENT_RETRYABLE if retryable else _TRANSIENT_UNSAFE
    return _ADVICE[error_class]


# ---------------------------------------------------------------------------
# Advisory rendering
# ---------------------------------------------------------------------------

RECOVERY_ADVISORY_OPEN = "<recovery-advisory>"
RECOVERY_ADVISORY_CLOSE = "</recovery-advisory>"
RECOVERY_ADVISORY_LABEL = (
    "(Attached by the platform to this tool result; not part of the tool's output.)"
)
#: 用户 / 工具写的同名字样转义成这样, 冒充不了平台写的那段(照 B-162 ``<platform-context>``)。
RECOVERY_ADVISORY_ESCAPES = (
    (RECOVERY_ADVISORY_OPEN, "&lt;recovery-advisory&gt;"),
    (RECOVERY_ADVISORY_CLOSE, "&lt;/recovery-advisory&gt;"),
)


def escape_recovery_advisory_tags(text: str) -> str:
    """把 ``text`` 里的 ``<recovery-advisory>`` / ``</recovery-advisory>`` 字样转义。"""
    for raw, safe in RECOVERY_ADVISORY_ESCAPES:
        text = text.replace(raw, safe)
    return text


#: 建议里引用模型给的值(工具名、路径)时的上限。超了留头尾、中间一个 ``…``。
_QUOTED_MAX_CHARS = 200


def _one_line(text: str) -> str:
    """模型给的值压成一行(空白折叠)并限长:换行不能在建议里另起一行冒充平台的话。"""
    flat = " ".join(text.split())
    if len(flat) <= _QUOTED_MAX_CHARS:
        return flat
    head = _QUOTED_MAX_CHARS * 2 // 3
    tail = _QUOTED_MAX_CHARS - head - 1
    return f"{flat[:head]}…{flat[-tail:]}"


def render_recovery_advisory(failure: ClassifiedToolError) -> str:
    """B-163 —— 把**一次**失败渲染成一段 ``<recovery-advisory>``, 贴在那条工具结果末尾。

    正文一行 ``{tool} [{class}]{ path=…}: {advice}``。``path=`` 只在带路径的失败
    (``mutation_not_landed``)上出现, 保留 L-4 的路径可见性。**不抄 summary**:
    错误原文就在这段正上方的工具结果里, 再抄一遍只是多花 token。

    确定性:同一次失败每次渲染的字节都一样(这段每一步都原样重挂, 字节一变前缀缓存
    就断)。工具名与路径来自模型:先压成一行、限长(:func:`_one_line`), 再转义同名标签。
    """
    head = f"{_one_line(failure.tool_name)} [{failure.error_class}]"
    if failure.path:
        head += f" path={_one_line(failure.path)}"
    body = escape_recovery_advisory_tags(f"{head}: {failure.advice}")
    return "\n".join(
        [RECOVERY_ADVISORY_OPEN, RECOVERY_ADVISORY_LABEL, body, RECOVERY_ADVISORY_CLOSE]
    )


def _summarise(error: BaseException) -> str:
    text = str(error).strip() or type(error).__name__
    if len(text) > _SUMMARY_MAX_CHARS:
        text = text[:_SUMMARY_MAX_CHARS] + "...[truncated]"
    return text
