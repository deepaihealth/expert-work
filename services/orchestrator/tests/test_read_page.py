"""``read_page`` —— 按需渲染文档单页。"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from orchestrator.tools.read_page import (
    MAX_PAGES_PER_CALL,
    RENDER_DPI,
    ReadPageTool,
    build_render_wrapper,
)
from orchestrator.tools.registry import ToolContext
from orchestrator.tools.sandbox import RecordingSandboxRuntime, SandboxOutcome


def _ctx() -> ToolContext:
    return ToolContext(tenant_id=uuid4(), run_id=uuid4(), user_id=uuid4())


def test_render_dpi_keeps_every_page_under_the_vision_limit() -> None:
    """100 dpi 下四种页型都 ≤1.15 MP 且长边 ≤1568 —— 150 dpi 全超线。"""
    geometries = {
        "pptx 16:9": (13.333, 7.5),
        "pptx 4:3": (10.0, 7.5),
        "docx Letter": (8.5, 11.0),
        "docx A4": (8.27, 11.69),
    }
    for name, (w_in, h_in) in geometries.items():
        w, h = round(w_in * RENDER_DPI), round(h_in * RENDER_DPI)
        assert w * h <= 1_150_000, f"{name} 超过 1.15 MP"
        assert max(w, h) <= 1568, f"{name} 长边超过 1568"


# ---------------------------------------------------------------------------
# 沙箱片段真跑 —— 与 test_document_figures.py:44 同一房规:用 exec(compile(...))
# 真跑生成的片段,而不是在源码字符串里断言子串。这里额外伪造 soffice /
# pdftoppm 到 PATH 最前面,让片段的 subprocess.run 调用真正落到桩程序上。
# ---------------------------------------------------------------------------


def _install_fake_office_binaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """伪造 soffice / pdftoppm。

    soffice 桩把源文件的完整 basename(含扩展名)写进它产出的 pdf 内容里 ——
    如果两次转换共用同一个 outdir,后一次会覆盖前一次的 pdf 文件,届时读到的
    内容会变成"另一个源文件"的标记而不是自己的,借此可以检出静默覆盖
    (spec §13.1 #5)。

    pdftoppm 桩固定按 2 位宽度补零产出(第 3 页 -> page-03.jpg),模拟真实
    pdftoppm 按总页数补零的行为;它把读到的 pdf 内容原样带进 jpg,让上面那条
    覆盖检测能一路传到最终产物。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    soffice = bin_dir / "soffice"
    soffice.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        'outdir = args[args.index("--outdir") + 1]\n'
        "src = args[-1]\n"
        "os.makedirs(outdir, exist_ok=True)\n"
        "base = os.path.basename(src)\n"
        "name = os.path.splitext(base)[0]\n"
        'with open(os.path.join(outdir, name + ".pdf"), "w") as fh:\n'
        '    fh.write("PDF-FROM:" + base)\n'
    )
    pdftoppm = bin_dir / "pdftoppm"
    pdftoppm.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        'unit = int(args[args.index("-f") + 1])\n'
        "pdf_path, prefix = args[-2], args[-1]\n"
        "with open(pdf_path) as fh:\n"
        "    pdf_content = fh.read()\n"
        'with open(f"{prefix}-{unit:02d}.jpg", "w") as fh:\n'
        '    fh.write(f"JPEG-PAGE-{unit}::{pdf_content}")\n'
    )
    for script in (soffice, pdftoppm):
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


def _run_render(tmp_path: Path, rel: str, *, units: list[int], out_rel: str) -> dict:
    """本地真跑渲染片段(与 test_document_figures._run 同一房规)。"""
    code = build_render_wrapper(rel, units=units, ws=str(tmp_path), out_rel=out_rel, dpi=RENDER_DPI)
    ns: dict = {}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(code, "<snippet>", "exec"), ns)  # noqa: S102
    return json.loads(buf.getvalue())


