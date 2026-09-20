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
recovery ``advice`` string, and renders a batch of failures into a
``<recovery-advisory>`` block for tail injection. It lives in the
``tools`` layer beside L-4's :mod:`~orchestrator.tools.mutation_classifier`
(a lower layer than ``graph_builder``) so the ``AgentState`` channel can
import :class:`ClassifiedToolError` at runtime without an import cycle.
``builder.py`` wires it at the tool catch sites + the agent-node advisory
injection.

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

#: Per-failure summary cap inside the advisory — keeps a batch of
#: failures from blowing the prompt-tail budget. Mirrors the 500-char
#: cap ``builder._format_error`` already applies on the ToolMessage path.
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
_NOT_FOUND_NEEDLES = ("not found", "no such file", "does not exist", " 404")
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
EXISTENCE_UNKNOWN = (
    "This says NOTHING about whether the target exists — the call never got "
    "far enough to look. Do not treat it as absent and do not recreate it "
    "from scratch; confirm with a separate read once the tool works again."
)


_ADVICE: dict[ToolErrorClass, str] = {
    "unknown_tool": (
        "This tool does not exist. Pick a tool from the available set; do not retry this name."
    ),
    "invalid_arguments": (
        "The arguments were rejected. Fix the arguments based on the error "
        "above; do not repeat the identical call."
    ),
    "blocked_by_policy": (
        "This call was blocked by a policy or approval gate. Wait for "
        "approval or surface the situation to the user; do not try to "
        "bypass it."
    ),
    "resource_not_found": (
        "The target (path or id) does not exist. Verify it exists before operating on it."
    ),
    "permission_denied": (
        "Permission was denied. Do not brute-force retry; surface this to "
        "the user. " + EXISTENCE_UNKNOWN
    ),
    "mutation_not_landed": (
        "This mutation did NOT land — do not assume the target has the "
        "requested content. Retry or surface the failure."
    ),
    "unknown": (
        "This failed for an unclear reason. Inspect the error and consider "
        "an alternative approach; avoid retrying the identical call. " + EXISTENCE_UNKNOWN
    ),
}

_TRANSIENT_RETRYABLE = (
    "A transient failure. This tool is safe to retry once; if it keeps "
    "failing, surface the failure to the user. " + EXISTENCE_UNKNOWN
)
_TRANSIENT_UNSAFE = (
    "A transient failure, but this tool is not safe to blindly replay "
    "(not read-only or idempotent). Verify the current state before "
    "retrying. " + EXISTENCE_UNKNOWN
)


def _advice(error_class: ToolErrorClass, retryable: bool, spec: ToolSpec | None) -> str:
    del spec  # capability already folded into ``retryable``
    if error_class == "transient":
        return _TRANSIENT_RETRYABLE if retryable else _TRANSIENT_UNSAFE
    return _ADVICE[error_class]


# ---------------------------------------------------------------------------
# Advisory rendering
# ---------------------------------------------------------------------------

_ADVISORY_PREAMBLE = (
    "The following tool calls from the previous batch did NOT succeed. "
    "Do not assume their effects took place; act on the per-tool guidance "
    "below rather than retrying blindly."
)


def render_recovery_advisory(failures: list[ClassifiedToolError]) -> str:
    """Render a batch of failures into a ``<recovery-advisory>`` block.

    Returns an empty string for an empty batch so the caller can skip
    injection. Each line is
    ``- {tool} [{class}]{ path=…}: {summary} → {advice}`` — the
    ``path=`` segment appears only for failures that carry one (the
    ``mutation_not_landed`` case), preserving L-4's path visibility.
    """
    if not failures:
        return ""
    lines = ["<recovery-advisory>", _ADVISORY_PREAMBLE]
    for f in failures:
        head = f"- {f.tool_name} [{f.error_class}]"
        if f.path:
            head += f" path={f.path}"
        lines.append(f"{head}: {f.summary} → {f.advice}")
    lines.append("</recovery-advisory>")
    return "\n".join(lines)


def _summarise(error: BaseException) -> str:
    text = str(error).strip() or type(error).__name__
    if len(text) > _SUMMARY_MAX_CHARS:
        text = text[:_SUMMARY_MAX_CHARS] + "...[truncated]"
    return text
