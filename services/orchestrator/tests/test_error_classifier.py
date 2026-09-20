"""Stream CM-1 — :mod:`orchestrator.tools.error_classifier` unit tests.

Pins the classifier contract: each failure signal maps to the right
:data:`ToolErrorClass`, ``retryable`` honours the tool's capability
(CM-B5), and a batch renders into a ``<recovery-advisory>`` block.
"""

from __future__ import annotations

from orchestrator.tools.error_classifier import (
    ClassifiedToolError,
    classified_mutation_not_landed,
    classify_tool_error,
    render_recovery_advisory,
)
from orchestrator.tools.registry import ToolNotFoundError, ToolSpec
from orchestrator.tools.sandbox import SandboxClaimTimeoutError

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _spec(*, idempotent: bool = False, read_only: bool = False) -> ToolSpec:
    return ToolSpec(
        name="t",
        description="d",
        is_read_only=read_only,
        idempotent=idempotent,
    )


# ---------------------------------------------------------------------------
# classification: control-flow signals
# ---------------------------------------------------------------------------


def test_unknown_tool_from_tool_not_found() -> None:
    err = classify_tool_error(tool_name="nope", error=ToolNotFoundError("nope"))
    assert err.error_class == "unknown_tool"
    assert err.retryable is False
    assert "does not exist" in err.advice


def test_blocked_takes_precedence_over_exception_type() -> None:
    # A middleware block is a control-flow signal; even a ValueError body
    # must classify as blocked, not invalid_arguments.
    err = classify_tool_error(
        tool_name="send_email", error=ValueError("invalid recipient"), blocked=True
    )
    assert err.error_class == "blocked_by_policy"
    assert err.retryable is False
    assert "approval" in err.advice


# ---------------------------------------------------------------------------
# classification: exception type beats text
# ---------------------------------------------------------------------------


def test_timeout_error_type_is_transient() -> None:
    err = classify_tool_error(tool_name="fetch", error=TimeoutError("boom"))
    assert err.error_class == "transient"


def test_sandbox_claim_timeout_is_transient() -> None:
    """B-85 —— 等并发赢家建沙箱等超时,必须落进 ``transient``,不是 ``unknown``。

    这条钉的是 :class:`SandboxClaimTimeoutError` 那个刻意的**双基类**:它同
    时是 ``TimeoutError``,所以上面那条按类型判的规则直接接住它。修复前这个
    失败走的是文本匹配,一条都不中 → ``unknown`` → advisory 劝模型「别重
    试」→ 模型放弃 → run 报 ``status=success`` 而产物为空(2026-09-19 金丝
    雀实况)。

    刻意**不往** ``_TRANSIENT_NEEDLES`` 加关键词:词表追不上错误文本,而类型
    追得上。有人把那两个基类改成一个,这条会红。
    """
    err = classify_tool_error(
        tool_name="exec_python",
        error=SandboxClaimTimeoutError("a sandbox is already being created — retry shortly"),
    )
    assert err.error_class == "transient"


def test_permission_error_type_is_permission_denied() -> None:
    err = classify_tool_error(tool_name="read", error=PermissionError("nope"))
    assert err.error_class == "permission_denied"


def test_file_not_found_type_is_resource_not_found() -> None:
    err = classify_tool_error(tool_name="read", error=FileNotFoundError("/x"))
    assert err.error_class == "resource_not_found"


# ---------------------------------------------------------------------------
# classification: text fallback
# ---------------------------------------------------------------------------


def test_text_timeout_is_transient() -> None:
    err = classify_tool_error(tool_name="api", error=RuntimeError("upstream timed out"))
    assert err.error_class == "transient"


def test_text_503_is_transient() -> None:
    err = classify_tool_error(tool_name="api", error=RuntimeError("HTTP 503 overloaded"))
    assert err.error_class == "transient"


def test_text_unauthorized_is_permission_denied() -> None:
    err = classify_tool_error(tool_name="api", error=RuntimeError("401 Unauthorized"))
    assert err.error_class == "permission_denied"


def test_text_not_found_is_resource_not_found() -> None:
    err = classify_tool_error(tool_name="db", error=RuntimeError("row not found"))
    assert err.error_class == "resource_not_found"


def test_text_invalid_is_invalid_arguments() -> None:
    err = classify_tool_error(tool_name="t", error=RuntimeError("field is required"))
    assert err.error_class == "invalid_arguments"
    assert "Fix the arguments" in err.advice


def test_unrecognised_is_unknown_and_not_retryable() -> None:
    err = classify_tool_error(tool_name="t", error=RuntimeError("weird"))
    assert err.error_class == "unknown"
    assert err.retryable is False


# ---------------------------------------------------------------------------
# CM-B5: retry is capability-bounded
# ---------------------------------------------------------------------------


def test_transient_read_only_is_retryable() -> None:
    err = classify_tool_error(tool_name="t", error=TimeoutError("x"), spec=_spec(read_only=True))
    assert err.retryable is True
    assert "safe to retry" in err.advice


def test_transient_idempotent_is_retryable() -> None:
    err = classify_tool_error(tool_name="t", error=TimeoutError("x"), spec=_spec(idempotent=True))
    assert err.retryable is True


def test_transient_non_idempotent_is_not_retryable() -> None:
    err = classify_tool_error(
        tool_name="send", error=TimeoutError("x"), spec=_spec(idempotent=False)
    )
    assert err.retryable is False
    assert "verify the current state" in err.advice.lower()


