"""Unit tests for tool-result overflow helpers (Stream CM-5, PR1).

Pure helpers only — the tools-node wiring (workspace write + footer
append) is covered by the PR2 integration tests. The per-tool
``full_content`` contract is asserted alongside each tool's own
truncation tests (test_exec_python_tool / test_http_tool /
test_mcp_tool).
"""

from __future__ import annotations

from uuid import UUID

from orchestrator.context.tool_result_prune import _footer_claimed_path, _footer_total_chars
from orchestrator.tools.overflow import (
    OVERFLOW_DIR,
    OVERFLOW_MAX_CHARS,
    clamp_overflow,
    overflow_rel_path,
    render_overflow_footer,
)

_RUN_ID = UUID("00000000-0000-0000-0000-000000000042")


# ---------------------------------------------------------------------------
# overflow_rel_path
# ---------------------------------------------------------------------------


def test_rel_path_groups_by_run_and_names_by_call_and_tool() -> None:
    rel = overflow_rel_path(run_id=_RUN_ID, call_id="call_abc123", tool_name="bash")
    assert rel == f"{OVERFLOW_DIR}/{_RUN_ID}/call_abc123-bash.txt"


def test_rel_path_without_run_id_falls_back_to_adhoc() -> None:
    rel = overflow_rel_path(run_id=None, call_id="c1", tool_name="http")
    assert rel == f"{OVERFLOW_DIR}/adhoc/c1-http.txt"


def test_rel_path_sanitizes_hostile_components() -> None:
    # call_id comes from the model provider — a traversal attempt must
    # never become a path segment.
    rel = overflow_rel_path(run_id=_RUN_ID, call_id="../../etc/passwd", tool_name="a/b")
    assert ".." not in rel
    # Only the directory separators of the fixed layout survive.
    assert rel.count("/") == 2
    assert rel.startswith(f"{OVERFLOW_DIR}/{_RUN_ID}/")


def test_rel_path_clamps_overlong_components() -> None:
    rel = overflow_rel_path(run_id=None, call_id="c" * 500, tool_name="t")
    filename = rel.rsplit("/", 1)[1]
    assert len(filename) < 200


def test_rel_path_empty_call_id_never_yields_empty_component() -> None:
    rel = overflow_rel_path(run_id=None, call_id="", tool_name="bash")
    assert rel == f"{OVERFLOW_DIR}/adhoc/unknown-bash.txt"


# ---------------------------------------------------------------------------
# clamp_overflow
# ---------------------------------------------------------------------------


def test_clamp_under_cap_returns_input_unchanged() -> None:
    payload = "x" * 1_000
    assert clamp_overflow(payload) is payload


def test_clamp_over_cap_keeps_head_and_appends_note() -> None:
    payload = "x" * (OVERFLOW_MAX_CHARS + 10)
    clamped = clamp_overflow(payload)
    assert clamped.startswith("x" * 100)
    assert clamped.endswith(f"[overflow file truncated at {OVERFLOW_MAX_CHARS} chars]")
    assert len(clamped) < OVERFLOW_MAX_CHARS + 100


# ---------------------------------------------------------------------------
# render_overflow_footer
# ---------------------------------------------------------------------------


def test_footer_references_path_and_size_inside_tagged_block() -> None:
    rel = f"{OVERFLOW_DIR}/adhoc/c1-bash.txt"
    footer = render_overflow_footer(rel=rel, total_chars=123_456)
    assert footer.startswith("\n\n<tool-result-overflow>")
    assert footer.rstrip().endswith("</tool-result-overflow>")
    assert rel in footer
    assert "123456 chars" in footer
    assert "read_file" in footer


def test_footer_tells_the_model_it_can_page_with_read_file() -> None:
    # B-136 —— footer 指给模型的工具必须真能分页读, 并且把参数名说出来。
    footer = render_overflow_footer(rel=f"{OVERFLOW_DIR}/adhoc/c1-bash.txt", total_chars=50_000)
    assert "read_file (offset / limit)" in footer
    assert "exec_python / bash" in footer


def test_reworded_footer_is_still_parsed_by_the_prune_gate() -> None:
    # CM-12 的 prune gate 从 footer 文案里正则抠路径和原始长度 —— 改措辞不能把它抠瞎。
    rel = f"{OVERFLOW_DIR}/adhoc/c1-bash.txt"
    content = "preview" + render_overflow_footer(rel=rel, total_chars=123_456)
    assert _footer_claimed_path(content) == rel
    assert _footer_total_chars(content) == 123_456
