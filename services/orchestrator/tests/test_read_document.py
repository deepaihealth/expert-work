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
from uuid import UUID, uuid4

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


def _ctx(*, agent_key: str = "") -> ToolContext:
    return ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=uuid4(), agent_key=agent_key)


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
    # B-64 —— 两段式:正文 + 图清单探测各一次 sandbox round-trip。
    assert len(client.execs) == 2
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


# ---------------------------------------------------------------------------
# B-50 Task 7 —— ``read_document`` 也读用户工作区。PR3 的计划漏登记了它
# (实测七个工作区调用点,计划只列了四个),所以补齐并钉住。
# ---------------------------------------------------------------------------


class _SequenceRuntime(RecordingSandboxRuntime):
    def __init__(self, stdouts: list[str]) -> None:
        super().__init__()
        self._stdouts = list(stdouts)

    async def exec(  # type: ignore[override]
        self,
        *,
        sandbox_id: Any,
        code: str,
        timeout_s: int | None,
        agent_key: str = "",
        run_id: UUID | None = None,
    ) -> SandboxOutcome:
        self.execs.append((sandbox_id, code))
        self.exec_agent_keys.append(agent_key)
        self.exec_run_ids.append(run_id)
        stdout = self._stdouts.pop(0) if self._stdouts else ""
        return SandboxOutcome(stdout=stdout, stderr="", exit_code=0, timed_out=False)


async def test_read_document_resolves_under_agent_root() -> None:
    """B-60 —— 「agent 根」现在就是视图根:绑了 agent 时 ``ws`` 是 ``/workspace``,
    不再拼 ``agents/<key>``。"""
    client = _client(json.dumps({"ok": True, "content": "x", "format": "pdf"}))
    await ReadDocumentTool(client=client).call(
        {"path": "报告.docx"}, ctx=_ctx(agent_key="plan-aaaaaaaa")
    )
    assert '"ws": "/workspace",' in client.execs[-1][1]


async def test_read_document_never_reaches_the_user_root() -> None:
    """PR6(Task 14)—— 搬迁跑完之后,读文档这条路也不许回落用户根。

    桩里第二个 outcome 是「用户根上有这份文档」的诱饵:回落还在的话这条会拿到
    ``legacy`` 而不是报错。这个工具在 PR3 的计划里**被漏登记过一次**(计划写四个
    工作区调用点,实测七个),所以摘回落时也要单独钉一遍,不能只钉 file_ops。
    """
    client = _SequenceRuntime(
        [
            json.dumps({"ok": False, "error": "not_found"}),
            json.dumps({"ok": True, "content": "legacy", "format": "pdf"}),
        ]
    )
    with pytest.raises(FileOpError):
        await ReadDocumentTool(client=client).call(
            {"path": "报告.docx"}, ctx=_ctx(agent_key="plan-aaaaaaaa")
        )
    assert len(client.execs) == 1, "回落被加回来了:多跑了一次 exec"
    assert '"ws": "/workspace",' in client.execs[0][1]


async def test_read_document_refuses_another_agents_path() -> None:
    with pytest.raises(ValueError, match="reserved layout segment"):
        await ReadDocumentTool(client=_client()).call(
            {"path": "agents/sop-bbbbbbbb/报告.docx"}, ctx=_ctx(agent_key="plan-aaaaaaaa")
        )


# ---------------------------------------------------------------------------
# B-64 —— 图清单接入 read_document:正文 + 清单两段式,清单前置且失败不拖垮正文。
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_content_is_prefixed_with_the_figure_map() -> None:
    """清单必须在头部 —— content 是 text[:cap] 硬截断,尾部会被砍掉。"""
    runtime = _SequenceRuntime(
        [
            json.dumps(
                {
                    "ok": True,
                    "content": "正文" * 100,
                    "format": "pptx",
                    "chars": 200,
                    "truncated": False,
                }
            ),
            json.dumps(
                {
                    "ok": True,
                    "state": "figures",
                    "format": "pptx",
                    "skipped_decorative": 0,
                    "figures": [
                        {
                            "unit": 3,
                            "kind": "picture",
                            "count": 1,
                            "w_pt": 288.0,
                            "h_pt": 192.0,
                            "anchor": "趋势",
                            "alt": None,
                            "title": "趋势",
                        }
                    ],
                }
            ),
        ]
    )
    tool = ReadDocumentTool(client=runtime)
    result = await tool.call({"path": "d.pptx"}, ctx=_ctx())
    head = result.content[:300]
    assert "read_page" in head
    assert result.content.index("read_page") < result.content.index("正文")
    assert result.meta["figures"] == 1
    assert result.meta["figures_state"] == "figures"


@pytest.mark.anyio
async def test_no_figures_leaves_content_byte_identical() -> None:
    """零图路径不许被碰 —— 这是「不回归」的钉子。"""
    body = "纯文字文档"
    runtime = _SequenceRuntime(
        [
            json.dumps(
                {
                    "ok": True,
                    "content": body,
                    "format": "docx",
                    "chars": len(body),
                    "truncated": False,
                }
            ),
            json.dumps({"ok": True, "state": "none", "figures": []}),
        ]
    )
    result = await ReadDocumentTool(client=runtime).call({"path": "d.docx"}, ctx=_ctx())
    assert result.content == body
    assert result.meta["figures"] == 0


@pytest.mark.anyio
async def test_inventory_failure_says_undetermined_not_silent() -> None:
    """清单探测本身失败 → 正文照给,但必须显式说「测不了」。"""
    runtime = _SequenceRuntime(
        [
            json.dumps(
                {
                    "ok": True,
                    "content": "正文",
                    "format": "pptx",
                    "chars": 2,
                    "truncated": False,
                }
            ),
            "",
        ]
    )
    result = await ReadDocumentTool(client=runtime).call({"path": "d.pptx"}, ctx=_ctx())
    assert "无法确定" in result.content
    assert result.meta["figures_state"] == "undetermined"
    assert "正文" in result.content


@pytest.mark.anyio
async def test_text_format_skips_the_probe_entirely() -> None:
    """扩展名早退 —— txt 连 OOXML zip 都不是,答案能白算,不该再起一次沙箱。

    这是「零图路径零额外开销」的钉子:去掉早退这条判断,``call`` 会对
    ``.md`` 也去跑一次图清单探测,这里的 ``execs`` 就会从 1 变成 2。
    """
    body = "普通的笔记文字"
    runtime = _SequenceRuntime(
        [
            json.dumps(
                {
                    "ok": True,
                    "content": body,
                    "format": "md",
                    "chars": len(body),
                    "truncated": False,
                }
            ),
        ]
    )
    result = await ReadDocumentTool(client=runtime).call({"path": "notes.md"}, ctx=_ctx())
    assert len(runtime.execs) == 1
    assert result.content == body
    assert result.meta["figures"] == 0
    assert result.meta["figures_state"] == "none"


@pytest.mark.anyio
async def test_supported_format_still_runs_the_probe() -> None:
    """反向钉子 —— pptx 这类真能分析的格式,早退闸门不能连它一起挡了。"""
    runtime = _SequenceRuntime(
        [
            json.dumps(
                {
                    "ok": True,
                    "content": "正文",
                    "format": "pptx",
                    "chars": 2,
                    "truncated": False,
                }
            ),
            json.dumps({"ok": True, "state": "none", "figures": []}),
        ]
    )
    await ReadDocumentTool(client=runtime).call({"path": "d.pptx"}, ctx=_ctx())
    assert len(runtime.execs) == 2