def test_transient_without_spec_is_not_retryable() -> None:
    # No spec → cannot prove the replay is safe → conservative.
    err = classify_tool_error(tool_name="t", error=TimeoutError("x"))
    assert err.retryable is False


def test_non_transient_never_retryable_even_if_read_only() -> None:
    err = classify_tool_error(
        tool_name="t", error=FileNotFoundError("/x"), spec=_spec(read_only=True)
    )
    assert err.retryable is False


# ---------------------------------------------------------------------------
# summary truncation
# ---------------------------------------------------------------------------


def test_long_summary_is_truncated() -> None:
    err = classify_tool_error(tool_name="t", error=RuntimeError("x" * 400))
    assert err.summary.endswith("...[truncated]")
    assert len(err.summary) < 400


def test_empty_message_falls_back_to_type_name() -> None:
    err = classify_tool_error(tool_name="t", error=ValueError())
    assert err.summary == "ValueError"


# ---------------------------------------------------------------------------
# CM-B2: mutation_not_landed factory (L-4 convergence, success-path)
# ---------------------------------------------------------------------------


def test_mutation_not_landed_factory() -> None:
    err = classified_mutation_not_landed(
        tool_name="save_artifact", summary="disk full", path="report.md"
    )
    assert err.error_class == "mutation_not_landed"
    assert err.retryable is True
    assert err.path == "report.md"
    assert err.summary == "disk full"
    assert "did NOT land" in err.advice


# ---------------------------------------------------------------------------
# render_recovery_advisory
# ---------------------------------------------------------------------------


def test_render_empty_is_blank() -> None:
    assert render_recovery_advisory([]) == ""


def test_render_wraps_and_lists_each_failure() -> None:
    failures = [
        ClassifiedToolError(
            tool_name="read_file",
            error_class="resource_not_found",
            summary="no such file",
            retryable=False,
            advice="Verify it exists.",
        ),
        ClassifiedToolError(
            tool_name="fetch",
            error_class="transient",
            summary="timed out",
            retryable=True,
            advice="Safe to retry once.",
        ),
    ]
    out = render_recovery_advisory(failures)
    assert out.startswith("<recovery-advisory>")
    assert out.rstrip().endswith("</recovery-advisory>")
    assert "- read_file [resource_not_found]: no such file → Verify it exists." in out
    assert "- fetch [transient]: timed out → Safe to retry once." in out


def test_render_includes_path_when_present() -> None:
    out = render_recovery_advisory(
        [
            ClassifiedToolError(
                tool_name="save_artifact",
                error_class="mutation_not_landed",
                summary="disk full",
                retryable=True,
                advice="Retry or surface.",
                path="report.md",
            )
        ]
    )
    assert (
        "- save_artifact [mutation_not_landed] path=report.md: disk full → Retry or surface." in out
    )


def test_render_single_failure_one_line() -> None:
    out = render_recovery_advisory(
        [
            ClassifiedToolError(
                tool_name="t",
                error_class="unknown_tool",
                summary="nope",
                retryable=False,
                advice="Pick a real tool.",
            )
        ]
    )
    body = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(body) == 1


# ---------------------------------------------------------------------------
# B-84 — "the call never got to look" must not read as "the thing is gone"
# ---------------------------------------------------------------------------

#: The literal supervisor error from run ``7fad305b`` (test environment,
#: 2026-09-16). Kept verbatim: the hyphen in "Time-out" and the absence of
#: 504 from the status list are exactly why this used to classify ``unknown``.
_SANDBOX_504 = (
    "sandbox create failed: 504: b'<html>\\r\\n<head><title>504 Gateway "
    "Time-out</title></head>\\r\\n<body><center><h1>504 Gateway Time-out"
    "</h1></center></body>\\r\\n</html>'"
)


def test_sandbox_504_is_transient_not_unknown() -> None:
    c = classify_tool_error(tool_name="list_dir", error=Exception(_SANDBOX_504))
    assert c.error_class == "transient"


def test_bare_gateway_time_out_wording_is_transient() -> None:
    """nginx spells it "Time-out"; the ``timeout`` needle alone misses it."""
    c = classify_tool_error(tool_name="list_dir", error=Exception("Gateway Time-out"))
    assert c.error_class == "transient"


def test_transient_advice_denies_the_not_found_inference() -> None:
    c = classify_tool_error(
        tool_name="list_dir", error=Exception(_SANDBOX_504), spec=_spec(read_only=True)
    )
    assert "NOTHING about whether the target exists" in c.advice
    assert "do not recreate it from scratch" in c.advice.lower()


def test_unsafe_transient_advice_also_denies_it() -> None:
    c = classify_tool_error(tool_name="write_file", error=Exception(_SANDBOX_504))
    assert c.retryable is False
    assert "NOTHING about whether the target exists" in c.advice


def test_unknown_advice_denies_the_not_found_inference() -> None:
    c = classify_tool_error(tool_name="t", error=Exception("something weird happened"))
    assert c.error_class == "unknown"
    assert "NOTHING about whether the target exists" in c.advice


def test_permission_denied_advice_denies_it_too() -> None:
    c = classify_tool_error(tool_name="t", error=PermissionError("nope"))
    assert c.error_class == "permission_denied"
    assert "NOTHING about whether the target exists" in c.advice


def test_resource_not_found_does_not_carry_the_disclaimer() -> None:
    """The one class that DOES license "it is not there" must stay clean —
    otherwise the disclaimer is noise and the model learns to ignore it."""
    c = classify_tool_error(tool_name="t", error=FileNotFoundError("gone"))
    assert c.error_class == "resource_not_found"
    assert "NOTHING about whether the target exists" not in c.advice