def test_wrapper_uses_a_private_outdir_per_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec §13.1 #5 回归钉:report.docx 与 report.pptx 同名不同扩展名,各自的
    中间 pdf 必须落在各自的 out_dir 下 —— 真跑桩程序后数盘面上有几份 pdf:
    私有 outdir 时两份都幸存;共用一个 outdir 时后一份会覆盖前一份,只剩一份。
    """
    _install_fake_office_binaries(tmp_path, monkeypatch)
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "report.docx").write_text("docx source")
    (tmp_path / "uploads" / "report.pptx").write_text("pptx source")

    env_a = _run_render(
        tmp_path, "uploads/report.docx", units=[1], out_rel=".tool_results/r1/figures/aaa"
    )
    env_b = _run_render(
        tmp_path, "uploads/report.pptx", units=[1], out_rel=".tool_results/r1/figures/bbb"
    )
    assert env_a["ok"] is True
    assert env_b["ok"] is True

    pdfs = sorted(tmp_path.glob("**/*.pdf"))
    assert len(pdfs) == 2, f"两份源文档的中间 pdf 应该各自幸存,实际:{pdfs}"
    contents = {p.read_text() for p in pdfs}
    assert contents == {"PDF-FROM:report.docx", "PDF-FROM:report.pptx"}


def test_wrapper_globs_instead_of_building_the_page_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec §13.1 #6 回归钉:pdftoppm 按总页数补零,第 3 页出 page-03.jpg 而不是
    page-3.jpg —— 真跑桩程序(固定 2 位补零),拼文件名的实现会找不到文件。
    """
    _install_fake_office_binaries(tmp_path, monkeypatch)
    (tmp_path / "d.pdf").write_text("fake pdf bytes")  # 已是 pdf,跳过 soffice 转换

    env = _run_render(tmp_path, "d.pdf", units=[3], out_rel=".tool_results/r1/figures/s")

    assert env["ok"] is True
    rel = env["rendered"][0]["rel"]
    assert rel.endswith("page-03.jpg")
    content = (tmp_path / rel).read_text()
    assert "JPEG-PAGE-3::" in content


def test_wrapper_renders_jpeg_not_png() -> None:
    code = build_render_wrapper(
        "d.pptx", units=[1], ws="/workspace", out_rel=".tool_results/r1/figures/s", dpi=RENDER_DPI
    )
    assert "-jpeg" in code
    assert "-png" not in code


@pytest.mark.anyio
async def test_too_many_units_is_refused_with_a_reason() -> None:
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(stdout="{}", stderr="", exit_code=0, timed_out=False)
    )
    tool = ReadPageTool(client=runtime)
    result = await tool.call(
        {"path": "d.pptx", "units": list(range(1, MAX_PAGES_PER_CALL + 5))}, ctx=_ctx()
    )
    assert "一次最多" in result.content
    assert result.state_updates.get("viewed_figures", []) == []


@pytest.mark.anyio
async def test_rendered_pages_become_refs_in_state() -> None:
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps(
                {
                    "ok": True,
                    "rendered": [
                        {
                            "unit": 3,
                            "rel": ".tool_results/r1/figures/abc/page-03.jpg",
                            "bytes": 61000,
                        }
                    ],
                }
            ),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call({"path": "d.pptx", "units": [3]}, ctx=_ctx())
    refs = result.state_updates["viewed_figures"]
    assert len(refs) == 1
    assert refs[0].startswith("expert_work://workspace/")
    assert "page-03.jpg" in refs[0]


@pytest.mark.anyio
async def test_soffice_missing_is_undetermined_not_empty() -> None:
    runtime = RecordingSandboxRuntime(
        SandboxOutcome(
            stdout=json.dumps({"ok": False, "error": "soffice_missing"}),
            stderr="",
            exit_code=0,
            timed_out=False,
        )
    )
    result = await ReadPageTool(client=runtime).call({"path": "d.pptx", "units": [1]}, ctx=_ctx())
    assert "无法" in result.content
    assert result.state_updates.get("viewed_figures", []) == []
