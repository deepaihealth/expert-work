"""Tests for ``read_document`` — Tier 1 document-parsing base capability.

Two layers, mirroring test_file_ops:
  1. In-sandbox parse snippet, executed locally against a temp workspace.
     Text formats are stdlib; binary formats ``importorskip`` their parser
     (present in the sandbox image + the CI shared venv).
  2. The ``ReadDocumentTool`` envelope → :class:`ToolResult` mapping, driven
     by a :class:`RecordingSandboxRuntime` returning a canned envelope.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from orchestrator.tools.file_ops import FileOpError
from orchestrator.tools.read_document import (
    ReadDocumentTool,
    build_read_document_wrapper,
)
from orchestrator.tools.registry import ToolBlockedError, ToolContext
from orchestrator.tools.sandbox import RecordingSandboxRuntime, SandboxOutcome

# ---------------------------------------------------------------------------
# Layer 1 — in-sandbox parse snippet (run locally with ws = tmp_path)
# ---------------------------------------------------------------------------


def _run(code: str) -> dict[str, Any]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(code, {})  # noqa: S102 — snippet is built from a fixed template
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def _read(rel: str, ws: Path, **kw: Any) -> dict[str, Any]:
    return _run(build_read_document_wrapper(rel, cap=kw.pop("cap", 100_000), ws=str(ws), **kw))


@pytest.mark.parametrize("ext", [".txt", ".md", ".csv", ".json", ".log"])
def test_text_formats_extracted(tmp_path: Path, ext: str) -> None:
    body = "line one\nline two 日本語"
    (tmp_path / f"f{ext}").write_text(body, encoding="utf-8")
    out = _read(f"f{ext}", tmp_path)
    assert out["ok"] is True
    assert out["content"] == body
    assert out["format"] == ext.lstrip(".")
    assert out["truncated"] is False


def test_cap_truncates_but_reports_full_char_count(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("x" * 100, encoding="utf-8")
    out = _read("big.txt", tmp_path, cap=10)
    assert out["content"] == "x" * 10
    assert out["truncated"] is True
    assert out["chars"] == 100


def test_xlsx_extracted(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["name", "age"])
    ws.append(["alice", 30])
    wb.save(tmp_path / "data.xlsx")
    out = _read("data.xlsx", tmp_path)
    assert out["ok"] is True
    assert out["format"] == "xlsx"
    assert "Sheet1" in out["content"]
    assert "alice" in out["content"]
    assert "30" in out["content"]


def test_pptx_extracted(tmp_path: Path) -> None:
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])  # title-only layout
    slide.shapes.title.text = "Quarterly Review"
    prs.save(tmp_path / "deck.pptx")
    out = _read("deck.pptx", tmp_path)
    assert out["ok"] is True
    assert out["format"] == "pptx"
    assert "Quarterly Review" in out["content"]


def test_docx_extracted(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_paragraph("First paragraph.")
    doc.add_paragraph("Second paragraph.")
    doc.save(tmp_path / "memo.docx")
    out = _read("memo.docx", tmp_path)
    assert out["ok"] is True
    assert out["format"] == "docx"
    assert "First paragraph." in out["content"]
    assert "Second paragraph." in out["content"]


def test_corrupt_binary_returns_parse_failed_not_crash(tmp_path: Path) -> None:
    pytest.importorskip("pdfplumber")
    (tmp_path / "broken.pdf").write_bytes(b"not a real pdf")
    out = _read("broken.pdf", tmp_path)
    assert out["ok"] is False
    assert out["error"] == "parse_failed"


def test_unsupported_format(tmp_path: Path) -> None:
    (tmp_path / "f.bin").write_bytes(b"\x00\x01\x02")
    out = _read("f.bin", tmp_path)
    assert out == {"ok": False, "error": "unsupported_format", "format": "bin"}


def test_not_found(tmp_path: Path) -> None:
    out = _read("missing.pdf", tmp_path)
    assert out == {"ok": False, "error": "not_found"}


def test_path_escape_denied(tmp_path: Path) -> None:
    out = _read("../secret.txt", tmp_path)
    assert out == {"ok": False, "error": "path_escapes_workspace"}


def test_oversized_rejected_before_parse(tmp_path: Path) -> None:
    (tmp_path / "huge.txt").write_text("x" * 50, encoding="utf-8")
    out = _read("huge.txt", tmp_path, max_bytes=10)
    assert out["ok"] is False
    assert out["error"] == "file_too_large"


# ---------------------------------------------------------------------------
# Layer 2 — ReadDocumentTool envelope → ToolResult mapping
# ---------------------------------------------------------------------------


def _ctx() -> ToolContext:
    return ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=uuid4())


def _client(stdout: str = "", *, exit_code: int = 0) -> RecordingSandboxRuntime:
    client = RecordingSandboxRuntime()
    client.outcome = SandboxOutcome(stdout=stdout, stderr="", exit_code=exit_code, timed_out=False)
    return client


async def test_tool_parses_envelope_into_result() -> None:
    env = {"ok": True, "content": "doc text", "format": "pdf", "chars": 8, "truncated": False}
    client = _client(json.dumps(env))
    result = await ReadDocumentTool(client=client).call({"path": "report.pdf"}, ctx=_ctx())
    assert result.content == "doc text"
    assert result.meta["format"] == "pdf"
    assert result.meta["chars"] == 8
    assert result.meta["path"] == "report.pdf"
    assert len(client.execs) == 1
    assert client.released


async def test_tool_spec_is_read_only() -> None:
    spec = ReadDocumentTool(client=_client()).spec
    assert spec.name == "read_document"
    assert spec.is_read_only is True
    assert spec.side_effect == "read_only"


async def test_tool_path_escape_raises_blocked() -> None:
    client = _client(json.dumps({"ok": False, "error": "path_escapes_workspace"}))
    with pytest.raises(ToolBlockedError):
        await ReadDocumentTool(client=client).call({"path": "x.pdf"}, ctx=_ctx())


async def test_tool_unsupported_raises_fileop() -> None:
    client = _client(json.dumps({"ok": False, "error": "unsupported_format", "format": "bin"}))
    with pytest.raises(FileOpError, match="unsupported_format"):
        await ReadDocumentTool(client=client).call({"path": "x.bin"}, ctx=_ctx())
